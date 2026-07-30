"""Restore or re-date a Google Task in place. One-off repair tooling.

Google soft-deletes tasks: `tasks.delete` flips a `deleted` flag rather than
removing the record, and the task keeps its id, title, notes and due date. So an
accidentally deleted occurrence can be brought back as *itself* -- PATCHing
`deleted: false` -- instead of being recreated as a new task.

That distinction matters less than it appears: the Tasks API exposes no
recurrence field at all (no series id, no parent pointer), so occurrences of a
recurring task are related only by sharing a title. But restoring in place does
preserve the original id and notes, which is what you want when the deleted copy
holds the real data.

This needs read-write Tasks scope, which the sync deliberately does not have.
Consent is cached in its own token file (GOOGLE_TOKEN_RW_FILE) so the read-only
token used by the sync and CI is never upgraded in place.

Dry run (default -- shows the change, writes nothing):
    python -m scripts.restore_task --task-id ID --undelete --due 2026-07-29

Apply it:
    python -m scripts.restore_task --task-id ID --undelete --due 2026-07-29 --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import TASKS_RW_SCOPES, load_config
from src.google_auth import get_credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cal-task-sync")

FIELDS = ("title", "due", "status", "deleted", "notes")


def _service(config):
    creds = get_credentials(
        config, scopes=TASKS_RW_SCOPES, token_file=config.google_token_rw_file
    )
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _find(service, task_id: str) -> tuple[str, dict]:
    """Locate a task by id across all lists, including deleted and unindexed ones.

    Uses get() per list rather than scanning list(). A just-un-deleted task can
    be missing from list() results for a while -- Google's list index lags
    behind the record -- while get() returns it immediately. Scanning would make
    this tool unable to see the very tasks it is meant to repair.
    """
    for tl in service.tasklists().list(maxResults=100).execute().get("items", []):
        try:
            return tl["id"], service.tasks().get(tasklist=tl["id"], task=task_id).execute()
        except HttpError as exc:
            if exc.resp.status not in (400, 404):
                raise
    raise SystemExit(f"Task {task_id!r} not found in any list.")


def _summary(task: dict) -> str:
    return json.dumps({k: task.get(k) for k in FIELDS}, indent=2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Restore or re-date a Google Task in place.")
    p.add_argument("--task-id", required=True, help="Task id (from the API, not the web URL).")
    p.add_argument("--undelete", action="store_true", help="Clear the deleted flag.")
    p.add_argument("--due", help="New due date as YYYY-MM-DD.")
    p.add_argument("--notes", help="Replace the notes field.")
    p.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry run.")
    args = p.parse_args(argv)

    if args.due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.due):
        raise SystemExit(f"--due must be YYYY-MM-DD, got {args.due!r}")
    if not (args.undelete or args.due or args.notes):
        raise SystemExit("Nothing to do: pass --undelete, --due and/or --notes.")

    config = load_config()
    service = _service(config)
    list_id, before = _find(service, args.task_id)

    body: dict = {}
    if args.undelete:
        body["deleted"] = False
    if args.due:
        # Google stores due as UTC midnight of the intended local date.
        body["due"] = f"{args.due}T00:00:00.000Z"
    if args.notes:
        body["notes"] = args.notes

    print(f"\nlist={list_id}\ntask={args.task_id}\n")
    print("BEFORE:", _summary(before))
    print("\nPATCH: ", json.dumps(body, indent=2))

    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply to commit.")
        return 0

    service.tasks().patch(tasklist=list_id, task=args.task_id, body=body).execute()

    # Re-read rather than trusting the response: un-deleting via PATCH is not a
    # documented guarantee, so confirm the flag actually cleared.
    _, after = _find(service, args.task_id)
    print("\nAFTER: ", _summary(after))

    problems = []
    if args.undelete and after.get("deleted"):
        problems.append("still marked deleted")
    if args.due and (after.get("due") or "")[:10] != args.due:
        problems.append(f"due is {after.get('due')!r}, expected {args.due}")
    if problems:
        print("\nFAILED: " + "; ".join(problems))
        return 1

    print("\nOK -- task restored in place (same id, notes preserved).")
    print("Run 'make sync' to pull it into the database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
