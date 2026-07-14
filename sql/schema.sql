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

create table if not exists public.weight_log (
    day              date primary key,
    weight_lbs       numeric,
    start_date       text,
    start_weight_lbs numeric,
    raw_notes        text,
    source_task_id   text,
    synced_at        timestamptz default now()
);
