"""AnthropicAdjudicator unit tests — fake client, no network.

The fixture models the case this role exists for: document 05's scanned
page, where the router refuses to trust a records-management stamp as a
text layer (tests/extraction/test_router.py) and the page comes back
through OCR with a smudged cell. That cell lands in the review queue at
``confidence_floor``, and the fact that settles it is printed in a footnote
the mapper never joined to the grid.

The adapter's obligations under test:

- configuration chains adjudicator-specific vars over the mapper's over the
  SDK's own, so this role can run on a different model without duplicating
  the endpoint (ADR 012's conformity mitigation);
- the document context is a SECOND cached system block, because this role
  is invoked once per queued item and items 2..n of a document must read
  the evidence out of cache;
- the proposal is returned even when its citations are missing or dangling
  — those cost the item its auto-resolution, never the pass (anti-goal #8);
- an envelope the pipeline cannot act on (truncated, unparseable, empty
  resolution, out-of-range confidence) raises instead of degrading into a
  proposal that reads as authoritative;
- no float ever touches the confidence the auto-resolve threshold is
  compared against;
- cost is computed from reported usage at configured prices.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace, TracebackType
from typing import Any
from uuid import UUID

import pytest

from tax_tables.adapters.anthropic_adjudicator import (
    AdjudicatorConfig,
    AdjudicatorConfigError,
    AdjudicatorError,
    AnthropicAdjudicator,
)
from tax_tables.extraction.model import (
    Cell,
    ExtractedDocument,
    ExtractedTable,
    ExtractionCost,
    ExtractionMethod,
    GridSource,
    OcrPageStats,
    PageExtraction,
    ProseBlock,
    ProseKind,
)
from tax_tables.ports.adjudicator import AdjudicationError, ReviewItem

ITEM_ID = UUID("11111111-1111-4111-8111-111111111111")
DOCUMENT_ID = UUID("22222222-2222-4222-8222-222222222222")

#: The stamp that makes this page look digital to a naive char count. It is
#: real text on a scanned page, and it settles nothing about the tax data.
STAMP = "Received by Records Management on 14 March 2026 Bates 000147"
#: The footnote that actually settles the smudged cell.
FOOTNOTE = "NOTE. The social security component is 6.2% of covered wages."


# ---------------------------------------------------------------------------
# Fixture document and item
# ---------------------------------------------------------------------------


def _document() -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 90.0, 300.0, 160.0),
        # A line grid found in the page image, then OCR per cell: the
        # shape the scanned fixture really produces.
        grid_source=GridSource.RULED_CELL_OCR,
        rows=[
            [Cell(text="Component"), Cell(text="Rate")],
            [
                Cell(text="Social security", confidence=Decimal("0.91")),
                # The smudge: read, but not confidently.
                Cell(text="6.2%", confidence=Decimal("0.42")),
            ],
        ],
        column_count=2,
    )
    page = PageExtraction(
        page_number=1,
        width=612.0,
        height=792.0,
        method=ExtractionMethod.OCR,
        tables=[table],
        prose=[
            ProseBlock(
                page_number=1,
                kind=ProseKind.BODY,
                text=STAMP,
                bbox=(30.0, 20.0, 400.0, 40.0),
                confidence=Decimal("0.88"),
            ),
            ProseBlock(
                page_number=1,
                kind=ProseKind.FOOTNOTE,
                text=FOOTNOTE,
                bbox=(30.0, 700.0, 400.0, 720.0),
                confidence=Decimal("0.95"),
            ),
        ],
        ocr_stats=OcrPageStats(
            word_count=124,
            mean_confidence=Decimal("0.87"),
            p10_confidence=Decimal("0.42"),
            low_confidence_fraction=Decimal("0.06"),
        ),
    )
    return ExtractedDocument(
        filename="05_payroll_tax_withholding_tables_TY2025.pdf",
        sha256="ef" * 32,
        pages=[page],
        cost=ExtractionCost(
            engine="vision-ocr", api_calls=1, usd=Decimal("0.02"), wall_seconds=4.1
        ),
    )


def _item(**overrides: Any) -> ReviewItem:
    values: dict[str, Any] = {
        "id": ITEM_ID,
        "document_id": DOCUMENT_ID,
        "source_page": 1,
        "table_id": "p1_t0",
        "row_index": 1,
        "col_index": 1,
        "raw_value": "6.2%",
        "reason": "confidence_floor: cell confidence 0.42 below 0.70",
    }
    values.update(overrides)
    return ReviewItem(**values)


def _cell_citation(*, table_id: str = "p1_t0", row: int = 1, col: int = 1) -> dict[str, Any]:
    return {
        "kind": "cell",
        "page": 1,
        "table_id": table_id,
        "row": row,
        "col": col,
        "prose_index": None,
    }


def _prose_citation(index: int = 1) -> dict[str, Any]:
    return {
        "kind": "prose",
        "page": 1,
        "table_id": None,
        "row": None,
        "col": None,
        "prose_index": index,
    }


RESOLUTION = (
    "The footnote states the social security component is 6.2% of covered "
    "wages, which matches the smudged cell at r1,c1; the mapped rate 0.062 "
    "is correct and this item is dismissible."
)


def _payload_text(
    *,
    resolution: str = RESOLUTION,
    # list[Any], not list[dict]: a model can emit anything here, and the
    # adapter's contract is that only the ENVELOPE may raise.
    citations: list[Any] | None = None,
    confidence: Any = 0.97,
) -> str:
    body: dict[str, Any] = {
        "resolution": resolution,
        "citations": [_cell_citation(), _prose_citation()] if citations is None else citations,
        "confidence": confidence,
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestAdjudicatorConfig:
    def test_reads_sdk_env_with_defaults(self) -> None:
        config = AdjudicatorConfig.from_env({"ANTHROPIC_API_KEY": "sk-test"})
        assert config.api_key == "sk-test"
        assert config.model == "claude-opus-5"
        assert config.base_url is None
        assert config.max_output_tokens == 8_000
        assert config.usd_per_mtok_in == Decimal(5)
        assert config.usd_per_mtok_out == Decimal(25)

    def test_mapper_vars_are_the_fallback(self) -> None:
        """Saying nothing about the adjudicator keeps it on the mapper's
        endpoint: one configured gateway serves both roles."""
        config = AdjudicatorConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "sk-direct",
                "SCHEMA_MAPPER_API_KEY": "gw-key",
                "SCHEMA_MAPPER_BASE_URL": "https://ai-gateway.vercel.sh/v1/anthropic",
                "SCHEMA_MAPPER_MODEL": "anthropic/claude-opus-5",
                "SCHEMA_MAPPER_USD_PER_MTOK_IN": "2.50",
                "SCHEMA_MAPPER_USD_PER_MTOK_OUT": "12.5",
            }
        )
        assert config.api_key == "gw-key"
        assert config.base_url == "https://ai-gateway.vercel.sh/v1/anthropic"
        assert config.model == "anthropic/claude-opus-5"
        assert config.usd_per_mtok_in == Decimal("2.50")
        assert config.usd_per_mtok_out == Decimal("12.5")

    def test_adjudicator_specific_vars_win(self) -> None:
        """ADR 012's conformity mitigation: this role may run on a cheaper
        or different-family model than the mapper."""
        config = AdjudicatorConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "sk-direct",
                "SCHEMA_MAPPER_API_KEY": "gw-key",
                "SCHEMA_MAPPER_MODEL": "anthropic/claude-opus-5",
                "SCHEMA_MAPPER_BASE_URL": "https://ai-gateway.vercel.sh/v1/anthropic",
                "SCHEMA_MAPPER_USD_PER_MTOK_IN": "5",
                "SCHEMA_MAPPER_USD_PER_MTOK_OUT": "25",
                "ADJUDICATOR_API_KEY": "adj-key",
                "ADJUDICATOR_MODEL": "claude-haiku-4-5",
                "ADJUDICATOR_BASE_URL": "https://adjudicator.example/v1",
                "ADJUDICATOR_USD_PER_MTOK_IN": "1",
                "ADJUDICATOR_USD_PER_MTOK_OUT": "5",
            }
        )
        assert config.api_key == "adj-key"
        assert config.model == "claude-haiku-4-5"
        assert config.base_url == "https://adjudicator.example/v1"
        assert config.usd_per_mtok_in == Decimal(1)
        assert config.usd_per_mtok_out == Decimal(5)

    def test_missing_key_is_an_error(self) -> None:
        with pytest.raises(AdjudicatorConfigError):
            AdjudicatorConfig.from_env({})

    def test_repr_never_renders_the_key(self) -> None:
        config = AdjudicatorConfig.from_env({"ADJUDICATOR_API_KEY": "sk-secret-value"})
        assert "sk-secret-value" not in repr(config)
        assert "sk-secret-value" not in str(config)

    def test_max_output_tokens_from_env(self) -> None:
        config = AdjudicatorConfig.from_env(
            {"ANTHROPIC_API_KEY": "k", "ADJUDICATOR_MAX_OUTPUT_TOKENS": "2000"}
        )
        assert config.max_output_tokens == 2000


# ---------------------------------------------------------------------------
# adjudicate — fake client end to end
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, message: Any) -> None:
        self._message = message

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._message


class _FakeMessages:
    def __init__(self, message: Any) -> None:
        self._message = message
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._message)


class _FakeClient:
    def __init__(self, message: Any) -> None:
        self.messages = _FakeMessages(message)


def _message(
    text: str,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 10_000,
    output_tokens: int = 2_000,
    cache_creation: int = 4_000,
    cache_read: int = 0,
    content: list[Any] | None = None,
) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)] if content is None else content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


def _config() -> AdjudicatorConfig:
    return AdjudicatorConfig(
        api_key="sk-test",
        model="claude-opus-5",
        base_url=None,
        usd_per_mtok_in=Decimal(5),
        usd_per_mtok_out=Decimal(25),
    )


def _adjudicate(message: Any, item: ReviewItem | None = None) -> tuple[Any, _FakeClient]:
    client = _FakeClient(message)
    adjudicator = AnthropicAdjudicator(_config(), client=client)
    return adjudicator.adjudicate(item or _item(), _document()), client


class TestRequestShape:
    def test_two_cached_system_blocks_carry_role_and_document(self) -> None:
        _, client = _adjudicate(_message(_payload_text()))

        (call,) = client.messages.calls
        assert call["model"] == "claude-opus-5"
        assert call["max_tokens"] == 8_000
        system = call["system"]
        assert len(system) == 2
        # Both breakpoints matter: the role prompt is shared across every
        # document, the document block across every item of one document.
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[1]["cache_control"] == {"type": "ephemeral"}
        assert "adjudicate ONE flagged item" in system[0]["text"]
        assert "## Extracted document" in system[1]["text"]
        assert "p1_t0" in system[1]["text"]
        assert FOOTNOTE in system[1]["text"]

    def test_user_turn_carries_the_item(self) -> None:
        _, client = _adjudicate(_message(_payload_text()))

        content = client.messages.calls[0]["messages"][0]["content"]
        assert str(ITEM_ID) in content
        assert "confidence_floor: cell confidence 0.42 below 0.70" in content
        assert '"raw_value": "6.2%"' in content

    def test_output_schema_is_enforced(self) -> None:
        _, client = _adjudicate(_message(_payload_text()))

        fmt = client.messages.calls[0]["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["required"] == ["resolution", "citations", "confidence"]
        assert fmt["schema"]["additionalProperties"] is False

    def test_only_one_call_per_item(self) -> None:
        _, client = _adjudicate(_message(_payload_text()))
        assert len(client.messages.calls) == 1


class TestAdjudicate:
    def test_returns_a_citated_proposal(self) -> None:
        adjudication, _ = _adjudicate(_message(_payload_text()))

        assert adjudication.item_id == ITEM_ID
        assert adjudication.resolution == RESOLUTION
        assert adjudication.citations == [_cell_citation(), _prose_citation()]
        assert adjudication.citations_valid is True

    def test_confidence_is_decimal_never_float(self) -> None:
        """The auto-resolve threshold is a Decimal comparison; a float here
        would decide an auto-close on binary rounding."""
        adjudication, _ = _adjudicate(_message(_payload_text(confidence=0.97)))
        assert isinstance(adjudication.confidence, Decimal)
        assert adjudication.confidence == Decimal("0.97")

    def test_footnote_only_citation_is_valid(self) -> None:
        adjudication, _ = _adjudicate(_message(_payload_text(citations=[_prose_citation(1)])))
        assert adjudication.citations_valid is True

    def test_prices_the_call(self) -> None:
        adjudication, _ = _adjudicate(_message(_payload_text()))

        cost = adjudication.cost
        assert cost is not None
        assert cost.engine == "claude-opus-5"
        assert cost.api_calls == 1
        assert cost.input_tokens == 10_000
        assert cost.output_tokens == 2_000
        assert cost.cache_write_tokens == 4_000
        # 10000/1M*5 + 4000/1M*5*1.25 + 2000/1M*25 = 0.05 + 0.025 + 0.05
        assert cost.usd == Decimal("0.125")
        assert cost.wall_seconds >= 0

    def test_second_item_reads_the_document_from_cache(self) -> None:
        """The whole reason the document is its own cached system block:
        item 2 of a document pays a tenth of the input price for the
        evidence, and the report can show it."""
        adjudication, _ = _adjudicate(
            _message(
                _payload_text(),
                input_tokens=200,
                output_tokens=1_000,
                cache_creation=0,
                cache_read=8_000,
            ),
            _item(reason="mapping: unreadable cell"),
        )
        cost = adjudication.cost
        assert cost is not None
        assert cost.cache_read_tokens == 8_000
        # 200/1M*5 + 8000/1M*5*0.1 + 1000/1M*25 = 0.001 + 0.004 + 0.025
        assert cost.usd == Decimal("0.03")


class TestCitationsAreNeverFatal:
    def test_unknown_table_id_is_not_valid(self) -> None:
        adjudication, _ = _adjudicate(
            _message(_payload_text(citations=[_cell_citation(table_id="p9_t9")]))
        )
        assert adjudication.citations_valid is False
        # The proposal survives for the human who now keeps the item.
        assert adjudication.resolution == RESOLUTION
        assert adjudication.confidence == Decimal("0.97")

    def test_row_outside_the_table_is_not_valid(self) -> None:
        adjudication, _ = _adjudicate(_message(_payload_text(citations=[_cell_citation(row=99)])))
        assert adjudication.citations_valid is False

    def test_prose_index_outside_the_page_is_not_valid(self) -> None:
        adjudication, _ = _adjudicate(_message(_payload_text(citations=[_prose_citation(7)])))
        assert adjudication.citations_valid is False

    def test_one_dangling_citation_taints_the_set(self) -> None:
        adjudication, _ = _adjudicate(
            _message(_payload_text(citations=[_prose_citation(1), _cell_citation(row=99)]))
        )
        assert adjudication.citations_valid is False
        assert len(adjudication.citations) == 2  # nothing dropped

    def test_empty_citation_list_is_not_valid(self) -> None:
        """An uncitated resolution is an opinion, not evidence: it may be
        stored, never applied."""
        adjudication, _ = _adjudicate(_message(_payload_text(citations=[])))
        assert adjudication.citations_valid is False
        assert adjudication.citations == []

    def test_non_dict_citation_is_wrapped_not_dropped(self) -> None:
        adjudication, _ = _adjudicate(_message(_payload_text(citations=["row 1 of the table"])))
        assert adjudication.citations_valid is False
        assert len(adjudication.citations) == 1
        assert "row 1 of the table" in json.dumps(adjudication.citations[0])


class TestMalformedResponses:
    def test_truncated_response_raises(self) -> None:
        with pytest.raises(AdjudicatorError, match="max_tokens"):
            _adjudicate(_message(_payload_text(), stop_reason="max_tokens"))

    def test_refusal_raises(self) -> None:
        with pytest.raises(AdjudicatorError, match="refusal"):
            _adjudicate(_message(_payload_text(), stop_reason="refusal"))

    def test_no_text_block_raises(self) -> None:
        with pytest.raises(AdjudicatorError, match="no text block"):
            _adjudicate(_message(_payload_text(), content=[SimpleNamespace(type="thinking")]))

    def test_non_json_raises(self) -> None:
        with pytest.raises(AdjudicatorError, match="JSON"):
            _adjudicate(_message("the cell reads 6.2%, so the record is fine"))

    def test_missing_envelope_key_raises(self) -> None:
        body = json.dumps({"resolution": RESOLUTION, "citations": [_prose_citation()]})
        with pytest.raises(AdjudicatorError, match="envelope"):
            _adjudicate(_message(body))

    def test_empty_resolution_raises(self) -> None:
        with pytest.raises(AdjudicatorError, match="resolution"):
            _adjudicate(_message(_payload_text(resolution="   ")))

    def test_non_string_resolution_raises(self) -> None:
        body = json.dumps(
            {
                "resolution": {"text": RESOLUTION},
                "citations": [_prose_citation()],
                "confidence": 0.9,
            }
        )
        with pytest.raises(AdjudicatorError, match="resolution"):
            _adjudicate(_message(body))

    @pytest.mark.parametrize("confidence", [1.2, -0.1, 2, -1])
    def test_confidence_outside_the_unit_interval_raises(self, confidence: float | int) -> None:
        with pytest.raises(AdjudicatorError, match="outside"):
            _adjudicate(_message(_payload_text(confidence=confidence)))

    def test_boolean_confidence_raises(self) -> None:
        """``True == 1`` in Python: a stray boolean would buy an
        auto-resolution at maximum confidence."""
        with pytest.raises(AdjudicatorError, match="unexpected type"):
            _adjudicate(_message(_payload_text(confidence=True)))

    def test_string_confidence_raises(self) -> None:
        with pytest.raises(AdjudicatorError, match="unexpected type"):
            _adjudicate(_message(_payload_text(confidence="0.95")))

    def test_failure_is_catchable_as_the_port_error(self) -> None:
        """The pipeline catches the PORT's error per item; the adapter's
        error must be one, or one bad response would abort the pass."""
        assert issubclass(AdjudicatorError, AdjudicationError)
        with pytest.raises(AdjudicationError):
            _adjudicate(_message("not json"))
