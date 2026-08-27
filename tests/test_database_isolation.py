"""The contamination class, made unrepresentable.

During the Phase 2b fan-out a concurrent ``make check`` dropped the public
schema out from under a live pipeline run: the suite and the run shared one
DSN, and ``reset_database`` does exactly what it says. Nothing warned, and the
run's persistence data was silently worthless.

Two guards now stand in the way — a separate database for the suite, and a
sentinel a long-running job holds — and the point of both is that they fail
LOUDLY. A guard that quietly skipped the reset would trade destroyed data for
a mysteriously stateful test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import conftest
from tests.conftest import PIPELINE_DSN, TEST_DSN, DatabaseInUseError


def test_the_suite_and_the_pipeline_do_not_share_a_database() -> None:
    assert TEST_DSN != PIPELINE_DSN


def test_reset_refuses_the_pipeline_database_outright() -> None:
    """Even with no run in flight: the pipeline database is not the suite's to
    drop, and a typo in TEST_DATABASE_URL must not be enough to lose data."""
    with pytest.raises(DatabaseInUseError, match="pipeline database"):
        conftest.reset_database(PIPELINE_DSN)


def test_reset_refuses_while_a_run_holds_the_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / ".fanout-active"
    sentinel.write_text("")
    monkeypatch.setattr(conftest, "FANOUT_SENTINEL", sentinel)
    with pytest.raises(DatabaseInUseError, match="holds the database"):
        conftest.reset_database(TEST_DSN)


def test_the_guard_lifts_when_the_run_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sentinel is a lock, not a latch: removing it restores normal
    behaviour, so a finished run does not wedge the suite."""
    sentinel = tmp_path / ".fanout-active"
    monkeypatch.setattr(conftest, "FANOUT_SENTINEL", sentinel)
    assert not sentinel.exists()
    conftest.reset_database(TEST_DSN)  # does not raise
