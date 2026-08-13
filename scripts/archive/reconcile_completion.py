"""Match Takeout completion status onto a COPY of task_completions.

For every row in task_completions, look up its (day, title) in a Google Takeout
export and, where a match exists, overwrite that row's completion fields with
what Google actually recorded. Nothing is written back to Supabase or Google --
this produces a corrected copy only, meant to be reviewed (e.g. via a
comparison heatmap) before any write decision is made.

Only the completion status is touched. Every other field (notes, task_id, day,
title, ...) is carried through unchanged from the original row, since notes/
weights were already confirmed correct in Supabase.

Run:
    python -m scripts.reconcile_completion /path/to/takeout.zip --out reconciled.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import load_config
from src.read import read_table
from src.timezones import resolve_timezone

from .parse_takeout_status import _load_tasks_json, extract_status_records


def _day_key(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)[:10]


def reconcile(df: pd.DataFrame, google_by_key: dict[tuple[str, str], dict]) -> list[dict]:
    reconciled = []
    for _, row in df.iterrows():
        key = (_day_key(row.get("day")), row.get("title"))
        g = google_by_key.get(key)

        out = row.to_dict()
        out["original_is_completed"] = bool(row.get("is_completed"))
        out["original_status"] = row.get("status")

        if g is None:
            out["google_is_completed"] = None
            out["google_status"] = None
            out["corrected_is_completed"] = out["original_is_completed"]
            out["corrected_status"] = out["original_status"]
            out["reconciliation"] = "no_google_data"
        else:
            out["google_is_completed"] = g["is_completed"]
            out["google_status"] = g["status"]
            out["corrected_is_completed"] = g["is_completed"]
            out["corrected_status"] = g["status"]
            out["reconciliation"] = (
                "changed" if out["original_is_completed"] != g["is_completed"] else "match"
            )
        reconciled.append(out)
    return reconciled


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reconcile stored completion status against Takeout.")
    p.add_argument("takeout", help="Takeout .zip or Tasks.json")
    p.add_argument("--list", default="Daily", help="Task list to reconcile (default: Daily).")
    p.add_argument("--out", required=True, help="Write the reconciled copy here (JSON).")
    args = p.parse_args(argv)

    config = load_config()
    tz = ZoneInfo(resolve_timezone(config, allow_google=False))

    print("Reading task_completions from Supabase (read-only)...", file=sys.stderr)
    df = read_table(config, "task_completions")
    if df.empty:
        print("task_completions is EMPTY.", file=sys.stderr)
        return 1
    print(f"  {len(df)} row(s) read.", file=sys.stderr)

    print(f"Parsing Takeout export ({args.takeout})...", file=sys.stderr)
    data = _load_tasks_json(Path(args.takeout).expanduser())
    google_records = extract_status_records(data, args.list, tz)
    google_by_key = {(r["day"], r["title"]): r for r in google_records}
    print(f"  {len(google_records)} Google-side record(s) extracted.", file=sys.stderr)

    reconciled = reconcile(df, google_by_key)

    counts = {"match": 0, "changed": 0, "no_google_data": 0}
    for r in reconciled:
        counts[r["reconciliation"]] += 1
    print(
        f"\n{len(reconciled)} row(s): {counts['match']} already matched Google, "
        f"{counts['changed']} corrected, {counts['no_google_data']} had no Google data "
        f"(left as-is).",
        file=sys.stderr,
    )

    # Route through pandas' JSON encoder once to normalize Timestamps/NaN/NaT,
    # since json.dumps chokes on those pandas-native types directly.
    safe = json.loads(pd.DataFrame(reconciled).to_json(orient="records", date_format="iso"))
    Path(args.out).write_text(json.dumps(safe, indent=2), encoding="utf-8")
    print(f"Wrote reconciled COPY to {args.out}. Supabase and Google were not written to.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
