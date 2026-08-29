"""Shared HTTP client for browser_service /scrape calls (W8).

Every webapp-side /scrape caller used to do a bare ``httpx.post`` +
``raise_for_status()`` (or no status check at all) — a browser-service 429
backpressure response or a 502 under memory pressure then surfaced as an
empty ``output_content``, a "successful" re-run with zero products, or an
opaque ``HTTPStatusError``. This helper gives all five call sites one
bounded-retry policy:

- **Transient (retry):** 429 / 502 / 503 / 504 and transport errors
  (timeout / connect / remote-protocol) — exactly what browser_service emits
  while saturated or restarting Chrome. Retry-After is honored from the
  response header first, then the JSON body.
- **Fatal (never retry):** 404 ("source invalid" — a distinct signal both
  existing consumers already special-case) and every other status (a 400 is
  a bad payload; retrying manufactures load and lies about the cause).
- **Budget:** ``total_budget_s`` (default ``timeout + 240``) short-circuits
  the remaining attempts. Attempt counts alone allow ~33 minutes inside one
  tool call (3 × 660s + backoff) — long enough to trip the celery/LLM
  timeout layers this helper is meant to protect.

Never raises: failures come back as ``ScrapeResult(ok=False)`` with a
``transient``/``throttled`` classification the caller can surface honestly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# Retry only what backpressure/instability looks like — NOT a scrape bug.
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
HTTP_NOT_FOUND = 404
HTTP_THROTTLED = 429

# Transport failures worth another attempt (connection refused mid-restart,
# read timeouts on a wedged executor). Anything else from httpx is fatal.
TRANSIENT_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)

# Headroom beyond the scrape's own server-side timeout before the retry
# budget closes (default for total_budget_s).
DEFAULT_BUDGET_SLACK_S = 240.0
# Don't start an attempt we can't hope to finish.
MIN_ATTEMPT_BUDGET_S = 5.0
DEFAULT_RETRY_AFTER_S = 5.0
MAX_RETRY_AFTER_S = 60.0


@dataclass
class ScrapeResult:
    """Outcome of one ``post_scrape_with_retry`` call."""

    ok: bool = False            # HTTP 200 + parseable JSON body
    status: int | None = None   # final HTTP status (None = no response at all)
    data: dict = field(default_factory=dict)
    error: str | None = None    # human-readable reason when not ok
    transient: bool = False     # last failure was the retryable class
    throttled: bool = False     # a 429 was observed (backpressure signal)
    attempts: int = 0

    @property
    def error_class(self) -> str:
        """job-311 F-C spirit: refuse != breakage. Deterministic buckets."""
        if self.ok:
            return "ok"
        if self.status == HTTP_NOT_FOUND:
            return "not_found"
        if self.throttled:
            return "throttled"
        return "transient" if self.transient else "fatal"


def _retry_after_seconds(resp: httpx.Response | None) -> float:
    """Retry-After: response header first, then the JSON body, else default."""
    if resp is None:
        return DEFAULT_RETRY_AFTER_S
    raw = resp.headers.get("Retry-After")
    if not raw:
        try:
            raw = (resp.json() or {}).get("retry_after")
        except ValueError:
            raw = None
    try:
        return min(max(float(raw), 1.0), MAX_RETRY_AFTER_S)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S


def _sleep_for_retry(resp: httpx.Response | None, deadline: float) -> bool:
    """Sleep toward the next attempt, capped by the budget. False = out of budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(_retry_after_seconds(resp), remaining))
    return True


def _summarize_failure(result: ScrapeResult) -> str:
    if result.throttled:
        return (
            f"browser_service throttled the scrape (HTTP 429) after "
            f"{result.attempts} attempt(s) — backpressure, re-run may succeed"
        )
    if result.status is None:
        return f"browser_service unreachable after {result.attempts} attempt(s)"
    return (
        f"browser_service returned HTTP {result.status} after "
        f"{result.attempts} attempt(s)"
    )


def post_scrape_with_retry(
    url: str,
    payload: dict,
    *,
    timeout: float,
    total_budget_s: float | None = None,
    max_attempts: int = 3,
) -> ScrapeResult:
    """POST ``payload`` to a browser_service /scrape endpoint with bounded retries.

    ``timeout`` is the per-attempt httpx timeout (callers keep their existing
    "scrape timeout + transport cushion" value); ``total_budget_s`` (default
    ``timeout + DEFAULT_BUDGET_SLACK_S``) bounds the whole call.
    """
    budget = (
        total_budget_s
        if total_budget_s is not None
        else timeout + DEFAULT_BUDGET_SLACK_S
    )
    deadline = time.monotonic() + budget
    result = ScrapeResult()

    for attempt in range(1, max_attempts + 1):
        # Short-circuit: attempt-counts alone can hold a worker for ~33 min.
        if attempt > 1 and deadline - time.monotonic() < MIN_ATTEMPT_BUDGET_S:
            result.attempts = attempt - 1
            break
        result.attempts = attempt
        try:
            resp = httpx.post(url, json=payload, timeout=timeout)
        except TRANSIENT_EXCEPTIONS as exc:
            result.status = None
            result.transient = True
            logger.warning(
                "browser_http: %s transient (%s), attempt %d/%d",
                url, exc, attempt, max_attempts,
            )
            if attempt < max_attempts and _sleep_for_retry(None, deadline):
                continue
            break
        except httpx.HTTPError as exc:
            # Non-transient transport problem (bad URL, invalid request).
            result.error = f"browser_service unreachable: {exc}"
            return result

        result.status = resp.status_code
        if resp.status_code == httpx.codes.OK:
            try:
                result.data = resp.json()
            except ValueError:
                result.error = "browser_service returned a non-JSON 200 body"
                return result
            result.ok = True
            result.transient = False
            result.error = None
            return result
        if resp.status_code == HTTP_NOT_FOUND:
            # "Source invalid" — a distinct signal callers special-case.
            result.error = "Scraper rejected by browser_service (source invalid)"
            return result
        if resp.status_code in RETRYABLE_STATUS_CODES:
            result.transient = True
            if resp.status_code == HTTP_THROTTLED:
                result.throttled = True
            if attempt < max_attempts and _sleep_for_retry(resp, deadline):
                continue
            break
        # Any other status is fatal — don't manufacture more load.
        result.error = f"browser_service returned HTTP {resp.status_code}"
        return result

    if not result.ok and not result.error:
        result.error = _summarize_failure(result)
    return result
