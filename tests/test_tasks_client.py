"""Tests for task listing. Run: python -m pytest

Regression cover for the sync silently dropping every incomplete task: passing
`completedMin` makes the Google Tasks API return completed tasks only, so a
single-pass fetch recorded no missed days at all.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tasks_client import _iter_tasks  # noqa: E402

TASKS = [
    {"id": "c1", "title": "Brush Teeth", "status": "completed", "completed": "2026-07-29T16:46:39Z"},
    {"id": "c2", "title": "Supplements", "status": "completed", "completed": "2026-07-29T16:46:44Z"},
    {"id": "n1", "title": "Run", "status": "needsAction"},
    {"id": "n2", "title": "Floss", "status": "needsAction"},
]


class FakeService:
    """Mimics the real API: completedMin filters out needsAction tasks.

    Also paginates, so the page-following logic is exercised.
    """

    def __init__(self, tasks=TASKS, page_size=3):
        self._rows = tasks
        self.page_size = page_size
        self.calls = []

    def tasks(self):  # noqa: A003 - mirrors the googleapiclient surface
        return self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        rows = self._rows
        if "completedMin" in kwargs:
            rows = [t for t in rows if t["status"] == "completed"]
        start = int(kwargs.get("pageToken") or 0)
        page = rows[start:start + self.page_size]
        nxt = start + self.page_size
        self._resp = {"items": page}
        if nxt < len(rows):
            self._resp["nextPageToken"] = str(nxt)
        return self

    def execute(self):
        return self._resp


def test_returns_both_completed_and_incomplete():
    got = list(_iter_tasks(FakeService(), "list-1"))
    assert {t["id"] for t in got} == {"c1", "c2", "n1", "n2"}


def test_incomplete_tasks_are_not_dropped():
    got = list(_iter_tasks(FakeService(), "list-1"))
    assert [t["id"] for t in got if t["status"] == "needsAction"] == ["n1", "n2"]


def test_no_duplicates_when_a_task_appears_in_both_passes():
    got = list(_iter_tasks(FakeService(), "list-1"))
    ids = [t["id"] for t in got]
    assert len(ids) == len(set(ids))


def test_makes_one_pass_with_completed_min_and_one_without():
    svc = FakeService()
    list(_iter_tasks(svc, "list-1"))
    with_min = [c for c in svc.calls if "completedMin" in c]
    without_min = [c for c in svc.calls if "completedMin" not in c]
    assert with_min and without_min


def test_always_requests_hidden_and_completed():
    svc = FakeService()
    list(_iter_tasks(svc, "list-1"))
    assert all(c["showCompleted"] and c["showHidden"] for c in svc.calls)


def test_deleted_tasks_stay_excluded():
    svc = FakeService()
    list(_iter_tasks(svc, "list-1"))
    assert all(c["showDeleted"] is False for c in svc.calls)


def test_pagination_is_followed():
    many = [{"id": f"t{i}", "status": "needsAction"} for i in range(7)]
    got = list(_iter_tasks(FakeService(tasks=many, page_size=2), "list-1"))
    assert len(got) == 7


def test_empty_list_yields_nothing():
    assert list(_iter_tasks(FakeService(tasks=[]), "list-1")) == []
