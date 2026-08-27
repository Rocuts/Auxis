"""Plain-SQL migration runner.

Deliberately tiny: the DDL is itself a deliverable, so migrations stay
readable .sql files applied in filename order. Design points (validated
against 2026 practice):

- Runs over the DIRECT (unpooled) endpoint: transaction-mode pooling breaks
  session state, and Neon lists schema migrations first among operations
  that need a direct connection. DSN resolution prefers
  MIGRATIONS_DATABASE_URL, then DATABASE_URL_UNPOOLED, then DATABASE_URL.
- pg_advisory_lock serializes concurrent runners (safe on a direct
  connection; it would NOT be safe through a transaction-mode pooler).
- One transaction per migration file, with a lock_timeout so a stuck DDL
  cannot hold the advisory lock forever.
- A file starting with ``-- migrate:no-transaction`` runs in autocommit —
  the escape hatch CREATE INDEX CONCURRENTLY will eventually need.
- Generous connect_timeout: the first connection to a scale-to-zero Neon
  compute can take seconds.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

ADVISORY_LOCK_KEY = 0x7461785F6D696772  # "tax_migr"
NO_TX_HEADER = "-- migrate:no-transaction"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def resolve_dsn() -> str:
    for var in ("MIGRATIONS_DATABASE_URL", "DATABASE_URL_UNPOOLED", "DATABASE_URL"):
        dsn = os.environ.get(var)
        if dsn:
            return dsn
    raise SystemExit("no DSN: set MIGRATIONS_DATABASE_URL, DATABASE_URL_UNPOOLED, or DATABASE_URL")


def apply_migrations(dsn: str, directory: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in filename order; return the names applied."""
    files = sorted(p for p in directory.glob("*.sql") if p.name[0].isdigit())
    applied: list[str] = []
    with psycopg.connect(dsn, connect_timeout=30, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " filename text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
            done = {row[0] for row in rows}
            for path in files:
                if path.name in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                if sql.lstrip().startswith(NO_TX_HEADER):
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)",
                        (path.name,),
                    )
                else:
                    with conn.transaction():
                        conn.execute("SET LOCAL lock_timeout = '10s'")
                        conn.execute(sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)",
                            (path.name,),
                        )
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply pending SQL migrations.")
    parser.add_argument("--dsn", default=None, help="overrides env-derived DSN")
    parser.add_argument(
        "--dir", type=Path, default=DEFAULT_MIGRATIONS_DIR, help="migrations directory"
    )
    args = parser.parse_args(argv)
    applied = apply_migrations(args.dsn or resolve_dsn(), args.dir)
    if applied:
        for name in applied:
            print(f"applied {name}")
    else:
        print("nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
