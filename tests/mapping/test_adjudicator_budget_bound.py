"""One adjudication item must not be able to outrun the pass's own budget.

Measured on production, gate 3.5-LIVE, 2026-08-27. A wall-clock budget was
added to the adjudication pass and it still did not bind: document 02
persisted its 8 records correctly and then overran a 1800 s invocation
**three times**. The budget was checked BETWEEN items, while a single
``adjudicate()`` call could spend the adapter's request timeout once per SDK
attempt — 300 s x (1 original + 3 retries) = 1200 s — so the very first item
blew past a 420 s budget before the loop ever got to look at the clock again.

A between-item deadline is only meaningful if a single item is bounded by
something smaller. That is the invariant this file pins, in the same style as
``tests/api/test_jobs.py::TestLeaseInvariant``: a coupling nobody checks is a
coupling that drifts.
"""

from __future__ import annotations

import inspect

from tax_tables.adapters import anthropic_adjudicator
from tax_tables.service.jobs import DEFAULT_ADJUDICATION_BUDGET_SECONDS


def _configured_max_retries() -> int:
    """The retry count the adjudicator's client is built with.

    Asserted against the module constant rather than a live client, because
    constructing one needs credentials. The builder is checked separately to
    prove it actually uses the constant.
    """
    return anthropic_adjudicator._MAX_RETRIES


def test_the_client_builder_uses_the_bounded_constants() -> None:
    """Guards the indirection: constants nobody wires in bound nothing."""
    source = inspect.getsource(anthropic_adjudicator.AnthropicAdjudicator._build_client)
    assert "max_retries=_MAX_RETRIES" in source
    assert "timeout=_REQUEST_TIMEOUT_SECONDS" in source


def worst_case_item_seconds() -> float:
    """Longest one item can take: the timeout, once per SDK attempt."""
    attempts = 1 + _configured_max_retries()
    return anthropic_adjudicator._REQUEST_TIMEOUT_SECONDS * attempts


class TestAdjudicatorIsBoundedPerItem:
    def test_one_item_cannot_exhaust_the_pass_budget(self) -> None:
        """The regression, stated as a number: before the fix this was
        1200 s against a 420 s budget."""
        assert worst_case_item_seconds() <= DEFAULT_ADJUDICATION_BUDGET_SECONDS

    def test_a_full_pass_still_fits_a_single_invocation(self) -> None:
        """The budget bounds items STARTED, so the pass can overshoot by at
        most one item's worth. Budget plus one item must still leave room for
        extraction, mapping and verification inside the function's
        ``maxDuration`` — the whole point is that the job reaches a terminal
        state."""
        overshoot = DEFAULT_ADJUDICATION_BUDGET_SECONDS + worst_case_item_seconds()
        assert overshoot <= 900

    def test_retries_are_not_disabled_entirely(self) -> None:
        """Bounded, not removed. A single transport blip should still be
        retried once — dropping to zero retries would trade a slow pass for a
        brittle one, and a failed adjudication costs a queue item its
        proposal."""
        assert _configured_max_retries() >= 1
