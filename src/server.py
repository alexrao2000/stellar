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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, Response

from .config import load_config
from .parsers import parse_wake_up
from .read import read_rows

log = logging.getLogger("cal-task-sync")

TEMPLATES = Path(__file__).resolve().parent.parent / "scripts" / "templates"

# Same clause the reconciler rewrites, so the note format stays consistent
# between historical repair and new entries.
CURRENT_WEIGHT_CLAUSE = re.compile(
    r"current\s*weight\s*:\s*[\d.]*\s*(?:lbs?)?", re.IGNORECASE
)

# A full read is ~1.8 MB over seven sequential round trips, about two seconds.
# Writes are folded into the cache rather than dropping it, so it stays correct
# between refreshes and can be held far longer than the original two minutes.
# POST /api/refresh forces a re-read.
CACHE_TTL = 900.0
_cache: dict[str, tuple[float, object]] = {}

app = FastAPI(title="task-tracker", docs_url="/docs")

# The history payload is ~1.8 MB of highly repetitive JSON. Compressed it is a
# fraction of that, which is most of the cold-load cost on any connection
# slower than localhost.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.on_event("startup")
def _warm_cache() -> None:
    """Pull the history in the background as soon as the server comes up.

    Otherwise the first page load after `make dev` pays the full read. Done on a
    thread so startup is not blocked, and failures are logged rather than raised
    -- an unreachable database should not stop the server booting, since the
    error pages explain the problem better than a crash does.
    """
    import threading

    def go():
        try:
            _task_history()
        except Exception as exc:  # noqa: BLE001
            log.warning("cache warm failed (%s); first request will be slower", exc)

    threading.Thread(target=go, daemon=True).start()


def _cached(key: str, build):
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
        return hit[1]
    value = build()
    _cache[key] = (time.monotonic(), value)
    return value


# Exactly what the pages consume. Whitelisted rather than passed through, so a
# new database column cannot silently start shipping to the browser.
HISTORY_COLS = (
    "id", "day", "list_title", "title", "is_completed", "completed_at",
    "notes", "weight_lbs", "weight_source", "takeout_weight",
)


def _shape(r: dict) -> dict:
    """One database row in the shape the pages consume."""
    rec = {c: r.get(c) for c in HISTORY_COLS}
    rec["day"] = str(rec["day"] or "")[:10]
    rec["is_completed"] = bool(r.get("is_completed"))
    # numeric comes back as a string from PostgREST
    for numeric in ("weight_lbs", "takeout_weight"):
        v = rec[numeric]
        rec[numeric] = None if v is None or v == "" else float(v)
    return rec


def _task_history() -> list[dict]:
    def build():
        rows = [_shape(r) for r in read_rows(load_config(), "task_history")]
        rows.sort(key=lambda r: (r["day"], r["title"]))
        return rows

    return _cached("task_history", build)


def _cache_apply(changed: list[dict]) -> None:
    """Fold a write into the cached history rather than dropping the cache.

    Clearing meant a single checkbox tap cost a full 6,221-row refetch on the
    next page load -- about two seconds, on every interaction. The rows a write
    touches are known, so patch those and leave the rest alone. The cache
    timestamp is preserved so this does not extend the TTL indefinitely.
    """
    hit = _cache.get("task_history")
    if not hit:
        return
    ts, rows = hit
    index = {r["id"]: i for i, r in enumerate(rows) if r.get("id") is not None}
    dirty = False
    for raw in changed:
        rec = _shape(raw)
        pos = index.get(rec["id"])
        if pos is None:
            rows.append(rec)
        else:
            rows[pos] = rec
        dirty = True
    if dirty:
        rows.sort(key=lambda r: (r["day"], r["title"]))
        _cache["task_history"] = (ts, rows)


# A bare checkmark in the app's accent purple -- no plate behind it, so the tab
# reads the same on a light or a dark browser chrome. Split between the two
# theme purples would be worse than either: the tab bar does not necessarily
# match the page.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<path d="M5 17l8 8L27 8" fill="none" stroke="#7A4FD8" stroke-width="5.5"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

_FAVICON_LINK = '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'


@app.get("/favicon.svg")
def favicon() -> Response:
    return Response(
        FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _render(template: str, boot: dict) -> str:
    import json

    path = TEMPLATES / template
    if not path.exists():
        raise HTTPException(500, f"template missing: {template}")
    html = path.read_text(encoding="utf-8").replace(
        "__BOOTSTRAP__", json.dumps(boot, separators=(",", ":"))
    )
    # Injected here rather than in each template: every page goes through this
    # one function, so there is no way to add a view and forget the icon.
    return _FAVICON_LINK + html


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
            # `w` so the shared chart can read both series without branching;
            # `time` is kept for anything reading this endpoint directly.
            out.append({"d": r["day"], "time": parsed[0], "w": parsed[1]})
    return out


@app.post("/api/refresh")
def api_refresh() -> dict:
    """Drop the cache so the next read hits Supabase.

    Writes patch the cache in place rather than clearing it; this is the escape
    hatch for when the database changed underneath us.
    """
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
        created = client.table("task_history").insert([
            {
                "day": day,
                "list_title": d["list_title"],
                "title": d["title"],
                "status": "needsAction",
                "is_completed": False,
            }
            for d in wanted
        ]).execute()
        _cache_apply(created.data or [])
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


def _sort_order() -> dict[str, int]:
    """title -> sort_order, cached; it changes only when habits are edited."""
    def build():
        rows = (
            _sb().table("recurring_tasks").select("title,sort_order").execute().data or []
        )
        return {r["title"]: r["sort_order"] for r in rows}

    return _cached("sort_order", build)


@app.get("/api/day/{day}")
def api_day(day: str) -> list[dict]:
    """A day's tasks, ordered as the check-in page shows them.

    Served from the cached history rather than querying Supabase. Writes patch
    that cache, so it is already current -- and this is on the path of every
    task tap, where a live round trip was costing ~200ms of visible spinner.
    """
    _check_day(day)
    order = _sort_order()
    rows = [r for r in _task_history() if r["day"] == day]
    rows.sort(key=lambda r: (order.get(r["title"], 999), r["title"]))
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
    _cache_apply(res.data)
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
    _cache_apply(res.data)
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
    _cache_apply(res.data)
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


# Both series render through scripts/templates/series_chart.html. They differ
# only in labels, units and axis direction, so the differences live here rather
# than in a second copy of a 500-line template.
WEIGHT_CFG = {
    "title": "Weight Log",
    "eyebrow": "task_history \u00b7 weight",
    "sub": "Every weigh-in on record, read from the reconciled data.",
    "unit": "lbs",
    "format": "number",
    "invert": False,
    "legend": "Daily",
    "noun": "weigh-ins",
}

WAKE_CFG = {
    "title": "Wake-up Time",
    "eyebrow": "task_history \u00b7 wake up",
    "sub": "When the day started. Lower on the chart is earlier.",
    "unit": "",
    "format": "time",
    # No inversion. The default already places smaller values lower, which is
    # exactly "lower is earlier"; inverting put midnight at the top and
    # contradicted the caption.
    "invert": False,
    "legend": "Daily",
    "noun": "mornings",
    "statLabels": {
        "lbl-start": "First",
        "lbl-low": "Earliest",
        "lbl-high": "Latest riser",
    },
}


@app.get("/weight", response_class=HTMLResponse)
def page_weight() -> str:
    return _render("series_chart.html", {
        "mode": "live", "endpoint": "/api/weights", "headers": {}, "paged": False,
        "cfg": WEIGHT_CFG,
    })


@app.get("/wake", response_class=HTMLResponse)
def page_wake() -> str:
    return _render("series_chart.html", {
        "mode": "live", "endpoint": "/api/wake", "headers": {}, "paged": False,
        "cfg": WAKE_CFG,
    })


@app.get("/consistency", response_class=HTMLResponse)
def page_consistency() -> str:
    return _render("consistency.html", {
        "mode": "live", "endpoint": "/api/task_history", "headers": {}, "paged": False,
    })


@app.get("/data", response_class=HTMLResponse)
def page_data() -> str:
    return _render("data_table.html", {
        "mode": "live", "endpoint": "/api/task_history", "headers": {}, "paged": False,
    })


@app.get("/health")
def health() -> dict:
    rows = _task_history()
    return {"ok": True, "task_history_rows": len(rows),
            "cached": "task_history" in _cache}
