-- Apply once before exposing the app on a public URL.
-- The FastAPI server reads with its secret key after authenticating the user;
-- browser roles therefore need no direct access to task data.
begin;

drop policy if exists "anon reads task_history" on public.task_history;
revoke select, insert, update, delete, truncate
    on public.task_history from anon, authenticated;

commit;
