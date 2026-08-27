"""Print the conformance table beside the accuracy table.

Loaded explicitly by ``make accuracy`` (``-p
tax_tables.observability.pytest_plugin``) rather than auto-discovered, for two
reasons: the accuracy harness under ``tests/accuracy/`` is the exercise's
oracle and is never edited to add reporting, and ``make check`` must stay
byte-for-byte the run it was before.

Printing only — it registers no fixtures, asserts nothing, and cannot change a
gate's verdict. The table is suppressed unless a real endpoint answered during
the session, so a suite driven by injected fake clients stays silent.
"""

from __future__ import annotations

from typing import Any

from tax_tables.observability.conformance import LEDGER, format_conformance_report


def pytest_terminal_summary(terminalreporter: Any, *args: Any, **kwargs: Any) -> None:
    if not LEDGER.observed_http():
        return
    report = format_conformance_report()
    if report:
        terminalreporter.write_line("")
        terminalreporter.write_line(report)
