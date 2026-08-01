-- Supabase / Postgres schema for cal-task-sync.
-- Apply via the Supabase SQL editor, the CLI, or the Supabase MCP.

create table if not exists public.task_completions (
    task_id      text primary key,
    list_id      text,
    list_title   text,
    title        text,
    status       text,
    day          date,
    due          text,
    completed_at timestamptz,
    notes        text,
    updated_at   timestamptz,
    is_completed boolean,
    source       text default 'tasks',
    synced_at    timestamptz default now()
);

alter table public.task_completions add column if not exists source text default 'tasks';

create index if not exists idx_task_completions_day on public.task_completions(day);
create index if not exists idx_task_completions_title on public.task_completions(title);

create table if not exists public.wake_up_log (
    day            date primary key,
    wake_time      time,
    wake_minutes   integer,
    raw_notes      text,
    source_task_id text,
    synced_at      timestamptz default now()
);

-- Single-row heartbeat used by the keepalive job to keep the free-tier project
-- from pausing (any query counts as activity).
create table if not exists public.heartbeat (
    id        integer primary key default 1,
    last_ping timestamptz default now(),
    note      text
);

create table if not exists public.weight_log (
    day              date primary key,
    weight_lbs       numeric,
    start_date       text,
    start_weight_lbs numeric,
    raw_notes        text,
    source_task_id   text,
    synced_at        timestamptz default now()
);

-- The reconciled record: Google Takeout for every task and its completion
-- status, Supabase weight_log for weight values. Built by
-- scripts/build_reconciled_export.py and loaded by scripts/load_task_history.py.
--
-- Deliberately a separate table from task_completions. That table is the raw
-- sync output and remains the historical snapshot the reconciliation was built
-- from; nothing should overwrite it. This one is the merged result, and is
-- intended to become the primary table if task tracking moves off Google.
create table if not exists public.task_history (
    id             bigserial primary key,
    day            date not null,
    list_title     text not null,
    title          text not null,
    status         text,
    is_completed   boolean,
    completed_at   timestamptz,
    notes          text,
    recurrence_id  text,
    weight_lbs     numeric,
    weight_source  text,
    takeout_weight numeric,
    imported_at    timestamptz default now(),
    -- The natural key the reconciled export already dedupes on, so loading is
    -- idempotent: a re-run updates in place instead of duplicating.
    unique (day, list_title, title)
);

create index if not exists idx_task_history_day on public.task_history(day);
create index if not exists idx_task_history_title on public.task_history(title);

-- Lock down PostgREST for anon/authenticated. The sync and keepalive jobs use
-- the service_role key, which bypasses RLS.
alter table public.task_completions enable row level security;
alter table public.wake_up_log enable row level security;
alter table public.weight_log enable row level security;
alter table public.heartbeat enable row level security;
alter table public.task_history enable row level security;
