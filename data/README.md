# Data

Committed deliberately. These are the recovery artifacts and the sources they
were built from. See [`../NOTES.md`](../NOTES.md) for the reasoning behind the
reconciliation.

## Sources

| file | what it is |
| --- | --- |
| `takeout/tasks-takeout-2026-07-31.zip` | Raw Google Takeout export. Full per-occurrence history and true completion status. Weights destroyed by the overwrite. See [`takeout/README.md`](takeout/README.md). |
| `supabase-history-truncated.json` | An early export of `task_completions`, taken before the pagination fix. **Truncated at 1000 rows** (2026-03-11 → 2026-07-14 only) — kept for provenance, not for use. |

Supabase itself is the other source and is not committed; `weight_log` is read
live by the build scripts.

## Built artifacts

| file | built by | what it is |
| --- | --- | --- |
| `reconciled_export.json` | `scripts/build_reconciled_export.py` | **The reconciled dataset.** 5,733 records: Takeout for every task and its completion status, Supabase `weight_log` for weight values, plus four supplied days. |
| `takeout_completion_status.json` | `scripts/parse_takeout_status.py` | Per-day completion status extracted from the Takeout, status only. |
| `jul29-google-snapshot.json` | ad-hoc probe | Full Google Tasks records for 2026-07-29, captured by id while they were still listable. |
| `pages/calendar.html` | `scripts/build_calendar_page.py` | Calendar view of the reconciled data. Self-contained; open it directly. |
| `pages/weight_log.html` | `scripts/build_weight_page.py` | Weight chart, 621 weigh-ins. Self-contained; open it directly. |

## Rebuilding

Everything above is reproducible. The two pages inline their data, so they go
stale when the source does — rebuild rather than edit them.

```bash
python -m scripts.build_reconciled_export data/takeout/tasks-takeout-2026-07-31.zip \
    --out data/reconciled_export.json
python -m scripts.build_calendar_page data/reconciled_export.json --out data/pages/calendar.html
python -m scripts.build_weight_page --out data/pages/weight_log.html
```

`build_reconciled_export` and `build_weight_page` read Supabase, so they need
`SUPABASE_URL` and a key in `.env`. All three are read-only with respect to
Supabase and Google — nothing here writes to either.

## Note

These contain personal data — daily weights, wake-up times, task notes.
Committed because this repository is private. If it is ever made public, remove
them **and** purge them from git history; deleting the files alone leaves the
contents in every prior commit.
