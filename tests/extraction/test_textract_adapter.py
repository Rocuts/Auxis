"""Textract adapter against the hand-constructed document 05 response.

There is no AWS account and no budget, so nothing here calls Textract. What
it does exercise is the whole adapter: the real pypdfium2 render of the real
scanned fixture, the real request shape, and a client that replays
``fixtures/textract/05_response.json`` — a **HAND-CONSTRUCTED** response, not
a recording, and a test below pins that label in place so the distinction
cannot quietly rot into a claim the project did not earn (CLAUDE.md: record
a real response "if credentials ever become available"; until then,
hand-construct one and label it as such).

That fixture's content is document 05's own printed content, transcribed
from a local OCR pass over the PDF. So the assertions here are the ones that
matter on the scan itself: the stub column survives, bounds read verbatim
with this document's "to" separator, the SUPERSEDED banner and the NOTE
block reach prose (the 3.8 percent surtax rate exists *only* there), and a
merged header behaves like a merged header.

Two rules are asserted rather than described, because getting either wrong
is silent data loss (anti-goal #8):

* a cell's confidence is the **minimum** of its words, never the mean — the
  fixture's weakest cell reads 0.55 where its mean would launder to 0.68;
* a malformed block raises. A CELL pointing at a WORD the response does not
  contain is a fault; a shorter table with no complaint is exactly the
  failure mode this design exists to prevent.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tax_tables.adapters.textract_extractor import TextractExtractor
from tax_tables.extraction.model import (
    ExtractedTable,
    ExtractionMethod,
    GridSource,
    PageExtraction,
    ProseKind,
)
from tax_tables.ports.extractor import PageBatch

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SCANNED_DOC = FIXTURES / "05_capital_gains_preferential_rates_TY2025.pdf"
RESPONSE = FIXTURES / "textract" / "05_response.json"
GENERATOR = FIXTURES / "gen_textract_fixture.py"

#: 612x792pt rendered at 300 DPI. pdfium rounds the height up by a pixel;
#: the adapter reports the image's own dimensions rather than a computed
#: ideal, because that is the frame Textract's geometry is defined against.
RENDER_SIZE = (2550.0, 3301.0)


class _Replay:
    """Stands in for a boto3 Textract client.

    Records every request so the tests can assert what would have gone over
    the wire, and answers with the committed response. No credentials, no
    network, no boto3 — the adapter's client is injected precisely so a
    keyless machine can exercise all of it.
    """

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def analyze_document(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self.response


@pytest.fixture(scope="module")
def response() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(RESPONSE.read_text(encoding="utf-8"))
    return payload


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    return SCANNED_DOC.read_bytes()


@pytest.fixture(scope="module")
def batch(response: dict[str, Any], pdf_bytes: bytes) -> PageBatch:
    return TextractExtractor(client=_Replay(response)).extract_pages(pdf_bytes, [1])


@pytest.fixture(scope="module")
def page(batch: PageBatch) -> PageExtraction:
    return batch.pages[0]


def _generator() -> ModuleType:
    """Import the fixture generator by path: ``fixtures/`` is deliberately not
    a package (it never ships in any bundle)."""
    spec = importlib.util.spec_from_file_location("gen_textract_fixture", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the generator's dataclass resolves its own
    # postponed annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _texts(table: ExtractedTable, row: int) -> list[str | None]:
    return [cell.text for cell in table.rows[row]]


def _column(table: ExtractedTable, column: int) -> list[str | None]:
    return [row[column].text for row in table.rows]


def _words(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [block for block in response["Blocks"] if block["BlockType"] == "WORD"]


class TestProvenance:
    def test_the_fixture_says_what_it_is(self, response: dict[str, Any]) -> None:
        """The honesty pin. This file is not a recorded API response and must
        never be quoted as evidence that one was captured."""
        assert "HAND-CONSTRUCTED" in response["_provenance"]
        assert "NOT a recorded API response" in response["_provenance"]

    def test_the_generator_is_deterministic(self) -> None:
        generator = _generator()
        first: str = generator.render()
        second: str = generator.render()
        assert first == second

    def test_the_committed_file_is_the_generator_output(self) -> None:
        assert RESPONSE.read_text(encoding="utf-8") == _generator().render()


class TestCostAndRequests:
    def test_engine_name(self) -> None:
        assert TextractExtractor().engine == "textract"

    def test_one_call_and_one_page_of_spend(self, batch: PageBatch) -> None:
        # Textract is a hosted service and the only extraction on this corpus
        # that costs anything on any target: one page, one call, list price.
        assert batch.api_calls == 1
        assert batch.usd == Decimal("0.015")

    def test_price_is_configurable(self, response: dict[str, Any], pdf_bytes: bytes) -> None:
        extractor = TextractExtractor(client=_Replay(response), usd_per_page=Decimal("0.004"))
        assert extractor.extract_pages(pdf_bytes, [1]).usd == Decimal("0.004")

    def test_price_env_override_is_honored(
        self, response: dict[str, Any], pdf_bytes: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEXTRACT_USD_PER_PAGE", "0.0125")
        extractor = TextractExtractor(client=_Replay(response))
        assert extractor.extract_pages(pdf_bytes, [1]).usd == Decimal("0.0125")

    def test_every_page_is_its_own_call(self, response: dict[str, Any], pdf_bytes: bytes) -> None:
        # The synchronous API takes one page at a time, so the page is the
        # unit of work, of cost and of fan-out. Asking for the same page twice
        # must bill twice: pretending otherwise would understate the bill.
        client = _Replay(response)
        batch = TextractExtractor(client=client).extract_pages(pdf_bytes, [1, 1])
        assert len(client.requests) == 2
        assert (len(batch.pages), batch.api_calls, batch.usd) == (2, 2, Decimal("0.030"))

    def test_request_sends_a_rendered_png_and_asks_only_for_tables(
        self, response: dict[str, Any], pdf_bytes: bytes
    ) -> None:
        client = _Replay(response)
        TextractExtractor(client=client).extract_pages(pdf_bytes, [1])
        request = client.requests[0]
        assert request["FeatureTypes"] == ["TABLES"]
        assert request["Document"]["Bytes"].startswith(b"\x89PNG")


class TestPage:
    def test_page_reports_ocr_method_and_render_geometry(self, page: PageExtraction) -> None:
        assert page.method is ExtractionMethod.OCR
        assert page.page_number == 1
        assert (page.width, page.height) == RENDER_SIZE

    def test_exactly_two_tables(self, page: PageExtraction) -> None:
        assert len(page.tables) == 2
        assert [table.table_id for table in page.tables] == ["p1_t0", "p1_t1"]

    def test_grid_provenance_is_ocr(self, page: PageExtraction) -> None:
        assert {table.grid_source for table in page.tables} == {GridSource.RULED_CELL_OCR}

    def test_table_boxes_are_in_render_pixels(self, page: PageExtraction) -> None:
        for table in page.tables:
            x0, top, x1, bottom = table.bbox
            assert 0 <= x0 < x1 <= page.width, table.table_id
            assert 0 <= top < bottom <= page.height, table.table_id


class TestTableOne:
    def test_shape(self, page: PageExtraction) -> None:
        # Five rows, not the line-grid path's four: this response folds the
        # printed caption band into the table as a merged row, which is what
        # Textract's TABLES feature does with a caption sitting on the top
        # rule. Same content, different side of the grid boundary.
        table = page.tables[0]
        assert (table.row_count, table.column_count) == (5, 5)

    def test_merged_header_keeps_text_at_the_anchor(self, page: PageExtraction) -> None:
        caption = _texts(page.tables[0], 0)
        assert caption[0] == "Table 1. Preferential rate bands by filing status, taxable income"
        # The positions the span covers are continuations, not empties: the
        # same None-vs-"" distinction the digital adapter emits and the
        # mapper's serializer reads back.
        assert caption[1] is None
        assert caption[2] is None

    def test_cells_beyond_the_span_are_empty_not_continuations(self, page: PageExtraction) -> None:
        row = page.tables[0].rows[0]
        assert [cell.text for cell in row[3:]] == ["", ""]
        # Nothing to misread is not the same as read badly: an empty cell is
        # confident about being empty.
        assert [cell.confidence for cell in row[3:]] == [Decimal(1), Decimal(1)]

    def test_stub_column_survives(self, page: PageExtraction) -> None:
        assert _column(page.tables[0], 0)[1:] == ["Rate", "0 percent", "15 percent", "20 percent"]

    def test_header_names_the_filing_statuses(self, page: PageExtraction) -> None:
        header = _texts(page.tables[0], 1)
        assert header == [
            "Rate",
            "Single",
            "Married Filing Jointly",
            "Married Filing Separately",
            "Head of Household",
        ]

    def test_bracket_bounds_read_verbatim(self, page: PageExtraction) -> None:
        # This document separates bounds with "to", not the en dash the other
        # four use. Normalizing it here would hide the trap from the mapper.
        cells = [cell.text for row in page.tables[0].rows for cell in row]
        assert "$0 to $48,350" in cells
        assert "$48,351 to $533,400" in cells
        assert "Over $533,400" in cells

    def test_open_ended_top_bracket_present_for_every_status(self, page: PageExtraction) -> None:
        top = _texts(page.tables[0], 4)[1:]
        assert all(text is not None and text.startswith("Over $") for text in top), top


class TestTableTwo:
    def test_shape(self, page: PageExtraction) -> None:
        table = page.tables[1]
        assert (table.row_count, table.column_count) == (4, 2)

    def test_header(self, page: PageExtraction) -> None:
        assert _texts(page.tables[1], 0) == ["Category", "Maximum rate"]

    @pytest.mark.parametrize(
        ("category", "rate"),
        [
            ("Unrecaptured section 1250 gain", "25 percent"),
            ("Collectibles and certain small business stock", "28 percent"),
            ("Short-term capital gain", "Ordinary rates"),
        ],
    )
    def test_category_rate_pairs(self, page: PageExtraction, category: str, rate: str) -> None:
        pairs = [(row[0].text, row[1].text) for row in page.tables[1].rows]
        assert (category, rate) in pairs


class TestCellConfidence:
    def test_cell_confidence_is_the_minimum_of_its_words(
        self, page: PageExtraction, response: dict[str, Any]
    ) -> None:
        """The weakest cell on the page, and the point of the whole rule.

        Its three words read 95.1, 55.0, 55.0. The minimum, 0.55, puts the
        cell in front of a reviewer; the mean, 0.68, would let two badly read
        bracket bounds through on the strength of one clean word.
        """
        cell = page.tables[0].rows[2][1]
        assert cell.text == "$0 to $48,350"
        assert cell.confidence == Decimal("0.55")
        assert cell.confidence < Decimal("0.68")  # what a mean would have reported

    def test_every_other_cell_reads_healthy(self, page: PageExtraction) -> None:
        weak = [
            (table.table_id, r, c, cell.text, cell.confidence)
            for table in page.tables
            for r, row in enumerate(table.rows)
            for c, cell in enumerate(row)
            if cell.text and cell.confidence < Decimal("0.9")
        ]
        assert [entry[:3] for entry in weak] == [("p1_t0", 2, 1)], weak

    def test_no_cell_is_flagged_for_unread_ink(self, page: PageExtraction) -> None:
        # Textract reports no ink coverage, so the OCR honesty flag has no
        # evidence to stand on here and must never be invented.
        assert all(table.flagged_cell_count == 0 for table in page.tables)


class TestProse:
    def test_superseded_notice_is_captured(self, page: PageExtraction) -> None:
        # Losing this sentence loses the lifecycle status of the whole
        # document, and doc 05 must never surface in a tax_year=2026 query.
        banner = [block for block in page.prose if "SUPERSEDED" in block.text]
        assert len(banner) == 1
        assert banner[0].kind is ProseKind.BODY

    def test_successor_circular_is_named(self, page: PageExtraction) -> None:
        assert any("CG-2026/03" in block.text for block in page.prose)

    def test_tax_year_is_stated_in_prose(self, page: PageExtraction) -> None:
        # 'Tax Year 2025' appears in no table cell. Lose it and doc 05's tax
        # year is only guessable from the document id, which the brief
        # forbids as a source.
        assert any("Tax Year 2025" in block.text for block in page.prose)

    def test_note_block_carries_the_surtax_rate(self, page: PageExtraction) -> None:
        notes = [block for block in page.prose if block.kind is ProseKind.FOOTNOTE]
        assert notes, "no footnote block classified"
        assert any("3.8" in block.text for block in notes), [block.text for block in notes]

    def test_table_caption_left_outside_a_table_stays_prose(self, page: PageExtraction) -> None:
        headings = [block for block in page.prose if block.kind is ProseKind.HEADING]
        assert [block.text for block in headings] == ["Table 2. Special rate categories"]

    def test_prose_never_duplicates_table_content(self, page: PageExtraction) -> None:
        """A LINE whose centre falls inside a table is table content.

        Without that filter every cell's words would be shipped twice — once
        as a grid cell and once as a sentence — and the mapper would see a
        document that says everything twice.
        """
        prose = "\n".join(block.text for block in page.prose)
        duplicated = [
            cell.text
            for table in page.tables
            for row in table.rows
            for cell in row
            if cell.text and len(cell.text) > 6 and cell.text in prose
        ]
        assert not duplicated, duplicated

    def test_prose_blocks_are_positioned_inside_the_page(self, page: PageExtraction) -> None:
        for block in page.prose:
            x0, top, x1, bottom = block.bbox
            assert 0 <= x0 < x1 <= page.width, block.text[:60]
            assert 0 <= top < bottom <= page.height, block.text[:60]


class TestPageStats:
    def test_word_count_matches_the_response(
        self, page: PageExtraction, response: dict[str, Any]
    ) -> None:
        # The dropout guard: a run that reports fewer words than the response
        # carries has lost content somewhere between block and grid.
        assert page.ocr_stats is not None
        assert page.ocr_stats.word_count == len(_words(response))

    def test_tail_statistics_follow_the_authored_confidences(
        self, page: PageExtraction, response: dict[str, Any]
    ) -> None:
        words = _words(response)
        low = [word for word in words if word["Confidence"] < 80]
        assert len(low) == 2, low
        assert page.ocr_stats is not None
        assert page.ocr_stats.low_confidence_fraction == (
            Decimal(len(low)) / Decimal(len(words))
        ).quantize(Decimal("0.0001"))
        # Two weak words out of nearly three hundred do not move the tenth
        # percentile; the cell-level minimum is what carries them to review.
        assert page.ocr_stats.p10_confidence >= Decimal("0.9")
        assert page.ocr_stats.mean_confidence >= Decimal("0.9")


class TestMalformedResponses:
    def test_a_cell_pointing_at_a_missing_word_raises(
        self, response: dict[str, Any], pdf_bytes: bytes
    ) -> None:
        broken = copy.deepcopy(response)
        cell = next(
            block
            for block in broken["Blocks"]
            if block["BlockType"] == "CELL" and block.get("Relationships")
        )
        cell["Relationships"][0]["Ids"].append("00000000-dead-4000-8000-000000000000")

        with pytest.raises(ValueError) as error:
            TextractExtractor(client=_Replay(broken)).extract_pages(pdf_bytes, [1])
        # Naming the block is the point: a response this large is only
        # debuggable if the exception says which block lied.
        assert cell["Id"] in str(error.value)
        assert "00000000-dead-4000-8000-000000000000" in str(error.value)

    def test_a_table_without_cells_raises(self, response: dict[str, Any], pdf_bytes: bytes) -> None:
        broken = copy.deepcopy(response)
        table = next(block for block in broken["Blocks"] if block["BlockType"] == "TABLE")
        table["Relationships"] = []

        with pytest.raises(ValueError, match="has no CELL children"):
            TextractExtractor(client=_Replay(broken)).extract_pages(pdf_bytes, [1])


class TestPlainCellSpans:
    """A span on a plain CELL means the same thing as a MERGED_CELL block.

    Textract emits no CELL for the positions a span covers, so the grid has
    to treat both the span and the resulting hole as continuation — the
    fixture's caption row exercises the MERGED_CELL path, and this exercises
    the other one on a response built for the purpose.
    """

    @staticmethod
    def _spanning_response() -> dict[str, Any]:
        def geometry(left: float, top: float, width: float, height: float) -> dict[str, Any]:
            return {"BoundingBox": {"Width": width, "Height": height, "Left": left, "Top": top}}

        return {
            "DocumentMetadata": {"Pages": 1},
            "Blocks": [
                {
                    "BlockType": "TABLE",
                    "Confidence": 99.0,
                    "Geometry": geometry(0.1, 0.1, 0.8, 0.2),
                    "Id": "table",
                    "Relationships": [{"Type": "CHILD", "Ids": ["cell-a", "cell-b"]}],
                },
                {
                    "BlockType": "CELL",
                    "Confidence": 98.0,
                    "RowIndex": 1,
                    "ColumnIndex": 1,
                    "RowSpan": 1,
                    "ColumnSpan": 2,
                    "Geometry": geometry(0.1, 0.1, 0.8, 0.1),
                    "Id": "cell-a",
                    "Relationships": [{"Type": "CHILD", "Ids": ["word-a"]}],
                },
                {
                    "BlockType": "CELL",
                    "Confidence": 98.0,
                    "RowIndex": 2,
                    "ColumnIndex": 1,
                    "Geometry": geometry(0.1, 0.2, 0.4, 0.1),
                    "Id": "cell-b",
                    "Relationships": [{"Type": "CHILD", "Ids": ["word-b"]}],
                },
                {
                    "BlockType": "WORD",
                    "Confidence": 91.0,
                    "Text": "Banner",
                    "Geometry": geometry(0.1, 0.1, 0.8, 0.1),
                    "Id": "word-a",
                },
                {
                    "BlockType": "WORD",
                    "Confidence": 93.0,
                    "Text": "Stub",
                    "Geometry": geometry(0.1, 0.2, 0.4, 0.1),
                    "Id": "word-b",
                },
            ],
            "AnalyzeDocumentModelVersion": "1.0",
        }

    def test_span_widens_the_grid_and_blanks_what_it_covers(self, pdf_bytes: bytes) -> None:
        page = (
            TextractExtractor(client=_Replay(self._spanning_response()))
            .extract_pages(pdf_bytes, [1])
            .pages[0]
        )
        table = page.tables[0]
        assert (table.row_count, table.column_count) == (2, 2)
        assert [cell.text for cell in table.rows[0]] == ["Banner", None]
        # Nothing claims (2, 2) at all, which can only be a continuation.
        assert [cell.text for cell in table.rows[1]] == ["Stub", None]
