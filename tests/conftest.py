"""Shared fixtures: a real Postgres per test, migrated from scratch.

Tests run against the docker-compose Postgres 18 (make db-up) locally and a
service container in CI. TEST_DATABASE_URL overrides the default DSN.

``reset_database`` DROPs the public schema, and during the Phase 2b fan-out it
did exactly that to a live pipeline run: the fan-out was pointed at this same
default DSN, and a concurrent ``make check`` wiped its documents mid-flight.
The two guards below make that class of accident unrepresentable rather than
merely regretted — a sentinel file that a long-running job creates, and a
separate database for such jobs to use.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from tax_tables.migrate import apply_migrations

#: The unit-test database. Deliberately NOT the database a pipeline fan-out
#: should use: see PIPELINE_DSN below and the module docstring.
TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://tax:tax@localhost:5433/tax_test")
#: Where a real pipeline run (pipeline_report, the fan-out, docker-compose)
#: writes. Never dropped by the test suite.
PIPELINE_DSN = "postgresql://tax:tax@localhost:5433/tax"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

#: A long-running pipeline job creates this file for its lifetime. While it
#: exists, nothing in the test suite may drop a schema.
FANOUT_SENTINEL = Path(
    os.environ.get("FANOUT_SENTINEL", str(Path(__file__).resolve().parents[1] / ".fanout-active"))
)


class DatabaseInUseError(RuntimeError):
    """A destructive test action was attempted while a pipeline run holds the
    database. Loud by design: the alternative is silently destroying someone
    else's data, which is what happened before this existed."""


def _refuse_if_in_use(dsn: str) -> None:
    if FANOUT_SENTINEL.exists():
        raise DatabaseInUseError(
            f"refusing to drop {dsn}: a pipeline run holds the database "
            f"({FANOUT_SENTINEL} exists). Wait for it, or remove the sentinel "
            "if the run is dead."
        )
    if dsn == PIPELINE_DSN:
        raise DatabaseInUseError(
            f"refusing to drop {dsn}: that is the pipeline database, not the "
            f"test database. Unit tests use {TEST_DSN}."
        )


def reset_database(dsn: str = TEST_DSN) -> None:
    """Drop everything and re-apply all migrations.

    Refuses while a pipeline run holds the database, and refuses outright to
    touch the pipeline DSN.
    """
    _refuse_if_in_use(dsn)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    apply_migrations(dsn, MIGRATIONS_DIR)


@pytest.fixture()
def db() -> Iterator[psycopg.Connection]:
    reset_database()
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        yield conn
