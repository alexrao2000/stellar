"""Storage backends for completion and metric rows.

Two interchangeable backends:
  - SQLiteStore: zero-setup local file.
  - SupabaseStore: hosted Postgres via supabase-py.

Both upsert idempotently so the sync can run as often as you like.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from .config import Config
from .parsers import CompletionRow, WakeUpRow, WeightRow


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store(Protocol):
    def upsert_completions(self, rows: list[CompletionRow]) -> None: ...
    def upsert_wake_ups(self, rows: list[WakeUpRow]) -> None: ...
    def upsert_weights(self, rows: list[WeightRow]) -> None: ...


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS task_completions (
    task_id      TEXT PRIMARY KEY,
    list_id      TEXT,
    list_title   TEXT,
    title        TEXT,
    status       TEXT,
    day          TEXT,
    due          TEXT,
    completed_at TEXT,
    notes        TEXT,
    updated_at   TEXT,
    is_completed INTEGER,
    source       TEXT DEFAULT 'tasks',
    synced_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_completions_day ON task_completions(day);
CREATE INDEX IF NOT EXISTS idx_task_completions_title ON task_completions(title);

CREATE TABLE IF NOT EXISTS wake_up_log (
    day            TEXT PRIMARY KEY,
    wake_time      TEXT,
    wake_minutes   INTEGER,
    raw_notes      TEXT,
    source_task_id TEXT,
    synced_at      TEXT
);

CREATE TABLE IF NOT EXISTS weight_log (
    day              TEXT PRIMARY KEY,
    weight_lbs       REAL,
    start_date       TEXT,
    start_weight_lbs REAL,
    raw_notes        TEXT,
    source_task_id   TEXT,
    synced_at        TEXT
);
"""


class SQLiteStore:
    def __init__(self, path: str):
        self.path = path
        with sqlite3.connect(self.path) as conn:
            conn.executescript(_SQLITE_DDL)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a DB was first created."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(task_completions)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE task_completions ADD COLUMN source TEXT DEFAULT 'tasks'")

    def _upsert(self, table: str, pk: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({pk}) DO UPDATE SET {updates}"
        )
        with sqlite3.connect(self.path) as conn:
            conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])

    def upsert_completions(self, rows: list[CompletionRow]) -> None:
        payload = []
        for r in rows:
            d = asdict(r)
            d["is_completed"] = 1 if r.is_completed else 0
            d["synced_at"] = _now()
            payload.append(d)
        self._upsert("task_completions", "task_id", payload)

    def upsert_wake_ups(self, rows: list[WakeUpRow]) -> None:
        payload = [{**asdict(r), "synced_at": _now()} for r in rows]
        self._upsert("wake_up_log", "day", payload)

    def upsert_weights(self, rows: list[WeightRow]) -> None:
        payload = [{**asdict(r), "synced_at": _now()} for r in rows]
        self._upsert("weight_log", "day", payload)


# --------------------------------------------------------------------------- #
# Supabase
# --------------------------------------------------------------------------- #
class SupabaseStore:
    def __init__(self, url: str, key: str):
        from supabase import create_client

        self.client = create_client(url, key)

    def upsert_completions(self, rows: list[CompletionRow]) -> None:
        if not rows:
            return
        payload = [{**asdict(r), "synced_at": _now()} for r in rows]
        self.client.table("task_completions").upsert(payload, on_conflict="task_id").execute()

    def upsert_wake_ups(self, rows: list[WakeUpRow]) -> None:
        if not rows:
            return
        payload = [{**asdict(r), "synced_at": _now()} for r in rows]
        self.client.table("wake_up_log").upsert(payload, on_conflict="day").execute()

    def upsert_weights(self, rows: list[WeightRow]) -> None:
        if not rows:
            return
        payload = [{**asdict(r), "synced_at": _now()} for r in rows]
        self.client.table("weight_log").upsert(payload, on_conflict="day").execute()


def make_store(config: Config) -> Store:
    if config.storage_backend == "supabase":
        return SupabaseStore(config.supabase_url, config.supabase_key)
    return SQLiteStore(config.sqlite_path)
