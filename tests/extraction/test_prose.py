"""Prose-kind heuristic: advisory, but pinned so it stays predictable."""

from __future__ import annotations

import pytest

from tax_tables.extraction.model import ProseKind
from tax_tables.extraction.prose import classify_block


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("(a) The additional amount is allowed once for age.", ProseKind.FOOTNOTE),
        ("NOTE. An additional surtax of 3.8 percent applies.", ProseKind.FOOTNOTE),
        ("Notes: The combined rate is the arithmetic sum.", ProseKind.FOOTNOTE),
        ("Source: Office of Tax Policy, release 2026-A.", ProseKind.FOOTNOTE),
        ("Table 1. Ordinary Income Rate Schedules, Tax Year 2026", ProseKind.HEADING),
        ("Standard Deduction Schedule", ProseKind.HEADING),
        ("This bulletin states the amounts.\nAll amounts are in dollars.", ProseKind.BODY),
        ("A single line that ends like a sentence does.", ProseKind.BODY),
    ],
)
def test_classify_block(text: str, kind: ProseKind) -> None:
    assert classify_block(text) == kind
