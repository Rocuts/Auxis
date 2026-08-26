"""Bedrock adapter unit tests — fake clients, no network, no AWS credentials.

What these pin is the hexagonal claim rather than a second implementation:
the AWS column of the ports table must be the *same* three adapters behind a
SigV4-signing client, so the obligations under test are the seams, not the
semantics.

- each factory returns the existing Anthropic adapter class, and does so with
  no API key anywhere in the environment (the SigV4 sentinel is what makes
  the shared config classes satisfiable on a target that has no key);
- the Bedrock model id — vendor-prefixed, unlike the direct API's — reaches
  the request and the cost report for all three roles, with the per-role
  ``*_MODEL`` overrides working exactly as on every other target;
- the config rules the direct-API adapters enforce survive the swap: an
  explicit key beats the sentinel, mapper prices do not follow a verifier
  pointed at a different model, and no config renders the sentinel;
- the client is constructed for the resolved region with the timeout and
  retry budget of the role that will use it, and a missing region is an
  error rather than a guessed default;
- the reused parsers work unchanged behind a Bedrock-shaped client: one
  happy path per role, through hand-built synthetic fixtures.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any
from uuid import uuid4

import anthropic
import pytest

from tax_tables.adapters.anthropic_adjudicator import AdjudicatorConfig, AnthropicAdjudicator
from tax_tables.adapters.anthropic_mapper import AnthropicSchemaMapper, MapperConfig
from tax_tables.adapters.anthropic_verifier import AnthropicRecordVerifier, VerifierConfig
from tax_tables.adapters.bedrock import (
    BEDROCK_DEFAULT_MODEL,
    SIGV4_SENTINEL,
    BedrockConfigError,
    _bedrock_env,
    _region,
    bedrock_adjudicator,
    bedrock_mapper,
    bedrock_verifier,
)
from tax_tables.domain.records import CanonicalRecord, FilingStatus, RecordType
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
from tax_tables.ports.adjudicator import ReviewItem
from tax_tables.ports.mapper import MappingResult
from tax_tables.ports.verifier import Verdict

# A model id that is deliberately NOT the default, for the override tests.
_OTHER_MODEL = "anthropic.claude-haiku-5"


# ---------------------------------------------------------------------------
# Synthetic fixtures (no fixture PDF, no ground truth — see anti-goal #1)
# ---------------------------------------------------------------------------


def _document() -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 100.0, 50.0),
        grid_source=GridSource.RULED_LINES,
        rows=[
            [Cell(text="Rate"), Cell(text="Single")],
            [Cell(text="10%"), Cell(text="0 to 9,000")],
        ],
        column_count=2,
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
        filename="synthetic.pdf",
        sha256="cd" * 32,
        pages=[page],
        cost=ExtractionCost(engine="pdfplumber", wall_seconds=0.1),
    )


def _cell_ref(row: int = 1, col: int = 0) -> dict[str, Any]:
    return {
        "kind": "cell",
        "page": 1,
        "table_id": "p1_t0",
        "row": row,
        "col": col,
        "prose_index": None,
    }


def _mapper_payload() -> str:
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
        "confidence": 0.96,
        "source_table_label": "table_1",
        "extra_attrs": [],
        "provenance": [_cell_ref()],
    }
    return json.dumps({"records": [record], "issues": []})


def _record() -> CanonicalRecord:
    return CanonicalRecord(
        source_page=1,
        table_id="p1_t0",
        record_type=RecordType.ORDINARY_INCOME_BRACKET,
        jurisdiction="US",
        filing_status=FilingStatus.SINGLE,
        tax_year=2026,
        lower_bound=0,
        upper_bound=9000,
        rate=Decimal("0.10"),
        currency="USD",
        attrs={"source_table_label": "table_1", "provenance": [_cell_ref()]},
        confidence=Decimal("0.96"),
    )


def _mapping() -> MappingResult:
    return MappingResult(records=[_record()], issues=[])


def _verifier_payload() -> str:
    return json.dumps({"verdicts": [{"record_index": 0, "verdict": "confirmed", "reason": None}]})


def _review_item() -> ReviewItem:
    return ReviewItem(
        id=uuid4(),
        document_id=uuid4(),
        source_page=1,
        table_id="p1_t0",
        row_index=1,
        col_index=1,
        raw_value="0 to 9,000",
        reason="bracket bounds read from a single cell",
    )


def _adjudicator_payload() -> str:
    return json.dumps(
        {
            "resolution": "cell r1,c1 reads '0 to 9,000'; the persisted bounds match it",
            "citations": [_cell_ref(row=1, col=1)],
            "confidence": 0.95,
        }
    )


# ---------------------------------------------------------------------------
# Fake client (own copy — nothing is imported from another test module)
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
    """Anything with ``messages.stream(**kwargs)`` returning a context manager
    with ``get_final_message()`` satisfies the adapters — which is precisely
    why ``anthropic.AnthropicBedrock`` can be dropped in unmodified."""

    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(_message(text))


def _message(text: str) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1_000,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


class TestFactories:
    """Each factory returns the existing adapter, built from an environment
    with no API key at all: the sentinel is what makes that possible."""

    def test_mapper_is_the_anthropic_adapter(self) -> None:
        mapper = bedrock_mapper({}, client=_FakeClient(_mapper_payload()))
        assert isinstance(mapper, AnthropicSchemaMapper)

    def test_verifier_is_the_anthropic_adapter(self) -> None:
        verifier = bedrock_verifier({}, client=_FakeClient(_verifier_payload()))
        assert isinstance(verifier, AnthropicRecordVerifier)

    def test_adjudicator_is_the_anthropic_adapter(self) -> None:
        adjudicator = bedrock_adjudicator({}, client=_FakeClient(_adjudicator_payload()))
        assert isinstance(adjudicator, AnthropicAdjudicator)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


class TestModelResolution:
    def test_default_model_matches_the_stack(self) -> None:
        """The default must equal ``BEDROCK_MODEL_ID`` in the CDK stack: the
        stack grants and configures that id, so a divergence would mean the
        synthesized template describes a different model from the one the
        code calls. Parsed out of the stack source so the check needs no CDK
        install."""
        stack_source = (
            Path(__file__).resolve().parents[2] / "infra" / "tax_tables_stack.py"
        ).read_text()
        constants = {
            target.id: node.value.value
            for node in ast.parse(stack_source).body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert constants["BEDROCK_MODEL_ID"] == BEDROCK_DEFAULT_MODEL

    def test_mapper_calls_the_default_model(self) -> None:
        client = _FakeClient(_mapper_payload())
        result = bedrock_mapper({}, client=client).map_document(_document())
        (call,) = client.messages.calls
        assert call["model"] == BEDROCK_DEFAULT_MODEL
        # The Bedrock id also reaches the itemized cost report.
        assert result.cost is not None
        assert result.cost.engine == BEDROCK_DEFAULT_MODEL

    def test_verifier_inherits_the_default_model(self) -> None:
        """No ``RECORD_VERIFIER_MODEL``: the verifier's chain falls back to
        ``SCHEMA_MAPPER_MODEL``, which is why one default covers all three."""
        client = _FakeClient(_verifier_payload())
        bedrock_verifier({}, client=client).verify(_document(), _mapping())
        (call,) = client.messages.calls
        assert call["model"] == BEDROCK_DEFAULT_MODEL

    def test_adjudicator_inherits_the_default_model(self) -> None:
        client = _FakeClient(_adjudicator_payload())
        bedrock_adjudicator({}, client=client).adjudicate(_review_item(), _document())
        (call,) = client.messages.calls
        assert call["model"] == BEDROCK_DEFAULT_MODEL

    def test_per_role_override_applies_to_that_role_only(self) -> None:
        """ADR 012's conformity mitigation, unchanged on AWS: the verifier
        can be pointed at another model by one env var, and the mapper stays
        where it was."""
        env = {"RECORD_VERIFIER_MODEL": _OTHER_MODEL}

        verifier_client = _FakeClient(_verifier_payload())
        bedrock_verifier(env, client=verifier_client).verify(_document(), _mapping())
        (verifier_call,) = verifier_client.messages.calls
        assert verifier_call["model"] == _OTHER_MODEL

        mapper_client = _FakeClient(_mapper_payload())
        bedrock_mapper(env, client=mapper_client).map_document(_document())
        (mapper_call,) = mapper_client.messages.calls
        assert mapper_call["model"] == BEDROCK_DEFAULT_MODEL

    def test_explicit_mapper_model_overrides_the_bedrock_default(self) -> None:
        env = _bedrock_env({"SCHEMA_MAPPER_MODEL": _OTHER_MODEL})
        assert MapperConfig.from_env(env).model == _OTHER_MODEL


# ---------------------------------------------------------------------------
# The SigV4 sentinel
# ---------------------------------------------------------------------------


class TestSigV4Sentinel:
    def test_sentinel_satisfies_the_shared_configs(self) -> None:
        env = _bedrock_env({})
        assert env["SCHEMA_MAPPER_API_KEY"] == SIGV4_SENTINEL == "sigv4"
        # All three roles resolve; none of them would without a key.
        assert MapperConfig.from_env(env).api_key == SIGV4_SENTINEL
        assert VerifierConfig.from_env(env).api_key == SIGV4_SENTINEL
        assert AdjudicatorConfig.from_env(env).api_key == SIGV4_SENTINEL

    def test_explicit_key_wins_over_the_sentinel(self) -> None:
        """``setdefault`` semantics: the sentinel is a floor, never an
        override. A deployment that does supply a key keeps it."""
        env = _bedrock_env({"SCHEMA_MAPPER_API_KEY": "sk-real-value"})
        assert MapperConfig.from_env(env).api_key == "sk-real-value"

    def test_role_specific_key_still_wins(self) -> None:
        env = _bedrock_env({"RECORD_VERIFIER_API_KEY": "sk-verifier-value"})
        assert VerifierConfig.from_env(env).api_key == "sk-verifier-value"
        # ... and the mapper is unaffected by the verifier's key.
        assert MapperConfig.from_env(env).api_key == SIGV4_SENTINEL

    def test_no_config_renders_the_sentinel(self) -> None:
        """``api_key`` is ``repr=False`` on all three configs. The sentinel is
        not secret, but a config that printed it would print a real key in the
        deployment that supplies one (anti-goal #10)."""
        env = _bedrock_env({})
        for config in (
            MapperConfig.from_env(env),
            VerifierConfig.from_env(env),
            AdjudicatorConfig.from_env(env),
        ):
            assert SIGV4_SENTINEL not in repr(config)
            assert SIGV4_SENTINEL not in str(config)


# ---------------------------------------------------------------------------
# Price gating carries over
# ---------------------------------------------------------------------------


class TestPriceGating:
    def test_verifier_on_another_model_does_not_inherit_mapper_prices(self) -> None:
        """The same-engine rule of ``VerifierConfig.from_env``: mapper prices
        describe the mapper's model, so a verifier pointed elsewhere falls to
        the role default instead of being billed at the mapper's rate."""
        env = _bedrock_env(
            {
                "SCHEMA_MAPPER_USD_PER_MTOK_IN": "2.50",
                "RECORD_VERIFIER_MODEL": _OTHER_MODEL,
            }
        )
        verifier = VerifierConfig.from_env(env)
        assert MapperConfig.from_env(env).usd_per_mtok_in == Decimal("2.50")
        assert verifier.usd_per_mtok_in != Decimal("2.50")
        # It is the role default, not some other inherited value.
        assert (
            verifier.usd_per_mtok_in
            == VerifierConfig.from_env(
                {"RECORD_VERIFIER_API_KEY": "k", "RECORD_VERIFIER_MODEL": _OTHER_MODEL}
            ).usd_per_mtok_in
        )

    def test_verifier_on_the_mapper_model_does_inherit(self) -> None:
        env = _bedrock_env({"SCHEMA_MAPPER_USD_PER_MTOK_IN": "2.50"})
        assert VerifierConfig.from_env(env).usd_per_mtok_in == Decimal("2.50")

    def test_adjudicator_on_another_model_does_not_inherit(self) -> None:
        env = _bedrock_env(
            {"SCHEMA_MAPPER_USD_PER_MTOK_OUT": "12.5", "ADJUDICATOR_MODEL": _OTHER_MODEL}
        )
        assert AdjudicatorConfig.from_env(env).usd_per_mtok_out != Decimal("12.5")


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------


class TestRegion:
    def test_missing_region_is_an_error_naming_both_variables(self) -> None:
        with pytest.raises(BedrockConfigError) as excinfo:
            _region({})
        message = str(excinfo.value)
        assert "AWS_REGION" in message
        assert "AWS_DEFAULT_REGION" in message

    def test_aws_region_wins(self) -> None:
        assert (
            _region({"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "eu-west-1"}) == "us-east-1"
        )

    def test_default_region_is_the_fallback(self) -> None:
        assert _region({"AWS_DEFAULT_REGION": "eu-west-1"}) == "eu-west-1"

    def test_empty_region_is_not_a_region(self) -> None:
        with pytest.raises(BedrockConfigError):
            _region({"AWS_REGION": ""})


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _recording_bedrock(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand in for ``anthropic.AnthropicBedrock`` and record its kwargs. No
    AWS call happens at construction time either way — this pins what would
    be handed to the real client."""
    calls: list[dict[str, Any]] = []

    class _Recorder:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)
            self.messages = _FakeMessages(_message("{}"))

    monkeypatch.setattr(anthropic, "AnthropicBedrock", _Recorder)
    return calls


class TestClientConstruction:
    def test_mapper_client_signs_for_the_configured_region(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _recording_bedrock(monkeypatch)
        bedrock_mapper({"AWS_REGION": "eu-central-1"})
        (kwargs,) = calls
        assert kwargs["aws_region"] == "eu-central-1"
        # The mapper's own _REQUEST_TIMEOUT_SECONDS / max_retries=3.
        assert kwargs["timeout"] == 900.0
        assert kwargs["max_retries"] == 3
        # No API key is passed: SigV4 is the credential.
        assert "api_key" not in kwargs

    def test_verifier_client_uses_the_verifier_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _recording_bedrock(monkeypatch)
        bedrock_verifier({"AWS_DEFAULT_REGION": "us-west-2"})
        (kwargs,) = calls
        assert kwargs["aws_region"] == "us-west-2"
        assert kwargs["timeout"] == 900.0

    def test_adjudicator_client_uses_the_shorter_adjudicator_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-item adjudication has its own, smaller budget upstream; the
        Bedrock client must carry that one, not the mapper's."""
        calls = _recording_bedrock(monkeypatch)
        bedrock_adjudicator({"AWS_REGION": "us-east-1"})
        (kwargs,) = calls
        assert kwargs["timeout"] == 300.0
        assert kwargs["max_retries"] == 3

    def test_no_client_is_built_when_one_is_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An injected client means no region is needed and no AWS client is
        constructed — which is what keeps this suite credential-free."""
        calls = _recording_bedrock(monkeypatch)
        bedrock_mapper({}, client=_FakeClient(_mapper_payload()))
        assert calls == []

    def test_missing_region_fails_before_any_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _recording_bedrock(monkeypatch)
        with pytest.raises(BedrockConfigError):
            bedrock_mapper({})


# ---------------------------------------------------------------------------
# End to end through a Bedrock-shaped client
# ---------------------------------------------------------------------------


class TestReusedParsersBehindBedrock:
    """The payoff: the parsers, schemas and cost formulas are the existing
    ones, so a Bedrock-shaped client produces the same domain objects."""

    def test_mapper_produces_a_canonical_record(self) -> None:
        client = _FakeClient(_mapper_payload())
        result = bedrock_mapper({}, client=client).map_document(_document())

        assert not result.issues
        (record,) = result.records
        assert record.record_type is RecordType.ORDINARY_INCOME_BRACKET
        assert record.filing_status is FilingStatus.SINGLE
        assert record.rate == Decimal("0.10")  # Decimal, never float
        assert record.tax_year == 2026
        assert record.attrs["provenance"]

        (call,) = client.messages.calls
        assert call["output_config"]["format"]["type"] == "json_schema"

    def test_verifier_produces_one_verdict(self) -> None:
        client = _FakeClient(_verifier_payload())
        result = bedrock_verifier({}, client=client).verify(_document(), _mapping())

        (verdict,) = result.verdicts
        assert verdict.record_index == 0
        assert verdict.verdict is Verdict.CONFIRMED
        assert not result.notes
        assert result.cost is not None
        assert result.cost.engine == BEDROCK_DEFAULT_MODEL

    def test_adjudicator_produces_a_citated_adjudication(self) -> None:
        item = _review_item()
        client = _FakeClient(_adjudicator_payload())
        adjudication = bedrock_adjudicator({}, client=client).adjudicate(item, _document())

        assert adjudication.item_id == item.id
        assert adjudication.confidence == Decimal("0.95")
        # The citation resolves against the extracted document, so this
        # proposal is eligible to auto-resolve.
        assert adjudication.citations_valid is True
        assert adjudication.cost is not None
        assert adjudication.cost.engine == BEDROCK_DEFAULT_MODEL
