"""RecordVerifier adapter for the Anthropic Messages API.

The independent second half of the semantic layer (ADR 012). It receives the
mapped records plus the *same* serialized extraction view the mapper worked
from, in a fresh context under a skeptic prompt, and returns one verdict per
record. Three properties are enforced here rather than hoped for:

- **Independence is structural.** The verifier never sees the mapper's
  reasoning, prompt transcript, or self-assessed confidence — only the
  records and the extraction view (see ``serialize_records``). Agreement
  between two independent derivations is evidence; a model agreeing with its
  own transcript is an echo.
- **Silence is never assent.** ``parse_verification_payload`` starts from
  "every record unjudged" and fails closed: a record the response skipped,
  double-judged inconsistently, or judged with a value outside the contract
  comes back DISPUTED with the reason attached, never confirmed by default.
- **A dispute is a reason, never a repair.** The verifier proposes no
  replacement values and no new records (anti-goal #8): the pipeline persists
  the disputed record as ``needs_review`` and routes the prose to the review
  queue.

Configuration reads ``RECORD_VERIFIER_*`` first, then falls back to the
mapper's ``SCHEMA_MAPPER_*`` and finally the SDK's ``ANTHROPIC_*`` names, so
the verifier can run a cheaper or different-family model than the mapper
without a second deployment story.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anthropic

from tax_tables.adapters.anthropic_mapper import CANONICAL_CONVENTIONS, serialize_document
from tax_tables.adapters.pricing import ANTHROPIC_CACHE_FACTORS, cache_factors_for
from tax_tables.domain.records import CanonicalRecord
from tax_tables.extraction.model import ExtractedDocument
from tax_tables.observability import conformance
from tax_tables.ports.mapper import MappingCost, MappingResult
from tax_tables.ports.verifier import RecordVerdict, Verdict, VerificationResult

_DEFAULT_MODEL = "claude-opus-5"
#: Claude Opus 5 list prices, USD per million tokens; override via env for a
#: cheaper verifier model or a gateway with different billing.
_DEFAULT_USD_PER_MTOK_IN = Decimal(5)
_DEFAULT_USD_PER_MTOK_OUT = Decimal(25)
_MTOK = Decimal(1_000_000)

#: Verdicts are small — one line of prose per record at worst — so the
#: verifier keeps its own ceiling instead of inheriting the mapper's
#: 64k record-generation budget.
_MAX_OUTPUT_TOKENS = 16_000
_REQUEST_TIMEOUT_SECONDS = 900.0


class VerifierConfigError(RuntimeError):
    """The environment does not describe a usable verification endpoint."""


class VerifierError(RuntimeError):
    """The verification call failed in a way that must abort verification for
    this document — a truncated or refused response, or a body that is not
    the contracted JSON. Never swallowed into a partial result: a half-judged
    batch would read as 'verified' downstream, which is exactly the silent
    assent the port forbids."""


@dataclass(frozen=True)
class VerifierConfig:
    # repr=False: a traceback or log line carrying the config must never
    # render the credential (anti-goal #10).
    api_key: str = field(repr=False)
    model: str = _DEFAULT_MODEL
    base_url: str | None = None
    usd_per_mtok_in: Decimal = _DEFAULT_USD_PER_MTOK_IN
    usd_per_mtok_out: Decimal = _DEFAULT_USD_PER_MTOK_OUT
    max_output_tokens: int = _MAX_OUTPUT_TOKENS
    #: Cache prices as multiples of the input price, defaulted from THIS
    #: role's model rather than Anthropic's ratios (see ``adapters.pricing``).
    #: A role pointed at another family prices its own cache tokens.
    cache_read_factor: Decimal = ANTHROPIC_CACHE_FACTORS.read
    cache_write_factor: Decimal = ANTHROPIC_CACHE_FACTORS.write

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VerifierConfig:
        """Verifier-specific vars win, then the mapper's, then the SDK's.

        The ``RECORD_VERIFIER_MODEL`` rung of that chain is the mitigation
        ADR 012 names for conformity risk: when two agents share a model they
        tend to share its mistakes, so the verifier must be pointable at a
        cheaper or different-family model than the mapper by configuration
        alone. The fallback to ``SCHEMA_MAPPER_*`` keeps a single-key
        deployment working; it is not the recommended setup.

        ``max_output_tokens`` deliberately does NOT inherit
        ``SCHEMA_MAPPER_MAX_OUTPUT_TOKENS``: that budget is sized for
        generating 50+ records, not for judging them.
        """
        source = os.environ if env is None else env
        api_key = (
            source.get("RECORD_VERIFIER_API_KEY")
            or source.get("SCHEMA_MAPPER_API_KEY")
            or source.get("ANTHROPIC_API_KEY")
        )
        if not api_key:
            raise VerifierConfigError(
                "no verification API key: set RECORD_VERIFIER_API_KEY, "
                "SCHEMA_MAPPER_API_KEY or ANTHROPIC_API_KEY"
            )
        mapper_model = source.get("SCHEMA_MAPPER_MODEL") or _DEFAULT_MODEL
        model = source.get("RECORD_VERIFIER_MODEL") or mapper_model
        # The mapper's env prices describe the MAPPER's model. They transfer
        # only when this role runs that same model; a role pointed elsewhere
        # without its own prices falls to the defaults, so a cheaper verifier
        # is never silently billed at another model's rate (adversarial-
        # review minor, promoted).
        same_engine = model == mapper_model
        factors = cache_factors_for(model)
        return cls(
            api_key=api_key,
            model=model,
            base_url=(
                source.get("RECORD_VERIFIER_BASE_URL")
                or source.get("SCHEMA_MAPPER_BASE_URL")
                or source.get("ANTHROPIC_BASE_URL")
            ),
            usd_per_mtok_in=Decimal(
                source.get("RECORD_VERIFIER_USD_PER_MTOK_IN")
                or (source.get("SCHEMA_MAPPER_USD_PER_MTOK_IN") if same_engine else None)
                or str(_DEFAULT_USD_PER_MTOK_IN)
            ),
            usd_per_mtok_out=Decimal(
                source.get("RECORD_VERIFIER_USD_PER_MTOK_OUT")
                or (source.get("SCHEMA_MAPPER_USD_PER_MTOK_OUT") if same_engine else None)
                or str(_DEFAULT_USD_PER_MTOK_OUT)
            ),
            max_output_tokens=int(
                source.get("RECORD_VERIFIER_MAX_OUTPUT_TOKENS") or _MAX_OUTPUT_TOKENS
            ),
            cache_read_factor=Decimal(
                source.get("RECORD_VERIFIER_CACHE_READ_FACTOR")
                or (source.get("SCHEMA_MAPPER_CACHE_READ_FACTOR") if same_engine else None)
                or str(factors.read)
            ),
            cache_write_factor=Decimal(
                source.get("RECORD_VERIFIER_CACHE_WRITE_FACTOR")
                or (source.get("SCHEMA_MAPPER_CACHE_WRITE_FACTOR") if same_engine else None)
                or str(factors.write)
            ),
        )


# ---------------------------------------------------------------------------
# Input serialization
# ---------------------------------------------------------------------------

#: Fields of a mapped record the verifier must never see. ``confidence`` is
#: the mapper's self-assessment and ``review_status`` is what the pipeline
#: already concluded from it; showing either anchors the second opinion on
#: the first one's belief. The verifier judges the mapping against the
#: document, not the mapper's confidence in it (port docstring, ADR 012).
_WITHHELD_FIELDS = ("confidence", "review_status")


def serialize_records(records: Sequence[CanonicalRecord]) -> str:
    """The records under review, as deterministic JSON, addressed by index.

    ``attrs`` travels in full — including ``provenance`` and
    ``source_table_label`` — because the citations are precisely what the
    verifier checks: a record whose cited cell does not say what the record
    claims is the failure this role exists to catch.

    Decimals ride as their exact strings (pydantic's JSON mode), so no float
    rounding stands between the persisted value and the value under review.
    """
    items: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        payload = record.model_dump(mode="json")
        for name in _WITHHELD_FIELDS:
            payload.pop(name, None)
        items.append({"record_index": index, **payload})
    return json.dumps(items, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Output schema (structured outputs)
# ---------------------------------------------------------------------------

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "record_index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["confirmed", "disputed"]},
                    "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["record_index", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# System prompt — skeptic role over the mapper's own conventions
# ---------------------------------------------------------------------------

_VERIFIER_ROLE = """\
You are the independent verifier inside a tax-table ingestion pipeline. You
did NOT produce the records under review, and you have not seen the reasoning
that produced them. Do not assume they are right.

Your input is two things:

1. The machine-extracted view of ONE document: cell grids (verbatim, row by
   row) and prose blocks (headings, body text, footnotes), with page numbers,
   table ids and per-page prose indexes. This is exactly the view the mapping
   model worked from. There is no other evidence and no other document.
2. The canonical records that model produced from that view, each addressed
   by "record_index".

Your objective is REFUTATION, not agreement. Attempt to break every record
against the input. A record survives only if you positively confirm both its
values and its citations.

## What to check, record by record

1. Citations. Every typed value and every extra attr must trace to the
   provenance the record cites: kind "cell" names table_id/row/col (0-based
   indexes into that table's "rows"), kind "prose" names page and
   prose_index. A citation pointing at a cell or block that does not exist,
   or at one whose text does not support the value, is a dispute.
2. Values, under the canonical conventions below: rate as a decimal fraction
   versus attribute keys ending in "_pct" kept as printed; bracket bounds
   transcribed from the page rather than re-derived; a lone dash in a value
   cell meaning null (never 0, never "unreadable"); "No limit" meaning null;
   a row that prints a current and a prior year keeping BOTH, the prior year
   as an extra attr on the same record and never as its own record.
3. tax_year. Only a statement in the document text is evidence ("applies to
   tax year 2026", "effective for taxable years beginning after ..."). A
   document ID or bulletin number is NEVER evidence: bulletins are issued in
   the year before the year they govern. A tax_year no sentence of this
   input supports is a dispute.
4. lifecycle_status "superseded" only when the document itself states it has
   been superseded or replaced.
5. Column attribution. The filing_status or taxpayer_class a record claims
   must be the column, or the schedule caption, that its cited cells
   actually sit under.

## Hard rules

- This corpus is synthetic. A real-world tax figure that is not printed in
  this input is wrong here by definition. Never confirm a value because it
  matches what you know of real tax law, never "correct" a record from
  outside knowledge, and dispute any value that appears nowhere in the input.
- Emit EXACTLY one verdict per record_index, 0 through N-1, where N is the
  number of records under review. No index twice, none missing.
- "confirmed" means the values AND the citations both check out. Anything
  you cannot positively confirm is "disputed" - including a record you are
  merely unsure about. Uncertainty is a dispute, not a pass.
- A dispute's "reason" is concrete prose naming the evidence you checked:
  the cell coordinates or prose index, and what that cell or block actually
  says.
- You repair nothing. Do not propose replacement records, corrected values,
  or additional records you believe were missed. A reason is prose for a
  human reviewer, not data for the pipeline.

"""

#: The verifier judges by the mapper's own law: the canonical conventions are
#: shared verbatim (see the comment on ``CANONICAL_CONVENTIONS``), so a
#: dispute is a disagreement about the document, never a prompt divergence
#: about the target schema. What stays independent is each role's reading of
#: the document.
VERIFIER_SYSTEM_PROMPT = _VERIFIER_ROLE + CANONICAL_CONVENTIONS

_USER_INSTRUCTION = (
    "Verify the mapped records below against the extracted document. Emit "
    "exactly one verdict per record_index, and dispute anything you cannot "
    "positively confirm from this input.\n\n"
)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

_VERDICT_VALUES = {member.value for member in Verdict}


def _clip(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)[:200]


def _reason_text(raw: Mapping[str, Any]) -> str | None:
    reason = raw.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def _describe(verdict: RecordVerdict) -> str:
    if verdict.reason:
        return f"{verdict.verdict.value} ({verdict.reason})"
    return verdict.verdict.value


def _verdict_for(index: int, raw: Mapping[str, Any], notes: list[str]) -> RecordVerdict:
    value = raw.get("verdict")
    reason = _reason_text(raw)
    if not isinstance(value, str) or value not in _VERDICT_VALUES:
        notes.append(f"record {index}: unrecognized verdict {value!r}; disputed fail-closed")
        detail = f"; stated reason: {reason}" if reason else ""
        return RecordVerdict(
            record_index=index,
            verdict=Verdict.DISPUTED,
            reason=f"verifier returned the unrecognized verdict {value!r}{detail}",
        )
    verdict = Verdict(value)
    if verdict is Verdict.DISPUTED:
        # The port requires a dispute to carry its why; an empty one is still
        # a dispute, just an unhelpful one.
        return RecordVerdict(record_index=index, verdict=verdict, reason=reason or "unspecified")
    return RecordVerdict(record_index=index, verdict=verdict, reason=reason)


def _reconcile(existing: RecordVerdict, proposed: RecordVerdict, notes: list[str]) -> RecordVerdict:
    """Two verdicts for one record. Agreeing confirmations collapse; anything
    else is the verifier disagreeing with itself, which cannot clear a
    record."""
    index = existing.record_index
    if existing.verdict is Verdict.CONFIRMED and proposed.verdict is Verdict.CONFIRMED:
        notes.append(f"record {index}: duplicate agreeing 'confirmed' verdicts; kept one")
        return existing
    notes.append(f"record {index}: conflicting duplicate verdicts; disputed fail-closed")
    return RecordVerdict(
        record_index=index,
        verdict=Verdict.DISPUTED,
        reason=(
            "verifier contradicted itself on this record: "
            + ", then ".join(_describe(v) for v in (existing, proposed))
        ),
    )


def parse_verification_payload(
    text: str, *, record_count: int
) -> tuple[list[RecordVerdict], list[str]]:
    """Assemble exactly ``record_count`` verdicts from the model's JSON.

    Fail-closed at every step: a record the response skipped, judged twice
    inconsistently, or judged with an out-of-contract value ends DISPUTED
    with the anomaly named. Verdicts that cannot protect any record (an index
    outside the batch, a non-integer index) are dropped into ``notes`` rather
    than guessed at — nothing the model said disappears silently.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerifierError(f"verification response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
        raise VerifierError("verification response JSON lacks the verdicts envelope")

    assigned: dict[int, RecordVerdict] = {}
    notes: list[str] = []
    conformance.LEDGER.record_items(conformance.VERIFIER, record_count)
    for raw in payload["verdicts"]:
        if not isinstance(raw, Mapping):
            conformance.LEDGER.record_malformed_item(
                conformance.VERIFIER, "verdict that is not an object"
            )
            notes.append(f"ignored a verdict that is not an object: {_clip(raw)}")
            continue
        index = raw.get("record_index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < record_count:
            conformance.LEDGER.record_malformed_item(
                conformance.VERIFIER, "verdict naming a record outside the batch"
            )
            notes.append(
                f"ignored a verdict naming record_index {index!r}, which is not one "
                f"of the {record_count} records under review"
            )
            continue
        proposed = _verdict_for(index, raw, notes)
        existing = assigned.get(index)
        assigned[index] = proposed if existing is None else _reconcile(existing, proposed, notes)

    for index in range(record_count):
        if index not in assigned:
            # Silence is not assent (port docstring) — and a record the model
            # was asked to judge and did not is a contract miss, not merely a
            # dispute.
            conformance.LEDGER.record_malformed_item(
                conformance.VERIFIER, "no verdict returned for a record under review"
            )
            assigned[index] = RecordVerdict(
                record_index=index,
                verdict=Verdict.DISPUTED,
                reason="verifier returned no verdict for this record",
            )
    return [assigned[index] for index in sorted(assigned)], notes


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def _clip_reason(exc: Exception, limit: int = 160) -> str:
    """A failure reason short enough for a report line."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AnthropicRecordVerifier:
    """RecordVerifier over the Anthropic Messages API (or any endpoint that
    speaks it, such as the Vercel AI Gateway)."""

    def __init__(self, config: VerifierConfig, *, client: Any | None = None) -> None:
        # ``client`` is injectable for tests; anything exposing
        # ``messages.stream(**kwargs)`` with a ``get_final_message()``
        # context manager qualifies.
        self._config = config
        self._client = client if client is not None else self._build_client(config)

    @staticmethod
    def _build_client(config: VerifierConfig) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=3,
            http_client=conformance.instrumented_http_client(
                conformance.VERIFIER, timeout=_REQUEST_TIMEOUT_SECONDS
            ),
        )

    def verify(self, extracted: ExtractedDocument, mapping: MappingResult) -> VerificationResult:
        if not mapping.records:
            # Recorded before the call counter: a document that mapped nothing
            # makes no verification call, so it must not enter the denominator.
            return VerificationResult(verdicts=[], cost=None)
        conformance.LEDGER.record_call(conformance.VERIFIER)
        try:
            return self._verify(extracted, mapping)
        except VerifierError as exc:
            conformance.LEDGER.record_schema_failure(conformance.VERIFIER, _clip_reason(exc))
            raise

    def _verify(self, extracted: ExtractedDocument, mapping: MappingResult) -> VerificationResult:
        records = mapping.records
        if not records:
            # Nothing to refute: a document that mapped no records spends no
            # verification credit and reports no cost it did not incur.
            return VerificationResult(verdicts=[], cost=None)

        started = time.perf_counter()
        # ``serialize_document`` is the mapper's own function, called on the
        # same extraction: the same-view guarantee of ADR 012 is a shared
        # code path, not a promise. The skeptic prompt carries a cache
        # breakpoint so a five-document run pays for it once.
        content = (
            _USER_INSTRUCTION
            + "## Extracted document\n"
            + serialize_document(extracted)
            + "\n\n## Mapped records under review\n"
            + serialize_records(records)
        )
        with self._client.messages.stream(
            model=self._config.model,
            max_tokens=self._config.max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": VERIFIER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        ) as stream:
            message = stream.get_final_message()

        if message.stop_reason != "end_turn":
            raise VerifierError(
                f"verification call ended with stop_reason={message.stop_reason!r}; "
                "refusing to parse a truncated or refused response"
            )
        text: str | None = None
        for block in message.content:
            candidate = getattr(block, "text", None) if block.type == "text" else None
            if candidate:
                text = candidate
                break
        if text is None:
            raise VerifierError("verification response contains no text block")

        verdicts, notes = parse_verification_payload(text, record_count=len(records))
        usage = message.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        usd = (
            Decimal(input_tokens) * self._config.usd_per_mtok_in
            + Decimal(cache_write) * self._config.usd_per_mtok_in * self._config.cache_write_factor
            + Decimal(cache_read) * self._config.usd_per_mtok_in * self._config.cache_read_factor
            + Decimal(output_tokens) * self._config.usd_per_mtok_out
        ) / _MTOK
        cost = MappingCost(
            engine=self._config.model,
            api_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
            usd=usd,
            wall_seconds=time.perf_counter() - started,
        )
        return VerificationResult(verdicts=verdicts, notes=notes, cost=cost)
