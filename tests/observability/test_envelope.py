"""The fence-framing accommodation: exactly how far it reaches, and where it
stops.

This is the one place the pipeline accepts a response the strict contract
rejects, so the tests are written from the rejection side first. Anything that
is not "one complete JSON value inside fence characters" must still fail hard —
the accommodation exists to remove two backticks, not to salvage a bad body.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from tax_tables.adapters.envelope import loads_fence_tolerant, strip_fence_framing
from tax_tables.observability import conformance
from tax_tables.observability.conformance import (
    MAPPER,
    RESIDUE_LEADING,
    RESIDUE_TRAILING,
    format_conformance_report,
)

BODY = '{"records": [], "issues": []}'


@pytest.fixture(autouse=True)
def _isolate_ledger() -> Any:
    conformance.LEDGER.reset()
    yield
    conformance.LEDGER.reset()


def _load(text: str) -> Any:
    return loads_fence_tolerant(text, role=MAPPER)


class TestAccepted:
    def test_a_clean_body_is_not_residue(self) -> None:
        assert _load(BODY) == {"records": [], "issues": []}
        assert conformance.LEDGER.snapshot() == {}

    def test_surrounding_whitespace_alone_is_not_residue(self) -> None:
        """json.loads already ignores whitespace, so a body that differs only
        by newlines parses strictly and must not be reported as accommodated."""
        assert _load(f"\n\n  {BODY}\n\n") == {"records": [], "issues": []}
        assert conformance.LEDGER.snapshot() == {}

    def test_the_measured_artifact_two_trailing_backticks(self) -> None:
        """The exact shape zai/glm-5.3-flash returned on fixture 02: a complete
        object, then a truncated closing fence."""
        assert _load(f"\n{BODY}\n``") == {"records": [], "issues": []}
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.residue_responses == 1
        assert counters.residue_trailing == 1
        assert counters.residue_leading == 0

    def test_a_full_fenced_block(self) -> None:
        assert _load(f"```json\n{BODY}\n```") == {"records": [], "issues": []}
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.residue_leading == 1
        assert counters.residue_trailing == 1
        assert counters.residue_responses == 1

    def test_a_bare_opening_fence_with_no_language_tag(self) -> None:
        assert _load(f"```\n{BODY}\n```") == {"records": [], "issues": []}
        assert conformance.LEDGER.snapshot()[MAPPER].residue_responses == 1

    def test_an_opening_fence_with_no_closing_one(self) -> None:
        assert _load(f"```json\n{BODY}") == {"records": [], "issues": []}
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.residue_leading == 1
        assert counters.residue_trailing == 0

    def test_parse_float_still_reaches_the_decoder(self) -> None:
        """Anti-float discipline survives the unwrap: 0.1 must arrive as a
        Decimal here exactly as it does on the strict path."""
        value = loads_fence_tolerant(
            '```json\n{"rate": 0.1}\n```', role=MAPPER, parse_float=Decimal
        )
        assert value["rate"] == Decimal("0.1")
        assert isinstance(value["rate"], Decimal)


class TestRejected:
    def test_prose_after_the_value_is_a_hard_failure(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _load(f"{BODY}\n\nI hope this helps!")
        assert conformance.LEDGER.snapshot() == {}

    def test_prose_before_the_value_is_a_hard_failure(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _load(f"Here are the records:\n{BODY}")

    def test_prose_inside_the_fence_is_a_hard_failure(self) -> None:
        """Stripping the framing must not turn a chatty response into a
        parseable one — the framing was not the whole problem."""
        with pytest.raises(json.JSONDecodeError):
            _load(f"```json\n{BODY}\nand a note about row 4\n```")

    def test_a_second_value_is_a_hard_failure(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _load(f"```json\n{BODY}\n{BODY}\n```")

    def test_truncated_json_is_a_hard_failure(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _load('```json\n{"records": [{"rate":')

    def test_an_empty_body_is_a_hard_failure(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _load("```json\n\n```")

    def test_backticks_with_content_on_the_same_line_are_not_framing(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _load(f"```json {BODY}```")

    def test_the_reported_error_describes_what_the_model_sent(self) -> None:
        """The strict error is re-raised, not the post-unwrap one: a traceback
        must describe the actual body, not an intermediate the caller never
        saw."""
        with pytest.raises(json.JSONDecodeError) as caught:
            _load(f"{BODY}\n\nI hope this helps!")
        assert "Extra data" in str(caught.value)


class TestStripFraming:
    def test_reports_the_positions_it_removed(self) -> None:
        inner, positions = strip_fence_framing(f"```json\n{BODY}\n```")
        assert inner.strip() == BODY
        assert positions == (RESIDUE_LEADING, RESIDUE_TRAILING)

    def test_reports_nothing_when_there_is_no_framing(self) -> None:
        inner, positions = strip_fence_framing(BODY)
        assert inner == BODY
        assert positions == ()


class TestReporting:
    def test_the_rate_and_the_position_breakdown_both_print(self) -> None:
        conformance.LEDGER.record_call(MAPPER)
        conformance.LEDGER.record_call(MAPPER)
        _load(f"\n{BODY}\n``")
        report = format_conformance_report()
        header = next(line for line in report.splitlines() if line.startswith("role"))
        assert "residue%" in header
        row = next(line for line in report.splitlines() if line.startswith(MAPPER))
        assert row.split()[-1] == "50.0%"
        assert "leading 0, trailing 1" in report

    def test_residue_never_touches_the_hard_failure_rate(self) -> None:
        """ADR 014's carve-out, enforced: the contract was met and only the
        presentation was not, so call_ok stays whole."""
        conformance.LEDGER.record_call(MAPPER)
        _load(f"\n{BODY}\n``")
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.schema_failures == 0
        assert counters.call_conformance == 1.0
        assert counters.residue_rate == 1.0
