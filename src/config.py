"""Centralized configuration loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks.readonly"]
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Full set of scopes requested during OAuth consent.
SCOPES = TASKS_SCOPES + CALENDAR_SCOPES


def _resolve(path_value: str) -> str:
    """Resolve a possibly-relative path against the project root."""
    p = Path(path_value)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    google_credentials_file: str = field(
        default_factory=lambda: _resolve(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
    )
    google_token_file: str = field(
        default_factory=lambda: _resolve(os.getenv("GOOGLE_TOKEN_FILE", "token.json"))
    )
    # For headless/CI runs, credentials and the cached token can be provided inline
    # as JSON via env vars (GitHub secrets) instead of files on disk.
    google_credentials_json: str = field(default_factory=lambda: os.getenv("GOOGLE_CREDENTIALS_JSON", ""))
    google_token_json: str = field(default_factory=lambda: os.getenv("GOOGLE_TOKEN_JSON", ""))
    task_lists: list[str] = field(default_factory=lambda: _split_csv(os.getenv("TASK_LISTS", "")))
    local_timezone: str = field(default_factory=lambda: os.getenv("LOCAL_TIMEZONE", "America/Los_Angeles"))

    storage_backend: str = field(default_factory=lambda: os.getenv("STORAGE_BACKEND", "supabase").lower())
    supabase_url: str = field(default_factory=lambda: os.getenv("SUPABASE_URL", ""))
    supabase_key: str = field(default_factory=lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    sqlite_path: str = field(default_factory=lambda: _resolve(os.getenv("SQLITE_PATH", "cal_task_sync.sqlite")))

    wake_up_task_title: str = field(default_factory=lambda: os.getenv("WAKE_UP_TASK_TITLE", "Wake Up"))
    weight_task_title: str = field(default_factory=lambda: os.getenv("WEIGHT_TASK_TITLE", "Weigh yourself"))

    # Calendar API does not expose Google Tasks (only real events), so it's off by
    # default. Enable only if you also want to ingest calendar events.
    include_calendar_events: bool = field(
        default_factory=lambda: os.getenv("INCLUDE_CALENDAR_EVENTS", "false").lower() == "true"
    )

    def validate(self) -> None:
        if not self.google_credentials_json and not Path(self.google_credentials_file).exists():
            raise FileNotFoundError(
                f"Google credentials not found at {self.google_credentials_file} and "
                "GOOGLE_CREDENTIALS_JSON is unset. Provide one of them."
            )
        if self.storage_backend == "supabase":
            if not self.supabase_url or not self.supabase_key:
                raise ValueError(
                    "STORAGE_BACKEND=supabase requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
                )
        elif self.storage_backend != "sqlite":
            raise ValueError(f"Unknown STORAGE_BACKEND: {self.storage_backend!r} (use 'supabase' or 'sqlite').")


def load_config() -> Config:
    return Config()
