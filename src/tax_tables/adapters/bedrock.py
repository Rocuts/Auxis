"""Bedrock adapters for the semantic layer — the AWS column of the ports table.

`SchemaMapper`, `RecordVerifier` and `Adjudicator` all read "Bedrock" in the
AWS column of CLAUDE.md's ports table (ADR 012 defines the three roles). This
module *is* that column, and it is deliberately thin: three factories that
resolve per-role configuration and construct an ``anthropic.AnthropicBedrock``
client, then hand both to the adapters that already exist.

Nothing about the semantic layer is re-implemented here. The prompts, the JSON
schemas, the fail-closed parsers, the provenance checks and the cost formulas
are the ones in ``anthropic_mapper`` / ``anthropic_verifier`` /
``anthropic_adjudicator``, imported rather than copied. Those adapters take an
injectable ``client`` exposing ``messages.stream(**kwargs)`` with a
``get_final_message()`` context manager, and ``AnthropicBedrock`` has exactly
that shape — it signs requests with SigV4 from the caller's AWS credentials
(the Lambda execution role) instead of sending an API key. Changing targets is
therefore a transport swap and a config resolution, which is what the
hexagonal claim is supposed to mean in practice.

Two consequences of the SigV4 transport are stated rather than left to be
discovered:

- **No API key exists on this target.** The existing config classes require a
  non-empty key, so ``_bedrock_env`` supplies the sentinel ``"sigv4"``. It is
  not a credential, it is never transmitted, and it never displaces a real
  one: an explicit key in the environment still wins (``setdefault``).
- **Model identifiers differ.** Bedrock ids carry the vendor prefix
  ("anthropic.claude-opus-5"), so the default here differs from the direct
  API's "claude-opus-5". The per-role *variable names* do not differ —
  ``SCHEMA_MAPPER_MODEL`` / ``RECORD_VERIFIER_MODEL`` / ``ADJUDICATOR_MODEL``
  are the same on every target, which is why the Lambda environments in
  ``infra/tax_tables_stack.py`` set exactly those.

Structured outputs, honestly: the reused adapters send ``output_config`` with
a ``json_schema`` format exactly as they do against the direct API, and this
module does not soften that. If a Bedrock runtime rejects the field or ignores
it, the failure is loud — the request errors, or the fail-closed parsers raise
``MapperError`` / ``VerifierError`` / ``AdjudicatorError`` on a body that is
not the contracted JSON. What cannot happen is silent degradation into
half-parsed records (anti-goal #8). Confirming that a live Bedrock endpoint
honours the field is a deploy-time item, and the CDK stack is synth-only: it
has never been deployed, so this module's behaviour against real Bedrock is
unverified by construction (README's honest-limitations section).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import anthropic

# The per-role request timeouts are imported, not re-declared: they are each
# adapter's own tuning (900s for a 50-record generation, 300s for a one-item
# adjudication), and a copy here would drift the first time one of them is
# retuned. Private by name because they are that module's business — this
# module is the one caller with a legitimate need to mirror them exactly.
from tax_tables.adapters.anthropic_adjudicator import (
    _MAX_RETRIES as _ADJUDICATOR_MAX_RETRIES,
)
from tax_tables.adapters.anthropic_adjudicator import (
    _REQUEST_TIMEOUT_SECONDS as _ADJUDICATOR_TIMEOUT_SECONDS,
)
from tax_tables.adapters.anthropic_adjudicator import AdjudicatorConfig, AnthropicAdjudicator
from tax_tables.adapters.anthropic_mapper import (
    _REQUEST_TIMEOUT_SECONDS as _MAPPER_TIMEOUT_SECONDS,
)
from tax_tables.adapters.anthropic_mapper import AnthropicSchemaMapper, MapperConfig
from tax_tables.adapters.anthropic_verifier import (
    _REQUEST_TIMEOUT_SECONDS as _VERIFIER_TIMEOUT_SECONDS,
)
from tax_tables.adapters.anthropic_verifier import AnthropicRecordVerifier, VerifierConfig

#: The Bedrock foundation-model id for all three semantic roles. It must equal
#: ``BEDROCK_MODEL_ID`` in ``infra/tax_tables_stack.py`` (line 76 at the time
#: of writing), which is what the stack puts in the Lambda environments —
#: otherwise a synthesized template would grant and configure one model while
#: the code asked for another. A unit test parses the stack file and pins the
#: equality, so drift fails a test instead of a deployment.
BEDROCK_DEFAULT_MODEL = "anthropic.claude-opus-5"

#: Placeholder for the API key the existing configs require and Bedrock never
#: uses. Spelled as the literal transport name so that anything that does
#: manage to print it reads as "this call was signed", not as a leaked
#: credential (anti-goal #10). The configs keep ``api_key`` out of their repr
#: regardless.
SIGV4_SENTINEL = "sigv4"

#: Retry budget of the direct-API adapters' ``_build_client`` (a literal ``3``
#: in all three of them), carried over so a throttled Bedrock call behaves
#: like a throttled direct-API call.
_MAX_RETRIES = 3


class BedrockConfigError(RuntimeError):
    """The environment does not describe a usable Bedrock endpoint."""


def _bedrock_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment as the existing config classes need to see it on AWS.

    Two defaults, both ``setdefault`` so an explicit value always wins:

    - ``SCHEMA_MAPPER_MODEL``: the verifier's and adjudicator's config chains
      already fall back to the mapper's model, so this one default covers all
      three roles, and ``RECORD_VERIFIER_MODEL`` / ``ADJUDICATOR_MODEL`` still
      override per role (ADR 012's conformity mitigation, unchanged on AWS).
    - ``SCHEMA_MAPPER_API_KEY``: the sentinel described in the module
      docstring. ``AnthropicBedrock`` authenticates with SigV4 and never sends
      it; it exists only so ``MapperConfig.from_env`` and friends — which
      correctly refuse to run against the direct API without a key — are
      satisfiable on a target that has no key to give them.
    """
    source = os.environ if env is None else env
    resolved = dict(source)
    resolved.setdefault("SCHEMA_MAPPER_MODEL", BEDROCK_DEFAULT_MODEL)
    resolved.setdefault("SCHEMA_MAPPER_API_KEY", SIGV4_SENTINEL)
    return resolved


def _region(env: Mapping[str, str] | None = None) -> str:
    """The AWS region to sign for.

    Lambda always sets ``AWS_REGION``; ``AWS_DEFAULT_REGION`` is the CLI/SDK
    spelling a local run is more likely to have. Neither present is a
    configuration error, not a default-to-us-east-1: signing against the wrong
    region fails at the far end with a message about credentials, which is a
    much worse way to learn this.
    """
    source = os.environ if env is None else env
    region = source.get("AWS_REGION") or source.get("AWS_DEFAULT_REGION")
    if not region:
        raise BedrockConfigError("no AWS region for Bedrock: set AWS_REGION or AWS_DEFAULT_REGION")
    return region


def _client(
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = _MAPPER_TIMEOUT_SECONDS,
    max_retries: int = _MAX_RETRIES,
) -> anthropic.AnthropicBedrock:
    """A Bedrock-signing client with the calling role's timeout budget.

    Each factory passes its own role's timeout (the mapper's and the
    verifier's agree at 900s; per-item adjudication runs on a shorter one);
    the default is the mapper's, so ``_client(env)`` alone is still sensible.
    All three values are *imported* from the direct-API adapters rather than
    copied, so a retune there cannot silently leave AWS on a stale budget —
    and a rename fails loudly at import instead of drifting.

    ``max_retries`` travels the same way, and for the same reason: the
    adjudicator's retry count was cut with its timeout (gate 3.5-LIVE), and a
    per-item budget is the product of the two. Importing only one of them
    would have left this target with a quarter of the intended ceiling.
    """
    return anthropic.AnthropicBedrock(
        aws_region=_region(env),
        timeout=timeout,
        max_retries=max_retries,
    )


def bedrock_mapper(
    env: Mapping[str, str] | None = None,
    *,
    client: Any | None = None,
) -> AnthropicSchemaMapper:
    """The SchemaMapper as it runs on AWS: same adapter, Bedrock transport.

    ``client`` is injectable for tests, exactly as on the adapter itself. It
    is always supplied here, so ``MapperConfig.base_url`` — meaningful only to
    the adapter's own direct-API client builder — is inert on this target.
    """
    resolved = _bedrock_env(env)
    if client is None:
        client = _client(resolved, timeout=_MAPPER_TIMEOUT_SECONDS)
    return AnthropicSchemaMapper(MapperConfig.from_env(resolved), client=client)


def bedrock_verifier(
    env: Mapping[str, str] | None = None,
    *,
    client: Any | None = None,
) -> AnthropicRecordVerifier:
    """The RecordVerifier as it runs on AWS.

    ``RECORD_VERIFIER_MODEL`` still points this role at a different model from
    the mapper's — the whole point of ADR 012's conformity mitigation — and
    the price-inheritance rule of ``VerifierConfig.from_env`` still applies:
    a verifier pointed at another model does not inherit the mapper's prices.
    """
    resolved = _bedrock_env(env)
    if client is None:
        client = _client(resolved, timeout=_VERIFIER_TIMEOUT_SECONDS)
    return AnthropicRecordVerifier(VerifierConfig.from_env(resolved), client=client)


def bedrock_adjudicator(
    env: Mapping[str, str] | None = None,
    *,
    client: Any | None = None,
) -> AnthropicAdjudicator:
    """The Adjudicator as it runs on AWS (one call per open queue item)."""
    resolved = _bedrock_env(env)
    if client is None:
        client = _client(
            resolved,
            timeout=_ADJUDICATOR_TIMEOUT_SECONDS,
            max_retries=_ADJUDICATOR_MAX_RETRIES,
        )
    return AnthropicAdjudicator(AdjudicatorConfig.from_env(resolved), client=client)
