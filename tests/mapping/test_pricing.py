"""Provider-aware cache pricing.

The three semantic adapters used to bill every cached token at Anthropic's
ratios (read 0.1x input, write 1.25x). That is right for Anthropic and wrong
for the gateway model ids this project now runs, so the run's cost table would
have been wrong at first print. These tests pin the resolution ladder — env
override, exact model id, provider namespace, conservative fallback — and the
arithmetic it feeds.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from tax_tables.adapters.anthropic_adjudicator import AdjudicatorConfig
from tax_tables.adapters.anthropic_mapper import MapperConfig
from tax_tables.adapters.anthropic_verifier import AnthropicRecordVerifier, VerifierConfig
from tax_tables.adapters.pricing import ANTHROPIC_CACHE_FACTORS, cache_factors_for
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
)
from tax_tables.ports.mapper import MappingResult

GLM = "zai/glm-5.3-flash"
QWEN = "alibaba/qwen-3-235b"


class TestCacheFactorsFor:
    def test_bare_id_is_anthropics_own_api(self) -> None:
        assert cache_factors_for("claude-opus-5") == ANTHROPIC_CACHE_FACTORS

    def test_anthropic_namespace_on_the_gateway(self) -> None:
        assert cache_factors_for("anthropic/claude-haiku-4.5") == ANTHROPIC_CACHE_FACTORS

    def test_glm_reads_cost_a_fifth_not_a_tenth(self) -> None:
        """$0.015 cache read against $0.075 input in the gateway catalogue.
        Anthropic's 0.1x would under-report this by half."""
        assert cache_factors_for(GLM).read == Decimal("0.2")

    def test_glm_publishes_no_cache_write_price_so_writes_bill_at_full_rate(self) -> None:
        assert cache_factors_for(GLM).write == Decimal(1)

    def test_a_model_with_no_published_cache_price_gets_no_discount(self) -> None:
        """qwen-3-235b publishes neither cache price and yet the gateway does
        return cache_read_input_tokens for it. Inventing Anthropic's 90%
        discount there would under-report the verifier's spend."""
        factors = cache_factors_for(QWEN)
        assert factors.read == Decimal(1)
        assert factors.write == Decimal(1)

    def test_an_unknown_provider_is_over_reported_never_under(self) -> None:
        factors = cache_factors_for("some-vendor/some-model")
        assert factors.read == Decimal(1)
        assert factors.write == Decimal(1)


class TestMapperConfigResolvesFactors:
    def test_default_model_keeps_the_anthropic_ratios(self) -> None:
        config = MapperConfig.from_env({"ANTHROPIC_API_KEY": "k"})
        assert config.cache_read_factor == Decimal("0.1")
        assert config.cache_write_factor == Decimal("1.25")

    def test_the_configured_model_sets_the_factors_with_no_extra_env(self) -> None:
        """The point of the delta: correct at first print, not after someone
        remembers to add two more variables."""
        config = MapperConfig.from_env({"ANTHROPIC_API_KEY": "k", "SCHEMA_MAPPER_MODEL": GLM})
        assert config.cache_read_factor == Decimal("0.2")
        assert config.cache_write_factor == Decimal(1)

    def test_an_explicit_override_wins(self) -> None:
        config = MapperConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_MODEL": GLM,
                "SCHEMA_MAPPER_CACHE_READ_FACTOR": "0.33",
                "SCHEMA_MAPPER_CACHE_WRITE_FACTOR": "1.5",
            }
        )
        assert config.cache_read_factor == Decimal("0.33")
        assert config.cache_write_factor == Decimal("1.5")


class TestSecondaryRolesResolveTheirOwnFactors:
    def test_a_cross_family_verifier_does_not_inherit_the_mappers_factors(self) -> None:
        """The delta-2 configuration: GLM maps, qwen verifies. The verifier
        must price qwen's cache reads, not GLM's."""
        config = VerifierConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_MODEL": GLM,
                "RECORD_VERIFIER_MODEL": QWEN,
            }
        )
        assert config.model == QWEN
        assert config.cache_read_factor == Decimal(1)
        assert config.cache_write_factor == Decimal(1)

    def test_a_same_engine_role_inherits_the_mappers_override(self) -> None:
        config = VerifierConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_MODEL": GLM,
                "SCHEMA_MAPPER_CACHE_READ_FACTOR": "0.42",
            }
        )
        assert config.model == GLM
        assert config.cache_read_factor == Decimal("0.42")

    def test_a_cross_family_role_ignores_the_mappers_override(self) -> None:
        """Same rule the per-token prices already follow: another model's
        billing never transfers onto this one."""
        config = VerifierConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_MODEL": GLM,
                "SCHEMA_MAPPER_CACHE_READ_FACTOR": "0.42",
                "RECORD_VERIFIER_MODEL": QWEN,
            }
        )
        assert config.cache_read_factor == Decimal(1)

    def test_the_adjudicator_follows_the_same_ladder(self) -> None:
        config = AdjudicatorConfig.from_env(
            {
                "ANTHROPIC_API_KEY": "k",
                "SCHEMA_MAPPER_MODEL": GLM,
                "ADJUDICATOR_MODEL": "anthropic/claude-haiku-4.5",
            }
        )
        assert config.cache_read_factor == Decimal("0.1")
        assert config.cache_write_factor == Decimal("1.25")


# ---------------------------------------------------------------------------
# The arithmetic, end to end through an adapter
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


class _FakeMessages:
    def __init__(self, message: Any) -> None:
        self._message = message

    def stream(self, **kwargs: Any) -> _FakeStream:
        return _FakeStream(self._message)


class _FakeClient:
    def __init__(self, message: Any) -> None:
        self.messages = _FakeMessages(message)


def _document() -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 100.0, 50.0),
        grid_source=GridSource.RULED_LINES,
        rows=[[Cell(text="Rate"), Cell(text="Single")], [Cell(text="10%"), Cell(text="$0")]],
        column_count=2,
    )
    page = PageExtraction(
        page_number=1,
        width=612.0,
        height=792.0,
        method=ExtractionMethod.DETERMINISTIC_TEXT,
        tables=[table],
        prose=[],
    )
    return ExtractedDocument(
        filename="01_federal_income_tax_rate_schedules_TY2026.pdf",
        sha256="ab" * 32,
        pages=[page],
        cost=ExtractionCost(engine="pdfplumber", wall_seconds=0.1),
    )


def _record() -> CanonicalRecord:
    return CanonicalRecord(
        source_page=1,
        table_id="p1_t0",
        record_type=RecordType.ORDINARY_INCOME_BRACKET,
        jurisdiction="US",
        attribute_key=None,
        filing_status=FilingStatus.SINGLE,
        taxpayer_class=None,
        tax_year=2026,
        lifecycle_status=LifecycleStatus.ACTIVE,
        lower_bound=0,
        upper_bound=9000,
        rate=Decimal("0.10"),
        amount=None,
        currency="USD",
        attrs={"source_table_label": "table_1"},
        confidence=Decimal("0.97"),
        review_status=ReviewStatus.CLEAN,
    )


def _message(*, cache_read: int, cache_write: int) -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text=json.dumps(
                    {"verdicts": [{"record_index": 0, "verdict": "confirmed", "reason": None}]}
                ),
            )
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1_000,
            output_tokens=1_000,
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
        ),
    )


def _glm_config(**overrides: Any) -> VerifierConfig:
    fields: dict[str, Any] = {
        "api_key": "k",
        "model": GLM,
        "base_url": "https://ai-gateway.vercel.sh",
        "usd_per_mtok_in": Decimal("0.075"),
        "usd_per_mtok_out": Decimal("0.25"),
        "cache_read_factor": cache_factors_for(GLM).read,
        "cache_write_factor": cache_factors_for(GLM).write,
        "contract_retries": 0,
    }
    fields.update(overrides)
    return VerifierConfig(**fields)


class TestBilledArithmetic:
    def test_glm_cache_reads_bill_at_a_fifth_of_input(self) -> None:
        client = _FakeClient(_message(cache_read=100_000, cache_write=0))
        result = AnthropicRecordVerifier(_glm_config(), client=client).verify(
            _document(), MappingResult(records=[_record()], issues=[])
        )
        cost = result.cost
        assert cost is not None
        # 1000/1M*0.075 + 100000/1M*0.075*0.2 + 1000/1M*0.25
        #   = 0.000075 + 0.0015 + 0.00025
        assert cost.usd == Decimal("0.001825")

    def test_the_anthropic_ratio_would_have_under_reported_it(self) -> None:
        """Guard the regression this delta fixes, by pricing the same usage
        the old way and showing the two numbers differ."""
        client = _FakeClient(_message(cache_read=100_000, cache_write=0))
        old = AnthropicRecordVerifier(
            _glm_config(cache_read_factor=Decimal("0.1")), client=client
        ).verify(_document(), MappingResult(records=[_record()], issues=[]))
        old_cost = old.cost
        assert old_cost is not None
        assert old_cost.usd == Decimal("0.001075")
        assert old_cost.usd < Decimal("0.001825")

    def test_a_provider_with_no_published_write_price_bills_writes_at_full_input(self) -> None:
        client = _FakeClient(_message(cache_read=0, cache_write=100_000))
        result = AnthropicRecordVerifier(_glm_config(), client=client).verify(
            _document(), MappingResult(records=[_record()], issues=[])
        )
        cost = result.cost
        assert cost is not None
        # 1000/1M*0.075 + 100000/1M*0.075*1.0 + 1000/1M*0.25
        assert cost.usd == Decimal("0.007825")


class TestSourceTableLabelIsNullableNotStringified:
    """Document 04 persisted the literal string "None" into a provenance
    field, because the schema made `source_table_label` non-nullable and the
    builder did `str(raw[...])` unguarded. A record read from a body
    paragraph has no printed designator; inventing one is a manufactured
    value, which is the class anti-goal #8 exists to prevent."""

    def test_a_missing_label_becomes_null_not_the_string_none(self) -> None:
        from tax_tables.adapters.anthropic_mapper import _build_record

        raw = {
            "record_type": "sales_tax_rate",
            "jurisdiction": "US-AL",
            "attribute_key": None,
            "filing_status": None,
            "taxpayer_class": None,
            "tax_year": None,
            "lifecycle_status": "active",
            "lower_bound": None,
            "upper_bound": None,
            "rate": Decimal("0.0929"),
            "amount": None,
            "currency": None,
            "confidence": Decimal("0.99"),
            "source_table_label": None,
            "provenance": [{"kind": "cell", "table_id": "p1_t0", "row": 1, "col": 1, "page": 1}],
        }
        record = _build_record(raw, _document())
        assert record.attrs["source_table_label"] is None

    def test_a_present_label_still_survives(self) -> None:
        from tax_tables.adapters.anthropic_mapper import _build_record

        raw = {
            "record_type": "sales_tax_rate",
            "jurisdiction": "US-AL",
            "attribute_key": None,
            "filing_status": None,
            "taxpayer_class": None,
            "tax_year": None,
            "lifecycle_status": "active",
            "lower_bound": None,
            "upper_bound": None,
            "rate": Decimal("0.0929"),
            "amount": None,
            "currency": None,
            "confidence": Decimal("0.99"),
            "source_table_label": "table_a",
            "provenance": [{"kind": "cell", "table_id": "p1_t0", "row": 1, "col": 1, "page": 1}],
        }
        assert _build_record(raw, _document()).attrs["source_table_label"] == "table_a"
