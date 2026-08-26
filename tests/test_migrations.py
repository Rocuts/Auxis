"""The migration runner applies cleanly and is idempotent."""

from __future__ import annotations

import psycopg

from tax_tables.migrate import apply_migrations
from tests.conftest import MIGRATIONS_DIR, TEST_DSN, reset_database


def test_migrations_apply_and_are_idempotent() -> None:
    reset_database()
    expected = sorted(p.name for p in MIGRATIONS_DIR.glob("0*.sql"))

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        rows = conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()
    assert [r[0] for r in rows] == expected

    # Second run: nothing pending.
    assert apply_migrations(TEST_DSN, MIGRATIONS_DIR) == []
