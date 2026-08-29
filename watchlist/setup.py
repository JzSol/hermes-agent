"""Install safe, profile-scoped IDO watchlist scheduler jobs."""

# This module owns all three packaged ``hermes-ido-*`` entry points, so the
# Windows UTF-8 bootstrap must run before any other import or output.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - interrupted-update recovery
    pass

import argparse
import copy
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Sequence

from cron import jobs as cron_jobs
from hermes_constants import get_hermes_home


SCAN_SCRIPT = "ido_scan.py"
REMIND_SCRIPT = "ido_remind.py"
SCAN_JOB_NAME = "IDO watchlist daily scan"
REMIND_JOB_NAME = "IDO watchlist reminders"
DEFAULT_SCAN_SCHEDULE = "0 9 * * *"
DEFAULT_REMIND_SCHEDULE = "every 1h"
_MANAGED_MARKER = "# Managed by hermes-ido-setup; safe to replace."

_STUBS = {
    SCAN_SCRIPT: (
        f"#!/usr/bin/env python3\n{_MANAGED_MARKER}\n"
        "import hermes_bootstrap\n"
        "from watchlist.scan import main\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    ),
    REMIND_SCRIPT: (
        f"#!/usr/bin/env python3\n{_MANAGED_MARKER}\n"
        "import hermes_bootstrap\n"
        "from watchlist.remind import main\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    ),
}


def _validate_delivery(value: str) -> str:
    deliver = str(value or "").strip()
    if not deliver:
        raise ValueError("--deliver is required (for example: telegram or local)")
    if any(char in deliver for char in "\r\n\x00"):
        raise ValueError("delivery target must be a single line")

    # These targets can feed scraped output back into an agent/session or fan
    # it out ambiguously.  Official watchlist jobs deliver only to an explicit
    # messaging target (or local logs) and always disable transcript mirroring.
    forbidden = {"origin", "all", "bot-chat", "in_channel"}
    for part in deliver.split(","):
        token = part.strip()
        if not token:
            raise ValueError("delivery target contains an empty item")
        base = token.split(":", 1)[0].strip().casefold()
        if base in forbidden:
            raise ValueError(
                f"delivery target {base!r} is not allowed for untrusted watchlist output"
            )
    return deliver


def _atomic_write_stub(path: Path, content: str, *, mode: int = 0o700) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _write_stub(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_stub(path, content)


def _prepare_scripts_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to use symlinked scripts directory: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"scripts path is not a private directory: {path}")
    if os.name != "nt":
        os.chmod(path, 0o700)


def _assert_stub_is_replaceable(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlinked script: {path}")
    if not path.exists():
        return
    if not path.is_file():
        raise ValueError(f"refusing to replace non-file script path: {path}")
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot inspect existing script {path}: {exc}") from exc
    if _MANAGED_MARKER not in existing:
        raise ValueError(
            f"refusing to overwrite user-managed script: {path}; move it first"
        )


def _active_matches(
    jobs: Sequence[dict[str, Any]], script: str
) -> list[dict[str, Any]]:
    return [
        job
        for job in jobs
        if str(job.get("script") or "").strip() == script
        and (
            job.get("state") not in {"completed", "error"}
            or cron_jobs._is_recoverable_error_job(job)
        )
    ]


def _upsert_job(
    jobs: Sequence[dict[str, Any]],
    *,
    script: str,
    name: str,
    schedule: str,
    deliver: str,
) -> dict[str, Any]:
    matches = _active_matches(jobs, script)
    if len(matches) > 1:
        ids = ", ".join(str(job.get("id", "?")) for job in matches)
        raise ValueError(
            f"multiple active jobs use {script}; remove duplicates first ({ids})"
        )

    safe_fields = {
        "name": name,
        "schedule": schedule,
        "deliver": deliver,
        "script": script,
        "prompt": "",
        "no_agent": True,
        "attach_to_session": False,
        "monitor_script": None,
        "monitor_url": None,
    }
    if matches:
        updated = cron_jobs.update_job(str(matches[0]["id"]), safe_fields)
        if updated is None:  # pragma: no cover - protected by the jobs lock
            raise RuntimeError(
                f"watchlist cron job disappeared during update: {script}"
            )
        if not updated.get("enabled", True) or updated.get("state") in {
            "paused",
            "error",
        }:
            resumed = cron_jobs.resume_job(str(updated["id"]))
            if resumed is None:  # pragma: no cover - protected by the jobs lock
                raise RuntimeError(
                    f"watchlist cron job disappeared during resume: {script}"
                )
            updated = resumed
        return updated

    return cron_jobs.create_job(
        prompt=None,
        schedule=schedule,
        name=name,
        deliver=deliver,
        script=script,
        no_agent=True,
        attach_to_session=False,
    )


def install(
    *,
    deliver: str,
    scan_schedule: str = DEFAULT_SCAN_SCHEDULE,
    remind_schedule: str = DEFAULT_REMIND_SCHEDULE,
) -> list[dict[str, Any]]:
    """Provision scripts and two safe, idempotent no-agent cron jobs."""

    target = _validate_delivery(deliver)
    # Validate both before writing anything so a typo cannot leave a half
    # installed profile.
    cron_jobs.parse_schedule(scan_schedule)
    cron_jobs.parse_schedule(remind_schedule)

    # Hold cron's re-entrant, cross-process lock across discovery and both
    # upserts.  Individual create/update calls also take this lock, but the
    # outer transaction prevents two concurrent installers from observing the
    # same empty snapshot and creating duplicate jobs.
    with cron_jobs._jobs_lock():
        existing = cron_jobs.list_jobs(include_disabled=True)
        for script in (SCAN_SCRIPT, REMIND_SCRIPT):
            matches = _active_matches(existing, script)
            if len(matches) > 1:
                ids = ", ".join(str(job.get("id", "?")) for job in matches)
                raise ValueError(
                    f"multiple active jobs use {script}; remove duplicates first ({ids})"
                )

        original_jobs = copy.deepcopy(cron_jobs.load_jobs())
        scripts_dir = get_hermes_home() / "scripts"
        if scripts_dir.is_symlink():
            raise ValueError(
                f"refusing to use symlinked scripts directory: {scripts_dir}"
            )
        if scripts_dir.exists() and not scripts_dir.is_dir():
            raise ValueError(f"scripts path is not a directory: {scripts_dir}")
        scripts_dir_existed = scripts_dir.exists()
        scripts_dir_mode = (
            stat.S_IMODE(scripts_dir.stat().st_mode) if scripts_dir_existed else None
        )
        stub_snapshots: dict[Path, tuple[str, int] | None] = {}

        try:
            _prepare_scripts_dir(scripts_dir)
            for filename in _STUBS:
                path = scripts_dir / filename
                _assert_stub_is_replaceable(path)
                stub_snapshots[path] = (
                    (
                        path.read_text(encoding="utf-8"),
                        stat.S_IMODE(path.stat().st_mode),
                    )
                    if path.exists()
                    else None
                )

            for filename, content in _STUBS.items():
                _write_stub(scripts_dir / filename, content)

            return [
                _upsert_job(
                    existing,
                    script=SCAN_SCRIPT,
                    name=SCAN_JOB_NAME,
                    schedule=scan_schedule,
                    deliver=target,
                ),
                _upsert_job(
                    existing,
                    script=REMIND_SCRIPT,
                    name=REMIND_JOB_NAME,
                    schedule=remind_schedule,
                    deliver=target,
                ),
            ]
        except BaseException as exc:
            rollback_errors: list[str] = []
            try:
                # ``save_jobs`` intentionally preserves disk-only IDs as
                # possible concurrent creates. Build the rollback payload
                # from a fresh disk snapshot so that protection remains
                # valid while our own newly-created canonical jobs are
                # explicitly removed.
                current_jobs = cron_jobs.load_jobs()
                original_ids = {
                    str(job.get("id"))
                    for job in original_jobs
                    if isinstance(job, dict) and job.get("id")
                }
                created_ids = {
                    str(job.get("id"))
                    for job in current_jobs
                    if isinstance(job, dict)
                    and job.get("id")
                    and str(job.get("id")) not in original_ids
                    and str(job.get("script") or "").strip() in _STUBS
                }
                concurrent_jobs = [
                    job
                    for job in current_jobs
                    if isinstance(job, dict)
                    and job.get("id")
                    and str(job.get("id")) not in (original_ids | created_ids)
                ]
                cron_jobs.save_jobs(
                    copy.deepcopy(original_jobs) + concurrent_jobs,
                    removed_ids=created_ids,
                )
            except Exception as rollback_exc:  # pragma: no cover - disk failure
                rollback_errors.append(f"cron jobs: {rollback_exc}")
            for path, snapshot in stub_snapshots.items():
                try:
                    if snapshot is None:
                        if path.exists() or path.is_symlink():
                            if path.is_dir() and not path.is_symlink():
                                raise OSError(
                                    f"rollback target became a directory: {path}"
                                )
                            path.unlink()
                    else:
                        previous_content, previous_mode = snapshot
                        _atomic_write_stub(path, previous_content, mode=previous_mode)
                except Exception as rollback_exc:  # pragma: no cover - disk failure
                    rollback_errors.append(f"{path}: {rollback_exc}")
            try:
                if scripts_dir.is_symlink():
                    raise OSError(
                        f"rollback scripts directory became a symlink: {scripts_dir}"
                    )
                if scripts_dir_existed and scripts_dir_mode is not None:
                    os.chmod(scripts_dir, scripts_dir_mode)
                elif not scripts_dir_existed and scripts_dir.exists():
                    scripts_dir.rmdir()
            except Exception as rollback_exc:  # pragma: no cover - disk failure
                rollback_errors.append(f"{scripts_dir}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    "watchlist install failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise


def scan_cli() -> None:
    from watchlist.scan import main as scan_main

    scan_main()


def remind_cli() -> None:
    from watchlist.remind import main as remind_main

    remind_main()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install safe daily scan and hourly reminder jobs for the IDO watchlist."
    )
    parser.add_argument(
        "--deliver",
        required=True,
        help="Explicit delivery target, such as telegram, discord:<channel>, or local.",
    )
    parser.add_argument("--scan-schedule", default=DEFAULT_SCAN_SCHEDULE)
    parser.add_argument("--remind-schedule", default=DEFAULT_REMIND_SCHEDULE)
    args = parser.parse_args(argv)

    try:
        jobs = install(
            deliver=args.deliver,
            scan_schedule=args.scan_schedule,
            remind_schedule=args.remind_schedule,
        )
    except ValueError as exc:
        parser.error(str(exc))
    for job in jobs:
        print(f"{job['name']}: {job['id']} ({job['schedule_display']})")
    print("Watchlist jobs installed with no agent and transcript mirroring disabled.")
    return 0


__all__ = [
    "SCAN_SCRIPT",
    "REMIND_SCRIPT",
    "DEFAULT_SCAN_SCHEDULE",
    "DEFAULT_REMIND_SCHEDULE",
    "install",
    "scan_cli",
    "remind_cli",
    "main",
]
