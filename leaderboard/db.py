"""SQLite layer for the public leaderboard.

Schema is intentionally tiny: one `submissions` row per URL someone submitted,
plus the score breakdown JSON-encoded so we don't have to schema-migrate every
time the rubric changes.

Connections are per-call (sqlite3 is happy with that) and we use WAL so the
background worker can write while the API thread reads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT    NOT NULL,
    title           TEXT    NOT NULL DEFAULT '',
    brief           TEXT    NOT NULL DEFAULT '',
    submitter       TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    error           TEXT    NOT NULL DEFAULT '',
    combined        REAL,                                 -- 0-10, null until done
    score_json      TEXT,                                 -- full scores.json dump
    artifacts_dir   TEXT    NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,                     -- unix seconds
    finished_at     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_submissions_status   ON submissions (status);
CREATE INDEX IF NOT EXISTS idx_submissions_combined ON submissions (combined DESC);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(path: Path):
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def insert_submission(
    db_path: Path,
    url: str,
    title: str,
    brief: str,
    submitter: str,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO submissions (url, title, brief, submitter, status, created_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?)",
            (url, title, brief, submitter, int(time.time())),
        )
        return int(cur.lastrowid)


def claim_next_queued(db_path: Path) -> dict | None:
    """Atomically mark one queued submission as running and return it."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM submissions WHERE status = 'queued' "
            "ORDER BY created_at ASC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE submissions SET status = 'running' WHERE id = ?", (row["id"],)
        )
        return dict(row)


def mark_done(
    db_path: Path,
    sub_id: int,
    combined: float,
    score_json: dict[str, Any],
    artifacts_dir: str,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE submissions SET status = 'done', combined = ?, score_json = ?, "
            "artifacts_dir = ?, finished_at = ? WHERE id = ?",
            (
                float(combined),
                json.dumps(score_json),
                artifacts_dir,
                int(time.time()),
                sub_id,
            ),
        )


def mark_failed(db_path: Path, sub_id: int, error: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE submissions SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (error[:2000], int(time.time()), sub_id),
        )


def get_submission(db_path: Path, sub_id: int) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (sub_id,)
        ).fetchone()
        return dict(row) if row else None


def leaderboard(db_path: Path, limit: int = 200) -> list[dict]:
    """Top scored submissions (done only), sorted by combined desc."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, title, brief, submitter, combined, created_at, finished_at "
            "FROM submissions WHERE status = 'done' AND combined IS NOT NULL "
            "ORDER BY combined DESC, finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def queue_state(db_path: Path) -> dict:
    """Counts by status, for the live UI."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM submissions GROUP BY status"
        ).fetchall()
    out = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


def recent(db_path: Path, limit: int = 20) -> list[dict]:
    """Latest submissions of any status — for an activity feed."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, url, title, submitter, status, combined, created_at "
            "FROM submissions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
