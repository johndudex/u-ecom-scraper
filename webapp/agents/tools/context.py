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

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_ctx: dict = {
    "state": None,
    "agent_name": "",
    "probe_method": "",
    "anti_bot": False,
}


def set_tool_context(state: dict, agent_name: str = "") -> None:
    _ctx["state"] = state
    _ctx["agent_name"] = agent_name
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


def clear_tool_context() -> None:
    _ctx["state"] = None
    _ctx["agent_name"] = ""
    _ctx["probe_method"] = ""
    _ctx["anti_bot"] = False


def get_state() -> Optional[dict]:
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
