#!/usr/bin/env python
"""Copy the still-open non-recurring tasks out of task_history and into the pool.

task_history is the reconciled historical record. This script READS it and
never writes to it -- the original rows stay exactly where they are. The pool
becomes the live home for undated work from here on; the old rows remain as
history, which is what they are.

Why these rows need a different home: 38 non-recurring tasks live in
task_history, and their `day` is an artefact of when the sync ran rather than
when anything was due -- 32 of the 38 share 2026-07-14. A table keyed on
(day, list_title, title) has nowhere honest to put a task with no date.

Dry run by default, like everything else here:

    python -m scripts.seed_pool              # print what it would insert
    python -m scripts.seed_pool --apply      # insert it
"""
from __future__ import annotations

import argparse
import math
import sys
import zlib

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.read import read_rows  # noqa: E402

# The recurring dailies have their own home in recurring_tasks; everything else
# in task_history is one-off work.
RECURRING_LIST = "Daily"


def scatter(index: int, total: int, title: str) -> tuple[float, float]:
    """A stable starting position for a star nobody has dragged yet.

    A phyllotaxis spiral rather than random placement: random scatter clumps,
    and clumping is the thing that made the first sketch look like a graph
    hairball. The title seeds a small jitter so the spiral is not visible as a
    spiral. Deterministic, so re-running this does not rearrange the sky.
    """
    golden = math.pi * (3.0 - math.sqrt(5.0))          # ~2.39996 rad
    radius = 0.40 * math.sqrt((index + 0.5) / max(total, 1))
    angle = index * golden
    h = zlib.crc32(title.encode())
    jx = ((h & 0xFF) / 255.0 - 0.5) * 0.05
    jy = (((h >> 8) & 0xFF) / 255.0 - 0.5) * 0.05
    x = 0.5 + radius * math.cos(angle) + jx
    y = 0.5 + radius * math.sin(angle) + jy
    return round(min(max(x, 0.04), 0.96), 4), round(min(max(y, 0.06), 0.94), 4)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually insert; without it, only prints")
    args = ap.parse_args()

    config = load_config()

    history = read_rows(config, "task_history")
    candidates = [
        r for r in history
        if (r.get("list_title") or "") != RECURRING_LIST and not r.get("is_completed")
    ]
    candidates.sort(key=lambda r: ((r.get("list_title") or ""), (r.get("title") or "")))

    from supabase import create_client
    client = create_client(config.supabase_url, config.supabase_key)

    try:
        existing_rows = client.table("pool_tasks").select("title").execute().data or []
    except Exception as exc:  # noqa: BLE001
        print(f"could not read pool_tasks: {exc}")
        print("apply the pool tables from sql/schema.sql in the Supabase SQL editor first.")
        return 2
    existing = {r["title"] for r in existing_rows}

    fresh = [r for r in candidates if (r.get("title") or "") not in existing]

    print(f"task_history holds {len(history):,} rows")
    print(f"  non-recurring and still open : {len(candidates)}")
    print(f"  already in the pool          : {len(candidates) - len(fresh)}")
    print(f"  to insert                    : {len(fresh)}")
    print()

    payload = []
    for i, row in enumerate(fresh):
        x, y = scatter(i, len(fresh), row.get("title") or "")
        payload.append({
            "title": row["title"],
            "notes": row.get("notes") or None,
            "x": x, "y": y,
        })
        print(f"  + {row['title']:<28} from {row.get('list_title'):<12} "
              f"(history day {row.get('day')})  ->  x={x} y={y}")

    if not payload:
        print("\nnothing to do.")
        return 0

    if not args.apply:
        print(f"\ndry run: nothing written. re-run with --apply to insert {len(payload)}.")
        print("task_history is not modified either way.")
        return 0

    created = client.table("pool_tasks").insert(payload).execute().data or []
    print(f"\ninserted {len(created)} into pool_tasks.")
    print("task_history untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
