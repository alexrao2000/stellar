"""Parse Google Tasks records into structured rows for storage.

Two kinds of output:
  - task completion rows (every task instance)
  - parsed metric rows (wake-up time, weight) from the notes field
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .tasks_client import TaskRecord

# "Wake-up Time: 7:30 AM"  /  "Wake up Time: 06:05 pm"
_WAKE_RE = re.compile(r"wake[\s-]*up\s*time\s*:\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])", re.IGNORECASE)

# "Current Weight: 178.2 lbs"
_CURRENT_WEIGHT_RE = re.compile(r"current\s*weight\s*:\s*([\d.]+)\s*lbs?", re.IGNORECASE)

# "Start Date+Weight: 8/8/24, 181.9 lbs"
_START_RE = re.compile(
    r"start\s*date\s*\+?\s*weight\s*:\s*([\d/]+)\s*,\s*([\d.]+)\s*lbs?", re.IGNORECASE
)


@dataclass
class CompletionRow:
    task_id: str
    list_id: str
    list_title: str
    title: str
    status: str
    day: str | None  # YYYY-MM-DD bucket
    due: str | None
    completed_at: str | None
    notes: str | None
    updated_at: str | None
    is_completed: bool


@dataclass
class WakeUpRow:
    day: str
    wake_time: str  # HH:MM:SS (24h)
    wake_minutes: int  # minutes since midnight, for easy plotting
    raw_notes: str
    source_task_id: str


@dataclass
class WeightRow:
    day: str
    weight_lbs: float
    start_date: str | None
    start_weight_lbs: float | None
    raw_notes: str
    source_task_id: str


def _to_local_date(ts: str | None, tz: ZoneInfo) -> str | None:
    """Convert an RFC3339 timestamp to a local YYYY-MM-DD date string."""
    if not ts:
        return None
    cleaned = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz).date().isoformat()


def bucket_day(task: TaskRecord, tz: ZoneInfo) -> str | None:
    """Assign a task to a calendar day.

    Prefer the `due` date (a daily task's intended day), so a task finished after
    midnight still counts for the day it was due. Fall back to completed, then updated.
    """
    # `due` is RFC3339 but only the date part is meaningful in Google Tasks.
    if task.due:
        date_part = task.due[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
            return date_part
    return _to_local_date(task.completed, tz) or _to_local_date(task.updated, tz)


def to_completion_row(task: TaskRecord, tz: ZoneInfo) -> CompletionRow:
    return CompletionRow(
        task_id=task.task_id,
        list_id=task.list_id,
        list_title=task.list_title,
        title=task.title,
        status=task.status,
        day=bucket_day(task, tz),
        due=task.due,
        completed_at=task.completed,
        notes=task.notes,
        updated_at=task.updated,
        is_completed=task.status == "completed",
    )


def parse_wake_up(notes: str | None) -> tuple[str, int] | None:
    """Return (HH:MM:SS, minutes_since_midnight) or None if not present."""
    if not notes:
        return None
    m = _WAKE_RE.search(notes)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    minute = int(m.group(2))
    if minute > 59:
        return None
    if m.group(3).lower() == "pm":
        hour += 12
    t = time(hour=hour, minute=minute)
    return t.strftime("%H:%M:%S"), hour * 60 + minute


def parse_weight(notes: str | None) -> tuple[float, str | None, float | None] | None:
    """Return (current_weight, start_date, start_weight) or None if no current weight."""
    if not notes:
        return None
    cur = _CURRENT_WEIGHT_RE.search(notes)
    if not cur:
        return None
    current_weight = float(cur.group(1))
    start = _START_RE.search(notes)
    start_date = start.group(1) if start else None
    start_weight = float(start.group(2)) if start else None
    return current_weight, start_date, start_weight


def extract_metrics(
    task: TaskRecord,
    day: str | None,
    wake_title: str,
    weight_title: str,
) -> tuple[WakeUpRow | None, WeightRow | None]:
    """Produce metric rows from a single task, matched by title."""
    if not day or not task.notes:
        return None, None

    wake_row: WakeUpRow | None = None
    weight_row: WeightRow | None = None
    title = task.title.strip().lower()

    if title == wake_title.strip().lower():
        parsed = parse_wake_up(task.notes)
        if parsed:
            wake_row = WakeUpRow(
                day=day,
                wake_time=parsed[0],
                wake_minutes=parsed[1],
                raw_notes=task.notes,
                source_task_id=task.task_id,
            )

    if title == weight_title.strip().lower():
        parsed_w = parse_weight(task.notes)
        if parsed_w:
            weight_row = WeightRow(
                day=day,
                weight_lbs=parsed_w[0],
                start_date=parsed_w[1],
                start_weight_lbs=parsed_w[2],
                raw_notes=task.notes,
                source_task_id=task.task_id,
            )

    return wake_row, weight_row
