"""Profile-scoped SQLite persistence for the IDO watchlist.

Unlike the session store, this database contains user-owned watchlist data and
is kept as a sibling database at ``$HERMES_HOME/watchlist.db``.  The path is
resolved when :func:`connect` is called so profile switches and tests always
use the active Hermes home.

The store deliberately has no schema version table and no FTS5 index.  Its
schema is created with idempotent ``CREATE`` statements, and all data writes
use the shared IMMEDIATE transaction helper.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home


def watchlist_db_path() -> Path:
    """Return the active profile's watchlist database path."""

    return get_hermes_home() / "watchlist.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    ticker          TEXT,
    url             TEXT,
    first_seen_at   INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'candidate',
    notes           TEXT,
    archived        INTEGER NOT NULL DEFAULT 0,
    risk_score      INTEGER,
    risk_facts      TEXT,
    risk_coverage   REAL,
    asym_score      INTEGER,
    asym_facts      TEXT,
    asym_coverage   REAL,
    -- Which rubric produced the scores.  A scan-stage 72 is a weaker claim
    -- than a deep-stage 72 (it was never checked for an audit); rendering them
    -- identically would smuggle back the false confidence the coverage floor
    -- exists to prevent.
    score_stage     TEXT,
    UNIQUE(source, slug)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_projects_status
    ON projects(status, archived);
CREATE INDEX IF NOT EXISTS idx_watchlist_projects_updated_at
    ON projects(updated_at);

CREATE TABLE IF NOT EXISTS stages (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    label               TEXT NOT NULL,
    due_at              TEXT NOT NULL,
    schedule_revision   INTEGER NOT NULL DEFAULT 1,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    checklist_state     TEXT NOT NULL DEFAULT 'pending',
    done_at             INTEGER,
    source              TEXT NOT NULL DEFAULT 'scraped',
    url                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_watchlist_stages_project_due
    ON stages(project_id, due_at, sort_order);
CREATE INDEX IF NOT EXISTS idx_watchlist_stages_state
    ON stages(checklist_state);

CREATE TABLE IF NOT EXISTS outbox (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL UNIQUE,
    project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
    stage_id        TEXT REFERENCES stages(id) ON DELETE CASCADE,
    horizon         TEXT,
    state           TEXT NOT NULL DEFAULT 'pending',
    reason          TEXT,
    emit_run_id     TEXT,
    emit_attempts   INTEGER NOT NULL DEFAULT 0,
    emitted_at      INTEGER,
    settled_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_watchlist_outbox_state
    ON outbox(state, kind);
CREATE INDEX IF NOT EXISTS idx_watchlist_outbox_stage
    ON outbox(stage_id, state);

CREATE TABLE IF NOT EXISTS heartbeats (
    id          TEXT PRIMARY KEY,
    job         TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    ran_at      INTEGER NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    UNIQUE(job, run_id)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_heartbeats_job_ran
    ON heartbeats(job, ran_at);

CREATE TABLE IF NOT EXISTS job_observations (
    id                  TEXT PRIMARY KEY,
    job                 TEXT NOT NULL,
    cron_last_run_at    TEXT NOT NULL,
    last_status         TEXT NOT NULL,
    last_delivery_error TEXT,
    observed_at         INTEGER NOT NULL,
    observed_by         TEXT NOT NULL,
    UNIQUE(job, cron_last_run_at)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_observations_job_time
    ON job_observations(job, observed_at);
"""


_INITIALIZED_PATHS: set[str] = set()
_UNSET = object()


def _now() -> int:
    """Return an integer epoch timestamp, matching the sibling stores."""

    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _due_at(value: Any) -> str:
    """Normalize a stage deadline and require an explicit timezone offset."""

    text = str(value or "").strip()
    if not text:
        raise ValueError("stage due_at must not be empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("stage due_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stage due_at must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc).isoformat()


def _json_dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open and initialize the active profile's watchlist database."""

    path = Path(db_path) if db_path is not None else watchlist_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"refusing to open symlinked watchlist database: {path}")
    resolved = str(path.resolve())
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="watchlist.db")
        conn.execute("PRAGMA foreign_keys=ON")
        if resolved not in _INITIALIZED_PATHS:
            conn.executescript(SCHEMA_SQL)
            _INITIALIZED_PATHS.add(resolved)
    except Exception:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def connect_closing(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a watchlist DB connection and always close its file descriptor."""

    conn = connect(db_path=db_path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class Project:
    id: str
    source: str
    slug: str
    name: str
    ticker: Optional[str] = None
    url: Optional[str] = None
    first_seen_at: int = 0
    updated_at: int = 0
    status: str = "candidate"
    notes: Optional[str] = None
    archived: bool = False
    risk_score: Optional[int] = None
    risk_facts: Any = None
    risk_coverage: Optional[float] = None
    asym_score: Optional[int] = None
    asym_facts: Any = None
    asym_coverage: Optional[float] = None
    score_stage: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "slug": self.slug,
            "name": self.name,
            "ticker": self.ticker,
            "url": self.url,
            "first_seen_at": self.first_seen_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "notes": self.notes,
            "archived": bool(self.archived),
            "risk_score": self.risk_score,
            "risk_facts": self.risk_facts,
            "risk_coverage": self.risk_coverage,
            "asym_score": self.asym_score,
            "asym_facts": self.asym_facts,
            "asym_coverage": self.asym_coverage,
            "score_stage": self.score_stage,
        }


@dataclass
class Stage:
    id: str
    project_id: str
    kind: str
    label: str
    due_at: str
    schedule_revision: int = 1
    sort_order: int = 0
    checklist_state: str = "pending"
    done_at: Optional[int] = None
    source: str = "scraped"
    url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind,
            "label": self.label,
            "due_at": self.due_at,
            "schedule_revision": self.schedule_revision,
            "sort_order": self.sort_order,
            "checklist_state": self.checklist_state,
            "done_at": self.done_at,
            "source": self.source,
            "url": self.url,
        }


@dataclass
class Outbox:
    id: str
    kind: str
    dedupe_key: str
    project_id: Optional[str] = None
    stage_id: Optional[str] = None
    horizon: Optional[str] = None
    state: str = "pending"
    reason: Optional[str] = None
    emit_run_id: Optional[str] = None
    emit_attempts: int = 0
    emitted_at: Optional[int] = None
    settled_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "dedupe_key": self.dedupe_key,
            "project_id": self.project_id,
            "stage_id": self.stage_id,
            "horizon": self.horizon,
            "state": self.state,
            "reason": self.reason,
            "emit_run_id": self.emit_run_id,
            "emit_attempts": self.emit_attempts,
            "emitted_at": self.emitted_at,
            "settled_at": self.settled_at,
        }


@dataclass
class Heartbeat:
    id: str
    job: str
    run_id: str
    ran_at: int
    status: str
    detail: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job": self.job,
            "run_id": self.run_id,
            "ran_at": self.ran_at,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class JobObservation:
    id: str
    job: str
    cron_last_run_at: str
    last_status: str
    last_delivery_error: Optional[str]
    observed_at: int
    observed_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job": self.job,
            "cron_last_run_at": self.cron_last_run_at,
            "last_status": self.last_status,
            "last_delivery_error": self.last_delivery_error,
            "observed_at": self.observed_at,
            "observed_by": self.observed_by,
        }


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        source=row["source"],
        slug=row["slug"],
        name=row["name"],
        ticker=row["ticker"],
        url=row["url"],
        first_seen_at=row["first_seen_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        notes=row["notes"],
        archived=bool(row["archived"]),
        risk_score=row["risk_score"],
        risk_facts=_json_load(row["risk_facts"]),
        risk_coverage=row["risk_coverage"],
        asym_score=row["asym_score"],
        asym_facts=_json_load(row["asym_facts"]),
        asym_coverage=row["asym_coverage"],
        score_stage=row["score_stage"],
    )


def _stage_from_row(row: sqlite3.Row) -> Stage:
    return Stage(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        label=row["label"],
        due_at=row["due_at"],
        schedule_revision=row["schedule_revision"],
        sort_order=row["sort_order"],
        checklist_state=row["checklist_state"],
        done_at=row["done_at"],
        source=row["source"],
        url=row["url"],
    )


def _outbox_from_row(row: sqlite3.Row) -> Outbox:
    return Outbox(
        id=row["id"],
        kind=row["kind"],
        dedupe_key=row["dedupe_key"],
        project_id=row["project_id"],
        stage_id=row["stage_id"],
        horizon=row["horizon"],
        state=row["state"],
        reason=row["reason"],
        emit_run_id=row["emit_run_id"],
        emit_attempts=row["emit_attempts"],
        emitted_at=row["emitted_at"],
        settled_at=row["settled_at"],
    )


def _heartbeat_from_row(row: sqlite3.Row) -> Heartbeat:
    return Heartbeat(
        id=row["id"],
        job=row["job"],
        run_id=row["run_id"],
        ran_at=row["ran_at"],
        status=row["status"],
        detail=_json_load(row["detail"]),
    )


def _observation_from_row(row: sqlite3.Row) -> JobObservation:
    return JobObservation(
        id=row["id"],
        job=row["job"],
        cron_last_run_at=row["cron_last_run_at"],
        last_status=row["last_status"],
        last_delivery_error=row["last_delivery_error"],
        observed_at=row["observed_at"],
        observed_by=row["observed_by"],
    )


def create_project(
    conn: sqlite3.Connection,
    *,
    source: str,
    slug: str,
    name: str,
    ticker: Optional[str] = None,
    url: Optional[str] = None,
    status: str = "candidate",
    notes: Optional[str] = None,
    archived: bool = False,
    risk_score: Optional[int] = None,
    risk_facts: Any = None,
    risk_coverage: Optional[float] = None,
    asym_score: Optional[int] = None,
    asym_facts: Any = None,
    asym_coverage: Optional[float] = None,
    score_stage: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """Create a project and return its id."""

    source = str(source or "").strip()
    slug = str(slug or "").strip()
    name = str(name or "").strip()
    if not source or not slug or not name:
        raise ValueError("source, slug, and name must not be empty")
    if status not in {"candidate", "watching", "dropped", "done"}:
        raise ValueError(f"invalid project status: {status!r}")
    pid = project_id or _new_id("p")
    timestamp = _now()
    with write_txn(conn):
        conn.execute(
            "INSERT INTO projects ("
            "id, source, slug, name, ticker, url, first_seen_at, updated_at, "
            "status, notes, archived, risk_score, risk_facts, risk_coverage, "
            "asym_score, asym_facts, asym_coverage, score_stage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                source,
                slug,
                name,
                ticker,
                url,
                timestamp,
                timestamp,
                status,
                notes,
                int(archived),
                risk_score,
                _json_dump(risk_facts),
                risk_coverage,
                asym_score,
                _json_dump(asym_facts),
                asym_coverage,
                score_stage,
            ),
        )
    return pid


def upsert_project(
    conn: sqlite3.Connection,
    *,
    source: str,
    slug: str,
    name: str,
    ticker: Optional[str] = None,
    url: Optional[str] = None,
    risk_score: Any = _UNSET,
    risk_facts: Any = _UNSET,
    risk_coverage: Any = _UNSET,
    asym_score: Any = _UNSET,
    asym_facts: Any = _UNSET,
    asym_coverage: Any = _UNSET,
    score_stage: Any = _UNSET,
) -> str:
    """Insert or refresh a source row without clobbering user edits.

    On conflict only source-owned fields and supplied scores are updated.
    ``status``, ``notes``, and ``archived`` remain untouched so a daily scan
    cannot undo a user's promotion or annotations.
    """

    source = str(source or "").strip()
    slug = str(slug or "").strip()
    name = str(name or "").strip()
    if not source or not slug or not name:
        raise ValueError("source, slug, and name must not be empty")
    timestamp = _now()
    with write_txn(conn):
        row = conn.execute(
            "SELECT id, score_stage FROM projects WHERE source = ? AND slug = ?",
            (source, slug),
        ).fetchone()
        if row is None:
            pid = _new_id("p")
            conn.execute(
                "INSERT INTO projects (id, source, slug, name, ticker, url, "
                "first_seen_at, updated_at, status, risk_score, risk_facts, "
                "risk_coverage, asym_score, asym_facts, asym_coverage, score_stage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    source,
                    slug,
                    name,
                    ticker,
                    url,
                    timestamp,
                    timestamp,
                    None if risk_score is _UNSET else risk_score,
                    None if risk_facts is _UNSET else _json_dump(risk_facts),
                    None if risk_coverage is _UNSET else risk_coverage,
                    None if asym_score is _UNSET else asym_score,
                    None if asym_facts is _UNSET else _json_dump(asym_facts),
                    None if asym_coverage is _UNSET else asym_coverage,
                    None if score_stage is _UNSET else score_stage,
                ),
            )
            return pid

        pid = row["id"]
        sets = ["name = ?", "ticker = ?", "url = ?", "updated_at = ?"]
        params: list[Any] = [name, ticker, url, timestamp]

        # A deep-stage score is verified work the user initiated; the daily scan
        # only ever has listing-grade facts.  Letting a scan write over it would
        # quietly downgrade a promoted project's diligence back to scan-grade
        # every morning — the same class of loss as clobbering their notes, and
        # invisible because the number would still look like a number.  Enforced
        # here rather than at the caller so no future caller can get it wrong.
        existing_stage = row["score_stage"] if "score_stage" in row.keys() else None
        downgrade = (
            existing_stage == "deep"
            and score_stage is not _UNSET
            and score_stage != "deep"
        )
        supplied_scores = (
            ("risk_score", risk_score),
            ("risk_facts", risk_facts),
            ("risk_coverage", risk_coverage),
            ("asym_score", asym_score),
            ("asym_facts", asym_facts),
            ("asym_coverage", asym_coverage),
            ("score_stage", score_stage),
        )
        if not downgrade:
            for column, value in supplied_scores:
                if value is not _UNSET:
                    sets.append(f"{column} = ?")
                    params.append(
                        _json_dump(value) if column.endswith("facts") else value
                    )
        params.append(pid)
        conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params)
        return pid


def get_project(conn: sqlite3.Connection, project_id: str) -> Optional[Project]:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = ? ORDER BY updated_at DESC LIMIT 1",
            (str(project_id).lower(),),
        ).fetchone()
    return _project_from_row(row) if row is not None else None


def get_project_by_source_slug(
    conn: sqlite3.Connection, source: str, slug: str
) -> Optional[Project]:
    row = conn.execute(
        "SELECT * FROM projects WHERE source = ? AND slug = ?", (source, slug)
    ).fetchone()
    return _project_from_row(row) if row is not None else None


def list_projects(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = False,
    status: Optional[str] = None,
) -> list[Project]:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_archived:
        clauses.append("archived = 0")
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM projects"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, first_seen_at ASC"
    return [_project_from_row(row) for row in conn.execute(sql, params).fetchall()]


def update_project(
    conn: sqlite3.Connection,
    project_id: str,
    **fields: Any,
) -> bool:
    """Patch project fields, leaving unspecified fields unchanged."""

    allowed = {
        "name",
        "ticker",
        "url",
        "status",
        "notes",
        "archived",
        "risk_score",
        "risk_facts",
        "risk_coverage",
        "asym_score",
        "asym_facts",
        "asym_coverage",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown project fields: {', '.join(sorted(unknown))}")
    if "name" in fields and not str(fields["name"] or "").strip():
        raise ValueError("project name must not be empty")
    if "status" in fields and fields["status"] not in {
        "candidate",
        "watching",
        "dropped",
        "done",
    }:
        raise ValueError(f"invalid project status: {fields['status']!r}")
    if not fields:
        return False

    sets = []
    params: list[Any] = []
    for column, value in fields.items():
        if column == "name":
            value = str(value).strip()
        elif column == "archived":
            value = int(bool(value))
        elif column.endswith("_facts"):
            value = _json_dump(value)
        sets.append(f"{column} = ?")
        params.append(value)
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(project_id)
    with write_txn(conn):
        cur = conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
        )
    return cur.rowcount > 0


def archive_project(conn: sqlite3.Connection, project_id: str) -> bool:
    return update_project(conn, project_id, archived=True)


def delete_project(conn: sqlite3.Connection, project_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return cur.rowcount > 0


def create_stage(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    kind: str,
    label: str,
    due_at: str,
    schedule_revision: int = 1,
    sort_order: int = 0,
    checklist_state: str = "pending",
    done_at: Optional[int] = None,
    source: str = "scraped",
    url: Optional[str] = None,
    stage_id: Optional[str] = None,
) -> str:
    """Create a dated project stage and return its id."""

    if checklist_state not in {"pending", "done", "skipped"}:
        raise ValueError(f"invalid checklist state: {checklist_state!r}")
    sid = stage_id or _new_id("s")
    with write_txn(conn):
        conn.execute(
            "INSERT INTO stages (id, project_id, kind, label, due_at, "
            "schedule_revision, sort_order, checklist_state, done_at, source, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                project_id,
                kind,
                label,
                _due_at(due_at),
                schedule_revision,
                sort_order,
                checklist_state,
                done_at,
                source,
                url,
            ),
        )
    return sid


def get_stage(conn: sqlite3.Connection, stage_id: str) -> Optional[Stage]:
    row = conn.execute("SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()
    return _stage_from_row(row) if row is not None else None


def list_stages(
    conn: sqlite3.Connection, project_id: str, *, include_completed: bool = True
) -> list[Stage]:
    sql = "SELECT * FROM stages WHERE project_id = ?"
    params: list[Any] = [project_id]
    if not include_completed:
        sql += " AND checklist_state = 'pending'"
    sql += " ORDER BY sort_order ASC, due_at ASC, id ASC"
    return [_stage_from_row(row) for row in conn.execute(sql, params).fetchall()]


def update_stage(conn: sqlite3.Connection, stage_id: str, **fields: Any) -> bool:
    """Patch a stage; rescheduling re-arms its old reminder horizons."""

    allowed = {
        "kind",
        "label",
        "due_at",
        "sort_order",
        "checklist_state",
        "done_at",
        "source",
        "url",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown stage fields: {', '.join(sorted(unknown))}")
    if "checklist_state" in fields and fields["checklist_state"] not in {
        "pending",
        "done",
        "skipped",
    }:
        raise ValueError(f"invalid checklist state: {fields['checklist_state']!r}")
    if not fields:
        return False

    with write_txn(conn):
        row = conn.execute("SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()
        if row is None:
            return False
        sets: list[str] = []
        params: list[Any] = []
        if "due_at" in fields:
            fields["due_at"] = _due_at(fields["due_at"])
        due_changed = "due_at" in fields and fields["due_at"] != row["due_at"]
        for column, value in fields.items():
            sets.append(f"{column} = ?")
            params.append(value)
        if "checklist_state" in fields and fields["checklist_state"] in {
            "done",
            "skipped",
        }:
            if "done_at" not in fields:
                sets.append("done_at = ?")
                params.append(_now())
        elif fields.get("checklist_state") == "pending":
            sets.append("done_at = NULL")
        if due_changed:
            next_revision = int(row["schedule_revision"]) + 1
            sets.append("schedule_revision = ?")
            params.append(next_revision)
        params.append(stage_id)
        cur = conn.execute(f"UPDATE stages SET {', '.join(sets)} WHERE id = ?", params)
        if due_changed:
            now = _now()
            conn.execute(
                "UPDATE outbox SET state = 'suppressed', reason = ?, settled_at = ? "
                "WHERE stage_id = ? AND state IN ('pending', 'emitted') "
                "AND dedupe_key NOT LIKE ?",
                (
                    "schedule_revised",
                    now,
                    stage_id,
                    f"stage:{stage_id}:r{int(row['schedule_revision']) + 1}:%",
                ),
            )
    return cur.rowcount > 0


def set_stage_state(
    conn: sqlite3.Connection, stage_id: str, checklist_state: str
) -> bool:
    return update_stage(conn, stage_id, checklist_state=checklist_state)


def create_outbox(
    conn: sqlite3.Connection,
    *,
    kind: str,
    dedupe_key: str,
    project_id: Optional[str] = None,
    stage_id: Optional[str] = None,
    horizon: Optional[str] = None,
    state: str = "pending",
    reason: Optional[str] = None,
) -> str:
    """Create one deduplicated outbound row and return its id."""

    oid = _new_id("o")
    with write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO outbox (id, kind, dedupe_key, project_id, "
            "stage_id, horizon, state, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, kind, dedupe_key, project_id, stage_id, horizon, state, reason),
        )
        row = conn.execute(
            "SELECT id FROM outbox WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
    return row["id"]


def get_outbox(conn: sqlite3.Connection, outbox_id: str) -> Optional[Outbox]:
    row = conn.execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
    return _outbox_from_row(row) if row is not None else None


def list_outbox(
    conn: sqlite3.Connection,
    *,
    state: Optional[str] = None,
    kind: Optional[str] = None,
    stage_id: Optional[str] = None,
) -> list[Outbox]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (("state", state), ("kind", kind), ("stage_id", stage_id)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = "SELECT * FROM outbox"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id ASC"
    return [_outbox_from_row(row) for row in conn.execute(sql, params).fetchall()]


def emit_outbox(
    conn: sqlite3.Connection,
    outbox_id: str,
    *,
    emit_run_id: str,
    emitted_at: Optional[int] = None,
) -> bool:
    # Atomic compare-and-swap on state.  Without the `state = 'pending'` guard a
    # confirmed, suppressed or abandoned row could be re-emitted — resurrecting a
    # terminal state and re-sending a message the user already received — and two
    # concurrent emitters would both report success and both send.  The UPDATE's
    # own rowcount is the lock: exactly one caller can move a row out of pending.
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE outbox SET state = 'emitted', emit_run_id = ?, "
            "emit_attempts = emit_attempts + 1, emitted_at = ? "
            "WHERE id = ? AND state = 'pending'",
            (emit_run_id, _now() if emitted_at is None else emitted_at, outbox_id),
        )
    return cur.rowcount > 0


#: Legal predecessors for each settle target.  `confirmed` requires `emitted`
#: because you can only confirm delivery of something that was actually sent.
#: `suppressed` accepts `pending` (horizon_superseded / schedule_revised, never
#: emitted) as well as `emitted`.  `pending` is the requeue after a failed emit.
_SETTLE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "confirmed": ("emitted",),
    "suppressed": ("pending", "emitted"),
    "abandoned": ("pending", "emitted"),
    "pending": ("emitted",),
}


def settle_outbox(
    conn: sqlite3.Connection,
    outbox_id: str,
    state: str,
    *,
    reason: Optional[str] = None,
    settled_at: Optional[int] = None,
) -> bool:
    allowed_from = _SETTLE_TRANSITIONS.get(state)
    if allowed_from is None:
        raise ValueError(f"invalid outbox state: {state!r}")
    timestamp = settled_at if settled_at is not None else _now()
    # Target-specific guards, not a blanket "is it live?" check.  `confirmed`
    # is only reachable from `emitted`: confirming a `pending` row would mark a
    # reminder the user never received as delivered — the precise lie this
    # outbox exists to prevent — and a stale reconciliation race could do it.
    placeholders = ", ".join("?" for _ in allowed_from)
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE outbox SET state = ?, reason = ?, settled_at = ? "
            f"WHERE id = ? AND state IN ({placeholders})",
            (
                state,
                reason,
                timestamp if state != "pending" else None,
                outbox_id,
                *allowed_from,
            ),
        )
    return cur.rowcount > 0


def record_heartbeat(
    conn: sqlite3.Connection,
    *,
    job: str,
    run_id: str,
    status: str,
    detail: Any = None,
    ran_at: Optional[int] = None,
) -> str:
    heartbeat_id = _new_id("h")
    with write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO heartbeats "
            "(id, job, run_id, ran_at, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (
                heartbeat_id,
                job,
                run_id,
                _now() if ran_at is None else ran_at,
                status,
                _json_dump(detail),
            ),
        )
        row = conn.execute(
            "SELECT id FROM heartbeats WHERE job = ? AND run_id = ?", (job, run_id)
        ).fetchone()
    return row["id"]


def list_heartbeats(
    conn: sqlite3.Connection, *, job: Optional[str] = None
) -> list[Heartbeat]:
    if job is None:
        rows = conn.execute("SELECT * FROM heartbeats ORDER BY ran_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM heartbeats WHERE job = ? ORDER BY ran_at DESC", (job,)
        ).fetchall()
    return [_heartbeat_from_row(row) for row in rows]


def record_job_observation(
    conn: sqlite3.Connection,
    *,
    job: str,
    cron_last_run_at: str,
    last_status: str,
    last_delivery_error: Optional[str] = None,
    observed_at: Optional[int] = None,
    observed_by: str = "watchlist",
) -> str:
    observation_id = _new_id("j")
    with write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO job_observations "
            "(id, job, cron_last_run_at, last_status, last_delivery_error, "
            "observed_at, observed_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                observation_id,
                job,
                str(cron_last_run_at),
                last_status,
                last_delivery_error,
                _now() if observed_at is None else observed_at,
                observed_by,
            ),
        )
        row = conn.execute(
            "SELECT id FROM job_observations WHERE job = ? AND cron_last_run_at = ?",
            (job, str(cron_last_run_at)),
        ).fetchone()
    return row["id"]


def list_job_observations(
    conn: sqlite3.Connection, *, job: Optional[str] = None
) -> list[JobObservation]:
    if job is None:
        rows = conn.execute(
            "SELECT * FROM job_observations ORDER BY observed_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM job_observations WHERE job = ? ORDER BY observed_at DESC",
            (job,),
        ).fetchall()
    return [_observation_from_row(row) for row in rows]


__all__ = [
    "SCHEMA_SQL",
    "Project",
    "Stage",
    "Outbox",
    "Heartbeat",
    "JobObservation",
    "watchlist_db_path",
    "connect",
    "connect_closing",
    "create_project",
    "upsert_project",
    "get_project",
    "get_project_by_source_slug",
    "list_projects",
    "update_project",
    "archive_project",
    "delete_project",
    "create_stage",
    "get_stage",
    "list_stages",
    "update_stage",
    "set_stage_state",
    "create_outbox",
    "get_outbox",
    "list_outbox",
    "emit_outbox",
    "settle_outbox",
    "record_heartbeat",
    "list_heartbeats",
    "record_job_observation",
    "list_job_observations",
]
