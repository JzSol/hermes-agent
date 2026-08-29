"""Pure, coverage-aware scoring for watchlist projects.

The two axes intentionally share only the aggregation rule. Risk measures
verifiable diligence facts; asymmetry measures structural room to move. An
unknown fact is not a negative fact: it is excluded from the weighted mean and
reduces coverage.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from watchlist.config import DEFAULT_SCORE_ANCHORS, normalize_label

UNKNOWN = "UNKNOWN"

# Data, rather than control flow: this seed list can be edited as the fund
# landscape changes without changing the scoring rubric.
BIG_BACKER_KEYWORDS = (
    "a16z",
    "andreessen horowitz",
    "binance",
    "paradigm",
    "dragonfly",
    "pantera",
    "polychain",
    "multicoin",
    "framework",
    "electric capital",
    "coinbase ventures",
    "sequoia",
    "jump crypto",
    "galaxy digital",
)

RISK_WEIGHTS = {
    "backer_overlap": 30,
    "audit": 25,
    "team_disclosure": 15,
    "vesting_disclosure": 15,
    "raise_disclosed": 10,
    "round_type_disclosed": 5,
}

ASYM_WEIGHTS = {
    "fdv": 35,
    "raise_size": 25,
    "float_at_tge": 25,
    "fdv_raise_ratio": 15,
}

#: Scan stage: the inputs a launchpad *listing* can actually supply.  Verified
#: against the live ICODrops listing on 2026-07-15 — it publishes FDV, raise,
#: round type and backers, and publishes no audit status, team disclosure or
#: vesting schedule at all.  Measuring a scanned project against the deep-stage
#: rubric pins its coverage near 0.35 forever, so every project renders
#: INSUFFICIENT DATA: accurate, and useless.  Coverage must be measured against
#: the inputs obtainable at this stage, not against an aspirational superset.
SCAN_RISK_WEIGHTS = {
    "backer_overlap": 55,
    "raise_disclosed": 25,
    "round_type_disclosed": 20,
}

SCAN_ASYM_WEIGHTS = {
    "fdv": 40,
    "raise_size": 30,
    "fdv_raise_ratio": 30,
}

#: Deep stage: the full rubric, once the interactive deep-dive has verified the
#: fields a listing never carries.
DEEP_RISK_WEIGHTS = RISK_WEIGHTS
DEEP_ASYM_WEIGHTS = ASYM_WEIGHTS

SCAN = "scan"
DEEP = "deep"

_STAGE_WEIGHTS: dict[str, tuple[Mapping[str, int], Mapping[str, int]]] = {
    SCAN: (SCAN_RISK_WEIGHTS, SCAN_ASYM_WEIGHTS),
    DEEP: (DEEP_RISK_WEIGHTS, DEEP_ASYM_WEIGHTS),
}


def _stage_weights(stage: str, axis: int) -> Mapping[str, int]:
    if stage not in _STAGE_WEIGHTS:
        raise ValueError(f"unknown scoring stage: {stage!r}")
    return _STAGE_WEIGHTS[stage][axis]


@dataclass(frozen=True)
class Score:
    #: ``None`` when no input was known.  A weighted mean over zero inputs has
    #: no value, and 0 would mean "maximally risky" on this higher-is-safer
    #: scale — a verdict invented from no evidence, and indistinguishable from
    #: a fully-covered project that genuinely fails every check.
    value: Optional[int]
    facts: Mapping[str, Any]
    coverage: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "facts": dict(self.facts),
            "coverage": self.coverage,
        }


def _is_unknown(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().upper() in {"", UNKNOWN}
    )


def _clamp(value: float) -> int:
    if not math.isfinite(value):
        return 0
    return max(0, min(100, int(round(value))))


def _token(value: Any) -> str:
    # One normalization, shared with the anchor validator in `config` so the
    # two can never disagree about what a label means.
    return normalize_label(value)


_NUMBER_RE = re.compile(r"^[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)$")


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:].strip()
    if cleaned.endswith(("%", "x")):
        cleaned = cleaned[:-1].strip()
    if _NUMBER_RE.fullmatch(cleaned) is None:
        return None
    try:
        number = float(cleaned.replace(",", ""))
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _is_token_character(char: str) -> bool:
    return bool(char) and (
        char == "_" or char.isalnum() or unicodedata.category(char).startswith("M")
    )


def contains_keyword_token(value: str, keyword: str) -> bool:
    """Match a Unicode token without treating combining marks as boundaries."""
    text = value.casefold()
    needle = keyword.casefold()
    if not needle:
        return False
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return False
        before = text[index - 1] if index else ""
        end = index + len(needle)
        after = text[end] if end < len(text) else ""

        if not _is_token_character(before) and not _is_token_character(after):
            return True
        start = index + 1


def _anchor(anchors: Mapping[str, Any], axis: str, field: str, key: str) -> Any:
    values = anchors.get(axis, {}).get(field, {})
    if not isinstance(values, Mapping):
        return None
    if key in values:
        return values[key]
    # Accept the equivalent low/mid/high names in user-supplied anchor maps.
    aliases = {"best": "high", "worst": "low"}
    return values.get(aliases.get(key, key))


def _backer_score(value: Any, anchors: Mapping[str, Any]) -> Optional[int]:
    if isinstance(value, (list, tuple, set)):
        if not all(isinstance(item, str) for item in value):
            return None
        value = sum(
            any(
                contains_keyword_token(item, keyword) for keyword in BIG_BACKER_KEYWORDS
            )
            for item in value
        )
    count = _number(value)
    if count is None:
        return None
    one = _number(_anchor(anchors, "risk", "backer_overlap", "mid"))
    two = _number(_anchor(anchors, "risk", "backer_overlap", "high"))
    one = 1 if one is None else one
    two = 2 if two is None else two
    if count >= two:
        return 100
    if count >= one:
        return 50
    return 0


def _risk_categories(anchors: Mapping[str, Any], field: str) -> dict[str, int]:
    """Build the exact token -> score map for ``field`` from the anchors.

    The anchors are the single source of truth for what each category *is*, so
    editing ``watchlist.score_anchors`` in config actually changes scoring
    rather than being silently ignored.
    """

    categories: dict[str, int] = {}
    for tier, score in (("low", 0), ("mid", 50), ("high", 100)):
        label = _anchor(anchors, "risk", field, tier)
        if label is None:
            continue
        categories[_token(label)] = score
    return categories


def _categorical_score(value: Any, categories: Mapping[str, int]) -> Optional[int]:
    """Score an exact, normalized category.

    Matching is exact rather than substring: ``"audit_pending" in "audit"``-style
    containment let an audit that has not happened score as a clean audit, and
    let ``audited_no_criticals`` collide with the ``critical`` tier and score
    *below* a pending one.  An unrecognized value returns ``None`` (-> UNKNOWN),
    never a guessed score — inventing "maximally risky" or "maximally safe" from
    a value we failed to parse is the fabricated-fact bug this module exists to
    avoid, and it is what ``_numeric_component`` already does for unparseable
    numbers.
    """

    if not isinstance(value, str):
        return None
    return categories.get(_token(value))


def _risk_component(
    field: str, value: Any, anchors: Mapping[str, Any]
) -> Optional[int]:
    if field == "backer_overlap":
        return _backer_score(value, anchors)
    return _categorical_score(value, _risk_categories(anchors, field))


def _numeric_component(
    field: str, value: Any, anchors: Mapping[str, Any]
) -> Optional[int]:
    number = _number(value)
    if number is None:
        return None
    best = _number(_anchor(anchors, "asym", field, "best"))
    middle = _number(_anchor(anchors, "asym", field, "mid"))
    worst = _number(_anchor(anchors, "asym", field, "worst"))
    if best is None or middle is None or worst is None:
        return None
    if not best < middle < worst:
        return None
    if number <= best:
        return 100
    if number <= middle:
        return _clamp(100 - 50 * (number - best) / (middle - best))
    if number <= worst:
        return _clamp(50 - 50 * (number - middle) / (worst - middle))
    return 0


def _aggregate(
    facts: Mapping[str, Any],
    weights: Mapping[str, int],
    scorer,
    anchors: Mapping[str, Any],
) -> Score:
    normalized: dict[str, Any] = {}
    weighted_total = 0.0
    known_weight = 0
    for field, weight in weights.items():
        value = facts.get(field, UNKNOWN)
        normalized[field] = UNKNOWN if _is_unknown(value) else value
        if _is_unknown(value):
            continue
        component = scorer(field, value, anchors)
        if component is None:
            normalized[field] = UNKNOWN
            continue
        weighted_total += weight * component
        known_weight += weight
    coverage = known_weight / sum(weights.values())
    value = None if known_weight == 0 else _clamp(weighted_total / known_weight)
    return Score(value=value, facts=normalized, coverage=coverage)


def score_risk(
    facts: Mapping[str, Any],
    *,
    stage: str = DEEP,
    anchors: Optional[Mapping[str, Any]] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Score:
    """Score diligence facts on the 0–100 risk axis.

    ``stage`` selects which inputs coverage is measured against — see
    ``SCAN_RISK_WEIGHTS``.  It defaults to ``DEEP`` so a caller that forgets to
    pass one is held to the *stricter* rubric and under-claims rather than
    over-claims confidence.
    """

    selected = config.get("score_anchors") if config else anchors
    return _aggregate(
        facts if isinstance(facts, Mapping) else {},
        _stage_weights(stage, 0),
        _risk_component,
        selected or DEFAULT_SCORE_ANCHORS,
    )


def score_asymmetry(
    facts: Mapping[str, Any],
    *,
    stage: str = DEEP,
    anchors: Optional[Mapping[str, Any]] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Score:
    """Score structural asymmetry facts on the 0–100 axis.

    ``stage`` selects the applicable inputs; see ``score_risk``.
    """

    selected = config.get("score_anchors") if config else anchors
    return _aggregate(
        facts if isinstance(facts, Mapping) else {},
        _stage_weights(stage, 1),
        lambda field, value, selected_anchors: _numeric_component(
            field, value, selected_anchors
        ),
        selected or DEFAULT_SCORE_ANCHORS,
    )


def coverage_eligible(score: Optional[Score], min_coverage: float = 0.5) -> bool:
    """Return whether a score is honest enough to rank or filter."""

    return (
        score is not None and score.value is not None and score.coverage >= min_coverage
    )


def is_rug_shape(
    risk_score: Optional[float],
    asym_score: Optional[float],
    *,
    asym_min: float = 70,
    risk_max: float = 45,
) -> bool:
    """Return whether a sufficiently-scored row has the rug-shape warning."""

    return (
        risk_score is not None
        and asym_score is not None
        and asym_score >= asym_min
        and risk_score <= risk_max
    )


__all__ = [
    "UNKNOWN",
    "BIG_BACKER_KEYWORDS",
    "RISK_WEIGHTS",
    "ASYM_WEIGHTS",
    "Score",
    "score_risk",
    "score_asymmetry",
    "coverage_eligible",
    "is_rug_shape",
]
