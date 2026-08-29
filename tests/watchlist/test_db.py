"""Behavioral tests for the profile-scoped watchlist store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from watchlist import db
from watchlist.config import DEFAULT_CONFIG, load_config


def test_schema_uses_a_sibling_db_without_fts_or_version_table(tmp_path):
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "projects",
            "stages",
            "outbox",
            "heartbeats",
            "job_observations",
        } <= tables
        assert "schema_version" not in tables
        assert not any("fts" in name.lower() for name in tables)
    finally:
        conn.close()


def test_project_upsert_preserves_user_fields_and_updates_source_fields(tmp_path):
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        project_id = db.upsert_project(
            conn,
            source="icodrops",
            slug="alpha",
            name="Alpha",
            ticker="ALP",
            url="https://example.test/alpha",
        )
        assert db.update_project(conn, project_id, status="watching", notes="keep this")

        assert (
            db.upsert_project(
                conn,
                source="icodrops",
                slug="alpha",
                name="Alpha Updated",
                ticker="ALP2",
                url="https://example.test/alpha-new",
            )
            == project_id
        )

        project = db.get_project(conn, project_id)
        assert project is not None
        assert project.name == "Alpha Updated"
        assert project.ticker == "ALP2"
        assert project.status == "watching"
        assert project.notes == "keep this"
        assert db.get_project(conn, "alpha").id == project_id
    finally:
        conn.close()


def test_stage_reschedule_suppresses_unsettled_old_revision(tmp_path):
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        project_id = db.create_project(
            conn, source="source", slug="alpha", name="Alpha"
        )
        stage_id = db.create_stage(
            conn,
            project_id=project_id,
            kind="sale",
            label="Sale",
            due_at="2026-07-20T12:00:00+00:00",
        )
        outbox_id = db.create_outbox(
            conn,
            kind="reminder",
            dedupe_key=f"stage:{stage_id}:r1:T-24h",
            project_id=project_id,
            stage_id=stage_id,
            horizon="T-24h",
        )

        assert db.update_stage(conn, stage_id, due_at="2026-07-22T12:00:00+00:00")
        stage = db.get_stage(conn, stage_id)
        old_row = db.get_outbox(conn, outbox_id)
        assert stage.schedule_revision == 2
        assert old_row.state == "suppressed"
        assert old_row.reason == "schedule_revised"
    finally:
        conn.close()


def test_offset_deadlines_are_stored_in_utc_and_sorted_by_instant(tmp_path):
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        project_id = db.create_project(
            conn, source="source", slug="alpha", name="Alpha"
        )
        later_id = db.create_stage(
            conn,
            project_id=project_id,
            kind="sale",
            label="Later",
            due_at="2026-07-20T09:30:00-04:00",
        )
        earlier_id = db.create_stage(
            conn,
            project_id=project_id,
            kind="kyc",
            label="Earlier",
            due_at="2026-07-20T14:00:00+02:00",
        )

        stages = db.list_stages(conn, project_id)
        assert [stage.id for stage in stages] == [earlier_id, later_id]
        assert all(stage.due_at.endswith("+00:00") for stage in stages)
        instants = [datetime.fromisoformat(stage.due_at) for stage in stages]
        assert instants == sorted(instants)
        assert all(value.tzinfo == timezone.utc for value in instants)
    finally:
        conn.close()


def _outbox_row(conn):
    project_id = db.create_project(conn, source="source", slug="alpha", name="Alpha")
    stage_id = db.create_stage(
        conn,
        project_id=project_id,
        kind="sale",
        label="Sale",
        due_at="2026-07-20T12:00:00+00:00",
    )
    return db.create_outbox(
        conn,
        kind="reminder",
        dedupe_key=f"stage:{stage_id}:r1:T-24h",
        project_id=project_id,
        stage_id=stage_id,
        horizon="T-24h",
    )


def test_a_never_emitted_row_cannot_be_confirmed(tmp_path):
    """`confirmed` means "the user received it".

    Confirming a pending row would record a delivery that never happened — the
    exact lie the outbox exists to prevent — and a stale reconciliation race
    could otherwise do it.
    """
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        outbox_id = _outbox_row(conn)
        assert db.settle_outbox(conn, outbox_id, "confirmed") is False
        assert db.get_outbox(conn, outbox_id).state == "pending"

        # But it is confirmable once actually emitted.
        db.emit_outbox(conn, outbox_id, emit_run_id="run-a")
        assert db.settle_outbox(conn, outbox_id, "confirmed") is True
        assert db.get_outbox(conn, outbox_id).state == "confirmed"
    finally:
        conn.close()


def test_only_one_emitter_can_claim_a_pending_row(tmp_path):
    """The UPDATE's rowcount is the lock.

    Two hourly runs racing on the same row must not both send it.
    """
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        outbox_id = _outbox_row(conn)
        assert db.emit_outbox(conn, outbox_id, emit_run_id="run-a") is True
        # Second claimer loses: the row is no longer pending.
        assert db.emit_outbox(conn, outbox_id, emit_run_id="run-b") is False
        assert db.get_outbox(conn, outbox_id).emit_run_id == "run-a"
        assert db.get_outbox(conn, outbox_id).emit_attempts == 1
    finally:
        conn.close()


def test_terminal_outbox_states_cannot_be_resurrected(tmp_path):
    """confirmed / suppressed / abandoned are terminal.

    Re-emitting one would re-send a message the user already received; flipping
    an abandoned row to confirmed would make the audit trail claim a delivery
    that never happened.
    """
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        for terminal in ("confirmed", "suppressed", "abandoned"):
            outbox_id = _outbox_row(conn)
            db.emit_outbox(conn, outbox_id, emit_run_id="run-a")
            assert db.settle_outbox(conn, outbox_id, terminal) is True

            assert db.emit_outbox(conn, outbox_id, emit_run_id="run-b") is False
            assert db.settle_outbox(conn, outbox_id, "confirmed") is False
            row = db.get_outbox(conn, outbox_id)
            assert row.state == terminal
            db.delete_project(conn, row.project_id)
    finally:
        conn.close()


def test_pending_row_may_be_suppressed_without_being_emitted(tmp_path):
    """The horizon_superseded path settles a row that was never sent."""
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        outbox_id = _outbox_row(conn)
        assert (
            db.settle_outbox(conn, outbox_id, "suppressed", reason="horizon_superseded")
            is True
        )
        row = db.get_outbox(conn, outbox_id)
        assert row.state == "suppressed"
        assert row.emitted_at is None
    finally:
        conn.close()


def test_heartbeat_and_observation_have_independent_same_second_keys(tmp_path):
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        db.record_heartbeat(conn, job="scan", run_id="run-1", status="ok", ran_at=100)
        db.record_job_observation(
            conn,
            job="scan",
            cron_last_run_at="2026-07-15T10:00:00+00:00",
            last_status="error",
            observed_at=100,
            observed_by="remind",
        )
        assert len(db.list_heartbeats(conn, job="scan")) == 1
        assert len(db.list_job_observations(conn, job="scan")) == 1
    finally:
        conn.close()


def test_watchlist_path_resolves_at_call_time(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("HERMES_HOME", str(first))
    assert db.watchlist_db_path() == first / "watchlist.db"
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert db.watchlist_db_path() == second / "watchlist.db"


def test_connect_refuses_a_preexisting_database_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", encoding="utf-8")
    linked_db = tmp_path / "watchlist.db"
    linked_db.symlink_to(victim)

    with pytest.raises(OSError, match="symlinked watchlist database"):
        db.connect(linked_db)

    assert victim.read_text(encoding="utf-8") == "do not touch"


def test_invalid_config_warns_and_uses_defaults(caplog):
    config = load_config({
        "watchlist": {
            "risk_floor": "high",
            "sort_by": "returns",
            "fetch": {"delay_seconds": -1},
        }
    })
    assert config["risk_floor"] == DEFAULT_CONFIG["risk_floor"]
    assert config["sort_by"] == DEFAULT_CONFIG["sort_by"]
    assert config["fetch"]["delay_seconds"] == DEFAULT_CONFIG["fetch"]["delay_seconds"]
    assert "Invalid watchlist" in caplog.text


@pytest.mark.parametrize("value", [[], {}])
def test_unhashable_sort_values_warn_and_use_default(value, caplog):
    config = load_config({"watchlist": {"sort_by": value}})

    assert config["sort_by"] == DEFAULT_CONFIG["sort_by"]
    assert "sort_by" in caplog.text


def test_durations_keep_fractions_and_counts_must_be_whole(caplog):
    """A validated value must not become a different value.

    ``int(0.5)`` turned a valid half-second timeout into 0 (no timeout at all),
    and ``int(1.5)`` silently turned a fractional count into 1 rather than
    reporting it as the config error it is.
    """
    config = load_config({
        "watchlist": {"fetch": {"timeout_seconds": 0.5, "delay_seconds": 2.5}}
    })
    assert config["fetch"]["timeout_seconds"] == 0.5
    assert config["fetch"]["delay_seconds"] == 2.5

    rejected = load_config({"watchlist": {"fetch": {"max_details_per_run": 1.5}}})
    assert (
        rejected["fetch"]["max_details_per_run"]
        == DEFAULT_CONFIG["fetch"]["max_details_per_run"]
    )
    assert "max_details_per_run" in caplog.text

    non_finite = load_config({
        "watchlist": {"fetch": {"timeout_seconds": float("inf")}}
    })
    assert (
        non_finite["fetch"]["timeout_seconds"]
        == DEFAULT_CONFIG["fetch"]["timeout_seconds"]
    )


def test_malformed_anchor_sets_warn_and_fall_back_per_field(caplog):
    """Unvalidated anchors fail silently and look like "no data".

    Out-of-order asymmetry bounds make every fact UNKNOWN — coverage quietly
    collapses — and duplicate risk labels collapse two tiers into one, so a
    category scores as the wrong tier.  Neither looks like bad config.
    """
    from watchlist.scoring import score_asymmetry, score_risk

    bad_order = load_config({
        "watchlist": {
            "score_anchors": {"asym": {"fdv": {"best": 500, "mid": 100, "worst": 10}}}
        }
    })
    assert (
        bad_order["score_anchors"]["asym"]["fdv"]
        == DEFAULT_CONFIG["score_anchors"]["asym"]["fdv"]
    )
    # Falling back keeps the axis scorable rather than silently UNKNOWN.
    assert (
        score_asymmetry({"fdv": 25_000_000}, anchors=bad_order["score_anchors"]).value
        == 100
    )

    dupes = load_config({
        "watchlist": {
            "score_anchors": {"risk": {"audit": {"low": "same", "high": "same"}}}
        }
    })
    assert (
        dupes["score_anchors"]["risk"]["audit"]
        == DEFAULT_CONFIG["score_anchors"]["risk"]["audit"]
    )
    assert (
        score_risk({"audit": "audited_clean"}, anchors=dupes["score_anchors"]).value
        == 100
    )
    assert "score_anchors" in caplog.text


def test_anchor_validation_is_field_aware_and_uses_the_scorer_normalization():
    """A field-agnostic validator accepts anchors the scorer cannot use.

    Each of these was accepted and then silently ignored or mis-scored, which is
    indistinguishable from having configured nothing at all.
    """
    from watchlist.scoring import score_risk

    # backer_overlap is numeric thresholds even though it lives on the risk
    # axis; a label there is silently ignored by the numeric scorer.
    labelled = load_config({
        "watchlist": {"score_anchors": {"risk": {"backer_overlap": {"high": "two"}}}}
    })
    assert (
        labelled["score_anchors"]["risk"]["backer_overlap"]
        == DEFAULT_CONFIG["score_anchors"]["risk"]["backer_overlap"]
    )

    # Distinct strings, same token: the validator must collapse labels exactly
    # the way the scorer does, or it validates something other than what runs.
    same_token = load_config({
        "watchlist": {
            "score_anchors": {
                "risk": {"audit": {"low": "not audited", "mid": "not-audited"}}
            }
        }
    })
    assert (
        same_token["score_anchors"]["risk"]["audit"]
        == DEFAULT_CONFIG["score_anchors"]["risk"]["audit"]
    )
    assert (
        score_risk({"audit": "no_audit"}, anchors=same_token["score_anchors"]).value
        == 0
    )

    # A misspelled tier key is a typo, and a merged typo does nothing at all.
    typo = load_config({
        "watchlist": {"score_anchors": {"risk": {"audit": {"hgih": "reviewed"}}}}
    })
    assert (
        typo["score_anchors"]["risk"]["audit"]
        == DEFAULT_CONFIG["score_anchors"]["risk"]["audit"]
    )

    # An explicit null is invalid, not an instruction to silently remove a
    # categorical tier while retaining the other defaults.
    null_tier = load_config({
        "watchlist": {"score_anchors": {"risk": {"audit": {"low": None}}}}
    })
    assert (
        null_tier["score_anchors"]["risk"]["audit"]
        == DEFAULT_CONFIG["score_anchors"]["risk"]["audit"]
    )


def test_daily_scan_cannot_downgrade_a_deep_dived_project(tmp_path):
    """A deep-stage score is verified work the user initiated.

    The daily scan only ever has listing-grade facts, so letting it write over a
    promoted project's diligence would quietly downgrade it every morning — the
    same class of loss as clobbering their notes, and invisible, because the
    number would still look like a number.
    """
    conn = db.connect(tmp_path / "watchlist.db")
    try:
        db.upsert_project(
            conn,
            source="icodrops",
            slug="alpha",
            name="Alpha",
            risk_score=90,
            risk_coverage=1.0,
            score_stage="deep",
        )
        # A later scan supplies weaker, listing-grade scores.
        db.upsert_project(
            conn,
            source="icodrops",
            slug="alpha",
            name="Alpha Renamed",
            risk_score=40,
            risk_coverage=1.0,
            score_stage="scan",
        )
        project = db.list_projects(conn)[0]
        assert project.risk_score == 90  # verified score preserved
        assert project.score_stage == "deep"
        assert project.name == "Alpha Renamed"  # source-owned fields still refresh
    finally:
        conn.close()
