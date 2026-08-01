"""Render the reconciled export as a standalone calendar page.

A Google-Calendar-style week view: one column per day, tasks listed vertically,
completed tiles faded and struck through, incomplete tiles vibrant so misses are
what catch the eye. Hovering a tile shows its notes and full record.

Input is the JSON produced by scripts/build_reconciled_export.py -- Takeout for
every task and its completion status, Supabase weight_log for weight values.

Titles, notes and list names are deduplicated into lookup pools before inlining:
5,732 records share only ~617 distinct notes, so embedding them per-record would
multiply the payload several times over for no benefit.

Read-only. Reads a file, writes a file; touches neither Supabase nor Google.

Run:
    python -m scripts.build_calendar_page reconciled_export.json --out calendar.html
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "templates" / "calendar_view.html"

WSRC_CODE = {None: 0, "supabase": 1, "not recorded": 2, "inductive": 3, "reported": 4}


def build_payload(records: list[dict]) -> dict:
    titles: dict[str, int] = {}
    notes: dict[str, int] = {}
    lists: dict[str, int] = {}

    def intern(pool: dict[str, int], value: str) -> int:
        if value not in pool:
            pool[value] = len(pool)
        return pool[value]

    rows = []
    for r in records:
        note = r.get("notes")
        rows.append([
            r["day"],
            intern(titles, r.get("title") or ""),
            intern(lists, r.get("list_title") or ""),
            1 if r.get("is_completed") else 0,
            None if note is None else intern(notes, note),
            r.get("completed_at"),
            r.get("weight_lbs"),
            WSRC_CODE.get(r.get("weight_source"), 0),
            r.get("takeout_weight"),
        ])

    def keys(pool: dict[str, int]) -> list[str]:
        return [k for k, _ in sorted(pool.items(), key=lambda kv: kv[1])]

    return {
        "titles": keys(titles),
        "notes": keys(notes),
        "lists": keys(lists),
        "rows": sorted(rows, key=lambda x: (x[0], x[1])),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render the reconciled export as a calendar page.")
    p.add_argument("source", help="reconciled_export.json")
    p.add_argument("--out", default="calendar.html", help="Output path.")
    args = p.parse_args(argv)

    records = json.loads(Path(args.source).read_text(encoding="utf-8"))
    if not records:
        print("Source is empty -- nothing to render.", file=sys.stderr)
        return 1

    payload = build_payload(records)
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(payload, separators=(",", ":"))
    )
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")

    days = {r[0] for r in payload["rows"]}
    done = sum(1 for r in payload["rows"] if r[3])
    print(
        f"{len(payload['rows'])} record(s) across {len(days)} day(s) "
        f"({min(days)} .. {max(days)}); {done} completed.\n"
        f"pools: {len(payload['titles'])} titles, {len(payload['notes'])} notes, "
        f"{len(payload['lists'])} lists\n"
        f"-> {out} ({out.stat().st_size:,} bytes)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
