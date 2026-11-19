"""Episode ledger. Every stage transition is recorded so a run is resumable and auditable."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class Stage(StrEnum):
    TOPIC = "topic"
    DOSSIER = "dossier"
    SCRIPT = "script"
    FACT_CHECK = "fact_check"
    RENDER = "render"
    ASSEMBLE = "assemble"
    QC = "qc"
    PUBLISH_PRIVATE = "publish_private"
    HUMAN_REVIEW = "human_review"


ORDER: tuple[Stage, ...] = tuple(Stage)

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
  slug TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
  detail TEXT, artifact TEXT, at TEXT NOT NULL,
  FOREIGN KEY (slug) REFERENCES episodes(slug)
);
CREATE TABLE IF NOT EXISTS topic_history (
  slug TEXT PRIMARY KEY, topic TEXT NOT NULL, published_at TEXT
);
"""


@dataclass(slots=True)
class Ledger:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def ensure_episode(self, slug: str, title: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO episodes(slug,title,created_at) VALUES (?,?,?)",
                (slug, title, self._now()),
            )

    def record(self, slug: str, stage: Stage, status: str,
               detail: str = "", artifact: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO stage_runs(slug,stage,status,detail,artifact,at) VALUES (?,?,?,?,?,?)",
                (slug, str(stage), status, detail, artifact, self._now()),
            )

    def completed(self, slug: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT stage FROM stage_runs WHERE slug=? AND status='ok'", (slug,)
            ).fetchall()
        return {r["stage"] for r in rows}

    def next_stage(self, slug: str) -> Stage | None:
        done = self.completed(slug)
        return next((s for s in ORDER if str(s) not in done), None)

    def history(self, slug: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT stage,status,detail,artifact,at FROM stage_runs WHERE slug=? ORDER BY id",
                (slug,),
            ).fetchall()
        return [dict(r) for r in rows]
