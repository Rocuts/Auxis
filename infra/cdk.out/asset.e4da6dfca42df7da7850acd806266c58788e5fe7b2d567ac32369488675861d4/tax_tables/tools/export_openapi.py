"""Export the API's OpenAPI 3.1 document to docs/openapi.yaml.

Usage:
    uv run python -m tax_tables.tools.export_openapi [output-path]

App construction needs no live database — settings here are placeholders
that never leave the schema — so the export runs anywhere the code does.
A contract test regenerates the schema and diffs it against the committed
file, so a stale export fails CI rather than shipping.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from tax_tables.api.app import create_app
from tax_tables.api.settings import ApiSettings

DEFAULT_OUTPUT = Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml"


def openapi_document() -> dict[str, Any]:
    settings = ApiSettings(
        database_url="postgresql://unused/schema-export",
        api_key="schema-export",
        cron_secret="schema-export",
    )
    return create_app(settings).openapi()


def render() -> str:
    return yaml.safe_dump(openapi_document(), sort_keys=False, allow_unicode=True)


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    output.write_text(render(), encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
