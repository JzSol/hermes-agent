"""Installation tests for the packaged watchlist scheduler entry point."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from cron import jobs as cron_jobs
from watchlist import setup


def test_fresh_profile_install_is_idempotent_and_mirror_safe(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    first = setup.install(deliver="telegram")
    second = setup.install(deliver="telegram")

    assert [job["id"] for job in second] == [job["id"] for job in first]
    jobs = cron_jobs.list_jobs(include_disabled=True)
    assert len(jobs) == 2
    assert {job["script"] for job in jobs} == {
        setup.SCAN_SCRIPT,
        setup.REMIND_SCRIPT,
    }
    for job in jobs:
        assert job["no_agent"] is True
        assert job["attach_to_session"] is False
        assert job["deliver"] == "telegram"
        assert job["monitor_script"] is None
        assert job["monitor_url"] is None

    for filename in (setup.SCAN_SCRIPT, setup.REMIND_SCRIPT):
        path = home / "scripts" / filename
        assert path.exists()
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        text = path.read_text(encoding="utf-8")
        assert text.index("import hermes_bootstrap") < text.index("from watchlist.")
    if os.name != "nt":
        assert stat.S_IMODE((home / "scripts").stat().st_mode) == 0o700


def test_install_hardens_an_existing_agent_job(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    existing = cron_jobs.create_job(
        prompt="summarize the scan",
        schedule="every 2h",
        script=setup.SCAN_SCRIPT,
        deliver="origin",
        attach_to_session=True,
    )

    setup.install(deliver="local")
    hardened = next(
        job
        for job in cron_jobs.list_jobs(include_disabled=True)
        if job["id"] == existing["id"]
    )
    assert hardened["no_agent"] is True
    assert hardened["prompt"] == ""
    assert hardened["attach_to_session"] is False
    assert hardened["deliver"] == "local"


def test_install_resumes_an_existing_paused_job(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    existing = cron_jobs.create_job(
        prompt=None,
        schedule="every 2h",
        script=setup.SCAN_SCRIPT,
        deliver="local",
        no_agent=True,
    )
    cron_jobs.pause_job(existing["id"], reason="maintenance")

    installed = setup.install(deliver="local")

    resumed = next(job for job in installed if job["id"] == existing["id"])
    assert resumed["enabled"] is True
    assert resumed["state"] == "scheduled"
    assert resumed["paused_at"] is None
    assert resumed["paused_reason"] is None


def test_install_recovers_an_existing_recurring_error_job(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    existing = cron_jobs.create_job(
        prompt=None,
        schedule="every 2h",
        script=setup.SCAN_SCRIPT,
        deliver="local",
        no_agent=True,
    )
    cron_jobs.update_job(
        existing["id"],
        {"state": "error", "enabled": True, "next_run_at": None},
    )

    installed = setup.install(deliver="local")

    recovered = next(job for job in installed if job["id"] == existing["id"])
    assert recovered["enabled"] is True
    assert recovered["state"] == "scheduled"
    jobs = cron_jobs.list_jobs(include_disabled=True)
    assert len([job for job in jobs if job["script"] == setup.SCAN_SCRIPT]) == 1


def test_concurrent_installs_create_one_job_per_script(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _index: setup.install(deliver="local"), range(2))
        )

    assert [job["id"] for job in results[0]] == [job["id"] for job in results[1]]
    jobs = cron_jobs.list_jobs(include_disabled=True)
    assert len(jobs) == 2
    assert {job["script"] for job in jobs} == {
        setup.SCAN_SCRIPT,
        setup.REMIND_SCRIPT,
    }


def test_install_does_not_claim_an_unrelated_same_basename_job(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    unrelated_script = str(tmp_path / setup.SCAN_SCRIPT)
    unrelated = cron_jobs.create_job(
        prompt=None,
        schedule="every 2h",
        script=unrelated_script,
        deliver="local",
        no_agent=True,
    )

    installed = setup.install(deliver="local")

    assert unrelated["id"] not in {job["id"] for job in installed}
    jobs = cron_jobs.list_jobs(include_disabled=True)
    assert len(jobs) == 3
    assert (
        next(job for job in jobs if job["id"] == unrelated["id"])["script"]
        == unrelated_script
    )


@pytest.mark.parametrize("target", ["", "origin", "all", "bot-chat", "in_channel"])
def test_install_rejects_ambiguous_or_agent_facing_delivery(
    monkeypatch, tmp_path, target
):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pytest.raises(ValueError):
        setup.install(deliver=target)
    assert not (home / "scripts").exists()
    assert cron_jobs.list_jobs(include_disabled=True) == []


def test_reminder_stub_runs_in_a_fresh_profile(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    setup.install(deliver="local")

    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(home / "scripts" / setup.REMIND_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "IDO watchlist health digest" in result.stdout


def test_install_refuses_to_overwrite_user_script(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    custom = scripts / setup.SCAN_SCRIPT
    custom.write_text("print('mine')\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(ValueError, match="user-managed"):
        setup.install(deliver="local")

    assert custom.read_text(encoding="utf-8") == "print('mine')\n"
    assert not (scripts / setup.REMIND_SCRIPT).exists()
    assert cron_jobs.list_jobs(include_disabled=True) == []


def test_stub_write_failure_rolls_back_the_install(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_write = setup._write_stub
    calls = 0

    def fail_second(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        real_write(path, content)

    monkeypatch.setattr(setup, "_write_stub", fail_second)
    with pytest.raises(OSError, match="simulated write failure"):
        setup.install(deliver="local")

    assert not (home / "scripts").exists()
    assert cron_jobs.list_jobs(include_disabled=True) == []


def test_job_write_failure_rolls_back_scripts_and_jobs(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_upsert = setup._upsert_job
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated job failure")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(setup, "_upsert_job", fail_second)
    with pytest.raises(OSError, match="simulated job failure"):
        setup.install(deliver="local")

    assert not (home / "scripts").exists()
    assert cron_jobs.list_jobs(include_disabled=True) == []
