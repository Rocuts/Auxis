"""OpenAPI contract: the app serves 3.1, and the committed export is the
schema the app actually serves — a stale docs/openapi.yaml fails here
rather than shipping.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tax_tables.tools.export_openapi import DEFAULT_OUTPUT, render


class TestOpenApi:
    def test_served_schema_is_openapi_31(self, client: TestClient) -> None:
        payload = client.get("/openapi.json").json()
        assert payload["openapi"].startswith("3.1")
        assert set(payload["paths"]) == {
            "/documents",
            "/documents/{document_id}",
            "/jobs/{job_id}",
            "/records",
            "/records/resolve",
            "/internal/sweep",
        }

    def test_committed_export_matches_the_app(self) -> None:
        committed = Path(DEFAULT_OUTPUT)
        assert committed.is_file(), (
            "docs/openapi.yaml is missing — run: uv run python -m tax_tables.tools.export_openapi"
        )
        assert committed.read_text(encoding="utf-8") == render(), (
            "docs/openapi.yaml is stale — regenerate with: "
            "uv run python -m tax_tables.tools.export_openapi"
        )
