"""Per-role routing, pinned so a two-endpoint topology cannot ship broken.

ADR 014 §8i, scoped by §8j. **The arm actually running today needs none of
this**: the operator overrode the venue, so mapper, verifier and adjudicator
all speak to the AI Gateway on one credential (``TestGatewayArmAsShipped``
below pins that). What the rest of this file pins is the *mechanism a
direct-Anthropic or Bedrock arm would use* — kept as armor because the hazard
it demonstrates is real for any topology that splits roles across two
endpoints, and because proving it costs milliseconds.

In that split topology the mapper and adjudicator sit on the DIRECT Anthropic
route while the verifier stays on the AI Gateway running a different model
family — ADR 012's conformity mitigation, which is the whole reason the
verifier exists.

Every role's config resolves through the same fallback chain::

    <ROLE>_<VAR>  ->  SCHEMA_MAPPER_<VAR>  ->  ANTHROPIC_<VAR>

That chain is a convenience for the single-endpoint case and a **trap** for this
one. Move the mapper to the direct route and the verifier's unset ``BASE_URL``
and ``API_KEY`` fall through to the mapper's — so ``alibaba/qwen-3-235b`` gets
posted to ``api.anthropic.com`` with an ``sk-ant`` key. Every verifier call
fails, and the accuracy harness fails the gate on a verifier failure exactly as
it does on a mapper failure ("128/128 through the two-agent layer", not
"128/128 while the second agent was down"). One paid run, lost to an unset
variable.

That hazard was found by reading the chain before the run rather than after it,
and this test is what keeps it found. It is keyless and offline: every case
builds its config from an explicit mapping, so nothing here reads the ambient
environment or spends a cent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tax_tables.adapters.anthropic_adjudicator import AdjudicatorConfig
from tax_tables.adapters.anthropic_mapper import MapperConfig
from tax_tables.adapters.anthropic_verifier import VerifierConfig

GATEWAY = "https://ai-gateway.vercel.sh"

#: The mapper/adjudicator model for the enforcement arm. A bare id with no
#: provider namespace IS the direct Anthropic route (see adapters/pricing.py),
#: which is what makes ``base_url`` unset the correct expression of "direct".
DIRECT_MODEL = "claude-haiku-4-5"

#: The verifier stays on the gateway, on a different family (ADR 012).
GATEWAY_MODEL = "alibaba/qwen-3-235b"


def _enforcement_arm_env() -> dict[str, str]:
    """The intended topology, with placeholder credentials.

    Mirrors the variables the operator sets in ``.env``; the values here are
    structural stand-ins, never real keys (anti-goal #10).
    """
    return {
        # Mapper -> direct. base_url deliberately ABSENT: unset means direct.
        "SCHEMA_MAPPER_API_KEY": "sk-ant-placeholder",
        "SCHEMA_MAPPER_MODEL": DIRECT_MODEL,
        "SCHEMA_MAPPER_USD_PER_MTOK_IN": "1",
        "SCHEMA_MAPPER_USD_PER_MTOK_OUT": "5",
        # Verifier -> gateway. BOTH of these are load-bearing.
        "RECORD_VERIFIER_API_KEY": "vck_placeholder",
        "RECORD_VERIFIER_BASE_URL": GATEWAY,
        "RECORD_VERIFIER_MODEL": GATEWAY_MODEL,
        "RECORD_VERIFIER_USD_PER_MTOK_IN": "0.22",
        "RECORD_VERIFIER_USD_PER_MTOK_OUT": "0.88",
        # Adjudicator -> nothing set: it inherits the mapper, which is the
        # intent (same route, same model, same prices).
    }


def _gateway_arm_env() -> dict[str, str]:
    """The topology as shipped (ADR 014 §8j): one endpoint, one credential."""
    return {
        "SCHEMA_MAPPER_API_KEY": "vck_placeholder",
        "SCHEMA_MAPPER_BASE_URL": GATEWAY,
        "SCHEMA_MAPPER_MODEL": "anthropic/claude-haiku-4.5",
        "SCHEMA_MAPPER_USD_PER_MTOK_IN": "1",
        "SCHEMA_MAPPER_USD_PER_MTOK_OUT": "5",
        "RECORD_VERIFIER_MODEL": GATEWAY_MODEL,
        "RECORD_VERIFIER_USD_PER_MTOK_IN": "0.22",
        "RECORD_VERIFIER_USD_PER_MTOK_OUT": "0.88",
    }


class TestGatewayArmAsShipped:
    """The escalated arm the operator actually funded — no split, no second key."""

    def test_all_three_roles_share_one_endpoint(self) -> None:
        env = _gateway_arm_env()
        routes = {
            MapperConfig.from_env(env).base_url,
            VerifierConfig.from_env(env).base_url,
            AdjudicatorConfig.from_env(env).base_url,
        }
        assert routes == {GATEWAY}, "one venue; §8i's cross-endpoint hazard cannot arise"

    def test_all_three_roles_share_one_credential(self) -> None:
        env = _gateway_arm_env()
        keys = {
            MapperConfig.from_env(env).api_key,
            VerifierConfig.from_env(env).api_key,
            AdjudicatorConfig.from_env(env).api_key,
        }
        assert len(keys) == 1, "no secret rotation is the point of the override"

    def test_cross_family_mitigation_survives_the_venue_override(self) -> None:
        """The verifier must still be a different family from the mapper."""
        env = _gateway_arm_env()
        mapper = MapperConfig.from_env(env)
        verifier = VerifierConfig.from_env(env)
        assert mapper.model.split("/")[0] == "anthropic"
        assert verifier.model.split("/")[0] == "alibaba"

    def test_namespaced_haiku_still_resolves_anthropic_cache_ratios(self) -> None:
        """The gateway id carries a provider namespace; the ratios must follow
        the provider, not the venue."""
        config = MapperConfig.from_env(_gateway_arm_env())
        assert config.cache_read_factor == Decimal("0.1")
        assert config.cache_write_factor == Decimal("1.25")

    def test_adjudicator_inherits_the_escalated_model_and_its_prices(self) -> None:
        env = _gateway_arm_env()
        mapper = MapperConfig.from_env(env)
        adjudicator = AdjudicatorConfig.from_env(env)
        assert adjudicator.model == mapper.model == "anthropic/claude-haiku-4.5"
        assert adjudicator.usd_per_mtok_in == mapper.usd_per_mtok_in == Decimal("1")
        assert adjudicator.usd_per_mtok_out == mapper.usd_per_mtok_out == Decimal("5")

    def test_verifier_keeps_its_own_prices_because_it_runs_another_model(self) -> None:
        """A role pointed elsewhere must never be billed at the mapper's rate."""
        config = VerifierConfig.from_env(_gateway_arm_env())
        assert config.usd_per_mtok_in == Decimal("0.22")
        assert config.usd_per_mtok_out == Decimal("0.88")
        assert config.cache_read_factor == Decimal("1"), "qwen publishes no cache discount"


class TestEnforcementArmTopology:
    """The DIRECT-route arm's wiring: proven, pinned, and not in use today."""

    def test_mapper_is_on_the_direct_route(self) -> None:
        config = MapperConfig.from_env(_enforcement_arm_env())
        assert config.base_url is None, "unset base_url is how 'direct' is expressed"
        assert config.model == DIRECT_MODEL

    def test_verifier_stays_on_the_gateway_with_a_different_family(self) -> None:
        config = VerifierConfig.from_env(_enforcement_arm_env())
        assert config.base_url == GATEWAY
        assert config.model == GATEWAY_MODEL

    def test_verifier_uses_its_own_credential(self) -> None:
        """Two endpoints require two credentials; a shared one cannot work."""
        env = _enforcement_arm_env()
        mapper = MapperConfig.from_env(env)
        verifier = VerifierConfig.from_env(env)
        assert verifier.api_key != mapper.api_key

    def test_adjudicator_inherits_the_mapper_route_and_model(self) -> None:
        env = _enforcement_arm_env()
        mapper = MapperConfig.from_env(env)
        adjudicator = AdjudicatorConfig.from_env(env)
        assert adjudicator.base_url == mapper.base_url is None
        assert adjudicator.model == mapper.model == DIRECT_MODEL

    def test_adjudicator_inherits_mapper_prices_because_it_runs_that_model(self) -> None:
        env = _enforcement_arm_env()
        mapper = MapperConfig.from_env(env)
        adjudicator = AdjudicatorConfig.from_env(env)
        assert adjudicator.usd_per_mtok_in == mapper.usd_per_mtok_in
        assert adjudicator.usd_per_mtok_out == mapper.usd_per_mtok_out

    def test_cross_family_independence_survives_the_escalation(self) -> None:
        """ADR 012's mitigation is the point of the verifier; it must not be
        collapsed into a same-family echo by the route change."""
        env = _enforcement_arm_env()
        mapper = MapperConfig.from_env(env)
        verifier = VerifierConfig.from_env(env)
        assert verifier.model != mapper.model
        assert verifier.model.split("/")[0] != "anthropic"


class TestTheHazardThisTestExistsFor:
    def test_unpinned_verifier_follows_the_mapper_to_the_wrong_endpoint(self) -> None:
        """The naive escalation, demonstrated rather than described.

        Drop the two ``RECORD_VERIFIER_`` routing variables and the fallback
        chain silently posts a non-Anthropic model to Anthropic's API.
        """
        env = _enforcement_arm_env()
        del env["RECORD_VERIFIER_BASE_URL"]
        del env["RECORD_VERIFIER_API_KEY"]

        verifier = VerifierConfig.from_env(env)
        mapper = MapperConfig.from_env(env)

        # This is the bug, asserted so its shape is unmistakable:
        assert verifier.base_url is None, "fell through to the direct route"
        assert verifier.api_key == mapper.api_key, "fell through to the sk-ant key"
        assert verifier.model == GATEWAY_MODEL, "…while still asking for qwen"
        # i.e. POST https://api.anthropic.com  {"model": "alibaba/qwen-3-235b"}


class TestPricingResolvesWithoutBeingTold:
    def test_direct_haiku_self_resolves_anthropic_cache_ratios(self) -> None:
        """0.1x read / 1.25x write, from the model id alone.

        Confirmed against the live catalogue: Claude Haiku 4.5 prices input at
        $1.00/Mtok, cache read at $0.10 and cache write at $1.25 — the ratios
        ADR 014 §3 pre-registered before the escalation was funded.
        """
        config = MapperConfig.from_env(_enforcement_arm_env())
        assert config.cache_read_factor == Decimal("0.1")
        assert config.cache_write_factor == Decimal("1.25")

    def test_gateway_verifier_does_not_borrow_anthropic_cache_discounts(self) -> None:
        """qwen publishes no cache price, so cached tokens bill at full input
        rate rather than another vendor's discount."""
        config = VerifierConfig.from_env(_enforcement_arm_env())
        assert config.cache_read_factor == Decimal("1")
        assert config.cache_write_factor == Decimal("1")


@pytest.mark.parametrize(
    "missing",
    ["RECORD_VERIFIER_BASE_URL", "RECORD_VERIFIER_API_KEY"],
)
def test_each_verifier_routing_variable_is_independently_required(missing: str) -> None:
    """Neither of the two is redundant: dropping either one alone already
    breaks the topology, in a different way each time."""
    env = _enforcement_arm_env()
    del env[missing]
    verifier = VerifierConfig.from_env(env)
    mapper = MapperConfig.from_env(env)

    if missing == "RECORD_VERIFIER_BASE_URL":
        assert verifier.base_url is None  # right key, wrong endpoint
        assert verifier.api_key != mapper.api_key
    else:
        assert verifier.base_url == GATEWAY  # right endpoint, wrong key
        assert verifier.api_key == mapper.api_key
