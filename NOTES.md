# Working notes

Running record of what is established about the weight data, so it does not have
to be re-derived or re-argued.

## Inferring weight on missing days

Weight is a slowly-varying quantity. It does not jump 30 lbs in a day, a week,
or a month. So a day with no recorded weight is not unconstrained: the days
around it bound what it could have been, and the tighter the bracket, the
tighter the inference.

This means a missing day sitting between two known days can be estimated from
them. It holds whether or not the day was ever recorded, because the actual body
weight existed regardless of whether it reached the database.

Corollary: when empty records cluster together, the whole cluster is still
constrained by the known values on either side of it. Values inside a gap are
relative to each other and to the bracketing days, not arbitrary.

## The scale

Reads in **0.2 lb increments**, so candidate values are a discrete set
(152.0, 152.2, 152.4, 152.6, ...), not a continuum. This narrows any estimate.

Applies to the current scale only. The early history came from a different
scale — 181.9 (start) and 187.7 (peak) are not multiples of 0.2.

## Reference points

- Current weight: **152.0**, as of the last weigh-in on 2026-07-31.
- Jul 2026 sat in a ~151–156 band. Recorded: Jul 17 = 152.4, Jul 20 = 155.6,
  Jul 21 = 153.8, Jul 22 = 155.8, Jul 23 = 154.0, Jul 28 = 152.6,
  Jul 29 = 151.6, Jul 30 = 153.4.
- 152.6 is a real, recurring reading — `weight_log` holds it on 8 separate days
  (2025-06-25, 2025-07-16, 2025-07-17, 2025-07-28, 2025-09-24, 2025-09-25,
  2026-06-27, 2026-06-30, 2026-07-13, 2026-07-28). It must not be treated as
  inherently suspect.

## The 152.6 propagation — "the overwrite"

Referred to throughout as **the overwrite**: a single event, not a general
description.

Every "Weigh yourself" occurrence in Google Takeout carries the same weight,
152.6 — 677 of 681 records — and all 677 share one `updated` timestamp to the
microsecond:

    2026-07-29T17:19:57.420223Z

Editing one occurrence's description propagates it to the whole recurring
series, so a change meant for one day landed on every day. **Who made that edit
is unresolved and is not to be relitigated.**

Consequence that matters: the pre-overwrite values are **destroyed in Takeout**.
For any day Takeout covers but `weight_log` does not, the original reading is
unrecoverable from Google. `weight_log` is the only surviving source of real
per-day weights (621 days, 253 distinct values, 145.8–187.7).

The overwrite was **indiscriminate** — it did not skip occurrences that were
blank. Days with no weigh-in at all (no scale, or simply forgotten, and the task
left incomplete) now also read 152.6, where previously there was nothing.

So **a 152.6 in Takeout carries no information whatsoever.** It cannot indicate
whether a weight was taken, let alone what it was. It marks only that the
occurrence existed when the overwrite ran. The surviving signals are:

1. **completion status** — did a weigh-in happen at all
2. **presence of a `weight_log` row** — did the real value survive anywhere

## `weight_log` is the source of truth

Supabase `weight_log` is a snapshot of the original Google Tasks data taken
**before** the overwrite. For every affected day it is the only surviving record
of the real value, and it is authoritative: where it has a row, that value
stands and is not to be second-guessed.

Any inaccuracy in it would have to predate the snapshot. That possibility is
closed — there is no evidence for it, too much time has passed, and it is not
worth pursuing. Do not reopen it.

The only real limitation is **coverage, not accuracy**: 621 days, ending
2026-07-30. Where it has no row it is silent, not wrong.

## Each source has exactly one flaw

A second Takeout was taken *after* the overwrite, specifically to get an
authoritative read on completion status. Comparing the two sources showed each
has one defect, and they do not overlap:

| source | its one flaw | authoritative for |
| --- | --- | --- |
| Google Takeout | the overwrite — weights destroyed | **completion status** |
| Supabase `weight_log` | late completions missed | **weight values** |

That is why the reconciled export is Takeout for everything, `weight_log` for
the number.

### Why the pipeline runs this direction

The original plan was the reverse: take completion status from the Takeout and
merge it *into* the Supabase data, with Supabase as the base. That was abandoned
deliberately.

Completion status is the harder payload to move: 5,733 booleans keyed by day
*and* title across every task, carrying the day-bucketing and duplicate-
occurrence edge cases. Weight is 621 scalars keyed by day for a single task.
Moving the simpler payload into the richer base is the lower-risk direction, so
the pipeline was reversed to Takeout-as-base.

### Why Supabase misses completions

The sync only captures a completion it can still see. A task marked complete
after the last sync run that could reach that occurrence is never recorded as
complete — and because an occurrence drops out of `list()` within roughly 24h,
the sync cannot go back for it later. The completion is lost permanently.

This hits **late completions** — tasks finished a day behind. Observed in
practice on Floss and Brush Teeth (Twice), which are routinely completed the
following day.

Signature: every one of the 5 corrections the reconciliation found was
`false -> true`, never the reverse. Consistent with a capture window closing,
not with wrong data being written.

## Completion implies a recorded weight

Alex never completes "Weigh yourself" without recording a weight. Verified:
620 of 620 weigh-ins that have a `weight_log` value are marked completed, with
**zero** inverse cases (a weight logged without completion).

This makes completion status a reliable signal for classifying gaps.

## The 46 Takeout weigh-days with no `weight_log` row

Split by completion status:

- **42 are `needsAction`** — no weigh-in happened (often: no scale that day).
  Correctly marked `not recorded`; weight is genuinely blank, not lost.
- **4 are `completed`** — a weight *was* taken but never reached Supabase.
  Labelling these `not recorded` is wrong and is pending correction:

  | day | Takeout value | note |
  | --- | --- | --- |
  | 2026-07-18 | 152.6 | propagation value; original unknown |
  | 2026-07-24 | 152.6 | propagation value; original unknown |
  | 2026-07-27 | 152.6 | propagation value; original unknown |
  | 2026-07-31 | 152.0 | differs from 152.6, so written *after* the
  propagation — likely the genuine reading |

### Decision

**2026-07-18, 07-24, 07-27 are set to 152.6**, by Alex's inductive reasoning.
Not recalled outright, and not interpolated by any script: the task was
completed so a weight was taken; the real value is destroyed in Takeout and
absent from Supabase; July sat in a ~151–156 band; 152.6 is on the scale's 0.2
grid and is a value actually recorded twice nearby (Jul 13, Jul 28).

The surrounding days **corroborate** the conclusion; they are not where it came
from. Do not describe these as estimated or interpolated from neighbours.

Corroboration was attempted and is not available: all three days are missing
from `task_completions` entirely, not merely from `weight_log`, so no raw note
text survives anywhere in Supabase. Nothing confirms the value; nothing
contradicts it.

Supporting the mechanism, the sync ran daily around 21:45–22:05 UTC and captured
every neighbouring day — Jul 17, 20, 21, 22, 23, 28 all landed, while 18, 24 and
27 are simply absent. Consistent with the occurrence ageing out before a sync
could see it.

**2026-07-31 is set aside** — it is today, and its Takeout value (152.0) differs
from the propagated 152.6, so it was written after the overwrite and is likely
genuine. Not covered by the inference above.
