"""Entry point: pull Google Tasks -> parse -> store.

Run:  python -m src.sync
"""
from __future__ import annotations

import logging
import sys
from zoneinfo import ZoneInfo

from .config import load_config
from .parsers import WakeUpRow, WeightRow, extract_metrics, to_completion_row
from .store import make_store
from .tasks_client import fetch_tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cal-task-sync")


def _dedupe_latest(rows: list, tasks_by_id: dict) -> list:
    """Keep one row per day; if duplicated, prefer the most recently updated task."""
    best: dict[str, object] = {}
    for row in rows:
        existing = best.get(row.day)
        if existing is None:
            best[row.day] = row
            continue
        new_updated = tasks_by_id.get(row.source_task_id)
        old_updated = tasks_by_id.get(existing.source_task_id)
        if (new_updated or "") >= (old_updated or ""):
            best[row.day] = row
    return list(best.values())


def run() -> int:
    config = load_config()
    config.validate()
    tz = ZoneInfo(config.local_timezone)

    log.info("Fetching tasks from Google Tasks (backend=%s)...", config.storage_backend)
    tasks = fetch_tasks(config)
    log.info("Fetched %d tasks.", len(tasks))

    tasks_by_id = {t.task_id: (t.updated or "") for t in tasks}

    completions = [to_completion_row(t, tz) for t in tasks]

    wake_rows: list[WakeUpRow] = []
    weight_rows: list[WeightRow] = []
    for t in tasks:
        day = next((c.day for c in completions if c.task_id == t.task_id), None)
        wake, weight = extract_metrics(t, day, config.wake_up_task_title, config.weight_task_title)
        if wake:
            wake_rows.append(wake)
        if weight:
            weight_rows.append(weight)

    wake_rows = _dedupe_latest(wake_rows, tasks_by_id)
    weight_rows = _dedupe_latest(weight_rows, tasks_by_id)

    store = make_store(config)
    store.upsert_completions(completions)
    store.upsert_wake_ups(wake_rows)
    store.upsert_weights(weight_rows)

    log.info(
        "Stored %d completions, %d wake-up rows, %d weight rows.",
        len(completions),
        len(wake_rows),
        len(weight_rows),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001 - top-level guard for cron logs
        log.exception("Sync failed: %s", exc)
        sys.exit(1)
