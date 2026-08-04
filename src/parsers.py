"""Parse Google Tasks records into structured rows for storage.

Two kinds of output:
  - task completion rows (every task instance)
  - parsed metric rows (wake-up time, weight) from the notes field
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .titles import matches_metric_title

if TYPE_CHECKING:
    # Type-only. Importing tasks_client at runtime drags in googleapiclient and
    # google-auth, which the note parsers here never touch -- and which the app
    # server would then carry purely to use parse_wake_up. Annotations are
    # strings under `from __future__ import annotations`, so this is safe.
    from .calendar_client import CalendarEventRecord
    from .tasks_client import TaskRecord

# "Wake-up Time: 7:30 AM"  /  "Wake up Time: 06:05 pm"
_WAKE_RE = re.compile(r"wake[\s-]*up\s*time\s*:\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])", re.IGNORECASE)

# "Current Weight: 178.2 lbs" / "Current Weight: 182.4 (notes...)"
_CURRENT_WEIGHT_RE = re.compile(r"current\s*weight\s*:\s*([\d.]+)\s*(?:lbs?)?", re.IGNORECASE)

# "Start Date+Weight: 8/8/24, 181.9 lbs"
_START_RE = re.compile(
    r"start\s*date\s*\+?\s*weight\s*:\s*([\d/]+)\s*,\s*([\d.]+)\s*(?:lbs?)?", re.IGNORECASE
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
    source: str = "tasks"  # "tasks" or "calendar"


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
    """Assign a task to a calendar day, or None when it has no meaningful one.

    Prefer the `due` date (a daily task's intended day), so a task finished after
    midnight still counts for the day it was due. A completed task with no due
    date still has a real day: when it was done.

    An *incomplete* task with no due date is an undated backlog item ("Clean
    Car"), not something scheduled. Bucketing it to `updated` would invent a
    missed day it was never due on, so it gets no day and is filtered out
    downstream.
    """
    # `due` is RFC3339 but only the date part is meaningful in Google Tasks.
    if task.due:
        date_part = task.due[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
            return date_part
    if task.completed:
        return _to_local_date(task.completed, tz) or _to_local_date(task.updated, tz)
    return None


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


def normalize_us_date(value: str | None) -> str | None:
    """Normalize M/D/YY or M/D/YYYY to YYYY-MM-DD."""
    if not value:
        return None
    cleaned = value.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def seed_start_weight_rows(weight_rows: list[WeightRow]) -> list[WeightRow]:
    """Add a baseline point from Start Date+Weight when that day isn't already logged."""
    seeded = list(weight_rows)
    seen_days = {r.day for r in weight_rows}
    for row in weight_rows:
        day = normalize_us_date(row.start_date)
        if not day or day in seen_days or row.start_weight_lbs is None:
            continue
        seeded.append(
            WeightRow(
                day=day,
                weight_lbs=float(row.start_weight_lbs),
                start_date=row.start_date,
                start_weight_lbs=row.start_weight_lbs,
                raw_notes=f"[seeded start weight] {row.raw_notes}",
                source_task_id=row.source_task_id,
            )
        )
        seen_days.add(day)
    return seeded


def metrics_from(
    title: str,
    day: str | None,
    notes: str | None,
    source_id: str,
    wake_title: str,
    weight_title: str,
    aliases: dict[str, str] | None = None,
) -> tuple[WakeUpRow | None, WeightRow | None]:
    """Produce metric rows from any titled, dated, note-bearing item.

    Title matching is tolerant of the whitespace/case/emoji drift a
    hand-recreated instance picks up, so a replacement weigh-in still yields a
    datapoint instead of silently dropping out of the series.
    """
    if not day or not notes:
        return None, None

    wake_row: WakeUpRow | None = None
    weight_row: WeightRow | None = None

    if matches_metric_title(title, wake_title, aliases):
        parsed = parse_wake_up(notes)
        if parsed:
            wake_row = WakeUpRow(
                day=day,
                wake_time=parsed[0],
                wake_minutes=parsed[1],
                raw_notes=notes,
                source_task_id=source_id,
            )

    if matches_metric_title(title, weight_title, aliases):
        parsed_w = parse_weight(notes)
        if parsed_w:
            weight_row = WeightRow(
                day=day,
                weight_lbs=parsed_w[0],
                start_date=parsed_w[1],
                start_weight_lbs=parsed_w[2],
                raw_notes=notes,
                source_task_id=source_id,
            )

    return wake_row, weight_row


def extract_metrics(
    task: TaskRecord,
    day: str | None,
    wake_title: str,
    weight_title: str,
) -> tuple[WakeUpRow | None, WeightRow | None]:
    """Produce metric rows from a single Google Task, matched by title."""
    return metrics_from(task.title, day, task.notes, task.task_id, wake_title, weight_title)


def calendar_event_to_completion(ev: "CalendarEventRecord") -> CompletionRow:
    """Map a Calendar event instance to a completion row."""
    return CompletionRow(
        task_id=ev.event_id,
        list_id=ev.calendar_id,
        list_title=ev.calendar_summary,
        title=ev.title,
        status=ev.status or "",
        day=ev.day,
        due=None,
        completed_at=None,
        notes=ev.description,
        updated_at=ev.updated,
        # Placeholder until we confirm how completion is encoded in Calendar;
        # a cancelled instance is definitely not complete.
        is_completed=(ev.status or "") != "cancelled",
        source="calendar",
    )


def calendar_extract_metrics(
    ev: "CalendarEventRecord",
    wake_title: str,
    weight_title: str,
) -> tuple[WakeUpRow | None, WeightRow | None]:
    """Produce metric rows from a Calendar event's description."""
    return metrics_from(ev.title, ev.day, ev.description, ev.event_id, wake_title, weight_title)
