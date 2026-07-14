"""Entry point: pull Google Tasks + Calendar history -> parse -> store.

Run:  python -m src.sync
"""
from __future__ import annotations

import logging
import sys
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from .calendar_client import fetch_calendar_events
from .config import load_config
from .parsers import (
    WakeUpRow,
    WeightRow,
    calendar_event_to_completion,
    calendar_extract_metrics,
    extract_metrics,
    seed_start_weight_rows,
    to_completion_row,
)
from .store import make_store
from .tasks_client import fetch_tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cal-task-sync")


def _dedupe_latest(rows: list, updated_by_id: dict) -> list:
    """Keep one row per day; if duplicated, prefer the most recently updated source."""
    best: dict[str, object] = {}
    for row in rows:
        existing = best.get(row.day)
        if existing is None:
            best[row.day] = row
            continue
        new_updated = updated_by_id.get(row.source_task_id, "")
        old_updated = updated_by_id.get(existing.source_task_id, "")
        if new_updated >= old_updated:
            best[row.day] = row
    return list(best.values())


def run() -> int:
    config = load_config()
    config.validate()
    tz = ZoneInfo(config.local_timezone)

    # --- Google Tasks (current state + limited completed history) ---
    log.info("Fetching tasks from Google Tasks (backend=%s)...", config.storage_backend)
    tasks = fetch_tasks(config)
    log.info("Fetched %d tasks.", len(tasks))

    # --- Google Calendar (optional; does NOT contain Google Tasks, only events) ---
    calendar_events: list = []
    if config.include_calendar_events:
        try:
            log.info("Fetching event history from Google Calendar...")
            calendar_events = fetch_calendar_events(config)
            log.info("Fetched %d calendar event instances.", len(calendar_events))
        except HttpError as exc:
            log.warning(
                "Calendar fetch failed (%s). If this is 'accessNotConfigured', enable the "
                "Google Calendar API for your project, then re-run.",
                exc,
            )

    updated_by_id: dict[str, str] = {t.task_id: (t.updated or "") for t in tasks}
    updated_by_id.update({e.event_id: (e.updated or "") for e in calendar_events})

    # --- Completion rows (every task/event instance) ---
    completions = [to_completion_row(t, tz) for t in tasks]
    completions += [calendar_event_to_completion(e) for e in calendar_events]

    # --- Metric rows from both sources ---
    wake_rows: list[WakeUpRow] = []
    weight_rows: list[WeightRow] = []
    for t in tasks:
        day = to_completion_row(t, tz).day
        wake, weight = extract_metrics(t, day, config.wake_up_task_title, config.weight_task_title)
        if wake:
            wake_rows.append(wake)
        if weight:
            weight_rows.append(weight)
    for e in calendar_events:
        wake, weight = calendar_extract_metrics(e, config.wake_up_task_title, config.weight_task_title)
        if wake:
            wake_rows.append(wake)
        if weight:
            weight_rows.append(weight)

    wake_rows = _dedupe_latest(wake_rows, updated_by_id)
    weight_rows = _dedupe_latest(weight_rows, updated_by_id)
    weight_rows = seed_start_weight_rows(weight_rows)

    store = make_store(config)
    store.upsert_completions(completions)
    store.upsert_wake_ups(wake_rows)
    store.upsert_weights(weight_rows)

    log.info(
        "Stored %d completions (%d tasks + %d calendar), %d wake-up days, %d weight days.",
        len(completions),
        len(tasks),
        len(calendar_events),
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
