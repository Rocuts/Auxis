"""JobRunner selection, by config, in one place.

``POST /documents`` returns 202 and hands the job to a runner. Which runner
depends on the target, and the choice must live somewhere both entrypoints
(`tax_tables.api.main` locally, `api/index.py` on Vercel) read the same way —
otherwise "adapters behind config" is true of one and not the other.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from tax_tables.ports.jobs import JobRunner, NullJobRunner


def runner_from_env(env: Mapping[str, str] | None = None) -> JobRunner:
    """``JOB_RUNNER``: ``vercel`` kicks its own sweep endpoint and lets cron
    retry; ``none`` (the default) enqueues only, which is what the local
    ``make api`` and the contract tests want — there, work is started by an
    explicit sweep rather than by a background surprise.
    """
    source = os.environ if env is None else env
    choice = (source.get("JOB_RUNNER") or "none").strip().lower()
    if choice == "vercel":
        from tax_tables.adapters.vercel_runner import VercelJobRunner

        return VercelJobRunner(env=source)
    if choice == "none":
        return NullJobRunner()
    raise ValueError(f"unknown JOB_RUNNER {choice!r}: expected vercel or none")
