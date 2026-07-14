"""One-off probe: discover how the recurring tasks appear in Google Calendar.

Lists all calendars, then for each calendar searches events for the wake/weight
task titles across a wide date range and prints date + description so we can see
the real data shape before building the sync.

Run:  python -m scripts.probe_calendar
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleapiclient.discovery import build

from src.config import load_config
from src.google_auth import get_credentials

SEARCH_TERMS = ["Wake Up", "Weigh yourself", "Wake-up", "Weight"]
TIME_MIN = "2024-01-01T00:00:00Z"


def main() -> int:
    config = load_config()
    creds = get_credentials(config)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    print("=== Calendars ===")
    calendars = []
    page_token = None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        for cal in resp.get("items", []):
            calendars.append(cal)
            print(f"  id={cal['id']!r}  summary={cal.get('summary')!r}  primary={cal.get('primary', False)}")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    for cal in calendars:
        cal_id = cal["id"]
        for term in SEARCH_TERMS:
            events = []
            page_token = None
            while True:
                resp = (
                    service.events()
                    .list(
                        calendarId=cal_id,
                        q=term,
                        singleEvents=True,
                        orderBy="startTime",
                        timeMin=TIME_MIN,
                        maxResults=2500,
                        pageToken=page_token,
                    )
                    .execute()
                )
                events.extend(resp.get("items", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            if not events:
                continue
            print(f"\n=== {cal.get('summary')!r} / q={term!r}: {len(events)} events ===")
            for ev in events[:15]:
                start = ev.get("start", {})
                when = start.get("date") or start.get("dateTime")
                desc = (ev.get("description") or "").replace("\n", " ")[:120]
                print(f"  {when}  summary={ev.get('summary')!r}  desc={desc!r}")
            if len(events) > 15:
                print(f"  ... and {len(events) - 15} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
