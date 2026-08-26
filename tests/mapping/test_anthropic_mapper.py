"""AnthropicSchemaMapper unit tests — fake client, no network.

The adapter's obligations under test:

- configuration comes from env (endpoint, key, model) so one adapter serves
  the direct Anthropic API and the Vercel AI Gateway;
- the serialized grid is verbatim and deterministic (merged cells stay null,
  empty cells stay "", abnormal cells are annotated, filenames never travel —
  tax_year must be unlearnable from anything but document content);
- no float ever touches a mapped value: JSON numbers parse straight to
  Decimal from their source text, exactly like the accuracy harness loader;
- a record the model proposes that fails canonical validation becomes a
  MappingIssue with provenance, never a crash and never a silent drop
  (anti-goal #8);
- cost is computed from reported usage at configured prices.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from tax_tables.adapters.anthropic_mapper import (
    AnthropicSchemaMapper,
    MapperConfig,
    MapperConfigError,
    MapperError,
    parse_mapping_payload,
    serialize_document,
)
from tax_tables.domain.records import FilingStatus, LifecycleStatus, RecordType
from tax_tables.extraction.model import (
    Cell,
    ExtractedDocument,
    ExtractedTable,
    ExtractionCost,
    ExtractionMethod,
    GridSource,
    PageExtraction,
    ProseBlock,
    ProseKind,
)

# ---------------------------------------------------------------------------
# Fixture document
# ---------------------------------------------------------------------------


def _document(*, table_confidence_cells: Decimal = Decimal(1)) -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 100.0, 50.0),
        grid_source=GridSource.RULED_LINES,
        rows=[
            [Cell(text="Rate"), Cell(text="Single"), Cell(text=None)],
            [
                Cell(text="10%"),
                Cell(text="$0 – $9,000", confidence=table_confidence_cells),
                Cell(text=""),
            ],
        ],
        column_count=3,
    )
    prose = ProseBlock(
        page_number=1,
        kind=ProseKind.FOOTNOTE,
        text="NOTE. A 3.8% surtax applies.",
        bbox=(0.0, 60.0, 100.0, 70.0),
    )
    page = PageExtraction(
        page_number=1,
        width=612.0,
        height=792.0,
        method=ExtractionMethod.DETERMINISTIC_TEXT,
        tables=[table],
        prose=[prose],
    )
    return ExtractedDocument(
        filename="01_federal_income_tax_rate_schedules_TY2026.pdf",
        sha256="ab" * 32,
        pages=[page],
        cost=ExtractionCost(engine="pdfplumber", wall_seconds=0.1),
    )


def _payload_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source_page": 1,
        "table_id": "p1_t0",
        "record_type": "ordinary_income_bracket",
        "jurisdiction": "US",
        "attribute_key": None,
        "filing_status": "single",
        "taxpayer_class": None,
        "tax_year": 2026,
        "effective_from": None,
        "effective_to": None,
        "lifecycle_status": "active",
        "lower_bound": 0,
        "upper_bound": 9000,
        "rate": 0.10,
        "amount": None,
        "currency": "USD",
        "confidence": 0.97,
        "source_table_label": "table_1",
        "extra_attrs": [],
        "provenance": [
            {
                "kind": "cell",
                "page": 1,
                "table_id": "p1_t0",
                "row": 1,
                "col": 0,
                "prose_index": None,
            }
        ],
    }
    record.update(overrides)
    return record


def _payload_text(*records: dict[str, Any], issues: list[dict[str, Any]] | None = None) -> str:
    return json.dumps({"records": list(records), "issues": issues or []})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestMapperConfig:
    def test_reads_anthropic_env_with_defaults(self) -> None:
        config = MapperConfig.from_env({"ANTHROPIC_API_KEY": "sk-test"})
        assert config.api_key == "sk-test"
        assert config.model == "claude-opus-5"
        assert config.base_url is None

    def test_mapper_specific_vars_win(self) -> None:
        config = MapperConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "sk-direct",
                "SCHEMA_MAPPER_API_KEY": "gw-key",
                "SCHEMA_MAPPER_BASE_URL": "https://ai-gateway.vercel.sh/v1/anthropic",
                "SCHEMA_MAPPER_MODEL": "anthropic/claude-opus-5",
            }
        )
        assert config.api_key == "gw-key"
        assert config.base_url == "https://ai-gateway.vercel.sh/v1/anthropic"
        assert config.model == "anthropic/claude-opus-5"

    def test_missing_key_is_an_error(self) -> None:
        with pytest.raises(MapperConfigError):
            MapperConfig.from_env({})

    def test_repr_never_renders_the_key(self) -> None:
        config = MapperConfig.from_env({"ANTHROPIC_API_KEY": "sk-secret-value"})
        assert "sk-secret-value" not in repr(config)
        assert "sk-secret-value" not in str(config)

    def test_max_output_tokens_from_env(self) -> None:
        config = MapperConfig.from_env(
            {"ANTHROPIC_API_KEY": "k", "SCHEMA_MAPPER_MAX_OUTPUT_TOKENS": "32000"}
        )
        assert config.max_output_tokens == 32000

    def test_price_overrides(self) -> None:
        config = MapperConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_USD_PER_MTOK_IN": "2.50",
                "SCHEMA_MAPPER_USD_PER_MTOK_OUT": "12.5",
            }
        )
        assert config.usd_per_mtok_in == Decimal("2.50")
        assert config.usd_per_mtok_out == Decimal("12.5")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerializeDocument:
    def test_grid_is_verbatim_with_null_and_empty_preserved(self) -> None:
        data = json.loads(serialize_document(_document()))
        table = data["pages"][0]["tables"][0]
        assert table["table_id"] == "p1_t0"
        assert table["rows"][0] == ["Rate", "Single", None]
        assert table["rows"][1] == ["10%", "$0 – $9,000", ""]

    def test_abnormal_cells_are_annotated(self) -> None:
        doc = _document(table_confidence_cells=Decimal("0.4"))
        data = json.loads(serialize_document(doc))
        notes = data["pages"][0]["tables"][0]["cell_notes"]
        assert {"row": 1, "col": 1, "confidence": "0.4"} in notes

    def test_prose_blocks_are_indexed_per_page(self) -> None:
        data = json.loads(serialize_document(_document()))
        block = data["pages"][0]["prose"][0]
        assert block["index"] == 0
        assert block["kind"] == "footnote"
        assert block["text"] == "NOTE. A 3.8% surtax applies."

    def test_filename_never_travels(self) -> None:
        text = serialize_document(_document())
        assert "TY2026" not in text
        assert "01_federal" not in text

    def test_deterministic(self) -> None:
        assert serialize_document(_document()) == serialize_document(_document())


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


class TestParseMappingPayload:
    def test_builds_decimal_records_without_float(self) -> None:
        result = parse_mapping_payload(
            _payload_text(
                _payload_record(
                    rate=0.1,
                    extra_attrs=[
                        {"key": "state_rate_pct", "value": 3.25},
                        {"key": "imposes_state_sales_tax", "value": True},
                    ],
                )
            ),
            extracted=_document(),
        )
        assert not result.issues
        (record,) = result.records
        assert isinstance(record.rate, Decimal)
        assert record.rate == Decimal("0.1")
        assert isinstance(record.attrs["state_rate_pct"], Decimal)
        assert record.attrs["state_rate_pct"] == Decimal("3.25")
        assert record.attrs["imposes_state_sales_tax"] is True

    def test_enums_dates_and_label_contract(self) -> None:
        result = parse_mapping_payload(
            _payload_text(
                _payload_record(
                    record_type="sales_tax_rate",
                    filing_status=None,
                    lower_bound=None,
                    upper_bound=None,
                    tax_year=None,
                    rate=None,
                    effective_from="2026-01-01",
                    source_table_label="table_1",
                )
            ),
            extracted=_document(),
        )
        (record,) = result.records
        assert record.record_type is RecordType.SALES_TAX_RATE
        assert record.effective_from == date(2026, 1, 1)
        assert record.lifecycle_status is LifecycleStatus.ACTIVE
        assert record.table_id == "p1_t0"  # extraction key space, unchanged
        assert record.attrs["source_table_label"] == "table_1"
        assert record.attrs["provenance"]  # traceability rides with the record

    def test_filing_status_enum(self) -> None:
        result = parse_mapping_payload(
            _payload_text(_payload_record(filing_status="qualifying_surviving_spouse")),
            extracted=_document(),
        )
        (record,) = result.records
        assert record.filing_status is FilingStatus.QUALIFYING_SURVIVING_SPOUSE

    def test_invalid_record_becomes_issue_not_crash(self) -> None:
        result = parse_mapping_payload(
            _payload_text(
                _payload_record(lower_bound=9000, upper_bound=100),  # inverted
                _payload_record(),  # valid — must survive its bad neighbour
            ),
            extracted=_document(),
        )
        assert len(result.records) == 1
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.source_page == 1
        assert issue.table_id == "p1_t0"
        assert "upper_bound" in issue.reason

    def test_non_integral_bound_becomes_issue(self) -> None:
        result = parse_mapping_payload(
            _payload_text(_payload_record(lower_bound=100.5)),
            extracted=_document(),
        )
        assert not result.records
        assert len(result.issues) == 1

    def test_attribute_key_mirrored_into_attrs(self) -> None:
        """The harness compares the sub-discriminator under its per-type
        field name out of attrs; the adapter must make natural-key identity
        imply that field's equality (found by adversarial review)."""
        result = parse_mapping_payload(
            _payload_text(
                _payload_record(
                    record_type="employment_tax_rate",
                    attribute_key="social_security",
                    filing_status=None,
                    lower_bound=None,
                    upper_bound=None,
                    rate=0.062,
                )
            ),
            extracted=_document(),
        )
        (record,) = result.records
        assert record.attrs["component"] == "social_security"

    def test_attribute_key_mirror_wins_over_model_supplied_extra(self) -> None:
        result = parse_mapping_payload(
            _payload_text(
                _payload_record(
                    record_type="wage_base",
                    attribute_key="social_security_wage_base",
                    filing_status=None,
                    lower_bound=None,
                    upper_bound=None,
                    rate=None,
                    amount=176100,
                    extra_attrs=[{"key": "item", "value": "a different spelling"}],
                )
            ),
            extracted=_document(),
        )
        (record,) = result.records
        assert record.attrs["item"] == "social_security_wage_base"

    def test_mirror_map_matches_the_harness(self) -> None:
        from tax_tables.domain.records import ATTRIBUTE_KEY_FIELD as DOMAIN_MAP
        from tests.accuracy.harness import ATTRIBUTE_KEY_FIELD as HARNESS_MAP

        assert dict(DOMAIN_MAP) == dict(HARNESS_MAP)

    def test_malformed_model_issue_degrades_never_aborts(self) -> None:
        """A model-emitted issue with out-of-range coordinates must not kill
        the document run (found by adversarial review)."""
        result = parse_mapping_payload(
            _payload_text(
                _payload_record(),
                issues=[
                    {
                        "source_page": 0,
                        "table_id": None,
                        "row_index": -1,
                        "col_index": -3,
                        "raw_value": None,
                        "reason": "dash cell ambiguous",
                    }
                ],
            ),
            extracted=_document(),
        )
        assert len(result.records) == 1  # the good record survives
        (issue,) = result.issues
        assert issue.source_page == 1
        assert issue.row_index is None
        assert issue.col_index is None
        assert issue.reason == "dash cell ambiguous"

    def test_model_issues_pass_through(self) -> None:
        result = parse_mapping_payload(
            _payload_text(
                issues=[
                    {
                        "source_page": 1,
                        "table_id": "p1_t0",
                        "row_index": 1,
                        "col_index": 2,
                        "raw_value": "??",
                        "reason": "cell is unreadable",
                    }
                ]
            ),
            extracted=_document(),
        )
        assert not result.records
        (issue,) = result.issues
        assert issue.row_index == 1
        assert issue.reason == "cell is unreadable"

    def test_confidence_floored_by_extraction_confidence(self) -> None:
        doc = _document(table_confidence_cells=Decimal("0.6"))
        table_conf = doc.pages[0].tables[0].confidence
        assert table_conf < Decimal("0.97")
        result = parse_mapping_payload(
            _payload_text(_payload_record(confidence=0.97)),
            extracted=doc,
        )
        (record,) = result.records
        assert record.confidence == table_conf


# ---------------------------------------------------------------------------
# map_document — fake client end to end
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
) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


def _config() -> MapperConfig:
    return MapperConfig(
        api_key="sk-test",
        model="claude-opus-5",
        base_url=None,
        usd_per_mtok_in=Decimal(5),
        usd_per_mtok_out=Decimal(25),
    )


class TestMapDocument:
    def test_maps_and_prices(self) -> None:
        client = _FakeClient(_message(_payload_text(_payload_record())))
        mapper = AnthropicSchemaMapper(_config(), client=client)
        result = mapper.map_document(_document())

        assert len(result.records) == 1
        cost = result.cost
        assert cost is not None
        assert cost.api_calls == 1
        assert cost.input_tokens == 10_000
        assert cost.output_tokens == 2_000
        # 10000/1M*5 + 4000/1M*5*1.25 + 2000/1M*25 = 0.05 + 0.025 + 0.05
        assert cost.usd == Decimal("0.125")
        assert cost.engine == "claude-opus-5"

    def test_request_shape(self) -> None:
        client = _FakeClient(_message(_payload_text(_payload_record())))
        AnthropicSchemaMapper(_config(), client=client).map_document(_document())

        (call,) = client.messages.calls
        assert call["model"] == "claude-opus-5"
        system = call["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        fmt = call["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["required"] == ["records", "issues"]
        # The serialized grid travels in the user turn.
        assert "p1_t0" in call["messages"][0]["content"]

    def test_truncated_response_raises(self) -> None:
        client = _FakeClient(_message(_payload_text(), stop_reason="max_tokens"))
        with pytest.raises(MapperError, match="max_tokens"):
            AnthropicSchemaMapper(_config(), client=client).map_document(_document())

    def test_malformed_json_raises(self) -> None:
        client = _FakeClient(_message("not json at all"))
        with pytest.raises(MapperError, match="JSON"):
            AnthropicSchemaMapper(_config(), client=client).map_document(_document())
