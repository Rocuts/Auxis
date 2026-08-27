"""The document pipeline, composed once for every target:

    extracted cell grid -> SchemaMapper -> RecordVerifier -> validators/triage
        -> persist / review queue -> Adjudicator (single pass over the queue)

Ports in, ports out: the router, mapper, verifier, adjudicator, and
repository arrive as interfaces, so the same function runs under
docker-compose (pdfplumber + tesseract + local Postgres), on Vercel
(pdfplumber + vision-OCR + Neon), and in the AWS design (Textract + Bedrock
+ RDS) without change.

Accounting invariant (anti-goal #8): every record the mapper produced is
either persisted (possibly flagged ``needs_review``) or lands in the
review queue with its findings; every mapper issue lands in the review
queue. Nothing is dropped between the mapper and the database.

The semantic-layer amendment (ADR 012) adds two bounded roles:

- The verifier's disputes enter triage as FLAG findings — a disputed record
  persists as ``needs_review`` and the dispute's reason is queued. The
  verifier never corrects anything, and a mapper/verifier disagreement is
  never settled by the models talking.
- The adjudicator runs once per open queue item, after persistence. At or
  above ``auto_resolve_threshold`` (with valid citations) the item resolves
  with its audit trail — but only when the item's record actually persisted
  (a FLAG finding): a queue row born from a triage REJECT, an ingest-side
  refusal, or a mapping issue is the only live signal that data is absent
  from the fact table, and the adjudicator cannot restore a record, so such
  items only ever receive a stored proposal. A failed adjudication — the
  model call, or the write racing a human — leaves its item open and is
  reported, never raised past the pass. Without a repository (dry run)
  there is no queue, so the adjudicator is not consulted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from tax_tables.extraction.model import ExtractedDocument, ExtractionMethod
from tax_tables.observability import conformance
from tax_tables.ports.adjudicator import (
    DEFAULT_AUTO_RESOLVE_THRESHOLD,
    Adjudication,
    Adjudicator,
    ReviewItem,
)
from tax_tables.ports.mapper import MappingCost, MappingIssue, MappingResult, SchemaMapper
from tax_tables.ports.repository import IngestOutcome, RecordRepository
from tax_tables.ports.verifier import RecordVerifier, VerificationError, VerificationResult
from tax_tables.validation.validators import (
    DEFAULT_CONFIDENCE_FLOOR,
    FLAG_RULES,
    RULE_VERIFIER_DISPUTE,
    RULE_VERIFIER_UNAVAILABLE,
    Finding,
    Severity,
    TriageResult,
    review_queue_entry,
    triage,
)


class DocumentExtractor(Protocol):
    """What the pipeline needs from the extraction layer — satisfied by
    ``ExtractionRouter``."""

    def extract(self, pdf_bytes: bytes, *, filename: str) -> ExtractedDocument: ...


@dataclass(frozen=True)
class AdjudicationOutcome:
    """What the adjudicator pass did with one queued item.

    ``auto_resolved``: threshold met, citations valid, and the item's record
    persisted — resolved with its audit trail. ``proposal_stored``: below
    threshold, citations invalid, or the item stands for absent data — the
    proposal awaits a human, item still open. ``error``: the adjudication
    call failed, or the write found the item no longer open (a human or a
    concurrent run got there first); this pass left the item untouched.
    """

    item_id: UUID
    disposition: Literal["auto_resolved", "proposal_stored", "error"]
    adjudication: Adjudication | None
    error: str | None = None
    #: Spend the failed call still incurred (a paid-for truncated or
    #: malformed response); None when the failure never got a response.
    error_cost: MappingCost | None = None


@dataclass(frozen=True)
class PipelineResult:
    """Everything one document run produced, for reporting and for tests.

    ``document_id``/``ingest`` are None on a dry run (no repository) —
    extraction, mapping, verification, and triage still happen, nothing is
    persisted and nothing is adjudicated. ``verification`` is None when no
    verifier was configured.
    """

    extracted: ExtractedDocument
    mapping: MappingResult
    triage: TriageResult
    document_id: UUID | None
    ingest: IngestOutcome | None
    review_entries: int
    verification: VerificationResult | None = None
    adjudications: list[AdjudicationOutcome] = field(default_factory=list)


def issue_entry(issue: MappingIssue) -> dict[str, object]:
    """One mapper issue as a review-queue entry. Public for the AWS
    split-step handlers, like ``dispute_findings``."""
    return {
        "source_page": issue.source_page,
        "table_id": issue.table_id,
        "row_index": issue.row_index,
        "col_index": issue.col_index,
        "raw_value": issue.raw_value,
        "reason": f"mapping: {issue.reason}",
    }


def dispute_findings(verification: VerificationResult) -> list[Finding]:
    """Verifier disputes as triage findings: same FLAG machinery, same
    review-queue entries as the module's own rules. Public because the AWS
    target's split-step handlers (tax_tables.aws.handlers) recompose the
    same pipeline stage by stage."""
    return [
        Finding(
            rule=RULE_VERIFIER_DISPUTE,
            severity=Severity.FLAG,
            detail=verdict.reason or "unspecified",
            record_index=verdict.record_index,
        )
        for verdict in verification.disputed
    ]


def unverified_findings(records: Sequence[object], reason: str) -> list[Finding]:
    """Every mapper-validated record of a document the verifier could not
    judge, flagged rather than trusted.

    The baseline run made the cost of the alternatives concrete. Document 04
    produced the run's only fully conformant mapper response — 19 records,
    zero mapping issues — and then the verifier returned a body with no
    verdicts envelope. Raising discarded all 19; persisting them clean would
    have asserted an independent confirmation that never happened. Neither is
    acceptable, so the records persist as ``needs_review`` under their own
    rule, and the review queue carries the reason.

    Silence is still never assent (ADR 012): "the verifier was unavailable" is
    a different claim from "the verifier agreed", and the two must never print
    the same.
    """
    return [
        Finding(
            rule=RULE_VERIFIER_UNAVAILABLE,
            severity=Severity.FLAG,
            detail=reason,
            record_index=index,
        )
        for index in range(len(records))
    ]


def _may_auto_resolve(item: ReviewItem) -> bool:
    """Only an item whose record actually persisted may auto-close.

    FLAG findings queue under ``"<rule>: <detail>"`` with the record in the
    fact table as ``needs_review`` — closing such an item loses nothing.
    Every other reason (the ``bracket_overlap`` REJECT in either spelling,
    ``cross_document_natural_key_conflict``, ``"mapping: ..."`` issues, or
    anything a future writer invents) stands for data ABSENT from the fact
    table, and the open row is the only live signal of that absence. The
    adjudicator cannot restore a record, so those items never auto-resolve;
    the proposal is stored for the human instead. Default-deny: an
    unrecognized reason is treated as absent data.
    """
    rule, sep, _ = item.reason.partition(": ")
    return bool(sep) and rule in FLAG_RULES


def adjudicate_open_items(
    repository: RecordRepository,
    adjudicator: Adjudicator,
    document_id: UUID,
    extracted: ExtractedDocument,
    threshold: Decimal,
) -> list[AdjudicationOutcome]:
    outcomes: list[AdjudicationOutcome] = []
    for item in repository.list_open_reviews(document_id):
        try:
            adjudication = adjudicator.adjudicate(item, extracted)
        except Exception as exc:
            # AdjudicationError, an SDK transport failure (rate limit,
            # connection loss), anything: one item's failure is reported and
            # the pass continues. Persistence already committed; a raise here
            # would discard the whole PipelineResult for data that is
            # already in the database (anti-goal #8; adversarial review).
            spent = getattr(exc, "cost", None)
            outcomes.append(
                AdjudicationOutcome(
                    item_id=item.id,
                    disposition="error",
                    adjudication=None,
                    error=f"{type(exc).__name__}: {exc}",
                    error_cost=spent if isinstance(spent, MappingCost) else None,
                )
            )
            continue
        payload = adjudication.audit_payload()
        try:
            if (
                adjudication.citations_valid
                and adjudication.confidence >= threshold
                and _may_auto_resolve(item)
            ):
                repository.resolve_review(
                    item.id,
                    resolution=payload,
                    resolved_by=f"adjudicator:{payload['engine'] or 'unknown'}",
                )
                disposition: Literal["auto_resolved", "proposal_stored"] = "auto_resolved"
            else:
                repository.propose_resolution(item.id, payload)
                disposition = "proposal_stored"
        except ValueError as exc:
            # The item stopped being open between listing and writing — a
            # human or a concurrent run resolved it first. Their audit
            # record stands; this pass reports the race and moves on.
            outcomes.append(
                AdjudicationOutcome(
                    item_id=item.id,
                    disposition="error",
                    adjudication=adjudication,
                    error=str(exc),
                )
            )
            continue
        outcomes.append(
            AdjudicationOutcome(item_id=item.id, disposition=disposition, adjudication=adjudication)
        )
    return outcomes


def run_document(
    pdf_bytes: bytes,
    *,
    filename: str,
    router: DocumentExtractor,
    mapper: SchemaMapper,
    verifier: RecordVerifier | None = None,
    repository: RecordRepository | None = None,
    adjudicator: Adjudicator | None = None,
    confidence_floor: Decimal = DEFAULT_CONFIDENCE_FLOOR,
    auto_resolve_threshold: Decimal = DEFAULT_AUTO_RESOLVE_THRESHOLD,
) -> PipelineResult:
    extracted = router.extract(pdf_bytes, filename=filename)
    mapping = mapper.map_document(extracted)
    conformance.LEDGER.record_document_records(filename, len(mapping.records))

    verification: VerificationResult | None = None
    disputes: list[Finding] = []
    if verifier is not None:
        try:
            verification = verifier.verify(extracted, mapping)
        except VerificationError as exc:
            # Contained, not fatal, and not silent: the verifier's own
            # contract failure (after its bounded retries) flags this
            # document's records instead of losing them or blessing them.
            reason = "verifier unavailable: " + " ".join(str(exc).split())[:160]
            verification = VerificationResult(verdicts=[], notes=[reason], cost=None)
            disputes = unverified_findings(mapping.records, reason)
            conformance.LEDGER.record_verification_outcome(
                verified=0, unverified=len(mapping.records)
            )
        else:
            disputes = dispute_findings(verification)
            conformance.LEDGER.record_verification_outcome(
                verified=len(mapping.records), unverified=0
            )

    triaged = triage(mapping.records, confidence_floor=confidence_floor, extra_findings=disputes)

    if repository is None:
        return PipelineResult(
            extracted=extracted,
            mapping=mapping,
            triage=triaged,
            document_id=None,
            ingest=None,
            review_entries=0,
            verification=verification,
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
    # is absent from the fact table, FLAGs (including verifier disputes)
    # explain why a persisted row says needs_review. A flag without its why
    # is useless to a reviewer.
    entries = [
        review_queue_entry(mapping.records[finding.record_index], finding)
        for finding in triaged.findings
    ]
    entries.extend(issue_entry(issue) for issue in mapping.issues)
    queued = repository.queue_review(handle.id, entries) if entries else 0

    adjudications: list[AdjudicationOutcome] = []
    if adjudicator is not None:
        adjudications = adjudicate_open_items(
            repository, adjudicator, handle.id, extracted, auto_resolve_threshold
        )

    return PipelineResult(
        extracted=extracted,
        mapping=mapping,
        triage=triaged,
        document_id=handle.id,
        ingest=outcome,
        review_entries=queued,
        verification=verification,
        adjudications=adjudications,
    )
