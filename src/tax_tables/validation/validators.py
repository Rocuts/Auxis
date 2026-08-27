"""Semantic validators and review-queue triage over CanonicalRecord batches.

These run after the SchemaMapper (Phase 2b) and before the repository. They
are pure functions: no database, no I/O, no clock — which is what lets the
accuracy harness and the API share exactly the same verdicts.

Two severities, and the split matters:

- ``REJECT`` mirrors something the database itself would refuse. Catching it
  here is not redundant: an exclusion-constraint violation surfaces from
  Postgres as one opaque error for a whole batch, whereas a finding names the
  offending record, its neighbour, and the overlapping interval — a far better
  provenance trail for the review queue.
- ``FLAG`` marks a record that is *representable* but doubtful. It is still
  persisted, with ``review_status`` flipped to ``needs_review``. A doubtful
  value is never dropped and never silently corrected (anti-goal #8).

Findings address records by batch index rather than by value: CanonicalRecord
is frozen but not hashable (it carries dict ``attrs``), one record can collect
several findings, and two records in a batch can be genuinely identical.

Chains follow the DDL exactly — (jurisdiction, record_type, tax_year,
filing_status, taxpayer_class, lifecycle_status), with the same COALESCE
semantics the exclusion constraint uses, so a NULL discriminator groups with
an empty-string one instead of escaping the check the way SQL NULL would.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tax_tables.domain.records import CanonicalRecord, ReviewStatus

#: Records below this mapping confidence are persisted for human review.
DEFAULT_CONFIDENCE_FLOOR = Decimal("0.7")

#: A rate is a decimal fraction (0.22 = 22%). Anything above this is almost
#: certainly a percentage that was never divided by 100 — no jurisdiction in
#: the corpus taxes above 50%.
_RATE_UPPER_PLAUSIBLE = Decimal("0.5")
#: Small negatives are legitimate: document 03 carries a statutory rebate of
#: -0.03% (-0.0003 as a fraction). Anything materially below that is not a
#: rebate, it is a parse error.
_RATE_LOWER_PLAUSIBLE = Decimal("-0.05")
#: Tolerance for the derived-column identity, in percentage points.
_DERIVED_SUM_TOLERANCE = Decimal("0.001")

#: Document 03's derived column: combined = state + average local.
_DERIVED_SUM_KEYS = ("state_rate_pct", "avg_local_rate_pct", "combined_rate_pct")

RULE_BRACKET_OVERLAP = "bracket_overlap"
RULE_BRACKET_GAP = "bracket_gap"
RULE_BRACKET_BOTTOM = "bracket_bottom"
RULE_OPEN_TOP = "open_top"
RULE_RATE_PLAUSIBILITY = "rate_plausibility"
RULE_CONFIDENCE_FLOOR = "confidence_floor"
RULE_DERIVED_SUM = "derived_sum"
#: Not a rule of this module: the independent RecordVerifier's disputes enter
#: triage as extra findings under this name (ADR 012), so a disputed record
#: rides the same FLAG machinery — persisted as needs_review, reason queued.
RULE_VERIFIER_DISPUTE = "verifier_dispute"
#: Also not a rule of this module. When the verifier cannot return a usable
#: verdict set for a whole document — its own contract failure, after its
#: retries — every mapper-validated record of that document is flagged under
#: this name rather than persisted as though verified. Silence is never
#: assent (ADR 012), and the alternative behaviours are both worse: losing
#: the document discards sound records, and persisting it clean asserts an
#: independent confirmation that never happened.
RULE_VERIFIER_UNAVAILABLE = "verifier_unavailable"

#: Rules whose findings are always FLAG severity: the record they indict IS
#: in the fact table, marked needs_review. Only review-queue items born from
#: these rules are eligible for adjudicator auto-resolution — every other
#: queue entry (the bracket_overlap REJECT, ingest-side refusals, mapping
#: issues) stands for data ABSENT from the fact table, and its open row is
#: the only live signal of that absence. The adjudicator cannot restore a
#: record, so auto-closing such an item would silence the loss (anti-goal
#: #8; found by the adversarial review of the ADR 012 diff).
FLAG_RULES = frozenset(
    {
        RULE_BRACKET_GAP,
        RULE_BRACKET_BOTTOM,
        RULE_OPEN_TOP,
        RULE_RATE_PLAUSIBILITY,
        RULE_CONFIDENCE_FLOOR,
        RULE_DERIVED_SUM,
        RULE_VERIFIER_DISPUTE,
        RULE_VERIFIER_UNAVAILABLE,
    }
)


#: Rules whose review-queue items an adjudicator may auto-close. Strictly
#: narrower than FLAG_RULES: a FLAG says the record IS in the fact table, but
#: it does not follow that a model may close the item unattended.
#:
#: The two verifier-born rules are excluded deliberately, and the exclusion was
#: earned. On document 01 the verifier raised a dispute whose asserted "actual"
#: value was simply wrong — it claimed a record held 257300 when the record
#: held 257250 — and the adjudicator then auto-resolved at 0.98 confidence
#: while repeating that false premise, in a rationale that also said the record
#: was "correct as persisted". It had the true value in front of it: the queue
#: entry carries the full record. So a dispute-born item is now default-deny
#: like a REJECT-born one: it stands for a SECOND opinion that something is
#: wrong, and a THIRD model agreeing with the second is not evidence, it is
#: correlation (the conformity risk ADR 012 names). A human closes those.
AUTO_RESOLVABLE_RULES = frozenset(FLAG_RULES - {RULE_VERIFIER_DISPUTE, RULE_VERIFIER_UNAVAILABLE})


class Severity(StrEnum):
    """REJECT: the database would refuse this row. FLAG: keep it, review it."""

    REJECT = "reject"
    FLAG = "flag"


class Finding(BaseModel):
    """One rule's verdict on one record of a batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(min_length=1)
    severity: Severity
    detail: str = Field(min_length=1)
    record_index: int = Field(ge=0)


class RejectedRecord(BaseModel):
    """A record bound for the review queue instead of the fact table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    record: CanonicalRecord
    findings: list[Finding]


class TriageResult(BaseModel):
    """Partition of an input batch. ``persistable`` and ``rejected`` together
    account for every input record: nothing is ever dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    persistable: list[CanonicalRecord]
    rejected: list[RejectedRecord]
    findings: list[Finding]

    @property
    def record_count(self) -> int:
        return len(self.persistable) + len(self.rejected)


# --------------------------------------------------------------------------
# Chain grouping
# --------------------------------------------------------------------------

ChainKey = tuple[str, str, int | None, str, str, str]


def chain_key(record: CanonicalRecord) -> ChainKey:
    """The discriminator chain a record belongs to.

    Mirrors ``no_overlapping_brackets``: filing_status and taxpayer_class are
    coalesced to '' exactly as the constraint expression does, so None and ''
    land in the same chain rather than silently escaping comparison. tax_year
    is left uncoalesced to match the DDL; every bracket record carries one
    anyway (``bracket_requires_chain``).
    """
    return (
        record.jurisdiction,
        str(record.record_type),
        record.tax_year,
        str(record.filing_status or ""),
        record.taxpayer_class or "",
        str(record.lifecycle_status),
    )


def _bracket_chains(records: Sequence[CanonicalRecord]) -> dict[ChainKey, list[int]]:
    """Batch indexes of every bracket record, grouped by chain, in batch order."""
    chains: dict[ChainKey, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        if record.is_bracket:
            chains[chain_key(record)].append(index)
    return chains


def _fmt(record: CanonicalRecord) -> str:
    upper = "and over" if record.upper_bound is None else record.upper_bound
    return f"[{record.lower_bound}, {upper}]"


def _overlaps(a: CanonicalRecord, b: CanonicalRecord) -> bool:
    """Inclusive integer overlap, an open upper bound being +infinity.

    The DDL stores ``[lo, hi]`` as the half-open ``int8range(lo, hi + 1)``;
    ``&&`` on those ranges is exactly this predicate.
    """
    a_lo, b_lo = a.lower_bound, b.lower_bound
    assert a_lo is not None and b_lo is not None  # guaranteed by is_bracket
    a_hi_ok = a.upper_bound is None or a.upper_bound >= b_lo
    b_hi_ok = b.upper_bound is None or b.upper_bound >= a_lo
    return a_hi_ok and b_hi_ok


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def check_bracket_overlap(records: Sequence[CanonicalRecord]) -> list[Finding]:
    """REJECT every bracket that overlaps one already accepted in its chain.

    Batch order decides: the first arrival holds the interval, later claimants
    are the rejects. This is the application-side mirror of the exclusion
    constraint, and it exists to name *which* record collided with which —
    Postgres reports only that the batch failed.
    """
    findings: list[Finding] = []
    for indexes in _bracket_chains(records).values():
        accepted: list[int] = []
        for index in indexes:
            conflict = next(
                (other for other in accepted if _overlaps(records[index], records[other])),
                None,
            )
            if conflict is None:
                accepted.append(index)
                continue
            findings.append(
                Finding(
                    rule=RULE_BRACKET_OVERLAP,
                    severity=Severity.REJECT,
                    detail=(
                        f"bracket {_fmt(records[index])} overlaps "
                        f"{_fmt(records[conflict])} at batch index {conflict} "
                        f"in the same chain"
                    ),
                    record_index=index,
                )
            )
    return findings


def check_bracket_gap(records: Sequence[CanonicalRecord]) -> list[Finding]:
    """FLAG both neighbours of any hole between consecutive brackets.

    Bounds are inclusive whole-currency integers, so adjacency means
    ``next.lower == prev.upper + 1``. Gap-freeness is a cross-row aggregate
    and therefore cannot be a database constraint (see the ``bracket_gaps``
    diagnostic view in migration 0004); it lives here instead.

    Only genuine holes are reported. A ``next.lower <= prev.upper`` collision
    is an overlap, which ``check_bracket_overlap`` already owns — reporting it
    twice under two names would just make the review queue noisier.
    """
    findings: list[Finding] = []
    for indexes in _bracket_chains(records).values():
        ordered = sorted(indexes, key=lambda i: (records[i].lower_bound or 0, i))
        for prev_index, next_index in pairwise(ordered):
            prev, nxt = records[prev_index], records[next_index]
            if prev.upper_bound is None:
                continue  # open-ended: an overlap, not a gap
            gap_start = prev.upper_bound + 1
            next_lower = nxt.lower_bound
            assert next_lower is not None
            if next_lower <= gap_start:
                continue
            detail = (
                f"gap [{gap_start}, {next_lower - 1}] between {_fmt(prev)} "
                f"and {_fmt(nxt)} in the same chain"
            )
            findings.extend(
                Finding(
                    rule=RULE_BRACKET_GAP,
                    severity=Severity.FLAG,
                    detail=detail,
                    record_index=index,
                )
                for index in (prev_index, next_index)
            )
    return findings


def check_bracket_bottom(records: Sequence[CanonicalRecord]) -> list[Finding]:
    """FLAG a multi-bracket chain that does not start at the domain floor.

    Bounds are ``ge=0``, so a chain whose lowest bracket starts above 0 has
    an uncovered head — the classic symptom of a first data row lost to a
    header band. ``check_bracket_gap`` walks pairs and cannot see it: the
    missing row is its own only evidence. ``check_open_top`` guards the top
    of the chain; this rule is its mirror at the bottom. A lone bracket is
    exempt — a single threshold record legitimately starts high.
    """
    findings: list[Finding] = []
    for indexes in _bracket_chains(records).values():
        if len(indexes) < 2:
            continue
        lowest = min(indexes, key=lambda i: (records[i].lower_bound or 0, i))
        first_lower = records[lowest].lower_bound or 0
        if first_lower > 0:
            findings.append(
                Finding(
                    rule=RULE_BRACKET_BOTTOM,
                    severity=Severity.FLAG,
                    detail=(
                        f"chain's lowest bracket {_fmt(records[lowest])} starts at "
                        f"{first_lower}; [0, {first_lower - 1}] is uncovered"
                    ),
                    record_index=lowest,
                )
            )
    return findings


def check_open_top(records: Sequence[CanonicalRecord]) -> list[Finding]:
    """FLAG chains whose open-ended ("and over") bracket is missing, doubled,
    or not actually on top.

    A rate schedule of two or more brackets must terminate in exactly one
    open-ended bracket holding the greatest lower bound; anything else means
    the top row was misread — usually a truncated "and over" cell.
    """
    findings: list[Finding] = []
    for indexes in _bracket_chains(records).values():
        if len(indexes) < 2:
            continue
        open_indexes = [i for i in indexes if records[i].upper_bound is None]
        top_index = max(indexes, key=lambda i: (records[i].lower_bound or 0, i))
        if not open_indexes:
            findings.append(
                Finding(
                    rule=RULE_OPEN_TOP,
                    severity=Severity.FLAG,
                    detail=(
                        f"chain of {len(indexes)} brackets has no open-ended top; "
                        f"highest bracket is {_fmt(records[top_index])}"
                    ),
                    record_index=top_index,
                )
            )
        elif len(open_indexes) > 1:
            findings.extend(
                Finding(
                    rule=RULE_OPEN_TOP,
                    severity=Severity.FLAG,
                    detail=(
                        f"chain has {len(open_indexes)} open-ended brackets "
                        f"at batch indexes {open_indexes}; exactly one is expected"
                    ),
                    record_index=index,
                )
                for index in open_indexes
            )
        elif open_indexes[0] != top_index:
            findings.append(
                Finding(
                    rule=RULE_OPEN_TOP,
                    severity=Severity.FLAG,
                    detail=(
                        f"open-ended bracket {_fmt(records[open_indexes[0]])} sits below "
                        f"bounded bracket {_fmt(records[top_index])} in the same chain"
                    ),
                    record_index=open_indexes[0],
                )
            )
    return findings


def check_rate_plausibility(records: Sequence[CanonicalRecord]) -> list[Finding]:
    """FLAG rates that are unlikely to be decimal fractions.

    The canonical convention is 0.22 = 22%. A value above 0.5 is almost
    always a percentage that never got divided by 100; a value materially
    below zero is a parse error rather than document 03's legitimate
    statutory rebate (-0.0003). Both are flags, never rejects — the value is
    persisted for review, because the reviewer needs to see what was read.
    """
    findings: list[Finding] = []
    for index, record in enumerate(records):
        rate = record.rate
        if rate is None:
            continue
        if rate > _RATE_UPPER_PLAUSIBLE:
            findings.append(
                Finding(
                    rule=RULE_RATE_PLAUSIBILITY,
                    severity=Severity.FLAG,
                    detail=(
                        f"rate {rate} exceeds {_RATE_UPPER_PLAUSIBLE}; "
                        f"suspected percentage not converted to a fraction"
                    ),
                    record_index=index,
                )
            )
        elif rate < _RATE_LOWER_PLAUSIBLE:
            findings.append(
                Finding(
                    rule=RULE_RATE_PLAUSIBILITY,
                    severity=Severity.FLAG,
                    detail=f"rate {rate} is implausibly negative for a statutory rebate",
                    record_index=index,
                )
            )
    return findings


def check_confidence_floor(
    records: Sequence[CanonicalRecord], *, floor: Decimal = DEFAULT_CONFIDENCE_FLOOR
) -> list[Finding]:
    """FLAG records the pipeline is not confident about. They are persisted
    with ``review_status='needs_review'``, never withheld."""
    return [
        Finding(
            rule=RULE_CONFIDENCE_FLOOR,
            severity=Severity.FLAG,
            detail=f"confidence {record.confidence} is below the floor of {floor}",
            record_index=index,
        )
        for index, record in enumerate(records)
        if record.confidence < floor
    ]


def _attr_decimal(value: object) -> Decimal | None:
    """Parse a JSONB attribute into a Decimal.

    ``attrs`` is a JSON dict, so a number may arrive as int, float or str;
    ``Decimal(str(x))`` handles all three without float rounding. ``None`` is
    preserved: a null rate means *no tax imposed*, which is a fact, not a
    missing value.
    """
    if value is None:
        return None
    return Decimal(str(value))


def check_derived_sum(records: Sequence[CanonicalRecord]) -> list[Finding]:
    """FLAG document 03's derived column when it fails its own identity.

    ``combined_rate_pct`` is stated in the document as the sum of the state
    rate and the average local rate. A None operand contributes 0 to the
    arithmetic — but stays None in ``attrs``, and is never itself reported as
    missing, because null means "no tax imposed", which is not zero.
    """
    findings: list[Finding] = []
    for index, record in enumerate(records):
        attrs = record.attrs
        present = [key for key in _DERIVED_SUM_KEYS if key in attrs]
        if not present:
            continue  # not a derived-column record at all
        if len(present) != len(_DERIVED_SUM_KEYS):
            # A partial triple is exactly the case this rule exists for — a
            # mapper that lost one of the three columns. Skipping it would
            # disable the only cross-check at the moment it matters.
            missing = [key for key in _DERIVED_SUM_KEYS if key not in attrs]
            findings.append(
                Finding(
                    rule=RULE_DERIVED_SUM,
                    severity=Severity.FLAG,
                    detail=f"derived rate columns incomplete: missing {missing}",
                    record_index=index,
                )
            )
            continue
        raw = [attrs[key] for key in _DERIVED_SUM_KEYS]
        if all(value is None for value in raw):
            continue  # nothing derived: no tax imposed at any level
        try:
            state, local, combined = (_attr_decimal(value) for value in raw)
        except (InvalidOperation, ValueError):
            columns = dict(zip(_DERIVED_SUM_KEYS, raw, strict=True))
            findings.append(
                Finding(
                    rule=RULE_DERIVED_SUM,
                    severity=Severity.FLAG,
                    detail=f"derived rate columns are not numeric: {columns}",
                    record_index=index,
                )
            )
            continue
        total = (state or Decimal(0)) + (local or Decimal(0))
        expected = combined or Decimal(0)
        if abs(total - expected) > _DERIVED_SUM_TOLERANCE:
            findings.append(
                Finding(
                    rule=RULE_DERIVED_SUM,
                    severity=Severity.FLAG,
                    detail=(f"state {state} + local {local} = {total}, but combined is {combined}"),
                    record_index=index,
                )
            )
    return findings


# --------------------------------------------------------------------------
# Batch entry points
# --------------------------------------------------------------------------


def validate_batch(
    records: Sequence[CanonicalRecord], *, confidence_floor: Decimal = DEFAULT_CONFIDENCE_FLOOR
) -> list[Finding]:
    """Run every rule over a batch, in rule order then batch order.

    Rules are independent: each sees the whole batch, so a record rejected by
    one rule is still examined by the others. That keeps the review queue
    honest — a bracket that overlaps *and* is missing its open-ended top gets
    both facts recorded, not just the first one.
    """
    findings = [
        *check_bracket_overlap(records),
        *check_bracket_gap(records),
        *check_bracket_bottom(records),
        *check_open_top(records),
        *check_rate_plausibility(records),
        *check_confidence_floor(records, floor=confidence_floor),
        *check_derived_sum(records),
    ]
    return sorted(findings, key=lambda f: (f.record_index, f.rule))


def triage(
    records: Sequence[CanonicalRecord],
    *,
    confidence_floor: Decimal = DEFAULT_CONFIDENCE_FLOOR,
    extra_findings: Sequence[Finding] = (),
) -> TriageResult:
    """Split a batch into what the repository may insert and what must go to
    the review queue instead.

    Records carrying only FLAG findings are persisted as copies with
    ``review_status='needs_review'`` — the inputs are frozen and are never
    mutated, so the caller's batch (and the accuracy harness's view of the
    mapper's raw output) stays exactly as the mapper produced it.

    ``extra_findings`` lets upstream judges (the RecordVerifier's disputes)
    join the partition under the same accounting: same severities, same
    review-queue entries. An extra finding addressing a record index outside
    the batch is a caller bug and raises — dropping it silently would lose a
    dispute (anti-goal #8).
    """
    for extra in extra_findings:
        if extra.record_index >= len(records):
            raise ValueError(
                f"extra finding addresses record index {extra.record_index} "
                f"of a {len(records)}-record batch"
            )
    findings = sorted(
        [*validate_batch(records, confidence_floor=confidence_floor), *extra_findings],
        key=lambda f: (f.record_index, f.rule),
    )
    by_index: dict[int, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_index[finding.record_index].append(finding)

    persistable: list[CanonicalRecord] = []
    rejected: list[RejectedRecord] = []
    for index, record in enumerate(records):
        own = by_index.get(index, [])
        if any(f.severity is Severity.REJECT for f in own):
            rejected.append(RejectedRecord(index=index, record=record, findings=own))
        elif own:
            persistable.append(
                record.model_copy(update={"review_status": ReviewStatus.NEEDS_REVIEW})
            )
        else:
            persistable.append(record)
    return TriageResult(persistable=persistable, rejected=rejected, findings=findings)


def review_queue_entry(record: CanonicalRecord, finding: Finding) -> dict[str, Any]:
    """Shape one (record, finding) pair for ``review_queue`` (migration 0004).

    Returned as a plain dict of column values, not written: the repository
    adapter owns SQL, and it is the only layer that knows ``document_id``.
    ``row_index``/``col_index`` are None because a semantic finding indicts a
    whole record, not one cell — those columns carry coordinates only for
    extraction- and mapping-level issues. ``raw_value`` is the record itself
    as JSON, so a reviewer sees precisely what the pipeline proposed.
    """
    return {
        "source_page": record.source_page,
        "table_id": record.table_id,
        "row_index": None,
        "col_index": None,
        "raw_value": record.model_dump_json(),
        "reason": f"{finding.rule}: {finding.detail}",
    }
