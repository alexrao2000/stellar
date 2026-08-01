"""Local app server. Serves the interfaces and proxies Supabase reads.

The pages fetch same-origin from this server rather than talking to Supabase
directly, which means the database key never reaches the browser. That also
removes the need for an anon key and for a public read policy on task_history:
the credential stays in this process.

Read-only. Every endpoint here is a SELECT; nothing writes to Supabase or
Google.

Run:  make dev        (or: uvicorn src.server:app --reload --port 8000)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import load_config
from .parsers import parse_wake_up
from .read import read_table

log = logging.getLogger("cal-task-sync")

TEMPLATES = Path(__file__).resolve().parent.parent / "scripts" / "templates"

# Reading the full table takes a few seconds and it changes rarely, so hold it
# briefly rather than re-fetching on every navigation. POST /api/refresh clears.
CACHE_TTL = 120.0
_cache: dict[str, tuple[float, object]] = {}

app = FastAPI(title="task-tracker", docs_url="/docs")


def _cached(key: str, build):
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
        return hit[1]
    value = build()
    _cache[key] = (time.monotonic(), value)
    return value


def _clean(value):
    """JSON-safe: pandas NaN/NaT are not valid JSON and serialize as NaN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if pd.isna(value) if not isinstance(value, (list, dict, str)) else False:
        return None
    return value


def _task_history() -> list[dict]:
    def build():
        config = load_config()
        df = read_table(config, "task_history")
        if df.empty:
            return []
        df = df.sort_values(["day", "title"])
        cols = [
            "day", "list_title", "title", "is_completed", "completed_at",
            "notes", "weight_lbs", "weight_source", "takeout_weight",
        ]
        out = []
        for _, r in df.iterrows():
            rec = {c: _clean(r.get(c)) for c in cols}
            rec["day"] = str(rec["day"])[:10]
            rec["is_completed"] = bool(r.get("is_completed"))
            for numeric in ("weight_lbs", "takeout_weight"):
                if rec[numeric] is not None:
                    rec[numeric] = float(rec[numeric])
            out.append(rec)
        return out

    return _cached("task_history", build)


def _render(template: str, boot: dict) -> str:
    import json

    path = TEMPLATES / template
    if not path.exists():
        raise HTTPException(500, f"template missing: {template}")
    return path.read_text(encoding="utf-8").replace(
        "__BOOTSTRAP__", json.dumps(boot, separators=(",", ":"))
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/task_history")
def api_task_history() -> list[dict]:
    """The reconciled record. Returned whole -- the client does not page this."""
    return _task_history()


@app.get("/api/weights")
def api_weights() -> list[dict]:
    """Per-day weights, taken from task_history rather than weight_log.

    task_history is the reconciled record, so it includes the days that were
    supplied after the overwrite and that weight_log never captured.
    """
    wake_or_weight = [
        r for r in _task_history()
        if r["weight_lbs"] is not None and r["title"].strip().lower() == "weigh yourself"
    ]
    return [{"d": r["day"], "w": round(float(r["weight_lbs"]), 1)} for r in wake_or_weight]


@app.get("/api/wake")
def api_wake() -> list[dict]:
    """Wake-up times parsed out of task_history notes.

    Reuses src.parsers.parse_wake_up rather than re-implementing the regex --
    a second copy here silently returned nothing, having lost the IGNORECASE
    flag that lets it match "Wake-up Time:" as actually written.
    """
    out = []
    for r in _task_history():
        if r["title"].strip().lower() != "wake up" or not r["notes"]:
            continue
        parsed = parse_wake_up(r["notes"])
        if parsed:
            out.append({"d": r["day"], "time": parsed[0], "minutes": parsed[1]})
    return out


@app.post("/api/refresh")
def api_refresh() -> dict:
    """Drop the cache so the next read hits Supabase."""
    _cache.clear()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def page_calendar() -> str:
    # Same-origin, no credentials, whole set in one response.
    return _render("calendar_view.html", {
        "mode": "live", "endpoint": "/api/task_history", "headers": {}, "paged": False,
    })


@app.get("/weight", response_class=HTMLResponse)
def page_weight() -> str:
    return _render("weight_log.html", {
        "mode": "live", "endpoint": "/api/weights", "headers": {}, "paged": False,
    })


@app.get("/health")
def health() -> dict:
    rows = _task_history()
    return {"ok": True, "task_history_rows": len(rows),
            "cached": "task_history" in _cache}
