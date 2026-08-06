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
traffic routes to ``ZAI_FALLBACK_MODEL``.

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

from .llm_breaker import effective_model

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
    """``ChatOpenAI`` with classified, bounded, jittered per-call retry.

    Override of ``_generate``/``_agenerate`` (the methods langchain routes
    ``invoke``/``ainvoke`` to) survives ``bind_tools`` (which returns a
    ``RunnableBinding`` that delegates to the underlying model's ``_generate``),
    so the retry applies to every LLM call the react loop makes. The base
    ``max_retries`` MUST be 0 — the SDK's own retry is blind + unbounded in
    effect, which is what this class replaces.
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[override]
        cfg = _retry_settings()
        # Bind the parent method OUTSIDE the lambda: zero-arg super() inside a
        # lambda doesn't get the __class__ closure cell (RuntimeError: super(): no
        # arguments). Explicit super() + a local binding is robust.
        _super_generate = super(ClassifiedRetryChatOpenAI, self)._generate
        return _retry_classified_sync(
            lambda: _super_generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            cfg,
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[override]
        cfg = _retry_settings()
        _super_agenerate = super(ClassifiedRetryChatOpenAI, self)._agenerate

        async def _call():
            return await _super_agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

        return await _retry_classified_async(_call, cfg)


def _classified_retry_enabled() -> bool:
    try:
        return bool(getattr(settings, "LLM_CLASSIFIED_RETRY", True))
    except Exception:
        return True


def get_llm(model: Optional[str] = None, temperature: float = 0.3, timeout: Optional[int] = None) -> ChatOpenAI:
    """Create a ChatOpenAI instance configured for the Z.AI API.

    The per-model circuit breaker (llm_breaker) is consulted here: if the
    requested model is tripped (N consecutive failures), the fallback model is
    used instead. Phase 2 adds classified per-call retry (max_retries=0 on the
    SDK; ``ClassifiedRetryChatOpenAI`` adds the classified layer) unless the
    ``LLM_CLASSIFIED_RETRY`` kill-switch is off. Pass ``timeout`` (seconds) to
    override the default ``LLM_REQUEST_TIMEOUT`` for short-lived calls (e.g.
    field discovery).
    """
    requested = model or getattr(settings, "ZAI_MAIN_MODEL", "glm-5-turbo")
    base_kwargs = dict(
        openai_api_base=getattr(settings, "ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4/"),
        openai_api_key=settings.ZAI_API_KEY,
        model=effective_model(requested),
        temperature=temperature,
        # Per-call HTTP timeout — the primary hang guard (a stuck request dies
        # here rather than hanging indefinitely).
        timeout=timeout if timeout is not None else getattr(settings, "LLM_REQUEST_TIMEOUT", 600),
    )
    if _classified_retry_enabled():
        # Phase 2: max_retries=0 (no blind SDK retry) + classified retry layer.
        return ClassifiedRetryChatOpenAI(max_retries=0, **base_kwargs)
    # Kill-switch off → pre-Phase-2 behavior (blind SDK retry).
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
