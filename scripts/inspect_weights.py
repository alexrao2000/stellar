"""READ-ONLY: dump weight_log so a suspected bad write can be diagnosed.

Writes nothing. Shows the most recent rows, the distribution of values, and
which days share the single most common weight -- which is what a bad
propagating write would look like.

Run:  python -m scripts.inspect_weights
"""
from __future__ import annotations

import sys
from collections import Counter

from src.config import load_config
from src.read import read_table


def main() -> int:
    config = load_config()
    print(f"backend={config.storage_backend}\n")

    df = read_table(config, "weight_log")
    if df.empty:
        print("weight_log is EMPTY.")
        return 0

    df = df.sort_values("day")
    print(f"rows={len(df)}  span={df['day'].min()} .. {df['day'].max()}\n")

    print("=== 25 most recent ===")
    cols = [c for c in ("day", "weight_lbs", "source_task_id", "synced_at") if c in df]
    print(df[cols].tail(25).to_string(index=False))

    counts = Counter(df["weight_lbs"].tolist())
    print("\n=== 10 most common values ===")
    for val, n in counts.most_common(10):
        flag = "  <-- suspicious spread" if n > 3 else ""
        print(f"  {val!s:>10}  x{n}{flag}")

    top_val, top_n = counts.most_common(1)[0]
    if top_n > 3:
        print(f"\n=== every day holding {top_val} ===")
        hits = df[df["weight_lbs"] == top_val]
        print(hits[cols].to_string(index=False))
        print(
            "\nIf synced_at is clustered, one write did this. source_task_id "
            "identifies which Google task each value came from."
        )

    print("\n=== distinct synced_at buckets (most recent 10) ===")
    if "synced_at" in df:
        print(df["synced_at"].astype(str).str[:16].value_counts().head(10).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
