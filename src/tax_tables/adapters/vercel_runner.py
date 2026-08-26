"""Vercel JobRunner: kick the sweep now, let cron be the backstop.

`POST /documents` must return 202 without blocking on extraction, and Vercel
functions are request-scoped — there is no resident worker to hand the job to
(ADR 008). The cron sweep solves correctness: a scheduled request claims
`queued` jobs with `FOR UPDATE SKIP LOCKED` and runs them. What it does not
solve is *latency*: cron granularity is one minute, so an upload can sit for
up to ~60s before anything starts.

This runner closes that gap without weakening anything. On `notify` it fires
one fire-and-forget HTTP request at the deployment's own
`POST /internal/sweep`, which starts a **separate** function invocation that
does the work. The upload's own request returns 202 immediately either way.

The important property is that the kick is an **optimization, never a
dependency**. It is best-effort by construction:

- a tiny read timeout, because we deliberately do not wait for the sweep to
  finish — the invocation outlives our abandoned response;
- it runs on a daemon thread, so ``notify`` returns immediately. The port
  contract says a runner "must not block", and a synchronous kick broke it
  twice over: it added its whole timeout to every 202 (measured at 3.01s
  against a local server), and on a single-worker server it *self-deadlocked*
  — the sweep cannot be served while the upload handler waiting on it is
  still running;
- every exception swallowed and logged; the caller already earned its 202;
- unconfigured (no base URL, no secret) is a silent no-op, not an error.

On request-scoped compute the thread may not outlive the response the
platform is waiting to return. That is expected, and is exactly why the
cron backstop exists: a kick that never leaves cost nothing, and the job is
still ``queued``.

If the kick fails, is dropped, or never fires at all, the job row is still
`queued` and the next cron sweep picks it up. That is the same contract the
Step Functions runner honours on AWS: a lost notification delays work, it
never loses it.

Stdlib `urllib` on purpose: one fire-and-forget POST does not justify adding
an HTTP client to the runtime bundle (`anthropic` ships `httpx2`, not
`httpx`, so there is nothing already there to borrow).
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from uuid import UUID

logger = logging.getLogger(__name__)

#: We abandon the response on purpose — the sweep runs as its own invocation
#: and takes far longer than this. Long enough to establish the connection
#: and deliver the request, short enough never to delay the 202.
KICK_TIMEOUT_SECONDS = 3.0

#: Ask the kicked sweep for a single job: this upload's. Cron does the
#: draining; a kick that grabbed a large batch would make one upload pay the
#: latency of everyone else's backlog.
KICK_LIMIT = 1


def _default_send(request: urllib.request.Request, timeout: float) -> None:
    with urllib.request.urlopen(request, timeout=timeout):
        return None


class VercelJobRunner:
    """JobRunner for request-scoped compute: kick now, cron as the retry."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        send: Callable[[urllib.request.Request, float], None] | None = None,
    ) -> None:
        source = os.environ if env is None else env
        self._base_url = _base_url(source)
        self._secret = source.get("CRON_SECRET")
        # Deployment Protection sits in front of preview deployments, so a
        # self-call is blocked by the platform before it reaches our handler
        # unless it carries the automation bypass.
        self._bypass = source.get("VERCEL_AUTOMATION_BYPASS_SECRET")
        self._send = send if send is not None else _default_send
        # Tests join on this; nothing in production reads it.
        self._last_thread: threading.Thread | None = None

    def notify(self, job_id: UUID) -> None:
        if not self._base_url or not self._secret:
            # Nothing to kick, or nothing to authenticate with. The sweep
            # still runs on its schedule; this is a latency feature.
            logger.debug("job %s: no self-invocation configured; cron will sweep", job_id)
            return
        request = urllib.request.Request(
            url=f"{self._base_url}/internal/sweep?limit={KICK_LIMIT}",
            method="POST",
            data=b"",
            headers=self._headers(),
        )
        thread = threading.Thread(
            target=self._kick,
            args=(request, job_id),
            name=f"sweep-kick-{job_id}",
            daemon=True,
        )
        thread.start()
        self._last_thread = thread

    def _kick(self, request: urllib.request.Request, job_id: UUID) -> None:
        try:
            self._send(request, KICK_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError):
            # Including the timeout we expect: the sweep outlives our wait.
            logger.info("job %s: sweep kick not acknowledged; cron will retry", job_id)
        except Exception:
            # Nothing may escape: this runs on a thread nobody joins, where an
            # unhandled exception is a log line at best.
            logger.exception("job %s: sweep kick failed", job_id)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._secret}",
            "Content-Length": "0",
        }
        if self._bypass:
            headers["x-vercel-protection-bypass"] = self._bypass
            headers["x-vercel-set-bypass-cookie"] = "false"
        return headers


def _base_url(source: Mapping[str, str]) -> str | None:
    """The deployment's own origin.

    ``SELF_BASE_URL`` is the explicit override (local runs, tests, a custom
    domain). ``VERCEL_URL`` is the platform's per-deployment host with no
    scheme, so https is prepended — never http, which would make the bearer
    token cross the wire in clear.
    """
    explicit = source.get("SELF_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    host = source.get("VERCEL_URL")
    if host:
        return f"https://{host.rstrip('/')}"
    return None
