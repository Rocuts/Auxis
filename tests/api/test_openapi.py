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
        # An exhaustive inventory, not a subset check: a route that appears
        # without being added here is an undocumented surface.
        assert set(payload["paths"]) == {
            "/documents",
            "/documents/{document_id}",
            "/jobs/{job_id}",
            "/records",
            "/records/resolve",
            "/reviews",
            "/reviews/{review_id}",
            "/internal/sweep",
        }

    def test_the_review_surface_is_read_only(self, client: TestClient) -> None:
        """The write half of the review queue is out of scope by decision.
        Pinned in the schema too, so 'read-only' survives a future edit that
        adds a handler without revisiting the decision."""
        payload = client.get("/openapi.json").json()
        for path in ("/reviews", "/reviews/{review_id}"):
            assert set(payload["paths"][path]) == {"get"}, path

    def test_committed_export_matches_the_app(self) -> None:
        committed = Path(DEFAULT_OUTPUT)
        assert committed.is_file(), (
            "docs/openapi.yaml is missing — run: uv run python -m tax_tables.tools.export_openapi"
        )
        assert committed.read_text(encoding="utf-8") == render(), (
            "docs/openapi.yaml is stale — regenerate with: "
            "uv run python -m tax_tables.tools.export_openapi"
        )
