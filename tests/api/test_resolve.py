"""GET /records/resolve contract: the bracket containing an amount, off the
same GiST index (and the same COALESCE expressions) that makes overlap
unrepresentable. A data lookup — the response carries the record, never a
computed liability.
"""

from __future__ import annotations

from decimal import Decimal

import httpx2
from fastapi.testclient import TestClient

from tax_tables.domain.records import FilingStatus, LifecycleStatus
from tests.api.conftest import record, seed_records


def _seed_chains() -> None:
    seed_records(
        [
            record(lower_bound=0, upper_bound=9000, rate=Decimal("0.10")),
            record(lower_bound=9001, upper_bound=38000, rate=Decimal("0.12")),
            record(lower_bound=38001, upper_bound=None, rate=Decimal("0.22")),
            # A parallel chain that must never answer for single filers.
            record(
                filing_status=FilingStatus.MARRIED_FILING_JOINTLY,
                lower_bound=0,
                upper_bound=18000,
                rate=Decimal("0.10"),
            ),
            # Estates/trusts: discriminated by taxpayer_class alone.
            record(
                filing_status=None,
                taxpayer_class="estate_or_trust",
                lower_bound=0,
                upper_bound=3250,
                rate=Decimal("0.10"),
            ),
            record(
                filing_status=None,
                taxpayer_class="estate_or_trust",
                lower_bound=3251,
                upper_bound=None,
                rate=Decimal("0.37"),
            ),
        ],
        sha_prefix="aa",
    )
    seed_records(
        [
            record(
                lifecycle_status=LifecycleStatus.SUPERSEDED,
                lower_bound=0,
                upper_bound=99999,
                rate=Decimal("0.99"),
                filing_status=FilingStatus.HEAD_OF_HOUSEHOLD,
            )
        ],
        sha_prefix="bb",
    )


def _resolve(client: TestClient, **params: str | int) -> httpx2.Response:
    defaults: dict[str, str | int] = {"tax_year": 2026, "jurisdiction": "ZZ-API"}
    defaults.update(params)
    return client.get("/records/resolve", params=defaults)


class TestResolve:
    def test_amount_inside_a_bracket(self, client: TestClient) -> None:
        _seed_chains()
        response = _resolve(client, amount=5000, filing_status="single")
        assert response.status_code == 200
        payload = response.json()
        assert payload["amount"] == 5000
        assert payload["record"]["lower_bound"] == 0
        assert payload["record"]["upper_bound"] == 9000
        assert Decimal(payload["record"]["rate"]) == Decimal("0.10")

    def test_inclusive_upper_boundary(self, client: TestClient) -> None:
        _seed_chains()
        assert (
            _resolve(client, amount=9000, filing_status="single").json()["record"]["upper_bound"]
            == 9000
        )
        assert (
            _resolve(client, amount=9001, filing_status="single").json()["record"]["lower_bound"]
            == 9001
        )

    def test_open_top_bracket_contains_any_amount_above_it(self, client: TestClient) -> None:
        _seed_chains()
        payload = _resolve(client, amount=10_000_000, filing_status="single").json()
        assert payload["record"]["upper_bound"] is None
        assert Decimal(payload["record"]["rate"]) == Decimal("0.22")

    def test_chains_answer_independently(self, client: TestClient) -> None:
        _seed_chains()
        single = _resolve(client, amount=10000, filing_status="single").json()
        joint = _resolve(client, amount=10000, filing_status="married_filing_jointly").json()
        assert single["record"]["lower_bound"] == 9001  # single's second bracket
        assert (joint["record"]["lower_bound"], joint["record"]["upper_bound"]) == (0, 18000)
        # Above the joint chain's only bracket there is nothing to resolve.
        assert (
            _resolve(client, amount=50000, filing_status="married_filing_jointly").status_code
            == 404
        )

    def test_estate_chain_resolves_by_taxpayer_class_alone(self, client: TestClient) -> None:
        _seed_chains()
        payload = _resolve(client, amount=5000, taxpayer_class="estate_or_trust").json()
        assert payload["record"]["taxpayer_class"] == "estate_or_trust"
        assert payload["record"]["filing_status"] is None
        assert Decimal(payload["record"]["rate"]) == Decimal("0.37")

    def test_superseded_chains_never_resolve(self, client: TestClient) -> None:
        _seed_chains()
        response = _resolve(client, amount=5000, filing_status="head_of_household")
        assert response.status_code == 404

    def test_no_chain_is_404(self, client: TestClient) -> None:
        _seed_chains()
        assert (
            _resolve(client, amount=5000, filing_status="single", tax_year=1999).status_code == 404
        )

    def test_missing_both_discriminators_is_422(self, client: TestClient) -> None:
        assert _resolve(client, amount=5000).status_code == 422
