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
    python -m scripts.build_calendar_page source_of_truth.json --out calendar.html
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "templates" / "calendar_view.html"

WSRC_CODE = {None: 0, "supabase": 1, "not recorded": 2, "inductive": 3, "reported": 4}


def assert_safe_to_embed(key: str) -> None:
    """Refuse to bake a key that bypasses RLS into a page.

    A live page ships its key to the browser, so it must be one that RLS
    governs. Supabase keys are JWTs carrying a `role` claim; anything other than
    anon can read and write every table regardless of policy, which would put
    full database access into a file that gets shared around.
    """
    parts = key.split(".")
    if len(parts) != 3:
        # Not a JWT -- the newer publishable/secret format. Those are
        # distinguishable by prefix; only reject the one that is clearly secret.
        if key.startswith("sb_secret_"):
            raise SystemExit(
                "Refusing to embed a secret key in a page that ships to the browser. "
                "Use the publishable key (sb_publishable_...)."
            )
        return
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:  # noqa: BLE001 - unparseable, fall through rather than block
        return
    if role and role != "anon":
        raise SystemExit(
            f"Refusing to embed a '{role}' key in a page that ships to the browser: "
            "it bypasses row-level security and can read and write every table. "
            "Set SUPABASE_ANON_KEY to the anon / publishable key."
        )


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
    p.add_argument("source", nargs="?", help="source_of_truth.json (omit with --live)")
    p.add_argument("--out", default="calendar.html", help="Output path.")
    p.add_argument(
        "--live", action="store_true",
        help="Fetch task_history from Supabase in the browser instead of inlining a snapshot.",
    )
    args = p.parse_args(argv)

    if args.live:
        from src.config import load_config

        config = load_config()
        key = config.supabase_anon_key
        if not config.supabase_url or not key:
            raise SystemExit(
                "--live needs SUPABASE_URL and SUPABASE_ANON_KEY in .env. "
                "SUPABASE_ANON_KEY must be the anon / publishable key, not the "
                "service role key -- it ships to the browser."
            )
        assert_safe_to_embed(key)
        base = config.supabase_url.rstrip("/")
        boot = {
            "mode": "live",
            "endpoint": (
                f"{base}/rest/v1/task_history?select=day,list_title,title,is_completed,"
                "completed_at,notes,weight_lbs,weight_source,takeout_weight"
                "&order=day.asc,title.asc"
            ),
            "headers": {"apikey": key, "Authorization": f"Bearer {key}"},
            "paged": True,  # PostgREST caps a response at 1000 rows
        }
        summary = f"live against {base}/rest/v1/task_history"
    else:
        if not args.source:
            raise SystemExit("Provide a source JSON, or pass --live.")
        records = json.loads(Path(args.source).read_text(encoding="utf-8"))
        if not records:
            print("Source is empty -- nothing to render.", file=sys.stderr)
            return 1
        payload = build_payload(records)
        boot = {"mode": "inline", "data": payload}
        days = {r[0] for r in payload["rows"]}
        summary = (
            f"{len(payload['rows'])} record(s) across {len(days)} day(s) "
            f"({min(days)} .. {max(days)}); "
            f"{sum(1 for r in payload['rows'] if r[3])} completed\n"
            f"pools: {len(payload['titles'])} titles, {len(payload['notes'])} notes, "
            f"{len(payload['lists'])} lists"
        )

    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__BOOTSTRAP__", json.dumps(boot, separators=(",", ":"))
    )
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")

    print(f"{summary}\n-> {out} ({out.stat().st_size:,} bytes)", file=sys.stderr)
    if args.live:
        print(
            "NOTE: a live page cannot be published as an Artifact -- the CSP there "
            "blocks requests to external hosts. Open it locally or host it yourself.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
