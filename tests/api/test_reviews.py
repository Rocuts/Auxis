"""GET /reviews contract: the read-only review surface.

Honest-limitation #10 said the review queue was a table with no HTTP
surface. This closes the read half of that gap and deliberately not the
write half: human adjudication stays out of scope, so the API exposes no
way to resolve or dismiss an item. The 405 tests below are the assertion
that the omission is designed rather than forgotten.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import seed_reviews


def _ids(payload: dict) -> list[str]:  # type: ignore[type-arg]
    return [item["id"] for item in payload["items"]]


class TestListing:
    def _seed(self) -> tuple[str, list[str]]:
        document_id, items = seed_reviews(
            [
                {
                    "reason": "unreadable cell",
                    "source_page": 1,
                    "table_id": "p1_t0",
                    "row_index": 3,
                    "col_index": 1,
                    "raw_value": "??",
                },
                {
                    "reason": "verifier_dispute: rate disagrees",
                    "source_page": 1,
                    "table_id": "p1_t0",
                    "row_index": 4,
                },
                {
                    "reason": "bracket_overlap: 0-9000",
                    "source_page": 2,
                    "resolve": {
                        "resolution": {"decision": "kept", "citations": ["p2_t0:r1"]},
                        "resolved_by": "adjudicator:test-model",
                    },
                },
            ],
            sha_prefix="c1",
        )
        return str(document_id), [str(item) for item in items]

    def test_listing_is_public_and_ordered(self, client: TestClient) -> None:
        _document_id, items = self._seed()
        response = client.get("/reviews")
        assert response.status_code == 200
        assert _ids(response.json()) == items

    def test_status_filter(self, client: TestClient) -> None:
        _document_id, items = self._seed()
        assert _ids(client.get("/reviews", params={"status": "open"}).json()) == items[:2]
        assert _ids(client.get("/reviews", params={"status": "resolved"}).json()) == items[2:]
        assert _ids(client.get("/reviews", params={"status": "dismissed"}).json()) == []

    def test_document_id_filter(self, client: TestClient) -> None:
        document_id, items = self._seed()
        other, _ = seed_reviews([{"reason": "elsewhere"}], sha_prefix="c2")
        assert _ids(client.get("/reviews", params={"document_id": document_id}).json()) == items
        assert len(_ids(client.get("/reviews", params={"document_id": str(other)}).json())) == 1

    def test_cursor_walk_covers_every_item_exactly_once(self, client: TestClient) -> None:
        _document_id, items = self._seed()
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor  # type: ignore[assignment]
            payload = client.get("/reviews", params=params).json()
            seen.extend(_ids(payload))
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        assert seen == items


class TestDetail:
    def test_resolved_item_carries_its_full_audit_trail(self, client: TestClient) -> None:
        """ADR 012: auto-resolution is acceptable only because it is always
        accountable. The API has to actually show the accounting."""
        _document_id, items = seed_reviews(
            [
                {
                    "reason": "dash means null?",
                    "source_page": 3,
                    "resolve": {
                        "resolution": {"decision": "null", "citations": ["p3_t1:r7c2"]},
                        "resolved_by": "adjudicator:test-model",
                    },
                }
            ],
            sha_prefix="c3",
        )
        payload = client.get(f"/reviews/{items[0]}").json()
        assert payload["status"] == "resolved"
        assert payload["resolution"] == {"decision": "null", "citations": ["p3_t1:r7c2"]}
        assert payload["resolved_by"] == "adjudicator:test-model"
        assert payload["resolved_at"] is not None
        assert payload["source_page"] == 3

    def test_stored_proposal_is_visible_while_the_item_stays_open(self, client: TestClient) -> None:
        """Below the threshold the adjudicator stores a proposal and the item
        stays human. A reviewer cannot act on a proposal they cannot see."""
        _document_id, items = seed_reviews(
            [{"reason": "ambiguous unit", "propose": {"decision": "percent", "confidence": "0.6"}}],
            sha_prefix="c4",
        )
        payload = client.get(f"/reviews/{items[0]}").json()
        assert payload["status"] == "open"
        assert payload["resolution"] == {"decision": "percent", "confidence": "0.6"}
        assert payload["resolved_by"] is None
        assert payload["resolved_at"] is None

    def test_unknown_item_is_404(self, client: TestClient) -> None:
        response = client.get("/reviews/2b1e4e5a-0000-4000-8000-000000000000")
        assert response.status_code == 404


class TestValidation:
    def test_unknown_status_is_422(self, client: TestClient) -> None:
        assert client.get("/reviews", params={"status": "pending"}).status_code == 422

    def test_malformed_cursor_is_400(self, client: TestClient) -> None:
        assert client.get("/reviews", params={"cursor": "not-base64"}).status_code == 400

    def test_limit_over_the_declared_maximum_is_422(self, client: TestClient) -> None:
        assert client.get("/reviews", params={"limit": 10_000}).status_code == 422


class TestNoWritePath:
    """Read-only by design. Human adjudication is out of scope, and these
    assertions are what keep 'no write path' from decaying into 'nobody got
    around to it yet'."""

    def test_collection_rejects_writes(self, client: TestClient) -> None:
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)("/reviews")
            assert response.status_code == 405, method

    def test_item_rejects_writes(self, client: TestClient) -> None:
        _document_id, items = seed_reviews([{"reason": "immutable"}], sha_prefix="c5")
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)(f"/reviews/{items[0]}")
            assert response.status_code == 405, method
