"""Unit tests for note parsing. Run: python -m pytest (or python tests/test_parsers.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parsers import parse_wake_up, parse_weight  # noqa: E402


def test_parse_wake_up_basic():
    assert parse_wake_up("Wake-up Time: 7:30 AM") == ("07:30:00", 7 * 60 + 30)


def test_parse_wake_up_pm_and_noise():
    assert parse_wake_up("did stuff. Wake up Time: 12:05 pm") == ("12:05:00", 12 * 60 + 5)


def test_parse_wake_up_midnight():
    assert parse_wake_up("Wake-up Time: 12:00 AM") == ("00:00:00", 0)


def test_parse_wake_up_missing():
    assert parse_wake_up("no time here") is None
    assert parse_wake_up(None) is None


def test_parse_weight_full():
    notes = "Start Date+Weight: 8/8/24, 181.9 lbs, Current Weight: 178.2 lbs"
    assert parse_weight(notes) == (178.2, "8/8/24", 181.9)


def test_parse_weight_without_lbs_unit():
    notes = "Start Date+Weight: 8/8/24, 181.9 lbs, Current Weight: 182.4 (this was with my jacket)"
    assert parse_weight(notes) == (182.4, "8/8/24", 181.9)


def test_parse_weight_current_only():
    assert parse_weight("Current Weight: 175 lbs") == (175.0, None, None)


def test_parse_weight_missing():
    assert parse_weight("Start Date+Weight: 8/8/24, 181.9 lbs") is None


def test_seed_start_weight_rows():
    from src.parsers import WeightRow, seed_start_weight_rows

    rows = [
        WeightRow(
            day="2026-07-11",
            weight_lbs=150.8,
            start_date="8/8/24",
            start_weight_lbs=181.9,
            raw_notes="...",
            source_task_id="abc",
        )
    ]
    seeded = seed_start_weight_rows(rows)
    days = {r.day for r in seeded}
    assert days == {"2024-08-08", "2026-07-11"}
    start = next(r for r in seeded if r.day == "2024-08-08")
    assert start.weight_lbs == 181.9


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
