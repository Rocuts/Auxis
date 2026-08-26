"""Shared fixtures: a real Postgres per test, migrated from scratch.

Tests run against the docker-compose Postgres 18 (make db-up) locally and a
service container in CI. TEST_DATABASE_URL overrides the default DSN.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from tax_tables.migrate import apply_migrations

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://tax:tax@localhost:5433/tax")
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def reset_database(dsn: str = TEST_DSN) -> None:
    """Drop everything and re-apply all migrations."""
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    apply_migrations(dsn, MIGRATIONS_DIR)


@pytest.fixture()
def db() -> Iterator[psycopg.Connection]:
    reset_database()
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        yield conn
