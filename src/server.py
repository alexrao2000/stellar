"""Local app server. Serves the interfaces, reads Supabase, and records check-ins.

The pages fetch same-origin from this server rather than talking to Supabase
directly, which means the database key never reaches the browser. That also
removes the need for an anon key and for a public read policy on task_history:
the credential stays in this process.

Reads are cached; writes are bounded to task_history and to the fields a daily
check-in touches. Nothing here writes to Google.

Run:  make dev        (or: uvicorn src.server:app --reload --port 8000)
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import load_config
from .parsers import parse_wake_up
from .read import read_table

log = logging.getLogger("cal-task-sync")

TEMPLATES = Path(__file__).resolve().parent.parent / "scripts" / "templates"

# Same clause the reconciler rewrites, so the note format stays consistent
# between historical repair and new entries.
CURRENT_WEIGHT_CLAUSE = re.compile(
    r"current\s*weight\s*:\s*[\d.]*\s*(?:lbs?)?", re.IGNORECASE
)

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
# Writes
#
# Everything below mutates task_history. Bounded on purpose: only that table,
# only rows for a given day, and only the fields a daily check-in touches.
# --------------------------------------------------------------------------- #
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# A weigh-in outside this range is a typo, not a reading. Refuse rather than
# write a number that would wreck the chart and need reconciling later.
WEIGHT_MIN, WEIGHT_MAX = 50.0, 500.0


def _sb():
    from supabase import create_client

    config = load_config()
    if not config.supabase_url or not config.supabase_key:
        raise HTTPException(500, "SUPABASE_URL and a key are required in .env.")
    return create_client(config.supabase_url, config.supabase_key)


def _check_day(day: str) -> str:
    if not DAY_RE.match(day or ""):
        raise HTTPException(400, f"day must be YYYY-MM-DD, got {day!r}")
    return day


# Everything before this is the reconciled historical record. It took a long
# recovery to get right and nothing in a daily check-in has any business
# touching it, so writes to earlier days are refused outright.
#
# 2026-07-31 rather than 08-01: the app went live on the 31st Pacific, so that
# day is one the user records normally rather than part of the frozen history.
#
# Temporary by intent -- lift it deliberately when back-editing is wanted, not
# by accident. Reads are unaffected; the calendar still shows all of history.
WRITE_FLOOR = date(2026, 7, 31)


def _check_writable(day: str) -> str:
    _check_day(day)
    if date.fromisoformat(day) < WRITE_FLOOR:
        raise HTTPException(
            403,
            f"writes to {day} are disabled: everything before "
            f"{WRITE_FLOOR.isoformat()} is the reconciled history and is read-only.",
        )
    return day


def _day_rows(client, day: str) -> list[dict]:
    return (
        client.table("task_history").select("*").eq("day", day).execute().data or []
    )


@app.post("/api/day/{day}/ensure")
def api_ensure_day(day: str) -> dict:
    """Materialise a day's recurring occurrences if they don't exist yet.

    This is what replaces Google's recurrence. Insert-only: rows that already
    exist are left exactly as they are, so calling it repeatedly is harmless and
    can never clobber a completion or a weight already recorded.
    """
    _check_writable(day)
    client = _sb()

    defs = (
        client.table("recurring_tasks").select("*").eq("active", True)
        .order("sort_order").execute().data or []
    )
    if not defs:
        raise HTTPException(500, "recurring_tasks is empty -- apply sql/schema.sql.")

    existing = {(r["list_title"], r["title"]) for r in _day_rows(client, day)}
    wanted = [
        d for d in defs
        if (d["list_title"], d["title"]) not in existing
        and (not d.get("starts_on") or d["starts_on"] <= day)
        and (not d.get("ends_on") or d["ends_on"] >= day)
    ]
    if wanted:
        client.table("task_history").insert([
            {
                "day": day,
                "list_title": d["list_title"],
                "title": d["title"],
                "status": "needsAction",
                "is_completed": False,
            }
            for d in wanted
        ]).execute()
        _cache.clear()
    return {"day": day, "created": [d["title"] for d in wanted],
            "already_present": len(existing)}


@app.post("/api/ensure_range")
def api_ensure_range(start: str, end: str) -> dict:
    """Materialise every day in [start, end]. Insert-only, same as the single-day form.

    Deliberately not run automatically over the future: a pre-created row for a
    day that has not happened yet reads as an incomplete task, which is how the
    calendar ends up showing a wall of false misses. Today materialises itself on
    page load; use this to fill a gap.
    """
    _check_writable(start)
    _check_writable(end)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if d1 < d0:
        raise HTTPException(400, "end is before start")
    if (d1 - d0).days > 366:
        raise HTTPException(400, "range longer than 366 days; narrow it")

    created: dict[str, list[str]] = {}
    for i in range((d1 - d0).days + 1):
        day = (d0 + timedelta(days=i)).isoformat()
        result = api_ensure_day(day)
        if result["created"]:
            created[day] = result["created"]
    return {"start": start, "end": end, "days_touched": len(created), "created": created}


@app.get("/api/day/{day}")
def api_day(day: str) -> list[dict]:
    """A day's tasks, ordered as the check-in page shows them."""
    _check_day(day)
    client = _sb()
    defs = {
        d["title"]: d["sort_order"]
        for d in (client.table("recurring_tasks").select("title,sort_order")
                  .execute().data or [])
    }
    rows = _day_rows(client, day)
    rows.sort(key=lambda r: (defs.get(r["title"], 999), r["title"]))
    return rows


@app.post("/api/task/{task_id}/toggle")
def api_toggle(task_id: int, done: bool) -> dict:
    """Set a single task's completion state."""
    client = _sb()
    # Addressed by id, not day -- look the row up so the write floor still applies.
    found = client.table("task_history").select("day").eq("id", task_id).execute().data
    if not found:
        raise HTTPException(404, f"no task_history row with id {task_id}")
    _check_writable(str(found[0]["day"])[:10])

    res = client.table("task_history").update({
        "is_completed": done,
        "status": "completed" if done else "needsAction",
        "completed_at": datetime.now(timezone.utc).isoformat() if done else None,
    }).eq("id", task_id).execute()
    if not res.data:
        raise HTTPException(404, f"no task_history row with id {task_id}")
    _cache.clear()
    return res.data[0]


@app.post("/api/day/{day}/weight")
def api_set_weight(day: str, lbs: float) -> dict:
    """Record a weigh-in, writing both the numeric column and the note text."""
    _check_writable(day)
    if not (WEIGHT_MIN <= lbs <= WEIGHT_MAX):
        raise HTTPException(
            400, f"{lbs} lbs is outside {WEIGHT_MIN}-{WEIGHT_MAX}; refusing as a typo."
        )
    client = _sb()
    rows = [r for r in _day_rows(client, day) if r["title"] == "Weigh yourself"]
    if not rows:
        raise HTTPException(404, f"no 'Weigh yourself' row for {day} -- ensure the day first.")
    row = rows[0]

    notes = row.get("notes") or "Start Date+Weight: 8/8/24, 181.9 lbs, Current Weight: "
    if CURRENT_WEIGHT_CLAUSE.search(notes):
        notes = CURRENT_WEIGHT_CLAUSE.sub(f"Current Weight: {lbs:.1f} lbs", notes, count=1)
    else:
        notes = notes.rstrip() + f" {lbs:.1f} lbs"

    res = client.table("task_history").update({
        "weight_lbs": lbs,
        "weight_source": "recorded",
        "notes": notes,
        "is_completed": True,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row["id"]).execute()
    _cache.clear()
    return res.data[0]


@app.post("/api/day/{day}/wake")
def api_set_wake(day: str, time: str) -> dict:
    """Record a wake-up time as HH:MM (24h)."""
    _check_writable(day)
    m = TIME_RE.match(time or "")
    if not m:
        raise HTTPException(400, f"time must be HH:MM (24h), got {time!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    suffix = "am" if hour < 12 else "pm"
    display = f"{(hour % 12) or 12}:{minute:02d} {suffix}"

    client = _sb()
    rows = [r for r in _day_rows(client, day) if r["title"] == "Wake Up"]
    if not rows:
        raise HTTPException(404, f"no 'Wake Up' row for {day} -- ensure the day first.")
    row = rows[0]

    res = client.table("task_history").update({
        "notes": f"Wake-up Time: {display}",
        "is_completed": True,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row["id"]).execute()
    _cache.clear()
    return res.data[0]


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
@app.get("/today", response_class=HTMLResponse)
def page_today(day: str | None = None) -> str:
    """The daily check-in, and the app's front door.

    Served at both / and /today. Recording the day is the thing done every
    day; the calendar and charts are for looking back, so they sit one click
    away rather than in the way.
    """
    from zoneinfo import ZoneInfo

    from .timezones import resolve_timezone

    if day:
        _check_day(day)
    else:
        tz = ZoneInfo(resolve_timezone(load_config(), allow_google=False))
        day = datetime.now(tz).date().isoformat()
    # A day below the write floor still renders -- read-only rather than broken.
    return _render("today.html", {
        "day": day,
        "writable": date.fromisoformat(day) >= WRITE_FLOOR,
        "floor": WRITE_FLOOR.isoformat(),
    })


@app.get("/calendar", response_class=HTMLResponse)
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
