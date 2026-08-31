"""LLM factory — thin wrapper around langchain-openai ChatOpenAI for Z.AI.

Phase 2 (Per-Phase Execution Contract): replaces the blind ``max_retries=5`` SDK
retry — which amplified one hung z.ai request to ~30-60 min — with classified,
bounded, jittered per-call retry (``ClassifiedRetryChatOpenAI``). The retry lives
in the LLM layer (subclass of ``_generate``/``_agenerate``) so it composes with
``bind_tools`` + the react loop: a transient blip on the 20th tool call no longer
discards the prior 19. Caller-bug errors (auth/bad-request) fail fast; transient
classes (timeout/connection/5xx) retry a bounded number of times with
exponential-jitter backoff; 429 honors Retry-After.

Also consults the per-model circuit breaker (``llm_breaker``): a tripped model's
traffic routes to its provider-local fallback (ZAI_FALLBACK_MODEL for Z.AI
models; LITELLM_FALLBACK_MODEL — normally empty = no swap — for ``litellm/``
models). Breaker state is recorded in ``ClassifiedRetryChatOpenAI`` (the retry
layer intercepts every call and knows its model name); the old
``CircuitBreakerCallback`` was dead code on langchain-core 1.5.1+ (callbacks
never receive the ``serialized`` payload, so it could never extract a model).

Provider routing: a model name prefixed ``litellm/`` (configurable via
``LITELLM_MODEL_PREFIXES``) is sent to the LiteLLM proxy (``LITELLM_BASE_URL``)
with the prefix stripped client-side — see ``_provider_for``. The prefix IS the
kill switch: unsetting it routes the model back to Z.AI with no code change.

Kill-switch ``LLM_CLASSIFIED_RETRY`` (settings, default on): off → revert to the
pre-Phase-2 plain ChatOpenAI with the old ``max_retries``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import openai
from django.conf import settings
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from .llm_breaker import effective_model, record_failure, record_success

logger = logging.getLogger(__name__)

# Exception classification (openai v2 hierarchy).
_CALLER_BUG_ERRORS = (
    openai.BadRequestError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    openai.UnprocessableEntityError,
)
_TRANSIENT_ERRORS = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)

# [job-84 chemistwarehouse] Mid-stream transport deaths arrive as BARE httpx
# exceptions: openai's SSE reader has no `except httpx.*` clauses, so the
# proxy tearing down a chunked response ("peer closed connection without
# sending complete message body (incomplete chunked read)") escapes the
# openai hierarchy entirely and fell through every retry arm on attempt 1.
# TransportError is the common ancestor of RemoteProtocolError/ReadError/
# WriteError/ConnectError — exactly the transient class.
try:
    import httpx as _httpx

    _HTTPX_TRANSPORT_ERRORS = (_httpx.TransportError,)
except Exception:  # pragma: no cover — httpx ships with langchain-openai
    _HTTPX_TRANSPORT_ERRORS = ()

_TRANSIENT_ERRORS = _TRANSIENT_ERRORS + _HTTPX_TRANSPORT_ERRORS


def _retry_settings() -> dict:
    """Read retry config lazily (Django settings may not be ready at import).

    Two classes, two ladders (job12-fix-plan-FINAL §S1). The **rate-limit**
    class burned its whole budget in ~7.5s on job 12 (4× HTTP 429 in an 8s
    burst; sleeps 1.6/4.2/1.7s) and the exception then took the whole graph
    down — it now gets 6 attempts, its own base (2.0) and a 1.0s floor
    (``uniform(0, x)`` could legally return ~0s → an instant guaranteed-repeat
    429). Worst case 6×30s = 180s, ~17% of the 900s job wall; the job-12 burst
    is absorbed with margin. The **transient** class is unchanged.
    """
    try:
        return {
            # ── transient (timeout/connection/5xx) — unchanged, test-locked ──
            "transient_max": int(getattr(settings, "LLM_RETRY_TRANSIENT_MAX", 2)),
            "backoff_base": float(getattr(settings, "LLM_RETRY_BACKOFF_BASE", 1.5)),
            "backoff_cap": float(getattr(settings, "LLM_RETRY_BACKOFF_CAP", 30.0)),
            # ── rate limit (429) — classed ladder ──
            "ratelimit_max": int(getattr(settings, "LLM_RETRY_RATELIMIT_MAX", 6)),
            "ratelimit_base": float(getattr(settings, "LLM_RETRY_RATELIMIT_BASE", 2.0)),
            "backoff_floor": float(getattr(settings, "LLM_RETRY_BACKOFF_FLOOR", 1.0)),
        }
    except Exception:
        return {
            "transient_max": 2,
            "ratelimit_max": 6,
            "backoff_base": 1.5,
            "ratelimit_base": 2.0,
            "backoff_cap": 30.0,
            "backoff_floor": 1.0,
        }


def _parse_retry_after(exc: BaseException) -> float | None:
    """Pull a Retry-After value (seconds) from a RateLimitError, capped."""
    try:
        resp = getattr(exc, "response", None)
        if resp is not None and hasattr(resp, "headers"):
            ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
            if ra is not None:
                return min(float(ra), 60.0)
    except Exception:
        pass
    return None


# Retry-After jitter half-width: the jittered sleep is uniform in
# [0.8·RA, 1.2·RA], clamped to [floor, cap]. Chosen so N workers that 429'd
# together (all reading the SAME Retry-After) no longer wake in lockstep and
# re-collide, while every wake-up stays inside the window the provider asked
# for. ±20% keeps a 10s guidance within 8-12s — materially de-correlated at
# job-12 burst scale, without inventing a delay the provider didn't ask for.
_RETRY_AFTER_JITTER = 0.20


def _backoff_delay(
    attempt: int,
    cfg: dict,
    kind: str = "transient",
    retry_after: float | None = None,
) -> float:
    """Full-jitter exponential backoff for one retry of ``kind``.

    Transient class keeps its original shape — uniform in
    ``[0, min(cap, backoff_base·2**attempt)]``. The rate-limit class swaps in
    ``ratelimit_base`` and never sleeps less than ``backoff_floor`` (a ~0s
    sleep would re-fire the 429 immediately).

    A provider Retry-After is honored but jittered (see ``_RETRY_AFTER_JITTER``)
    and then clamped into ``[floor, cap]`` — so a huge guidance collapses onto
    the cap instead of eating the job wall clock.
    """
    import random as _r  # local alias to keep module surface clean

    base, floor = (
        (cfg["ratelimit_base"], cfg["backoff_floor"])
        if kind == "rate_limit"
        else (cfg["backoff_base"], 0.0)
    )
    cap = cfg["backoff_cap"]
    if retry_after is not None:
        delay = _r.uniform(
            retry_after * (1.0 - _RETRY_AFTER_JITTER),
            retry_after * (1.0 + _RETRY_AFTER_JITTER),
        )
        return min(max(delay, floor), cap)
    ceiling = max(floor, min(cap, base * (2 ** attempt)))
    return _r.uniform(floor, ceiling) if ceiling > floor else floor


# ── Provider routing (LiteLLM proxy) ─────────────────────────────────────────

def _litellm_prefixes() -> tuple[str, ...]:
    """Configured provider prefixes (``litellm/`` by default). Read lazily —
    Django settings may not be ready at import."""
    try:
        raw = getattr(settings, "LITELLM_MODEL_PREFIXES", "litellm/")
        if isinstance(raw, str):
            raw = raw.split(",")
        return tuple(p for p in (s.strip() for s in raw) if p)
    except Exception:
        return ("litellm/",)


def _is_litellm_model(model: str) -> bool:
    """True if ``model`` should route to the LiteLLM proxy. Requires the
    feature flag AND a key — without a key the proxy would 401 every call."""
    try:
        if not getattr(settings, "LITELLM_ENABLED", True):
            return False
        if not getattr(settings, "LITELLM_API_KEY", ""):
            return False
    except Exception:
        return False
    return model.startswith(_litellm_prefixes())


def _provider_for(model: str) -> tuple[str, str, str]:
    """Resolve (base_url, api_key, model_name_to_send) for a model.

    LiteLLM-prefixed models route to ``LITELLM_BASE_URL`` with the prefix
    stripped client-side (the breaker key stays the FULL configured string —
    see ``ClassifiedRetryChatOpenAI._breaker_key`` — so record and lookup can
    never diverge). Everything else goes to Z.AI unchanged.
    """
    if _is_litellm_model(model):
        for p in _litellm_prefixes():
            if model.startswith(p):
                return (
                    getattr(settings, "LITELLM_BASE_URL", "https://llm.johnjf.xyz/v1"),
                    getattr(settings, "LITELLM_API_KEY", ""),
                    model[len(p):],
                )
    return (
        getattr(settings, "ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4/"),
        getattr(settings, "ZAI_API_KEY", ""),
        model,
    )


def _litellm_fallback(requested: str) -> Optional[str]:
    """Provider-local breaker fallback for ``requested``.

    litellm models → ``LITELLM_FALLBACK_MODEL`` (empty string = no swap; the
    proxy exposes one model, so there is nothing to fall back TO — a
    cross-provider GLM fallback would 404).
    ZAI models → ``None`` = use the configured ``ZAI_FALLBACK_MODEL`` default
    (effective_model treats None as "not specified", NOT as "no fallback" —
    the distinction matters, see llm_breaker.effective_model).
    """
    if _is_litellm_model(requested):
        return getattr(settings, "LITELLM_FALLBACK_MODEL", "")
    return None


def _provider_code(exc: BaseException) -> Optional[str]:
    """Provider error code from an openai exception, if it carries one.

    Z.AI's 429s arrive as ``{"error": {"code": 1302, ...}}`` — the SDK surfaces
    that as ``exc.body``. The bare class name (``RateLimitError``) hides which
    limit was hit, and job 12's 429s were indistinguishable from any other 429.
    """
    for container in (getattr(exc, "body", None), getattr(exc, "error", None)):
        if not isinstance(container, dict):
            continue
        err = container.get("error", container)
        if isinstance(err, dict):
            if err.get("code") is not None:
                return str(err["code"])
        elif err is not None:
            return str(err)
    code = getattr(exc, "code", None)
    return str(code) if code is not None else None


def _error_detail(exc: BaseException) -> str:
    """Stable ``Class/http=NNN/code=N`` string for the exhaustion record."""
    parts = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if status:
        parts.append(f"http={status}")
    code = _provider_code(exc)
    if code is not None:
        parts.append(f"code={code}")
    return "/".join(parts)


def _retry_budget(kind: str, cfg: dict) -> int:
    return cfg["ratelimit_max"] if kind == "rate_limit" else cfg["transient_max"]


def _retry_classified_sync(fn, cfg: dict, *, model: Optional[str] = None, sleep=time.sleep):
    """Run a sync LLM call with classified retry. Raises the last exception after
    the class's retry budget is exhausted."""
    attempt, slept = 0, 0.0
    while True:
        try:
            return fn()
        except openai.RateLimitError as exc:
            attempt, slept = _handle_retry(
                "rate_limit", exc, attempt, slept, cfg,
                retry_after=_parse_retry_after(exc), sleep=sleep, model=model,
            )
        except _TRANSIENT_ERRORS as exc:
            attempt, slept = _handle_retry(
                "transient", exc, attempt, slept, cfg, sleep=sleep, model=model
            )
        except _CALLER_BUG_ERRORS:
            raise  # caller/config bug — never retry
        except openai.APIError as exc:
            # Generic APIError (not a recognized subclass) — conservative retry.
            attempt, slept = _handle_retry(
                "transient", exc, attempt, slept, cfg, sleep=sleep, model=model
            )


async def _retry_classified_async(fn, cfg: dict, *, model: Optional[str] = None, asleep=asyncio.sleep):
    """Async counterpart of _retry_classified_sync.

    The sleep is awaited HERE, not inside a shared helper: handing
    ``asyncio.sleep`` to a sync helper just leaks an un-awaited coroutine, so
    the async path previously had no backoff at all.
    """
    attempt, slept = 0, 0.0
    while True:
        try:
            return await fn()
        except openai.RateLimitError as exc:
            attempt, slept, delay = _next_retry(
                "rate_limit", exc, attempt, slept, cfg,
                retry_after=_parse_retry_after(exc), model=model,
            )
        except _TRANSIENT_ERRORS as exc:
            attempt, slept, delay = _next_retry("transient", exc, attempt, slept, cfg, model=model)
        except _CALLER_BUG_ERRORS:
            raise
        except openai.APIError as exc:
            attempt, slept, delay = _next_retry("transient", exc, attempt, slept, cfg, model=model)
        await asleep(delay)


def _handle_retry(
    kind: str,
    exc,
    attempt: int,
    slept: float,
    cfg: dict,
    *,
    sleep=time.sleep,
    retry_after=None,
    model: Optional[str] = None,
) -> tuple[int, float]:
    """One sync retry step: sleep this failure's backoff and return
    ``(next attempt #, total sleep so far)``; on budget exhaustion emit the
    ``llm-retry-exhausted`` record and re-raise."""
    attempt, slept, delay = _next_retry(kind, exc, attempt, slept, cfg, retry_after, model)
    sleep(delay)
    return attempt, slept


def _next_retry(
    kind: str,
    exc,
    attempt: int,
    slept: float,
    cfg: dict,
    retry_after=None,
    model: Optional[str] = None,
) -> tuple[int, float, float]:
    """Resolve this failure into ``(next attempt #, updated sleep total, delay)``.

    Raises the provider exception once the class's budget is exhausted — after
    emitting the ``llm-retry-exhausted`` record, so a provider outage lands in
    the logs with its class/model/code instead of surfacing only as a job
    ``error_message`` (job 12's 429s never reached the log at all).
    """
    budget = _retry_budget(kind, cfg)
    if attempt >= budget:
        logger.error(
            "llm-retry-exhausted class=%s model=%s error=%s attempts=%d slept=%.1fs budget=%d cap=%.1fs",
            kind, model or "?", _error_detail(exc), attempt, slept, budget, cfg["backoff_cap"],
        )
        raise exc
    delay = _backoff_delay(attempt + 1, cfg, kind, retry_after)
    logger.info(
        "llm classified-retry: %s on attempt %d/%d, sleeping %.1fs: %s",
        kind, attempt + 1, budget, delay, type(exc).__name__,
    )
    return attempt + 1, slept + delay, delay


class ClassifiedRetryChatOpenAI(ChatOpenAI):
    # Breaker key = the model string AS CONFIGURED (``litellm/standardcompute``,
    # prefix intact) — NOT ``model_name``, which is the post-strip string sent
    # to the server. ``effective_model`` looks up the configured string, so
    # recording under anything else makes record/lookup diverge silently (the
    # critique round's key-coherence finding). Set by ``get_llm``.
    _breaker_name: Optional[str] = PrivateAttr(default=None)
    """``ChatOpenAI`` with classified, bounded, jittered per-call retry.

    Overrides of ``_generate``/``_agenerate`` (the methods langchain routes
    ``invoke``/``ainvoke`` to) AND ``_stream``/``_astream`` (the lane
    ``streaming=True`` models are routed through instead — without which the
    retry was dead code for every ``litellm/`` code_writer call, job 84)
    survive ``bind_tools`` (which returns a ``RunnableBinding`` that delegates
    to the underlying model's methods), so the retry applies to every LLM call
    the react loop makes. The base ``max_retries`` MUST be 0 — the SDK's own
    retry is blind + unbounded in effect, which is what this class replaces.

    Also the breaker's observation point: the four overrides intercept every
    outcome and record under ``self.model_name`` (the configured string,
    prefix intact). The old CircuitBreakerCallback never worked on
    langchain-core 1.5.1+ — callbacks don't receive the ``serialized`` payload —
    so recording moved here (record/lookup keys identical by construction).
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[override]
        cfg = _retry_settings()
        # Bind the parent method OUTSIDE the lambda: zero-arg super() inside a
        # lambda doesn't get the __class__ closure cell (RuntimeError: super(): no
        # arguments). Explicit super() + a local binding is robust.
        _super_generate = super(ClassifiedRetryChatOpenAI, self)._generate
        try:
            result = _retry_classified_sync(
                lambda: _super_generate(messages, stop=stop, run_manager=run_manager, **kwargs),
                cfg,
                # The configured model string goes into the exhaustion record so
                # a provider outage is attributable from the log line alone.
                model=self._breaker_key(),
            )
        except Exception as exc:
            self._record_breaker(exc)
            raise
        record_success(self._breaker_key())
        return result

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[override]
        cfg = _retry_settings()
        _super_agenerate = super(ClassifiedRetryChatOpenAI, self)._agenerate

        async def _call():
            return await _super_agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

        try:
            result = await _retry_classified_async(_call, cfg, model=self._breaker_key())
        except Exception as exc:
            self._record_breaker(exc)
            raise
        record_success(self._breaker_key())
        return result

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        # [job-84 chemistwarehouse] With ``streaming=True`` (every litellm
        # model — get_llm sets it to dodge the proxy's ~60s non-stream 504)
        # langchain routes invoke() through ``_stream``, NOT ``_generate``:
        # the retry override above was dead code for code_writer, and the
        # first mid-stream transport death failed the whole phase. Retry wraps
        # the FULL consumption — a stream that dies mid-flight raises from
        # iteration, not from the call — then replays the buffered chunks.
        cfg = _retry_settings()
        _super_stream = super(ClassifiedRetryChatOpenAI, self)._stream
        try:
            chunks = _retry_classified_sync(
                lambda: list(
                    _super_stream(messages, stop=stop, run_manager=run_manager, **kwargs)
                ),
                cfg,
                model=self._breaker_key(),
            )
        except Exception as exc:
            self._record_breaker(exc)
            raise
        record_success(self._breaker_key())
        yield from chunks

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        # Async counterpart of ``_stream`` (LLM_ASYNC_EXECUTION path).
        cfg = _retry_settings()
        _super_astream = super(ClassifiedRetryChatOpenAI, self)._astream

        async def _consume():
            return [
                chunk
                async for chunk in _super_astream(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            ]

        try:
            chunks = await _retry_classified_async(_consume, cfg, model=self._breaker_key())
        except Exception as exc:
            self._record_breaker(exc)
            raise
        record_success(self._breaker_key())
        for chunk in chunks:
            yield chunk

    def _breaker_key(self) -> str:
        """Configured model string (prefix intact); falls back to model_name
        when get_llm didn't set the private attr (direct construction)."""
        return self._breaker_name or self.model_name

    def _record_breaker(self, exc: BaseException) -> None:
        """record_failure for health-class errors only (timeout/connection/5xx).
        Caller bugs (auth/bad-request) are config problems, not model health."""
        if isinstance(exc, _CALLER_BUG_ERRORS):
            return
        try:
            record_failure(self._breaker_key())
        except Exception:
            pass  # breaker bookkeeping must never mask the real exception


def _classified_retry_enabled() -> bool:
    try:
        return bool(getattr(settings, "LLM_CLASSIFIED_RETRY", True))
    except Exception:
        return True


def get_llm(model: Optional[str] = None, temperature: float = 0.3, timeout: Optional[int] = None) -> ChatOpenAI:
    """Create a ChatOpenAI instance configured for the Z.AI API (or the LiteLLM
    proxy for ``litellm/``-prefixed models — see ``_provider_for``).

    The per-model circuit breaker (llm_breaker) is consulted here: if the
    requested model is tripped (N consecutive failures), the fallback model is
    used instead. Phase 2 adds classified per-call retry (max_retries=0 on the
    SDK; ``ClassifiedRetryChatOpenAI`` adds the classified layer) unless the
    ``LLM_CLASSIFIED_RETRY`` kill-switch is off. Pass ``timeout`` (seconds) to
    override the default ``LLM_REQUEST_TIMEOUT`` for short-lived calls (e.g.
    field discovery).

    LiteLLM-prefixed models get ``streaming=True``: the proxy's gateway 504s a
    non-streaming request whose generation exceeds ~60s (measured), and the
    reasoning model routinely exceeds that on codegen. Streaming keeps bytes
    flowing — measured: non-stream 504 at 62s vs stream first-chunk 5.5s /
    full 11K chars over 119s. langchain's react loop consumes streamed chunks
    and assembles the final AIMessage identically, so behavior is unchanged.
    """
    requested = model or getattr(settings, "ZAI_MAIN_MODEL", "glm-5-turbo")
    # Config-error guard: a litellm/-prefixed model with routing unavailable
    # (disabled flag or missing key) must NOT silently fall through to Z.AI with
    # the UNSTRIPPED name — Z.AI 400s it ("modelCode: does not exist", caller-bug
    # class, no retry) and every LLM call dies instantly with zero diagnostics
    # (Railway job 6: 3 code_writer cycles, no tool calls, no draft). The prefix
    # is an explicit routing instruction; if it can't be honored, say so loudly.
    if requested.startswith(_litellm_prefixes()) and not _is_litellm_model(requested):
        raise RuntimeError(
            f"model '{requested}' requests the LiteLLM proxy but routing is "
            "unavailable (LITELLM_ENABLED=false or LITELLM_API_KEY missing/empty). "
            "Set LITELLM_API_KEY on the worker, or unset the prefix in "
            "CODE_WRITER_MODEL to return to Z.AI."
        )
    # ORDER IS LOAD-BEARING: breaker swap first (provider-local fallback — a
    # litellm model falls back only within litellm, or not at all), THEN resolve
    # the provider from the swapped name, so base_url follows the actual model.
    effective = effective_model(requested, fallback=_litellm_fallback(requested))
    base_url, api_key, model_name = _provider_for(effective)
    base_kwargs = dict(
        openai_api_base=base_url,
        openai_api_key=api_key,
        model=model_name,
        temperature=temperature,
        # Per-call HTTP timeout — the primary hang guard (a stuck request dies
        # here rather than hanging indefinitely).
        timeout=timeout if timeout is not None else getattr(settings, "LLM_REQUEST_TIMEOUT", 600),
        # LiteLLM proxy: gateway 504s non-streaming gens > ~60s (measured).
        streaming=_is_litellm_model(effective),
    )
    if _classified_retry_enabled():
        # Phase 2: max_retries=0 (no blind SDK retry) + classified retry layer.
        llm = ClassifiedRetryChatOpenAI(max_retries=0, **base_kwargs)
        # Breaker key = configured string (prefix intact), not the stripped name
        # sent to the server — keeps record/lookup coherent across the swap.
        llm._breaker_name = requested
        return llm
    # Kill-switch off → pre-Phase-2 behavior (blind SDK retry). Breaker not
    # recorded on this path (no retry-layer interception).
    return ChatOpenAI(max_retries=getattr(settings, "LLM_MAX_RETRIES", 5), **base_kwargs)


def get_main_llm(temperature: float = 0.3) -> ChatOpenAI:
    """Return the main model (glm-5-turbo) for subagent reasoning."""
    return get_llm(
        model=getattr(settings, "ZAI_MAIN_MODEL", "glm-5-turbo"),
        temperature=temperature,
    )


def get_small_llm(temperature: float = 0.3, timeout: Optional[int] = None) -> ChatOpenAI:
    """Return the small / fast model (glm-5-turbo) for quick decisions."""
    return get_llm(
        model=getattr(settings, "ZAI_SMALL_MODEL", "glm-5-turbo"),
        temperature=temperature,
        timeout=timeout,
    )
