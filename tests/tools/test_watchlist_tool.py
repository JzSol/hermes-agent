"""Discovery, reachability, and deep-dive dispatch tests for watchlist."""

from __future__ import annotations

import json

from model_tools import get_tool_definitions, handle_function_call
from tools.registry import discover_builtin_tools, registry
from tools.tool_search import is_deferrable_tool_name
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset
from hermes_cli.tools_config import (
    CONFIGURABLE_TOOLSETS,
    _DEFAULT_OFF_TOOLSETS,
    _get_platform_tools,
)
from watchlist import db
from watchlist.scoring import DEEP, score_asymmetry, score_risk


def test_watchlist_is_discovered_and_registered_by_the_literal_contract():
    imported = discover_builtin_tools()

    assert "tools.watchlist_tool" in imported
    entry = registry.get_entry("watchlist")
    assert entry is not None
    assert entry.name == "watchlist"
    assert entry.toolset == "watchlist"
    assert entry.is_async is False
    assert registry.get_schema("watchlist")["name"] == "watchlist"


def test_watchlist_is_explicit_opt_in_and_absent_from_platform_bundles():
    assert "watchlist" in resolve_toolset("watchlist")
    assert "watchlist" not in _HERMES_CORE_TOOLS

    for name in TOOLSETS:
        if name.startswith("hermes-"):
            assert "watchlist" not in resolve_toolset(name), name
    assert "watchlist" not in resolve_toolset("hermes-webhook")
    assert "watchlist" not in resolve_toolset("hermes-gateway")

    explicit = {"platform_toolsets": {"cli": ["hermes-cli", "watchlist"]}}
    assert "watchlist" in _get_platform_tools(explicit, "cli")
    hostile = {"platform_toolsets": {"webhook": ["hermes-webhook", "watchlist"]}}
    assert "watchlist" not in _get_platform_tools(hostile, "webhook")


def test_cron_cannot_reach_the_watchlist_tool():
    """The scheduled path imports the store directly and never needs the tool.

    A cron agent runs auto-approved with no user present (approvals.cron_mode),
    so handing it a tool that mutates watchlist state buys nothing and widens
    the trust boundary — automated sessions get narrower capability defaults
    than an interactive user, per the architecture contract.
    """
    assert "watchlist" not in resolve_toolset("hermes-cron")

    definitions = get_tool_definitions(
        enabled_toolsets=["watchlist"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    assert [item["function"]["name"] for item in definitions] == ["watchlist"]
    assert any(item[0] == "watchlist" for item in CONFIGURABLE_TOOLSETS)
    assert "watchlist" in _DEFAULT_OFF_TOOLSETS
    # Deferrable on purpose.  Deferral hides the schema from the prompt; it does
    # not remove the capability — tool_search/tool_call still reach it, which is
    # what the bridge exists for.  Forcing it non-deferrable would put a niche,
    # opt-in tool's schema in every turn's prompt for no gain, and would mean a
    # leaf feature editing a shared registry file to special-case itself.
    assert is_deferrable_tool_name("watchlist") is True


def test_watchlist_schema_teaches_the_canonical_deep_fact_vocabulary():
    description = registry.get_schema("watchlist")["parameters"]["properties"][
        "risk_facts"
    ]["description"]

    for value in (
        "no_audit",
        "audited_unclear",
        "audited_clean",
        "anonymous",
        "partial",
        "doxxed_history",
        "undisclosed",
        "full",
        "range",
        "exact",
        "named",
    ):
        assert value in description


def test_health_output_honours_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    conn = db.connect()
    try:
        for index in range(5):
            db.record_heartbeat(
                conn,
                job="scan",
                run_id=f"run-{index}",
                status="ok",
                ran_at=100 + index,
            )
    finally:
        conn.close()

    discover_builtin_tools()
    out = json.loads(
        handle_function_call("watchlist", {"action": "health", "limit": 2})
    )
    assert out["success"] is True
    assert len(out["heartbeats"]) == 2


def test_dispatch_promote_persists_a_deep_stage_score(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    conn = db.connect()
    try:
        project_id = db.create_project(
            conn,
            source="icodrops",
            slug="alpha",
            name="Alpha",
            status="candidate",
            risk_facts={
                "backer_overlap": 1,
                "raise_disclosed": "exact",
                "round_type_disclosed": "named",
            },
            asym_facts={
                "fdv": 150_000_000,
                "raise_size": 5_000_000,
                "fdv_raise_ratio": 60,
            },
            score_stage="scan",
        )
    finally:
        conn.close()

    result = json.loads(
        handle_function_call(
            "watchlist",
            {
                "action": "promote",
                "project_id": project_id,
                "risk_facts": {
                    "audit": "audited_clean",
                    "team_disclosure": "doxxed_history",
                    "vesting_disclosure": "full",
                },
                "asym_facts": {"float_at_tge": 15},
            },
        )
    )
    assert result["success"] is True

    conn = db.connect()
    try:
        project = db.get_project(conn, project_id)
        assert project is not None
        assert project.status == "watching"
        assert project.score_stage == DEEP

        risk_facts = {
            "backer_overlap": 1,
            "raise_disclosed": "exact",
            "round_type_disclosed": "named",
            "audit": "audited_clean",
            "team_disclosure": "doxxed_history",
            "vesting_disclosure": "full",
        }
        asym_facts = {
            "fdv": 150_000_000,
            "raise_size": 5_000_000,
            "fdv_raise_ratio": 60,
            "float_at_tge": 15,
        }
        assert project.risk_score == score_risk(risk_facts, stage=DEEP).value
        assert project.asym_score == score_asymmetry(asym_facts, stage=DEEP).value
        assert project.risk_facts == risk_facts
        assert project.asym_facts == asym_facts
    finally:
        conn.close()


def test_show_refuses_facts_instead_of_silently_discarding_them():
    """`show` is read-only; only `promote` re-scores at the deep stage.

    Accepting facts here and returning success would silently drop a deep-dive's
    findings — the caller would believe a project was verified when nothing was
    written.  The schema advertises the fields on the tool, not per action, so
    this is an easy call to get wrong; it must fail loudly.
    """
    discover_builtin_tools()
    out = json.loads(handle_function_call("watchlist", {
        "action": "show",
        "project_id": "whatever",
        "risk_facts": {"audit": "audited_clean"},
    }))
    assert out.get("success") is not True
    assert "promote" in json.dumps(out)
