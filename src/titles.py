"""Fold replacement task instances back into their parent recurring task.

Deleting a single day's instance of a daily task and hand-recreating it produces
a second task whose title is *nearly* the same ("Weigh yourself " with a stray
space, "Weigh yourself (redo)", a typo). Everything downstream groups by the raw
title, so the split shows up twice over: a gap in the daily task's streak, plus a
sparse one-off task cluttering the consistency ranking.

Folding on title similarity alone is not safe here -- "Brush Teeth" and
"Brush Teeth (Twice)" are genuinely different daily tasks. The discriminator is
*occupancy*: a replacement fills a day the parent series is missing, whereas a
distinct task runs alongside it. So a candidate is folded only when it is
similarly titled AND sparse AND lands exclusively on days the parent series does
not already cover AND falls inside the parent's date span.

Raw rows are never rewritten; canonicalization happens at read time.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

# A title is an established series once it covers this many distinct days.
MIN_SERIES_DAYS = 10
# A fold candidate may cover at most this many distinct days.
MAX_SPARSE_DAYS = 3
# Minimum normalized-title similarity to consider folding.
DEFAULT_THRESHOLD = 0.86

# Decorative punctuation to trim from the ends. Deliberately excludes brackets so
# a meaningful suffix like "(Twice)" survives intact and stays distinguishable.
_EDGE_JUNK = re.compile(r"""^[\s*_~\-–—:;,.!?#"'`]+|[\s*_~\-–—:;,.!?#"'`]+$""")
_WS = re.compile(r"\s+")


def normalize(title: str) -> str:
    """Aggressively normalize a title for comparison only.

    Casefolds, strips accents and emoji/variation selectors, collapses internal
    whitespace, and trims leading/trailing punctuation. Deliberately preserves
    interior parentheticals -- "(Twice)" distinguishes a real task.
    """
    if not title:
        return ""
    # Decompose and drop combining marks + symbol/other codepoints (emoji).
    decomposed = unicodedata.normalize("NFKD", title)
    kept = [
        ch for ch in decomposed
        if not unicodedata.combining(ch) and unicodedata.category(ch) not in {"So", "Cf"}
    ]
    cleaned = _WS.sub(" ", "".join(kept)).strip().casefold()
    return _EDGE_JUNK.sub("", cleaned)


def similarity(a: str, b: str) -> float:
    """Normalized-title similarity in [0, 1]."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


@dataclass(frozen=True)
class Fold:
    """One variant title folded into a canonical one."""
    variant: str
    canonical: str
    days: tuple[date, ...]
    score: float
    reason: str

    def describe(self) -> str:
        when = ", ".join(d.isoformat() for d in self.days)
        return f"{self.variant!r} -> {self.canonical!r} ({self.reason}; {when})"


def _days_by_title(records) -> dict[str, set[date]]:
    out: dict[str, set[date]] = {}
    for title, day in records:
        if title is None or day is None:
            continue
        out.setdefault(title, set()).add(day)
    return out


def _fold_exact(days: dict[str, set[date]]) -> tuple[dict[str, str], list[Fold]]:
    """Fold titles that are identical after normalization (whitespace, case, emoji)."""
    clusters: dict[str, list[str]] = {}
    for title in days:
        clusters.setdefault(normalize(title), []).append(title)

    aliases: dict[str, str] = {}
    folds: list[Fold] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        # Canonical = the spelling covering the most days, ties broken alphabetically
        # so the choice is deterministic across runs.
        canonical = max(members, key=lambda t: (len(days[t]), t))
        for variant in members:
            if variant == canonical:
                continue
            aliases[variant] = canonical
            folds.append(
                Fold(
                    variant=variant,
                    canonical=canonical,
                    days=tuple(sorted(days[variant])),
                    score=1.0,
                    reason="identical after normalization",
                )
            )
    return aliases, folds


def _fold_gaps(
    days: dict[str, set[date]],
    settled: dict[str, str],
    threshold: float,
    min_series_days: int,
    max_sparse_days: int,
) -> tuple[dict[str, str], list[Fold]]:
    """Fold sparse, similarly-titled variants that fill gaps in an established series."""
    # Collapse already-aliased titles into their canonical day sets first.
    merged: dict[str, set[date]] = {}
    for title, day_set in days.items():
        merged.setdefault(settled.get(title, title), set()).update(day_set)

    series = {t: d for t, d in merged.items() if len(d) >= min_series_days}
    if not series:
        return {}, []

    aliases: dict[str, str] = {}
    folds: list[Fold] = []
    for title, day_set in sorted(merged.items()):
        if title in series or len(day_set) > max_sparse_days:
            continue

        best: tuple[float, str] | None = None
        for cand, cand_days in series.items():
            score = similarity(title, cand)
            if score < threshold:
                continue
            # Must fill gaps, not co-occur: no shared days with the parent.
            if day_set & cand_days:
                continue
            # Must sit inside the parent's span, so a genuinely new task that
            # merely resembles an old one is not swallowed.
            lo, hi = min(cand_days), max(cand_days)
            if not all(lo <= d <= hi for d in day_set):
                continue
            if best is None or score > best[0]:
                best = (score, cand)

        if best:
            score, canonical = best
            aliases[title] = canonical
            folds.append(
                Fold(
                    variant=title,
                    canonical=canonical,
                    days=tuple(sorted(day_set)),
                    score=score,
                    reason=f"fills gap in similar series (similarity {score:.0%})",
                )
            )
    return aliases, folds


def infer_aliases(
    records,
    explicit: dict[str, str] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    min_series_days: int = MIN_SERIES_DAYS,
    max_sparse_days: int = MAX_SPARSE_DAYS,
) -> tuple[dict[str, str], list[Fold]]:
    """Build a variant-title -> canonical-title map from (title, day) records.

    Explicit mappings from config always win over inferred ones.
    Returns the alias map and a human-readable list of what was folded.
    """
    days = _days_by_title(records)

    aliases, folds = _fold_exact(days)
    gap_aliases, gap_folds = _fold_gaps(
        days, aliases, threshold, min_series_days, max_sparse_days
    )
    aliases.update(gap_aliases)
    folds.extend(gap_folds)

    for variant, canonical in (explicit or {}).items():
        aliases[variant] = canonical
        folds = [f for f in folds if f.variant != variant]
        folds.append(
            Fold(
                variant=variant,
                canonical=canonical,
                days=tuple(sorted(days.get(variant, set()))),
                score=1.0,
                reason="explicit TITLE_ALIASES override",
            )
        )

    return _resolve_chains(aliases), folds


def _resolve_chains(aliases: dict[str, str]) -> dict[str, str]:
    """Collapse a -> b -> c into a -> c, tolerating cycles."""
    resolved: dict[str, str] = {}
    for start in aliases:
        seen = {start}
        target = aliases[start]
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        resolved[start] = target
    return resolved


def canonicalize(title: str, aliases: dict[str, str]) -> str:
    """Map a raw title to its canonical form (identity when unmapped)."""
    return aliases.get(title, title)


def matches_metric_title(title: str, configured: str, aliases: dict[str, str] | None = None) -> bool:
    """True when `title` denotes the configured metric task.

    Tolerates the whitespace/case/emoji variants that a hand-recreated instance
    picks up, so a replacement weigh-in still yields a weight datapoint.
    """
    canonical = canonicalize(title, aliases or {})
    target = normalize(configured)
    return normalize(canonical) == target or normalize(title) == target
