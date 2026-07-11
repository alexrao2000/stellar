"""Read helpers returning pandas DataFrames for the dashboard."""
from __future__ import annotations

import sqlite3

import pandas as pd

from .config import Config


def _read_sqlite(path: str, table: str) -> pd.DataFrame:
    with sqlite3.connect(path) as conn:
        try:
            return pd.read_sql_query(f"SELECT * FROM {table}", conn)
        except Exception:
            return pd.DataFrame()


def _read_supabase(config: Config, table: str) -> pd.DataFrame:
    from supabase import create_client

    client = create_client(config.supabase_url, config.supabase_key)
    resp = client.table(table).select("*").execute()
    return pd.DataFrame(resp.data or [])


def read_table(config: Config, table: str) -> pd.DataFrame:
    if config.storage_backend == "supabase":
        return _read_supabase(config, table)
    return _read_sqlite(config.sqlite_path, table)


def get_wake_ups(config: Config) -> pd.DataFrame:
    df = read_table(config, "wake_up_log")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    return df.sort_values("day")


def get_weights(config: Config) -> pd.DataFrame:
    df = read_table(config, "weight_log")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    df["weight_lbs"] = pd.to_numeric(df["weight_lbs"], errors="coerce")
    return df.sort_values("day")


def get_completions(config: Config) -> pd.DataFrame:
    df = read_table(config, "task_completions")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"], errors="coerce")
    return df
