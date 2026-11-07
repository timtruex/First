"""SQLite-backed job store. The bot writes jobs; the worker claims and advances them."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

STATES = (
    "collecting",   # bot is still receiving images / notes
    "queued",       # ready for the worker
    "scripting",
    "awaiting_approval",
    "voicing",
    "rendering",
    "done",
    "failed",
    "cancelled",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    chat_id       INTEGER,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    affiliate_url TEXT NOT NULL,
    asin          TEXT,
    payload       TEXT NOT NULL DEFAULT '{}',
    error         TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs(state, created_at);
CREATE TABLE IF NOT EXISTS job_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL REFERENCES jobs(id),
    at       REAL NOT NULL,
    state    TEXT NOT NULL,
    note     TEXT
);
"""


@dataclass
class Job:
    id: str
    state: str
    chat_id: int | None
    created_at: float
    updated_at: float
    affiliate_url: str
    asin: str | None
    payload: dict[str, Any]
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            state=row["state"],
            chat_id=row["chat_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            affiliate_url=row["affiliate_url"],
            asin=row["asin"],
            payload=json.loads(row["payload"] or "{}"),
            error=row["error"],
        )


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    def create(self, affiliate_url: str, asin: str | None, chat_id: int | None) -> Job:
        now = time.time()
        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, state, chat_id, created_at, updated_at, affiliate_url, asin, payload)"
                " VALUES (?,?,?,?,?,?,?,'{}')",
                (job_id, "collecting", chat_id, now, now, affiliate_url, asin),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"no such job: {job_id}")
        return Job.from_row(row)

    def latest_collecting(self, chat_id: int) -> Job | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE chat_id = ? AND state = 'collecting'"
                " ORDER BY created_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return Job.from_row(row) if row else None

    def latest_for_chat(self, chat_id: int, states: tuple[str, ...] = ()) -> Job | None:
        q = "SELECT * FROM jobs WHERE chat_id = ?"
        args: list[Any] = [chat_id]
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args.extend(states)
        q += " ORDER BY created_at DESC LIMIT 1"
        with self._conn() as c:
            row = c.execute(q, args).fetchone()
        return Job.from_row(row) if row else None

    def recent_for_asin(
        self, chat_id: int | None, asin: str, states: tuple[str, ...], max_age_s: float
    ) -> Job | None:
        """A job for this product that is still open for more photos."""
        cutoff = time.time() - max_age_s
        q = (
            f"SELECT * FROM jobs WHERE asin = ? AND created_at > ?"
            f" AND state IN ({','.join('?' * len(states))})"
        )
        args: list[Any] = [asin, cutoff, *states]
        if chat_id is not None:
            q += " AND chat_id = ?"
            args.append(chat_id)
        q += " ORDER BY created_at DESC LIMIT 1"
        with self._conn() as c:
            row = c.execute(q, args).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, states: tuple[str, ...] = (), limit: int = 20) -> list[Job]:
        q = "SELECT * FROM jobs"
        args: list[Any] = []
        if states:
            q += f" WHERE state IN ({','.join('?' * len(states))})"
            args.extend(states)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [Job.from_row(r) for r in c.execute(q, args).fetchall()]

    def claim_next(self, from_state: str, to_state: str) -> Job | None:
        """Atomically move the oldest job out of `from_state` so two workers never share one."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY created_at LIMIT 1", (from_state,)
            ).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                (to_state, time.time(), row["id"]),
            )
            c.execute("COMMIT")
            job = Job.from_row(row)
        job.state = to_state
        self.log(job.id, to_state, "claimed")
        return job

    def set_state(self, job_id: str, state: str, note: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state: {state}")
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                (state, time.time(), job_id),
            )
        self.log(job_id, state, note)

    def fail(self, job_id: str, error: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET state = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (error[:4000], time.time(), job_id),
            )
        self.log(job_id, "failed", error[:500])

    def merge_payload(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                c.execute("COMMIT")
                raise KeyError(job_id)
            payload = json.loads(row["payload"] or "{}")
            payload.update(patch)
            c.execute(
                "UPDATE jobs SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload), time.time(), job_id),
            )
            c.execute("COMMIT")
        return payload

    def log(self, job_id: str, state: str, note: str | None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO job_events (job_id, at, state, note) VALUES (?,?,?,?)",
                (job_id, time.time(), state, note),
            )
