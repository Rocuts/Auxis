"""Re-ingesting a document must converge on ONE record set, whatever the
mapper emitted last time.

Measured on production, gate 3.5-LIVE, 2026-08-27. Document 01 was ingested
twice — once by the original worker, once by the lease reclaim after the
platform killed it. It ended with **60 records where 32 are correct**: 28
brackets present twice, identical in page, table, bounds, rate, confidence
and provenance, differing only in ``taxpayer_class`` (``null`` on the first
run, ``"individual"`` on the second).

Nothing was broken in the constraint. ``records_natural_key`` includes
``taxpayer_class``, so the two rows are genuinely distinct keys and the
upsert had nothing to collide with. What was broken is the *assumption*
underneath it, written into migration 0003 and into
``tests/test_conflict_policy``'s docstring: that re-ingesting a document
upserts rather than duplicates. **That holds only while the mapper is
deterministic, and ADR 014 §8 measured that ours is not** — ``taxpayer_class``
null-vs-``individual`` is one of the four convention fields it named as
varying between runs.

So idempotence cannot be delegated to a row-level key whose columns the
mapper is free to vary. It has to be document-scoped: this document's prior
records are replaced by this run's set, atomically.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import psycopg
import pytest

from tax_tables.adapters import postgres as postgres_module
from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.domain.records import CanonicalRecord, FilingStatus, RecordType
from tax_tables.ports.repository import DocumentHandle
from tests.conftest import TEST_DSN


def _sha(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _register(repo: PostgresRecordRepository, name: str) -> DocumentHandle:
    return repo.register_document(sha256=_sha(name), filename=name, byte_size=1)


def _bracket(lower: int, upper: int, rate: str, *, taxpayer_class: str | None) -> CanonicalRecord:
    """One ordinary-income bracket. ``taxpayer_class`` is the field the live
    mapper varied between two runs of the same document."""
    return CanonicalRecord(
        source_page=1,
        table_id="p1_t0",
        record_type=RecordType.ORDINARY_INCOME_BRACKET,
        jurisdiction="US-FED",
        filing_status=FilingStatus.SINGLE,
        taxpayer_class=taxpayer_class,
        tax_year=2026,
        lower_bound=lower,
        upper_bound=upper,
        rate=Decimal(rate),
        currency="USD",
        confidence=Decimal(1),
    )


def _count(document_id: object) -> int:
    with psycopg.connect(TEST_DSN) as conn, conn.transaction():
        row = conn.execute(
            "SELECT count(*) FROM records WHERE document_id = %s", (document_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


class TestStochasticReingest:
    def test_reingest_replaces_rather_than_accumulates(self, db: psycopg.Connection) -> None:
        """The production regression, reduced to its smallest form: the same
        document mapped twice, the second run differing only in the one field
        the live mapper actually varied."""
        with PostgresRecordRepository(TEST_DSN) as repo:
            doc = _register(repo, "stochastic.pdf")
            first = repo.ingest(doc.id, [_bracket(0, 11925, "0.10", taxpayer_class=None)])
            assert first.persisted == 1
            assert _count(doc.id) == 1

            second = repo.ingest(doc.id, [_bracket(0, 11925, "0.10", taxpayer_class="individual")])
            assert second.persisted == 1

        # Before the fix this was 2: two genuinely distinct natural keys for
        # one real-world bracket, and the live URL served both.
        assert _count(doc.id) == 1

    def test_reingest_with_fewer_records_drops_the_stale_ones(self, db: psycopg.Connection) -> None:
        """A run that emits less than the previous run must not leave the
        previous run's extra records behind — that is the same accumulation
        bug wearing a different hat, and it silently inflates any count taken
        off the live URL."""
        with PostgresRecordRepository(TEST_DSN) as repo:
            doc = _register(repo, "shrinking.pdf")
            repo.ingest(
                doc.id,
                [
                    _bracket(0, 11925, "0.10", taxpayer_class=None),
                    _bracket(11926, 48475, "0.12", taxpayer_class=None),
                ],
            )
            assert _count(doc.id) == 2
            repo.ingest(doc.id, [_bracket(0, 11925, "0.10", taxpayer_class=None)])
        assert _count(doc.id) == 1

    def test_another_documents_records_are_untouched(self, db: psycopg.Connection) -> None:
        """The delete is document-scoped. Cross-document supersession and the
        cross-document natural-key conflict policy are a different mechanism
        and must not be disturbed by re-ingesting a neighbour."""
        with PostgresRecordRepository(TEST_DSN) as repo:
            keeper = _register(repo, "keeper.pdf")
            repo.ingest(keeper.id, [_bracket(200000, 300000, "0.35", taxpayer_class=None)])

            churner = _register(repo, "churner.pdf")
            repo.ingest(churner.id, [_bracket(0, 11925, "0.10", taxpayer_class=None)])
            repo.ingest(churner.id, [_bracket(0, 11925, "0.10", taxpayer_class="individual")])

        assert _count(keeper.id) == 1
        assert _count(churner.id) == 1

    def test_a_failed_reingest_leaves_the_previous_set_intact(
        self, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete-then-insert is only safe if it is ONE transaction. A worker
        killed between the two would otherwise leave the document with no
        records at all — trading duplicated data for lost data, which is the
        worse of the two (anti-goal #8)."""
        with PostgresRecordRepository(TEST_DSN) as repo:
            doc = _register(repo, "atomic.pdf")
            repo.ingest(doc.id, [_bracket(0, 11925, "0.10", taxpayer_class=None)])
            assert _count(doc.id) == 1

            calls = {"n": 0}
            real = postgres_module._record_params

            def explode(*args: object, **kwargs: object) -> object:
                calls["n"] += 1
                if calls["n"] > 1:
                    raise RuntimeError("worker killed mid-persist")
                return real(*args, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(postgres_module, "_record_params", explode)

            with pytest.raises(RuntimeError):
                repo.ingest(
                    doc.id,
                    [
                        _bracket(0, 11925, "0.10", taxpayer_class="individual"),
                        _bracket(11926, 48475, "0.12", taxpayer_class="individual"),
                    ],
                )

        # The rollback restored the original row: no half state, no empty
        # document.
        assert _count(doc.id) == 1
