#!/usr/bin/env python
"""Return the Today page's task order to the default.

The default is measurements above tasks, each group in the order the
definitions were created. It applies to every active definition whose
sort_order is 0; dragging on the page writes explicit positions 1..n instead.

The sort_order values seeded with recurring_tasks (1-8, alphabetical) were never
anyone's choice, but they are explicit, so they hold the default off. This sets
them back to 0. It touches recurring_tasks only -- display order, not history.

Dry-run by default, like everything else in scripts/: prints the order before
and after and writes nothing without --apply.

    python -m scripts.reset_task_order            # show
    python -m scripts.reset_task_order --apply    # do it
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config        # noqa: E402
from src.postgrest import create_client   # noqa: E402
from src.server import _definitions_order_key, _measurement_titles  # noqa: E402


def ordered(defs: list[dict]) -> list[dict]:
    m = _measurement_titles()
    return sorted(defs, key=lambda d: _definitions_order_key(d, m))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually write; omit to preview")
    args = ap.parse_args()

    cfg = load_config()
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    defs = client.table("recurring_tasks").select("*").execute().data or []
    active = [d for d in defs if d.get("active")]

    before = ordered(active)
    after = ordered([{**d, "sort_order": 0} for d in active])
    to_reset = [d for d in active if d.get("sort_order")]

    width = max(len(d["title"]) for d in active)
    print(f"{'position':>8}  {'now':<{width}}  (sort_order)   {'after reset':<{width}}")
    for i, (b, a) in enumerate(zip(before, after), 1):
        print(f"{i:>8}  {b['title']:<{width}}  ({b.get('sort_order')!s:>3})          {a['title']}")
    print()
    if not to_reset:
        print("Already at the default. Nothing to do.")
        return 0

    if not args.apply:
        print(f"DRY RUN -- nothing written. {len(to_reset)} definitions would be set to sort_order 0.")
        print("Re-run with --apply once the order above looks right.")
        return 0

    for d in to_reset:
        client.table("recurring_tasks").update({"sort_order": 0}).eq("id", d["id"]).execute()
    check = client.table("recurring_tasks").select("id,sort_order").execute().data or []
    left = [r for r in check if r["id"] in {d["id"] for d in active} and r["sort_order"]]
    print(f"reset {len(to_reset)} definitions; {len(left)} still explicit (expected 0)")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
