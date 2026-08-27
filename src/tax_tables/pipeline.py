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
  above ``auto_resolve_threshold`` (with valid, mechanically supported
  citations) the item resolves with its audit trail — but only when the
  record the item carries is actually IN the fact table, asked of the fact
  table per item rather than inferred from the item's reason. A queue row
  standing for data the database refused — a triage REJECT, an ingest-side
  refusal, a mapping issue — is the only live signal of that absence, and
  the adjudicator cannot restore a record, so such items only ever receive
  a stored proposal. A failed adjudication — the
  model call, or the write racing a human — leaves its item open and is
  reported, never raised past the pass. Without a repository (dry run)
  there is no queue, so the adjudicator is not consulted.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ValidationError

from tax_tables.domain.records import CanonicalRecord
from tax_tables.extraction.model import ExtractedDocument, ExtractionMethod
from tax_tables.observability import conformance
from tax_tables.ports.adjudicator import (
    DEFAULT_AUTO_RESOLVE_THRESHOLD,
    Adjudication,
    Adjudicator,
    ReviewItem,
    resolution_is_supported,
)
from tax_tables.ports.mapper import MappingCost, MappingIssue, MappingResult, SchemaMapper
from tax_tables.ports.repository import IngestOutcome, RecordRepository
from tax_tables.ports.verifier import RecordVerifier, VerificationError, VerificationResult
from tax_tables.validation.validators import (
    AUTO_RESOLVABLE_RULES,
    DEFAULT_CONFIDENCE_FLOOR,
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

    ``auto_resolved``: threshold met, citations valid, the resolution's
    figures mechanically supported by those citations, and the item's record
    confirmed present in the fact table — resolved with its audit trail.
    ``proposal_stored``: any one of those unmet, including an item whose
    record the database never accepted — the proposal awaits a human, item
    still open. ``error``: the adjudication
    call failed, or the write found the item no longer open (a human or a
    concurrent run got there first); this pass left the item untouched.
    ``deadline_exceeded``: the pass ran out of its wall-clock budget before
    reaching this item. The item is untouched and still open, which is the
    documented fallback (an item the adjudicator cannot settle waits for a
    human) — but it is REPORTED rather than silently skipped, because a
    queue item that quietly vanished from the report would be exactly the
    invisible loss anti-goal #8 forbids.
    """

    item_id: UUID
    disposition: Literal["auto_resolved", "proposal_stored", "error", "deadline_exceeded"]
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


def _may_auto_resolve(item: ReviewItem, repository: RecordRepository) -> bool:
    """Only an item whose record is IN THE FACT TABLE may auto-close.

    Two independent gates, both of which must pass.

    **Presence.** Closing a queue row whose record is in the table loses
    nothing: the record is still there to be re-examined. Closing a row
    whose record is ABSENT destroys the only live signal of the loss, and
    the adjudicator cannot restore a record (anti-goal #8). So the question
    is asked of the fact table itself, per item, via
    ``RecordRepository.record_present``.

    It used to be inferred from the reason prefix — FLAG rules eligible,
    everything else denied — and that proxy was false in two reachable
    ways, both of which the fixture-05 run exhibited:

    1. **A record can collect a REJECT and a FLAG at once.** Triage runs
       every rule over every record, so a bracket-overlap REJECT and a
       ``confidence_floor`` FLAG on one record queue one row each. Triage
       persisted nothing, yet the FLAG row passed the old gate. No
       enumeration of *reasons* can be sound, because the reasons are
       per-finding and persistence is per-record.
    2. **Triage's ``persistable`` is a proposal, not an outcome.** A
       FLAG-only record still meets the exclusion constraint and the
       natural key at ``ingest``, and can be refused there — which is
       precisely what happened to document 05's four "Over $X" brackets.
       Their FLAG rows described a record the database had rejected.

    **Rule name.** ADR 014 §8's gate 1 is retained on top: verifier-born
    items stay human-only whether or not their record persisted, because a
    third model agreeing with the second is correlation, not corroboration.

    Default-deny throughout: an unrecognized reason, a row carrying no
    record at all (a ``"mapping: ..."`` issue, whose ``raw_value`` is a cell
    string), or a ``raw_value`` that will not parse are all treated as
    absent data.
    """
    rule, sep, _ = item.reason.partition(": ")
    if not sep or rule not in AUTO_RESOLVABLE_RULES:
        return False
    if item.raw_value is None:
        return False
    try:
        record = CanonicalRecord.model_validate_json(item.raw_value)
    except ValidationError:
        # A mapping issue's raw_value is the offending CELL, not a record.
        # Nothing to look up, so nothing to close.
        return False
    return repository.record_present(item.document_id, record)


def adjudicate_open_items(
    repository: RecordRepository,
    adjudicator: Adjudicator,
    document_id: UUID,
    extracted: ExtractedDocument,
    threshold: Decimal,
    budget_seconds: float | None = None,
) -> list[AdjudicationOutcome]:
    """Adjudicate this document's open queue items, within a wall-clock budget.

    **Why there is a budget at all.** This pass runs after records are already
    persisted, and it is optional by design: an item it cannot settle waits
    for a human. But its cost is unbounded in TIME — one call may spend the
    adapter's request timeout, and the SDK retries it — so on request-scoped
    compute a slow queue can consume the whole function budget and the job
    never reaches a terminal state at all. Measured on production 2026-08-27:
    document 01 persisted its records at ~360 s and then spent the rest of a
    1800 s invocation in adjudication without finishing, twice.

    Trading a *resolved queue item* for a *finished job* is the right trade
    every time: the item was always allowed to wait for a human, whereas an
    unfinished job loses the whole run's bookkeeping.
    """
    outcomes: list[AdjudicationOutcome] = []
    started = time.monotonic()
    items = repository.list_open_reviews(document_id)
    for index, item in enumerate(items):
        if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
            outcomes.extend(
                AdjudicationOutcome(
                    item_id=remaining.id,
                    disposition="deadline_exceeded",
                    adjudication=None,
                    error=(
                        f"adjudication budget of {budget_seconds:.0f}s exhausted before "
                        f"this item; {len(items) - index} item(s) left open for a human"
                    ),
                )
                for remaining in items[index:]
            )
            break
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
                and _may_auto_resolve(item, repository)
                # citations_valid proves the cited cells EXIST; this proves
                # they carry the figures the resolution asserts. Fail-closed:
                # a resolution the evidence does not mechanically support goes
                # to a human, however confident the model was.
                and resolution_is_supported(
                    adjudication.resolution, adjudication.citations, extracted
                )
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
    adjudication_budget_seconds: float | None = None,
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
            # A body that arrived and broke the contract was paid for; only a
            # transport failure that never answered is free.
            spent = getattr(exc, "cost", None)
            verification = VerificationResult(
                verdicts=[],
                notes=[reason],
                cost=spent if isinstance(spent, MappingCost) else None,
            )
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
            repository,
            adjudicator,
            handle.id,
            extracted,
            auto_resolve_threshold,
            budget_seconds=adjudication_budget_seconds,
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
