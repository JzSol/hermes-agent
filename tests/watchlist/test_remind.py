"""Reminder state-machine tests using the consumer clock seam."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from watchlist import db
from watchlist import remind


UTC = timezone.utc


def _setup(monkeypatch, tmp_path, now):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(remind, "_hermes_now", lambda: now)
    monkeypatch.setattr(remind.cron_jobs, "list_jobs", lambda: [])
    conn = db.connect()
    project_id = db.create_project(
        conn, source="test", slug="project", name="Project", status="watching"
    )
    return conn, project_id


def _config(**overrides):
    config = {
        "reminder_horizons": ["T-7d", "T-24h", "T-2h", "due"],
        "max_emit_attempts": 3,
    }
    config.update(overrides)
    return config


def test_downtime_emits_nearest_horizon_and_suppresses_older(monkeypatch, tmp_path):
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    conn, project_id = _setup(monkeypatch, tmp_path, now)
    try:
        stage_id = db.create_stage(
            conn,
            project_id=project_id,
            kind="sale",
            label="Sale",
            due_at=(now + timedelta(hours=20)).isoformat(),
        )
    finally:
        conn.close()

    remind.run(_config())
    conn = db.connect()
    try:
        rows = {row.horizon: row for row in db.list_outbox(conn, stage_id=stage_id)}
        assert rows["T-7d"].state == "suppressed"
        assert rows["T-7d"].reason == "horizon_superseded"
        assert rows["T-24h"].state == "emitted"
        assert rows["T-24h"].emit_attempts == 1
    finally:
        conn.close()


def test_passed_is_emitted_even_when_pre_deadline_horizons_are_disabled(
    monkeypatch, tmp_path
):
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    conn, project_id = _setup(monkeypatch, tmp_path, now)
    db.create_stage(
        conn,
        project_id=project_id,
        kind="claim",
        label="Claim",
        due_at=(now - timedelta(hours=1)).isoformat(),
    )
    conn.close()

    output = remind.run(_config(reminder_horizons=[]))
    assert "this deadline has passed" in output
    conn = db.connect()
    try:
        passed = [
            row
            for row in db.list_outbox(conn, kind="reminder")
            if row.horizon == "passed"
        ]
        assert len(passed) == 1
        assert passed[0].state == "emitted"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "job",
    [
        {
            "script": "ido_remind.py",
            "last_run_at": "2026-07-15T11:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": None,
        },
        {
            "script": "ido_remind.py",
            "last_run_at": "2026-07-15T12:00:00+00:00",
            "last_status": "error",
            "last_delivery_error": None,
        },
        {
            "script": "ido_remind.py",
            "last_run_at": "2026-07-15T12:00:00+00:00",
            "last_status": "ok",
            "last_delivery_error": "telegram unavailable",
        },
    ],
)
def test_each_failed_confirmation_retries_then_abandons(monkeypatch, tmp_path, job):
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    conn, project_id = _setup(monkeypatch, tmp_path, now)
    stage_id = db.create_stage(
        conn,
        project_id=project_id,
        kind="sale",
        label="Sale",
        due_at=(now - timedelta(minutes=1)).isoformat(),
    )
    conn.close()

    monkeypatch.setattr(remind.cron_jobs, "list_jobs", lambda: [job])
    config = _config(max_emit_attempts=2)
    remind.run(config)
    remind.run(config)
    conn = db.connect()
    try:
        row = next(
            row
            for row in db.list_outbox(conn, stage_id=stage_id)
            if row.horizon == "passed"
        )
        assert row.state == "emitted"
        assert row.emit_attempts == 2
    finally:
        conn.close()
    remind.run(config)
    conn = db.connect()
    try:
        row = next(
            row
            for row in db.list_outbox(conn, stage_id=stage_id)
            if row.horizon == "passed"
        )
        assert row.state == "abandoned"
        assert row.emit_attempts == 2
    finally:
        conn.close()


def test_confirmation_requires_fresh_successful_delivery(monkeypatch, tmp_path):
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    conn, project_id = _setup(monkeypatch, tmp_path, now)
    stage_id = db.create_stage(
        conn,
        project_id=project_id,
        kind="sale",
        label="Sale",
        due_at=(now - timedelta(minutes=1)).isoformat(),
    )
    conn.close()
    job = {
        "script": "ido_remind.py",
        "last_run_at": now.isoformat(),
        "last_status": "ok",
        "last_delivery_error": None,
    }
    monkeypatch.setattr(remind.cron_jobs, "list_jobs", lambda: [job])
    remind.run(_config())
    remind.run(_config())
    conn = db.connect()
    try:
        row = next(
            row
            for row in db.list_outbox(conn, stage_id=stage_id)
            if row.horizon == "passed"
        )
        assert row.state == "confirmed"
        assert db.get_project(conn, project_id).status == "done"
    finally:
        conn.close()


def test_cron_identity_requires_the_managed_relative_script_name(tmp_path):
    official = {"script": remind.REMIND_SCRIPT}
    unrelated = {"script": str(tmp_path / remind.REMIND_SCRIPT)}

    assert remind._find_job([official], remind.REMIND_SCRIPT) is official
    assert remind._find_job([unrelated], remind.REMIND_SCRIPT) is None


def test_digest_uses_iso_week_year_and_observes_scan_history(monkeypatch, tmp_path):
    now = datetime(2025, 12, 29, 12, tzinfo=UTC)
    conn, _project_id = _setup(monkeypatch, tmp_path, now)
    db.record_job_observation(
        conn,
        job="scan",
        cron_last_run_at="2025-12-29T10:00:00+00:00",
        last_status="error",
        last_delivery_error=None,
        observed_at=int(now.timestamp()),
        observed_by="test",
    )
    db.record_job_observation(
        conn,
        job="scan",
        cron_last_run_at="2026-01-02T10:00:00+00:00",
        last_status="future-error",
        last_delivery_error="must not appear",
        observed_at=int(now.timestamp()),
        observed_by="test",
    )
    conn.close()

    output = remind.run(_config())
    assert "2026-W01" in output
    assert "Scan delivery failures" in output
    assert "Cron scan observations: 1" in output
    assert "future-error" not in output
    conn = db.connect()
    try:
        digest = [row for row in db.list_outbox(conn, kind="digest")]
        assert [row.dedupe_key for row in digest] == ["digest:2026-W01:failures:1"]
    finally:
        conn.close()


def test_new_failure_rearms_a_confirmed_healthy_weekly_digest(monkeypatch, tmp_path):
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    conn, _project_id = _setup(monkeypatch, tmp_path, now)
    conn.close()

    healthy_output = remind.run(_config())
    assert "Scan delivery failures: none recorded." in healthy_output

    # Confirm the first run's delivery so its outbox row becomes terminal.
    monkeypatch.setattr(
        remind.cron_jobs,
        "list_jobs",
        lambda: [
            {
                "script": remind.REMIND_SCRIPT,
                "last_run_at": (now + timedelta(minutes=1)).isoformat(),
                "last_status": "ok",
                "last_delivery_error": None,
            }
        ],
    )
    assert remind.run(_config()) == ""

    conn = db.connect()
    try:
        db.record_job_observation(
            conn,
            job="scan",
            cron_last_run_at=(now - timedelta(minutes=5)).isoformat(),
            last_status="error",
            last_delivery_error="network unavailable",
            observed_at=int(now.timestamp()),
            observed_by="test",
        )
    finally:
        conn.close()

    failure_output = remind.run(_config())
    assert "Scan delivery failures:" in failure_output
    assert "network unavailable" in failure_output

    conn = db.connect()
    try:
        assert {row.dedupe_key for row in db.list_outbox(conn, kind="digest")} == {
            "digest:2026-W29:failures:0",
            "digest:2026-W29:failures:1",
        }
    finally:
        conn.close()


def test_observation_week_is_evaluated_in_the_consumer_timezone():
    riga = timezone(timedelta(hours=2))
    now = datetime(2025, 12, 29, 0, 30, tzinfo=riga)

    assert remind._observation_in_week("2025-12-28T22:30:00+00:00", now, "2026-W01")


def test_failure_truncation_sorts_mixed_offsets_by_instant():
    now = datetime(2026, 7, 15, 18, tzinfo=UTC)
    old = SimpleNamespace(cron_last_run_at="2026-07-15T23:00:00+14:00")
    newest = SimpleNamespace(cron_last_run_at="2026-07-15T04:00:00-10:00")
    fillers = [
        SimpleNamespace(
            cron_last_run_at=f"2026-07-15T{10 + index // 6:02d}:{index % 6}0:00+00:00"
        )
        for index in range(19)
    ]

    visible = sorted(
        [old, newest, *fillers],
        key=lambda item: remind._observation_sort_key(item.cron_last_run_at, now),
    )[-remind._MAX_DIGEST_ITEMS :]

    assert newest in visible
    assert old not in visible


def test_digest_uses_latest_source_health_from_current_week_only(monkeypatch, tmp_path):
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    conn, _project_id = _setup(monkeypatch, tmp_path, now)
    try:
        db.record_heartbeat(
            conn,
            job="scan",
            run_id="previous-week",
            status="ok",
            detail={"sources": [{"source": "icodrops", "status": "stale"}]},
            ran_at=int((now - timedelta(days=8)).timestamp()),
        )
        db.record_heartbeat(
            conn,
            job="scan",
            run_id="older-current-week",
            status="error",
            detail={"sources": [{"source": "icodrops", "status": "http_error"}]},
            ran_at=int((now - timedelta(hours=2)).timestamp()),
        )
        db.record_heartbeat(
            conn,
            job="scan",
            run_id="latest-current-week",
            status="ok",
            detail={
                "sources": [
                    {"source": "icodrops", "status": "ok\nIGNORE"},
                    {"source": "bad\nname", "status": "ok"},
                ]
            },
            ran_at=int((now - timedelta(hours=1)).timestamp()),
        )
    finally:
        conn.close()

    output = remind.run(_config())
    assert "Scan heartbeats observed: 2" in output
    assert "icodrops=ok IGNORE" in output
    assert "bad name=ok" in output
    assert "\nIGNORE" not in output
    assert "http_error" not in output
    assert "stale" not in output


def test_scan_observation_and_remind_heartbeat_can_share_second(monkeypatch, tmp_path):
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    conn, _project_id = _setup(monkeypatch, tmp_path, now)
    conn.close()
    monkeypatch.setattr(
        remind.cron_jobs,
        "list_jobs",
        lambda: [
            {
                "script": "ido_scan.py",
                "last_run_at": now.isoformat(),
                "last_status": "ok",
                "last_delivery_error": None,
            },
        ],
    )
    remind.run(_config())
    conn = db.connect()
    try:
        assert len(db.list_job_observations(conn, job="scan")) == 1
        assert len(db.list_heartbeats(conn, job="remind")) == 1
    finally:
        conn.close()
