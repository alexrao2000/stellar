"""Backfill historical Google Tasks from a Google Takeout export.

The public Tasks/Calendar APIs don't return the full history of completed
recurring task instances, but a Google Takeout "Tasks" export does. This importer
reads that export (a .zip or the inner Tasks.json) and upserts every task instance
into the same tables the live sync uses, tagged source='takeout'.

Run:  python -m src.takeout_import /path/to/takeout.zip
"""
from __future__ import annotations

import json
import logging
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_config
from .parsers import (
    CompletionRow,
    WakeUpRow,
    WeightRow,
    _to_local_date,
    metrics_from,
    seed_start_weight_rows,
)
from .store import make_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cal-task-sync")


def _load_tasks_json(path: Path) -> dict:
    """Load Tasks.json from a .json file or from within a Takeout .zip."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            candidates = [n for n in zf.namelist() if n.endswith("Tasks.json")]
            if not candidates:
                raise FileNotFoundError("No Tasks.json found inside the ZIP.")
            with zf.open(candidates[0]) as f:
                return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _scheduled_day(task: dict, tz: ZoneInfo) -> str | None:
    """Bucket a task to a local calendar day.

    Prefer scheduled_time.start (stored at local midnight), which represents the
    intended day; fall back to completed, then created.
    """
    scheduled = task.get("scheduled_time") or []
    start = None
    for entry in scheduled:
        if entry.get("current"):
            start = entry.get("start")
            break
    if start is None and scheduled:
        start = scheduled[0].get("start")
    if start:
        day = _to_local_date(start, tz)
        if day:
            return day
    return _to_local_date(task.get("completed"), tz) or _to_local_date(task.get("created"), tz)


def _iter_tasks(data: dict):
    for task_list in data.get("items", []):
        list_title = task_list.get("title", "")
        list_id = task_list.get("id", "")
        for task in task_list.get("items", []):
            yield list_id, list_title, task


def run(path: Path) -> int:
    config = load_config()
    config.validate()
    tz = ZoneInfo(config.local_timezone)

    data = _load_tasks_json(path)
    log.info("Loaded Takeout export from %s", path)

    completions: list[CompletionRow] = []
    wake_rows: list[WakeUpRow] = []
    weight_rows: list[WeightRow] = []
    updated_by_id: dict[str, str] = {}

    total = 0
    for list_id, list_title, task in _iter_tasks(data):
        total += 1
        task_id = task["id"]
        status = task.get("status", "needsAction")
        notes = task.get("notes")
        completed = task.get("completed")
        updated = task.get("updated", "")
        day = _scheduled_day(task, tz)
        updated_by_id[task_id] = updated or ""

        completions.append(
            CompletionRow(
                task_id=task_id,
                list_id=list_id,
                list_title=list_title,
                title=task.get("title", ""),
                status=status,
                day=day,
                due=task.get("due"),
                completed_at=completed,
                notes=notes,
                updated_at=updated,
                is_completed=status == "completed",
                source="takeout",
            )
        )

        wake, weight = metrics_from(
            task.get("title", ""), day, notes, task_id, config.wake_up_task_title, config.weight_task_title
        )
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
        "Imported %d task instances -> %d completions, %d wake-up days, %d weight days.",
        total,
        len(completions),
        len(wake_rows),
        len(weight_rows),
    )
    return 0


def _dedupe_latest(rows: list, updated_by_id: dict) -> list:
    """Keep one row per day, preferring the most recently updated task."""
    best: dict[str, object] = {}
    for row in rows:
        existing = best.get(row.day)
        if existing is None or updated_by_id.get(row.source_task_id, "") >= updated_by_id.get(
            existing.source_task_id, ""
        ):
            best[row.day] = row
    return list(best.values())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.takeout_import /path/to/takeout.zip (or Tasks.json)")
        sys.exit(2)
    try:
        sys.exit(run(Path(sys.argv[1]).expanduser()))
    except Exception as exc:  # noqa: BLE001
        log.exception("Takeout import failed: %s", exc)
        sys.exit(1)
