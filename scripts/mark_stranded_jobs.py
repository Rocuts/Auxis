#!/usr/bin/env python3
"""Mark the gate 3.5-LIVE stranded jobs as failed. MARK, never delete.

Five production jobs were left in `running` on 2026-08-27 when the Vercel
platform killed their workers at the then-configured 300 s `maxDuration`.
Nothing rewrote those rows, because the processes that would have written
them were gone, and the cron sweep of the day selected only `queued` — so it
could not see them (fixed since: `service.jobs.sweep_pending` now honours a
lease/visibility timeout).

This script closes those five rows to a terminal state so that:

  * the failure is recorded ON the row, where `GET /jobs/{id}` shows it,
    rather than only in a log; and
  * the documents become re-ingestable — a `running` job reads as live, so
    the sha256 natural key hands back the stranded job and refuses a fresh
    one.

**The rows are evidence and are never deleted.** The error payload names the
gate, so anyone reading the table later can find out what happened without
this file.

Idempotent and narrow by construction: it targets five literal ids, touches
a row only while it is still `running`, and reports exactly what it changed.

Usage (the DSN is read from the environment and never printed):

    DATABASE_URL='postgresql://...' uv run python scripts/mark_stranded_jobs.py
    DATABASE_URL='postgresql://...' uv run python scripts/mark_stranded_jobs.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.types.json import Jsonb

#: The five jobs from the gate 3.5-LIVE concurrent seed, by id.
STRANDED_JOB_IDS = (
    "019b3bb7-5256-4429-a02e-71451b6e5547",
    "18168a16-afe2-4824-9da3-04f2d2a7bb2f",
    "bf8d14bc-4d2f-4bec-8d0f-0264447f97be",
    "b76ff13a-92b6-4218-b189-b0eb78ec62e7",
    "cce94477-bf56-4164-85c2-9a3df316aad0",
)

ERROR_PAYLOAD = {
    "type": "worker_killed_maxduration",
    "error_class": "PlatformTimeout",
    "message": (
        "Worker killed at the 300s Vercel maxDuration during the gate 3.5-LIVE "
        "concurrent five-document seed on 2026-08-27. The row was stranded in "
        "'running' because sweep_pending selected only 'queued' jobs and could "
        "not reclaim it. Closed to 'failed' by an operator-authorised mutation "
        "so the loss is recorded on the row and the document is re-ingestable. "
        "Root cause fixed by the lease/visibility timeout in "
        "service.jobs.sweep_pending; maxDuration raised 300s -> 1800s."
    ),
    "gate": "3.5-LIVE",
    "marked_by": "operator-authorised mutation, scripts/mark_stranded_jobs.py",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and write nothing",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("error: DATABASE_URL is required", file=sys.stderr)
        return 2

    changed = 0
    with psycopg.connect(dsn, connect_timeout=30) as conn:
        for job_id in STRANDED_JOB_IDS:
            row = conn.execute(
                "SELECT status, attempt FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
            if row is None:
                print(f"  ?  {job_id}  not found — skipped")
                continue
            status, attempt = row
            if status != "running":
                print(f"  =  {job_id}  already '{status}' — left alone")
                continue
            if args.dry_run:
                print(f"  ~  {job_id}  running (attempt {attempt}) -> would mark failed")
                changed += 1
                continue
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', finished_at = now(), error = %s
                    WHERE id = %s AND status = 'running'
                    """,
                    (Jsonb(ERROR_PAYLOAD), job_id),
                )
            print(f"  ✓  {job_id}  running (attempt {attempt}) -> failed")
            changed += 1

    verb = "would change" if args.dry_run else "changed"
    print(f"{verb} {changed} of {len(STRANDED_JOB_IDS)} row(s); none deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
