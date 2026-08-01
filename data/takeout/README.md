# Archived Google Takeout exports

Raw, unmodified Google Takeout **Tasks** exports, committed deliberately.

## Why these are archived

The Google Tasks API cannot enumerate history. `tasks.list()` returns only the
currently-materialized occurrence of a recurring task; past occurrences drop out
of the listing within roughly 24 hours. They still exist server-side and remain
reachable via `tasks.get(id)` — but only if you captured the id while it was
still listable. Once it ages out with no id recorded, it is unreachable through
the API forever.

A Takeout export is the one source that contains the full per-occurrence
history: every instance, its own notes, and its real completion status. It is
how `weight_log`, `wake_up_log`, and `task_completions` were originally
backfilled, and it is the only way to reconstruct the record if the database is
lost or a day is missed.

Takeout reflects Google's state **at export time**, so it cannot be regenerated
retroactively. That is the reason to keep the file rather than re-export later:
a future export cannot recover what a past one held.

## Contents

| File | Exported | Covers |
| --- | --- | --- |
| `tasks-takeout-2026-07-31.zip` | 2026-07-31 | 5,767 task instances across 5 lists; Daily list spans 2024-09-19 → 2026-08-23 (682 days) |

Each zip contains a single `Takeout/Tasks/Tasks.json`.

## Fields worth knowing

Takeout records carry a `task_recurrence_id` that groups occurrences of the same
recurring series. The live Tasks API exposes **no** recurrence field at all, so
this grouping is available only here.

## Using them

```bash
# Full backfill into the configured store
make import-takeout FILE=data/takeout/tasks-takeout-2026-07-31.zip

# Per-day completion status only (read-only)
python -m scripts.parse_takeout_status data/takeout/tasks-takeout-2026-07-31.zip --out status.json

# Compare stored completion status against Google's (read-only, writes a corrected copy)
python -m scripts.reconcile_completion data/takeout/tasks-takeout-2026-07-31.zip --out reconciled.json
```

## Note

These contain personal data — daily weights, wake-up times, and task notes. They
are committed because this repository is private. If it is ever made public,
remove them **and** purge them from git history; deleting the file alone leaves
the contents in every prior commit.
