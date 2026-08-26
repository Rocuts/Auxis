"""JobRunner port — how an accepted upload's pipeline run gets executed.

``POST /documents`` persists the document and job rows, calls
``JobRunner.notify``, and returns 202 immediately: the runner is a hint,
never a dependency. Three adapters by target (CLAUDE.md): Step Functions
Distributed Map on AWS, Vercel Queues (fallback: the cron sweep — a no-op
runner plus ``sweep_pending`` driven by a scheduled request), and an
in-process worker pool locally. Because the sweep picks up any ``queued``
job, a lost or crashed notification delays work; it never loses it.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class JobRunner(Protocol):
    def notify(self, job_id: UUID) -> None:
        """Signal that a queued job exists. Must not block and must not
        raise past the caller: the job row is the source of truth."""
        ...


class NullJobRunner:
    """For request-scoped targets: the cron sweep does the work, uploads
    only enqueue."""

    def notify(self, job_id: UUID) -> None:
        return None
