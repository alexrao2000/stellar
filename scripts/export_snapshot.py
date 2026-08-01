"""Export stored task_completions rows as a Google-Tasks-shaped JSON snapshot.

Pairs with scripts/seed_tasks.py: this reads the durable history out of the
database and emits the exact task-resource shape the seeder inserts, so a range
of days can be re-created in Google Tasks with their real per-day completion
state and notes.

    python -m scripts.export_snapshot --start 2024-08-01 --end 2026-07-28 > snap.json
    python -m scripts.seed_tasks --from snap.json --list-id LIST_ID --apply

Read-only with respect to the database. Writes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from src.config import load_config
from src.read import read_table
from src.timezones import local_midnight_utc, resolve_timezone


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Export stored history as a seedable snapshot.")
    p.add_argument("--start", required=True, help="First day to export, YYYY-MM-DD.")
    p.add_argument("--end", required=True, help="Last day to export, inclusive, YYYY-MM-DD.")
    p.add_argument("--title", action="append", help="Restrict to these titles (repeatable).")
    p.add_argument("--out", help="Write here instead of stdout.")
    args = p.parse_args(argv)

    config = load_config()
    tz_name = resolve_timezone(config, allow_google=False)
    df = read_table(config, "task_completions")
    if df.empty:
        print("task_completions is EMPTY -- nothing to export.", file=sys.stderr)
        return 1

    df["day"] = pd.to_datetime(df["day"], errors="coerce").dt.date
    start = pd.to_datetime(args.start).date()
    end = pd.to_datetime(args.end).date()
    df = df[df["day"].notna() & (df["day"] >= start) & (df["day"] <= end)]
    if args.title:
        df = df[df["title"].isin(args.title)]

    # One row per day+title. Prefer a completed record over an incomplete one for
    # the same slot, mirroring how the dashboard collapses duplicates.
    df = df.sort_values("is_completed").drop_duplicates(subset=["day", "title"], keep="last")

    records = []
    for _, r in df.sort_values(["day", "title"]).iterrows():
        done = bool(r.get("is_completed"))
        rec = {
            "title": r.get("title") or "",
            "due": local_midnight_utc(r["day"].isoformat(), tz_name),
            "status": "completed" if done else "needsAction",
        }
        if r.get("notes") is not None and not pd.isna(r.get("notes")):
            rec["notes"] = str(r["notes"])
        # Only completed tasks may carry a completion timestamp.
        if done and r.get("completed_at") and not pd.isna(r.get("completed_at")):
            rec["completed"] = str(r["completed_at"])
        records.append(rec)

    payload = json.dumps(records, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
    else:
        print(payload)

    done_n = sum(1 for r in records if r["status"] == "completed")
    print(
        f"{len(records)} record(s) across {df['day'].nunique()} day(s): "
        f"{done_n} completed, {len(records) - done_n} incomplete.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
