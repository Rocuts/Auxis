"""Router invariants against the real fixture PDFs.

The load-bearing one: a page with a usable text layer is NEVER sent to an
OCR adapter — OCR costs money on two of three targets, and the router is
the only thing standing between a digital PDF and that spend.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from tax_tables.extraction.model import ExtractionMethod, PageExtraction
from tax_tables.extraction.router import ExtractionRouter, route
from tax_tables.ports.extractor import PageBatch

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

DIGITAL_DOCS = [
    "01_federal_income_tax_rate_schedules_TY2026.pdf",
    "02_standard_deduction_schedule_TY2026.pdf",
    "03_state_local_sales_tax_rates_2026.pdf",
    "04_employment_tax_rates_and_thresholds_2026.pdf",
]
SCANNED_DOC = "05_capital_gains_preferential_rates_TY2025.pdf"


class RecordingExtractor:
    """Minimal TableExtractor that records what it was asked for."""

    def __init__(self, engine: str) -> None:
        self._engine = engine
        self.requested: list[list[int]] = []

    @property
    def engine(self) -> str:
        return self._engine

    def extract_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> PageBatch:
        self.requested.append(list(page_numbers))
        return PageBatch(
            pages=[
                PageExtraction(
                    page_number=n,
                    width=612,
                    height=792,
                    method=ExtractionMethod.DETERMINISTIC_TEXT
                    if self._engine == "digital"
                    else ExtractionMethod.OCR,
                )
                for n in page_numbers
            ]
        )


def synthetic_scan_pdf(*, stamp: str | None) -> bytes:
    """A one-page PDF holding a page-sized JPEG (a 'scan'), optionally with a
    text stamp long enough to clear MIN_TEXT_CHARS. Hand-rolled writer: no
    PDF-authoring dependency exists in this project, and the routing question
    only needs geometry, not content."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 160), (235, 235, 235)).save(buf, format="JPEG")
    jpeg = buf.getvalue()
    w, h = 612.0, 792.0
    parts = [f"q {w:.0f} 0 0 {h:.0f} 0 0 cm /Im0 Do Q"]
    if stamp:
        parts.append(f"BT /F1 9 Tf 40 {h - 24:.0f} Td ({stamp}) Tj ET")
    content = "\n".join(parts).encode("latin-1")
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.0f} {h:.0f}] "
            "/Contents 4 0 R /Resources << /XObject << /Im0 5 0 R >> "
            "/Font << /F1 6 0 R >> >> >>"
        ).encode(),
        4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        5: (
            b"<< /Type /XObject /Subtype /Image /Width 120 /Height 160 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            b"/Length %d >>\nstream\n%s\nendstream" % (len(jpeg), jpeg)
        ),
        6: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for num in sorted(objs):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


class TestRoute:
    @pytest.mark.parametrize("name", DIGITAL_DOCS)
    def test_text_layer_documents_route_deterministic(self, name: str) -> None:
        methods = route((FIXTURES / name).read_bytes())
        assert set(methods) == {ExtractionMethod.DETERMINISTIC_TEXT}

    def test_scan_with_text_stamp_still_routes_to_ocr(self) -> None:
        # The mixed page: a page-sized image plus a stamp that clears
        # MIN_TEXT_CHARS. Trusting the char count alone would classify this
        # digital; pdfplumber would then find no tables and the document
        # would come back empty at full confidence — anti-goal #8. The
        # page-sized image must win.
        stamp = "Received by Records Management on 14 March 2026 Bates 000147"
        assert len(stamp) >= 50
        assert route(synthetic_scan_pdf(stamp=stamp)) == [ExtractionMethod.OCR]

    def test_plain_scan_control_routes_to_ocr(self) -> None:
        assert route(synthetic_scan_pdf(stamp=None)) == [ExtractionMethod.OCR]

    def test_scanned_document_routes_to_ocr(self) -> None:
        methods = route((FIXTURES / SCANNED_DOC).read_bytes())
        assert set(methods) == {ExtractionMethod.OCR}

    def test_doc_03_routes_both_pages(self) -> None:
        methods = route((FIXTURES / DIGITAL_DOCS[2]).read_bytes())
        assert len(methods) == 2


class TestExtractionRouter:
    def test_digital_document_never_reaches_ocr_adapter(self) -> None:
        digital, ocr = RecordingExtractor("digital"), RecordingExtractor("ocr")
        router = ExtractionRouter(digital=digital, ocr=ocr)
        pdf = (FIXTURES / DIGITAL_DOCS[0]).read_bytes()
        doc = router.extract(pdf, filename=DIGITAL_DOCS[0])
        assert ocr.requested == []
        assert digital.requested == [[1]]
        assert doc.cost.engine == "digital"
        assert doc.cost.usd == 0

    def test_scanned_document_never_reaches_digital_adapter(self) -> None:
        digital, ocr = RecordingExtractor("digital"), RecordingExtractor("ocr")
        router = ExtractionRouter(digital=digital, ocr=ocr)
        pdf = (FIXTURES / SCANNED_DOC).read_bytes()
        doc = router.extract(pdf, filename=SCANNED_DOC)
        assert digital.requested == []
        assert ocr.requested == [[1]]
        assert doc.methods == {ExtractionMethod.OCR}

    def test_sha256_matches_input_bytes(self) -> None:
        import hashlib

        pdf = (FIXTURES / DIGITAL_DOCS[0]).read_bytes()
        router = ExtractionRouter(
            digital=RecordingExtractor("digital"), ocr=RecordingExtractor("ocr")
        )
        doc = router.extract(pdf, filename=DIGITAL_DOCS[0])
        assert doc.sha256 == hashlib.sha256(pdf).hexdigest()

    def test_adapter_dropping_a_page_fails_loudly(self) -> None:
        class DroppingExtractor(RecordingExtractor):
            def extract_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> PageBatch:
                return PageBatch(pages=[])

        router = ExtractionRouter(
            digital=DroppingExtractor("digital"), ocr=RecordingExtractor("ocr")
        )
        pdf = (FIXTURES / DIGITAL_DOCS[0]).read_bytes()
        with pytest.raises(RuntimeError, match="no extraction for pages"):
            router.extract(pdf, filename=DIGITAL_DOCS[0])
