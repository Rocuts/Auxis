"""Conformance as a measured rate, not a standing caveat.

The Vercel AI Gateway forwards the adapters' ``output_config`` json_schema
request but does not *enforce* it for a non-Anthropic model (dev-log,
2026-08-26): the contract is honoured by the model's instruction-following,
not by the transport. The adapters already fail closed on a non-conformant
body — a ``MapperError`` rather than a persisted guess — so the exposure was
never silent corruption. What was missing is a number: on this run, how often
did the model actually break the contract?

Three quantities, deliberately kept apart because they mean different things:

``schema_failures``
    The whole call broke the contract — a body that is not JSON, a body
    missing the required envelope, a truncated or refused generation. The
    document loses its mapping, so this is the failure that costs records.

``malformed_items``
    The envelope was right and an item inside it was not: a proposed record
    that fails canonical validation, a verdict naming a record outside the
    batch, a record the verifier skipped entirely. These are precisely the
    review-queue items anti-goal #8 exists to produce — counted now, not
    merely produced. A model-authored issue about a cell it could not read is
    NOT counted here: raising that issue is the contract working, not
    breaking.

``retries``
    Retryable HTTP responses (408/409/429/5xx) the SDK absorbed. A
    *throughput* property, not a conformance one — a 429 retried through says
    nothing about whether the model can emit the schema. It is reported
    beside the rates so the two are never conflated, and it is a lower bound:
    a retry provoked by a connection error never reaches an HTTP response.

``residue``
    Responses whose JSON was complete and correct but arrived inside markdown
    code-fence framing, which ``adapters.envelope`` strips under a rule narrow
    enough to reject anything else. Counted by position because that is the
    difference between a model that opens a fence and one that closes a reply.
    A *presentation* property: the accommodation exists so this shows up as a
    published rate instead of a silent repair, and ADR 014 deliberately keeps
    it out of the escalation trigger, which fires on hard contract failures.

The ledger is process-wide and additive by design: the adapters record into it
from wherever they run, and a reporter renders it at the end. It is only
rendered when real HTTP attempts were observed, so a suite driven by injected
fake clients prints nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

MAPPER = "mapper"
VERIFIER = "verifier"
ADJUDICATOR = "adjudicator"

#: Role order for the report; a role with no calls is omitted.
ROLES = (MAPPER, VERIFIER, ADJUDICATOR)

#: Where fence framing sat on a response the transport accommodated. Recorded
#: per occurrence so the report can say which end the model framed, not merely
#: that it framed something.
RESIDUE_LEADING = "leading"
RESIDUE_TRAILING = "trailing"

#: Statuses the Anthropic SDK treats as retryable. A response carrying one of
#: these was, by construction, either retried or the final attempt of an
#: exhausted retry budget.
_RETRYABLE_STATUSES = frozenset({408, 409, 429})

#: How many distinct failure reasons to keep per role. The point is to name
#: what broke, not to transcribe every occurrence.
_MAX_REASONS = 12


@dataclass(frozen=True)
class RoleCounters:
    calls: int = 0
    items: int = 0
    schema_failures: int = 0
    malformed_items: int = 0
    http_attempts: int = 0
    retries: int = 0
    residue_responses: int = 0
    residue_leading: int = 0
    residue_trailing: int = 0

    @property
    def residue_rate(self) -> float | None:
        """Share of calls whose body needed fence framing removed."""
        if self.calls == 0:
            return None
        return self.residue_responses / self.calls

    @property
    def call_conformance(self) -> float | None:
        """Share of calls that returned a parseable, contract-shaped body."""
        if self.calls == 0:
            return None
        return 1.0 - self.schema_failures / self.calls

    @property
    def item_conformance(self) -> float | None:
        """Share of proposed items that were themselves well formed."""
        if self.items == 0:
            return None
        return 1.0 - self.malformed_items / self.items


@dataclass
class ConformanceLedger:
    """Thread-safe counters. Both live targets run the pipeline concurrently
    (a worker pool locally, overlapping queue subscribers on Vercel), so the
    increments are guarded rather than assumed serial."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counters: dict[str, RoleCounters] = field(default_factory=dict)
    _reasons: dict[str, list[str]] = field(default_factory=dict)

    def _bump(self, role: str, **deltas: int) -> None:
        with self._lock:
            current = self._counters.get(role, RoleCounters())
            self._counters[role] = replace(
                current, **{name: getattr(current, name) + by for name, by in deltas.items()}
            )

    def _note(self, role: str, reason: str) -> None:
        with self._lock:
            reasons = self._reasons.setdefault(role, [])
            if reason not in reasons and len(reasons) < _MAX_REASONS:
                reasons.append(reason)

    # -- recording ---------------------------------------------------------

    def record_call(self, role: str) -> None:
        """One logical model call, about to be attempted."""
        self._bump(role, calls=1)

    def record_items(self, role: str, count: int) -> None:
        """How many items this call's response was expected to carry — the
        denominator of the item-level rate."""
        if count:
            self._bump(role, items=count)

    def record_schema_failure(self, role: str, reason: str) -> None:
        self._bump(role, schema_failures=1)
        self._note(role, reason)

    def record_malformed_item(self, role: str, reason: str) -> None:
        self._bump(role, malformed_items=1)
        self._note(role, reason)

    def record_envelope_residue(self, role: str, positions: Iterable[str]) -> None:
        """One response accommodated, plus one count per end that was framed."""
        seen = tuple(positions)
        self._bump(
            role,
            residue_responses=1,
            residue_leading=sum(1 for p in seen if p == RESIDUE_LEADING),
            residue_trailing=sum(1 for p in seen if p == RESIDUE_TRAILING),
        )
        self._note(role, f"fence framing stripped ({'+'.join(seen)})")

    def record_http_status(self, role: str, status: int) -> None:
        retryable = status in _RETRYABLE_STATUSES or status >= 500
        self._bump(role, http_attempts=1, retries=1 if retryable else 0)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._reasons.clear()

    # -- reading -----------------------------------------------------------

    def snapshot(self) -> dict[str, RoleCounters]:
        with self._lock:
            return dict(self._counters)

    def reasons(self, role: str) -> list[str]:
        with self._lock:
            return list(self._reasons.get(role, ()))

    def observed_http(self) -> bool:
        """True once a real endpoint answered. The report is gated on this so
        a suite of injected fake clients renders nothing."""
        return any(counters.http_attempts for counters in self.snapshot().values())


#: The process-wide ledger the adapters record into.
LEDGER = ConformanceLedger()


def response_hook(role: str, ledger: ConformanceLedger = LEDGER) -> Callable[[Any], None]:
    """An httpx response event hook bound to one role.

    Reads the status line only — never the body, which on a streaming call has
    not arrived yet and is the model's answer, not ours to consume.
    """

    def hook(response: Any) -> None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            ledger.record_http_status(role, status)

    return hook


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_HEADER = (
    "role",
    "calls",
    "items",
    "schema_fail",
    "malformed",
    "residue",
    "http_att",
    "retryable",
    "call_ok",
    "item_ok",
    "residue%",
)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def format_conformance_report(ledger: ConformanceLedger = LEDGER) -> str:
    """The table that ships beside the accuracy table.

    Empty when nothing was recorded: a report with no measurement behind it
    would read as "zero failures" when it means "no run".
    """
    snapshot = ledger.snapshot()
    present = [role for role in ROLES if role in snapshot]
    present += sorted(role for role in snapshot if role not in ROLES)
    if not present:
        return ""

    rows: list[tuple[str, ...]] = [_HEADER]
    for role in present:
        counters = snapshot[role]
        rows.append(
            (
                role,
                str(counters.calls),
                str(counters.items),
                str(counters.schema_failures),
                str(counters.malformed_items),
                str(counters.residue_responses),
                str(counters.http_attempts),
                str(counters.retries),
                _pct(counters.call_conformance),
                _pct(counters.item_conformance),
                _pct(counters.residue_rate),
            )
        )

    widths = [max(len(row[index]) for row in rows) for index in range(len(_HEADER))]
    lines = ["semantic-layer conformance (measured on this run)"]
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))

    lines.append(
        "  call_ok = calls returning a contract-shaped body; "
        "item_ok = proposed items that were well formed."
    )
    lines.append(
        "  retryable counts 408/409/429/5xx responses the SDK absorbed - throughput, "
        "not conformance."
    )
    lines.append(
        "  residue = complete JSON that arrived inside markdown fence framing, stripped "
        "at the transport"
    )
    lines.append(
        "  boundary (ADR 014): reported, never an escalation trigger - the contract "
        "was met, the presentation was not."
    )
    for role in present:
        counters = snapshot[role]
        if counters.residue_responses:
            lines.append(
                f"  {role}: fence framing by position - "
                f"leading {counters.residue_leading}, trailing {counters.residue_trailing}"
            )
    for role in present:
        reasons: Iterable[str] = ledger.reasons(role)
        for reason in reasons:
            lines.append(f"  {role}: {reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Transport instrumentation
# ---------------------------------------------------------------------------

#: One httpx client per (role, timeout). The adapters are constructed per
#: invocation on request-scoped compute, so building a fresh transport each
#: time would leak sockets inside a reused function instance; httpx clients
#: are thread-safe, so one per role is both correct and better pooled.
_HTTP_CLIENTS: dict[tuple[str, float], Any] = {}
_HTTP_CLIENTS_LOCK = threading.Lock()


def _sdk_httpx() -> Any:
    """The httpx module the installed Anthropic SDK type-checks against.

    ``anthropic`` 1.x validates ``http_client`` with an ``isinstance`` check
    and ships ``httpx2``, so a transport built from the ``httpx`` on the path
    is rejected at client construction. Resolving the module from the SDK
    keeps this correct across that split instead of pinning a guess — and
    means this package declares no httpx dependency of its own.
    """
    from anthropic import _base_client

    module = getattr(_base_client, "httpx2", None) or getattr(_base_client, "httpx", None)
    if module is None:  # pragma: no cover - no SDK shape known to reach here
        import httpx

        return httpx
    return module


def instrumented_http_client(role: str, *, timeout: float) -> Any:
    """The transport this role's SDK client should use, counting attempts.

    Retries happen below the SDK's public surface, so the only honest place to
    see them is the HTTP layer: every attempt is one request through this
    client, and the SDK's documented ``http_client`` parameter is the
    supported way in.
    """
    httpx = _sdk_httpx()

    key = (role, timeout)
    with _HTTP_CLIENTS_LOCK:
        client = _HTTP_CLIENTS.get(key)
        if client is None:
            client = httpx.Client(
                timeout=httpx.Timeout(timeout),
                event_hooks={"response": [response_hook(role)]},
            )
            _HTTP_CLIENTS[key] = client
        return client
