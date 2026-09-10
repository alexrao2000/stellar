# Sparse Daily Task Events

## Goal

Remove the day-materialization request from the interactive path without
weakening the protections around the reconciled history. A daily task should
appear immediately from its recurrence definition. The database should gain a
row only when the user explicitly completes or undoes that task, or records a
task-specific value such as weight or wake time.

The change must preserve all existing historical data and the response shapes
consumed by the Today, Calendar, Weight, Wake, Consistency, and Data pages.

## Chosen approach

Continue using `task_history`; do not create a second event table or migrate old
rows. Each recurrence definition gains a `sparse_since` date. Rows before that
date retain their current dense-snapshot meaning. On and after that date,
`task_history` stores sparse events:

- no physical row means untouched;
- a physical row with `is_completed = true` means completed;
- a physical row with `is_completed = false` means explicitly undone;
- task-specific values are stored in a JSONB `metadata` object.

This is preferable to a new table because the unique historical record, write
floor, row-addressed mutations, server-only credentials, and all compatibility
columns remain in one place. It also avoids a bulk copy whose only purpose would
be to recreate data that already has the right natural key.

## Schema and invariants

The migration is additive:

1. Add `recurring_tasks.sparse_since date not null default current_date`.
2. Add `task_history.recurring_task_id bigint` referencing
   `recurring_tasks(id)` with delete restricted.
3. Add `task_history.metadata jsonb not null default '{}'::jsonb`.
4. Add a partial unique index on `(day, recurring_task_id)` when
   `recurring_task_id is not null`.
5. Retain the existing unique constraint on `(day, list_title, title)`.

Applying the migration stamps existing recurrence definitions with the date on
which the migration is applied. No `task_history` row is updated or deleted.
New recurrence definitions default to sparse behavior from their creation day.

The recurrence-definition row is the stable task identity. Once sparse events
exist for a definition, its title and list are not renamed in place. A rename or
schedule change ends the old definition with `ends_on` and inserts a new one.
This preserves the schedule that graphs use for historical denominators.

The existing `WRITE_FLOOR` remains authoritative. Every old and new mutation
must pass it. The browser continues to have no direct table credentials.

## Read model

The server builds a compatibility projection rather than materializing missing
rows. For each requested day it:

1. selects recurrence definitions whose `starts_on`/`ends_on` include the day;
2. selects physical `task_history` rows for that day;
3. before a definition's `sparse_since`, returns physical rows exactly as now;
4. on or after `sparse_since`, left-joins the physical event by
   `recurring_task_id`, falling back to the existing natural key during rollout;
5. emits a virtual incomplete row when no event exists.

Virtual rows have `id = null`, the stable `recurring_task_id`, the existing
legacy fields, and additive `event_state = "untouched"`. Physical sparse rows
emit `event_state = "completed"` or `"undone"`. Existing clients can ignore the
two additive fields and continue using `is_completed`.

`GET /api/day/{day}` returns expected tasks even when there are no physical
rows, so opening a day requires one day-scoped event query rather than an ensure
write followed by another read. The definition query remains process-cached.

`GET /api/task_history` and `GET /api/consistency` preserve their existing
shapes while generating expected sparse occurrences through today in the
configured application timezone. They do not generate future occurrences.
Existing physical future rows remain visible for backward compatibility.

`GET /api/weights` and `GET /api/wake` emit points only for physical events
that contain measurements. They combine legacy typed columns/notes before the
cutover with `metadata` after it and keep their current response formats.

## Write model

Add a stable-identity mutation:

`POST /api/day/{day}/task/{recurring_task_id}/toggle?done=true|false`

The endpoint validates the day, write floor, definition, and that the definition
is scheduled on that day. It atomically upserts the one sparse event by
`(day, recurring_task_id)`. Both true and false states retain a physical row, so
an explicit undo is distinguishable from never touching the task.

Weight and wake endpoints use the same upsert path. They create or update a
completed event and store respectively:

- weight: `{"weight_lbs": 148.5, "source": "recorded"}`;
- wake: `{"wake_time": "07:30"}`.

Their returned JSON is projected into the current legacy shape so the frontend
and any existing readers remain compatible. Historical rows continue using
their existing typed columns and notes.

The old numeric-ID toggle endpoint remains for existing physical rows. The old
`/ensure` and `/ensure_range` routes remain temporarily as authenticated,
validated no-op compatibility endpoints that perform no database work and
return a deprecation marker. The bundled frontend stops calling them.

## Graph behavior

### Calendar

Before `sparse_since`, Calendar renders the dense historical rows unchanged.
From `sparse_since` through today, it renders every scheduled recurrence:

- completed event: completed tile;
- undone event: missed tile;
- no event: missed tile;
- measurement event: completed tile with its projected value.

Future virtual tasks are not added to the history feed, so they cannot become a
wall of false misses.

### Consistency

The denominator is the count of dates on which the recurrence definition says
the task was expected. The numerator is completed physical events. Both undone
and untouched expected tasks count as misses; future dates are excluded. The
pre-cutover numerator and denominator remain the existing dense rows.

### Weight and wake

These series union pre-cutover legacy values with post-cutover metadata values,
deduplicate by day, and sort by day. Untouched and undone tasks produce no data
point. Existing endpoint keys and chart behavior do not change.

## Frontend behavior

Today removes `scheduleEnsure`, its timer, and its `ensured` state. The first day
read already contains virtual expected tasks. Boolean task buttons call the new
day-and-definition endpoint, while the existing optimistic desired-state queue
continues to coalesce rapid clicks. Measurement autosave continues to fire after
its short input debounce and becomes self-sufficient because the server upserts
the event directly.

The local-storage format remains compatible. A stale cached row that lacks
`recurring_task_id` is replaced by the fresh day response before it can be
mutated.

## Error handling and concurrency

- The unique indexes prevent duplicate natural-key or definition-key events.
- Event upserts are atomic; rapid toggles converge on the last requested state.
- A definition outside its active date range returns 404 and writes nothing.
- Invalid measurements return 400 and write nothing.
- PostgREST transport failures continue to surface as retryable 503 responses.
- Failed optimistic writes restore the last server-confirmed state unless a
  newer desired state is already queued.

## Migration and rollout

1. Export and count `task_history` and `recurring_tasks`; retain the export as a
   rollback artifact.
2. Apply the additive SQL migration. Record row counts again and assert that no
   historical row changed.
3. Deploy code that understands both dense and sparse rows. Existing rows and
   endpoints remain readable throughout the rollout.
4. Verify day, calendar, consistency, weight, and wake projections across the
   day before, day of, and day after each definition's `sparse_since`.
5. Switch the bundled Today page to the stable-identity writes and remove its
   ensure call in the same deployment.
6. Observe the no-op ensure endpoint for one compatibility window. Remove it in
   a later release only after confirming no supported client calls it.

Rollback is code-only: redeploy the previous application. The added columns and
indexes are harmless to old code, and no old rows were transformed. Rows written
after cutover are valid `task_history` rows and remain visible to the previous
application. No down migration deletes columns or events.

## Verification and PR evidence

Automated tests will cover:

- schema migration is additive and idempotent;
- dense days are unchanged;
- sparse days synthesize untouched tasks without writes;
- completed, undone, weight, and wake events overlay correctly;
- the write floor and definition date bounds reject writes;
- concurrent/repeated upserts do not duplicate rows;
- all graph endpoints preserve their public JSON shapes and boundary math;
- `/ensure` performs no database call;
- the frontend no longer references `/ensure` and still queues rapid toggles;
- the complete pre-existing test suite remains green.

The PR will include the migration procedure, rollback procedure, red/green test
evidence, full-suite output, and representative before/after API fixtures. It
will not deploy or apply the production migration.
