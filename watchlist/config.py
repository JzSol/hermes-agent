"""Configuration for the IDO watchlist.

Watchlist behavior lives under ``watchlist:`` in ``config.yaml``.  This
module owns defaults and validation so a malformed optional section cannot
kill a scheduled run: invalid values produce a warning and fall back to the
corresponding default.
"""

from __future__ import annotations

import copy
import logging
import math
import re
from collections.abc import Mapping
from typing import Any, Optional

from hermes_cli.config import read_raw_config

logger = logging.getLogger(__name__)


DEFAULT_SCORE_ANCHORS: dict[str, dict[str, dict[str, Any]]] = {
    "risk": {
        "backer_overlap": {"low": 0, "mid": 1, "high": 2},
        "audit": {
            "low": "no_audit",
            "mid": "audited_unclear",
            "high": "audited_clean",
        },
        "team_disclosure": {
            "low": "anonymous",
            "mid": "partial",
            "high": "doxxed_history",
        },
        "vesting_disclosure": {
            "low": "undisclosed",
            "mid": "partial",
            "high": "full",
        },
        "raise_disclosed": {
            "low": "undisclosed",
            "mid": "range",
            "high": "exact",
        },
        "round_type_disclosed": {
            "low": "undisclosed",
            "high": "named",
        },
    },
    "asym": {
        "fdv": {"best": 25_000_000, "mid": 150_000_000, "worst": 500_000_000},
        "raise_size": {"best": 1_000_000, "mid": 5_000_000, "worst": 25_000_000},
        "float_at_tge": {"best": 5, "mid": 15, "worst": 30},
        "fdv_raise_ratio": {"best": 20, "mid": 60, "worst": 150},
    },
}


DEFAULT_CONFIG: dict[str, Any] = {
    "sources": ["icodrops"],
    "shortlist_limit": 10,
    "sort_by": "asym",
    "risk_floor": 40,
    "min_coverage": 0.5,
    "rug_shape": {"asym_min": 70, "risk_max": 45},
    "score_anchors": DEFAULT_SCORE_ANCHORS,
    "reminder_horizons": ["T-7d", "T-24h", "T-2h", "due"],
    "max_emit_attempts": 3,
    "fetch": {
        "timeout_seconds": 30,
        "delay_seconds": 1.0,
        "max_details_per_run": 40,
    },
    "max_stages_per_project": 24,
}

DEFAULTS = DEFAULT_CONFIG
_HORIZONS = {"T-7d", "T-24h", "T-2h", "due"}


def _warn(key: str, value: Any) -> None:
    logger.warning("Invalid watchlist.%s=%r; using the default", key, value)


def _valid_number(
    value: Any, *, minimum: float, maximum: Optional[float] = None
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return (
        math.isfinite(number)
        and number >= minimum
        and (maximum is None or number <= maximum)
    )


def _copy_default(key: str) -> Any:
    return copy.deepcopy(DEFAULT_CONFIG[key])


def _merge_score_anchors(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _warn("score_anchors", value)
        return _copy_default("score_anchors")
    merged = _copy_default("score_anchors")
    for axis in ("risk", "asym"):
        supplied_axis = value.get(axis)
        if not isinstance(supplied_axis, Mapping):
            if supplied_axis is not None:
                _warn(f"score_anchors.{axis}", supplied_axis)
            continue
        for field, anchors in supplied_axis.items():
            if field not in merged[axis]:
                _warn(f"score_anchors.{axis}.{field}", anchors)
                continue
            if not isinstance(anchors, Mapping):
                _warn(f"score_anchors.{axis}.{field}", anchors)
                continue
            candidate = dict(merged[axis][field])
            candidate.update(anchors)
            if not _valid_anchor_set(axis, field, candidate):
                # Fall back to this field's default rather than accept it.  An
                # unvalidated anchor set fails silently and invisibly: bad
                # asymmetry bounds make every fact UNKNOWN (coverage quietly
                # collapses), and duplicate risk labels let one tier overwrite
                # another so a category scores as the wrong tier.  Both look
                # like "no data" rather than "bad config".
                _warn(f"score_anchors.{axis}.{field}", anchors)
                continue
            merged[axis][field] = candidate
    return merged


def normalize_label(value: Any) -> str:
    """Canonical token for an anchor label or a categorical fact value.

    Shared with :mod:`watchlist.scoring` on purpose: the validator must collapse
    labels exactly the way the scorer does, or it validates something other than
    what runs.  ``"not audited"`` and ``"not-audited"`` are distinct strings but
    the same token, so a distinctness check that does not normalize would pass
    them and then let one tier silently overwrite the other.
    """

    return re.sub(r"[\s_-]+", "_", str(value).strip().lower())


#: Tier keys each axis accepts.  Anything else is a typo, and a merged typo is
#: silently ignored by the scorer — indistinguishable from having set nothing.
_ANCHOR_TIER_KEYS: dict[str, frozenset[str]] = {
    "risk": frozenset({"low", "mid", "high"}),
    "asym": frozenset({"best", "mid", "worst", "low", "high"}),
}

#: Risk fields whose anchors are numeric thresholds rather than category labels.
_NUMERIC_RISK_FIELDS = frozenset({"backer_overlap"})


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _valid_anchor_set(axis: str, field: str, candidate: Mapping[str, Any]) -> bool:
    """Validate one field's anchor set, per axis *and* per field.

    Asymmetry anchors are interpolation bounds: strictly ordered finite numbers.
    ``backer_overlap`` is numeric thresholds even though it lives on the risk
    axis — a categorical label there is accepted by a field-agnostic check and
    then silently ignored by the numeric scorer.  Every other risk field is a
    category label: present, non-empty, and distinct *after normalization*.
    """

    if any(key not in _ANCHOR_TIER_KEYS[axis] for key in candidate):
        return False

    def _tier(name: str, *alias: str) -> Any:
        for key in (name, *alias):
            if key in candidate:
                return candidate[key]
        return None

    if axis == "asym" or field in _NUMERIC_RISK_FIELDS:
        if axis == "asym":
            bounds = [_tier("best", "high"), _tier("mid"), _tier("worst", "low")]
        else:
            bounds = [_tier("low"), _tier("mid"), _tier("high")]
        if not all(_is_number(value) for value in bounds):
            return False
        try:
            numeric_bounds = [float(value) for value in bounds]
        except (OverflowError, TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in numeric_bounds):
            return False
        return all(a < b for a, b in zip(numeric_bounds, numeric_bounds[1:]))

    present = [candidate[key] for key in ("low", "mid", "high") if key in candidate]
    if len(present) < 2:
        return False
    if not all(isinstance(value, str) for value in present):
        return False
    labels = [normalize_label(t) for t in present]
    if any(not label for label in labels):
        return False
    return len(set(labels)) == len(labels)


def load_config(raw_config: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Load and validate the watchlist section of ``config.yaml``.

    ``raw_config`` is injectable for tests and callers that already loaded
    YAML. With no argument the shared raw config reader is used.
    """

    raw = read_raw_config() if raw_config is None else raw_config
    section = raw.get("watchlist", {}) if isinstance(raw, Mapping) else {}
    if not isinstance(section, Mapping):
        _warn("section", section)
        section = {}
    result = copy.deepcopy(DEFAULT_CONFIG)

    value = section.get("sources", _UNSET)
    if value is not _UNSET:
        if isinstance(value, list) and all(
            isinstance(item, str) and item.strip() for item in value
        ):
            result["sources"] = [item.strip() for item in value]
        else:
            _warn("sources", value)

    validators = {
        "shortlist_limit": lambda item: _valid_number(item, minimum=1),
        "risk_floor": lambda item: _valid_number(item, minimum=0, maximum=100),
        "min_coverage": lambda item: _valid_number(item, minimum=0, maximum=1),
        "max_emit_attempts": lambda item: _valid_number(item, minimum=1),
        "max_stages_per_project": lambda item: _valid_number(item, minimum=1),
    }
    for key, validator in validators.items():
        if key not in section:
            continue
        item = section[key]
        if validator(item) and isinstance(item, int):
            result[key] = item
        elif key == "min_coverage" and validator(item):
            result[key] = float(item)
        else:
            _warn(key, item)

    if "sort_by" in section:
        if isinstance(section["sort_by"], str) and section["sort_by"] in {
            "asym",
            "risk",
        }:
            result["sort_by"] = section["sort_by"]
        else:
            _warn("sort_by", section["sort_by"])

    if "rug_shape" in section:
        item = section["rug_shape"]
        if (
            isinstance(item, Mapping)
            and _valid_number(item.get("asym_min"), minimum=0, maximum=100)
            and _valid_number(item.get("risk_max"), minimum=0, maximum=100)
        ):
            result["rug_shape"] = {
                "asym_min": item["asym_min"],
                "risk_max": item["risk_max"],
            }
        else:
            _warn("rug_shape", item)

    if "score_anchors" in section:
        result["score_anchors"] = _merge_score_anchors(section["score_anchors"])

    if "reminder_horizons" in section:
        item = section["reminder_horizons"]
        if isinstance(item, list) and all(isinstance(horizon, str) for horizon in item):
            if all(horizon in _HORIZONS for horizon in item):
                result["reminder_horizons"] = list(dict.fromkeys(item))
            else:
                _warn("reminder_horizons", item)
        else:
            _warn("reminder_horizons", item)

    if "fetch" in section:
        fetch = section["fetch"]
        if isinstance(fetch, Mapping):
            result_fetch = result["fetch"]
            # Durations are floats; counts are integers.  Coercing a duration
            # with int() silently turned a valid 0.5s timeout into 0, and
            # coercing a count silently turned 1.5 into 1 — both passed
            # validation and then became a different value than was configured.
            # A count that is not a whole number is a config error, not a
            # rounding opportunity.
            for key, minimum, is_count in (
                ("timeout_seconds", 0.001, False),
                ("delay_seconds", 0, False),
                ("max_details_per_run", 1, True),
            ):
                if key not in fetch:
                    continue
                item = fetch[key]
                if not _valid_number(item, minimum=minimum):
                    _warn(f"fetch.{key}", item)
                    continue
                if is_count and not float(item).is_integer():
                    _warn(f"fetch.{key}", item)
                    continue
                result_fetch[key] = int(item) if is_count else float(item)
        else:
            _warn("fetch", fetch)

    return result


def get_config(raw_config: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Compatibility alias for callers that prefer an accessor name."""

    return load_config(raw_config)


def default_score_anchors() -> dict[str, Any]:
    """Return a detached copy of the default/config-independent anchors."""

    return copy.deepcopy(DEFAULT_SCORE_ANCHORS)


_UNSET = object()


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULTS",
    "DEFAULT_SCORE_ANCHORS",
    "default_score_anchors",
    "load_config",
    "get_config",
]
