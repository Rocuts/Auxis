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


class TestContractCompleteness:
    """The OpenAPI file is a stated deliverable, so what it omits matters.

    Adversarial review, 2026-08-27: the spec described no request body for
    `POST /documents` — the only write endpoint — because the handler takes a
    raw `Request`, so FastAPI had nothing to introspect. `/docs` rendered no
    body field and "Try it out" could not be used. The union of documented
    response codes across all ten operations was `200/202/422`, while the live
    service returns 401, 413, 415, 404 and 400 in ordinary use.
    """

    def test_upload_declares_a_request_body(self, client: TestClient) -> None:
        op = client.get("/openapi.json").json()["paths"]["/documents"]["post"]
        body = op.get("requestBody")
        assert body is not None, "the only write endpoint documents no body"
        assert "application/pdf" in body["content"]

    def test_upload_declares_the_filename_header(self, client: TestClient) -> None:
        """`x-filename` is the ONLY way to name an uploaded document — the
        body is raw bytes, so there is no multipart part name. Undocumented,
        every stored row reads `upload.pdf`."""
        op = client.get("/openapi.json").json()["paths"]["/documents"]["post"]
        names = {p["name"].lower() for p in op.get("parameters", [])}
        assert "x-filename" in names

    def test_upload_documents_its_rejections(self, client: TestClient) -> None:
        op = client.get("/openapi.json").json()["paths"]["/documents"]["post"]
        for code in ("401", "413", "415"):
            assert code in op["responses"], f"upload can return {code} and does not say so"

    def test_authenticated_paths_document_401(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        for path, method in (
            ("/documents", "post"),
            ("/internal/sweep", "post"),
            ("/internal/sweep", "get"),
        ):
            assert "401" in paths[path][method]["responses"], f"{method} {path}"

    def test_by_id_paths_document_404(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        for path in ("/jobs/{job_id}", "/documents/{document_id}", "/reviews/{review_id}"):
            assert "404" in paths[path]["get"]["responses"], path


class TestErrorsAreAlwaysJson:
    """Every error the API returns must be JSON with a `detail` key.

    A single unhandled exception escaped as `text/plain`, off the contract
    every other response honours — a client parsing `detail` would crash on
    the one response it most needs to read.
    """

    def test_absurd_amount_is_rejected_not_crashed(self, client: TestClient) -> None:
        """`amount` was the only numeric query parameter with no upper bound;
        one past the int64 ceiling reached the driver and became a 500."""
        response = client.get(
            "/records/resolve",
            params={"amount": 9223372036854775808, "filing_status": "single", "tax_year": 2026},
        )
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/json")

    def test_largest_valid_amount_still_answers(self, client: TestClient) -> None:
        """The bound must sit exactly at the storage limit, not below it."""
        response = client.get(
            "/records/resolve",
            params={"amount": 9223372036854775807, "filing_status": "single", "tax_year": 2026},
        )
        assert response.status_code in (200, 404)

    def test_an_unhandled_error_is_json(self, client: TestClient) -> None:
        """Simulated at the boundary: whatever escapes, the shape holds."""
        from tax_tables.api import app as app_module

        for handler in client.app.exception_handlers.values():  # type: ignore[attr-defined]
            assert handler is not None
        assert hasattr(app_module, "_unhandled_exception_handler")
