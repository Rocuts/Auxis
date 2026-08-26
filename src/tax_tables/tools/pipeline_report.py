"""Run the full pipeline over PDF documents and report, per document:
mapped records, issues, triage verdicts, persistence outcome, and mapper
cost. Optionally dumps every intermediate artifact as JSON for inspection.

Reads only the PDFs and the environment (mapping endpoint/key/model; see
``MapperConfig.from_env``). The test oracle has no business here.

Usage:
    uv run python -m tax_tables.tools.pipeline_report fixtures/01_x.pdf \
        [--dsn postgresql://...] [--artifacts DIR]

Without --dsn the run is dry: extraction, mapping, and triage happen,
nothing is persisted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tax_tables.adapters.anthropic_mapper import AnthropicSchemaMapper, MapperConfig, MapperError
from tax_tables.adapters.pdfplumber_extractor import PdfplumberExtractor
from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.adapters.tesseract_extractor import TesseractExtractor
from tax_tables.domain.records import ReviewStatus
from tax_tables.extraction.router import ExtractionRouter
from tax_tables.pipeline import PipelineResult, run_document


def _dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")


def _write_artifacts(directory: Path, stem: str, result: PipelineResult) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _dump(directory / f"{stem}.extracted.json", result.extracted.model_dump(mode="json"))
    _dump(
        directory / f"{stem}.mapping.json",
        {
            "records": [record.model_dump(mode="json") for record in result.mapping.records],
            "issues": [issue.model_dump(mode="json") for issue in result.mapping.issues],
            "cost": (
                None if result.mapping.cost is None else result.mapping.cost.model_dump(mode="json")
            ),
        },
    )
    _dump(
        directory / f"{stem}.triage.json",
        {
            "findings": [finding.model_dump(mode="json") for finding in result.triage.findings],
            "rejected": [entry.model_dump(mode="json") for entry in result.triage.rejected],
            "persistable": [record.model_dump(mode="json") for record in result.triage.persistable],
        },
    )
    _dump(
        directory / f"{stem}.outcome.json",
        {
            "document_id": None if result.document_id is None else str(result.document_id),
            "ingest": None if result.ingest is None else vars(result.ingest),
            "review_entries": result.review_entries,
        },
    )


def run(paths: list[Path], dsn: str | None, artifacts: Path | None) -> int:
    router = ExtractionRouter(digital=PdfplumberExtractor(), ocr=TesseractExtractor())
    mapper = AnthropicSchemaMapper(MapperConfig.from_env())
    repository = PostgresRecordRepository(dsn) if dsn else None

    header = (
        "document",
        "records",
        "issues",
        "rejected",
        "flagged",
        "persisted",
        "conflicts",
        "overlaps",
        "tok_in",
        "tok_out",
        "usd",
        "wall_s",
    )
    rows: list[tuple[str, ...]] = [header]
    errors: list[str] = []
    try:
        for path in paths:
            try:
                result = run_document(
                    path.read_bytes(),
                    filename=path.name,
                    router=router,
                    mapper=mapper,
                    repository=repository,
                )
            except MapperError as exc:
                # One document's failure must not silence the report for the
                # rest; it is named below and reflected in the exit code.
                errors.append(f"{path.name}: {exc}")
                rows.append((path.name, *("!",) * (len(header) - 1)))
                continue
            if artifacts is not None:
                _write_artifacts(artifacts, path.stem, result)
            cost = result.mapping.cost
            flagged = sum(
                1
                for record in result.triage.persistable
                if record.review_status is ReviewStatus.NEEDS_REVIEW
            )
            rows.append(
                (
                    path.name,
                    str(len(result.mapping.records)),
                    str(len(result.mapping.issues)),
                    str(len(result.triage.rejected)),
                    str(flagged),
                    "-" if result.ingest is None else str(result.ingest.persisted),
                    "-" if result.ingest is None else str(result.ingest.cross_document_conflicts),
                    "-" if result.ingest is None else str(result.ingest.overlap_rejections),
                    "-"
                    if cost is None
                    else str(cost.input_tokens + cost.cache_write_tokens + cost.cache_read_tokens),
                    "-" if cost is None else str(cost.output_tokens),
                    "-" if cost is None else f"{cost.usd:.4f}",
                    "-" if cost is None else f"{cost.wall_seconds:.1f}",
                )
            )
    finally:
        if repository is not None:
            repository.close()

    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for index, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))
    if errors:
        print("\nfailed documents:")
        for error in errors:
            print(f"  {error}")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path, help="PDF files to ingest")
    parser.add_argument("--dsn", default=None, help="Postgres DSN; omit for a dry run")
    parser.add_argument("--artifacts", type=Path, default=None, help="dump JSON artifacts here")
    args = parser.parse_args()
    missing = [str(path) for path in args.pdfs if not path.is_file()]
    if missing:
        print(f"no such file: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(args.pdfs, args.dsn, args.artifacts))


if __name__ == "__main__":
    main()
