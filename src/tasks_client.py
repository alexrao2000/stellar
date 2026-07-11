"""Thin client over the Google Tasks API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from googleapiclient.discovery import build

from .config import Config
from .google_auth import get_credentials


@dataclass
class TaskRecord:
    task_id: str
    list_id: str
    list_title: str
    title: str
    status: str  # "needsAction" | "completed"
    due: str | None  # RFC3339, date portion is meaningful
    completed: str | None  # RFC3339 timestamp
    notes: str | None
    updated: str | None


def _build_service(config: Config):
    creds = get_credentials(config)
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _iter_task_lists(service, wanted_names: list[str]) -> Iterator[dict]:
    page_token = None
    wanted = {n.lower() for n in wanted_names}
    while True:
        resp = service.tasklists().list(maxResults=100, pageToken=page_token).execute()
        for tl in resp.get("items", []):
            if not wanted or tl.get("title", "").lower() in wanted:
                yield tl
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def _iter_tasks(service, list_id: str) -> Iterator[dict]:
    page_token = None
    while True:
        resp = (
            service.tasks()
            .list(
                tasklist=list_id,
                showCompleted=True,
                showHidden=True,
                maxResults=100,
                pageToken=page_token,
            )
            .execute()
        )
        yield from resp.get("items", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def fetch_tasks(config: Config) -> list[TaskRecord]:
    """Fetch all (or configured) task lists and return flattened task records."""
    service = _build_service(config)
    records: list[TaskRecord] = []
    for tl in _iter_task_lists(service, config.task_lists):
        list_id = tl["id"]
        list_title = tl.get("title", "")
        for t in _iter_tasks(service, list_id):
            records.append(
                TaskRecord(
                    task_id=t["id"],
                    list_id=list_id,
                    list_title=list_title,
                    title=t.get("title", ""),
                    status=t.get("status", "needsAction"),
                    due=t.get("due"),
                    completed=t.get("completed"),
                    notes=t.get("notes"),
                    updated=t.get("updated"),
                )
            )
    return records
