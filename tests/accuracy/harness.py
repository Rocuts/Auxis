"""The accuracy harness: comparison and reporting machinery.

``tests/accuracy/`` is the *only* place licensed to read
``fixtures/ground_truth.json`` (anti-goal #1) — it is the test oracle, and an
oracle a module under ``src/`` can see is not an oracle. ``test_harness.py``
enforces that mechanically.

What lives here is machinery only. There is deliberately **no** end-to-end
accuracy run in Phase 2a: the SchemaMapper adapter does not exist yet, and an
accuracy table assembled from stubbed mappings would be a fabricated result —
worse than a red one (anti-goal #2). So the pieces below are built and unit-
tested against synthetic hand-built pairs, and the oracle is loaded and
structurally validated, nothing more. Phase 2b supplies the ``actual`` side.

Three properties this machinery must have, because the Phase 2 gate depends
on them:

- **No float ever touches an expected value.** JSON numbers are parsed
  straight to ``Decimal`` from their source text, so ``0.1`` in the oracle is
  ``Decimal("0.1")`` and never ``Decimal("0.1000000000000000055511151231...")``.
- **An absent value is not a null value.** A mapper that never produced
  ``state_rate_pct`` is not the same as one that correctly produced NULL for a
  state imposing no sales tax. The comparison reports ``<absent>`` distinctly
  (anti-goal #8 at the reporting layer).
- **Every failure is named.** Below target, the report lists each failing
  record's natural key and reason. "126/128" without the two names is not a
  gate result.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tax_tables.domain.records import CanonicalRecord, LifecycleStatus, RecordType

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES_DIR: Final = REPO_ROOT / "fixtures"
GROUND_TRUTH_PATH: Final = FIXTURES_DIR / "ground_truth.json"

#: Oracle field -> the name it is compared under on a mapped record. The
#: oracle's ``table_id`` is the label the *document prints* ("table_1",
#: "section_3", "footnote"); ``CanonicalRecord.table_id`` is extraction
#: provenance ("p1_t0"). Two key spaces — comparing them to each other
#: compares different things, and every one of the 128 records would
#: mismatch. The mapper's side of the contract lives in the SchemaMapper
#: port docstring: extraction provenance stays in ``table_id``, and the
#: document's own label rides in ``attrs["source_table_label"]``.
COMPARED_AS: Final[Mapping[str, str]] = {"table_id": "source_table_label"}

#: Oracle commentary, not assertions: free prose documenting a trap or an
#: extraction subtlety. A mapper cannot (and must not) reproduce English
#: sentences, so these fields are never compared.
NOT_COMPARED: Final = frozenset({"note", "extraction_note"})

#: Which field of an expected entry carries the DDL's ``attribute_key``
#: sub-discriminator, per record type. Mirrors the comment on
#: ``migrations/0003_records.sql``: employment component, wage-base item,
#: surtax name, payroll period, deduction condition, gain category. Record
#: types absent from this map have no sub-discriminator (their natural key is
#: already unique on the remaining components).
ATTRIBUTE_KEY_FIELD: Final[Mapping[RecordType, str]] = {
    RecordType.ADDITIONAL_STANDARD_DEDUCTION: "condition",
    RecordType.EMPLOYMENT_TAX_RATE: "component",
    RecordType.SPECIAL_GAIN_RATE: "category",
    RecordType.SURTAX_THRESHOLD: "surtax",
    RecordType.WAGE_BASE: "item",
    RecordType.WITHHOLDING_ALLOWANCE: "payroll_period",
}


class _Absent:
    """Sentinel for "the actual record has no such field at all".

    Deliberately not equal to ``None``: an unproduced value and a value the
    mapper correctly determined to be NULL are different outcomes, and
    collapsing them would hide exactly the silent-loss failure anti-goal #8
    is about.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


ABSENT: Final = _Absent()


def _undo_float(value: Any) -> Any:
    """Defense in depth behind ``parse_float=Decimal``.

    The loader already parses JSON numbers directly into ``Decimal`` from
    their source text; this catches any float that reaches the tree by some
    other route and converts it the only safe way, ``Decimal(str(x))``.
    ``bool`` is checked first because ``bool`` is a subclass of ``int``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_undo_float(item) for item in value]
    if isinstance(value, dict):
        return {key: _undo_float(item) for key, item in value.items()}
    return value


class NaturalKey(NamedTuple):
    """Record identity, mirroring ``records_natural_key`` in the DDL.

    Two departures from the constraint, both deliberate:

    - ``source_document`` leads, because accuracy is reported *per document*
      and the oracle's key space is scoped that way. In the database the
      document is reachable through ``document_id`` provenance instead.
    - ``lower_bound`` stands in for the DDL's ``bracket int8range``; the
      oracle states inclusive bounds and the upper bound is redundant for
      identity once overlap is impossible.
    """

    source_document: str
    record_type: str
    jurisdiction: str
    tax_year: int | None
    filing_status: str | None
    taxpayer_class: str | None
    attribute_key: str | None
    lower_bound: int | None


def format_key(key: NaturalKey) -> str:
    """One-line rendering for failure listings; ``-`` marks a NULL component."""
    return " | ".join("-" if part is None else str(part) for part in key)


class DocumentExpectation(BaseModel):
    """One entry of the oracle's ``documents`` array."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    file: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_record_count: int = Field(ge=0)


class Totals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    records: int = Field(ge=0)
    by_record_type: dict[str, int] = Field(default_factory=dict)


class ExpectedRecord(BaseModel):
    """One entry of the oracle's ``expected_records`` array.

    The typed core mirrors ``CanonicalRecord``; every other field the oracle
    states (``state_rate_pct``, ``component``, ``prior_year_amount``,
    ``rule``, ...) is kept verbatim as an extra and is compared against the
    mapped record's ``attrs`` under the same name. ``extra="allow"`` is what
    makes the harness survive Phase 2b adding record shapes without a schema
    edit here.

    ``lifecycle_status`` follows the oracle's stated convention: absent means
    ``active``. It is therefore always compared, even when the entry is
    silent about it — document 05's records must come back ``superseded`` and
    the other four documents' must not.

    ``filing_status`` is typed ``str``, not ``FilingStatus``, deliberately:
    the oracle is the authority on what the source documents say, and it says
    ``qualifying_surviving_spouse`` twice (documents 02 and 04) — a status the
    domain enum and the DDL's CHECK do not yet admit. Typing it as the enum
    here would make the harness unable to *load* the oracle, which would hide
    a real domain gap behind an import error instead of reporting it.
    ``natural_key`` and ``values_equal`` normalize the StrEnum and plain-str
    spellings, so nothing else has to care.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    source_document: str = Field(min_length=1)
    source_page: int = Field(ge=1)
    table_id: str = Field(min_length=1)
    record_type: RecordType
    jurisdiction: str = Field(min_length=2)
    tax_year: int | None = None
    filing_status: str | None = None
    taxpayer_class: str | None = None
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE
    lower_bound: int | None = None
    upper_bound: int | None = None
    rate: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None

    @property
    def attribute_key(self) -> str | None:
        """The sub-discriminator value, read from this record type's field."""
        source = ATTRIBUTE_KEY_FIELD.get(self.record_type)
        if source is None:
            return None
        value = (self.model_extra or {}).get(source)
        return None if value is None else str(value)

    def compared_fields(self) -> dict[str, Any]:
        """The fields this entry asserts, and therefore the fields a mapped
        record is judged on.

        Fields the oracle is silent about are not compared — a mapper may
        legitimately carry extra provenance in ``attrs`` (extraction notes,
        raw cell text) without being wrong. ``lifecycle_status`` is the one
        field compared even when unset, because its default *is* an
        assertion.
        """
        stated = {name: getattr(self, name) for name in sorted(self.model_fields_set)}
        stated.update(dict(sorted((self.model_extra or {}).items())))
        stated["lifecycle_status"] = self.lifecycle_status
        for name in NOT_COMPARED:
            stated.pop(name, None)
        return stated


class GroundTruth(BaseModel):
    """The oracle, validated for internal consistency on load.

    A miscounted oracle would silently move the accuracy denominator, so the
    counts are cross-checked three ways before anything is compared against
    it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    conventions: dict[str, str] = Field(default_factory=dict)
    documents: list[DocumentExpectation]
    totals: Totals
    expected_records: list[ExpectedRecord]
    deliberate_traps: list[Any] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_counts(self) -> GroundTruth:
        if len(self.expected_records) != self.totals.records:
            raise ValueError(
                f"totals.records={self.totals.records} but "
                f"{len(self.expected_records)} expected_records are listed"
            )
        for document in self.documents:
            actual = sum(1 for r in self.expected_records if r.source_document == document.file)
            if actual != document.expected_record_count:
                raise ValueError(
                    f"{document.file}: expected_record_count="
                    f"{document.expected_record_count} but {actual} records reference it"
                )
        for record_type, count in self.totals.by_record_type.items():
            actual = sum(1 for r in self.expected_records if r.record_type == record_type)
            if actual != count:
                raise ValueError(
                    f"totals.by_record_type[{record_type}]={count} but {actual} records have it"
                )
        keys = [natural_key(r) for r in self.expected_records]
        if len(set(keys)) != len(keys):
            raise ValueError("expected_records contain duplicate natural keys")
        return self

    @property
    def record_count(self) -> int:
        return len(self.expected_records)


def load_ground_truth(path: Path = GROUND_TRUTH_PATH) -> GroundTruth:
    """Parse and validate the oracle.

    ``parse_float=Decimal`` is the load-bearing argument: it builds each
    number from the JSON source text, so no expected value is ever routed
    through a binary float.
    """
    raw = _undo_float(json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal))
    return GroundTruth.model_validate(raw)


ActualRecord = tuple[str, CanonicalRecord]
"""A mapped record plus the document it came from — the document name is not
on ``CanonicalRecord`` (persistence carries it as ``document_id``), but it is
half of the accuracy key."""


def natural_key(record: ExpectedRecord | ActualRecord) -> NaturalKey:
    """Identity for an expected entry or a ``(document, CanonicalRecord)`` pair.

    Both sides must produce byte-identical keys or every record reads as one
    ``missing`` plus one ``spurious``; ``str()`` normalizes the StrEnum and
    plain-``str`` spellings of the same discriminator to one form.
    """
    if isinstance(record, tuple):
        document, canonical = record
        return NaturalKey(
            source_document=document,
            record_type=str(canonical.record_type),
            jurisdiction=canonical.jurisdiction,
            tax_year=canonical.tax_year,
            filing_status=_text(canonical.filing_status),
            taxpayer_class=_text(canonical.taxpayer_class),
            attribute_key=_text(canonical.attribute_key),
            lower_bound=canonical.lower_bound,
        )
    return NaturalKey(
        source_document=record.source_document,
        record_type=str(record.record_type),
        jurisdiction=record.jurisdiction,
        tax_year=record.tax_year,
        filing_status=_text(record.filing_status),
        taxpayer_class=_text(record.taxpayer_class),
        attribute_key=record.attribute_key,
        lower_bound=record.lower_bound,
    )


def _text(value: object | None) -> str | None:
    return None if value is None else str(value)


def actual_value(record: ActualRecord, field_name: str) -> Any:
    """Read one compared field off a mapped record.

    Core fields come from the model; everything else must be in ``attrs``
    under the oracle's own name — that is the contract the Phase 2b mapper
    owes this harness. A name found in neither yields ``ABSENT``, never
    ``None``.
    """
    document, canonical = record
    if field_name == "source_document":
        return document
    if field_name in CanonicalRecord.model_fields:
        return getattr(canonical, field_name)
    return canonical.attrs.get(field_name, ABSENT)


def values_equal(expected: object, actual: object) -> bool:
    """Compare across the representation gap between oracle and model.

    Numeric equality is by value (``15400`` == ``Decimal("15400.00")``), so
    the harness does not fail a correct mapper over ``numeric(14,2)``
    scale. ``bool`` is handled before the numeric branch — ``True == 1`` is
    true in Python and must not be here, because ``imposes_state_sales_tax``
    is a genuine boolean. Dates compare against their ISO spelling, since the
    oracle states them as strings.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, int | Decimal) and isinstance(actual, int | Decimal):
        return Decimal(expected) == Decimal(actual)
    if isinstance(expected, date) and isinstance(actual, str):
        return expected.isoformat() == actual
    if isinstance(expected, str) and isinstance(actual, date):
        return expected == actual.isoformat()
    return bool(expected == actual)


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """One field that disagrees, carrying both sides for the report."""

    field: str
    expected: Any
    actual: Any

    def render(self) -> str:
        return f"{self.field}: expected {_render(self.expected)}, actual {_render(self.actual)}"


@dataclass(frozen=True, slots=True)
class RecordMismatch:
    """A record present on both sides whose values disagree."""

    key: NaturalKey
    diffs: tuple[FieldDiff, ...]


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    matched: tuple[NaturalKey, ...] = ()
    field_mismatches: tuple[RecordMismatch, ...] = ()
    missing: tuple[NaturalKey, ...] = ()
    spurious: tuple[NaturalKey, ...] = ()
    fields_compared: int = 0
    fields_differing: int = 0
    #: Subset of ``spurious``: keys a mapper emitted more than once.
    duplicate_actual_keys: tuple[NaturalKey, ...] = ()

    @property
    def expected_count(self) -> int:
        return len(self.matched) + len(self.field_mismatches) + len(self.missing)

    @property
    def is_perfect(self) -> bool:
        return len(self.matched) == self.expected_count and not self.spurious


def _render(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, _Absent):
        return repr(value)
    if isinstance(value, str):
        return value
    return str(value)


def compare(expected: Sequence[ExpectedRecord], actual: Sequence[ActualRecord]) -> ComparisonResult:
    """Match mapped records to expected ones by natural key, then by value.

    Collision policy, chosen to mirror what the database would do: the
    natural key is ``UNIQUE NULLS NOT DISTINCT`` in the DDL, so a mapper
    emitting two records under one key could not persist both. The first
    occurrence is the candidate for matching and every later one is reported
    as ``spurious`` — stable, and it surfaces the duplication instead of
    letting a second row quietly overwrite the first.

    Duplicate keys on the *expected* side mean the oracle itself is
    ambiguous; that is a hard error, not a comparison outcome.
    """
    expected_by_key: dict[NaturalKey, ExpectedRecord] = {}
    for entry in expected:
        key = natural_key(entry)
        if key in expected_by_key:
            raise ValueError(f"duplicate natural key among expected records: {format_key(key)}")
        expected_by_key[key] = entry

    actual_by_key: dict[NaturalKey, ActualRecord] = {}
    duplicates: list[NaturalKey] = []
    for pair in actual:
        key = natural_key(pair)
        if key in actual_by_key:
            duplicates.append(key)
            continue
        actual_by_key[key] = pair

    matched: list[NaturalKey] = []
    mismatches: list[RecordMismatch] = []
    fields_compared = 0
    fields_differing = 0

    for key, entry in expected_by_key.items():
        candidate = actual_by_key.get(key)
        if candidate is None:
            continue
        diffs: list[FieldDiff] = []
        for name, want in entry.compared_fields().items():
            fields_compared += 1
            got = actual_value(candidate, COMPARED_AS.get(name, name))
            if not values_equal(want, got):
                fields_differing += 1
                diffs.append(FieldDiff(field=name, expected=want, actual=got))
        if diffs:
            mismatches.append(RecordMismatch(key=key, diffs=tuple(diffs)))
        else:
            matched.append(key)

    missing = tuple(key for key in expected_by_key if key not in actual_by_key)
    spurious = tuple(key for key in actual_by_key if key not in expected_by_key)

    return ComparisonResult(
        matched=tuple(matched),
        field_mismatches=tuple(mismatches),
        missing=missing,
        spurious=spurious + tuple(duplicates),
        fields_compared=fields_compared,
        fields_differing=fields_differing,
        duplicate_actual_keys=tuple(duplicates),
    )


GroupBy = Literal["document", "record_type"]

_GROUP_INDEX: Final[Mapping[str, int]] = {"document": 0, "record_type": 1}


@dataclass(frozen=True, slots=True)
class _GroupCounts:
    expected: int = 0
    matched: int = 0
    mismatched: int = 0
    missing: int = 0
    spurious: int = 0

    def plus(self, **kwargs: int) -> _GroupCounts:
        current = {
            "expected": self.expected,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "missing": self.missing,
            "spurious": self.spurious,
        }
        for name, delta in kwargs.items():
            current[name] += delta
        return _GroupCounts(**current)


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Fixed-width table, no dependencies. First column left-aligned (names),
    the rest right-aligned (counts)."""
    widths = [
        max([len(header), *(len(row[i]) for row in rows)]) for i, header in enumerate(headers)
    ]

    def line(cells: Sequence[str]) -> str:
        parts = [cells[0].ljust(widths[0])]
        parts += [cells[i].rjust(widths[i]) for i in range(1, len(cells))]
        return "  ".join(parts).rstrip()

    rule = "-" * (sum(widths) + 2 * (len(widths) - 1))
    return [line(headers), rule, *(line(row) for row in rows)]


def format_report(result: ComparisonResult, by: GroupBy = "document") -> str:
    """Plain-text accuracy report, grouped by document or by record type.

    The Phase 2 gate asks for a table *and*, below target, for every failing
    record named with its reason — so the failure listing is not optional
    output, it is the part of the report that makes a red result usable.
    """
    index = _GROUP_INDEX[by]
    groups: dict[str, _GroupCounts] = {}

    def bump(key: NaturalKey, **kwargs: int) -> None:
        name = str(key[index])
        groups[name] = groups.get(name, _GroupCounts()).plus(**kwargs)

    for key in result.matched:
        bump(key, expected=1, matched=1)
    for mismatch in result.field_mismatches:
        bump(mismatch.key, expected=1, mismatched=1)
    for key in result.missing:
        bump(key, expected=1, missing=1)
    for key in result.spurious:
        bump(key, spurious=1)

    headers = ("group" if by == "record_type" else "document", "exp", "ok", "diff", "miss", "extra")
    rows = [
        [
            name,
            str(counts.expected),
            str(counts.matched),
            str(counts.mismatched),
            str(counts.missing),
            str(counts.spurious),
        ]
        for name, counts in sorted(groups.items())
    ]
    total = _GroupCounts(
        expected=result.expected_count,
        matched=len(result.matched),
        mismatched=len(result.field_mismatches),
        missing=len(result.missing),
        spurious=len(result.spurious),
    )
    rows.append(
        [
            "TOTAL",
            str(total.expected),
            str(total.matched),
            str(total.mismatched),
            str(total.missing),
            str(total.spurious),
        ]
    )

    lines = [f"accuracy by {by}", *_render_table(headers, rows), ""]
    lines.append(f"field-level accuracy: {len(result.matched)}/{result.expected_count}")
    lines.append(f"fields compared: {result.fields_compared}, differing: {result.fields_differing}")
    lines.extend(_failure_lines(result))
    return "\n".join(lines)


def _failure_lines(result: ComparisonResult) -> list[str]:
    """Every failing record, named, with the reason it failed."""
    failures = len(result.field_mismatches) + len(result.missing) + len(result.spurious)
    if failures == 0:
        return []
    duplicates = set(result.duplicate_actual_keys)
    lines = ["", f"failing records ({failures}):"]
    for mismatch in result.field_mismatches:
        lines.append(f"  [field mismatch] {format_key(mismatch.key)}")
        lines.extend(f"      {diff.render()}" for diff in mismatch.diffs)
    for key in result.missing:
        lines.append(f"  [missing] {format_key(key)}")
        lines.append("      no mapped record carries this natural key")
    for key in result.spurious:
        reason = (
            "duplicate natural key: a later record under a key already produced"
            if key in duplicates
            else "mapped record has no counterpart in the ground truth"
        )
        lines.append(f"  [spurious] {format_key(key)}")
        lines.append(f"      {reason}")
    return lines


def iter_source_documents(truth: GroundTruth) -> Iterable[Path]:
    """The fixture PDFs the oracle refers to, as paths."""
    return (FIXTURES_DIR / document.file for document in truth.documents)
