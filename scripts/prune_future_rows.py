#!/usr/bin/env python
"""Delete pre-created rows for days that have not happened yet.

A row that exists before its day does is indistinguishable, to every read in
this app, from a task that was missed. That is how the calendar ends up showing
a wall of false misses stretching into the future.

Only rows that were never touched are eligible. Anything completed, or carrying
a weight, a wake time or a note, is real data recorded ahead of time and is
listed as skipped rather than removed -- deciding it was junk is not this
script's call to make.

Dry-run by default, like everything else in scripts/. Prints exactly what it
would delete, day by day, and writes nothing without --apply.

    python -m scripts.prune_future_rows              # show
    python -m scripts.prune_future_rows --apply      # do it
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config          # noqa: E402
from src.postgrest import create_client     # noqa: E402

# Anything here means the row carries recorded information, whatever its date.
TOUCHED = ("weight_lbs", "takeout_weight", "completed_at", "notes")


def touched(row: dict) -> bool:
    if row.get("is_completed"):
        return True
    return any(row.get(f) not in (None, "") for f in TOUCHED)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; omit to preview")
    ap.add_argument("--through", metavar="YYYY-MM-DD",
                    help="only days up to and including this date")
    args = ap.parse_args()

    cfg = load_config()
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    today = date.today().isoformat()
    rows = client.table("task_history").select("*").filter(
        "day", "gt", today).order("day").execute().data or []
    if args.through:
        rows = [r for r in rows if r["day"][:10] <= args.through]

    if not rows:
        print(f"No rows dated after {today}. Nothing to do.")
        return 0

    keep = [r for r in rows if touched(r)]
    drop = [r for r in rows if not touched(r)]

    days: dict[str, list[dict]] = {}
    for r in drop:
        days.setdefault(r["day"][:10], []).append(r)

    print(f"today = {today}")
    print(f"rows dated in the future: {len(rows)}")
    print(f"  untouched (would delete): {len(drop)}")
    print(f"  carrying data (kept):     {len(keep)}")
    print()
    print("would delete, by day:")
    for d in sorted(days):
        titles = Counter(r["title"] for r in days[d])
        print(f"  {d}  {len(days[d]):2} rows  "
              f"{', '.join(sorted(titles))}")

    if keep:
        print()
        print("KEPT -- these carry recorded data despite being future-dated:")
        for r in sorted(keep, key=lambda r: r["day"]):
            why = [f for f in TOUCHED if r.get(f) not in (None, "")]
            if r.get("is_completed"):
                why.insert(0, "is_completed")
            print(f"  {r['day'][:10]}  id={r['id']}  {r['title']!r}  ({', '.join(why)})")

    print()
    before = len(rows)
    if not args.apply:
        print(f"DRY RUN -- nothing written. {len(drop)} rows would be deleted, "
              f"{len(keep)} kept.")
        print("Re-run with --apply once the list above looks right.")
        return 0

    # Delete by id, one day at a time, so a failure part-way leaves a state
    # that is easy to read rather than a half-finished range.
    deleted = 0
    for d in sorted(days):
        for r in days[d]:
            client.table("task_history").delete().eq("id", r["id"]).execute()
            deleted += 1
        print(f"  deleted {len(days[d]):2} rows for {d}")

    after = client.table("task_history").select("id").filter(
        "day", "gt", today).execute().data or []
    print()
    print(f"deleted {deleted} rows; future rows {before} -> {len(after)} "
          f"(expected {len(keep)})")
    return 0 if len(after) == len(keep) else 1


if __name__ == "__main__":
    raise SystemExit(main())
