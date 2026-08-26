"""The Phase 2a gate report: per-document extraction, printed as a table.

Runs the router + both local adapters over a directory of PDFs and reports,
per document: the method the router chose, grid dimensions, prose/footnote
capture, quality flags, and cost. Exits nonzero if any text-layer document
cost anything — "docs 01-04 are $0" is a gate condition, not a hope.

Reads only the PDFs. The ground truth is a test oracle and has no business
here (anti-goal #1).

Usage: uv run python -m tax_tables.tools.extraction_report [fixtures_dir]
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from tax_tables.adapters.pdfplumber_extractor import PdfplumberExtractor
from tax_tables.adapters.tesseract_extractor import TesseractExtractor
from tax_tables.extraction.model import ExtractedDocument, ExtractionMethod, ProseKind
from tax_tables.extraction.router import ExtractionRouter


def _table_dims(doc: ExtractedDocument) -> str:
    return ", ".join(f"p{t.page_number}:{t.row_count}x{t.column_count}" for t in doc.tables)


def _prose_counts(doc: ExtractedDocument) -> str:
    counts = dict.fromkeys(ProseKind, 0)
    for block in doc.prose:
        counts[block.kind] += 1
    return f"{counts[ProseKind.HEADING]}h/{counts[ProseKind.BODY]}b/{counts[ProseKind.FOOTNOTE]}f"


def _ocr_summary(doc: ExtractedDocument) -> str:
    stats = [p.ocr_stats for p in doc.pages if p.ocr_stats is not None]
    if not stats:
        return "-"
    words = sum(s.word_count for s in stats)
    p10 = min(s.p10_confidence for s in stats)
    return f"{words}w p10={p10}"


def run(fixtures_dir: Path) -> int:
    router = ExtractionRouter(digital=PdfplumberExtractor(), ocr=TesseractExtractor())
    pdfs = sorted(fixtures_dir.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs under {fixtures_dir}", file=sys.stderr)
        return 2

    header = (
        "document",
        "method",
        "engine",
        "tables (page:rows x cols)",
        "prose h/b/f",
        "flags",
        "conf",
        "ocr",
        "usd",
        "wall_s",
    )
    rows: list[tuple[str, ...]] = [header]
    text_layer_spend: list[str] = []

    for pdf_path in pdfs:
        doc = router.extract(pdf_path.read_bytes(), filename=pdf_path.name)
        methods = "+".join(sorted(m.value for m in doc.methods))
        flagged = sum(t.flagged_cell_count for t in doc.tables)
        irregular = sum(len(t.irregular_row_indexes) for t in doc.tables)
        rows.append(
            (
                doc.filename,
                methods,
                doc.cost.engine,
                _table_dims(doc),
                _prose_counts(doc),
                f"{flagged} cells/{irregular} rows",
                str(doc.confidence),
                _ocr_summary(doc),
                str(doc.cost.usd),
                f"{doc.cost.wall_seconds:.2f}",
            )
        )
        if doc.methods == {ExtractionMethod.DETERMINISTIC_TEXT} and (
            doc.cost.usd != Decimal(0) or doc.cost.api_calls
        ):
            text_layer_spend.append(doc.filename)

    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for index, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * w for w in widths))

    if text_layer_spend:
        print(f"\nGATE FAILURE: text-layer documents spent money: {text_layer_spend}")
        return 1
    print("\ngate: every text-layer document extracted at $0 with no API calls")
    return 0


def main() -> None:
    fixtures = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixtures")
    raise SystemExit(run(fixtures))


if __name__ == "__main__":
    main()
