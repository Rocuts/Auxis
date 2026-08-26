"""GET /records contract: filters, the superseded exclusion the gate names,
and cursor pagination that stays stable under concurrent inserts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from tax_tables.domain.records import FilingStatus, LifecycleStatus, RecordType
from tests.api.conftest import record, seed_records


def _ids(payload: dict) -> list[str]:  # type: ignore[type-arg]
    return [item["id"] for item in payload["items"]]


class TestFilters:
    def _seed(self) -> None:
        seed_records(
            [
                record(lower_bound=0, upper_bound=9000, rate=Decimal("0.10")),
                record(lower_bound=9001, upper_bound=None, rate=Decimal("0.22")),
                record(
                    filing_status=FilingStatus.MARRIED_FILING_JOINTLY,
                    lower_bound=0,
                    upper_bound=18000,
                ),
                record(
                    record_type=RecordType.STANDARD_DEDUCTION,
                    lower_bound=None,
                    upper_bound=None,
                    rate=None,
                    amount=Decimal("15400"),
                ),
                record(tax_year=2025, lower_bound=0, upper_bound=8500),
                record(
                    jurisdiction="ZZ-OTHER",
                    lower_bound=0,
                    upper_bound=7000,
                    confidence=Decimal("0.4"),
                ),
                record(
                    record_type=RecordType.SALES_TAX_RATE,
                    filing_status=None,
                    tax_year=None,
                    lower_bound=None,
                    upper_bound=None,
                    rate=Decimal("0.0625"),
                    effective_from=date(2026, 1, 1),
                    effective_to=date(2026, 6, 30),
                ),
            ],
            sha_prefix="aa",
        )
        # The document-05 trap, synthetically: superseded records CARRYING
        # tax_year=2026 must still not surface in a 2026 query.
        seed_records(
            [
                record(
                    lifecycle_status=LifecycleStatus.SUPERSEDED,
                    lower_bound=0,
                    upper_bound=9999,
                    rate=Decimal("0.11"),
                ),
                record(
                    lifecycle_status=LifecycleStatus.SUPERSEDED,
                    lower_bound=10000,
                    upper_bound=None,
                    rate=Decimal("0.23"),
                ),
            ],
            sha_prefix="bb",
        )

    def test_tax_year_2026_excludes_superseded(self, client: TestClient) -> None:
        """The gate assertion, verbatim from CLAUDE.md: this is a test, not
        a claim."""
        self._seed()
        payload = client.get("/records", params={"tax_year": 2026}).json()
        # Three single/joint brackets, the standard deduction, and the
        # low-confidence ZZ-OTHER bracket — all active 2026.
        assert len(payload["items"]) == 5
        assert all(item["lifecycle_status"] == "active" for item in payload["items"])
        assert all(item["tax_year"] == 2026 for item in payload["items"])

    def test_include_superseded_surfaces_them_flagged(self, client: TestClient) -> None:
        self._seed()
        payload = client.get(
            "/records", params={"tax_year": 2026, "include_superseded": "true"}
        ).json()
        statuses = {item["lifecycle_status"] for item in payload["items"]}
        assert len(payload["items"]) == 7  # the five active plus both superseded
        assert statuses == {"active", "superseded"}

    def test_jurisdiction_record_type_and_filing_status(self, client: TestClient) -> None:
        self._seed()
        rows = client.get(
            "/records",
            params={
                "jurisdiction": "ZZ-API",
                "record_type": "ordinary_income_bracket",
                "filing_status": "single",
                "tax_year": 2026,
            },
        ).json()["items"]
        assert len(rows) == 2
        assert {row["filing_status"] for row in rows} == {"single"}

    def test_min_confidence(self, client: TestClient) -> None:
        self._seed()
        rows = client.get("/records", params={"min_confidence": "0.9"}).json()["items"]
        assert all(Decimal(row["confidence"]) >= Decimal("0.9") for row in rows)
        assert not any(row["jurisdiction"] == "ZZ-OTHER" for row in rows)

    def test_effective_on_window(self, client: TestClient) -> None:
        self._seed()
        inside = client.get("/records", params={"effective_on": "2026-03-01"}).json()["items"]
        assert any(row["record_type"] == "sales_tax_rate" for row in inside)
        outside = client.get(
            "/records", params={"effective_on": "2026-07-01", "record_type": "sales_tax_rate"}
        ).json()["items"]
        assert outside == []

    def test_rates_and_amounts_are_exact_digit_strings(self, client: TestClient) -> None:
        self._seed()
        rows = client.get("/records", params={"record_type": "standard_deduction"}).json()["items"]
        (deduction,) = rows
        assert Decimal(deduction["amount"]) == Decimal("15400")

    def test_unknown_record_type_is_422(self, client: TestClient) -> None:
        assert client.get("/records", params={"record_type": "nonsense"}).status_code == 422


class TestPagination:
    def test_walk_is_stable_under_concurrent_inserts(self, client: TestClient) -> None:
        """Keyset pagination on (created_at, id): every record that existed
        when the walk started is seen exactly once, however many records
        land mid-walk (the gate's stability requirement)."""
        seed_records(
            [
                record(lower_bound=index * 1000, upper_bound=index * 1000 + 999)
                for index in range(7)
            ],
            sha_prefix="aa",
        )
        original = set(_ids(client.get("/records", params={"limit": 200}).json()))
        assert len(original) == 7

        first = client.get("/records", params={"limit": 3}).json()
        assert len(first["items"]) == 3
        assert first["next_cursor"] is not None
        seen = _ids(first)

        # Concurrent activity between pages: a second document lands.
        seed_records(
            [
                record(
                    jurisdiction="ZZ-MIDWALK",
                    lower_bound=index * 1000,
                    upper_bound=index * 1000 + 999,
                )
                for index in range(3)
            ],
            sha_prefix="cc",
        )

        cursor = first["next_cursor"]
        while cursor is not None:
            page = client.get("/records", params={"limit": 3, "cursor": cursor}).json()
            seen.extend(_ids(page))
            cursor = page["next_cursor"]

        assert len(seen) == len(set(seen)), "no record may appear twice"
        assert original <= set(seen), "no pre-walk record may be skipped"

    def test_malformed_cursor_is_400(self, client: TestClient) -> None:
        assert client.get("/records", params={"cursor": "not-a-cursor"}).status_code == 400

    def test_limit_over_the_declared_maximum_is_422(self, client: TestClient) -> None:
        assert client.get("/records", params={"limit": 100000}).status_code == 422
