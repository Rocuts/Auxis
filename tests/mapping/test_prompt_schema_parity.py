"""Every schema-required key must be NAMED in its role's prompt text.

Three times this project lost records to the same shape: the pipeline knew
something it never told the model, and a transport that does not enforce
``output_config`` made the silence expensive.

1. The verifier's response envelope was called ``verdicts`` in the schema and
   nowhere in the prompt, while the prompt named ``issues`` — a key the
   verifier's own schema forbids. Document 05 lost verification three times.
2. The mapper's fifteen required record keys were declared in
   ``_RECORD_SCHEMA["required"]`` and never enumerated in the prompt.
   Document 03 lost all 51 records to one omitted ``confidence``.
3. Same class, caught here instead of in a gate.

``required`` in a JSON schema is a claim the gateway does not check, so the
prompt is the ONLY channel that actually carries the contract. This test
makes the gap unrepresentable: add a required key without naming it in the
prompt and the suite fails, keyless and offline, in milliseconds — instead of
thirty-one minutes and a document's worth of records later (ADR 014 §8d/§8e).
"""

from __future__ import annotations

from typing import Any

import pytest

from tax_tables.adapters import anthropic_adjudicator, anthropic_mapper, anthropic_verifier


def required_keys(schema: Any) -> set[str]:
    """Every key named by a ``required`` list anywhere in the schema tree.

    Walks nested objects and array items, so a key required only inside
    ``provenance`` or inside one ``verdicts`` entry counts exactly like a
    top-level one — the model has to emit it either way.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in node.get("required") or []:
                if isinstance(key, str):
                    found.add(key)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return found


ROLES = [
    pytest.param(
        anthropic_mapper.RESPONSE_SCHEMA,
        anthropic_mapper.SYSTEM_PROMPT,
        id="mapper",
    ),
    pytest.param(
        anthropic_verifier.RESPONSE_SCHEMA,
        anthropic_verifier.VERIFIER_SYSTEM_PROMPT,
        id="verifier",
    ),
    pytest.param(
        anthropic_adjudicator.RESPONSE_SCHEMA,
        anthropic_adjudicator.ADJUDICATOR_SYSTEM_PROMPT,
        id="adjudicator",
    ),
]


class TestPromptSchemaParity:
    @pytest.mark.parametrize(("schema", "prompt"), ROLES)
    def test_every_required_key_is_named_in_the_prompt(
        self, schema: dict[str, Any], prompt: str
    ) -> None:
        missing = sorted(key for key in required_keys(schema) if key not in prompt)
        assert not missing, (
            "schema requires keys the prompt never names, so a non-enforcing "
            f"gateway has no way to learn them: {missing}"
        )

    @pytest.mark.parametrize(("schema", "prompt"), ROLES)
    def test_the_schema_actually_requires_something(
        self, schema: dict[str, Any], prompt: str
    ) -> None:
        """Guards the guard: an empty required set would pass the parity
        assertion vacuously."""
        assert required_keys(schema)

    def test_no_role_is_told_to_emit_another_role_s_envelope(self) -> None:
        """The defect behind §8d, pinned directly.

        Output discipline used to live inside the shared conventions, so every
        role inherited the mapper's and was told to put commentary in
        ``issues`` — a key only the mapper's schema has, and one the other two
        forbid under ``additionalProperties: False``.
        """
        for schema, prompt in (
            (anthropic_verifier.RESPONSE_SCHEMA, anthropic_verifier.VERIFIER_SYSTEM_PROMPT),
            (
                anthropic_adjudicator.RESPONSE_SCHEMA,
                anthropic_adjudicator.ADJUDICATOR_SYSTEM_PROMPT,
            ),
        ):
            own = required_keys(schema)
            assert "issues" not in own  # precondition of the check below
            # The word may appear only while being disowned, never as an
            # instruction: every mention must sit near a negation.
            for line in prompt.splitlines():
                if '"issues"' in line:
                    assert "no " in line.lower() or "not " in line.lower(), line
