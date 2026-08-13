# cal-task-sync

Pulls your daily **Google Tasks**, records every task's completion status, and parses
the special "Wake Up" and "Weigh yourself" notes into clean time-series so you can graph
your **wake-up time** and **weight** over time. Runs on a schedule so the data stays fresh.

## What it does

- Reads all (or selected) Google Tasks lists via the Tasks API (read-only).
- Stores every task instance in `task_completions` (title, status, completed time, notes).
- Parses notes into:
  - `wake_up_log` from `Wake-up Time: 7:30 AM`
  - `weight_log` from `Start Date+Weight: 8/8/24, 181.9 lbs, Current Weight: 178.2 lbs`
- Buckets each task to a day (by due/scheduled date), so a task you finish after midnight
  still counts for the day it was due (handles your nightly-update spillover).
- Idempotent upserts, so it can run as often as you like (default: hourly).
- A local app (`make dev`) for the daily check-in, plus calendar, charts and a data table.

## Safety: never write to Google Tasks directly

**Every write to the Google Tasks API goes through a script that dry-runs by
default and is reviewed before it is applied.** No exceptions.

A write meant for one occurrence propagates to the **whole recurring series**,
silently and irreversibly — this destroyed ~2 years of weight values here, 677
records overwritten in a single edit. And the API cannot detect it: history is
not enumerable, so a series cannot be compared before and after a write. A clean
read is not evidence that a write was safe.

Record data through the app (`make dev`), which writes to Supabase
`task_history` — diffable, row-addressed and recoverable. See
[`NOTES.md`](NOTES.md) for the incident and [`CLAUDE.md`](CLAUDE.md) for the
full working rules.

## Data sources (important)

Google's public APIs do **not** expose the full day-by-day history of recurring tasks:

- **Google Tasks API** (`make sync`): returns your current tasks plus a small set of
  recently completed ones. Good for keeping data fresh going forward.
- **Google Calendar API**: does **not** return Google Tasks at all, only real calendar
  events. It's therefore disabled by default (`INCLUDE_CALENDAR_EVENTS=false`).
- **Google Takeout** (`make import-takeout`): the only source with the complete history
  of every completed recurring task instance (with per-day notes). Use it once to backfill
  years of wake-up times, weights, and completion status.

Typical flow: **import Takeout once** for history, then let the **hourly sync** accumulate
new days automatically.

## Architecture

```
Google Takeout (one-time backfill) --\
                                      >--> parsers --> store (SQLite or Supabase) --> local app (make dev)
Google Tasks API (hourly, ongoing) --/
        ^
   launchd (hourly)
```

## Setup

### 1. Python environment

```bash
cd /Users/alexr/cal-task-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration

```bash
cp .env.example .env
```

`credentials.json` (your Google OAuth desktop client) is already in place. The defaults use
the local **SQLite** backend, so no other config is required to start.

To use Supabase instead, set in `.env`:

```
STORAGE_BACKEND=supabase
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
```

and apply [`sql/schema.sql`](sql/schema.sql) to your Supabase project.

### 3. First run (one-time Google consent)

```bash
make sync
```

A browser window opens once for Google consent. The token is cached in `token.json`,
so later runs refresh the access token automatically. If the refresh token is expired
or revoked (common for OAuth apps still in **Testing** mode after ~7 days), the next
`make sync` deletes the stale token and opens the auth link again. To force that:

```bash
make reauth
```

### 4. Backfill full history from Google Takeout (one-time)

The APIs only expose recent history, so backfill the rest from a Takeout export:

1. Go to https://takeout.google.com/, **Deselect all**, check only **Tasks**, and export.
2. Import the downloaded ZIP (or the inner `Tasks.json`):

```bash
make import-takeout FILE=/path/to/takeout-XXXX.zip
```

This loads every historical wake-up time, weight, and per-day completion into the same tables.

### 5. Run the app

```bash
make dev     # http://localhost:8000
```

### 6. Schedule it

Recommended: **GitHub Actions** (runs even when your Mac is off). See
[Deployment](#deployment-github-actions) below.

Optional local alternative (only runs when the Mac is awake):

```bash
make install-scheduler   # macOS launchd job
launchctl unload ~/Library/LaunchAgents/com.alexr.caltasksync.plist  # to stop
```

## Deployment (GitHub Actions)

The daily sync runs in CI twice a day (~2am and ~2pm Pacific) via
[.github/workflows/sync.yml](.github/workflows/sync.yml). This requires the
Supabase backend (CI has no persistent local file). Running in CI means the sync
does not depend on your laptop being awake or online.

### Keeping Supabase awake

Free-tier Supabase projects pause after ~7 days of inactivity. Two things prevent that:

- The twice-daily sync writes to the database, which counts as activity.
- A separate [keepalive workflow](.github/workflows/keepalive.yml) pings a
  `heartbeat` table every 2 days. It has **no Google dependency**, so even if the
  Google token expires and the sync temporarily fails, the project stays awake.

The keepalive only needs the `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` secrets.

### One-time setup

1. Move storage to Supabase:
   - Restore/unpause the Supabase project, apply [`sql/schema.sql`](sql/schema.sql).
   - In `.env` set `STORAGE_BACKEND=supabase`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
   - Migrate existing local data: `make migrate-supabase`.
2. Publish the Google OAuth app (Google Cloud Console -> OAuth consent screen ->
   Publish). Without this, the app stays in "Testing" mode and refresh tokens expire
   after ~7 days, breaking the unattended job.
3. Make sure a valid `token.json` exists locally (`make sync` once).
4. Add these GitHub repository secrets (Settings -> Secrets and variables -> Actions):

   | Secret | Value |
   | --- | --- |
   | `SUPABASE_URL` | your project URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | service role key |
   | `GOOGLE_CREDENTIALS_JSON` | full contents of `credentials.json` |
   | `GOOGLE_TOKEN_JSON` | full contents of `token.json` |

You can trigger a run manually from the Actions tab (workflow_dispatch) to verify.

## Make targets

```bash
make sync                          # Pull Google Tasks and store wake/weight data
make reauth                        # Force Google re-consent, then sync
make import-takeout FILE=take.zip  # Backfill full history from a Takeout export
make migrate-supabase              # Copy local SQLite data into Supabase
make dev                           # Run the app locally: check-in, calendar, weight chart
make keepalive                     # Ping Supabase so the free project stays active
make install-scheduler             # Install local launchd job (optional)
make help                          # List targets
```

## The app

```bash
make dev     # http://localhost:8000
```

| route | what |
| --- | --- |
| `/` | daily check-in — tap tasks, record weight and wake time, navigate days |
| `/calendar` | every day; faded = done, vibrant = missed, hover for detail |
| `/weight` | weigh-ins with a trailing 7-day average |
| `/docs` | generated API docs |

Reads and writes Supabase `task_history`, the reconciled record. Pages fetch
same-origin, so the database key stays in the server process and never reaches
the browser. Days before `WRITE_FLOOR` (`src/server.py`) are read-only.

Each new day materialises itself on load from `recurring_tasks` — that table is
what replaces Google's recurrence, which its API neither exposes nor can create.

## Notes / customization

- The metric tasks are matched by title via `WAKE_UP_TASK_TITLE` and `WEIGHT_TASK_TITLE` in `.env`.
  Matching tolerates case, stray whitespace, and emoji, so a hand-recreated instance still
  produces a datapoint.
- **Replacement instances are folded into their parent daily task.** If you delete one day's
  instance of a daily task and recreate it by hand, the new task gets a slightly different
  title, which would otherwise show up as a gap in that task's streak *plus* a sparse one-off
  in the consistency ranking. A candidate is folded only when it is similarly titled, sparse,
  and lands exclusively on days the parent series is missing — so genuinely separate tasks
  that run alongside each other (e.g. `Brush Teeth` and `Brush Teeth (Twice)`) are never
  merged. Tune with `TITLE_FOLD_THRESHOLD`, or pin/override specific pairs with
  `TITLE_ALIASES`. Folds are listed in the Consistency tab and never rewrite stored rows.
- Limit which lists are synced with `TASK_LISTS=Daily,Health` (empty = all lists).
- **Missed days are recorded, not just completions.** The Tasks API needs two passes per
  list: `completedMin` is what makes it return completed tasks from further back than the
  recent handful, but it also filters out every incomplete task, so one call can return deep
  history or current work but never both. Results are merged on task id.
- Undated incomplete tasks (backlog items like "Clean Car") get no day bucket and are left
  out of the stats — they were never scheduled, so counting them as missed would be wrong.
  An incomplete task dated in the future is likewise not yet a miss.
- Timezone for day-bucketing is resolved in this order: `LOCAL_TIMEZONE` override →
  your **Google Calendar timezone setting** → machine-detected → UTC. The Calendar
  setting is authoritative and keeps bucketing correct even on UTC CI runners.
- The dashboard has a "Sync now" button to pull fresh data from Google Tasks on demand.
- `credentials.json`, `token.json`, and `.env` are gitignored.
