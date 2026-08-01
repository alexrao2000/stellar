"""Resolve which timezone to use for bucketing tasks into calendar days.

Priority:
  1. Explicit LOCAL_TIMEZONE env override.
  2. Google Calendar timezone setting (authoritative for the user).
  3. This machine's detected timezone.
  4. UTC.

The Google Calendar source matters because the sync runs on GitHub Actions (UTC),
where machine-local detection would otherwise mis-bucket evening tasks.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config, detect_local_timezone

log = logging.getLogger("cal-task-sync")


def local_midnight_utc(day: str, tz_name: str) -> str:
    """RFC3339 UTC timestamp for local midnight on `day` (YYYY-MM-DD).

    Google Tasks encodes a due date as local midnight expressed in UTC -- so a
    Pacific date is stored as 07:00:00Z, not 00:00:00Z. Writing a literal
    00:00:00Z puts the task at 5pm the *previous* local day, and any bucketing
    that converts to a local calendar day then files it a day early.
    """
    local = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz_name))
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def resolve_timezone(config: Config, allow_google: bool = True) -> str:
    env_tz = os.getenv("LOCAL_TIMEZONE", "").strip()
    if env_tz:
        return env_tz

    if allow_google:
        try:
            from .calendar_client import fetch_calendar_timezone

            tz = fetch_calendar_timezone(config)
            if tz:
                log.info("Using Google Calendar timezone: %s", tz)
                return tz
        except Exception as exc:  # noqa: BLE001
            log.warning("Google Calendar timezone lookup failed (%s).", exc)

    machine_tz = detect_local_timezone()
    if machine_tz:
        log.info("Using machine-detected timezone: %s", machine_tz)
        return machine_tz

    log.info("Falling back to UTC timezone.")
    return "UTC"
