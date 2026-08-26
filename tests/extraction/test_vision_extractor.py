"""Vercel OCR adapter: a vision model reads the rendered page (ADR 010).

Built keyless, like the mapper: a fake client returns recorded/synthetic
response shapes, so every fidelity and fail-closed rule is exercised without
a credential. Live verification joins the 2b-live push — what cannot be
tested here is whether a real model *obeys* the prompt, not whether this
module handles what comes back.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from tax_tables.adapters.vision_extractor import (
    AnthropicVisionExtractor,
    VisionOcrConfig,
    VisionOcrConfigError,
    VisionOcrError,
    parse_page_payload,
)
from tax_tables.extraction.model import ExtractionMethod, GridSource, ProseKind
from tests.api.conftest import tiny_pdf


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
    input_tokens: int = 3_000,
    output_tokens: int = 900,
) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _config() -> VisionOcrConfig:
    return VisionOcrConfig(
        api_key="sk-test",
        model="claude-opus-5",
        usd_per_mtok_in=Decimal(5),
        usd_per_mtok_out=Decimal(25),
    )


def _cell(text: str | None, confidence: float = 1.0, ink: bool = False) -> dict[str, Any]:
    return {"text": text, "confidence": confidence, "ink_without_text": ink}


#: A recorded-shape response modelled on fixture 05: a two-level header with a
#: merged cell, a genuinely blank cell, an unreadable region, and a NOTE block
#: that is the only place its surtax rate appears.
RECORDED = {
    "tables": [
        {
            "bbox": [0.1, 0.2, 0.9, 0.6],
            "rows": [
                [_cell("Taxable income"), _cell("Rate"), _cell(None)],
                [_cell("$0 to $48,000"), _cell("0"), _cell("")],
                [_cell("$48,001 and over"), _cell("15"), _cell("", 0.0, True)],
            ],
            "irregular_rows": [],
        }
    ],
    "prose": [
        {"text": "Capital Gains Rates", "bbox": [0.1, 0.05, 0.9, 0.1], "confidence": 0.99},
        {
            "text": "NOTE. An additional surtax of 3.8 percent applies.",
            "bbox": [0.1, 0.7, 0.9, 0.8],
            "confidence": 0.93,
        },
    ],
}


class TestConfig:
    def test_role_key_wins_then_falls_back_to_the_shared_one(self) -> None:
        assert VisionOcrConfig.from_env({"VISION_OCR_API_KEY": "a"}).api_key == "a"
        assert VisionOcrConfig.from_env({"ANTHROPIC_API_KEY": "b"}).api_key == "b"
        both = VisionOcrConfig.from_env({"VISION_OCR_API_KEY": "a", "ANTHROPIC_API_KEY": "b"})
        assert both.api_key == "a"

    def test_no_key_is_a_config_error_naming_the_variables(self) -> None:
        with pytest.raises(VisionOcrConfigError, match="VISION_OCR_API_KEY"):
            VisionOcrConfig.from_env({})

    def test_the_credential_never_renders(self) -> None:
        """anti-goal #10: a traceback carrying the config must not leak it."""
        assert "sk-test" not in repr(_config())


class TestFidelity:
    def _page(self) -> Any:
        return parse_page_payload(json.dumps(RECORDED), page_number=1, width=2550, height=3300)

    def test_merged_continuation_and_empty_cell_stay_distinct(self) -> None:
        """The one distinction the mapper's serializer actually reads: null
        means 'spanned by a merge', "" means 'exists and is blank'."""
        rows = self._page().tables[0].rows
        assert rows[0][2].text is None
        assert rows[1][2].text == ""

    def test_unreadable_ink_is_flagged_not_dropped(self) -> None:
        cell = self._page().tables[0].rows[2][2]
        assert cell.text == "" and cell.confidence == 0 and cell.ink_without_text is True
        assert self._page().tables[0].flagged_cell_count == 1

    def test_values_are_verbatim(self) -> None:
        rows = self._page().tables[0].rows
        assert rows[1][1].text == "0"  # not "0%", not 0.0
        assert rows[2][0].text == "$48,001 and over"

    def test_prose_travels_with_its_kind(self) -> None:
        """Doc 05's surtax rate exists only in a NOTE block; losing prose
        loses records."""
        prose = self._page().prose
        assert [b.kind for b in prose] == [ProseKind.HEADING, ProseKind.FOOTNOTE]
        assert "3.8 percent" in prose[1].text

    def test_grid_source_records_the_engine(self) -> None:
        assert self._page().tables[0].grid_source is GridSource.VISION_MODEL
        assert self._page().method is ExtractionMethod.OCR

    def test_ragged_rows_are_recorded_not_forced(self) -> None:
        payload = {
            "tables": [
                {
                    "bbox": [0, 0, 1, 1],
                    "rows": [[_cell("a"), _cell("b")], [_cell("c")]],
                    "irregular_rows": [],
                }
            ],
            "prose": [],
        }
        table = parse_page_payload(
            json.dumps(payload), page_number=2, width=100, height=100
        ).tables[0]
        assert table.column_count == 2
        assert table.irregular_row_indexes == [1]


class TestGeometry:
    def test_normalized_boxes_scale_to_image_pixels(self) -> None:
        table = parse_page_payload(
            json.dumps(RECORDED), page_number=1, width=1000, height=2000
        ).tables[0]
        assert table.bbox == (100.0, 400.0, 900.0, 1200.0)

    @pytest.mark.parametrize("bad", [[0.9, 0.2, 0.1, 0.6], [0, 0, 2, 1], "nope", [1, 2, 3], None])
    def test_an_unusable_box_degrades_to_the_page_never_raises(self, bad: Any) -> None:
        """Geometry here is model-estimated and advisory. A bad rectangle is
        metadata being wrong; it must never cost a table (anti-goal #8)."""
        payload = {
            "tables": [{"bbox": bad, "rows": [[_cell("a")]], "irregular_rows": []}],
            "prose": [],
        }
        table = parse_page_payload(
            json.dumps(payload), page_number=1, width=800, height=600
        ).tables[0]
        assert table.bbox == (0.0, 0.0, 800.0, 600.0)
        assert table.rows[0][0].text == "a"


class TestPageStats:
    def test_word_count_counts_real_tokens_not_the_models_claim(self) -> None:
        """The dropout guard: a page that comes back confident and mostly
        empty must be catchable by coverage."""
        stats = parse_page_payload(
            json.dumps(RECORDED), page_number=1, width=100, height=100
        ).ocr_stats
        assert stats is not None
        # Table rows contribute 3 + 4 + 4 = 11 tokens (merged-continuation
        # and empty cells contribute nothing); the two prose blocks add
        # 3 + 8. Counted from the text, never from the model's own claim.
        assert stats.word_count == 22
        assert stats.low_confidence_fraction > 0  # the zero-confidence cell


class TestFailsClosed:
    @pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "stop_sequence"])
    def test_a_truncated_or_refused_read_raises(self, stop_reason: str) -> None:
        client = _FakeClient(_message(json.dumps(RECORDED), stop_reason=stop_reason))
        extractor = AnthropicVisionExtractor(_config(), client=client)
        with pytest.raises(VisionOcrError, match="stop_reason"):
            extractor.extract_pages(tiny_pdf(text="scan"), [1])

    def test_a_response_with_no_text_block_raises(self) -> None:
        message = SimpleNamespace(
            content=[SimpleNamespace(type="image", text=None)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        extractor = AnthropicVisionExtractor(_config(), client=_FakeClient(message))
        with pytest.raises(VisionOcrError, match="no text block"):
            extractor.extract_pages(tiny_pdf(text="scan"), [1])

    def test_non_json_raises(self) -> None:
        with pytest.raises(VisionOcrError, match="not JSON"):
            parse_page_payload("I could not read this page.", page_number=1, width=1, height=1)

    def test_a_table_with_no_rows_raises_rather_than_vanishing(self) -> None:
        payload = {
            "tables": [{"bbox": [0, 0, 1, 1], "rows": [], "irregular_rows": []}],
            "prose": [],
        }
        with pytest.raises(VisionOcrError, match="no rows"):
            parse_page_payload(json.dumps(payload), page_number=1, width=1, height=1)

    def test_a_non_string_cell_value_raises(self) -> None:
        payload = {
            "tables": [
                {
                    "bbox": [0, 0, 1, 1],
                    "rows": [[{"text": 12.5, "confidence": 1}]],
                    "irregular_rows": [],
                }
            ],
            "prose": [],
        }
        with pytest.raises(VisionOcrError, match="neither a string nor null"):
            parse_page_payload(json.dumps(payload), page_number=1, width=1, height=1)


class TestCostAndTransport:
    def test_one_call_per_page_priced_from_reported_usage(self) -> None:
        client = _FakeClient(_message(json.dumps(RECORDED)))
        extractor = AnthropicVisionExtractor(_config(), client=client)
        batch = extractor.extract_pages(tiny_pdf(pages=2, text="scan"), [1, 2])
        assert batch.api_calls == 2
        assert len(batch.pages) == 2
        # (3000 * 5 + 900 * 25) / 1e6 per page, twice.
        per_page = (Decimal(3_000) * Decimal(5) + Decimal(900) * Decimal(25)) / Decimal(1_000_000)
        assert batch.usd == per_page * 2

    def test_the_page_is_sent_as_an_image_with_the_schema_enforced(self) -> None:
        client = _FakeClient(_message(json.dumps(RECORDED)))
        AnthropicVisionExtractor(_config(), client=client).extract_pages(tiny_pdf(text="scan"), [1])
        (call,) = client.messages.calls
        blocks = call["messages"][0]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["media_type"] == "image/png"
        assert call["output_config"]["format"]["type"] == "json_schema"

    def test_engine_names_the_model(self) -> None:
        extractor = AnthropicVisionExtractor(_config(), client=_FakeClient(_message("{}")))
        assert extractor.engine == "vision:claude-opus-5"
