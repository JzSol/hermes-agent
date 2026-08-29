"""Honesty-preserving text rendering for watchlist output."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Optional

from hermes_time import now as _hermes_now
from watchlist.config import load_config
from watchlist.scoring import UNKNOWN, is_rug_shape

MAX_PLATFORM_OUTPUT = 4000

# No-agent cron output still passes through the normal platform delivery
# pipeline.  That pipeline intentionally treats these tokens as out-of-band
# control directives, so source-controlled project names and URLs must never
# be allowed to emit them verbatim.  Keep this guard at the final rendering
# boundary as defense in depth for every current and future source field.
_MEDIA_DIRECTIVE_RE = re.compile(r"media[ \t]*:", re.IGNORECASE)
_INTERNAL_DIRECTIVE_RE = re.compile(
    r"\[\[(?:audio_as_voice|as_document)\]\]|\[silent\]", re.IGNORECASE
)
_BARE_SILENCE_LINE_RE = re.compile(
    r"^(?P<leading>[ \t]*)(?P<token>SILENT|NO_REPLY|NO[ \t]+REPLY)"
    r"(?P<trailing>[ \t]*)$",
    re.IGNORECASE,
)


def _neutralize_bare_silence_lines(text: str) -> str:
    rendered: list[str] = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        match = _BARE_SILENCE_LINE_RE.fullmatch(content)
        if match is None:
            rendered.append(line)
            continue
        rendered.append(
            f"{match.group('leading')}［{match.group('token')}］"
            f"{match.group('trailing')}{ending}"
        )
    return "".join(rendered)


def sanitize_delivery_text(value: Any) -> str:
    """Render untrusted text without activating Hermes delivery directives."""

    text = str(value)
    text = _MEDIA_DIRECTIVE_RE.sub(lambda match: match.group(0)[:-1] + "：", text)
    text = _INTERNAL_DIRECTIVE_RE.sub(
        lambda match: "［" + match.group(0)[1:-1] + "］", text
    )
    return _neutralize_bare_silence_lines(text)


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _watchlist_config(cfg: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if cfg is None:
        return load_config()
    if "watchlist" in cfg and "shortlist_limit" not in cfg:
        return load_config(cfg)
    return dict(cfg)


def _coverage(row: Any, axis: str) -> Optional[float]:
    value = _value(row, f"{axis}_coverage")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _score(row: Any, axis: str) -> Optional[float]:
    value = _value(row, f"{axis}_score")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _axis_eligible(row: Any, axis: str, min_coverage: float) -> bool:
    """Whether an axis is covered well enough to rank, filter, or judge on.

    One predicate, used by the partition, the label, and the rug verdict — so
    a row can never be ranked by a number it is not allowed to display.
    """

    score = _score(row, axis)
    coverage = _coverage(row, axis)
    return score is not None and coverage is not None and coverage >= min_coverage


def _score_label(row: Any, axis: str, min_coverage: float) -> str:
    score = _score(row, axis)
    coverage = _coverage(row, axis)
    if score is None or coverage is None or coverage < min_coverage:
        return "INSUFFICIENT DATA"
    stage = _value(row, "score_stage")
    # A scan score was never checked for an audit; a deep one was.  Rendering
    # them identically would let thin-but-covered evidence read as verified.
    label = {"scan": "scan", "deep": "verified"}.get(str(stage), "unrated")
    return (
        f"{int(score) if score.is_integer() else score:.0f} ({coverage:.0%}, {label})"
    )


def _fact_summary(row: Any, axis: str) -> str:
    facts = _value(row, f"{axis}_facts")
    if facts is None:
        return ""
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except (TypeError, ValueError):
            return facts
    if not isinstance(facts, Mapping):
        return str(facts)
    rendered = []
    for key, value in facts.items():
        value_text = "unknown" if value == UNKNOWN else str(value)
        rendered.append(f"{key}={value_text}")
    return ", ".join(rendered)


def _shortlist_row(row: Any, cfg: Mapping[str, Any]) -> str:
    name = _value(row, "name", "Unnamed project")
    ticker = _value(row, "ticker")
    identity = f"{name} ({ticker})" if ticker else str(name)
    # The rug verdict needs adequate coverage on BOTH axes.  Judging it from raw
    # scores let a row print "INSUFFICIENT DATA" and a confident RUG SHAPE in the
    # same line — the honesty contract contradicting itself, and a verdict drawn
    # from the very numbers we just told the reader not to trust.
    min_coverage = cfg["min_coverage"]
    rug = (
        _axis_eligible(row, "risk", min_coverage)
        and _axis_eligible(row, "asym", min_coverage)
        and is_rug_shape(
            _score(row, "risk"),
            _score(row, "asym"),
            asym_min=cfg["rug_shape"]["asym_min"],
            risk_max=cfg["rug_shape"]["risk_max"],
        )
    )
    warning = " ⚠ RUG SHAPE" if rug else ""
    lines = [
        f"• {identity} — risk {_score_label(row, 'risk', cfg['min_coverage'])}; "
        f"asym {_score_label(row, 'asym', cfg['min_coverage'])}{warning}",
    ]
    source = _value(row, "source")
    if source:
        lines.append(f"  source: {source}")
    for axis in ("risk", "asym"):
        facts = _fact_summary(row, axis)
        if facts:
            lines.append(f"  {axis} facts: {facts}")
    url = _value(row, "url")
    if url:
        lines.append(f"  {url}")
    return "\n".join(lines)


def render_shortlist(
    rows: Iterable[Any], cfg: Optional[Mapping[str, Any]] = None
) -> str:
    """Render a bounded shortlist with coverage-aware ranking.

    Rows whose risk axis is below the coverage floor cannot satisfy the risk
    filter. Rows whose selected sort axis is below the floor are displayed in
    a separate insufficient-data block, after all rankable rows.
    """

    config = _watchlist_config(cfg)
    min_coverage = float(config.get("min_coverage", 0.5))
    risk_floor = float(config.get("risk_floor", 40))
    sort_by = config.get("sort_by", "asym")
    candidates: list[Any] = []
    insufficient: list[Any] = []
    for row in rows:
        risk = _score(row, "risk")
        if risk is None or not _axis_eligible(row, "risk", min_coverage):
            insufficient.append(row)
            continue
        if risk < risk_floor:
            continue
        if not _axis_eligible(row, sort_by, min_coverage):
            insufficient.append(row)
            continue
        candidates.append(row)

    candidates.sort(
        key=lambda row: (
            -(_score(row, sort_by) or 0),
            str(_value(row, "name", "")).casefold(),
        )
    )
    # One combined budget: `shortlist_limit` is "rows per digest", so applying it
    # to each block separately silently allowed twice the configured rows — and
    # twice the platform output budget with it.  Ranked rows have first claim.
    limit = max(1, int(config.get("shortlist_limit", 10)))
    candidates = candidates[:limit]
    insufficient = insufficient[: max(0, limit - len(candidates))]

    lines = [
        "IDO watchlist shortlist",
        "Asymmetry is structural room to move, not a prediction.",
    ]
    if candidates:
        lines.append("")
        lines.extend(_shortlist_row(row, config) for row in candidates)
    else:
        lines.append(
            "No projects currently meet the risk floor and coverage requirements."
        )
    if insufficient:
        lines.extend(["", "INSUFFICIENT DATA (not ranked or eligible for the filter)"])
        lines.extend(_shortlist_row(row, config) for row in insufficient)
    output = sanitize_delivery_text("\n".join(lines))
    if len(output) > MAX_PLATFORM_OUTPUT:
        output = output[: MAX_PLATFORM_OUTPUT - 1].rstrip() + "…"
    return output


def _parse_due_at(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _deadline_text(due_at: Any, current: datetime) -> str:
    parsed = _parse_due_at(due_at)
    if parsed is not None:
        compare_now = current
        if parsed.tzinfo is None and current.tzinfo is not None:
            parsed = parsed.replace(tzinfo=current.tzinfo)
        if parsed < compare_now:
            return "this deadline has passed"
    return f"due {due_at}" if due_at else "deadline unknown"


def _checklist_text(item: Any) -> str:
    checklist = _value(item, "checklist")
    if checklist is None:
        checklist = _value(item, "stages")
    if not isinstance(checklist, (list, tuple)):
        state = _value(item, "checklist_state", "pending")
        label = _value(item, "label", "stage")
        current = " ← you are here" if state == "pending" else ""
        return f"{'✓' if state in {'done', 'skipped'} else '✗'} {label}{current}"
    rendered = []
    current_found = False
    for stage in checklist:
        state = _value(stage, "checklist_state", _value(stage, "state", "pending"))
        label = _value(stage, "label", "stage")
        marker = "✓" if state in {"done", "skipped"} else "✗"
        current = " ← you are here" if state == "pending" and not current_found else ""
        current_found = current_found or state == "pending"
        rendered.append(f"{marker} {label}{current}")
    return "  ".join(rendered)


def render_reminders(due: Iterable[Any], now: Optional[datetime] = None) -> str:
    """Render due stages, explicitly labelling stale deadlines."""

    current = now or _hermes_now()
    lines = ["IDO watchlist reminders"]
    rendered_any = False
    for item in due:
        rendered_any = True
        project = _value(item, "project")
        stage = _value(item, "stage")
        subject = stage if stage is not None else item
        project_name = (
            _value(project, "name") or _value(item, "project_name") or "Project"
        )
        label = _value(subject, "label", "stage")
        due_at = _value(subject, "due_at", _value(item, "due_at"))
        lines.append(f"• {project_name} — {label}: {_deadline_text(due_at, current)}")
        lines.append(
            f"  checklist: {_checklist_text(item if _value(item, 'checklist') is not None else subject)}"
        )
        url = _value(subject, "url", _value(item, "url"))
        if url:
            lines.append(f"  {url}")
        next_stage = _value(item, "next_stage")
        if next_stage is None:
            stages = _value(item, "stages")
            if isinstance(stages, (list, tuple)):
                pending_indexes = [
                    index
                    for index, stage_item in enumerate(stages)
                    if _value(
                        stage_item,
                        "checklist_state",
                        _value(stage_item, "state", "pending"),
                    )
                    == "pending"
                ]
                if pending_indexes:
                    next_index = pending_indexes[0] + 1
                    if next_index < len(stages):
                        next_stage = stages[next_index]
        if next_stage is not None:
            lines.append(f"  next: {_value(next_stage, 'label', str(next_stage))}")
    if not rendered_any:
        lines.append("No watchlist reminders are due.")
    output = sanitize_delivery_text("\n".join(lines))
    if len(output) > MAX_PLATFORM_OUTPUT:
        output = output[: MAX_PLATFORM_OUTPUT - 1].rstrip() + "…"
    return output


__all__ = [
    "MAX_PLATFORM_OUTPUT",
    "sanitize_delivery_text",
    "render_shortlist",
    "render_reminders",
]
