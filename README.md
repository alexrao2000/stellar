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
- Buckets each task to a day by its **due date**, so a task you finish after midnight still
  counts for the day it was due (handles your nightly-update spillover).
- Idempotent upserts, so it can run as often as you like (default: hourly).
- A Streamlit dashboard charts wake-up time and weight.

## Architecture

```
Google Tasks API  ->  sync.py  ->  parsers  ->  store (SQLite or Supabase)  ->  Streamlit dashboard
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

### 4. Dashboard

```bash
make dashboard
```

### 5. Schedule it (hourly, macOS launchd)

```bash
make install-scheduler
```

Logs go to `logs/sync.out.log` and `logs/sync.err.log`. Stop with:

```bash
launchctl unload ~/Library/LaunchAgents/com.alexr.caltasksync.plist
```

## Make targets

```bash
make sync                 # Pull Google Tasks and store wake/weight data
make reauth               # Force Google re-consent, then sync
make dashboard            # Launch Streamlit charts
make install-scheduler    # Install hourly launchd sync job
make help                 # List targets
```

## Notes / customization

- The metric tasks are matched by title via `WAKE_UP_TASK_TITLE` and `WEIGHT_TASK_TITLE` in `.env`.
- Limit which lists are synced with `TASK_LISTS=Daily,Health` (empty = all lists).
- Set `LOCAL_TIMEZONE` for correct day bucketing of completed timestamps.
- `credentials.json`, `token.json`, and `.env` are gitignored.
