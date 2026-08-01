# Session log — 2026-07-30 / 08-01

What happened, why, and what is still open. Companion to [`NOTES.md`](NOTES.md),
which holds the settled facts about the data itself; this file is the narrative.

Branch: `data-recovery/completion-status-audit` — 21 commits,
[PR #1](https://github.com/alexrao2000/task-tracker/pull/1), unmerged.

---

## How it started

A single deleted "Weigh yourself" occurrence. Investigating it exposed that the
Google Tasks API is far weaker than the project assumed, that the sync had been
recording incomplete data for its whole life, and that the weight history in
Google had been destroyed.

## What the Google Tasks API actually does

Established by direct probing, not documentation. Most of the session's
surprises trace back to these.

- **No recurrence data at all.** The full Task resource is `assignmentInfo,
  completed, deleted, due, etag, hidden, id, kind, links, notes, parent,
  position, selfLink, status, title, updated, webViewLink`. There is no
  recurrence field, so occurrences of a recurring task are related **only** by
  sharing a title — and the API cannot create a recurring task at all.
- **`list()` returns only the current occurrence.** Measured against a week of
  the UI: it showed 8 tasks on each of Jul 27 – Aug 2, while `list()` returned 8
  for *today* and **0 for every other day**. Future days are UI-only
  projections; past days drop out within ~24h.
- **Dropped-out records still exist** and are retrievable by `get()` *if the id
  was captured while listable*. Verified: all 6 of Jul 29's ids returned intact
  the next day. They are un-enumerable, not deleted.
- **Each day is a new record with a new id.** Yesterday's "Brush Teeth" and
  today's are different tasks.
- **`completedMin` silently excludes incomplete tasks.** With it: 47 tasks, all
  completed. Without: 61, including 14 incomplete.
- **Deletion is soft.** `tasks.delete` flips a flag; the record keeps its id,
  title, notes and due date, and `showDeleted=True` still returns it.
- **Takeout carries `task_recurrence_id`**, which the live API does not expose.

**Consequence:** the sync has roughly a 24-hour window to capture each day
*permanently*. A day of failed runs is unrecoverable, because the ids were never
recorded and nothing surfaces an error.

## The overwrite

Every "Weigh yourself" occurrence in Takeout carried the same 152.6 — 677 of 681
records — all sharing one `updated` timestamp to the microsecond:

    2026-07-29T17:19:57.420223Z

Editing one occurrence's description propagates it across the whole recurring
series, so a change meant for one day landed on every day. Attribution is
unresolved and is **not to be relitigated**. See `NOTES.md`.

It was **indiscriminate** — it did not skip blank occurrences, so days with no
weigh-in also came to read 152.6. A 152.6 in Takeout therefore carries no
information whatsoever.

I was wrong about this for a long stretch, insisting the data was fine on the
strength of the two or three records the API would show me — which happened to
be among the handful that were *not* 152.6. The user was right throughout.

## The reconciliation

Neither store was correct alone, and their flaws did not overlap:

| source | its one flaw | authoritative for |
| --- | --- | --- |
| Google Takeout | the overwrite — weights destroyed | **completion status** |
| Supabase `weight_log` | late completions never captured | **weight values** |

So: Takeout for everything, `weight_log` for the number. Originally planned the
other way round; reversed because completion status is 5,733 booleans keyed by
day *and* title, while weight is 621 scalars keyed by day — moving the simpler
payload into the richer base is lower risk.

**Result: 5,733 records.** 607 weights replaced, 13 already agreed, 42 days
blanked as genuine non-weigh-ins, 4 supplied. Distinct weight values went
**5 → 253**, spanning 145.8–187.7.

### The four supplied days

Completed weigh-ins that Supabase never captured — absent from
`task_completions` entirely, so no note text survived anywhere.

| day | value | basis |
| --- | --- | --- |
| 2026-07-18 | 152.6 | inductive |
| 2026-07-24 | 152.6 | inductive |
| 2026-07-27 | 152.6 | inductive |
| 2026-07-31 | 152.0 | reported — differs from the propagated value, so written after it |

Classifying these correctly needed a signal the user supplied: **completion
implies a recorded weight.** Verified 620/620 with zero counterexamples. That
splits the 46 uncaptured days into 42 genuine blanks and these 4.

## Bugs found and fixed

Ordered by how much damage each was doing.

1. **The sync never recorded a missed day.** `completedMin` dropped every
   incomplete task, so completion rates trended to 100%. Two-pass merged fetch.
2. **Supabase reads truncated at 1000 rows.** PostgREST caps a response; the
   unpaginated read silently cut `task_completions` off at ~4 months. This had
   been corrupting the dashboard and every export.
3. **Due dates written as midnight UTC.** Google encodes local midnight
   (07:00Z for Pacific). A literal `00:00Z` filed a task a day early — and
   because it landed where a real occurrence already sat, the dedupe *silently
   dropped one of them*. One record, two days damaged.
4. **Undated backlog items** bucketed to their `updated` timestamp, inventing
   missed days. Future-dated incomplete tasks counted as misses.
5. **Blank weight chart.** `draw()` no-ops at zero canvas size and only window
   resize retriggered it, so an embedded frame sizing after load stayed blank
   forever. `ResizeObserver`.
6. **Mojibake.** Raw non-ASCII in JS with no charset declared rendered as
   `â^'28.5`. All non-ASCII is now `\u` escapes.
7. **`restore_task` couldn't see its own work** — it located tasks by scanning
   `list()`, which cannot see a just-un-deleted task. Uses `get()`.
8. **`/api/wake` returned nothing** — a duplicated regex had lost its
   `IGNORECASE` flag. Now calls the tested `parse_wake_up`.
9. **Keepalive failed silently.** It broke on Jul 31 when Supabase disabled
   legacy keys, and nothing said so — with a ~7 day pause window and a 2-day
   cadence, that was three attempts of margin. Now daily, and opens a GitHub
   issue on failure.

## What was built

**Recovery tooling** (`scripts/`) — all read-only against Google and Supabase
except `load_task_history.py`:

- `parse_takeout_status.py` — per-day completion status from a Takeout export
- `build_reconciled_export.py` — the merge; Takeout base + Supabase weights
- `reconcile_completion.py` — compares stored status against Takeout
- `export_snapshot.py`, `inspect_weights.py` — read-only diagnostics
- `restore_task.py` — un-delete / re-date a task in place; dry-run by default
- `seed_tasks.py` — re-create tasks from a snapshot; dry-run by default
- `load_task_history.py` — the one writer; upsert-only, refuses a non-empty
  table without `--replace`

**The app** (`src/server.py`, `make dev` → `localhost:8000`):

```
/  /today     daily check-in — 8 tasks, weight, wake time; day navigation
/calendar     day columns, faded = done, vibrant = missed, hover for detail
/weight       621+ weigh-ins with a trailing 7-calendar-day average
/docs         generated API docs
```

Pages fetch same-origin, so **the database key never reaches the browser** —
which also removed the need for an anon key and a public read policy.

**Supabase**: `task_history` (the reconciled record, 5,733 rows loaded) and
`recurring_tasks` (what replaces Google's recurrence — `/today` materialises
each day on load, no cron).

## Write safety

The app can now write, so the guardrails matter. All verified against the live
database, not assumed:

- **`WRITE_FLOOR = 2026-07-31`.** Everything earlier is the reconciled history
  and is refused. Applied to every path *including* `toggle`, which is
  id-addressed and so resolves the row's day first — otherwise trivially
  bypassable.
- **`ensure` is insert-only**; it cannot clobber a completion or weight already
  recorded.
- **Weights outside 50–500 lbs are rejected** as typos.
- Only `task_history`, only the fields a check-in sets.
- `build_calendar_page --live` **refuses to embed a non-anon key** in a page,
  decoding the JWT `role` claim. Verified it rejects `service_role` and
  `sb_secret_`.

## Security

A `service_role` key reached this session's transcript via `.env` file-tracking.
Rotation is **complete and verified**: legacy JWT keys are disabled project-wide
and the leaked key now returns `401`. Auth is the newer `sb_secret_…` key under
`SUPABASE_SECRET_KEY`.

**Do not re-enable legacy keys** — that is the only thing keeping the leaked one
dead. An earlier attempt at rotation actually re-*enabled* them, reviving it.

## Google Tasks

**Untouched, except one action the user ran:** `restore_task --apply`, which
un-deleted one task and re-dated it. Beyond that, 16 seeded tasks were created
and then all deleted at the user's request. The archive
`data/takeout/tasks-takeout-2026-07-31.zip` is byte-identical to the original
download (SHA-256 verified).

A full delete-and-reinsert of Google was proposed and **abandoned**: only 62
tasks are reachable via `list()`, so it would have doubled every historical day
in the UI rather than replacing it. The only way to clear history is deleting
the task list itself, which also destroys the recurrence definitions.

## Open items

| item | state |
| --- | --- |
| Hosting on Vercel | not started — needs pandas trimmed from the request path, password auth, `vercel.json` |
| **Auth before deploying** | **not started — a write-enabled app with no auth is world-writable** |
| Merge PR #1 | 21 commits, unmerged |
| Scheduled Google sync | off since 2026-07-30, deliberately |
| Streamlit dashboard | still present; intended to retire once `/wake`, `/consistency`, `/data` are ported |
| Purge `data/` from git history | untracked but still in every prior commit; the Takeout zip needs a durable home first |
| Venv `pip` script | broken shebang from a moved virtualenv — use `python -m pip` |

## Things worth not relearning

- **Verify against the data, not from memory.** Nearly every wrong turn here was
  me reasoning from an incomplete API view instead of checking.
- **Silent truncation is the recurring failure mode.** The 1000-row cap, the
  `completedMin` filter, the dedupe dropping a displaced record — none raised an
  error. All three were found by counting rows, not by seeing a failure.
- **Test on the worst case.** The first seed test used Jul 14, the last day in
  the snapshot, which surfaced the stale-tail problem immediately.
- **Dry-run defaults earned their keep**, twice — they caught a write that would
  have overwritten a real reading.
