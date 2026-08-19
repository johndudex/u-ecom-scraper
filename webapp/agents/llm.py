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


def _retry_settings() -> dict:
    """Read retry config lazily (Django settings may not be ready at import)."""
    try:
        return {
            "transient_max": int(getattr(settings, "LLM_RETRY_TRANSIENT_MAX", 2)),
            "ratelimit_max": int(getattr(settings, "LLM_RETRY_RATELIMIT_MAX", 3)),
            "backoff_base": float(getattr(settings, "LLM_RETRY_BACKOFF_BASE", 1.5)),
            "backoff_cap": float(getattr(settings, "LLM_RETRY_BACKOFF_CAP", 30.0)),
        }
    except Exception:
        return {"transient_max": 2, "ratelimit_max": 3, "backoff_base": 1.5, "backoff_cap": 30.0}


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


def _backoff_delay(attempt: int, cfg: dict) -> float:
    """Full-jitter exponential backoff: uniform in [0, min(cap, base * 2**attempt)]."""
    import random as _r  # local alias to keep module surface clean
    return _r.uniform(0.0, min(cfg["backoff_cap"], cfg["backoff_base"] * (2 ** attempt)))


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


def _retry_classified_sync(fn, cfg: dict):
    """Run a sync LLM call with classified retry. Raises the last exception after
    the class's retry budget is exhausted."""
    attempt = 0
    while True:
        try:
            return fn()
        except openai.RateLimitError as exc:
            attempt = _handle_retry(
                "rate_limit", exc, attempt, cfg,
                retry_after=_parse_retry_after(exc), sleep=time.sleep,
            )
        except _TRANSIENT_ERRORS as exc:
            attempt = _handle_retry(
                "transient", exc, attempt, cfg, sleep=time.sleep
            )
        except _CALLER_BUG_ERRORS:
            raise  # caller/config bug — never retry
        except openai.APIError as exc:
            # Generic APIError (not a recognized subclass) — conservative retry.
            attempt = _handle_retry("transient", exc, attempt, cfg, sleep=time.sleep)


async def _retry_classified_async(fn, cfg: dict):
    """Async counterpart of _retry_classified_sync (uses asyncio.sleep)."""
    attempt = 0
    while True:
        try:
            return await fn()
        except openai.RateLimitError as exc:
            attempt = _handle_retry(
                "rate_limit", exc, attempt, cfg,
                retry_after=_parse_retry_after(exc), sleep=asyncio.sleep,
            )
        except _TRANSIENT_ERRORS as exc:
            attempt = _handle_retry("transient", exc, attempt, cfg, sleep=asyncio.sleep)
        except _CALLER_BUG_ERRORS:
            raise
        except openai.APIError as exc:
            attempt = _handle_retry("transient", exc, attempt, cfg, sleep=asyncio.sleep)


def _handle_retry(kind: str, exc, attempt: int, cfg: dict, *, sleep, retry_after=None) -> int:
    """Decide whether to retry (returning the next attempt #) or re-raise."""
    budget = cfg["ratelimit_max"] if kind == "rate_limit" else cfg["transient_max"]
    if attempt >= budget:
        logger.warning(
            "llm classified-retry: %s exhausted after %d attempts: %s",
            kind, attempt, type(exc).__name__,
        )
        raise exc
    attempt += 1
    delay = retry_after if retry_after is not None else _backoff_delay(attempt, cfg)
    logger.info(
        "llm classified-retry: %s on attempt %d/%d, sleeping %.1fs: %s",
        kind, attempt, budget, delay, type(exc).__name__,
    )
    sleep(delay)
    return attempt


class ClassifiedRetryChatOpenAI(ChatOpenAI):
    # Breaker key = the model string AS CONFIGURED (``litellm/standardcompute``,
    # prefix intact) — NOT ``model_name``, which is the post-strip string sent
    # to the server. ``effective_model`` looks up the configured string, so
    # recording under anything else makes record/lookup diverge silently (the
    # critique round's key-coherence finding). Set by ``get_llm``.
    _breaker_name: Optional[str] = PrivateAttr(default=None)
    """``ChatOpenAI`` with classified, bounded, jittered per-call retry.

    Override of ``_generate``/``_agenerate`` (the methods langchain routes
    ``invoke``/``ainvoke`` to) survives ``bind_tools`` (which returns a
    ``RunnableBinding`` that delegates to the underlying model's ``_generate``),
    so the retry applies to every LLM call the react loop makes. The base
    ``max_retries`` MUST be 0 — the SDK's own retry is blind + unbounded in
    effect, which is what this class replaces.

    Also the breaker's observation point: ``_generate``/``_agenerate`` intercept
    every outcome and record under ``self.model_name`` (the configured string,
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
            result = await _retry_classified_async(_call, cfg)
        except Exception as exc:
            self._record_breaker(exc)
            raise
        record_success(self._breaker_key())
        return result

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
