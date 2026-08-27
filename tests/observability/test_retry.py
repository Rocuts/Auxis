"""The bounded retry policy.

Justified by measurement rather than instinct: the baseline run's mapper
appended prose after a complete JSON value on 3 of 4 bodies on document 01 and
2 of 5 gate calls, which is a per-call coin flip rather than an inability. The
tests below pin the three properties that keep retrying honest — it is bounded,
every attempt is counted, and it never merges or salvages across attempts.
"""

from __future__ import annotations

from typing import Any

import pytest

from tax_tables.adapters.retry import with_bounded_retries
from tax_tables.observability import conformance
from tax_tables.observability.conformance import MAPPER, ConformanceLedger


class _Contract(Exception):
    pass


class _Transport(Exception):
    pass


@pytest.fixture(autouse=True)
def _isolate_ledger() -> Any:
    conformance.LEDGER.reset()
    yield
    conformance.LEDGER.reset()


def _run(outcomes: list[Any], **kwargs: Any) -> tuple[Any, list[float]]:
    """Drive the helper through a scripted sequence of attempt outcomes."""
    slept: list[float] = []
    remaining = list(outcomes)

    def operation() -> str:
        result = remaining.pop(0)
        if isinstance(result, Exception):
            raise result
        return str(result)

    value = with_bounded_retries(
        operation,
        role=MAPPER,
        contract_error=_Contract,
        contract_backoff=3.0,
        transport_backoff=300.0,
        sleep=slept.append,
        **kwargs,
    )
    return value, slept


class TestBoundedness:
    def test_a_clean_first_attempt_does_not_retry(self) -> None:
        value, slept = _run(["ok"])
        assert value == "ok"
        assert slept == []
        assert conformance.LEDGER.snapshot()[MAPPER].calls == 1

    def test_it_recovers_from_a_transient_contract_failure(self) -> None:
        """The measured failure mode: one body carries prose, the next does
        not. Re-asking is the whole remedy."""
        value, slept = _run([_Contract("Extra data"), "ok"])
        assert value == "ok"
        assert slept == [3.0]

    def test_it_gives_up_after_exactly_two_retries(self) -> None:
        with pytest.raises(_Contract):
            _run([_Contract("a"), _Contract("b"), _Contract("c")])
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.calls == 3
        assert counters.schema_failures == 3

    def test_retries_are_configurable_to_zero(self) -> None:
        with pytest.raises(_Contract):
            _run([_Contract("a"), "ok"], retries=0)
        assert conformance.LEDGER.snapshot()[MAPPER].calls == 1

    def test_the_final_exception_propagates_unchanged(self) -> None:
        """Callers upstream must see exactly what they would have seen with no
        retry policy at all — the harness reports on that message."""
        with pytest.raises(_Contract, match="third"):
            _run([_Contract("first"), _Contract("second"), _Contract("third")])


class TestEveryAttemptIsCounted:
    def test_retrying_depresses_the_rate_rather_than_hiding_behind_it(self) -> None:
        """Two attempts to emit one schema is 50% conformance, and the table
        must say so. A retry that reported 100% would turn the instrument into
        an advertisement."""
        value, _ = _run([_Contract("Extra data"), "ok"])
        assert value == "ok"
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.calls == 2
        assert counters.schema_failures == 1
        assert counters.call_conformance == 0.5

    def test_transport_failures_stay_out_of_the_rate(self) -> None:
        value, _slept = _run([_Transport("RateLimitError"), "ok"])
        assert value == "ok"
        counters = conformance.LEDGER.snapshot()[MAPPER]
        assert counters.calls == 2
        assert counters.transport_failures == 1
        assert counters.schema_failures == 0
        # Two calls, one of which never answered: the one that did was clean.
        assert counters.responses == 1
        assert counters.call_conformance == 1.0

    def test_the_two_backoffs_are_distinct(self) -> None:
        """A contract failure is settled the moment the body lands, so re-ask
        promptly. A throttle needs the measured ~5-minute window."""
        _, slept = _run([_Contract("a"), _Transport("b"), "ok"])
        assert slept == [3.0, 300.0]


class TestNoValueRepair:
    def test_nothing_is_carried_across_attempts(self) -> None:
        """A retry re-asks the question; it never merges a failed body into a
        later one. The value returned is the last attempt's, whole."""
        value, _ = _run([_Contract("partial records"), "second-attempt-body"])
        assert value == "second-attempt-body"

    def test_an_isolated_ledger_can_be_measured_independently(self) -> None:
        ledger = ConformanceLedger()
        assert ledger.snapshot() == {}
