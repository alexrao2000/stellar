"""Keep a free-tier Supabase project active.

Supabase pauses free projects after ~7 days of inactivity. This job performs a
tiny write against a dedicated `heartbeat` table so the project stays awake. It
deliberately has NO Google dependency, so it keeps working even if the Google
OAuth token expires and the main sync is temporarily failing.

Run:  python -m src.keepalive
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from .config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cal-task-sync")


def run() -> int:
    config = load_config()
    if not config.supabase_url or not config.supabase_key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
        return 2

    from supabase import create_client

    client = create_client(config.supabase_url, config.supabase_key)
    now = datetime.now(timezone.utc).isoformat()
    client.table("heartbeat").upsert(
        {"id": 1, "last_ping": now, "note": "keepalive"}, on_conflict="id"
    ).execute()
    log.info("Heartbeat written at %s; Supabase project kept active.", now)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001
        log.exception("Keepalive failed: %s", exc)
        sys.exit(1)
