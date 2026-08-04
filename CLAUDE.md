# Working in this repo

## Never write to Google Tasks directly

**No direct writes to the Google Tasks API. Anything that touches it goes
through a script that dry-runs by default, prints before/after, and is applied
only after the human reviews that output.**

This is the most important rule here, and it is not negotiable on grounds of
convenience or urgency.

Why:

- A write intended for **one occurrence propagates to the entire recurring
  series**. There is no per-occurrence write. This destroyed roughly two years
  of weight values in this project — 677 of 681 records overwritten with a
  single value in one atomic edit.
- It is **silent and irreversible**. No preview, no undo, no error.
- **The API cannot diagnose the damage.** `tasks.list()` returns only the
  current occurrence; history is not enumerable. So there is no way to compare a
  series before and after a write.

That last point has a direct behavioural consequence: **never offer a clean read
as evidence that a write was safe.** "I checked and found no problem" means
nothing when the tool structurally cannot see the thing being checked. Saying it
anyway wasted hours and repeatedly contradicted the user, who was right.

Write to Supabase `task_history` instead — diffable, row-addressed, recoverable.

## Other standing rules

- **Confirm the plan before building.** State it, then wait for a go-ahead.
  Do not start implementing off your own reading of the request.
- **Dry-run defaults.** Any script that mutates anything takes `--apply`; the
  default prints what it would do. This has already prevented one bad write.
- **Do not re-enable Supabase legacy API keys.** A `service_role` key leaked; it
  is dead only because legacy keys are disabled. Auth is the `sb_secret_…` key
  in `SUPABASE_SECRET_KEY`.
- **`WRITE_FLOOR` in `src/server.py`** guards the reconciled history. Days before
  it are read-only. Lift it deliberately, never as a side effect.
- Use `python -m pip`, not `pip` — the venv was relocated and its `pip` script
  has a stale shebang.

## Where things are

| file | what |
| --- | --- |
| `NOTES.md` | settled facts about the data — the overwrite, which source is authoritative for what, why |
| `DEV_PROCESS.md` | narrative log of the recovery: what the Google API actually does, bugs found, what was built |
| `src/server.py` | the app — check-in, calendar, weight chart, API |
| `scripts/` | recovery and build tooling, all dry-run by default |
| `data/` | untracked; the Takeout archive and built artifacts |

## Known traps

Every one of these was found by counting rows, not by anything raising an error.
Silent truncation is the recurring failure mode in this codebase — when a result
looks plausible, check its size against what you expected.

- **PostgREST caps a response at 1000 rows.** Page, or silently lose the tail.
- **`completedMin` excludes incomplete tasks entirely** from the Tasks API.
- **Google encodes a due date as local midnight in UTC** (07:00Z for Pacific).
  Writing `00:00Z` files the task a day early.
- **A day's tasks drop out of `tasks.list()` within ~24h** and are then reachable
  only by an id captured beforehand.
