"""Centralized configuration loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks.readonly"]


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
    task_lists: list[str] = field(default_factory=lambda: _split_csv(os.getenv("TASK_LISTS", "")))
    local_timezone: str = field(default_factory=lambda: os.getenv("LOCAL_TIMEZONE", "America/Los_Angeles"))

    storage_backend: str = field(default_factory=lambda: os.getenv("STORAGE_BACKEND", "supabase").lower())
    supabase_url: str = field(default_factory=lambda: os.getenv("SUPABASE_URL", ""))
    supabase_key: str = field(default_factory=lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    sqlite_path: str = field(default_factory=lambda: _resolve(os.getenv("SQLITE_PATH", "cal_task_sync.sqlite")))

    wake_up_task_title: str = field(default_factory=lambda: os.getenv("WAKE_UP_TASK_TITLE", "Wake Up"))
    weight_task_title: str = field(default_factory=lambda: os.getenv("WEIGHT_TASK_TITLE", "Weigh yourself"))

    def validate(self) -> None:
        if not Path(self.google_credentials_file).exists():
            raise FileNotFoundError(
                f"Google credentials file not found at {self.google_credentials_file}. "
                "Download an OAuth desktop client from Google Cloud and set GOOGLE_CREDENTIALS_FILE."
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
