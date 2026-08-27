"""Fence framing is tolerated at the transport boundary. Nothing else is.

The Vercel AI Gateway forwards ``output_config`` without enforcing it for a
non-Anthropic model, so the contract is honoured by instruction-following. In
practice that produces one specific artifact and no other: a complete, correct
JSON object wrapped in markdown code-fence characters — the model answering as
if it were writing to a chat window. The first measured run of
``zai/glm-5.3-flash`` returned exactly that (``stop_reason='end_turn'``, a
7,880-character valid object, then a two-backtick remnant), and the whole
document failed on two characters.

This module accommodates that framing and refuses everything else, because the
distance between "strip fence characters" and "salvage what you can from a bad
body" is the distance between a transport fix and silent data invention
(anti-goal #8). The rule is deliberately narrow:

**Accepted** — exactly one complete JSON value parses, and the only other
content is fence framing: an optional leading fence line (a backtick run,
optionally with a language tag, on its own line), an optional trailing backtick
run, and whitespace anywhere.

**Rejected, as a hard contract failure** — prose before or after the value, a
second JSON value, a truncated value, an empty body. Every one of those means
the model said something the contract has no place for, and guessing which part
was meant is exactly the failure mode this codebase exists to avoid.

**Never silent.** Each accommodation is recorded in the conformance ledger by
role and by position, and the residue rate prints beside the accuracy table. An
accommodation nobody can see is a repair; one that shows up as a measured rate
is a documented property of the model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from tax_tables.observability import conformance

#: A leading fence must be its own line: a backtick run, an optional language
#: tag ("json"), then the line break. Backticks followed by content on the same
#: line are not framing and are left alone to fail.
_LEADING_FENCE = re.compile(r"\A\s*`+[ \t]*[A-Za-z0-9_+.-]*[ \t]*\r?\n")

#: A trailing fence is a backtick run at the end of the body. The run length is
#: not checked: the measured artifact was two backticks, not three, and a
#: truncated fence is still nothing but fence characters.
_TRAILING_FENCE = re.compile(r"\s*`+\s*\Z")


def strip_fence_framing(text: str) -> tuple[str, tuple[str, ...]]:
    """Return the body with fence framing removed, and which ends carried it.

    Whitespace alone is not residue: ``json.loads`` already ignores it, so a
    body that differs from the contract only by surrounding newlines parses
    strictly and never reaches this function.
    """
    positions: list[str] = []
    inner = text
    leading = _LEADING_FENCE.match(inner)
    if leading is not None:
        inner = inner[leading.end() :]
        positions.append(conformance.RESIDUE_LEADING)
    trailing = _TRAILING_FENCE.search(inner)
    if trailing is not None:
        inner = inner[: trailing.start()]
        positions.append(conformance.RESIDUE_TRAILING)
    return inner, tuple(positions)


def loads_fence_tolerant(
    text: str,
    *,
    role: str,
    parse_float: Callable[[str], Any] | None = None,
) -> Any:
    """``json.loads`` for a model response, tolerating fence framing only.

    Raises ``json.JSONDecodeError`` — the caller's existing failure path —
    whenever the body is anything other than one JSON value inside optional
    fence framing. The error reported is always the *strict* one, so a
    traceback describes what the model actually sent rather than what was left
    after an attempted unwrap.
    """
    decoder = json.JSONDecoder(parse_float=parse_float)
    try:
        return decoder.decode(text)
    except json.JSONDecodeError as strict_error:
        inner, positions = strip_fence_framing(text)
        if not positions:
            raise
        try:
            value = decoder.decode(inner)
        except json.JSONDecodeError:
            # The framing was not the whole problem. Report the original.
            raise strict_error from None
        conformance.LEDGER.record_envelope_residue(role, positions)
        return value


# ---------------------------------------------------------------------------
# The closed shape list
# ---------------------------------------------------------------------------
#
# Two deviations beyond fence framing were measured on the baseline run, both
# structural rather than semantic, and both are repaired here — and NOWHERE
# else, so the list stays auditable in one place:
#
#   1. ``extra_attrs`` arriving as a JSON object instead of the declared array
#      of ``{key, value}`` pairs. Measured on documents 02, 03 and 04.
#   2. A number arriving as a quoted string ("0.99", "6.595"). Measured on
#      ``confidence`` (every document) and ``rate`` (47 of 51 records on
#      document 03).
#
# Both are lossless rewrites of the SAME values into the declared shape: no
# value is invented, dropped, rounded, or chosen between. Anything else — a
# missing required field the pipeline cannot derive, a value of the wrong
# semantic type, prose after the JSON — remains a hard contract failure.


def adapt_extra_attrs(value: Any, *, role: str) -> list[Any]:
    """Normalize ``extra_attrs`` to the declared list of ``{key, value}``.

    An object ``{"a": 1}`` becomes ``[{"key": "a", "value": 1}]``: the same
    pairs, in the declared container. A value that is neither a list nor a
    mapping is returned as an empty list's worth of nothing — the caller's
    existing ``or []`` handling — because inventing pairs is not repair.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        conformance.LEDGER.record_adaptation(role, conformance.ADAPT_ATTRS_OBJECT)
        return [{"key": key, "value": item} for key, item in value.items()]
    return []


def adapt_numeric(value: Any, *, role: str) -> Any:
    """Turn a quoted number into a ``Decimal``; leave everything else alone.

    Deliberately strict about what counts as a quoted number: the string must
    parse as a decimal in full. ``"6.595"`` converts; ``"No limit"``,
    ``"Ordinary rates"`` and ``""`` do not, and fall through unchanged to the
    caller's type check, which rejects them — a non-numeric string in a
    numeric slot is a semantic error and must stay one.
    """
    if not isinstance(value, str):
        return value
    try:
        converted = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return value
    if not converted.is_finite():
        # "NaN" and "Infinity" parse as Decimals and are not numbers a tax
        # value may take.
        return value
    conformance.LEDGER.record_adaptation(role, conformance.ADAPT_NUMERIC_STRING)
    return converted
