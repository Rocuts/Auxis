"""Contract-test fixtures: the real app over the real test database.

No route handler is faked and no response is stubbed — every test drives
HTTP through the app into Postgres. Uploads are hand-rolled synthetic PDFs
(the fixture corpus is for the accuracy gate, not for API plumbing), and
seeded records are synthetic values that appear nowhere in the oracle.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.api.app import create_app
from tax_tables.api.settings import ApiSettings
from tax_tables.domain.records import CanonicalRecord, FilingStatus, RecordType
from tests.conftest import TEST_DSN, reset_database

API_KEY = "test-api-key"
CRON_SECRET = "test-cron-secret"

AUTH = {"X-API-Key": API_KEY}
BEARER = {"Authorization": f"Bearer {CRON_SECRET}"}


def make_settings(**overrides: Any) -> ApiSettings:
    values: dict[str, Any] = {
        "database_url": TEST_DSN,
        "api_key": API_KEY,
        "cron_secret": CRON_SECRET,
    }
    values.update(overrides)
    return ApiSettings(**values)


def make_client(**setting_overrides: Any) -> TestClient:
    return TestClient(create_app(make_settings(**setting_overrides)))


@pytest.fixture()
def client() -> Iterator[TestClient]:
    reset_database()
    with make_client() as test_client:
        yield test_client


def tiny_pdf(*, pages: int = 1, text: str = "Synthetic tax table") -> bytes:
    """A minimal valid text PDF (hand-rolled, like the router tests): unique
    ``text`` gives a unique sha256, ``pages`` exercises the page cap."""
    w, h = 612.0, 792.0
    objs: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids: list[str] = []
    font_num = 3 + 2 * pages
    for index in range(pages):
        page_num = 3 + 2 * index
        content_num = page_num + 1
        kids.append(f"{page_num} 0 R")
        content = f"BT /F1 12 Tf 72 {h - 72:.0f} Td ({text} p{index + 1}) Tj ET".encode("latin-1")
        objs[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.0f} {h:.0f}] "
            f"/Contents {content_num} 0 R "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>"
        ).encode()
        objs[content_num] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {pages} >>".encode()
    objs[font_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for num in sorted(objs):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def record(**overrides: Any) -> CanonicalRecord:
    """A synthetic canonical record; values unlike anything in the oracle."""
    values: dict[str, Any] = {
        "source_page": 1,
        "table_id": "p1_t0",
        "record_type": RecordType.ORDINARY_INCOME_BRACKET,
        "jurisdiction": "ZZ-API",
        "filing_status": FilingStatus.SINGLE,
        "tax_year": 2026,
        "lower_bound": 0,
        "upper_bound": 9000,
        "rate": Decimal("0.10"),
        "currency": "USD",
        "attrs": {"source_table_label": "table_api"},
        "confidence": Decimal("0.95"),
    }
    values.update(overrides)
    return CanonicalRecord(**values)


def seed_records(records: list[CanonicalRecord], *, sha_prefix: str = "aa") -> UUID:
    """Ingest synthetic records under a synthetic document; returns its id."""
    with PostgresRecordRepository(TEST_DSN) as repository:
        handle = repository.register_document(
            sha256=sha_prefix * 32, filename=f"{sha_prefix}_seed.pdf", byte_size=1
        )
        outcome = repository.ingest(handle.id, records)
        assert outcome.persisted == len(records), "seed records must not collide"
        return handle.id


def seed_reviews(
    entries: list[dict[str, Any]],
    *,
    sha_prefix: str = "cc",
    document_id: UUID | None = None,
) -> tuple[UUID, list[UUID]]:
    """Queue synthetic review items under a document; returns the document id
    and the item ids in insertion order.

    Each entry may carry ``resolve`` (a resolution mapping plus ``resolved_by``)
    or ``propose`` (a stored proposal that leaves the item open) so a test can
    build the three states the audit-trail constraint distinguishes.
    """
    with PostgresRecordRepository(TEST_DSN) as repository:
        if document_id is None:
            handle = repository.register_document(
                sha256=sha_prefix * 32, filename=f"{sha_prefix}_review.pdf", byte_size=1
            )
            document_id = handle.id
        before = _review_ids(repository, document_id)
        repository.queue_review(
            document_id,
            [
                {k: v for k, v in entry.items() if k not in ("resolve", "propose")}
                for entry in entries
            ],
        )
        created = [item for item in _review_ids(repository, document_id) if item not in before]
        for entry, item_id in zip(entries, created, strict=True):
            if "resolve" in entry:
                payload = dict(entry["resolve"])
                repository.resolve_review(
                    item_id,
                    resolution=payload.get("resolution", {"note": "resolved"}),
                    resolved_by=payload.get("resolved_by", "adjudicator:test-model"),
                )
            elif "propose" in entry:
                repository.propose_resolution(item_id, entry["propose"])
        return document_id, created


def _review_ids(repository: PostgresRecordRepository, document_id: UUID) -> list[UUID]:
    rows = repository.connection.execute(
        "SELECT id FROM review_queue WHERE document_id = %s ORDER BY created_at, id",
        (document_id,),
    ).fetchall()
    repository.connection.commit()
    return [row[0] for row in rows]
