"""The conformance ledger: what it counts, what it refuses to conflate, and
that the adapters actually feed it.

The measurement exists because the gateway forwards ``output_config`` without
enforcing it for non-Anthropic models, so schema conformance became a
probabilistic property that the run has to report as a rate rather than the
README asserting it as a caveat.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tax_tables.adapters.anthropic_mapper import (
    AnthropicSchemaMapper,
    MapperConfig,
    MapperError,
    parse_mapping_payload,
)
from tax_tables.adapters.anthropic_verifier import parse_verification_payload
from tax_tables.extraction.model import (
    Cell,
    ExtractedDocument,
    ExtractedTable,
    ExtractionCost,
    ExtractionMethod,
    GridSource,
    PageExtraction,
)
from tax_tables.observability import conformance
from tax_tables.observability.conformance import (
    ADJUDICATOR,
    MAPPER,
    VERIFIER,
    ConformanceLedger,
    format_conformance_report,
)


@pytest.fixture(autouse=True)
def _isolate_ledger() -> Any:
    """The process-wide ledger is shared state; every test starts empty and
    leaves it empty for the rest of the suite."""
    conformance.LEDGER.reset()
    yield
    conformance.LEDGER.reset()


class TestCounters:
    def test_a_clean_run_is_one_hundred_percent(self) -> None:
        ledger = ConformanceLedger()
        ledger.record_call(MAPPER)
        ledger.record_items(MAPPER, 128)
        counters = ledger.snapshot()[MAPPER]
        assert counters.call_conformance == 1.0
        assert counters.item_conformance == 1.0

    def test_a_schema_failure_lowers_the_call_rate_only(self) -> None:
        ledger = ConformanceLedger()
        for _ in range(4):
            ledger.record_call(MAPPER)
        ledger.record_items(MAPPER, 100)
        ledger.record_schema_failure(MAPPER, "response is not valid JSON")
        counters = ledger.snapshot()[MAPPER]
        assert counters.call_conformance == 0.75
        assert counters.item_conformance == 1.0

    def test_a_malformed_item_lowers_the_item_rate_only(self) -> None:
        ledger = ConformanceLedger()
        ledger.record_call(MAPPER)
        ledger.record_items(MAPPER, 100)
        ledger.record_malformed_item(MAPPER, "unmappable record: ValidationError")
        counters = ledger.snapshot()[MAPPER]
        assert counters.call_conformance == 1.0
        assert counters.item_conformance == 0.99

    def test_rates_are_none_with_no_denominator(self) -> None:
        """A rate over zero calls must read as "-", never as 100%: no run is
        not the same claim as a clean run."""
        counters = ConformanceLedger().snapshot()
        assert counters == {}
        empty = ConformanceLedger()
        empty.record_items(MAPPER, 0)
        assert empty.snapshot() == {}


class TestRetriesAreNotConformance:
    def test_a_throttle_counts_as_a_retry_not_a_failure(self) -> None:
        """A 429 the SDK retried through says nothing about whether the model
        can emit the schema. The free tier threw these for a week; they must
        never depress the conformance rate."""
        ledger = ConformanceLedger()
        ledger.record_call(MAPPER)
        for status in (429, 429, 200):
            ledger.record_http_status(MAPPER, status)
        counters = ledger.snapshot()[MAPPER]
        assert counters.http_attempts == 3
        assert counters.retries == 2
        assert counters.schema_failures == 0
        assert counters.call_conformance == 1.0

    def test_server_errors_are_retryable_too(self) -> None:
        ledger = ConformanceLedger()
        for status in (500, 503, 408, 409, 200, 400):
            ledger.record_http_status(VERIFIER, status)
        assert ledger.snapshot()[VERIFIER].retries == 4

    def test_the_hook_reads_the_status_and_never_the_body(self) -> None:
        ledger = ConformanceLedger()
        hook = conformance.response_hook(MAPPER, ledger)

        class _Response:
            status_code = 429

            @property
            def text(self) -> str:  # pragma: no cover - must never be reached
                raise AssertionError("the hook must not touch a streaming body")

        hook(_Response())
        assert ledger.snapshot()[MAPPER].retries == 1


class TestReport:
    def test_empty_ledger_renders_nothing(self) -> None:
        assert format_conformance_report(ConformanceLedger()) == ""

    def test_roles_render_in_pipeline_order_with_rates(self) -> None:
        ledger = ConformanceLedger()
        for role in (ADJUDICATOR, VERIFIER, MAPPER):
            ledger.record_call(role)
            ledger.record_items(role, 10)
        ledger.record_malformed_item(MAPPER, "unmappable record: ValidationError")
        report = format_conformance_report(ledger)
        lines = report.splitlines()
        body = [line.split()[0] for line in lines if line.split() and line.split()[0] in ROLE_SET]
        assert body == [MAPPER, VERIFIER, ADJUDICATOR]
        assert "90.0%" in report
        assert "unmappable record: ValidationError" in report


ROLE_SET = {MAPPER, VERIFIER, ADJUDICATOR}


# ---------------------------------------------------------------------------
# The adapters actually feed it
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, message: Any) -> None:
        self._message = message

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._message


class _FakeClient:
    def __init__(self, message: Any) -> None:
        outer = self

        class _Messages:
            def stream(self, **kwargs: Any) -> _FakeStream:
                return _FakeStream(outer._message)

        self._message = message
        self.messages = _Messages()


def _document() -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 100.0, 50.0),
        grid_source=GridSource.RULED_LINES,
        rows=[[Cell(text="Rate"), Cell(text="Single")], [Cell(text="10%"), Cell(text="$0")]],
        column_count=2,
    )
    return ExtractedDocument(
        filename="01_federal_income_tax_rate_schedules_TY2026.pdf",
        sha256="ab" * 32,
        pages=[
            PageExtraction(
                page_number=1,
                width=612.0,
                height=792.0,
                method=ExtractionMethod.DETERMINISTIC_TEXT,
                tables=[table],
                prose=[],
            )
        ],
        cost=ExtractionCost(engine="pdfplumber", wall_seconds=0.1),
    )


def _message(text: str, *, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=10,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _mapper_config() -> MapperConfig:
    return MapperConfig(api_key="k", model="zai/glm-5.3-flash", usd_per_mtok_in=Decimal("0.075"))


class TestMapperFeedsTheLedger:
    def test_a_body_that_is_not_json_is_one_schema_failure(self) -> None:
        client = _FakeClient(_message("this is prose, not the contract"))
        with pytest.raises(MapperError):
            AnthropicSchemaMapper(_mapper_config(), client=client).map_document(_document())
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.calls == 1
        assert counters.schema_failures == 1
        assert counters.call_conformance == 0.0

    def test_a_truncated_generation_is_a_schema_failure(self) -> None:
        client = _FakeClient(_message('{"records": [], "issues": []}', stop_reason="max_tokens"))
        with pytest.raises(MapperError):
            AnthropicSchemaMapper(_mapper_config(), client=client).map_document(_document())
        assert conformance.LEDGER.snapshot()[MAPPER].schema_failures == 1

    def test_a_conformant_empty_response_is_a_clean_call(self) -> None:
        client = _FakeClient(_message('{"records": [], "issues": []}'))
        AnthropicSchemaMapper(_mapper_config(), client=client).map_document(_document())
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.calls == 1
        assert counters.schema_failures == 0

    def test_an_unmappable_record_is_a_malformed_item_not_a_failed_call(self) -> None:
        """Anti-goal #8's review-queue item, counted. The call honoured the
        envelope; one item inside it did not."""
        payload = json.dumps({"records": [{"record_type": "nonsense"}], "issues": []})
        result = parse_mapping_payload(payload, extracted=_document())
        assert result.records == []
        assert len(result.issues) == 1
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.items == 1
        assert counters.malformed_items == 1
        assert counters.schema_failures == 0

    def test_a_model_authored_issue_is_not_a_conformance_miss(self) -> None:
        """The mapper raising an issue about a cell it could not read is the
        contract working. Counting it would make honesty look like failure."""
        payload = json.dumps(
            {
                "records": [],
                "issues": [
                    {
                        "source_page": 1,
                        "table_id": "p1_t0",
                        "row_index": 1,
                        "col_index": 1,
                        "raw_value": "—",
                        "reason": "dash is ambiguous here",
                    }
                ],
            }
        )
        result = parse_mapping_payload(payload, extracted=_document())
        assert len(result.issues) == 1
        # Zero records proposed, so no item denominator and nothing malformed.
        assert conformance.LEDGER.snapshot().get(MAPPER) is None


class TestVerifierFeedsTheLedger:
    def test_a_skipped_record_is_a_malformed_item(self) -> None:
        verdicts, _ = parse_verification_payload(
            json.dumps({"verdicts": [{"record_index": 0, "verdict": "confirmed"}]}),
            record_count=2,
        )
        assert len(verdicts) == 2
        counters = conformance.LEDGER.snapshot()[VERIFIER]
        assert counters.items == 2
        assert counters.malformed_items == 1

    def test_a_verdict_outside_the_batch_is_a_malformed_item(self) -> None:
        parse_verification_payload(
            json.dumps(
                {
                    "verdicts": [
                        {"record_index": 0, "verdict": "confirmed"},
                        {"record_index": 9, "verdict": "confirmed"},
                    ]
                }
            ),
            record_count=1,
        )
        assert conformance.LEDGER.snapshot()[VERIFIER].malformed_items == 1
