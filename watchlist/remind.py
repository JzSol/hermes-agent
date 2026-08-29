"""Hourly, database-only reminder entry point for the IDO watchlist."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from cron import jobs as cron_jobs
from hermes_time import now as _hermes_now
from watchlist.config import load_config
from watchlist.db import (
    Outbox,
    Stage,
    connect,
    create_outbox,
    emit_outbox,
    get_outbox,
    get_project,
    get_stage,
    list_heartbeats,
    list_job_observations,
    list_outbox,
    list_projects,
    list_stages,
    record_heartbeat,
    record_job_observation,
    settle_outbox,
    update_project,
)
from watchlist.render import (
    MAX_PLATFORM_OUTPUT,
    render_reminders,
    sanitize_delivery_text,
)


_MAX_DIGEST_ITEMS = 20
_MAX_DIGEST_DETAIL_CHARS = 300

REMIND_SCRIPT = "ido_remind.py"
SCAN_SCRIPT = "ido_scan.py"
HORIZON_DELTAS = {
    "T-7d": timedelta(days=7),
    "T-24h": timedelta(days=1),
    "T-2h": timedelta(hours=2),
}
HORIZON_ORDER = ("T-7d", "T-24h", "T-2h")
TERMINAL_OUTBOX_STATES = frozenset({"confirmed", "suppressed", "abandoned"})


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    parsed = _parse_datetime(value)
    return parsed.timestamp() if parsed is not None else None


def _script_matches(job: Mapping[str, Any], script: str) -> bool:
    value = str(job.get("script") or "").strip()
    return value == script


def _find_job(
    jobs: Iterable[Mapping[str, Any]], script: str
) -> Mapping[str, Any] | None:
    matches = [job for job in jobs if _script_matches(job, script)]
    # The script field is the stable identity.  If an invalid jobs file has
    # duplicate records, refusing to guess is safer than confirming the wrong
    # outbound run.
    return matches[0] if len(matches) == 1 else None


def _cron_jobs() -> list[Mapping[str, Any]]:
    try:
        return list(cron_jobs.list_jobs())
    except Exception:
        return []


def _delivery_confirmed(job: Mapping[str, Any] | None, emitted_at: int | None) -> bool:
    if job is None or emitted_at is None:
        return False
    last_run = _epoch(job.get("last_run_at"))
    return (
        last_run is not None
        and last_run >= emitted_at
        and job.get("last_status") == "ok"
        and job.get("last_delivery_error") is None
    )


def reconcile_outbox(
    conn: Any,
    jobs: Iterable[Mapping[str, Any]],
    *,
    max_emit_attempts: int,
) -> None:
    """Confirm or requeue rows emitted by the previous script invocation."""

    remind_job = _find_job(jobs, REMIND_SCRIPT)
    groups: dict[str, list[Outbox]] = defaultdict(list)
    for row in list_outbox(conn, state="emitted"):
        groups[row.emit_run_id or row.id].append(row)
    for rows in groups.values():
        confirmed = all(_delivery_confirmed(remind_job, row.emitted_at) for row in rows)
        for row in rows:
            if confirmed:
                settle_outbox(conn, row.id, "confirmed")
            elif row.emit_attempts >= max_emit_attempts:
                settle_outbox(conn, row.id, "abandoned", reason="delivery_unconfirmed")
            else:
                # The following emit_outbox call increments emit_attempts for
                # the retry.  Keeping the transition through db.py preserves
                # the store's emitted -> pending contract.
                settle_outbox(conn, row.id, "pending")


def observe_scan_job(
    conn: Any, jobs: Iterable[Mapping[str, Any]], now: datetime
) -> None:
    """Accumulate cron's scan delivery snapshot without overwriting history."""

    scan_job = _find_job(jobs, SCAN_SCRIPT)
    if scan_job is None:
        return
    last_run_at = scan_job.get("last_run_at")
    if not last_run_at:
        return
    record_job_observation(
        conn,
        job="scan",
        cron_last_run_at=str(last_run_at),
        last_status=str(scan_job.get("last_status") or "unknown"),
        last_delivery_error=scan_job.get("last_delivery_error"),
        observed_at=int(now.timestamp()),
        observed_by="remind",
    )


def _due_horizons(
    stage: Stage, now: datetime, configured: Iterable[str]
) -> tuple[str | None, list[str]]:
    due = _parse_datetime(stage.due_at)
    if due is None:
        return None, []
    if now > due:
        return "passed", ["passed"]
    if now == due:
        return ("due", ["due"]) if "due" in configured else (None, [])
    crossed = [
        horizon
        for horizon in HORIZON_ORDER
        if horizon in configured and now >= due - HORIZON_DELTAS[horizon]
    ]
    if not crossed:
        return None, []
    # HORIZON_ORDER is farthest-to-nearest, so the last crossed horizon is the
    # nearest-to-due one and is the only message emitted after downtime.
    return crossed[-1], crossed


def _dedupe_key(stage: Stage, horizon: str) -> str:
    return f"stage:{stage.id}:r{stage.schedule_revision}:{horizon}"


def _ensure_reminder_row(
    conn: Any, project_id: str, stage: Stage, horizon: str
) -> Outbox:
    outbox_id = create_outbox(
        conn,
        kind="reminder",
        dedupe_key=_dedupe_key(stage, horizon),
        project_id=project_id,
        stage_id=stage.id,
        horizon=horizon,
    )
    row = get_outbox(conn, outbox_id)
    if row is None:
        raise RuntimeError(f"outbox row disappeared: {outbox_id}")
    return row


def _suppress_if_live(conn: Any, row: Outbox, reason: str) -> None:
    if row.state in {"pending", "emitted"}:
        settle_outbox(conn, row.id, "suppressed", reason=reason)


def _emit_if_eligible(
    conn: Any,
    row: Outbox,
    *,
    run_id: str,
    emitted_at: int,
    max_emit_attempts: int,
) -> bool:
    if row.state != "pending":
        return False
    if row.emit_attempts >= max_emit_attempts:
        settle_outbox(conn, row.id, "abandoned", reason="max_emit_attempts")
        return False
    return emit_outbox(
        conn,
        row.id,
        emit_run_id=run_id,
        emitted_at=emitted_at,
    )


def _reminder_item(conn: Any, row: Outbox) -> dict[str, Any] | None:
    if row.stage_id is None or row.project_id is None:
        return None
    stage = get_stage(conn, row.stage_id)
    project = get_project(conn, row.project_id)
    if stage is None or project is None:
        return None
    stages = list_stages(conn, project.id)
    next_stage = None
    for index, candidate in enumerate(stages):
        if candidate.id == stage.id and index + 1 < len(stages):
            next_stage = stages[index + 1]
            break
    return {
        "project": project,
        "stage": stage,
        "stages": stages,
        "next_stage": next_stage,
        "horizon": row.horizon,
    }


def _is_current_revision(row: Outbox, stage: Stage | None) -> bool:
    return stage is not None and row.dedupe_key.startswith(
        f"stage:{stage.id}:r{stage.schedule_revision}:"
    )


def _passed_settled(conn: Any, stage: Stage) -> bool:
    for row in list_outbox(conn, stage_id=stage.id):
        if (
            row.horizon == "passed"
            and _is_current_revision(row, stage)
            and row.state in TERMINAL_OUTBOX_STATES
        ):
            return True
    return False


def _complete_projects(conn: Any, now: datetime) -> None:
    for project in list_projects(conn, status="watching"):
        stages = list_stages(conn, project.id)
        if not stages:
            continue
        complete = True
        for stage in stages:
            if stage.checklist_state in {"done", "skipped"}:
                continue
            due = _parse_datetime(stage.due_at)
            if due is None or due >= now or not _passed_settled(conn, stage):
                complete = False
                break
        if complete:
            update_project(conn, project.id, status="done")


def _observation_in_week(value: str, now: datetime, week_key: str) -> bool:
    parsed = _parse_datetime(value)
    if parsed is None or parsed > now:
        return False
    return parsed.astimezone(now.tzinfo).strftime("%G-W%V") == week_key


def _observation_sort_key(value: str, now: datetime) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        return datetime.min.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def _heartbeat_in_week(ran_at: int, now: datetime, week_key: str) -> bool:
    if ran_at > int(now.timestamp()):
        return False
    return datetime.fromtimestamp(ran_at, tz=now.tzinfo).strftime("%G-W%V") == week_key


def _one_line(value: Any, limit: int = _MAX_DIGEST_DETAIL_CHARS) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded_output(value: str) -> str:
    value = sanitize_delivery_text(value)
    if len(value) <= MAX_PLATFORM_OUTPUT:
        return value
    return value[: MAX_PLATFORM_OUTPUT - 1].rstrip() + "…"


def _digest_text(conn: Any, now: datetime, week_key: str) -> str:
    observations = [
        item
        for item in list_job_observations(conn, job="scan")
        if _observation_in_week(item.cron_last_run_at, now, week_key)
    ]
    scan_heartbeats = [
        item
        for item in list_heartbeats(conn, job="scan")
        if _heartbeat_in_week(item.ran_at, now, week_key)
    ]
    failed_observations = [
        item
        for item in observations
        if item.last_status != "ok" or item.last_delivery_error is not None
    ]
    source_health: dict[str, str] = {}
    for heartbeat in scan_heartbeats:
        heartbeat_detail = heartbeat.detail
        if not isinstance(heartbeat_detail, Mapping):
            continue
        for source in heartbeat_detail.get("sources", []):
            if isinstance(source, Mapping) and source.get("source"):
                source_health.setdefault(
                    _one_line(source["source"], 120),
                    _one_line(source.get("status", "unknown"), 80),
                )
    abandoned: list[Outbox] = []
    for row in list_outbox(conn, state="abandoned"):
        if row.stage_id is None:
            abandoned.append(row)
            continue
        stage = get_stage(conn, row.stage_id)
        if _is_current_revision(row, stage):
            abandoned.append(row)

    no_future: list[str] = []
    for project in list_projects(conn, status="watching"):
        future = False
        for stage in list_stages(conn, project.id):
            if stage.checklist_state in {"done", "skipped"}:
                continue
            due = _parse_datetime(stage.due_at)
            if due is not None and due > now:
                future = True
                break
        if not future:
            no_future.append(project.name)

    lines = [f"IDO watchlist health digest — {week_key}"]
    lines.append(f"Scan heartbeats observed: {len(scan_heartbeats)}")
    if scan_heartbeats:
        successful = [item for item in scan_heartbeats if item.status == "ok"]
        if successful:
            lines.append(f"Last successful scan: {successful[0].ran_at}")
        else:
            lines.append("Last successful scan: none recorded.")
    if source_health:
        lines.append(
            "Source health: "
            + ", ".join(
                f"{source}={status}" for source, status in sorted(source_health.items())
            )
        )
    else:
        lines.append("Source health: none recorded.")
    lines.append(f"Cron scan observations: {len(observations)}")
    if failed_observations:
        lines.append("Scan delivery failures:")
        visible_failures = sorted(
            failed_observations,
            key=lambda value: _observation_sort_key(value.cron_last_run_at, now),
        )[-_MAX_DIGEST_ITEMS:]
        for item in visible_failures:
            error = (
                f"; delivery={_one_line(item.last_delivery_error)}"
                if item.last_delivery_error
                else ""
            )
            lines.append(
                f"• {_one_line(item.cron_last_run_at, 80)}: "
                f"{_one_line(item.last_status, 80)}{error}"
            )
        if len(failed_observations) > len(visible_failures):
            lines.append(
                f"… {len(failed_observations) - len(visible_failures)} more failures omitted"
            )
    else:
        lines.append("Scan delivery failures: none recorded.")
    if abandoned:
        lines.append(f"Abandoned outbound rows: {len(abandoned)}")
    else:
        lines.append("Abandoned outbound rows: none.")
    if no_future:
        visible_projects = no_future[:_MAX_DIGEST_ITEMS]
        suffix = (
            f" (+{len(no_future) - len(visible_projects)} more)"
            if len(no_future) > len(visible_projects)
            else ""
        )
        lines.append(
            "Watched projects with no future reminders: "
            + ", ".join(_one_line(name, 120) for name in visible_projects)
            + suffix
        )
    else:
        lines.append("Watched projects with no future reminders: none.")
    return "\n".join(lines)


def _digest_dedupe_key(conn: Any, now: datetime, week_key: str) -> str:
    """Version a weekly digest when a new scan-delivery failure appears.

    A healthy digest remains once-per-week. If it is delivered before the
    week's first failure, the changed key permits one updated health digest
    instead of letting the terminal outbox row suppress that failure.
    """

    failure_count = sum(
        1
        for item in list_job_observations(conn, job="scan")
        if _observation_in_week(item.cron_last_run_at, now, week_key)
        and (item.last_status != "ok" or item.last_delivery_error is not None)
    )
    return f"digest:{week_key}:failures:{failure_count}"


def run(config: Mapping[str, Any] | None = None) -> str:
    """Reconcile, select, and render one hourly reminder run."""

    cfg = dict(config) if config is not None else load_config()
    now = _hermes_now()
    if now.tzinfo is None:
        raise ValueError("watchlist reminder clock must be timezone-aware")
    run_id = uuid.uuid4().hex
    emitted_at = int(now.timestamp())
    conn = connect()
    heartbeat_status = "ok"
    detail: dict[str, Any] = {}
    output_parts: list[str] = []
    try:
        jobs = _cron_jobs()
        max_attempts = int(cfg.get("max_emit_attempts", 3))
        reconcile_outbox(conn, jobs, max_emit_attempts=max_attempts)
        observe_scan_job(conn, jobs, now)

        emitted_reminders: list[Outbox] = []
        horizons = cfg.get("reminder_horizons", [])
        for project in list_projects(conn, status="watching"):
            for stage in list_stages(conn, project.id, include_completed=False):
                selected, crossed = _due_horizons(stage, now, horizons)
                if selected is None:
                    continue
                rows = {
                    horizon: _ensure_reminder_row(conn, project.id, stage, horizon)
                    for horizon in crossed
                }
                for horizon, row in rows.items():
                    if horizon != selected:
                        _suppress_if_live(conn, row, "horizon_superseded")
                selected_row = rows[selected]
                if _emit_if_eligible(
                    conn,
                    selected_row,
                    run_id=run_id,
                    emitted_at=emitted_at,
                    max_emit_attempts=max_attempts,
                ):
                    refreshed = get_outbox(conn, selected_row.id)
                    if refreshed is not None:
                        emitted_reminders.append(refreshed)

        _complete_projects(conn, now)

        week_key = now.strftime("%G-W%V")
        digest_key = _digest_dedupe_key(conn, now, week_key)
        digest_id = create_outbox(conn, kind="digest", dedupe_key=digest_key)
        digest_row = get_outbox(conn, digest_id)
        digest_output = _digest_text(conn, now, week_key)
        if digest_row is not None and _emit_if_eligible(
            conn,
            digest_row,
            run_id=run_id,
            emitted_at=emitted_at,
            max_emit_attempts=max_attempts,
        ):
            output_parts.append(digest_output)

        if emitted_reminders:
            items = [
                item
                for row in emitted_reminders
                if (item := _reminder_item(conn, row)) is not None
            ]
            if items:
                output_parts.append(render_reminders(items, now=now))
        detail = {
            "emitted_reminder_count": len(emitted_reminders),
            "digest_key": digest_key,
            "observed_scan_job": _find_job(jobs, SCAN_SCRIPT) is not None,
        }
        return _bounded_output("\n\n".join(output_parts))
    except Exception as exc:
        heartbeat_status = "error"
        detail = {"error": f"{type(exc).__name__}: {exc}"}
        return _bounded_output(
            f"IDO watchlist reminders failed: {type(exc).__name__}: {exc}"
        )
    finally:
        record_heartbeat(
            conn,
            job="remind",
            run_id=run_id,
            status=heartbeat_status,
            detail=detail,
            ran_at=emitted_at,
        )
        conn.close()


def main() -> str:
    output = run()
    if output:
        print(output)
    return output


__all__ = [
    "REMIND_SCRIPT",
    "SCAN_SCRIPT",
    "reconcile_outbox",
    "observe_scan_job",
    "run",
    "main",
]
