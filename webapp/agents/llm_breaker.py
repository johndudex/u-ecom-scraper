"""Per-model circuit breaker for z.ai LLM calls.

Bounds how long a bad/stalling model receives traffic: after
``LLM_CIRCUIT_BREAKER_THRESHOLD`` consecutive failures (timeouts/connection
errors) on a model, the breaker trips and routes that model's traffic to
``ZAI_FALLBACK_MODEL`` for ``LLM_CIRCUIT_BREAKER_COOLDOWN`` seconds.

This is a deliberate, bounded resilience layer that COMPOSES with the Phase 2
classified-retry work (which moves retry down to the call boundary). It does
NOT replace cancellation — a tripped breaker only avoids *sending* new calls to
a known-bad model; an in-flight call is still bounded by the call timeout.

In-process + thread-safe (one breaker state per celery worker process). Each
worker tracks its own view — acceptable because a stalling model stalls for all
workers, and the per-worker cooldown (default 60s) is short. A Redis-backed
cross-worker breaker is a future refinement, not needed for correctness.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# {model_name: {"failures": int, "tripped_until": float (epoch) | None}}
_state: dict[str, dict] = {}


def _config() -> tuple[bool, int, int, str]:
    """Read breaker config lazily (Django settings may not be ready at import)."""
    try:
        from django.conf import settings

        return (
            bool(getattr(settings, "LLM_CIRCUIT_BREAKER_ENABLED", True)),
            int(getattr(settings, "LLM_CIRCUIT_BREAKER_THRESHOLD", 4)),
            int(getattr(settings, "LLM_CIRCUIT_BREAKER_COOLDOWN", 60)),
            getattr(settings, "ZAI_FALLBACK_MODEL", "glm-5-turbo"),
        )
    except Exception:
        return True, 4, 60, "glm-5-turbo"


def _now() -> float:
    return time.monotonic()


def record_failure(model: Optional[str]) -> None:
    """Record a failed LLM call (timeout/connection/5xx). Trips the breaker at
    the configured threshold."""
    if not model:
        return
    enabled, threshold, cooldown, _ = _config()
    if not enabled:
        return
    with _lock:
        entry = _state.setdefault(model, {"failures": 0, "tripped_until": None})
        entry["failures"] += 1
        if entry["failures"] >= threshold and not entry.get("tripped_until"):
            entry["tripped_until"] = _now() + cooldown
            logger.warning(
                "llm_breaker: model '%s' tripped after %d consecutive failures "
                "(routing to fallback for %ds)",
                model,
                entry["failures"],
                cooldown,
            )


def record_success(model: Optional[str]) -> None:
    """Record a successful LLM call. Resets the consecutive-failure count."""
    if not model:
        return
    enabled, _, _, _ = _config()
    if not enabled:
        return
    with _lock:
        entry = _state.get(model)
        if entry and (entry["failures"] or entry.get("tripped_until")):
            entry["failures"] = 0
            entry["tripped_until"] = None


def is_tripped(model: Optional[str]) -> bool:
    """True if the breaker for ``model`` is currently tripped (within cooldown)."""
    if not model:
        return False
    enabled, _, _, _ = _config()
    if not enabled:
        return False
    with _lock:
        entry = _state.get(model)
        if not entry:
            return False
        until = entry.get("tripped_until")
        if until is None:
            return False
        if _now() < until:
            return True
        # Cooldown elapsed → auto-reset (half-open: let the next call through).
        entry["tripped_until"] = None
        entry["failures"] = 0
        return False


def effective_model(primary: Optional[str]) -> Optional[str]:
    """Return the model to actually use: the fallback if ``primary`` is tripped,
    else ``primary`` itself. No-op (returns primary) when disabled or primary is
    already the fallback."""
    if not primary:
        return primary
    enabled, _, _, fallback = _config()
    if not enabled or primary == fallback:
        return primary
    if is_tripped(primary):
        logger.info("llm_breaker: '%s' tripped → using fallback '%s'", primary, fallback)
        return fallback
    return primary


def status() -> dict[str, dict]:
    """Snapshot of breaker state (for diagnostics / health endpoints)."""
    with _lock:
        return {
            m: {"failures": e["failures"], "tripped": bool(e.get("tripped_until"))}
            for m, e in _state.items()
        }


# ── LangChain observation hook ──────────────────────────────────────────────
# Attached as a callback in graph._agent_config alongside _ToolCallLogger. It
# records per-LLM-call success/failure so the breaker reflects live traffic.
# Caller bugs (auth/bad-request) are NOT model-health failures → don't trip.

_CALLER_BUG_ERRORS = ("AuthenticationError", "BadRequestError", "PermissionDeniedError")


def _extract_model(serialized: dict | None) -> Optional[str]:
    """Pull the model name out of a langchain LLM `serialized` payload."""
    if not isinstance(serialized, dict):
        return None
    kwargs = serialized.get("kwargs")
    if isinstance(kwargs, dict):
        m = kwargs.get("model") or kwargs.get("model_name")
        if m:
            return str(m)
    # Fallback: some versions nest under invocation_params / name
    ip = serialized.get("invocation_params")
    if isinstance(ip, dict):
        m = ip.get("model") or ip.get("model_name")
        if m:
            return str(m)
    return None


from langchain_core.callbacks import BaseCallbackHandler


class CircuitBreakerCallback(BaseCallbackHandler):
    """LangChain BaseCallbackHandler that feeds the breaker.

    MUST extend BaseCallbackHandler (not a bare duck-typed class): langchain's
    callback manager accesses attributes like ``raise_error`` + calls many
    on_* hooks; the base class provides safe no-op defaults so this handler
    only needs to override ``on_llm_error``/``on_llm_end``. record_failure on
    health-class errors, record_success on success. Every override is defensive
    (never raises) — a throwing callback would disrupt langchain's run and mask
    the agent's own result.
    """

    def on_llm_error(self, error, *, serialized=None, **kwargs):  # type: ignore[no-untyped-def]
        try:
            err_type = type(error).__name__
            if any(bug in err_type for bug in _CALLER_BUG_ERRORS):
                return  # caller/config bug, not model health
            record_failure(_extract_model(serialized))
        except Exception:
            pass

    def on_llm_end(self, response, *, serialized=None, **kwargs):  # type: ignore[no-untyped-def]
        try:
            record_success(_extract_model(serialized))
        except Exception:
            pass
