"""Read task/event history from the Google Calendar API.

Recurring Google Tasks and recurring events show up in Google Calendar on every
date they were active, and (unlike the Tasks API) the Calendar view retains the
full history including any per-day description you added. This module expands
recurring items into individual dated occurrences.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from googleapiclient.discovery import build

from .config import Config
from .google_auth import get_credentials

log = logging.getLogger("cal-task-sync")

# How far back to look for history.
DEFAULT_TIME_MIN = "2023-01-01T00:00:00Z"


@dataclass
class CalendarEventRecord:
    event_id: str
    calendar_id: str
    calendar_summary: str
    title: str
    day: str | None  # YYYY-MM-DD
    description: str | None
    status: str | None  # confirmed / tentative / cancelled
    event_type: str | None
    updated: str | None


def _build_service(config: Config):
    creds = get_credentials(config)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _list_calendars(service) -> list[dict]:
    calendars: list[dict] = []
    page_token = None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        calendars.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return calendars


def _event_day(event: dict) -> str | None:
    start = event.get("start", {})
    # All-day items use `date`; timed items use `dateTime`.
    value = start.get("date") or start.get("dateTime")
    if not value:
        return None
    return value[:10]


def _iter_events(service, calendar_id: str, time_min: str, time_max: str):
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                singleEvents=True,  # expand recurring items into dated instances
                orderBy="startTime",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=2500,
                showDeleted=False,
                pageToken=page_token,
            )
            .execute()
        )
        yield from resp.get("items", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def fetch_calendar_events(
    config: Config,
    time_min: str = DEFAULT_TIME_MIN,
    time_max: str | None = None,
) -> list[CalendarEventRecord]:
    """Fetch every event instance across all visible calendars in the window."""
    from datetime import datetime, timedelta, timezone

    if time_max is None:
        time_max = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    service = _build_service(config)
    calendars = _list_calendars(service)
    log.info("Found %d calendars: %s", len(calendars), [c.get("summary") for c in calendars])

    records: list[CalendarEventRecord] = []
    for cal in calendars:
        cal_id = cal["id"]
        cal_summary = cal.get("summary", "")
        count = 0
        for ev in _iter_events(service, cal_id, time_min, time_max):
            count += 1
            records.append(
                CalendarEventRecord(
                    event_id=ev["id"],
                    calendar_id=cal_id,
                    calendar_summary=cal_summary,
                    title=ev.get("summary", ""),
                    day=_event_day(ev),
                    description=ev.get("description"),
                    status=ev.get("status"),
                    event_type=ev.get("eventType"),
                    updated=ev.get("updated"),
                )
            )
        log.info("  calendar %r: %d event instances", cal_summary, count)
    return records
