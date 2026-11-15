"""SQLite job queue.

One GPU job at a time (spec 6.5 resource rule), so the worker is a single
loop and the queue only needs to be durable, not concurrent. A job left
`running` by a killed worker is re-queued at startup; because each segment is
its own job and writes its own output file, a restart resumes from the last
finished segment (R7).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  dedupe TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

MAX_ATTEMPTS = 3


class JobQueue:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def enqueue(self, session: str, kind: str, payload: dict | None = None, dedupe: str | None = None) -> int | None:
        """Add a job. A `dedupe` key makes re-enqueueing the same work a no-op
        unless the earlier job failed, so the API can call this freely."""
        now = time.time()
        with self._conn() as c:
            if dedupe:
                row = c.execute("SELECT id, status FROM jobs WHERE dedupe=?", (dedupe,)).fetchone()
                if row:
                    if row["status"] == "failed":
                        c.execute("UPDATE jobs SET status='queued', attempts=0, error=NULL, updated=? WHERE id=?", (now, row["id"]))
                    return row["id"]
            cur = c.execute(
                "INSERT INTO jobs(session, kind, payload, created, updated, dedupe) VALUES (?,?,?,?,?,?)",
                (session, kind, json.dumps(payload or {}), now, now, dedupe),
            )
            return cur.lastrowid

    def recover(self) -> int:
        with self._conn() as c:
            return c.execute("UPDATE jobs SET status='queued', updated=? WHERE status='running'", (time.time(),)).rowcount

    def claim(self) -> dict | None:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            # Transcription before summarization: a summarize job must never
            # take the GPU while segments of any session still wait for STT.
            row = c.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY CASE kind WHEN 'transcribe' THEN 0 ELSE 1 END, id LIMIT 1"
            ).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated=? WHERE id=?", (time.time(), row["id"]))
            c.execute("COMMIT")
            d = dict(row)
            d["payload"] = json.loads(d["payload"])
            d["attempts"] += 1
            return d

    def finish(self, job_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET status='done', error=NULL, updated=? WHERE id=?", (time.time(), job_id))

    def fail(self, job_id: int, error: str, attempts: int) -> None:
        status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        with self._conn() as c:
            c.execute("UPDATE jobs SET status=?, error=?, updated=? WHERE id=?", (status, error[-2000:], time.time(), job_id))

    def for_session(self, session: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, kind, payload, status, attempts, error FROM jobs WHERE session=? ORDER BY id", (session,)).fetchall()
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def pending(self, session: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
        args: tuple[Any, ...] = ()
        if session:
            q += " AND session=?"
            args = (session,)
        with self._conn() as c:
            return c.execute(q, args).fetchone()[0]

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
