"""The document pipeline, composed once for every target:

    extracted cell grid -> SchemaMapper -> validators/triage -> persist

Ports in, ports out: the router, mapper, and repository arrive as
interfaces, so the same function runs under docker-compose (pdfplumber +
tesseract + local Postgres), on Vercel (pdfplumber + vision-OCR + Neon),
and in the AWS design (Textract + Bedrock + RDS) without change.

Accounting invariant (anti-goal #8): every record the mapper produced is
either persisted (possibly flagged ``needs_review``) or lands in the
review queue with its findings; every mapper issue lands in the review
queue. Nothing is dropped between the mapper and the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from tax_tables.extraction.model import ExtractedDocument, ExtractionMethod
from tax_tables.ports.mapper import MappingIssue, MappingResult, SchemaMapper
from tax_tables.ports.repository import IngestOutcome, RecordRepository
from tax_tables.validation.validators import (
    DEFAULT_CONFIDENCE_FLOOR,
    TriageResult,
    review_queue_entry,
    triage,
)


class DocumentExtractor(Protocol):
    """What the pipeline needs from the extraction layer — satisfied by
    ``ExtractionRouter``."""

    def extract(self, pdf_bytes: bytes, *, filename: str) -> ExtractedDocument: ...


@dataclass(frozen=True)
class PipelineResult:
    """Everything one document run produced, for reporting and for tests.

    ``document_id``/``ingest`` are None on a dry run (no repository) —
    extraction, mapping, and triage still happen, nothing is persisted.
    """

    extracted: ExtractedDocument
    mapping: MappingResult
    triage: TriageResult
    document_id: UUID | None
    ingest: IngestOutcome | None
    review_entries: int


def _issue_entry(issue: MappingIssue) -> dict[str, object]:
    return {
        "source_page": issue.source_page,
        "table_id": issue.table_id,
        "row_index": issue.row_index,
        "col_index": issue.col_index,
        "raw_value": issue.raw_value,
        "reason": f"mapping: {issue.reason}",
    }


def run_document(
    pdf_bytes: bytes,
    *,
    filename: str,
    router: DocumentExtractor,
    mapper: SchemaMapper,
    repository: RecordRepository | None = None,
    confidence_floor: Decimal = DEFAULT_CONFIDENCE_FLOOR,
) -> PipelineResult:
    extracted = router.extract(pdf_bytes, filename=filename)
    mapping = mapper.map_document(extracted)
    triaged = triage(mapping.records, confidence_floor=confidence_floor)

    if repository is None:
        return PipelineResult(
            extracted=extracted,
            mapping=mapping,
            triage=triaged,
            document_id=None,
            ingest=None,
            review_entries=0,
        )

    handle = repository.register_document(
        sha256=extracted.sha256,
        filename=filename,
        byte_size=len(pdf_bytes),
        page_count=extracted.page_count,
        source_kind=("scanned" if ExtractionMethod.OCR in extracted.methods else "digital"),
    )
    outcome = repository.ingest(handle.id, triaged.persistable)
    # Every finding is queued with its reason — REJECTs explain why a record
    # is absent from the fact table, FLAGs explain why a persisted row says
    # needs_review. A flag without its why is useless to a reviewer.
    entries = [
        review_queue_entry(mapping.records[finding.record_index], finding)
        for finding in triaged.findings
    ]
    entries.extend(_issue_entry(issue) for issue in mapping.issues)
    queued = repository.queue_review(handle.id, entries) if entries else 0

    return PipelineResult(
        extracted=extracted,
        mapping=mapping,
        triage=triaged,
        document_id=handle.id,
        ingest=outcome,
        review_entries=queued,
    )
