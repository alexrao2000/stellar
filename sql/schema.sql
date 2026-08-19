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
-- Archived recovery tooling in scripts/archive built and loaded this data.
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

-- Which habits recur daily. Google's recurrence engine is not reachable through
-- its API and is being migrated away from, so the app materialises each day's
-- occurrences itself from this table.
create table if not exists public.recurring_tasks (
    id         bigserial primary key,
    list_title text not null default 'Daily',
    title      text not null,
    active     boolean not null default true,
    sort_order integer not null default 0,
    starts_on  date,          -- null = no start bound
    ends_on    date,          -- null = ongoing
    created_at timestamptz default now(),
    unique (list_title, title)
);

alter table public.recurring_tasks enable row level security;

-- The eight dailies, seeded from what task_history shows running every day of
-- July 2026. Alphabetical, matching how the calendar orders them.
insert into public.recurring_tasks (list_title, title, sort_order, starts_on) values
    ('Daily', 'Brush Teeth',         1, '2026-08-01'),
    ('Daily', 'Brush Teeth (Twice)', 2, '2026-08-01'),
    ('Daily', 'Floss',               3, '2026-08-01'),
    ('Daily', 'Moisturize',          4, '2026-08-01'),
    ('Daily', 'Run',                 5, '2026-08-01'),
    ('Daily', 'Supplements',         6, '2026-08-01'),
    ('Daily', 'Wake Up',             7, '2026-08-01'),
    ('Daily', 'Weigh yourself',      8, '2026-08-01')
on conflict (list_title, title) do nothing;

-- Lock down PostgREST for anon/authenticated. The sync and keepalive jobs use
-- the service_role key, which bypasses RLS.
alter table public.task_completions enable row level security;
alter table public.wake_up_log enable row level security;
alter table public.weight_log enable row level security;
alter table public.heartbeat enable row level security;
alter table public.task_history enable row level security;

-- Pages now read through the authenticated server. Nothing in the browser
-- talks to PostgREST, so anon/authenticated roles get no direct table access.
drop policy if exists "anon reads task_history" on public.task_history;
revoke select on public.task_history from anon, authenticated;
revoke insert, update, delete, truncate on public.task_history from anon, authenticated;

-- --------------------------------------------------------------------------
-- The pool: non-recurring, undated work, drawn as a constellation.
--
-- Separate tables rather than a nullable task_history.day. That column is NOT
-- NULL and part of the unique key (day, list_title, title); undated tasks have
-- no honest value for it. The 38 non-recurring rows already in task_history
-- prove the point -- 32 of them share 2026-07-14, which is when the sync ran,
-- not when anything was due.
--
-- task_history is the reconciled historical record and stays exactly as it is.
-- scripts/seed_pool.py copies the still-open ones in here; it never writes back.
create table if not exists public.pool_tasks (
    id          bigserial primary key,
    title       text not null,
    notes       text,
    -- Fractions of the field, not pixels: a constellation arranged on a laptop
    -- has to still read on a phone.
    x           real not null default 0.5,
    y           real not null default 0.5,
    created_at  timestamptz default now(),
    resolved_at timestamptz                     -- null while still open
);

create index if not exists idx_pool_tasks_open
    on public.pool_tasks(resolved_at) where resolved_at is null;

-- A directed edge: before_id must happen before after_id. Cycles are rejected
-- in the API rather than here -- Postgres has no cheap way to express "this
-- insert must not close a loop", and the check needs the whole edge set.
create table if not exists public.pool_edges (
    before_id bigint not null references public.pool_tasks(id) on delete cascade,
    after_id  bigint not null references public.pool_tasks(id) on delete cascade,
    primary key (before_id, after_id),
    constraint pool_edges_no_self_link check (before_id <> after_id)
);

alter table public.pool_tasks enable row level security;
alter table public.pool_edges enable row level security;
