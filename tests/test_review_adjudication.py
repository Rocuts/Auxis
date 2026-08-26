"""Review-queue adjudication against a real Postgres (ADR 012).

These tests pin the persistence half of the adjudicator loop: what
``list_open_reviews`` hands the model, what an auto-resolution writes, what
a below-threshold proposal writes, and what migration 0007's CHECK refuses.

The property that matters most here is accountability. Auto-closing a tax
data finding is acceptable ONLY because every closed item carries the
resolution, its citations, who closed it, and when — so the audit trail is
not a convention the application is trusted to follow but a constraint the
database enforces (``closed_rows_carry_audit_trail``, migrations 0007/0008),
and an already resolved item can never be silently overwritten by a second
pass.

The payload under test is built by ``Adjudication.audit_payload()`` rather
than hand-written, so a change to what the adapter proposes cannot drift
away from what the queue stores.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.ports.adjudicator import Adjudication
from tax_tables.ports.mapper import MappingCost
from tests.conftest import TEST_DSN

RESOLVER = "adjudicator:claude-opus-5"

_CELL_CITATION: dict[str, Any] = {
    "kind": "cell",
    "page": 1,
    "table_id": "p1_t0",
    "row": 1,
    "col": 1,
    "prose_index": None,
}
_PROSE_CITATION: dict[str, Any] = {
    "kind": "prose",
    "page": 1,
    "table_id": None,
    "row": None,
    "col": None,
    "prose_index": 1,
}

#: The three findings one scanned document plausibly queues: a smudged
#: cell, a mapper issue, and a prose-only finding with no coordinates at
#: all (every optional column null — the queue must carry that faithfully).
ENTRIES: list[dict[str, Any]] = [
    {
        "source_page": 1,
        "table_id": "p1_t0",
        "row_index": 1,
        "col_index": 1,
        "raw_value": "6.2%",
        "reason": "confidence_floor: cell confidence 0.42 below 0.70",
    },
    {
        "source_page": 2,
        "table_id": "p2_t0",
        "row_index": 3,
        "col_index": 0,
        "raw_value": "—",
        "reason": "mapping: dash cell ambiguous",
    },
    {"reason": "verifier_dispute: footnote rate not reflected in any record"},
]


def _adjudication(item_id: UUID, *, confidence: str = "0.97") -> Adjudication:
    return Adjudication(
        item_id=item_id,
        resolution=(
            "The footnote states the social security component is 6.2% of "
            "covered wages, matching the smudged cell; the persisted record "
            "is correct and this item is dismissible."
        ),
        citations=[_CELL_CITATION, _PROSE_CITATION],
        confidence=Decimal(confidence),
        citations_valid=True,
        cost=MappingCost(
            engine="claude-opus-5",
            api_calls=1,
            input_tokens=200,
            output_tokens=1_000,
            cache_read_tokens=8_000,
            usd=Decimal("0.03"),
            wall_seconds=2.5,
        ),
    )


def _register(repository: PostgresRecordRepository, *, sha: str = "ab") -> UUID:
    return repository.register_document(
        sha256=sha * 32, filename="05_payroll_tax_withholding_tables_TY2025.pdf", byte_size=4096
    ).id


class TestListOpenReviews:
    def test_every_column_reaches_the_adjudicator(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            assert repository.queue_review(document_id, ENTRIES) == 3
            items = repository.list_open_reviews(document_id)

        assert len(items) == 3
        assert {item.document_id for item in items} == {document_id}
        by_reason = {item.reason: item for item in items}

        smudge = by_reason["confidence_floor: cell confidence 0.42 below 0.70"]
        assert (smudge.source_page, smudge.table_id) == (1, "p1_t0")
        assert (smudge.row_index, smudge.col_index) == (1, 1)
        assert smudge.raw_value == "6.2%"

        dash = by_reason["mapping: dash cell ambiguous"]
        assert (dash.source_page, dash.row_index, dash.col_index) == (2, 3, 0)
        assert dash.raw_value == "—"  # the dash travels verbatim

        # A finding with no coordinates keeps them null rather than
        # acquiring plausible zeros.
        prose_only = by_reason["verifier_dispute: footnote rate not reflected in any record"]
        assert prose_only.source_page is None
        assert prose_only.table_id is None
        assert prose_only.row_index is None
        assert prose_only.col_index is None
        assert prose_only.raw_value is None

    def test_items_arrive_in_insertion_order(self, db: psycopg.Connection) -> None:
        # Separate calls, because created_at defaults to now() -- the
        # TRANSACTION timestamp: entries queued in one call share it and are
        # ordered by id, so only distinct transactions make "first queued,
        # first adjudicated" a testable claim.
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            for entry in ENTRIES:
                repository.queue_review(document_id, [entry])
            items = repository.list_open_reviews(document_id)

        assert [item.reason for item in items] == [str(entry["reason"]) for entry in ENTRIES]

    def test_queue_is_scoped_to_its_document(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            other_id = _register(repository, sha="cd")
            repository.queue_review(document_id, ENTRIES)

            assert len(repository.list_open_reviews(document_id)) == 3
            assert repository.list_open_reviews(other_id) == []


class TestResolveReview:
    def test_audit_trail_is_written_and_round_trips(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            repository.queue_review(document_id, ENTRIES)
            item = repository.list_open_reviews(document_id)[0]
            payload = _adjudication(item.id).audit_payload()
            repository.resolve_review(item.id, resolution=payload, resolved_by=RESOLVER)

        row = db.execute(
            "SELECT status, resolution, resolved_by, resolved_at FROM review_queue WHERE id = %s",
            (item.id,),
        ).fetchone()
        assert row is not None
        status, resolution, resolved_by, resolved_at = row
        assert status == "resolved"
        assert resolved_by == RESOLVER
        assert resolved_at is not None

        assert resolution["resolution"] == payload["resolution"]
        assert resolution["citations"] == [_CELL_CITATION, _PROSE_CITATION]
        assert resolution["citations_valid"] is True
        assert resolution["engine"] == "claude-opus-5"
        # Confidence is stored as its exact digits, never as a float: the
        # threshold that closed the item must be re-checkable later.
        assert resolution["confidence"] == "0.97"
        assert Decimal(resolution["confidence"]) == Decimal("0.97")

    def test_resolved_item_leaves_the_open_queue(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            repository.queue_review(document_id, ENTRIES)
            item = repository.list_open_reviews(document_id)[0]
            repository.resolve_review(
                item.id, resolution=_adjudication(item.id).audit_payload(), resolved_by=RESOLVER
            )
            remaining = repository.list_open_reviews(document_id)

        assert item.id not in {other.id for other in remaining}
        assert len(remaining) == 2

    def test_resolving_twice_is_refused(self, db: psycopg.Connection) -> None:
        """An audit record is never overwritten: a second pass over the same
        queue must not be able to relabel who closed an item, or when."""
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            repository.queue_review(document_id, ENTRIES)
            item = repository.list_open_reviews(document_id)[0]
            first = _adjudication(item.id).audit_payload()
            repository.resolve_review(item.id, resolution=first, resolved_by=RESOLVER)

            with pytest.raises(ValueError, match=str(item.id)):
                repository.resolve_review(
                    item.id,
                    resolution=_adjudication(item.id, confidence="0.10").audit_payload(),
                    resolved_by="adjudicator:someone-else",
                )

        row = db.execute(
            "SELECT resolved_by, resolution->>'confidence' FROM review_queue WHERE id = %s",
            (item.id,),
        ).fetchone()
        assert row == (RESOLVER, "0.97")

    def test_resolving_an_unknown_item_is_refused(self, db: psycopg.Connection) -> None:
        missing = uuid4()
        with (
            PostgresRecordRepository(TEST_DSN) as repository,
            pytest.raises(ValueError, match=str(missing)),
        ):
            repository.resolve_review(
                missing, resolution=_adjudication(missing).audit_payload(), resolved_by=RESOLVER
            )


class TestProposeResolution:
    def test_proposal_is_stored_and_the_item_stays_human(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            repository.queue_review(document_id, ENTRIES)
            item = repository.list_open_reviews(document_id)[0]
            payload = _adjudication(item.id, confidence="0.55").audit_payload()
            repository.propose_resolution(item.id, payload)
            work_list = repository.list_open_reviews(document_id)

        # Below the threshold the reviewer gets the reasoning, not a
        # decision: the item is still theirs (status stays open below) — but
        # it leaves the adjudicator's WORK list, so a re-ingest of the same
        # document never re-pays to adjudicate it (promoted review minor).
        assert item.id not in {other.id for other in work_list}
        assert len(work_list) == 2

        row = db.execute(
            "SELECT status, resolution, resolved_by, resolved_at FROM review_queue WHERE id = %s",
            (item.id,),
        ).fetchone()
        assert row is not None
        status, resolution, resolved_by, resolved_at = row
        assert status == "open"
        assert resolved_by is None
        assert resolved_at is None
        assert resolution["confidence"] == "0.55"
        assert resolution["citations"] == [_CELL_CITATION, _PROSE_CITATION]

    def test_proposal_may_be_superseded_then_resolved(self, db: psycopg.Connection) -> None:
        """A stored proposal is not an audit record, so re-proposing is
        allowed where re-resolving is not."""
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            repository.queue_review(document_id, ENTRIES)
            item = repository.list_open_reviews(document_id)[0]
            repository.propose_resolution(
                item.id, _adjudication(item.id, confidence="0.55").audit_payload()
            )
            repository.propose_resolution(
                item.id, _adjudication(item.id, confidence="0.61").audit_payload()
            )
            repository.resolve_review(
                item.id,
                resolution=_adjudication(item.id).audit_payload(),
                resolved_by="human:reviewer@example.com",
            )

        row = db.execute(
            "SELECT status, resolved_by, resolution->>'confidence' FROM review_queue WHERE id = %s",
            (item.id,),
        ).fetchone()
        assert row == ("resolved", "human:reviewer@example.com", "0.97")

    def test_proposing_on_a_resolved_item_is_refused(self, db: psycopg.Connection) -> None:
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            repository.queue_review(document_id, ENTRIES)
            item = repository.list_open_reviews(document_id)[0]
            repository.resolve_review(
                item.id, resolution=_adjudication(item.id).audit_payload(), resolved_by=RESOLVER
            )

            with pytest.raises(ValueError, match=str(item.id)):
                repository.propose_resolution(
                    item.id, _adjudication(item.id, confidence="0.10").audit_payload()
                )

        row = db.execute(
            "SELECT resolution->>'confidence' FROM review_queue WHERE id = %s", (item.id,)
        ).fetchone()
        assert row == ("0.97",)


class TestAuditTrailCheck:
    """Migrations 0007/0008 exercised directly, not through the adapter: the
    audit trail must be impossible to omit even for SQL that never goes
    through this repository — for ANY exit from 'open', resolution or
    dismissal alike."""

    def _queued_item_id(self, db: psycopg.Connection) -> UUID:
        with PostgresRecordRepository(TEST_DSN) as repository:
            document_id = _register(repository)
            repository.queue_review(document_id, ENTRIES)
            return repository.list_open_reviews(document_id)[0].id

    def test_resolved_without_audit_trail_is_unrepresentable(self, db: psycopg.Connection) -> None:
        item_id = self._queued_item_id(db)
        with pytest.raises(psycopg.errors.CheckViolation) as caught:
            db.execute("UPDATE review_queue SET status = 'resolved' WHERE id = %s", (item_id,))
        assert caught.value.diag.constraint_name == "closed_rows_carry_audit_trail"

        row = db.execute("SELECT status FROM review_queue WHERE id = %s", (item_id,)).fetchone()
        assert row == ("open",)

    def test_partial_audit_trail_is_unrepresentable(self, db: psycopg.Connection) -> None:
        item_id = self._queued_item_id(db)
        with pytest.raises(psycopg.errors.CheckViolation) as caught:
            db.execute(
                "UPDATE review_queue"
                " SET status = 'resolved', resolution = '{\"resolution\": \"fine\"}'::jsonb"
                " WHERE id = %s",
                (item_id,),
            )
        assert caught.value.diag.constraint_name == "closed_rows_carry_audit_trail"

    def test_dismissed_without_audit_trail_is_unrepresentable(self, db: psycopg.Connection) -> None:
        # 0007 guarded only 'resolved'; 0008 closed the unaudited-dismissal
        # exit (promoted review minor).
        item_id = self._queued_item_id(db)
        with pytest.raises(psycopg.errors.CheckViolation) as caught:
            db.execute("UPDATE review_queue SET status = 'dismissed' WHERE id = %s", (item_id,))
        assert caught.value.diag.constraint_name == "closed_rows_carry_audit_trail"

    def test_audited_dismissal_is_representable_without_a_resolution(
        self, db: psycopg.Connection
    ) -> None:
        """A dismissal is a judgment that no resolution payload is needed —
        who and when still are."""
        item_id = self._queued_item_id(db)
        db.execute(
            "UPDATE review_queue SET status = 'dismissed',"
            " resolved_by = 'human:reviewer@example.com', resolved_at = now()"
            " WHERE id = %s",
            (item_id,),
        )
        row = db.execute(
            "SELECT status, resolution FROM review_queue WHERE id = %s", (item_id,)
        ).fetchone()
        assert row == ("dismissed", None)

    def test_open_item_may_carry_a_proposal(self, db: psycopg.Connection) -> None:
        """The CHECK constrains resolved rows only: a stored proposal on an
        open row is exactly what a below-threshold adjudication writes."""
        item_id = self._queued_item_id(db)
        db.execute(
            'UPDATE review_queue SET resolution = \'{"confidence": "0.55"}\'::jsonb WHERE id = %s',
            (item_id,),
        )
        row = db.execute(
            "SELECT status, resolution->>'confidence' FROM review_queue WHERE id = %s", (item_id,)
        ).fetchone()
        assert row == ("open", "0.55")
