"""Interactive, local-only actions for the IDO watchlist."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from tools.registry import registry, tool_error, tool_result
from watchlist.config import load_config
from watchlist.db import (
    connect_closing,
    create_stage,
    get_project,
    get_stage,
    list_heartbeats,
    list_job_observations,
    list_projects,
    list_stages,
    update_project,
    update_stage,
    upsert_project,
)
from watchlist.scoring import DEEP, score_asymmetry, score_risk

logger = logging.getLogger(__name__)

_ACTIONS = [
    "list",
    "show",
    "promote",
    "drop",
    "add_stage",
    "update_stage",
    "check",
    "health",
]
_STAGE_KINDS = ["register", "kyc", "fund", "sale", "allocation", "claim", "unlock"]
_CHECKLIST_STATES = ["pending", "done", "skipped"]

_RISK_FACT_DESCRIPTIONS = {
    "backer_overlap": (
        "Number of tier-1 backers confirmed from the backers' own side. "
        "Use a number; do not infer it from project marketing."
    ),
    "audit": (
        "Exact-match value only: no_audit, audited_unclear, or audited_clean. "
        "Anything else is UNKNOWN by design."
    ),
    "team_disclosure": (
        "Exact-match value only: anonymous, partial, or doxxed_history. "
        "Anything else is UNKNOWN by design."
    ),
    "vesting_disclosure": (
        "Exact-match value only: undisclosed, partial, or full. "
        "Anything else is UNKNOWN by design."
    ),
    "raise_disclosed": (
        "Exact-match value only: undisclosed, range, or exact. "
        "Anything else is UNKNOWN by design."
    ),
    "round_type_disclosed": (
        "Exact-match value only: undisclosed or named. "
        "Anything else is UNKNOWN by design."
    ),
}

WATCHLIST_SCHEMA = {
    "name": "watchlist",
    "description": (
        "Manage the local profile-scoped IDO watchlist: inspect projects, promote "
        "or drop them, record dated stages, update checklist state, and inspect "
        "scheduler health. This tool records facts and structure; it never emits "
        "a return forecast, price target, multiple, or probability of profit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": _ACTIONS,
                "description": "Action to perform.",
            },
            "project_id": {
                "type": "string",
                "description": "Project id, or its exact source slug for convenience.",
            },
            "status": {
                "type": "string",
                "enum": ["candidate", "watching", "dropped", "done"],
                "description": "Optional project status filter for action='list'.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived projects in action='list'.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": (
                    "Maximum number of rows returned by action='list' or "
                    "action='health' (default 50, maximum 200)."
                ),
            },
            "risk_facts": {
                "type": "object",
                "description": (
                    "Facts verified during the deep-dive. Canonical vocabulary is "
                    "exact-match: audit={no_audit,audited_unclear,audited_clean}; "
                    "team_disclosure={anonymous,partial,doxxed_history}; "
                    "vesting_disclosure={undisclosed,partial,full}; "
                    "raise_disclosed={undisclosed,range,exact}; "
                    "round_type_disclosed={undisclosed,named}. Values outside "
                    "these sets score UNKNOWN by design. backer_overlap is a "
                    "number of independently confirmed tier-1 backers."
                ),
                "properties": {
                    "backer_overlap": {
                        "type": "number",
                        "minimum": 0,
                        "description": _RISK_FACT_DESCRIPTIONS["backer_overlap"],
                    },
                    "audit": {
                        "type": "string",
                        "enum": ["no_audit", "audited_unclear", "audited_clean"],
                        "description": _RISK_FACT_DESCRIPTIONS["audit"],
                    },
                    "team_disclosure": {
                        "type": "string",
                        "enum": ["anonymous", "partial", "doxxed_history"],
                        "description": _RISK_FACT_DESCRIPTIONS["team_disclosure"],
                    },
                    "vesting_disclosure": {
                        "type": "string",
                        "enum": ["undisclosed", "partial", "full"],
                        "description": _RISK_FACT_DESCRIPTIONS["vesting_disclosure"],
                    },
                    "raise_disclosed": {
                        "type": "string",
                        "enum": ["undisclosed", "range", "exact"],
                        "description": _RISK_FACT_DESCRIPTIONS["raise_disclosed"],
                    },
                    "round_type_disclosed": {
                        "type": "string",
                        "enum": ["undisclosed", "named"],
                        "description": _RISK_FACT_DESCRIPTIONS["round_type_disclosed"],
                    },
                },
            },
            "asym_facts": {
                "type": "object",
                "description": (
                    "Verified structural facts for deep scoring: fdv and raise_size "
                    "are dollar amounts, float_at_tge is the percentage unlocked "
                    "at TGE, and fdv_raise_ratio is the FDV-to-raise ratio. "
                    "Unknown or unparseable values remain UNKNOWN."
                ),
                "properties": {
                    "fdv": {"type": "number", "minimum": 0},
                    "raise_size": {"type": "number", "minimum": 0},
                    "float_at_tge": {"type": "number", "minimum": 0},
                    "fdv_raise_ratio": {"type": "number", "minimum": 0},
                },
            },
            "kind": {
                "type": "string",
                "enum": _STAGE_KINDS,
                "description": "Stage kind for action='add_stage' or action='update_stage'.",
            },
            "label": {
                "type": "string",
                "description": "Human-readable stage label.",
            },
            "due_at": {
                "type": "string",
                "description": "ISO-8601 stage deadline with an explicit UTC offset.",
            },
            "stage_id": {
                "type": "string",
                "description": "Stage id for action='update_stage' or action='check'.",
            },
            "checklist_state": {
                "type": "string",
                "enum": _CHECKLIST_STATES,
                "description": "Checklist state; action='check' defaults to 'done'.",
            },
            "sort_order": {
                "type": "integer",
                "description": "Optional stage ordering value.",
            },
            "source": {
                "type": "string",
                "enum": ["scraped", "user"],
                "description": "Stage source; interactive stages default to 'user'.",
            },
            "url": {
                "type": "string",
                "description": "Optional source or claim URL for a stage.",
            },
            "job": {
                "type": "string",
                "enum": ["scan", "remind"],
                "description": "Optional job filter for action='health'.",
            },
        },
        "required": ["action"],
    },
}


def _as_mapping(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an object") from None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ValueError(f"{name} must be an object")


def _project_payload(conn: Any, project: Any, *, include_stages: bool = False) -> dict[str, Any]:
    payload = project.to_dict()
    if include_stages:
        payload["stages"] = [stage.to_dict() for stage in list_stages(conn, project.id)]
    return payload


def _stage_payload(stage: Any) -> dict[str, Any]:
    if stage is None:
        raise ValueError("stage not found")
    return stage.to_dict()


def _resolve_project(conn: Any, project_id: Any) -> Any:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    project = get_project(conn, project_id.strip())
    if project is None:
        raise ValueError(f"project not found: {project_id}")
    return project


def _promote(conn: Any, args: Mapping[str, Any]) -> str:
    project = _resolve_project(conn, args.get("project_id"))
    risk_supplied = args.get("risk_facts") is not None
    asym_supplied = args.get("asym_facts") is not None
    risk_facts = _as_mapping(args.get("risk_facts"), "risk_facts")
    asym_facts = _as_mapping(args.get("asym_facts"), "asym_facts")

    if risk_supplied or asym_supplied:
        existing_risk = project.risk_facts if isinstance(project.risk_facts, Mapping) else {}
        existing_asym = project.asym_facts if isinstance(project.asym_facts, Mapping) else {}
        merged_risk = {**existing_risk, **(risk_facts or {})}
        merged_asym = {**existing_asym, **(asym_facts or {})}
        config = load_config()
        risk = score_risk(merged_risk, stage=DEEP, config=config)
        asymmetry = score_asymmetry(merged_asym, stage=DEEP, config=config)
        # upsert_project owns score_stage and protects deep scores from later
        # scan-stage upserts.  update_project owns the user-facing status.
        upsert_project(
            conn,
            source=project.source,
            slug=project.slug,
            name=project.name,
            ticker=project.ticker,
            url=project.url,
            risk_score=risk.value,
            risk_facts=dict(risk.facts),
            risk_coverage=risk.coverage,
            asym_score=asymmetry.value,
            asym_facts=dict(asymmetry.facts),
            asym_coverage=asymmetry.coverage,
            score_stage=DEEP,
        )
    if not update_project(conn, project.id, status="watching"):
        raise ValueError(f"project not found: {project.id}")
    refreshed = get_project(conn, project.id)
    return tool_result(success=True, project=_project_payload(conn, refreshed, include_stages=True))


def _add_stage(conn: Any, args: Mapping[str, Any]) -> str:
    project = _resolve_project(conn, args.get("project_id"))
    kind = str(args.get("kind") or "").strip()
    label = str(args.get("label") or "").strip()
    due_at = args.get("due_at")
    if kind not in _STAGE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(_STAGE_KINDS)}")
    if not label:
        raise ValueError("label is required")
    if not due_at:
        raise ValueError("due_at is required")
    config = load_config()
    if len(list_stages(conn, project.id)) >= int(config["max_stages_per_project"]):
        raise ValueError("project has reached the configured stage limit")
    stage_id = create_stage(
        conn,
        project_id=project.id,
        kind=kind,
        label=label,
        due_at=str(due_at),
        sort_order=int(args.get("sort_order", 0)),
        source=str(args.get("source") or "user"),
        url=args.get("url"),
    )
    stage = get_stage(conn, stage_id)
    return tool_result(success=True, project_id=project.id, stage=_stage_payload(stage))


def _update_stage(conn: Any, args: Mapping[str, Any]) -> str:
    stage_id = args.get("stage_id")
    if not isinstance(stage_id, str) or not stage_id.strip():
        raise ValueError("stage_id is required")
    if get_stage(conn, stage_id.strip()) is None:
        raise ValueError(f"stage not found: {stage_id}")
    fields = {
        name: args[name]
        for name in ("kind", "label", "due_at", "sort_order", "checklist_state", "source", "url")
        if name in args
    }
    if "kind" in fields and fields["kind"] not in _STAGE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(_STAGE_KINDS)}")
    if not fields:
        raise ValueError("update_stage requires at least one stage field")
    if not update_stage(conn, stage_id.strip(), **fields):
        raise ValueError(f"stage not found: {stage_id}")
    stage = get_stage(conn, stage_id.strip())
    return tool_result(success=True, stage=_stage_payload(stage))


def watchlist_tool(args: Mapping[str, Any], **_kwargs: Any) -> str:
    """Dispatch one local watchlist action and return a JSON string."""

    if not isinstance(args, Mapping):
        return tool_error("watchlist arguments must be an object")
    action = str(args.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return tool_error(f"Unknown watchlist action '{action}'. Use: {', '.join(_ACTIONS)}")

    try:
        with connect_closing() as conn:
            if action == "list":
                limit = int(args.get("limit", 50))
                if limit < 1:
                    raise ValueError("limit must be at least 1")
                limit = min(limit, 200)
                projects = list_projects(
                    conn,
                    include_archived=bool(args.get("include_archived", False)),
                    status=args.get("status"),
                )[:limit]
                return tool_result(
                    success=True,
                    projects=[_project_payload(conn, project) for project in projects],
                    count=len(projects),
                )

            if action == "show":
                # Facts belong to `promote`, which re-scores at the deep stage.
                # Accepting them here and returning success would silently drop
                # a deep-dive's findings — the caller would believe the project
                # was verified when nothing was written.  Fail loudly instead.
                if args.get("risk_facts") is not None or args.get("asym_facts") is not None:
                    return tool_error(
                        "show is read-only and does not record facts. Pass "
                        "risk_facts/asym_facts to action='promote', which "
                        "re-scores the project at the deep stage."
                    )
                project = _resolve_project(conn, args.get("project_id"))
                return tool_result(
                    success=True,
                    project=_project_payload(conn, project, include_stages=True),
                )

            if action == "promote":
                return _promote(conn, args)

            if action == "drop":
                project = _resolve_project(conn, args.get("project_id"))
                if not update_project(conn, project.id, status="dropped"):
                    raise ValueError(f"project not found: {project.id}")
                return tool_result(success=True, project=_project_payload(conn, get_project(conn, project.id)))

            if action == "add_stage":
                return _add_stage(conn, args)

            if action == "update_stage":
                return _update_stage(conn, args)

            if action == "check":
                stage_id = args.get("stage_id")
                state = args.get("checklist_state", "done")
                if state not in _CHECKLIST_STATES:
                    raise ValueError(
                        f"checklist_state must be one of: {', '.join(_CHECKLIST_STATES)}"
                    )
                if not isinstance(stage_id, str) or not stage_id.strip():
                    raise ValueError("stage_id is required")
                if not update_stage(conn, stage_id.strip(), checklist_state=state):
                    raise ValueError(f"stage not found: {stage_id}")
                stage = get_stage(conn, stage_id.strip())
                return tool_result(success=True, stage=_stage_payload(stage))

            # health
            job = args.get("job")
            limit = int(args.get("limit", 50))
            if limit < 1:
                raise ValueError("limit must be at least 1")
            limit = min(limit, 200)
            heartbeats = list_heartbeats(conn, job=job)[:limit]
            observations = list_job_observations(conn, job=job)[:limit]
            return tool_result(
                success=True,
                heartbeats=[item.to_dict() for item in heartbeats],
                job_observations=[item.to_dict() for item in observations],
            )
    except (TypeError, ValueError, KeyError) as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover - registry also guards dispatch
        logger.exception("watchlist action failed")
        return tool_error(f"watchlist action failed: {type(exc).__name__}: {exc}")


registry.register(
    name="watchlist",
    toolset="watchlist",
    schema=WATCHLIST_SCHEMA,
    handler=lambda args, **kw: watchlist_tool(args, **kw),
    is_async=False,
    emoji="📋",
)


__all__ = ["WATCHLIST_SCHEMA", "watchlist_tool"]
