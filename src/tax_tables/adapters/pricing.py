"""Provider-aware cache-token multipliers for the semantic layer.

Cached input tokens are not billed at the plain input rate, and the discount
is a *provider's* decision, not a universal constant. The three adapters used
to hard-code Anthropic's ratios — cache write 1.25x input, cache read 0.1x —
which is correct on Anthropic's own API and on the gateway's ``anthropic/*``
ids, and wrong for every other family. Running the mapper on
``zai/glm-5.3-flash`` at Anthropic's 0.1x under-reports its cache reads by
half; running the verifier on ``alibaba/qwen-3-235b`` — which publishes no
cache pricing at all yet *does* return ``cache_read_input_tokens`` — would
invent a 90% discount out of nothing.

Under-reporting is the dangerous direction for a number that ships in a
README, so the fallback rule is deliberately asymmetric: **a provider with no
published cache discount is billed at the full input rate** (factor 1.0), never
at another vendor's discount.

Resolution ladder, most specific first:

1. an explicit ``*_CACHE_READ_FACTOR`` / ``*_CACHE_WRITE_FACTOR`` env var
   (applied by each role's ``from_env``, under the same ``same_engine`` rule
   that governs the per-token prices);
2. an exact model id in ``_MODEL_CACHE_FACTORS``;
3. the model's provider namespace in ``_PROVIDER_CACHE_FACTORS``;
4. ``_NO_PUBLISHED_DISCOUNT`` — full input rate for both.

Source for every ratio below: the Vercel AI Gateway model catalogue, read
2026-08-26::

    curl -s https://ai-gateway.vercel.sh/v1/models

Each entry is ``pricing.input_cache_read / pricing.input`` and
``pricing.input_cache_write / pricing.input`` for that id. Refresh with the
same query; an env override always wins, so a stale row is a wrong default,
never a wrong run.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CacheFactors:
    """Cache prices as multiples of the model's plain input price."""

    read: Decimal
    write: Decimal


#: Anthropic's published billing model, and the historical default of all
#: three adapters. Confirmed against the catalogue: ``anthropic/claude-opus-5``
#: prices cache read at $0.50 against $5.00 input (0.1x) and cache write at
#: $6.25 (1.25x); ``anthropic/claude-haiku-4.5`` gives $0.10/$1.25 against
#: $1.00 (the same ratios).
ANTHROPIC_CACHE_FACTORS = CacheFactors(read=Decimal("0.1"), write=Decimal("1.25"))

#: A provider that publishes no cache discount is billed at the full input
#: rate. Never guess another vendor's discount onto an unpriced model.
_NO_PUBLISHED_DISCOUNT = CacheFactors(read=Decimal(1), write=Decimal(1))

#: Exact ids, for the models this project actually configures. The z.ai family
#: does not share one ratio (its catalogue rows range from 0.1x to 0.2x), so
#: the id this project runs carries its own row rather than inheriting the
#: family approximation below.
_MODEL_CACHE_FACTORS: dict[str, CacheFactors] = {
    # $0.015 cache read against $0.075 input = 0.2x. No cache-write price is
    # published for any zai model, so writes bill at the input rate.
    "zai/glm-5.3-flash": CacheFactors(read=Decimal("0.2"), write=Decimal(1)),
    # $0.22 input, $0.88 output, and NO cache pricing of any kind — while the
    # gateway does report cache_read_input_tokens for this model. Full rate.
    "alibaba/qwen-3-235b": _NO_PUBLISHED_DISCOUNT,
}

#: Provider namespace of a gateway model id ("zai/glm-5.3-flash" -> "zai").
#: A bare id with no namespace is Anthropic's own API (``claude-opus-5``).
_PROVIDER_CACHE_FACTORS: dict[str, CacheFactors] = {
    "anthropic": ANTHROPIC_CACHE_FACTORS,
    # Modal ratio across the zai catalogue rows that publish one; no zai row
    # publishes a cache-write price.
    "zai": CacheFactors(read=Decimal("0.2"), write=Decimal(1)),
    # Alibaba publishes no cache prices anywhere in the qwen family.
    "alibaba": _NO_PUBLISHED_DISCOUNT,
}


def cache_factors_for(model: str) -> CacheFactors:
    """The cache multipliers to bill ``model``'s cached tokens at.

    Falls back to the full input rate for anything unrecognised, so a model
    this table has never heard of is over-reported rather than under-reported.
    """
    exact = _MODEL_CACHE_FACTORS.get(model)
    if exact is not None:
        return exact
    provider = model.split("/", 1)[0] if "/" in model else "anthropic"
    return _PROVIDER_CACHE_FACTORS.get(provider, _NO_PUBLISHED_DISCOUNT)
