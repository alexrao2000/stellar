"""Re-create task records in Google Tasks from a captured JSON snapshot.

Why this exists: Google drops a day's task records out of `list()` within about
24 hours. They still exist and are reachable by `get()` if you kept the id, but
the sync enumerates via `list()`, so an un-captured day is effectively invisible
to the pipeline forever.

A *newly created* task carrying a past due date and `status: completed` IS
listable (verified against the live API), so re-inserting a captured snapshot
makes that day visible to the sync again.

Input is a JSON array of task resources as returned by the Tasks API -- i.e.
what `tasks().get()` returns, with title/due/status/completed/notes.

Dry run (default, writes nothing):
    python -m scripts.seed_tasks --from snapshot.json --list-id LIST_ID

Apply:
    python -m scripts.seed_tasks --from snapshot.json --list-id LIST_ID --apply

Seeded copies are additive. This never deletes or modifies the originals: they
are the authoritative records, they hold the true completion timestamps, and
they remain reachable by id.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from googleapiclient.discovery import build

from src.config import TASKS_RW_SCOPES, load_config
from src.google_auth import get_credentials

COPIED = ("title", "due", "notes", "status", "completed")


def _service(config):
    creds = get_credentials(
        config, scopes=TASKS_RW_SCOPES, token_file=config.google_token_rw_file
    )
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _listable(service, list_id: str) -> list[dict]:
    """Every task the sync can currently enumerate in this list."""
    out, page_token = [], None
    while True:
        resp = (
            service.tasks()
            .list(tasklist=list_id, showCompleted=True, showHidden=True,
                  showDeleted=False, maxResults=100, pageToken=page_token)
            .execute()
        )
        out.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Re-create tasks from a captured snapshot.")
    p.add_argument("--from", dest="src", required=True, help="JSON array of task resources.")
    p.add_argument("--list-id", required=True, help="Target task list id.")
    p.add_argument("--apply", action="store_true", help="Actually create. Omit for a dry run.")
    args = p.parse_args(argv)

    records = json.loads(Path(args.src).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit("Input must be a JSON array of task resources.")

    config = load_config()
    service = _service(config)

    # Idempotency: a listable task with the same title+due is already visible to
    # the sync, so re-seeding it would only create a visible duplicate.
    existing = {
        (t.get("title", ""), (t.get("due") or "")[:10]) for t in _listable(service, args.list_id)
    }

    planned, skipped = [], []
    for r in records:
        key = (r.get("title", ""), (r.get("due") or "")[:10])
        (skipped if key in existing else planned).append(r)

    for r in skipped:
        print(f"  SKIP   {(r.get('due') or '')[:10]}  {r.get('title')!r} — already listable")
    for r in planned:
        body = {k: r[k] for k in COPIED if r.get(k) is not None}
        print(f"  CREATE {(r.get('due') or '')[:10]}  {r.get('title')!r}  status={body.get('status')}")

    print(f"\n{len(planned)} to create, {len(skipped)} already visible.")
    if not args.apply:
        print("Dry run -- nothing written. Re-run with --apply to commit.")
        return 0

    created = []
    for r in planned:
        body = {k: r[k] for k in COPIED if r.get(k) is not None}
        t = service.tasks().insert(tasklist=args.list_id, body=body).execute()
        created.append(t["id"])
        print(f"  created {t['id']}  {t.get('title')!r}")

    # Confirm by re-reading rather than trusting the insert responses.
    now_listable = {t["id"] for t in _listable(service, args.list_id)}
    missing = [i for i in created if i not in now_listable]
    if missing:
        print(f"\nWARNING: {len(missing)} created task(s) are not listable: {missing}")
        return 1
    print(f"\nOK -- {len(created)} seeded and confirmed listable. Run 'make sync' now:")
    print("the window before Google drops them out of list() again is ~24h.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
