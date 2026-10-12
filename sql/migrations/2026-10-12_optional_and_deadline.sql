-- Optional tasks and per-task deadlines. Additive: two columns on the task
-- definitions, nothing on task_history. Safe to run more than once.
--
-- optional          an optional task is shown blue and an unticked one is never
--                   counted as a miss.
-- deadline_minutes  minutes after midnight of the task's own day. Values from
--                   1440 up reach into the following day: 1530 is 1:30am the
--                   next night. Null means no deadline.
begin;

alter table public.recurring_tasks
    add column if not exists optional boolean not null default false;

alter table public.recurring_tasks
    add column if not exists deadline_minutes integer;

alter table public.recurring_tasks
    drop constraint if exists recurring_tasks_deadline_range;
alter table public.recurring_tasks
    add constraint recurring_tasks_deadline_range
    check (deadline_minutes is null or deadline_minutes between 0 and 2879);

commit;

-- PostgREST caches the schema; without this the new columns are invisible to
-- the API for a while after the migration.
notify pgrst, 'reload schema';
