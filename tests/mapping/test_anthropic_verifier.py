"""AnthropicRecordVerifier unit tests — fake client, no network.

The adapter's obligations under test:

- configuration comes from env with its own ``RECORD_VERIFIER_*`` rung above
  the mapper's, so the verifier can run a cheaper or different-family model
  than the model it is checking (ADR 012's conformity-risk mitigation);
- the records the verifier sees carry their citations but NOT the mapper's
  self-assessed confidence or the review status derived from it — a second
  opinion anchored on the first one's belief is not independent;
- assembly is fail-closed: skipped, duplicated, contradictory and
  out-of-contract verdicts all end as disputes with the anomaly named, and a
  verdict that cannot protect any record is noted rather than guessed at;
- the verifier never repairs anything — its output is verdicts and prose;
- cost is computed from reported usage at configured prices, and a document
  with no mapped records spends nothing at all.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from tax_tables.adapters.anthropic_verifier import (
    AnthropicRecordVerifier,
    VerifierConfig,
    VerifierConfigError,
    VerifierError,
    parse_verification_payload,
    serialize_records,
)
from tax_tables.domain.records import (
    CanonicalRecord,
    FilingStatus,
    LifecycleStatus,
    RecordType,
    ReviewStatus,
)
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
from tax_tables.ports.mapper import MappingIssue, MappingResult
from tax_tables.ports.verifier import Verdict

# ---------------------------------------------------------------------------
# Fixtures — synthetic throughout; the oracle is never read (anti-goal #1)
# ---------------------------------------------------------------------------


def _document(*, cell_confidence: Decimal = Decimal(1)) -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 100.0, 50.0),
        grid_source=GridSource.RULED_LINES,
        rows=[
            [Cell(text="Rate"), Cell(text="Single"), Cell(text=None)],
            [
                Cell(text="10%"),
                Cell(text="$0 to $9,000", confidence=cell_confidence),
                Cell(text=""),
            ],
        ],
        column_count=3,
    )
    prose = ProseBlock(
        page_number=1,
        kind=ProseKind.BODY,
        text="This schedule applies to tax year 2026.",
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


def _provenance() -> list[dict[str, Any]]:
    return [
        {
            "kind": "cell",
            "page": 1,
            "table_id": "p1_t0",
            "row": 1,
            "col": 1,
            "prose_index": None,
        }
    ]


def _record(**overrides: Any) -> CanonicalRecord:
    fields: dict[str, Any] = {
        "source_page": 1,
        "table_id": "p1_t0",
        "record_type": RecordType.ORDINARY_INCOME_BRACKET,
        "jurisdiction": "US",
        "attribute_key": None,
        "filing_status": FilingStatus.SINGLE,
        "taxpayer_class": None,
        "tax_year": 2026,
        "lifecycle_status": LifecycleStatus.ACTIVE,
        "lower_bound": 0,
        "upper_bound": 9000,
        "rate": Decimal("0.10"),
        "amount": None,
        "currency": "USD",
        "attrs": {"source_table_label": "table_1", "provenance": _provenance()},
        "confidence": Decimal("0.97"),
        "review_status": ReviewStatus.CLEAN,
    }
    fields.update(overrides)
    return CanonicalRecord(**fields)


def _mapping(*records: CanonicalRecord, issues: list[MappingIssue] | None = None) -> MappingResult:
    return MappingResult(records=list(records), issues=issues or [])


def _verdicts_text(*verdicts: dict[str, Any]) -> str:
    return json.dumps({"verdicts": list(verdicts)})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestVerifierConfig:
    def test_reads_anthropic_env_with_defaults(self) -> None:
        config = VerifierConfig.from_env({"ANTHROPIC_API_KEY": "sk-test"})
        assert config.api_key == "sk-test"
        assert config.model == "claude-opus-5"
        assert config.base_url is None
        assert config.usd_per_mtok_in == Decimal(5)
        assert config.usd_per_mtok_out == Decimal(25)

    def test_falls_back_to_the_mappers_vars(self) -> None:
        """A single-key deployment must still work: the verifier borrows the
        mapper's endpoint when it has none of its own."""
        config = VerifierConfig.from_env(
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

    def test_verifier_specific_vars_win(self) -> None:
        """The rung that lets the verifier run a different model family than
        the mapper — the conformity-risk mitigation of ADR 012."""
        config = VerifierConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "sk-direct",
                "SCHEMA_MAPPER_API_KEY": "gw-key",
                "SCHEMA_MAPPER_MODEL": "anthropic/claude-opus-5",
                "SCHEMA_MAPPER_BASE_URL": "https://ai-gateway.vercel.sh/v1/anthropic",
                "RECORD_VERIFIER_API_KEY": "verifier-key",
                "RECORD_VERIFIER_MODEL": "claude-sonnet-4-5",
                "RECORD_VERIFIER_BASE_URL": "https://verifier.example/v1",
            }
        )
        assert config.api_key == "verifier-key"
        assert config.model == "claude-sonnet-4-5"
        assert config.base_url == "https://verifier.example/v1"

    def test_missing_key_is_an_error(self) -> None:
        with pytest.raises(VerifierConfigError):
            VerifierConfig.from_env({})

    def test_repr_never_renders_the_key(self) -> None:
        config = VerifierConfig.from_env({"RECORD_VERIFIER_API_KEY": "sk-secret-value"})
        assert "sk-secret-value" not in repr(config)
        assert "sk-secret-value" not in str(config)

    def test_output_budget_is_its_own_and_defaults_small(self) -> None:
        """Verdicts are small; the mapper's record-generation budget must not
        leak into the verifier by accident."""
        config = VerifierConfig.from_env(
            {"ANTHROPIC_API_KEY": "k", "SCHEMA_MAPPER_MAX_OUTPUT_TOKENS": "64000"}
        )
        assert config.max_output_tokens == 16_000

    def test_output_budget_from_verifier_env(self) -> None:
        config = VerifierConfig.from_env(
            {"ANTHROPIC_API_KEY": "k", "RECORD_VERIFIER_MAX_OUTPUT_TOKENS": "4000"}
        )
        assert config.max_output_tokens == 4_000

    def test_price_falls_back_to_mapper_prices(self) -> None:
        config = VerifierConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_USD_PER_MTOK_IN": "2.50",
                "SCHEMA_MAPPER_USD_PER_MTOK_OUT": "12.5",
            }
        )
        assert config.usd_per_mtok_in == Decimal("2.50")
        assert config.usd_per_mtok_out == Decimal("12.5")

    def test_verifier_prices_win(self) -> None:
        config = VerifierConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_USD_PER_MTOK_IN": "2.50",
                "SCHEMA_MAPPER_USD_PER_MTOK_OUT": "12.5",
                "RECORD_VERIFIER_USD_PER_MTOK_IN": "1",
                "RECORD_VERIFIER_USD_PER_MTOK_OUT": "5",
            }
        )
        assert config.usd_per_mtok_in == Decimal(1)
        assert config.usd_per_mtok_out == Decimal(5)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerializeRecords:
    def test_withholds_the_mappers_self_assessment(self) -> None:
        """The verifier judges the mapping, not the mapper's belief in it.

        Asserted on ``serialize_records`` output only: the extraction view a
        verification call also carries may legitimately mention cell
        confidence, which is evidence about the page, not about the mapper.
        """
        text = serialize_records([_record()])
        items = json.loads(text)
        assert "confidence" not in items[0]
        assert "review_status" not in items[0]
        assert "confidence" not in text
        assert "review_status" not in text

    def test_carries_index_values_and_citations(self) -> None:
        items = json.loads(
            serialize_records(
                [_record(), _record(filing_status=FilingStatus.MARRIED_FILING_JOINTLY)]
            )
        )
        assert [item["record_index"] for item in items] == [0, 1]
        first = items[0]
        assert first["table_id"] == "p1_t0"
        assert first["lower_bound"] == 0
        assert first["upper_bound"] == 9000
        assert first["attrs"]["source_table_label"] == "table_1"
        assert first["attrs"]["provenance"] == _provenance()

    def test_decimals_survive_exactly(self) -> None:
        """No float stands between the persisted value and the reviewed one."""
        items = json.loads(
            serialize_records(
                [_record(rate=Decimal("0.0924"), attrs={"state_rate_pct": Decimal("4.00")})]
            )
        )
        assert items[0]["rate"] == "0.0924"
        assert items[0]["attrs"]["state_rate_pct"] == "4.00"

    def test_deterministic(self) -> None:
        assert serialize_records([_record()]) == serialize_records([_record()])

    def test_empty_batch_serializes_to_an_empty_list(self) -> None:
        assert json.loads(serialize_records([])) == []


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


class TestParseVerificationPayload:
    def test_full_coverage_happy_path(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": 0, "verdict": "confirmed", "reason": None},
                {
                    "record_index": 1,
                    "verdict": "disputed",
                    "reason": "cell p1_t0 r1c1 reads '$0 to $9,000', not 12000",
                },
            ),
            record_count=2,
        )
        assert not notes
        assert [v.record_index for v in verdicts] == [0, 1]
        assert verdicts[0].verdict is Verdict.CONFIRMED
        assert verdicts[1].verdict is Verdict.DISPUTED
        assert "r1c1" in (verdicts[1].reason or "")

    def test_out_of_order_verdicts_are_sorted(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": 1, "verdict": "confirmed", "reason": None},
                {"record_index": 0, "verdict": "confirmed", "reason": None},
            ),
            record_count=2,
        )
        assert not notes
        assert [v.record_index for v in verdicts] == [0, 1]

    def test_missing_verdict_is_disputed_not_assumed(self) -> None:
        verdicts, _notes = parse_verification_payload(
            _verdicts_text({"record_index": 0, "verdict": "confirmed", "reason": None}),
            record_count=3,
        )
        assert [v.verdict for v in verdicts] == [
            Verdict.CONFIRMED,
            Verdict.DISPUTED,
            Verdict.DISPUTED,
        ]
        assert verdicts[1].reason == "verifier returned no verdict for this record"

    def test_out_of_range_index_is_noted_and_protects_nothing(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": 7, "verdict": "confirmed", "reason": None},
                {"record_index": -1, "verdict": "confirmed", "reason": None},
            ),
            record_count=1,
        )
        assert len(notes) == 2
        assert "7" in notes[0]
        (verdict,) = verdicts
        assert verdict.verdict is Verdict.DISPUTED

    def test_non_integer_index_is_noted(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": "0", "verdict": "confirmed", "reason": None},
                {"record_index": True, "verdict": "confirmed", "reason": None},
                {"record_index": None, "verdict": "confirmed", "reason": None},
            ),
            record_count=1,
        )
        assert len(notes) == 3  # a bool is not an index, whatever int() says
        (verdict,) = verdicts
        assert verdict.verdict is Verdict.DISPUTED
        assert verdict.reason == "verifier returned no verdict for this record"

    def test_non_object_verdict_is_noted(self) -> None:
        verdicts, notes = parse_verification_payload(
            json.dumps({"verdicts": ["confirmed"]}),
            record_count=1,
        )
        assert len(notes) == 1
        assert verdicts[0].verdict is Verdict.DISPUTED

    def test_malformed_verdict_value_disputes_and_notes(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": 0, "verdict": "probably fine", "reason": "looks right"}
            ),
            record_count=1,
        )
        assert len(notes) == 1
        (verdict,) = verdicts
        assert verdict.verdict is Verdict.DISPUTED
        assert "probably fine" in (verdict.reason or "")
        assert "looks right" in (verdict.reason or "")

    def test_disputed_without_a_reason_gets_one(self) -> None:
        verdicts, _notes = parse_verification_payload(
            _verdicts_text({"record_index": 0, "verdict": "disputed", "reason": "   "}),
            record_count=1,
        )
        assert verdicts[0].reason == "unspecified"

    def test_duplicate_agreeing_confirmations_collapse(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": 0, "verdict": "confirmed", "reason": None},
                {"record_index": 0, "verdict": "confirmed", "reason": None},
            ),
            record_count=1,
        )
        (verdict,) = verdicts
        assert verdict.verdict is Verdict.CONFIRMED
        assert len(notes) == 1
        assert "duplicate" in notes[0]

    def test_conflicting_duplicates_are_disputed_with_both_reasons(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": 0, "verdict": "confirmed", "reason": "bounds check out"},
                {"record_index": 0, "verdict": "disputed", "reason": "rate cell says 10%"},
            ),
            record_count=1,
        )
        (verdict,) = verdicts
        assert verdict.verdict is Verdict.DISPUTED
        reason = verdict.reason or ""
        assert "contradicted itself" in reason
        assert "bounds check out" in reason
        assert "rate cell says 10%" in reason
        assert len(notes) == 1

    def test_duplicate_disputes_stay_disputed(self) -> None:
        verdicts, notes = parse_verification_payload(
            _verdicts_text(
                {"record_index": 0, "verdict": "disputed", "reason": "first doubt"},
                {"record_index": 0, "verdict": "disputed", "reason": "second doubt"},
            ),
            record_count=1,
        )
        (verdict,) = verdicts
        assert verdict.verdict is Verdict.DISPUTED
        assert "first doubt" in (verdict.reason or "")
        assert "second doubt" in (verdict.reason or "")
        assert len(notes) == 1

    def test_not_json_raises(self) -> None:
        with pytest.raises(VerifierError, match="JSON"):
            parse_verification_payload("not json at all", record_count=1)

    def test_missing_envelope_raises(self) -> None:
        with pytest.raises(VerifierError, match="envelope"):
            parse_verification_payload(json.dumps({"results": []}), record_count=1)

    def test_non_list_envelope_raises(self) -> None:
        with pytest.raises(VerifierError, match="envelope"):
            parse_verification_payload(json.dumps({"verdicts": "confirmed"}), record_count=1)


# ---------------------------------------------------------------------------
# verify — fake client end to end
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


def _config() -> VerifierConfig:
    return VerifierConfig(
        api_key="sk-test",
        model="claude-opus-5",
        base_url=None,
        usd_per_mtok_in=Decimal(5),
        usd_per_mtok_out=Decimal(25),
    )


class TestVerify:
    def test_judges_and_prices(self) -> None:
        client = _FakeClient(
            _message(
                _verdicts_text(
                    {"record_index": 0, "verdict": "confirmed", "reason": None},
                    {"record_index": 1, "verdict": "disputed", "reason": "no such column"},
                )
            )
        )
        verifier = AnthropicRecordVerifier(_config(), client=client)
        result = verifier.verify(_document(), _mapping(_record(), _record()))

        assert [v.verdict for v in result.verdicts] == [Verdict.CONFIRMED, Verdict.DISPUTED]
        assert [v.record_index for v in result.disputed] == [1]
        assert not result.notes
        cost = result.cost
        assert cost is not None
        assert cost.api_calls == 1
        assert cost.input_tokens == 10_000
        assert cost.output_tokens == 2_000
        # 10000/1M*5 + 4000/1M*5*1.25 + 2000/1M*25 = 0.05 + 0.025 + 0.05
        assert cost.usd == Decimal("0.125")
        assert cost.engine == "claude-opus-5"

    def test_uncovered_record_comes_back_disputed(self) -> None:
        client = _FakeClient(
            _message(_verdicts_text({"record_index": 0, "verdict": "confirmed", "reason": None}))
        )
        result = AnthropicRecordVerifier(_config(), client=client).verify(
            _document(), _mapping(_record(), _record())
        )
        assert result.verdicts[1].verdict is Verdict.DISPUTED
        assert result.verdicts[1].reason == "verifier returned no verdict for this record"

    def test_request_shape(self) -> None:
        client = _FakeClient(
            _message(_verdicts_text({"record_index": 0, "verdict": "confirmed", "reason": None}))
        )
        AnthropicRecordVerifier(_config(), client=client).verify(_document(), _mapping(_record()))

        (call,) = client.messages.calls
        assert call["model"] == "claude-opus-5"
        assert call["max_tokens"] == 16_000
        system = call["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert "independent verifier" in system[0]["text"]
        fmt = call["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["required"] == ["verdicts"]
        content = call["messages"][0]["content"]
        # Both halves travel in the user turn: the same extraction view the
        # mapper saw, plus the records to refute.
        assert "p1_t0" in content
        assert '"record_index": 0' in content
        assert "This schedule applies to tax year 2026." in content

    def test_no_records_spends_nothing(self) -> None:
        client = _FakeClient(_message(_verdicts_text()))
        result = AnthropicRecordVerifier(_config(), client=client).verify(_document(), _mapping())
        assert result.verdicts == []
        assert result.cost is None
        assert client.messages.calls == []  # not one token of credit

    def test_truncated_response_raises(self) -> None:
        client = _FakeClient(_message(_verdicts_text(), stop_reason="max_tokens"))
        with pytest.raises(VerifierError, match="max_tokens"):
            AnthropicRecordVerifier(_config(), client=client).verify(
                _document(), _mapping(_record())
            )

    def test_malformed_json_raises(self) -> None:
        client = _FakeClient(_message("not json at all"))
        with pytest.raises(VerifierError, match="JSON"):
            AnthropicRecordVerifier(_config(), client=client).verify(
                _document(), _mapping(_record())
            )

    def test_response_without_a_text_block_raises(self) -> None:
        client = _FakeClient(
            SimpleNamespace(
                content=[SimpleNamespace(type="thinking", thinking="...")],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=1,
                    output_tokens=1,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
            )
        )
        with pytest.raises(VerifierError, match="no text block"):
            AnthropicRecordVerifier(_config(), client=client).verify(
                _document(), _mapping(_record())
            )
