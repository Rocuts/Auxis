"""The spec freeze, pinned by a command instead of by a sentence.

`CANONICAL_CONVENTIONS` is the frozen instruction text every semantic role
shares, and the 81/128 gate is only "a measurement of the thing that ships"
if that text is the text that was measured. The project asserted this with a
hash literal in prose.

**Adversarial review, 2026-08-27, found that the published literal
`88b9ca03eaafcf05` reproduces under no recipe.** It was hashed at every
commit in history under 11 normalisations, 3 encodings and 7 algorithms:
zero hits. Its derivation is lost, so it was never checkable — the one number
the project labelled "proof, not assurance" had no command behind it.

This file replaces the sentence with the command. Two separate claims:

1. **The value**, recomputed here on every run. If the prompt text changes by
   one character this test fails, which is what "frozen" has to mean.
2. **The invariance**, which is the claim the gate actually rests on and which
   *is* independently verifiable:

       git diff fda868a..HEAD -- src/tax_tables/adapters/anthropic_mapper.py

   is empty, so the constant is byte-identical from the SPEC FREEZE v2 commit
   through HEAD, across every gate run recorded in the dev-log.
"""

from __future__ import annotations

import hashlib

from tax_tables.adapters.anthropic_mapper import CANONICAL_CONVENTIONS

#: sha256 of the constant, UTF-8 encoded, no normalisation, first 16 hex chars.
#: Reproduce with:
#:     uv run python -c "import hashlib; \
#:     from tax_tables.adapters.anthropic_mapper import CANONICAL_CONVENTIONS as C; \
#:     print(hashlib.sha256(C.encode()).hexdigest()[:16])"
SPEC_FREEZE_SHA256_PREFIX = "a5987cc0c324d1ac"

#: Length is a second, cheaper witness: a hash mismatch says *something*
#: changed, this says how much.
SPEC_FREEZE_CHARS = 18_579


def _digest() -> str:
    return hashlib.sha256(CANONICAL_CONVENTIONS.encode("utf-8")).hexdigest()


def test_canonical_conventions_match_the_frozen_digest() -> None:
    assert _digest()[:16] == SPEC_FREEZE_SHA256_PREFIX


def test_canonical_conventions_are_the_frozen_length() -> None:
    assert len(CANONICAL_CONVENTIONS) == SPEC_FREEZE_CHARS


def test_the_documented_recipe_is_the_one_used() -> None:
    """Guards the docstring above: if someone changes the algorithm or the
    encoding to make a future mismatch go away, this fails too."""
    assert _digest() == hashlib.sha256(CANONICAL_CONVENTIONS.encode("utf-8")).hexdigest()
    assert len(_digest()) == 64
