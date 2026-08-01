"""Render weight_log as a standalone, self-contained HTML page.

Reads every weigh-in from the configured store and inlines it into
scripts/templates/weight_log.html, producing a single file that needs no server,
no network, and no credentials to view. Useful for checking the series by eye
without standing up the Streamlit dashboard.

Read-only: nothing is written to the database or to Google.

Run:
    python -m scripts.build_weight_page --out weight_log.html
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.config import load_config
from src.read import read_table

TEMPLATE = Path(__file__).resolve().parent / "templates" / "weight_log.html"


def load_points(config) -> list[dict]:
    """Return [{d: 'YYYY-MM-DD', w: float}, ...] sorted by day."""
    df = read_table(config, "weight_log")
    if df.empty:
        return []
    df = df.copy()
    df["weight_lbs"] = pd.to_numeric(df["weight_lbs"], errors="coerce")
    df = df.dropna(subset=["weight_lbs", "day"]).sort_values("day")
    return [
        {"d": str(r["day"])[:10], "w": round(float(r["weight_lbs"]), 1)}
        for _, r in df.iterrows()
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render weight_log as a standalone HTML page.")
    p.add_argument("--out", default="weight_log.html", help="Output path.")
    args = p.parse_args(argv)

    config = load_config()
    points = load_points(config)
    if not points:
        print("weight_log is EMPTY -- nothing to render.", file=sys.stderr)
        return 1

    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__BOOTSTRAP__",
        json.dumps({"mode": "inline", "data": points}, separators=(",", ":")),
    )
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")

    print(
        f"{len(points)} weigh-in(s) {points[0]['d']} .. {points[-1]['d']} "
        f"-> {out} ({out.stat().st_size:,} bytes)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
