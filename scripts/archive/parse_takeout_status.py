"""Extract per-day completion status from a Google Takeout Tasks export.

Read-only: opens the zip (or a bare Tasks.json), writes nothing back to Google
or the database. Output is a JSON array of {day, title, status, recurrence_id}
for every task instance, meant to be diffed against task_completions to check
whether the database's completion state agrees with what Google actually
recorded.

Deliberately extracts *only* status -- not notes, not due dates beyond the day
bucket -- since completion state is the one field known to have drifted; notes
(weights) were already confirmed correct in Supabase.

Run:
    python -m scripts.parse_takeout_status /path/to/takeout.zip --out status.json
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import load_config
from src.timezones import resolve_timezone


def _load_tasks_json(path: Path) -> dict:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            candidates = [n for n in zf.namelist() if n.endswith("Tasks.json")]
            if not candidates:
                raise FileNotFoundError("No Tasks.json found inside the ZIP.")
            with zf.open(candidates[0]) as f:
                return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _day_of(task: dict, tz: ZoneInfo) -> str | None:
    """Bucket to a local calendar day, matching src.takeout_import's logic."""
    from src.parsers import _to_local_date

    scheduled = task.get("scheduled_time") or []
    start = None
    for entry in scheduled:
        if entry.get("current"):
            start = entry.get("start")
            break
    if start is None and scheduled:
        start = scheduled[0].get("start")
    if start:
        # Google encodes a due date as local midnight in UTC (07:00:00Z for a
        # Pacific day), so an exact 00:00:00Z is not a real instant -- it is a
        # date-only value written through the API. Converting it to a local day
        # would shift it back one day west of UTC; take its date verbatim. Where
        # local midnight really is 00:00Z this is identical anyway.
        if start.endswith("T00:00:00Z") or start.endswith("T00:00:00.000Z"):
            return start[:10]
        day = _to_local_date(start, tz)
        if day:
            return day
    return _to_local_date(task.get("completed"), tz) or _to_local_date(task.get("created"), tz)


def extract_status_records(data: dict, list_name: str, tz: ZoneInfo) -> list[dict]:
    """Pull {day, title, status, is_completed, recurrence_id} for one task list."""
    records = []
    for task_list in data.get("items", []):
        if task_list.get("title") != list_name:
            continue
        for task in task_list.get("items", []):
            day = _day_of(task, tz)
            if not day:
                continue
            records.append(
                {
                    "day": day,
                    "title": task.get("title", ""),
                    "status": task.get("status", "needsAction"),
                    "is_completed": task.get("status") == "completed",
                    "recurrence_id": task.get("task_recurrence_id"),
                }
            )
    records.sort(key=lambda r: (r["day"], r["title"]))
    return records


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract per-day completion status from Takeout.")
    p.add_argument("path", help="Takeout .zip or Tasks.json")
    p.add_argument("--list", default="Daily", help="Task list to extract (default: Daily).")
    p.add_argument("--out", help="Write JSON here instead of stdout.")
    args = p.parse_args(argv)

    config = load_config()
    tz = ZoneInfo(resolve_timezone(config, allow_google=False))

    data = _load_tasks_json(Path(args.path).expanduser())
    records = extract_status_records(data, args.list, tz)
    payload = json.dumps(records, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    days = {r["day"] for r in records}
    done = sum(1 for r in records if r["is_completed"])
    print(
        f"{len(records)} record(s) across {len(days)} day(s) "
        f"({min(days)} .. {max(days)}): {done} completed, {len(records) - done} incomplete.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
