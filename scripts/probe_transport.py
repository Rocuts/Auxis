"""Two probes against the configured mapper endpoint, run before a paid gate.

ADR 014 §8j. Both answer questions that a run's accuracy table cannot: one
about *access*, one about *whether the transport enforces the contract or
merely forwards it*.

Run with the mapper's own configuration so the probes describe the endpoint the
gate will actually use::

    uv run --env-file .env python -m scripts.probe_transport
    uv run --env-file .env python scripts/probe_transport.py

**Probe 1 — access.** Invokes the configured model with a trivial prompt and a
1-token ceiling. A `403` means the key is not entitled to this model (the
free tier's price ceiling returns exactly this for
``anthropic/claude-haiku-4.5``); a `429` means throttling rather than
entitlement. Either way the answer is about the account, not the model, and
the gate must not start.

**Probe 2 — enforcement.** This is the one that matters, and it reuses the
methodology that first exposed the gateway's non-enforcement for
``zai/glm-5.3-flash``: send an ``output_config`` json_schema whose ``required``
list contains a key the natural answer would omit, then ask the question whose
natural answer omits it.

- A transport that **enforces** the schema must return both keys. It has no
  choice; the constraint is applied server-side during decoding.
- A transport that **forwards** the parameter without enforcing it returns
  whatever the model felt like emitting. GLM returned ``{"answer": "four"}``,
  dropping the required ``confidence``.

The distinction is not cosmetic for this project: six gate runs were measured
on a transport that forwards, which is why ``required`` in a schema was a claim
rather than a guarantee and why the prompt had to carry the contract itself
(§8d/§8e). A single probe cannot *prove* enforcement — one compliant response
is also what a well-behaved model produces unprompted — so the probe is
designed so that only the NEGATIVE result is conclusive, and it says so in its
own output rather than overclaiming.

Nothing here reads the oracle, writes to a database, or touches the accuracy
harness. Cost is a few hundred tokens.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from tax_tables.adapters.anthropic_mapper import MapperConfig, MapperConfigError

#: A key the natural answer to the question below would not volunteer. The
#: schema demands it; only an enforcing transport can guarantee it appears.
TOY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "confidence"],
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
}

TOY_QUESTION = "What is two plus two? Answer in one word."


def _client(config: MapperConfig) -> Any:
    import anthropic

    kwargs: dict[str, Any] = {"api_key": config.api_key}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return anthropic.Anthropic(**kwargs)


def probe_access(config: MapperConfig) -> bool:
    """True when the configured model is invocable on this credential."""
    print("=" * 78)
    print("PROBE 1 - ACCESS")
    print(f"  model : {config.model}")
    print(f"  route : {config.base_url or 'https://api.anthropic.com (direct)'}")
    try:
        response = _client(config).messages.create(
            model=config.model,
            max_tokens=4,
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception as exc:  # noqa: BLE001 - the exception IS the result here
        status = getattr(exc, "status_code", None)
        print(f"  RESULT: BLOCKED ({type(exc).__name__}, status={status})")
        print(f"  detail: {str(exc)[:400]}")
        if status == 403:
            print("\n  -> 403 is an ENTITLEMENT answer, not an enforcement one.")
            print("     The credential cannot invoke this model. HOLD; do not start the gate.")
        elif status == 429:
            print("\n  -> 429 is THROUGHPUT, not entitlement. Re-probe; do not escalate.")
        return False

    meta = {
        k: v
        for k, v in (response.model_dump() if hasattr(response, "model_dump") else {}).items()
        if k in ("model", "id", "stop_reason")
    }
    print(f"  RESULT: OK  {meta}")
    for attr in ("provider", "resolved_provider", "resolvedProvider"):
        value = getattr(response, attr, None)
        if value is not None:
            print(f"  resolvedProvider: {value}")
    usage = getattr(response, "usage", None)
    if usage is not None:
        print(f"  usage: {usage}")
    return True


def probe_enforcement(config: MapperConfig) -> str:
    """Return 'enforced', 'forwarded', or 'inconclusive'."""
    print()
    print("=" * 78)
    print("PROBE 2 - ENFORCEMENT vs FORWARDING")
    print(f"  schema requires : {TOY_SCHEMA['required']}")
    print(f"  question        : {TOY_QUESTION!r}")
    print("  (the natural answer omits 'confidence' - that is the whole point)")
    try:
        response = _client(config).messages.create(
            model=config.model,
            max_tokens=256,
            messages=[{"role": "user", "content": TOY_QUESTION}],
            output_config={"format": {"type": "json_schema", "schema": TOY_SCHEMA}},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  RESULT: call failed ({type(exc).__name__}) - {str(exc)[:400]}")
        return "inconclusive"

    text = "".join(block.text for block in response.content if block.type == "text")
    print(f"  raw body: {text[:400]}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"  RESULT: FORWARDED - body is not even valid JSON ({exc})")
        return "forwarded"

    missing = [key for key in TOY_SCHEMA["required"] if key not in parsed]
    extra = [key for key in parsed if key not in TOY_SCHEMA["properties"]]
    print(f"  parsed keys: {sorted(parsed)}   missing required: {missing}   undeclared: {extra}")

    if missing or extra:
        print("\n  -> FORWARDED. The transport passed output_config through without")
        print("     enforcing it. `required` is a request, not a guarantee - exactly the")
        print("     condition the first six gate runs were measured under.")
        return "forwarded"

    print("\n  -> CONSISTENT WITH ENFORCEMENT, and deliberately not stated more strongly.")
    print("     One compliant response is also what a well-behaved model produces on its")
    print("     own. This probe can only FALSIFY enforcement, never confirm it. Label the")
    print("     arm on what the evidence supports, not on what was hoped for.")
    return "enforced"


def main() -> int:
    try:
        config = MapperConfig.from_env()
    except MapperConfigError as exc:
        print(f"config error: {exc}")
        return 2

    if not probe_access(config):
        print("\nHOLD: access is the blocker, not enforcement.")
        return 1

    verdict = probe_enforcement(config)
    print()
    print("=" * 78)
    print(f"VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
