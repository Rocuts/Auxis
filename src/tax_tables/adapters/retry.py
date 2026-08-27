"""Bounded retries for a model call, and the rule for what may be retried.

The baseline run measured a *reproducible* failure that a single attempt
cannot survive: `zai/glm-5.3-flash` appended prose after a complete JSON value
on 3 of 4 bodies on document 01, and on 2 of 5 gate calls overall. That is not
a systematic inability — the other bodies were clean — it is a per-call coin
flip, and the correct response to a coin flip is a bounded number of further
flips.

Two properties keep this honest:

- **It is a transport retry, never a value repair.** A retry re-asks the same
  question and takes whatever comes back, judged by the same unchanged
  contract. Nothing is merged across attempts, nothing is salvaged from a
  failed one, and a run that needs three attempts is not reported as though it
  needed one.
- **Every attempt is counted.** The conformance ledger records a call, and a
  failure, per attempt. So retries *depress* the measured conformance rate
  rather than hiding behind it: a model that needs two tries to emit its
  schema reads as 50%, which is the truth about the model.

Backoffs are separate because the two failures have different physics. A
contract failure is settled the instant the body arrives — re-ask promptly. A
transport failure is a rate limit, and the measured free-tier window did not
clear in 75 s but did in about five minutes, so the default spacing is sized
to that observation rather than to a guess.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from tax_tables.observability import conformance

#: Attempts AFTER the first. Two, because three total attempts drive the
#: measured ~25% per-call envelope failure rate to under 2% if the failures
#: are independent — and the baseline data is consistent with independence
#: (clean and failing bodies interleaved on the same document).
DEFAULT_CONTRACT_RETRIES = 2
DEFAULT_CONTRACT_RETRY_SECONDS = 3.0
#: Sized to the measured free-tier 429 window (~5 minutes; 75 s was not
#: enough). Tunable because a run against an unthrottled endpoint should not
#: pay for a limit it does not have.
DEFAULT_TRANSPORT_RETRY_SECONDS = 300.0


def clip_reason(exc: Exception, limit: int = 160) -> str:
    """A failure reason short enough for a report line. The full message still
    rides the exception the caller sees."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def with_bounded_retries[T](
    operation: Callable[[], T],
    *,
    role: str,
    contract_error: type[Exception],
    retries: int = DEFAULT_CONTRACT_RETRIES,
    contract_backoff: float = DEFAULT_CONTRACT_RETRY_SECONDS,
    transport_backoff: float = DEFAULT_TRANSPORT_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``operation``, retrying a bounded number of times.

    ``contract_error`` is the caller's own "the model broke the contract"
    exception; anything else is treated as a transport failure. The final
    attempt's exception propagates unchanged, so callers upstream see exactly
    what they would have seen without retries.
    """
    for attempt in range(retries + 1):
        conformance.LEDGER.record_call(role)
        try:
            return operation()
        except contract_error as exc:
            conformance.LEDGER.record_schema_failure(role, clip_reason(exc))
            if attempt == retries:
                raise
            backoff = contract_backoff
        except Exception as exc:
            # No body arrived: throttle, timeout, dropped connection. Counted
            # apart so it can never flatter the conformance rate.
            conformance.LEDGER.record_transport_failure(role, type(exc).__name__)
            if attempt == retries:
                raise
            backoff = transport_backoff
        sleep(backoff)
    raise AssertionError("unreachable")  # pragma: no cover
