"""Build the authoritative task history: Takeout as the base, Supabase weights on top.

Neither store is correct on its own:

  * Google Takeout has the true per-occurrence record -- every instance, its real
    completion status, its recurrence id -- but its weight values are destroyed.
    677 of 681 "Weigh yourself" occurrences carry the same 152.6, the result of an
    edit that propagated across the whole recurring series.
  * Supabase weight_log has the real per-day weights (69 distinct values spanning
    145.8-187.7) because it was captured incrementally before that happened, but
    its completion state drifted and it holds no per-occurrence detail.

So: take everything from Takeout, and override only the weight. Where weight_log
has a value for a day, it replaces the number inside that day's "Current Weight:"
note; the surrounding note text is preserved.

Where it does not, no weigh-in happened that day at all -- those are days without
a scale, not days whose reading went missing. The Takeout figure there is
propagation residue rather than a measurement, so the value is blanked and marked
"not recorded". An invented number that looks real is worse than an acknowledged
blank; the original is kept in takeout_weight for audit.

Read-only. Writes a JSON file; touches neither Supabase nor Google.

Run:
    python -m scripts.build_reconciled_export \
        data/takeout/tasks-takeout-2026-07-31.zip --out reconciled_export.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import load_config
from src.read import read_table
from src.timezones import resolve_timezone

from .parse_takeout_status import _day_of, _load_tasks_json

# Matches the number in "Current Weight: 151.6 lbs" so it can be swapped in place.
CURRENT_WEIGHT = re.compile(r"(current\s*weight\s*:\s*)([\d.]+)", re.IGNORECASE)

# Matches the whole figure including its unit, so a value that was never measured
# can be removed outright instead of leaving a stray number behind.
CURRENT_WEIGHT_CLAUSE = re.compile(
    r"current\s*weight\s*:\s*[\d.]+\s*(?:lbs?)?", re.IGNORECASE
)


def load_supabase_weights(config) -> dict[str, float]:
    """day -> weight_lbs from weight_log."""
    df = read_table(config, "weight_log")
    if df.empty:
        return {}
    df = df.copy()
    df["weight_lbs"] = pd.to_numeric(df["weight_lbs"], errors="coerce")
    df = df.dropna(subset=["weight_lbs", "day"])
    return {str(r["day"])[:10]: float(r["weight_lbs"]) for _, r in df.iterrows()}


def _rank(task: dict) -> tuple:
    """Prefer a completed occurrence, then the most recently updated."""
    return (task.get("status") == "completed", task.get("updated") or "")


def collect_takeout(data: dict, tz: ZoneInfo) -> list[dict]:
    """One record per (day, list, title); duplicates collapse to the best occurrence."""
    best: dict[tuple, tuple] = {}
    for task_list in data.get("items", []):
        lt = task_list.get("title", "")
        for task in task_list.get("items", []):
            day = _day_of(task, tz)
            if not day:
                continue
            key = (day, lt, task.get("title", ""))
            if key not in best or _rank(task) > _rank(best[key][1]):
                best[key] = (day, task, lt)
    return [
        {"day": d, "list_title": lt, "task": t}
        for (d, lt, _), (d2, t, _lt) in sorted(best.items())
        for d, t in [(d2, t)]
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Takeout base + Supabase weights.")
    p.add_argument("takeout", help="Takeout .zip or Tasks.json")
    p.add_argument("--out", required=True, help="Output JSON path.")
    p.add_argument(
        "--weight-title", default=None,
        help="Task title carrying the weight note (default: WEIGHT_TASK_TITLE).",
    )
    args = p.parse_args(argv)

    config = load_config()
    tz = ZoneInfo(resolve_timezone(config, allow_google=False))
    weight_title = (args.weight_title or config.weight_task_title).strip().lower()

    print("Reading weight_log from Supabase (read-only)...", file=sys.stderr)
    weights = load_supabase_weights(config)
    print(f"  {len(weights)} day(s) with a weight.", file=sys.stderr)

    print(f"Parsing Takeout ({args.takeout})...", file=sys.stderr)
    data = _load_tasks_json(Path(args.takeout).expanduser())
    entries = collect_takeout(data, tz)
    print(f"  {len(entries)} unique (day, list, title) record(s).", file=sys.stderr)

    out, stats = [], {"overridden": 0, "not_recorded": 0, "unchanged_value": 0}
    for e in entries:
        t, day = e["task"], e["day"]
        notes = t.get("notes")
        rec = {
            "day": day,
            "list_title": e["list_title"],
            "title": t.get("title", ""),
            "status": t.get("status", "needsAction"),
            "is_completed": t.get("status") == "completed",
            "completed_at": t.get("completed"),
            "notes": notes,
            "recurrence_id": t.get("task_recurrence_id"),
        }

        if rec["title"].strip().lower() == weight_title:
            m = CURRENT_WEIGHT.search(notes or "")
            rec["takeout_weight"] = float(m.group(2)) if m else None
            sup = weights.get(day)
            if sup is None:
                # No weight_log row means no weigh-in happened that day, so the
                # Takeout figure is an artifact of the propagation rather than a
                # measurement. Blank it: an invented number that looks real is
                # worse than an acknowledged blank. The original is preserved in
                # takeout_weight for audit.
                rec["weight_lbs"] = None
                rec["weight_source"] = "not recorded"
                if notes:
                    rec["notes"] = CURRENT_WEIGHT_CLAUSE.sub(
                        "Current Weight: (not recorded)", notes, count=1
                    )
                stats["not_recorded"] += 1
            else:
                rec["weight_lbs"] = sup
                rec["weight_source"] = "supabase"
                if notes and m:
                    rec["notes"] = CURRENT_WEIGHT.sub(
                        lambda mm: mm.group(1) + f"{sup:g}", notes, count=1
                    )
                if rec["takeout_weight"] is not None and abs(rec["takeout_weight"] - sup) > 0.05:
                    stats["overridden"] += 1
                else:
                    stats["unchanged_value"] += 1

        out.append(rec)

    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    weigh_n = sum(1 for r in out if "weight_source" in r)
    print(
        f"\n{len(out)} record(s) written.\n"
        f"  weigh-in records: {weigh_n}\n"
        f"    weight replaced from Supabase: {stats['overridden']}\n"
        f"    Supabase agreed with Takeout:  {stats['unchanged_value']}\n"
        f"    no weigh-in that day, blanked:  {stats['not_recorded']}\n"
        f"Wrote {args.out}. Supabase and Google were not written to.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
