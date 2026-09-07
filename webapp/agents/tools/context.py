"""Process-global context for tool-level guards.

Tools need access to the current graph state (probe results, target URL,
agent name) to enforce guards at execution time. This module uses a
simple module-level dict because contextvars and threading.local both
fail to propagate across LangGraph's internal asyncio task boundaries.

Celery uses prefork workers, so each worker process handles one task at
a time. A module-level global is safe in this context.

Usage in graph.py::

    from .tools.context import set_tool_context, clear_tool_context

    def _invoke_site_analyzer(state, config):
        set_tool_context(state, agent_name="site_analyzer")
        try:
            result = agent.invoke(...)
        finally:
            clear_tool_context()
"""

from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from collections import OrderedDict

logger = logging.getLogger(__name__)

_ctx: dict = {
    "state": None,
    "agent_name": "",
    "probe_method": "",
    "anti_bot": False,
    "tool_deadline": None,
    "invocation_cancelled": False,
}

# ---------------------------------------------------------------------------
# [wave-24 W24-1] Per-invocation cancellation latch.
#
# The legacy flag above is SELF-RESETTING: every fresh invocation's
# ``set_tool_context`` writes ``invocation_cancelled = False`` — and a fresh
# invocation is exactly the boundary a zombie thread crosses (prod 395: writer
# wall-clock death → tester phase starts → the zombie's ``edit_file`` landed
# 2s into the tester's run). The flag is kept for UNSTAMPED callers
# (playground, direct tool calls), but every agent invocation now also carries
# a per-invocation id:
#
#   * ``_invoke_agent_with_timeout`` mints the id on the node thread and
#     stamps it INSIDE the spawned invoke thread's context (``stamp_invocation``);
#   * LangGraph's ToolNode dispatches sync tools through a
#     ``ContextThreadPoolExecutor`` that copies the SUBMITTING thread's
#     context, so every tool call the agent makes — zombie included —
#     executes with that id visible;
#   * ``mark_invocation_cancelled`` records the id into the process-global
#     set below, which ``set_tool_context`` deliberately NEVER resets.
# ---------------------------------------------------------------------------

_current_invocation_id: contextvars.ContextVar = contextvars.ContextVar(
    "wave24_invocation_id", default=None
)
_cancelled_invocations: OrderedDict[str, None] = OrderedDict()
_CANCELLED_INVOCATIONS_CAP = 512
_cancelled_lock = threading.Lock()

# One-time runtime verification that context actually propagates through the
# executor layer ToolNode uses (get_executor_for_config). If a langchain-core
# upgrade ever swaps the context-propagating pool for a plain one, the
# per-invocation latch silently degrades to the legacy global flag — this
# makes the degradation loud instead of silent.
_propagation_checked = False
_propagation_ok = True


def set_tool_context(state: dict, agent_name: str = "") -> None:
    _ctx["state"] = state
    _ctx["agent_name"] = agent_name
    # Fresh invocation re-arms the LEGACY flag only. A stamped zombie thread
    # (wave-24 W24-1) stays disarmed via the per-invocation id set below,
    # which this reset must never touch — the prod-395 regression happened
    # exactly here, at the phase boundary the latch exists to survive.
    _ctx["invocation_cancelled"] = False
    probe = state.get("probe_result")
    if probe and isinstance(probe, dict):
        _ctx["probe_method"] = (
            probe.get("connectivity", {}).get("method_that_worked", "")
            or probe.get("method", "")
        )
        _ctx["anti_bot"] = (
            probe.get("anti_bot", {}).get("detected", False)
            if isinstance(probe.get("anti_bot"), dict)
            else False
        )
        # Robust fallback: if the ONLY working access method is a stealth
        # browser (uc_chrome_* or cloak_*), vanilla browser/HTTP were blocked
        # → anti-bot is present even if the probe didn't raise an explicit flag.
        if not _ctx["anti_bot"]:
            _m = str(_ctx["probe_method"])
            if _m.startswith("uc_chrome") or _m.startswith("cloak"):
                _ctx["anti_bot"] = True
    else:
        _ctx["probe_method"] = ""
        _ctx["anti_bot"] = False


def update_probe_result(result: dict) -> None:
    method = result.get("method", "")
    conn = result.get("connectivity") or {}
    if isinstance(conn, dict) and conn.get("method_that_worked"):
        method = conn["method_that_worked"]
    if "error" in method or "failed" in method:
        _ctx["probe_method"] = ""
        _ctx["anti_bot"] = False
        return
    _ctx["probe_method"] = method
    _ctx["anti_bot"] = bool(result.get("blocked", False))
    # T3.6: same stealth fallback set_tool_context applies — if the ONLY
    # working access method is a stealth browser, vanilla access was blocked
    # even without an explicit anti-bot flag. ``fingerprint_*`` (curl_cffi TLS
    # impersonation) is deliberately NOT here: it is HTTP-flavoured, and the
    # http_fetch proxy ladder — not the cloak browser — owns those sites.
    if not _ctx["anti_bot"]:
        try:
            from ..constants import STEALTH_METHOD_PREFIXES

            if str(method).startswith(STEALTH_METHOD_PREFIXES):
                _ctx["anti_bot"] = True
        except Exception:
            if str(method).startswith(("uc_chrome", "cloak")):
                _ctx["anti_bot"] = True


def clear_tool_context() -> None:
    _ctx["state"] = None
    _ctx["agent_name"] = ""
    _ctx["probe_method"] = ""
    _ctx["anti_bot"] = False
    _ctx["tool_deadline"] = None


def new_invocation_id() -> str:
    """Mint a per-invocation disarm id ([wave-24 W24-1])."""
    return uuid.uuid4().hex


def stamp_invocation(invocation_id: str) -> contextvars.Token:
    """Stamp the CALLING context with the invocation id.

    Call INSIDE the invoke thread (sync path) or on the event-loop thread
    before ``run_until_complete`` (async path — asyncio tasks copy the
    creating context, and the sync tools ``ainvoke`` dispatches run under the
    copied context). Returns the ContextVar token so the async path can
    unstamp in its ``finally``.
    """
    return _current_invocation_id.set(invocation_id)


def unstamp_invocation(token: contextvars.Token) -> None:
    """Undo a ``stamp_invocation`` (async path finally). Tolerates a token
    from an already-replaced context by falling back to an explicit None."""
    try:
        _current_invocation_id.reset(token)
    except Exception:
        _current_invocation_id.set(None)


def mark_invocation_cancelled(phase: str = "", invocation_id: str | None = None) -> None:
    """Latch the tool gate shut for the current invocation.

    [job-329] ``_invoke_agent_with_timeout`` abandons its thread on wall-clock
    deadline, but the zombie keeps looping LLM rounds whose ``edit_file`` calls
    corrupted the draft AFTER the tester verdict. Python cannot kill the
    thread — so abandonment disarms it and the global BaseTool patch makes
    every subsequent tool call raise.

    [wave-24 W24-1] The disarm now records the invocation ID into a
    process-global set that NO fresh ``set_tool_context`` resets, so the
    zombie stays disarmed across the phase boundary (the legacy flag alone
    was re-armed by the very next phase — prod 395). ``invocation_id``
    defaults to the calling context's stamp when the caller doesn't pass one.
    """
    target = (
        invocation_id if invocation_id is not None else _current_invocation_id.get(None)
    )
    if target is not None:
        with _cancelled_lock:
            _cancelled_invocations[target] = None
            while len(_cancelled_invocations) > _CANCELLED_INVOCATIONS_CAP:
                _cancelled_invocations.popitem(last=False)
    _ctx["invocation_cancelled"] = True
    _check_context_propagation_once()
    logger.warning(
        "tool context: %s invocation cancelled at wall-clock deadline — "
        "tools will refuse for this invocation%s",
        phase or "current",
        " (id latched)" if target is not None else " (legacy flag: unstamped)",
    )


def is_invocation_cancelled() -> bool:
    invocation_id = _current_invocation_id.get(None)
    if invocation_id is not None:
        with _cancelled_lock:
            return invocation_id in _cancelled_invocations
    return bool(_ctx["invocation_cancelled"])


def reset_invocation_latches() -> None:
    """[test support] Purge every latch: the cancelled-id set, the legacy
    flag, and the calling context's stamp. Production code must NEVER call
    this — it exists so a pytest process cannot leak a latch across tests
    (tests/conftest.py); the cancelled-id set intentionally has no production
    reset path."""
    with _cancelled_lock:
        _cancelled_invocations.clear()
    _ctx["invocation_cancelled"] = False
    _current_invocation_id.set(None)


def context_propagation_ok() -> bool | None:
    """True/False once the one-time executor propagation check has run,
    None before. Exposed for tests and health introspection."""
    return _propagation_ok if _propagation_checked else None


def _check_context_propagation_once() -> None:
    global _propagation_checked, _propagation_ok
    if _propagation_checked:
        return
    _propagation_checked = True
    try:
        from langchain_core.runnables.config import get_executor_for_config

        probe: contextvars.ContextVar = contextvars.ContextVar(
            "wave24_propagation_probe", default=None
        )
        probe.set("sentinel")
        # get_executor_for_config is a @contextmanager over
        # ContextThreadPoolExecutor — the same factory ToolNode uses — so
        # this probes exactly the dispatch boundary the latch leans on.
        with get_executor_for_config({}) as executor:
            seen = executor.submit(probe.get).result(timeout=10)
        _propagation_ok = seen == "sentinel"
    except Exception:
        _propagation_ok = False
    if not _propagation_ok:
        logger.error(
            "wave-24 zombie disarm DEGRADED: the tool executor does not "
            "propagate contextvars — the per-invocation latch falls back to "
            "the legacy global flag (which set_tool_context resets); pin "
            "langchain-core to a version whose get_executor_for_config "
            "copies context"
        )


def set_tool_deadline(deadline: float | None) -> None:
    """Stamp the invoking agent's wall-clock deadline (epoch seconds).

    [job-81] Blocking tools (run_scraper's browser dispatch) compare their own
    floored timeout against what's actually left of the invocation so a run
    that CANNOT finish is skipped with an honest marker instead of being
    abandoned mid-flight with its result lost. ``None`` (no deadline known)
    disables the guard — tools never refuse work for lack of information.
    """
    _ctx["tool_deadline"] = deadline


def get_tool_deadline() -> float | None:
    return _ctx["tool_deadline"]


def get_state() -> dict | None:
    return _ctx["state"]


def get_agent_name() -> str:
    return _ctx["agent_name"]


def get_probe_method() -> str:
    return _ctx["probe_method"]


def is_anti_bot_detected() -> bool:
    return _ctx["anti_bot"]


def get_target_url() -> str:
    state = get_state()
    if state:
        url = state.get("product_url") or ""
        if url:
            return url
        nav_findings = state.get("navigation_findings") or {}
        product_links = nav_findings.get("listing_page", {}).get("product_links") or []
        if product_links:
            return product_links[0]
        return state.get("url", "")
    return ""


def get_site_domain() -> str:
    state = get_state()
    if state:
        url = state.get("url", "")
        if url:
            from urllib.parse import urlparse

            try:
                return urlparse(url).hostname or ""
            except Exception:
                pass
    return ""
