"""One-time migration of local SQLite data into Supabase.

Reads all rows from the local SQLite tables and upserts them into Supabase using
the same store abstraction. Safe to re-run (idempotent upserts).

Usage:
  1. Ensure .env has SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
  2. Apply sql/schema.sql to the Supabase project.
  3. Run:  python -m src.migrate_to_supabase
"""
from __future__ import annotations

import logging
import sqlite3
import sys

from .config import load_config
from .parsers import CompletionRow, WakeUpRow, WeightRow
from .store import SupabaseStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cal-task-sync")

BATCH = 500


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    except sqlite3.OperationalError:
        return []


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def run() -> int:
    config = load_config()
    if not config.supabase_url or not config.supabase_key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env.")
        return 2

    store = SupabaseStore(config.supabase_url, config.supabase_key)
    conn = sqlite3.connect(config.sqlite_path)

    completions = _rows(conn, "task_completions")
    wake = _rows(conn, "wake_up_log")
    weight = _rows(conn, "weight_log")
    log.info("Read %d completions, %d wake, %d weight from SQLite.", len(completions), len(wake), len(weight))

    for batch in _chunk(completions, BATCH):
        store.upsert_completions(
            [
                CompletionRow(
                    task_id=r["task_id"],
                    list_id=r.get("list_id"),
                    list_title=r.get("list_title"),
                    title=r.get("title"),
                    status=r.get("status"),
                    day=r.get("day"),
                    due=r.get("due"),
                    completed_at=r.get("completed_at"),
                    notes=r.get("notes"),
                    updated_at=r.get("updated_at"),
                    is_completed=bool(r.get("is_completed")),
                    source=r.get("source", "tasks"),
                )
                for r in batch
            ]
        )
    for batch in _chunk(wake, BATCH):
        store.upsert_wake_ups(
            [
                WakeUpRow(
                    day=r["day"],
                    wake_time=r.get("wake_time"),
                    wake_minutes=r.get("wake_minutes"),
                    raw_notes=r.get("raw_notes"),
                    source_task_id=r.get("source_task_id"),
                )
                for r in batch
            ]
        )
    for batch in _chunk(weight, BATCH):
        store.upsert_weights(
            [
                WeightRow(
                    day=r["day"],
                    weight_lbs=r.get("weight_lbs"),
                    start_date=r.get("start_date"),
                    start_weight_lbs=r.get("start_weight_lbs"),
                    raw_notes=r.get("raw_notes"),
                    source_task_id=r.get("source_task_id"),
                )
                for r in batch
            ]
        )

    log.info("Migration complete.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001
        log.exception("Migration failed: %s", exc)
        sys.exit(1)
