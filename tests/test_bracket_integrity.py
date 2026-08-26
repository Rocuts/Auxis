"""Phase 1 gate: the DATABASE enforces bracket integrity, not application code.

These tests speak raw SQL on purpose — they prove the constraint holds even
against a client that bypasses the repository adapter entirely.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest
from psycopg import errors

DOC_ID = "00000000-0000-0000-0000-000000000001"


def _seed_document(db: psycopg.Connection) -> None:
    db.execute(
        "INSERT INTO documents (id, sha256, filename, byte_size) VALUES (%s, %s, 'probe.pdf', 1)",
        (DOC_ID, hashlib.sha256(b"probe").hexdigest()),
    )


def _insert_bracket(
    db: psycopg.Connection,
    *,
    lower: int,
    upper: int | None,
    filing_status: str | None = "single",
    taxpayer_class: str | None = "individual",
    tax_year: int = 2026,
) -> None:
    db.execute(
        """
        INSERT INTO records (document_id, source_page, table_id, tax_year,
            jurisdiction, record_type, filing_status, taxpayer_class,
            bracket, rate, currency, confidence)
        VALUES (%s, 1, 't1', %s, 'US-FED', 'ordinary_income_bracket',
                %s, %s, int8range(%s, %s, '[]'), 0.1, 'USD', 1.0)
        """,
        (DOC_ID, tax_year, filing_status, taxpayer_class, lower, upper),
    )


def test_overlapping_bracket_is_rejected_by_the_database(
    db: psycopg.Connection,
) -> None:
    _seed_document(db)
    _insert_bracket(db, lower=0, upper=12250)
    with pytest.raises(errors.ExclusionViolation):
        _insert_bracket(db, lower=12000, upper=49800)


def test_adjacent_and_open_ended_top_bracket_are_accepted(
    db: psycopg.Connection,
) -> None:
    _seed_document(db)
    _insert_bracket(db, lower=0, upper=12250)
    # Adjacent bracket: canonical half-open ranges make [0,12251) and
    # [12251,...) touch without overlapping.
    _insert_bracket(db, lower=12251, upper=49800)
    # Open-ended "and over" top bracket.
    _insert_bracket(db, lower=49801, upper=None)
    count = db.execute("SELECT count(*) FROM records").fetchone()
    assert count is not None and count[0] == 3


def test_negative_rate_is_accepted(db: psycopg.Connection) -> None:
    _seed_document(db)
    # Document 03 carries a legitimate negative local rate (statutory rebate);
    # the rate domain must not reject it.
    db.execute(
        """
        INSERT INTO records (document_id, source_page, table_id, jurisdiction,
            record_type, attribute_key, rate, confidence)
        VALUES (%s, 1, 'ta', 'US-NJ', 'sales_tax_rate', 'avg_local',
                -0.0003, 1.0)
        """,
        (DOC_ID,),
    )
    row = db.execute("SELECT rate FROM records WHERE attribute_key = 'avg_local'").fetchone()
    assert row is not None and float(row[0]) == -0.0003


def test_null_taxpayer_class_cannot_escape_the_constraint(
    db: psycopg.Connection,
) -> None:
    # Document 05's brackets carry no taxpayer_class; without the COALESCE in
    # the exclusion constraint every one of them would silently skip the
    # overlap check (NULL never conflicts in an exclusion constraint).
    _seed_document(db)
    _insert_bracket(db, lower=0, upper=48350, taxpayer_class=None, tax_year=2025)
    with pytest.raises(errors.ExclusionViolation):
        _insert_bracket(db, lower=40000, upper=500000, taxpayer_class=None, tax_year=2025)


def test_estate_trust_brackets_chain_without_filing_status(
    db: psycopg.Connection,
) -> None:
    # Document 01's Estates and Trusts schedule: brackets discriminated by
    # taxpayer_class alone, filing_status NULL. Before migration 0006 the
    # bracket_requires_chain CHECK rejected them outright; after it, the
    # exclusion constraint's COALESCE(filing_status, '') is load-bearing —
    # the four rows insert cleanly AND the chain still refuses overlaps.
    _seed_document(db)

    def estate_bracket(lower: int, upper: int | None) -> None:
        db.execute(
            """
            INSERT INTO records (document_id, source_page, table_id, tax_year,
                jurisdiction, record_type, filing_status, taxpayer_class,
                bracket, rate, currency, confidence)
            VALUES (%s, 1, 't2', 2026, 'US-FED', 'ordinary_income_bracket',
                    NULL, 'estate_or_trust', int8range(%s, %s, '[]'),
                    0.1, 'USD', 1.0)
            """,
            (DOC_ID, lower, upper),
        )

    estate_bracket(0, 3250)
    estate_bracket(3251, 11750)
    estate_bracket(11751, 16050)
    estate_bracket(16051, None)
    count = db.execute(
        "SELECT count(*) FROM records WHERE taxpayer_class = 'estate_or_trust'"
    ).fetchone()
    assert count is not None and count[0] == 4
    with pytest.raises(errors.ExclusionViolation):
        estate_bracket(10000, 20000)


def test_bracket_without_any_taxpayer_discriminator_is_rejected(
    db: psycopg.Connection,
) -> None:
    # The loosened CHECK still demands *some* discriminator: filing_status
    # and taxpayer_class both NULL must not slip into the chain space.
    _seed_document(db)
    with pytest.raises(errors.CheckViolation):
        _insert_bracket(db, lower=0, upper=12250, filing_status=None, taxpayer_class=None)
