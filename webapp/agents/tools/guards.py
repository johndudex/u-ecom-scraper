"""Tool-level guards that block calls violating known constraints.

Guards wrap tool functions and check the thread-local context before
executing.  If a constraint is violated, they return a blocking message
instead of executing the tool.

Three guards are provided:

- ``require_non_akamai_tool`` — blocks Playwright/web_fetch when the probe
  determined that UC Chrome is required (standard Chrome would be blocked).
- ``require_target_url`` — blocks navigation to URLs other than the target
  product URL (prevents agents from browsing off-target).
- ``require_non_blocked_domain`` — blocks web_fetch to the same domain when
  anti-bot was detected (HTTP requests would fail).

Guards support both sync and async tool functions. When applied to a
StructuredTool with a coroutine, the coroutine is also wrapped.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable
from urllib.parse import urlparse

from .context import (
    get_agent_name,
    get_probe_method,
    get_site_domain,
    get_target_url,
    is_anti_bot_detected,
)

logger = logging.getLogger(__name__)

_BLOCKED_BY_UC_CHROME = (
    "BLOCKED: This site requires UC Chrome ({method}). "
    "Playwright MCP browser tools are blocked — use probe_page for "
    "page access. Do NOT use playwright_browser_navigate, snapshot, "
    "click, or evaluate. Use probe_page (which routes through "
    "browser_service UC Chrome) for any page inspection needed."
)

_BLOCKED_OFF_TARGET = (
    "BLOCKED: You must analyze only the target product URL: {target}. "
    "Navigating to {url} is not allowed. Focus on the assigned product page."
)

_BLOCKED_DOMAIN_FETCH = (
    "BLOCKED: HTTP fetch to {domain} is blocked — anti-bot protection "
    "was detected. Direct HTTP requests to this domain return 403. "
    "Use probe_page for all page access."
)

_BLOCKED_WRONG_DOMAIN = (
    "BLOCKED: URL {url} is on a different domain ({probe_domain}) than "
    "the target site ({target_domain}). Stay on the target domain. "
    "Probing unrelated domains wastes budget and produces irrelevant results."
)


def _is_same_domain(url: str, domain: str) -> bool:
    try:
        return urlparse(url).hostname == domain
    except Exception:
        return False


def _coerce_url_str(value: Any) -> str:
    """Coerce a tool ``url`` argument into a string.

    LLMs occasionally pass ``url`` as a dict (e.g. ``{"url": "...", "title": ...}``)
    rather than a plain string.  Under langgraph v1 a tool that raises (here:
    ``urlparse``/``.rstrip`` on a dict) is re-raised by the ToolNode instead of
    being swallowed into a retry message, which crashes the agent.  Normalizing
    to a string up-front keeps the guards robust.  Generic — not site-specific.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "href", "link", "address", "value"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner
        return str(value)
    if value is None:
        return ""
    return str(value)


def _make_guard(check_fn: Callable[..., str | None]) -> Callable:
    """Create a guard that works with both sync and async functions."""

    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                block_msg = check_fn(func, args, kwargs)
                if block_msg:
                    return block_msg
                return await func(*args, **kwargs)

            return async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                block_msg = check_fn(func, args, kwargs)
                if block_msg:
                    return block_msg
                return func(*args, **kwargs)

            return sync_wrapper

    return decorator


def _check_akamai(func: Callable, args: tuple, kwargs: dict) -> str | None:
    method = get_probe_method()
    func_name = getattr(func, "__name__", "tool")
    logger.debug(
        "Guard require_non_akamai_tool: checking %s, method=%s",
        func_name,
        method,
    )
    if "error" in method or "failed" in method:
        logger.debug(
            "Guard: probe errored (%s), not blocking — allow agent to retry",
            method,
        )
        return None
    if method.startswith("uc_chrome"):
        if func_name in ("sync_call",):
            logger.info(
                "Guard: blocking %s — UC Chrome required (method=%s)",
                func_name,
                method,
            )
            return _BLOCKED_BY_UC_CHROME.format(method=method)
        return None
    return None


def _check_target_url(func: Callable, args: tuple, kwargs: dict) -> str | None:
    url = _coerce_url_str(kwargs.get("url", ""))
    if not url and args:
        url = _coerce_url_str(args[0])
    if not url:
        return None

    agent = get_agent_name()
    if agent not in ("site_analyzer", "product_analyzer"):
        return None

    target = get_target_url()
    if not target:
        return None

    if _urls_match(url, target):
        return None

    if agent == "product_analyzer":
        target_domain = get_site_domain()
        url_domain = None
        try:
            url_domain = urlparse(url).hostname
        except Exception:
            pass
        if url_domain and target_domain and url_domain == target_domain:
            return None

    logger.info(
        "Guard: blocking %s — off-target URL %s (target=%s, agent=%s)",
        getattr(func, "__name__", "tool"),
        url[:100],
        target[:100],
        agent,
    )
    return _BLOCKED_OFF_TARGET.format(target=target, url=url[:200])


def _check_blocked_domain(func: Callable, args: tuple, kwargs: dict) -> str | None:
    url = _coerce_url_str(kwargs.get("url", ""))
    if not url:
        return None
    if not is_anti_bot_detected():
        return None
    site_domain = get_site_domain()
    if not site_domain or not _is_same_domain(url, site_domain):
        return None
    logger.info(
        "Guard: blocking web_fetch to %s — anti-bot detected",
        url[:100],
    )
    return _BLOCKED_DOMAIN_FETCH.format(domain=site_domain)


def _check_same_domain(func: Callable, args: tuple, kwargs: dict) -> str | None:
    url = _coerce_url_str(kwargs.get("url", ""))
    if not url and args:
        url = _coerce_url_str(args[0])
    if not url:
        return None
    try:
        probe_domain = urlparse(url).hostname or ""
    except Exception:
        return None
    if not probe_domain:
        return None
    site_domain = get_site_domain()
    if not site_domain:
        return None
    if probe_domain == site_domain:
        return None
    logger.info(
        "Guard: blocking %s — wrong domain %s (target=%s)",
        getattr(func, "__name__", "tool"),
        probe_domain,
        site_domain,
    )
    return _BLOCKED_WRONG_DOMAIN.format(
        url=url[:200], probe_domain=probe_domain, target_domain=site_domain
    )


require_non_akamai_tool = _make_guard(_check_akamai)
require_target_url = _make_guard(_check_target_url)
require_non_blocked_domain = _make_guard(_check_blocked_domain)
require_same_domain = _make_guard(_check_same_domain)


def _urls_match(url_a: str, url_b: str) -> bool:
    """Check if two URLs refer to the same page.

    Compares scheme + hostname + path, ignoring:
    - trailing slashes
    - .html extension
    - query parameters
    - fragment identifiers
    """
    try:
        a = urlparse(url_a)
        b = urlparse(url_b)

        path_a = a.path.rstrip("/").removesuffix(".html")
        path_b = b.path.rstrip("/").removesuffix(".html")

        return a.hostname == b.hostname and path_a == path_b
    except Exception:
        try:
            return str(url_a).rstrip("/") == str(url_b).rstrip("/")
        except Exception:
            return False


def _catch_tool_errors_sync(func: Callable) -> Callable:
    """Wrap a tool func so exceptions return an error message instead of raising.

    v1's ToolNode RE-RAISES tool errors (v0.6 swallowed them into a retry
    message); this restores the forgiving v0.6 behavior so an agent whose tool
    errors gets a ``[tool error: ...]`` message + can recover, instead of the
    whole agent crashing. Generic — applies to every tool. [v1 tool-error handling]
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(
                "tool %s raised %s: %s — returning error message",
                getattr(func, "__name__", "tool"),
                type(e).__name__,
                str(e)[:200],
            )
            return f"[tool error: {type(e).__name__}: {str(e)[:500]}]"

    return wrapper


def _catch_tool_errors_async(coro: Callable) -> Callable:
    @functools.wraps(coro)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            logger.warning(
                "tool %s raised %s: %s — returning error message",
                getattr(coro, "__name__", "tool"),
                type(e).__name__,
                str(e)[:200],
            )
            return f"[tool error: {type(e).__name__}: {str(e)[:500]}]"

    return wrapper


def apply_tool_error_catcher(tool_obj: Any) -> Any:
    """Wrap a tool's func/coroutine to catch exceptions → error message.

    Apply AFTER guards so it's the outer wrapper (catches both tool-func errors
    and guard errors). Idempotent (skips if already applied). Works with
    ``@tool`` functions and ``StructuredTool`` instances.
    """
    if hasattr(tool_obj, "func") and tool_obj.func is not None:
        if not getattr(tool_obj, "_error_catcher_applied", False):
            tool_obj.func = _catch_tool_errors_sync(tool_obj.func)
            if hasattr(tool_obj, "coroutine") and tool_obj.coroutine is not None:
                tool_obj.coroutine = _catch_tool_errors_async(tool_obj.coroutine)
            try:
                tool_obj._error_catcher_applied = True
            except Exception:
                pass
    return tool_obj


def apply_guard(tool_obj: Any, guard: Callable) -> Any:
    """Apply a guard to a LangChain BaseTool by wrapping its func and coroutine.

    Returns the same tool object with the wrapped functions. Works with
    both ``@tool`` decorated functions and ``StructuredTool`` instances.
    """
    if hasattr(tool_obj, "func") and tool_obj.func is not None:
        tool_obj.func = guard(tool_obj.func)
        if hasattr(tool_obj, "coroutine") and tool_obj.coroutine is not None:
            tool_obj.coroutine = guard(tool_obj.coroutine)
        return tool_obj

    if callable(tool_obj):
        return guard(tool_obj)

    logger.warning(
        "Guard: cannot wrap tool %s — no func attribute",
        getattr(tool_obj, "name", "unknown"),
    )
    return tool_obj

    if callable(tool_obj):
        return guard(tool_obj)

    logger.warning(
        "Guard: cannot wrap tool %s — no func attribute",
        getattr(tool_obj, "name", "unknown"),
    )
    return tool_obj
