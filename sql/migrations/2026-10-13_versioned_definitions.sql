-- Versioned task definitions, the bin, and added tasks. Additive apart from
-- replacing one uniqueness rule. Safe to run more than once.
--
-- A change to a task (optional, deadline, name) from a given day ends the
-- current row the day before and starts a new one, so past days keep the
-- settings they had. Every version of one task shares a series_id.
--
-- series_id   which task this row is a version of; null means "its own id".
-- deleted_at  set while the task is in the bin; cleared on restore.
begin;

alter table public.recurring_tasks add column if not exists series_id bigint;
alter table public.recurring_tasks add column if not exists deleted_at timestamptz;

-- One row per title was the old rule; two versions of one task share a title.
-- Found by its columns, not its name, so this works whatever it was called.
do $$
declare c record;
begin
  for c in
    select con.conname
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace ns on ns.oid = rel.relnamespace
    where ns.nspname = 'public' and rel.relname = 'recurring_tasks'
      and con.contype = 'u'
      and (select array_agg(att.attname::text order by att.attname)
           from unnest(con.conkey) k join pg_attribute att
             on att.attrelid = rel.oid and att.attnum = k) = array['list_title', 'title']
  loop
    execute format('alter table public.recurring_tasks drop constraint %I', c.conname);
  end loop;
end $$;

-- Instead: one version per task per start day. Overlap between versions is
-- prevented by the app, which is the only writer.
create unique index if not exists recurring_tasks_series_start
    on public.recurring_tasks (coalesce(series_id, id), coalesce(starts_on, '0001-01-01'::date));

grant select, insert, update, delete on public.recurring_tasks to service_role;
grant usage on sequence public.recurring_tasks_id_seq to service_role;

commit;

notify pgrst, 'reload schema';
