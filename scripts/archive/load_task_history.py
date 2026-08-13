"""Load the reconciled export into the Supabase task_history table.

This is the one script in scripts/ that writes to Supabase. It is deliberately
narrow about what it can touch:

  * Only public.task_history. The table name is hardcoded; the original tables
    (task_completions, weight_log, wake_up_log) are never referenced.
  * Upsert only, on the natural key (day, list_title, title). No delete, no
    truncate, no drop -- there is no code path here that removes a row.
  * Refuses to run against a non-empty table unless --replace is passed, so a
    second run cannot silently churn an existing load.
  * Dry run by default. Nothing is written without --apply.

The table must exist first; PostgREST cannot issue DDL. Apply sql/schema.sql, or
paste the task_history block from it into the Supabase SQL editor.

Run:
    python -m scripts.load_task_history data/source_of_truth.json           # dry run
    python -m scripts.load_task_history data/source_of_truth.json --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.config import load_config

TABLE = "task_history"
CHUNK = 500

# Only these are written. Anything else in the source JSON is ignored rather
# than passed through, so an unexpected field cannot reach the database.
COLUMNS = (
    "day",
    "list_title",
    "title",
    "status",
    "is_completed",
    "completed_at",
    "notes",
    "recurrence_id",
    "weight_lbs",
    "weight_source",
    "takeout_weight",
)


def _client(config):
    from supabase import create_client

    if not config.supabase_url or not config.supabase_key:
        raise SystemExit("SUPABASE_URL and a key are required in .env.")
    return create_client(config.supabase_url, config.supabase_key)


def to_row(rec: dict) -> dict:
    row = {c: rec.get(c) for c in COLUMNS}
    row["is_completed"] = bool(rec.get("is_completed"))
    return row


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Load the reconciled export into task_history.")
    p.add_argument("source", help="reconciled export JSON (data/source_of_truth.json)")
    p.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry run.")
    p.add_argument(
        "--replace", action="store_true",
        help="Allow upserting over an already-populated table.",
    )
    args = p.parse_args(argv)

    records = json.loads(Path(args.source).read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise SystemExit("Source must be a non-empty JSON array.")

    rows = [to_row(r) for r in records]

    # The natural key must be unique or the upsert silently collapses rows.
    keys = {(r["day"], r["list_title"], r["title"]) for r in rows}
    if len(keys) != len(rows):
        raise SystemExit(
            f"Source has {len(rows)} rows but only {len(keys)} distinct "
            "(day, list_title, title) keys -- refusing to load."
        )

    config = load_config()
    client = _client(config)

    try:
        existing = client.table(TABLE).select("id", count="exact").limit(1).execute()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Could not read public.{TABLE}: {exc}\n"
            "Create it first -- apply sql/schema.sql, or paste its task_history "
            "block into the Supabase SQL editor."
        ) from exc

    n_existing = existing.count or 0
    days = {r["day"] for r in rows}
    weighed = sum(1 for r in rows if r["weight_lbs"] is not None)

    print(f"source      : {args.source}")
    print(f"rows        : {len(rows)} across {len(days)} days ({min(days)} .. {max(days)})")
    print(f"             {sum(1 for r in rows if r['is_completed'])} completed, "
          f"{weighed} carrying a weight")
    print(f"target      : public.{TABLE} (currently {n_existing} row(s))")
    print("writes to   : task_history only -- upsert on (day, list_title, title), never delete")

    if n_existing and not args.replace:
        print(
            f"\nRefusing to run: {TABLE} already has {n_existing} row(s). "
            "Pass --replace to upsert over them.",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply to commit.")
        return 0

    written = 0
    for i in range(0, len(rows), CHUNK):
        batch = rows[i:i + CHUNK]
        client.table(TABLE).upsert(batch, on_conflict="day,list_title,title").execute()
        written += len(batch)
        print(f"  upserted {written}/{len(rows)}", end="\r", file=sys.stderr)
    print(file=sys.stderr)

    # Re-read rather than trusting the responses.
    after = client.table(TABLE).select("id", count="exact").limit(1).execute()
    print(f"\nOK -- {written} row(s) upserted; public.{TABLE} now holds {after.count}.")
    if after.count != len(rows):
        print(
            f"NOTE: row count ({after.count}) differs from source ({len(rows)}); "
            "expected only if the table already held unrelated rows.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
