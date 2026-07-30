"""Unit tests for replacement-instance folding. Run: python -m pytest"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.titles import (  # noqa: E402
    canonicalize,
    infer_aliases,
    matches_metric_title,
    normalize,
    similarity,
)

BASE = date(2026, 1, 1)


def series(title, n, skip=()):
    """A daily series of `title` over n days, omitting offsets in `skip`."""
    return [(title, BASE + timedelta(days=i)) for i in range(n) if i not in skip]


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #
def test_normalize_whitespace_and_case():
    assert normalize("  Weigh   Yourself ") == normalize("weigh yourself")


def test_normalize_strips_emoji_and_edge_punctuation():
    assert normalize("Weigh yourself ✅") == "weigh yourself"
    assert normalize("*Weigh yourself!*") == "weigh yourself"


def test_normalize_keeps_interior_parenthetical():
    assert normalize("Brush Teeth (Twice)") == "brush teeth (twice)"


def test_normalize_empty():
    assert normalize("") == ""


# --------------------------------------------------------------------------- #
# Exact folding (post-normalization)
# --------------------------------------------------------------------------- #
def test_folds_trailing_whitespace_variant():
    records = series("Weigh yourself", 30, skip={10}) + [("Weigh yourself ", BASE + timedelta(days=10))]
    aliases, folds = infer_aliases(records)
    assert canonicalize("Weigh yourself ", aliases) == "Weigh yourself"
    assert len(folds) == 1


def test_canonical_is_the_denser_spelling():
    records = series("Weigh yourself", 30, skip={5}) + [("weigh YOURSELF", BASE + timedelta(days=5))]
    aliases, _ = infer_aliases(records)
    assert aliases["weigh YOURSELF"] == "Weigh yourself"


# --------------------------------------------------------------------------- #
# Gap folding
# --------------------------------------------------------------------------- #
def test_folds_sparse_lookalike_that_fills_a_gap():
    records = series("Weigh yourself", 30, skip={12}) + [("Weigh yourself (redo)", BASE + timedelta(days=12))]
    aliases, folds = infer_aliases(records, threshold=0.8)
    assert aliases.get("Weigh yourself (redo)") == "Weigh yourself"
    assert folds[0].days == (BASE + timedelta(days=12),)


def test_does_not_fold_cooccurring_distinct_task():
    """Regression: 'Brush Teeth' and 'Brush Teeth (Twice)' are separate daily tasks."""
    records = series("Brush Teeth", 30) + series("Brush Teeth (Twice)", 30)
    aliases, folds = infer_aliases(records, threshold=0.5)
    assert aliases == {}
    assert folds == []


def test_does_not_fold_when_days_overlap_even_partially():
    records = series("Run", 30) + [("Run!", BASE + timedelta(days=3))]
    aliases, _ = infer_aliases(records, threshold=0.5, max_sparse_days=3)
    # "Run!" normalizes to "run" -> exact fold is fine and expected here.
    assert aliases.get("Run!") == "Run"


def test_does_not_fold_dissimilar_title_in_a_gap():
    records = series("Weigh yourself", 30, skip={12}) + [("Call the dentist", BASE + timedelta(days=12))]
    aliases, folds = infer_aliases(records)
    assert aliases == {}
    assert folds == []


def test_does_not_fold_outside_the_parent_span():
    """A similarly-named task starting after the series ended is a new task."""
    records = series("Weigh yourself", 30) + [("Weigh yourself v2", BASE + timedelta(days=90))]
    aliases, _ = infer_aliases(records, threshold=0.8)
    assert aliases == {}


def test_does_not_fold_a_dense_candidate():
    records = series("Weigh yourself", 30, skip=set(range(10, 25))) + series(
        "Weigh yourself am", 30
    )[10:25]
    aliases, _ = infer_aliases(records, threshold=0.8, max_sparse_days=3)
    assert aliases == {}


def test_ignores_rows_with_no_day():
    aliases, folds = infer_aliases([("Weigh yourself", None), (None, BASE)])
    assert aliases == {} and folds == []


# --------------------------------------------------------------------------- #
# Explicit overrides
# --------------------------------------------------------------------------- #
def test_explicit_alias_wins_and_is_reported():
    records = series("Weigh yourself", 30) + [("Scale", BASE + timedelta(days=40))]
    aliases, folds = infer_aliases(records, explicit={"Scale": "Weigh yourself"})
    assert aliases["Scale"] == "Weigh yourself"
    assert any(f.reason.startswith("explicit") for f in folds)


def test_alias_chains_are_collapsed():
    aliases, _ = infer_aliases([], explicit={"a": "b", "b": "c"})
    assert aliases["a"] == "c"


def test_alias_cycle_does_not_hang():
    aliases, _ = infer_aliases([], explicit={"a": "b", "b": "a"})
    assert set(aliases) == {"a", "b"}


def test_threshold_of_one_disables_inference():
    records = series("Weigh yourself", 30, skip={12}) + [("Weigh yourself (redo)", BASE + timedelta(days=12))]
    aliases, _ = infer_aliases(records, threshold=1.0)
    assert "Weigh yourself (redo)" not in aliases


# --------------------------------------------------------------------------- #
# Metric title matching
# --------------------------------------------------------------------------- #
def test_metric_title_tolerates_drift():
    assert matches_metric_title("Weigh yourself ", "Weigh yourself")
    assert matches_metric_title("weigh YOURSELF", "Weigh yourself")
    assert matches_metric_title("Weigh yourself ✅", "Weigh yourself")


def test_metric_title_respects_aliases():
    assert matches_metric_title("Scale", "Weigh yourself", {"Scale": "Weigh yourself"})


def test_metric_title_rejects_other_tasks():
    assert not matches_metric_title("Brush Teeth", "Weigh yourself")


def test_similarity_bounds():
    assert similarity("Weigh yourself", "Weigh yourself") == 1.0
    assert similarity("Brush Teeth", "Brush Teeth (Twice)") < 0.86
