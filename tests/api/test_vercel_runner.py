"""The Vercel JobRunner's contract: it is a latency optimization that can
fail freely, never a dependency the 202 rests on."""

from __future__ import annotations

import urllib.error
import urllib.request
from uuid import uuid4

from tax_tables.adapters.vercel_runner import KICK_TIMEOUT_SECONDS, VercelJobRunner


def _notify_and_wait(runner: VercelJobRunner) -> None:
    """The kick runs on a daemon thread; tests join it to stay deterministic."""
    runner.notify(uuid4())
    thread = runner._last_thread
    if thread is not None:
        thread.join(timeout=5)


ENV = {
    "VERCEL_URL": "auxis-preview.vercel.app",
    "CRON_SECRET": "cron-secret",
}


class _Recorder:
    def __init__(self, raises: BaseException | None = None) -> None:
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []
        self._raises = raises

    def __call__(self, request: urllib.request.Request, timeout: float) -> None:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self._raises is not None:
            raise self._raises


class TestTheKick:
    def test_posts_an_authenticated_sweep_to_its_own_deployment(self) -> None:
        recorder = _Recorder()
        _notify_and_wait(VercelJobRunner(env=ENV, send=recorder))
        (request,) = recorder.requests
        assert request.full_url == "https://auxis-preview.vercel.app/internal/sweep?limit=1"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer cron-secret"
        assert recorder.timeouts == [KICK_TIMEOUT_SECONDS]

    def test_https_is_forced_on_the_platform_host(self) -> None:
        """The bearer token must never cross the wire in clear."""
        recorder = _Recorder()
        _notify_and_wait(VercelJobRunner(env=ENV, send=recorder))
        assert recorder.requests[0].full_url.startswith("https://")

    def test_explicit_base_url_wins(self) -> None:
        recorder = _Recorder()
        env = {**ENV, "SELF_BASE_URL": "http://localhost:8000/"}
        _notify_and_wait(VercelJobRunner(env=env, send=recorder))
        assert recorder.requests[0].full_url == "http://localhost:8000/internal/sweep?limit=1"

    def test_carries_the_protection_bypass_when_configured(self) -> None:
        """Deployment Protection blocks a preview self-call at the platform
        edge, before the handler's own bearer check ever runs."""
        recorder = _Recorder()
        env = {**ENV, "VERCEL_AUTOMATION_BYPASS_SECRET": "bypass-token"}
        _notify_and_wait(VercelJobRunner(env=env, send=recorder))
        assert recorder.requests[0].get_header("X-vercel-protection-bypass") == "bypass-token"

    def test_no_bypass_header_when_unset(self) -> None:
        recorder = _Recorder()
        _notify_and_wait(VercelJobRunner(env=ENV, send=recorder))
        assert recorder.requests[0].get_header("X-vercel-protection-bypass") is None


class TestItNeverCosts:
    """Every one of these would otherwise turn a valid upload into a 500."""

    def test_a_timeout_is_expected_and_swallowed(self) -> None:
        runner = VercelJobRunner(env=ENV, send=_Recorder(raises=TimeoutError("read timed out")))
        _notify_and_wait(runner)  # must not raise

    def test_a_connection_error_is_swallowed(self) -> None:
        runner = VercelJobRunner(env=ENV, send=_Recorder(raises=urllib.error.URLError("no route")))
        _notify_and_wait(runner)

    def test_an_http_error_is_swallowed(self) -> None:
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        _notify_and_wait(VercelJobRunner(env=ENV, send=_Recorder(raises=error)))

    def test_an_unexpected_exception_is_swallowed(self) -> None:
        _notify_and_wait(VercelJobRunner(env=ENV, send=_Recorder(raises=RuntimeError("boom"))))

    def test_unconfigured_is_a_silent_no_op(self) -> None:
        recorder = _Recorder()
        _notify_and_wait(VercelJobRunner(env={}, send=recorder))
        _notify_and_wait(VercelJobRunner(env={"VERCEL_URL": "x"}, send=recorder))
        _notify_and_wait(VercelJobRunner(env={"CRON_SECRET": "y"}, send=recorder))
        assert recorder.requests == []


class TestPortConformance:
    def test_it_satisfies_the_job_runner_protocol(self) -> None:
        from tax_tables.ports.jobs import JobRunner

        runner: JobRunner = VercelJobRunner(env=ENV, send=_Recorder())
        assert callable(runner.notify)

    def test_the_kick_thread_is_a_daemon(self) -> None:
        """It must never hold the process open. On a serverless target the
        platform freezes or reclaims the sandbox after the response, and a
        non-daemon thread would be a shutdown hazard for the local and
        docker-compose targets too."""
        runner = VercelJobRunner(env=ENV, send=_Recorder())
        runner.notify(uuid4())
        thread = runner._last_thread
        assert thread is not None and thread.daemon is True


class TestItNeverBlocks:
    """The port contract is "must not block". A synchronous kick broke it
    twice: it added its whole timeout to every 202 (measured at 3.01s against
    a local server), and on a single-worker server it self-deadlocked — the
    sweep could not be served until the upload handler waiting on it
    returned."""

    def test_notify_returns_before_the_request_completes(self) -> None:
        import threading
        import time

        released = threading.Event()

        def _slow(request: urllib.request.Request, timeout: float) -> None:
            released.wait(timeout=5)

        runner = VercelJobRunner(env=ENV, send=_slow)
        started = time.perf_counter()
        runner.notify(uuid4())
        elapsed = time.perf_counter() - started
        released.set()
        assert elapsed < 0.5, f"notify blocked for {elapsed:.2f}s"
