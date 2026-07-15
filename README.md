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
- A Streamlit dashboard charts wake-up time and weight.

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
                                      >--> parsers --> store (SQLite or Supabase) --> Streamlit dashboard
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

### 5. Dashboard

```bash
make dashboard
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
make keepalive                     # Ping Supabase so the free project stays active
make dashboard                     # Launch Streamlit charts
make install-scheduler             # Install local launchd job (optional)
make help                          # List targets
```

## Notes / customization

- The metric tasks are matched by title via `WAKE_UP_TASK_TITLE` and `WEIGHT_TASK_TITLE` in `.env`.
- Limit which lists are synced with `TASK_LISTS=Daily,Health` (empty = all lists).
- Timezone for day-bucketing is resolved in this order: `LOCAL_TIMEZONE` override →
  your **Google Calendar timezone setting** → machine-detected → UTC. The Calendar
  setting is authoritative and keeps bucketing correct even on UTC CI runners.
- The dashboard has a "Sync now" button to pull fresh data from Google Tasks on demand.
- `credentials.json`, `token.json`, and `.env` are gitignored.
