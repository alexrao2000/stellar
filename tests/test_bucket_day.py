"""Tests for day bucketing. Run: python -m pytest"""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parsers import bucket_day  # noqa: E402
from src.tasks_client import TaskRecord  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")


def task(**kw):
    base = dict(
        task_id="t", list_id="l", list_title="Daily", title="Run",
        status="needsAction", due=None, completed=None, notes=None, updated=None,
    )
    return TaskRecord(**{**base, **kw})


def test_due_date_wins():
    t = task(due="2026-07-29T00:00:00.000Z", completed="2026-07-30T08:00:00.000Z")
    assert bucket_day(t, TZ) == "2026-07-29"


def test_after_midnight_completion_counts_for_the_due_day():
    """The nightly-update spillover case: due the 29th, finished 1am on the 30th."""
    t = task(due="2026-07-29T00:00:00.000Z", status="completed",
             completed="2026-07-30T08:30:00.000Z")
    assert bucket_day(t, TZ) == "2026-07-29"


def test_completed_without_due_uses_completion_day():
    t = task(status="completed", completed="2026-07-29T16:46:39.000Z")
    assert bucket_day(t, TZ) == "2026-07-29"


def test_undated_incomplete_task_gets_no_day():
    """Regression: an undated backlog item must not invent a missed day.

    Falling back to `updated` here would mark "Clean Car" as missed on whatever
    day it was last touched, polluting completion stats.
    """
    t = task(status="needsAction", updated="2026-07-29T16:46:39.000Z")
    assert bucket_day(t, TZ) is None


def test_undated_incomplete_task_with_no_timestamps_at_all():
    assert bucket_day(task(), TZ) is None


def test_completed_with_unparseable_completion_falls_back_to_updated():
    t = task(status="completed", completed="not-a-timestamp",
             updated="2026-07-29T16:46:39.000Z")
    assert bucket_day(t, TZ) == "2026-07-29"


def test_malformed_due_is_ignored_in_favor_of_completion():
    t = task(due="garbage", status="completed", completed="2026-07-29T16:46:39.000Z")
    assert bucket_day(t, TZ) == "2026-07-29"


def test_timezone_shifts_the_bucket():
    """A UTC timestamp just after midnight belongs to the previous Pacific day."""
    t = task(status="completed", completed="2026-07-30T05:00:00.000Z")
    assert bucket_day(t, TZ) == "2026-07-29"
    assert bucket_day(t, ZoneInfo("UTC")) == "2026-07-30"
