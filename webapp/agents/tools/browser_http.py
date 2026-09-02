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
  timeout layers this helper is meant to protect. QW-5 (job-312): the final
  attempt is exempt from the short-circuit and gets at least
  ``FINAL_ATTEMPT_MIN_S`` even past the nominal budget, so worst case is
  budget + one bounded attempt — a retry with a real window instead of a
  guaranteed-loss sliver that reads as a scrape defect.

Never raises: failures come back as ``ScrapeResult(ok=False)`` with a
``transient``/``throttled`` classification the caller can surface honestly.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# [wave-14] browser_service base URL for the /cancel helper. django settings
# first (the webapp runs inside Django), env fallback for bare-process tests.
try:  # pragma: no cover - trivial config resolution
    from django.conf import settings as _dj_settings

    BROWSER_SERVICE_URL = getattr(
        _dj_settings, "BROWSER_SERVICE_URL", ""
    ) or os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")
except Exception:  # pragma: no cover - django not configured
    BROWSER_SERVICE_URL = os.environ.get(
        "BROWSER_SERVICE_URL", "http://browser_service:8001"
    )

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
# QW-5 (job-312): floor for the final attempt's per-attempt timeout. A retry
# sliced to a sliver of the caller's timeout always fails and misreads as a
# scrape defect (false strategy cascade); the last attempt gets a real window
# even past the nominal budget (bounded overshoot: budget + one attempt).
FINAL_ATTEMPT_MIN_S = 300.0


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
    # [wave-16 B3] The server's OWN diagnosis, surfaced verbatim from its
    # classified 502/503 body (error_class ∈ {chrome_crash, memory_pressure,
    # resource, ...}) so a gateway outage RCA is one log line, not an
    # archaeology dig. Empty when the body carried nothing (or no body).
    server_error_class: str = ""
    body_error: str = ""

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

    @property
    def unavailable(self) -> bool:
        """[wave-16 B3] browser_service itself said it cannot serve.

        True when its classified error body named an infra class, or when it
        was unreachable outright after every attempt (status None). This — not
        a site-side failure — is what should park a job, never strategy-switch
        it: the strategy was never given a fair window.
        """
        if not self.ok and self.status is None:
            return True
        return self.server_error_class in _SERVER_INFRA_CLASSES


# error_class values browser_service stamps on its classified 502/503 bodies —
# all meaning "the gateway/infrastructure failed", never "the site won".
_SERVER_INFRA_CLASSES = frozenset({"chrome_crash", "memory_pressure", "resource"})


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


def _attempt_timeout(
    timeout: float, deadline: float, attempt: int, max_attempts: int
) -> float:
    """Per-attempt httpx timeout for this attempt (QW-5, job-312).

    Attempt 1 keeps the FULL timeout — healthy single-attempt runs pay
    nothing. Middle attempts get what's left of the budget (floored at
    ``MIN_ATTEMPT_BUDGET_S``) so they can't overrun it. The final attempt is
    floored at ``FINAL_ATTEMPT_MIN_S`` (capped by the caller's timeout) even
    past the nominal budget — a real chance instead of a guaranteed loss.
    Never exceeds the caller's ``timeout``.
    """
    if attempt <= 1:
        return timeout
    remaining = deadline - time.monotonic()
    if attempt >= max_attempts:
        return max(min(timeout, remaining), min(FINAL_ATTEMPT_MIN_S, timeout))
    return min(timeout, max(remaining, MIN_ATTEMPT_BUDGET_S))


def _summarize_failure(result: ScrapeResult) -> str:
    if result.throttled:
        return (
            f"browser_service throttled the scrape (HTTP 429) after "
            f"{result.attempts} attempt(s) — backpressure, re-run may succeed"
        )
    if result.status is None:
        return f"browser_service unreachable after {result.attempts} attempt(s)"
    base = (
        f"browser_service returned HTTP {result.status} after "
        f"{result.attempts} attempt(s)"
    )
    # B3: append the server's own classification so the artifact says WHY
    # (chrome_crash / memory_pressure / resource) instead of a bare status.
    if result.server_error_class:
        base += f" error_class={result.server_error_class}"
    if result.body_error:
        base += f": {result.body_error[:160]}"
    return base


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
        is_final = attempt == max_attempts
        # Short-circuit: attempt-counts alone can hold a worker for ~33 min.
        # The final attempt is exempt (QW-5) — it gets a real window bounded
        # by FINAL_ATTEMPT_MIN_S instead of a guaranteed-loss sliver.
        if (
            not is_final
            and attempt > 1
            and deadline - time.monotonic() < MIN_ATTEMPT_BUDGET_S
        ):
            result.attempts = attempt - 1
            break
        result.attempts = attempt
        attempt_timeout = _attempt_timeout(timeout, deadline, attempt, max_attempts)
        if attempt_timeout < timeout:
            logger.info(
                "browser_http: %s attempt %d/%d timeout sliced %.0fs -> %.0fs "
                "(retry budget)",
                url, attempt, max_attempts, timeout, attempt_timeout,
            )
        try:
            resp = httpx.post(url, json=payload, timeout=attempt_timeout)
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
            # B3: capture the server's own diagnosis from its classified body
            # (present on every 502/503 browser_service emits since B2). The
            # LAST one wins — it describes the state that exhausted the ladder.
            try:
                _body = resp.json() or {}
            except ValueError:
                _body = {}
            if _body.get("error_class"):
                result.server_error_class = str(_body["error_class"])[:60]
            if _body.get("error"):
                result.body_error = str(_body["error"])[:300]
            if attempt < max_attempts and _sleep_for_retry(resp, deadline):
                continue
            break
        # Any other status is fatal — don't manufacture more load.
        result.error = f"browser_service returned HTTP {resp.status_code}"
        return result

    if not result.ok and not result.error:
        result.error = _summarize_failure(result)
    return result


def cancel_scrape(job_id: int, rid: str = "") -> dict:
    """[wave-14] Ask browser_service to cancel in-flight /scrape run(s).

    Fire-and-forget: the cancel endpoint is lock-free and answers in
    microseconds, but this client still uses a SHORT timeout and never raises —
    a caller that is cancelling is usually also giving up, and a cancel that
    hangs its caller just relocates the hang. Returns the browser_service
    report (``flagged``/``killed``/``unknown``) or ``{"requested": False,
    "error": ...}`` when unreachable.
    """
    try:
        resp = httpx.post(
            f"{BROWSER_SERVICE_URL}/cancel",
            json={"rid": rid or "", "job_id": int(job_id or 0)},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json() or {}
        return {"requested": False, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        logger.warning("cancel_scrape(job=%s): browser_service unreachable: %s", job_id, exc)
        return {"requested": False, "error": str(exc)[:200]}


# ── [wave-16 B3] dependency health: probe, bounded pre-flight, park ────────
# A browser_service outage used to be indistinguishable from a site failure:
# the cascade strategy-switched (or the executor burned its ladder) against an
# infrastructure that could not serve ANY strategy. Now callers can ask "is
# the gateway healthy", wait briefly for it, and PARK the job when it is not —
# a beat task resumes parked jobs once /health recovers.

HEALTH_TIMEOUT_S = 6.0
PREFLIGHT_MAX_WAIT_S = float(os.environ.get("BROWSER_SERVICE_PREFLIGHT_WAIT_S", "120"))
PREFLIGHT_POLL_S = 10.0
# [wave-16 B3] Beat-resumer batch (oldest parked first) per tick — small by
# design: the scrape worker pool is 2 slots, so a stampede only queues anyway.
BROWSER_RESUME_BATCH = int(os.environ.get("BROWSER_RESUME_BATCH", "3"))


def browser_service_healthy() -> bool:
    """True only when browser_service answers /health with status "ok".

    A 503 (degraded), a transport error, or a non-JSON body are all False.
    Deliberately strict: the callers use this to decide between proceeding
    and parking, and a "maybe" must read as "no" — a job parked a few extra
    minutes costs an idle slot, a job dispatched into a dead gateway burns a
    full run.
    """
    try:
        resp = httpx.get(
            f"{BROWSER_SERVICE_URL}/health", timeout=HEALTH_TIMEOUT_S
        )
        if resp.status_code != 200:
            return False
        return bool((resp.json() or {}).get("status") == "ok")
    except Exception:
        return False


def wait_for_browser_service(
    max_wait_s: float | None = None, poll_s: float = PREFLIGHT_POLL_S
) -> bool:
    """Bounded pre-flight: poll /health until healthy or the window closes.

    Used before the expensive phases (code_tester runs, execution). A short
    transient blip self-heals inside the window and the phase proceeds
    untouched; a real outage returns False so the caller can park the job
    instead of feeding a dead gateway a full run.
    """
    deadline = time.monotonic() + max(0.0, float(
        PREFLIGHT_MAX_WAIT_S if max_wait_s is None else max_wait_s
    ))
    while True:
        if browser_service_healthy():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def park_job_for_browser_service(job_id: int, reason: str) -> bool:
    """Park a job on STATUS_BROWSER_UNAVAILABLE with the outage detail.

    Mirrors the captcha/akamai park (check_accessibility) but NON-terminal:
    completed_at is left unset and the beat resumer re-dispatches the row
    (check_tracker's skip flags make that re-run resume from the right phase)
    once browser_service /health recovers. Never raises — a park that fails
    falls back to the job's normal failure handling.
    """
    try:
        from scraper.models import ScrapeJob

        updated = ScrapeJob.objects.filter(pk=job_id).update(
            status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE,
            error_message=str(reason)[:2000],
        )
        if updated:
            logger.warning(
                "park_job_for_browser_service: job %s parked (browser_service "
                "unavailable): %s", job_id, str(reason)[:200],
            )
        return bool(updated)
    except Exception as exc:
        logger.error(
            "park_job_for_browser_service(job=%s) failed: %s", job_id, exc
        )
        return False
