"""Read helpers.

Two shapes. `read_rows` returns plain dicts and imports nothing heavy; the app
server uses it so a request never pulls pandas -- and numpy and pyarrow behind
it -- into the process. `read_table` returns a DataFrame for the offline
scripts, and imports pandas lazily so merely importing this module stays cheap.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .config import Config

if TYPE_CHECKING:
    import pandas as pd


def _read_sqlite(path: str, table: str) -> pd.DataFrame:
    import pandas as pd

    with sqlite3.connect(path) as conn:
        try:
            return pd.read_sql_query(f"SELECT * FROM {table}", conn)
        except Exception:
            return pd.DataFrame()


def read_rows(
    config: Config,
    table: str,
    columns: str = "*",
    equals: dict[str, object] | None = None,
) -> list[dict]:
    """Every row of a table as plain dicts, paged.

    Separate from read_table so the app server can read without importing
    pandas: the Supabase client already returns dicts, and pandas pulls in numpy
    and pyarrow behind it. The offline scripts still use the DataFrame form.

    PostgREST caps a single response at 1000 rows, so a plain select silently
    truncates larger tables -- task_completions passes 1000 in about four months
    -- hence the paging.
    """
    from supabase import create_client

    from concurrent.futures import ThreadPoolExecutor

    step = 1000
    filters = equals or {}

    def fetch(start: int, *, with_count: bool = False):
        # A client per worker avoids relying on thread safety in the SDK while
        # still overlapping Supabase round trips. The old sequential loop made
        # a 6,221-row cold read wait for seven network latencies back-to-back.
        client = create_client(config.supabase_url, config.supabase_key)
        if with_count:
            query = client.table(table).select(columns, count="exact")
        else:
            query = client.table(table).select(columns)
        for column, value in filters.items():
            query = query.eq(column, value)
        return query.range(start, start + step - 1).execute()

    first = fetch(0, with_count=True)
    rows = list(first.data or [])
    if len(rows) < step:
        return rows

    total = first.count
    if total is None:
        # Count can be disabled by a PostgREST configuration. Preserve the
        # original safe paging behavior rather than risking truncation.
        start = step
        while True:
            batch = fetch(start).data or []
            rows.extend(batch)
            if len(batch) < step:
                return rows
            start += step

    starts = list(range(step, total, step))
    if not starts:
        return rows
    with ThreadPoolExecutor(max_workers=min(4, len(starts))) as pool:
        pages = pool.map(fetch, starts)
        for response in pages:  # map preserves page order
            rows.extend(response.data or [])
    return rows


def _read_supabase(config: Config, table: str) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame(read_rows(config, table))


def read_table(config: Config, table: str) -> pd.DataFrame:
    if config.storage_backend == "supabase":
        return _read_supabase(config, table)
    return _read_sqlite(config.sqlite_path, table)


def get_wake_ups(config: Config) -> pd.DataFrame:
    import pandas as pd

    df = read_table(config, "wake_up_log")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    return df.sort_values("day")


def get_weights(config: Config) -> pd.DataFrame:
    import pandas as pd

    df = read_table(config, "weight_log")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    df["weight_lbs"] = pd.to_numeric(df["weight_lbs"], errors="coerce")
    return df.sort_values("day")


def get_completions(config: Config) -> pd.DataFrame:
    import pandas as pd

    df = read_table(config, "task_completions")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"], errors="coerce")
    return df
