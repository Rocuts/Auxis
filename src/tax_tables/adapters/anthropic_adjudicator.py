"""Adjudicator adapter for the Anthropic Messages API (ADR 012).

One API call per open review-queue item: the adapter re-examines a single
queued finding against the document's full extracted evidence and returns a
citated proposal with a confidence. It is deliberately the *narrowest* of
the three model roles:

- **It never resolves anything.** The adapter produces an ``Adjudication``
  and stops; the pipeline compares its confidence to
  ``DEFAULT_AUTO_RESOLVE_THRESHOLD`` and decides between an auto-resolution
  with an audit trail and a stored proposal that stays with a human. Making
  the adapter the decider would put the auto-close policy inside the
  component least able to see the consequences.
- **It never edits a record and never invents a value.** Its output is
  prose plus citations; every claim must be readable in the extracted grid
  or prose it was handed (the same rule the mapper works under).
- **A citation problem is never an exception.** Missing or dangling
  citations mark ``citations_valid=False`` and the proposal is still
  returned, because a stored proposal a reviewer can read beats a lost one
  (anti-goal #8). Only a truncated, refused, or malformed response raises.

Configuration chains adjudicator-specific variables over the mapper's over
the SDK's own, so the role can run on a cheaper or different-family model
than the mapper without duplicating endpoint configuration.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anthropic

from tax_tables.adapters.anthropic_mapper import (
    CANONICAL_CONVENTIONS,
    PROVENANCE_SCHEMA,
    check_provenance,
    serialize_document,
)
from tax_tables.adapters.envelope import loads_fence_tolerant
from tax_tables.adapters.pricing import ANTHROPIC_CACHE_FACTORS, cache_factors_for
from tax_tables.extraction.model import ExtractedDocument
from tax_tables.observability import conformance
from tax_tables.ports.adjudicator import Adjudication, AdjudicationError, ReviewItem
from tax_tables.ports.mapper import MappingCost

_DEFAULT_MODEL = "claude-opus-5"
#: Claude Opus 5 list prices, USD per million tokens; override via env for
#: any other model or a gateway with different billing.
_DEFAULT_USD_PER_MTOK_IN = Decimal(5)
_DEFAULT_USD_PER_MTOK_OUT = Decimal(25)
_MTOK = Decimal(1_000_000)

#: One item, one short prose disposition plus a handful of citations: an
#: eighth of the mapper's ceiling is already generous, and a low ceiling
#: turns a runaway generation into a fast, loud failure instead of a slow
#: expensive one.
_MAX_OUTPUT_TOKENS = 8_000

#: Per-request ceiling, deliberately far below the mapper's.
#:
#: This is one item and one short disposition — a call that has not answered
#: in a minute and a half is not going to. The number is small because of what
#: sits above it: the pass has a wall-clock budget
#: (``service.jobs.DEFAULT_ADJUDICATION_BUDGET_SECONDS``) checked BETWEEN
#: items, so a single item that can run longer than that budget makes the
#: budget decorative. Production proved that on 2026-08-27: at 300 s x 4 SDK
#: attempts one item could spend 1200 s against a 420 s budget, and document
#: 02 overran a 1800 s invocation three times with its records already
#: correct. ``tests/mapping/test_adjudicator_budget_bound.py`` pins the
#: relationship so it cannot drift back.
_REQUEST_TIMEOUT_SECONDS = 90.0

#: One retry, not three. Bounded rather than removed: a single transport blip
#: should still be absorbed, but each extra attempt multiplies the timeout
#: above and this role is the one that runs AFTER the records are safely
#: persisted. A queue item that misses its proposal waits for a human, which
#: is the documented fallback; a job that never terminates loses the whole
#: run's bookkeeping.
_MAX_RETRIES = 1


class AdjudicatorConfigError(RuntimeError):
    """The environment does not describe a usable adjudication endpoint."""


class AdjudicatorError(AdjudicationError):
    """The adjudication call failed: a truncated or refused response, or a
    body that is not the contracted JSON.

    Subclasses the port's ``AdjudicationError`` because the *pipeline*
    catches it PER ITEM: one bad adjudication leaves its item open and
    named in the pass report, and never kills the pass over the rest of the
    queue. The review queue is the pipeline's safety net, so the code that
    drains it must not have a crash path through it (anti-goal #8).
    """


@dataclass(frozen=True)
class AdjudicatorConfig:
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
    def from_env(cls, env: Mapping[str, str] | None = None) -> AdjudicatorConfig:
        """Read the endpoint from the environment, adjudicator-specific
        variables first, then the mapper's, then the SDK's own.

        The chain is what lets ADR 012's conformity mitigation apply here
        too: pointing the adjudicator at a different model is one env var,
        and saying nothing keeps it on the mapper's endpoint.
        """
        source = os.environ if env is None else env
        api_key = (
            source.get("ADJUDICATOR_API_KEY")
            or source.get("SCHEMA_MAPPER_API_KEY")
            or source.get("ANTHROPIC_API_KEY")
        )
        if not api_key:
            raise AdjudicatorConfigError(
                "no adjudication API key: set ADJUDICATOR_API_KEY, "
                "SCHEMA_MAPPER_API_KEY, or ANTHROPIC_API_KEY"
            )
        mapper_model = source.get("SCHEMA_MAPPER_MODEL") or _DEFAULT_MODEL
        model = source.get("ADJUDICATOR_MODEL") or mapper_model
        # Mapper prices transfer only when this role runs the mapper's model
        # (see the same rule in VerifierConfig.from_env).
        same_engine = model == mapper_model
        factors = cache_factors_for(model)
        return cls(
            api_key=api_key,
            model=model,
            base_url=(
                source.get("ADJUDICATOR_BASE_URL")
                or source.get("SCHEMA_MAPPER_BASE_URL")
                or source.get("ANTHROPIC_BASE_URL")
            ),
            usd_per_mtok_in=Decimal(
                source.get("ADJUDICATOR_USD_PER_MTOK_IN")
                or (source.get("SCHEMA_MAPPER_USD_PER_MTOK_IN") if same_engine else None)
                or str(_DEFAULT_USD_PER_MTOK_IN)
            ),
            usd_per_mtok_out=Decimal(
                source.get("ADJUDICATOR_USD_PER_MTOK_OUT")
                or (source.get("SCHEMA_MAPPER_USD_PER_MTOK_OUT") if same_engine else None)
                or str(_DEFAULT_USD_PER_MTOK_OUT)
            ),
            max_output_tokens=int(
                source.get("ADJUDICATOR_MAX_OUTPUT_TOKENS") or _MAX_OUTPUT_TOKENS
            ),
            cache_read_factor=Decimal(
                source.get("ADJUDICATOR_CACHE_READ_FACTOR")
                or (source.get("SCHEMA_MAPPER_CACHE_READ_FACTOR") if same_engine else None)
                or str(factors.read)
            ),
            cache_write_factor=Decimal(
                source.get("ADJUDICATOR_CACHE_WRITE_FACTOR")
                or (source.get("SCHEMA_MAPPER_CACHE_WRITE_FACTOR") if same_engine else None)
                or str(factors.write)
            ),
        )


# ---------------------------------------------------------------------------
# Output schema (structured outputs)
# ---------------------------------------------------------------------------

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resolution": {"type": "string"},
        # The same PROVENANCE_SCHEMA the mapper's records cite under, and
        # checked by the same check_provenance: one citation law for the
        # whole semantic layer (ADR 012).
        "citations": {"type": "array", "items": PROVENANCE_SCHEMA},
        "confidence": {"type": "number"},
    },
    "required": ["resolution", "citations", "confidence"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_ADJUDICATOR_ROLE = """\
You adjudicate ONE open item from the review queue of a tax-table
ingestion pipeline.

Your input is the machine-extracted view of a single document — cell grids
(verbatim, row by row) and prose blocks (headings, body text, footnotes),
with page numbers, table ids, and per-page prose indexes — plus one queued
finding: its provenance coordinates, the raw value that triggered it, and
the reason it was queued. Re-examine that finding against the evidence and
propose how the item should be CLOSED.

The item carries a record (or a cell) the pipeline PROPOSED from this
document. Whether the fact table then accepted that record, you are not
told and cannot tell. Write nothing that assumes either answer.

You never modify a record, never re-run the mapping, and never invent a
value. You produce a proposal; someone else applies it.

## What to produce

1. "resolution": prose stating the correct disposition of this item, in
   terms a reviewer can act on. The register to aim for: "the dash at cell
   r3,c2 means no tax imposed, so the mapped null is correct; this item is
   dismissible", or "the disputed rate reads 0.062 at cell r1,c1 and the
   proposed record carries that same value; the mapping is correct as
   read". Describe what the DOCUMENT prints and what the pipeline
   PROPOSED — never say a record is stored, saved, in the database, or
   "correct as persisted", because for many items nothing was stored. If
   the evidence shows the pipeline got it WRONG, say so plainly and state
   what the document actually prints.
2. "citations": the specific cells and prose blocks that SETTLE the item —
   kind "cell" with table_id/row/col, or kind "prose" with page and
   prose_index; row, col, and prose_index are 0-based indexes into the
   input. Cite what you actually read, not the whole table. A resolution
   with no citations, or with a citation that names something this document
   does not contain, can never be applied automatically.
3. "confidence": your honest certainty in that disposition, 0 to 1.

## Hard rules

1. Every claim in the resolution must be readable in this input. This
   corpus is synthetic: outside knowledge of real-world tax law is wrong by
   definition, and a plausible real figure that is not printed here is not
   evidence, it is an invention.
2. If the printed evidence cannot settle the item — the cell is genuinely
   unreadable, the document contradicts itself, the answer depends on
   something the document never states — say exactly that and give a LOW
   confidence. "A human must look at this" is a correct output; a confident
   guess is the worst one, because the pipeline auto-closes what you claim
   to be confident about.
3. A queued item is not presumed wrong. Confirming that the pipeline read
   the page correctly is a resolution like any other, and needs the same
   citations.
4. Many items stand for records that never reached the fact table — a
   bracket that overlapped its neighbour, a natural key another document
   holds, a cell that could not be mapped. For those the open item is the
   record's only remaining trace, and the pipeline never auto-closes them
   however confident you are. Since the item does not tell you which case
   it is, write EVERY resolution so that it reads correctly either way:
   cite what the document prints, state what the correct value is, and say
   what a reviewer should do. A resolution whose only content is "the
   stored row is fine" is unusable — it is false wherever nothing was
   stored.
5. The conventions below define the canonical target the pipeline maps to.
   Judge the finding against them, not against a schema you would prefer.

"""

#: This role's envelope, for the same reason the verifier now states its
#: own: while output discipline lived inside the shared conventions, this
#: prompt ended by telling the adjudicator to put commentary in "issues" —
#: a key of the MAPPER's schema, which this schema forbids under
#: ``additionalProperties: False``. The adjudicator has not failed on it (it
#: names its three keys in prose above, which the verifier never did), but a
#: standing instruction to emit a forbidden key is the same latent defect
#: (ADR 014 §8d).
ADJUDICATOR_OUTPUT_DISCIPLINE = """\
## Output discipline

Return the JSON object and nothing else: no prose before it, no commentary
after it, no explanation of your reasoning. Numbers are JSON numbers, never
quoted strings.

The object has exactly three keys: "resolution", "citations" and
"confidence", as described at the top of this prompt. There is no "issues"
key and no "records" key in YOUR contract, whatever the conventions above
say about the mapper's; emitting one fails the contract outright.
"""

ADJUDICATOR_SYSTEM_PROMPT = (
    _ADJUDICATOR_ROLE + CANONICAL_CONVENTIONS + ADJUDICATOR_OUTPUT_DISCIPLINE
)

_DOCUMENT_HEADER = "## Extracted document\n"

_USER_INSTRUCTION = (
    "Adjudicate this review-queue item against the extracted document in "
    "your system context. Its raw_value is the record the pipeline PROPOSED, "
    "not evidence that the record was stored. Cite the cells and prose "
    "blocks that settle it.\n\n"
)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def _as_confidence(value: object) -> Decimal:
    """Confidence must be a number in [0, 1]. A boolean is not a number
    (``True == 1`` in Python, and an auto-resolution is exactly what a
    stray ``true`` would buy)."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise AdjudicatorError(
            f"adjudication confidence has unexpected type {type(value).__name__}"
        )
    confidence = Decimal(value)
    if not (Decimal(0) <= confidence <= Decimal(1)):
        raise AdjudicatorError(f"adjudication confidence {confidence} is outside [0, 1]")
    return confidence


def _as_citations(value: object) -> list[dict[str, Any]]:
    """Normalize the citation list without discarding anything.

    A non-dict entry is wrapped rather than dropped: it fails
    ``check_provenance`` (no ``kind``), so the proposal cannot auto-resolve,
    and the reviewer still sees what the model actually emitted.
    """
    if not isinstance(value, list):
        return [{"malformed_citations": json.dumps(value, default=str, ensure_ascii=False)}]
    return [
        entry
        if isinstance(entry, dict)
        else {"malformed_citation": json.dumps(entry, default=str, ensure_ascii=False)}
        for entry in value
    ]


def citations_are_valid(citations: list[dict[str, Any]], extracted: ExtractedDocument) -> bool:
    """True only when at least one citation exists and every one of them
    names a cell or prose block this document really has.

    Never raises: an invalid citation is a fact about the proposal, not a
    failure of the pass. It costs the item its auto-resolution, which is
    the entire point.
    """
    if not citations:
        return False
    try:
        check_provenance(list(citations), extracted)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def parse_adjudication_payload(
    text: str,
    *,
    item: ReviewItem,
    extracted: ExtractedDocument,
    cost: MappingCost | None = None,
) -> Adjudication:
    """Parse the model's JSON into an Adjudication, Decimal-safe.

    Envelope problems raise (there is nothing to store); citation problems
    do not (there is a proposal to store, it simply may not auto-resolve).
    Every raised error carries ``cost``: a malformed response was still a
    paid response, and the report must not show a failed call as free.
    """
    try:
        try:
            payload = loads_fence_tolerant(text, role=conformance.ADJUDICATOR, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise AdjudicatorError(f"adjudication response is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or not {"resolution", "citations", "confidence"} <= set(
            payload
        ):
            raise AdjudicatorError(
                "adjudication response JSON lacks the resolution/citations/confidence envelope"
            )

        resolution = payload["resolution"]
        if not isinstance(resolution, str) or not resolution.strip():
            raise AdjudicatorError("adjudication resolution is empty or not a string")

        citations = _as_citations(payload["citations"])
        return Adjudication(
            item_id=item.id,
            resolution=resolution,
            citations=citations,
            confidence=_as_confidence(payload["confidence"]),
            citations_valid=citations_are_valid(citations, extracted),
            cost=cost,
        )
    except AdjudicatorError as exc:
        if exc.cost is None:
            exc.cost = cost
        raise


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def _clip_reason(exc: Exception, limit: int = 160) -> str:
    """A failure reason short enough for a report line."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AnthropicAdjudicator:
    """Adjudicator over the Anthropic Messages API (or any endpoint that
    speaks it, such as the Vercel AI Gateway)."""

    def __init__(self, config: AdjudicatorConfig, *, client: Any | None = None) -> None:
        # ``client`` is injectable for tests; anything exposing
        # ``messages.stream(**kwargs)`` with a ``get_final_message()``
        # context manager qualifies.
        self._config = config
        self._client = client if client is not None else self._build_client(config)

    @staticmethod
    def _build_client(config: AdjudicatorConfig) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=_MAX_RETRIES,
            http_client=conformance.instrumented_http_client(
                conformance.ADJUDICATOR, timeout=_REQUEST_TIMEOUT_SECONDS
            ),
        )

    def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
        """One call, one item.

        The system turn carries TWO cached blocks: the role prompt, and the
        serialized document. The second breakpoint is what makes per-item
        adjudication affordable — this role is invoked once per queued item
        of the same document, so items 2..n read the whole grid/prose
        context out of cache at this provider's cache-read rate instead of
        resending it (a tenth of input on Anthropic, a fifth on z.ai, no
        discount at all where none is published — see ``adapters.pricing``).
        The itemized cost report shows that as cache-read tokens, which is
        why the token counts travel even when the model is unpriced.
        """
        conformance.LEDGER.record_call(conformance.ADJUDICATOR)
        try:
            adjudication = self._adjudicate(item, extracted)
            conformance.LEDGER.record_items(conformance.ADJUDICATOR, 1)
            return adjudication
        except AdjudicatorError as exc:
            # A contract failure means a body arrived and failed it: still an
            # item, unlike a transport failure below.
            conformance.LEDGER.record_items(conformance.ADJUDICATOR, 1)
            conformance.LEDGER.record_schema_failure(conformance.ADJUDICATOR, _clip_reason(exc))
            raise
        except Exception as exc:
            # No body ever arrived — a throttle that outlived the retry budget,
            # a timeout, a dropped connection. Counted apart from the
            # conformance rates: a call the model never answered says nothing
            # about whether the model can emit the contract, and folding it in
            # as a success flatters the number (found adversarially on
            # document 04, where 18 throttled adjudications were reporting as
            # 18 well-formed items).
            conformance.LEDGER.record_transport_failure(conformance.ADJUDICATOR, type(exc).__name__)
            raise

    def _adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
        started = time.perf_counter()
        with self._client.messages.stream(
            model=self._config.model,
            max_tokens=self._config.max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": ADJUDICATOR_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": _DOCUMENT_HEADER + serialize_document(extracted),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": _USER_INSTRUCTION + _serialize_item(item),
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        ) as stream:
            message = stream.get_final_message()

        # Cost is computed BEFORE the failure checks: a truncated or refused
        # response was still paid for, and the error must carry that spend.
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

        if message.stop_reason != "end_turn":
            raise AdjudicatorError(
                f"adjudication call ended with stop_reason={message.stop_reason!r}; "
                "refusing to parse a truncated or refused response",
                cost=cost,
            )
        text: str | None = None
        for block in message.content:
            candidate = getattr(block, "text", None) if block.type == "text" else None
            if candidate:
                text = candidate
                break
        if text is None:
            raise AdjudicatorError("adjudication response contains no text block", cost=cost)

        return parse_adjudication_payload(text, item=item, extracted=extracted, cost=cost)


def _serialize_item(item: ReviewItem) -> str:
    """The queued finding as deterministic JSON (UUIDs as strings). The item
    is all that changes between calls on one document, which is also what
    keeps the cached prefix stable."""
    return json.dumps(item.model_dump(mode="json"), ensure_ascii=False, indent=1)
