"""Playwright MCP tools for LangGraph agents.

Connects to the Playwright MCP server (running in Docker) via SSE transport
using the ``mcp`` library directly, then converts MCP tools to LangChain
BaseTool instances.

The MCP server connects to browser_service's no-proxy Chrome instance via CDP
(`http://browser_service:9222`).

A pooled ``ClientSession`` is reused across tool calls to avoid the expensive
SSE handshake (open + initialize) on every call. The session is lazily
created on first use and torn down when the running event loop changes
(e.g. a new Celery task) or when a tool call fails. Layered timeouts — a
per-call wall clock via ``asyncio.wait_for`` plus the SSE transport's
``sse_read_timeout`` — bound how long a single call can hang.

The module tracks browser availability in ``playwright_status`` so that
agent factories can make informed decisions when the MCP server is
unreachable.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Optional

from langchain_core.tools import BaseTool
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://localhost:8111/sse"

_MAX_TOOL_OUTPUT_CHARS = 30000

# --- MCP timeout constants ---------------------------------------------
# SSE read timeout for the pooled session. The mcp library's default is 300s,
# which is the root cause of the 20-minute stalls: a stalled SSE response
# would hang for the full 5 minutes before timing out. 90s is plenty for any
# legitimate Playwright MCP operation; the wall-clock cap below is the
# backstop.
_MCP_SSE_READ_TIMEOUT = 90.0

# tools/list is a tiny request — fail fast if the server is slow to enumerate.
# This runs once at tool-registration time (not per tool call).
_MCP_LIST_TOOLS_TIMEOUT = 20.0

# Belt-and-suspenders wall-clock cap on a single call_tool invocation. The
# SSE read timeout above should fire first for stalled responses; this catches
# any case where the call hangs without an SSE-level timeout.
_MCP_WALL_CLOCK_TIMEOUT = 120.0

# Per-category retry delays. Each category lists the delays applied BEFORE
# each retry (so a 2-tuple means "up to 2 retries"). An empty tuple means
# "no retry — fail fast".
_MCP_RETRY_DELAYS: dict[str, tuple[float, ...]] = {
    "connection_refused": (2.0, 5.0),
    "other_transient": (2.0,),
    "read_timeout": (),
    "unknown": (),
}

_SNAPSHOT_TOOLS = {"browser_snapshot", "browser_accessibility", "browser_full_snapshot"}

# Evaluate tools return structured programmatic data (e.g. JSON from
# extraction scripts).  These should NOT be truncated or the JSON becomes
# unparseable.  Use a much larger limit for these.
_EVALUATE_TOOLS = {"browser_evaluate", "playwright_browser_evaluate"}
_MAX_EVALUATE_OUTPUT_CHARS = 200000

_cached_tools: list[BaseTool] | None = None
_PREFIX = "playwright_"

# --- Pooled MCP session state -------------------------------------------
# A single ClientSession is reused across tool calls to skip the SSE
# handshake on every call. Lazily created on first use; torn down when the
# running event loop changes (new Celery task) or when a call fails.
_session: Any = None  # ClientSession
_session_stack: Optional[AsyncExitStack] = None
_session_loop: Any = None  # asyncio.AbstractEventLoop for stale detection
_session_lock: Optional[asyncio.Lock] = None

playwright_status: dict[str, Any] = {
    "available": False,
    "checked": False,
    "error": "",
    "url": "",
    "tool_count": 0,
}


def get_playwright_status() -> dict[str, Any]:
    return dict(playwright_status)


def _resolve_mcp_url(mcp_url: Optional[str] = None) -> str:
    if mcp_url:
        return mcp_url
    try:
        from django.conf import settings

        url = getattr(settings, "PLAYWRIGHT_MCP_URL", "")
        if url:
            return url
    except Exception:
        pass
    return DEFAULT_MCP_URL


def _classify_error(exc: Exception) -> str:
    """Classify an exception into a retry-strategy category.

    Returns one of:

    - ``"connection_refused"``: ECONNREFUSED, ConnectError, "connect error".
      The MCP server is likely down or restarting → 2 retries (2s + 5s).
    - ``"read_timeout"``: ReadTimeout, asyncio.TimeoutError, "timed out".
      A stalled SSE response — retrying immediately would hit the same
      timeout → 0 retries (fail fast).
    - ``"other_transient"``: ECONNRESET, broken pipe, "connection closed",
      "session is closed". The connection died mid-call → 1 retry (2s).
    - ``"unknown"``: anything else. Treat as a logic error → 0 retries.

    Unwraps anyio ExceptionGroups which wrap the real cause (e.g. a
    ``McpError("Connection closed")`` hidden inside a TaskGroup wrapper whose
    own message doesn't contain the marker).
    """
    # Collect all error messages: the top-level + any nested (ExceptionGroup).
    msgs = [str(exc).lower(), repr(exc).lower(), type(exc).__name__.lower()]
    if hasattr(exc, "exceptions"):  # BaseExceptionGroup (Python 3.11+, anyio)
        for inner in exc.exceptions:
            msgs.append(str(inner).lower())
            msgs.append(repr(inner).lower())
            msgs.append(type(inner).__name__.lower())
            if hasattr(inner, "exceptions"):
                for inner2 in inner.exceptions:
                    msgs.append(str(inner2).lower())
                    msgs.append(type(inner2).__name__.lower())
    full = " ".join(msgs)

    # Order matters: read_timeout before connection_refused, because an
    # asyncio.TimeoutError raised by wait_for should map to read_timeout
    # regardless of any "connection" wording in nested exceptions.
    if (
        isinstance(exc, asyncio.TimeoutError)
        or "readtimeout" in full
        or "timed out" in full
        or '"timeout"' in full
    ):
        return "read_timeout"
    if (
        "connection refused" in full
        or "econnrefused" in full
        or "connecterror" in full
        or "connect error" in full
        or "[errno 111]" in full
    ):
        return "connection_refused"
    if (
        "econnreset" in full
        or "connection reset" in full
        or "connectionreseterror" in full
        or "broken pipe" in full
        or "connection closed" in full
        or "connection aborted" in full
        or "econnaborted" in full
        or "remote end closed" in full
        or "session is closed" in full
        or "session closed" in full
    ):
        return "other_transient"
    return "unknown"


async def _get_session(mcp_url: str) -> Any:
    """Lazily create or reuse the pooled MCP ``ClientSession``.

    - If a session exists AND its event loop is the running loop → reuse it.
    - If a session exists BUT the loop changed (new Celery task) → close it
      and create a new one.
    - If no session → open ``sse_client`` + ``ClientSession`` + ``initialize()``.

    A module-level ``asyncio.Lock`` prevents two concurrent callers from
    both creating a session. The lock is created lazily because the event
    loop may not exist at import time.
    """
    global _session, _session_stack, _session_loop, _session_lock

    if _session_lock is None:
        _session_lock = asyncio.Lock()

    async with _session_lock:
        running_loop = asyncio.get_running_loop()

        # Reuse path: same session, same loop.
        if _session is not None and _session_loop is running_loop:
            return _session

        # Stale-session path: loop changed (new Celery task). Tear down
        # before creating a fresh one.
        if _session is not None:
            await _close_session()

        from mcp import ClientSession
        from mcp.client.sse import sse_client

        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(mcp_url, sse_read_timeout=_MCP_SSE_READ_TIMEOUT)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await asyncio.wait_for(session.initialize(), timeout=20)
        except Exception:
            # Clean up any partially-initialized state before re-raising.
            try:
                await stack.aclose()
            except Exception as close_exc:
                logger.warning(
                    "Error closing partial MCP session stack: %s", close_exc
                )
            raise

        _session = session
        _session_stack = stack
        _session_loop = running_loop
        logger.debug("Created pooled MCP session to %s", mcp_url)
        return session


async def _close_session() -> None:
    """Tear down the pooled MCP session (best-effort).

    Does NOT acquire the session lock — callers that need exclusive access
    (e.g. ``_get_session``) must hold it. ``_call_mcp_tool`` calls this
    directly on failure, accepting the small race window in exchange for
    simplicity: the next ``_get_session`` will recreate the session under
    the lock.

    **Cross-loop safety**: when the session was created on a now-dead event
    loop (common with the ``asyncio.run``-per-call pattern in sync tool
    dispatch), ``stack.aclose()`` triggers anyio's cross-task cancel-scope
    error (``RuntimeError: generator didn't stop after athrow()``) and can
    hang indefinitely. In that case we skip ``aclose`` — the dead loop's
    ``shutdown_asyncgens`` already finalized the underlying SSE/httpx
    connections. For same-loop sessions we ``aclose`` with a 10s timeout so
    a half-dead connection can't block cleanup.
    """
    global _session, _session_stack, _session_loop
    _session = None
    old_loop = _session_loop
    _session_loop = None
    stack = _session_stack
    _session_stack = None
    if stack is not None:
        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if old_loop is not None and old_loop is not running_loop:
                logger.debug(
                    "_close_session: discarding stale session (loop mismatch) "
                    "— dead loop already cleaned up connections"
                )
            else:
                await asyncio.wait_for(stack.aclose(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("_close_session: aclose timed out after 10s — discarding")
        except Exception as exc:
            logger.warning("Error closing MCP session stack: %s", exc)


def _format_tool_result(tool_name: str, result: Any) -> str:
    """Format an MCP ``CallToolResult`` into a string for the agent.

    Pulls the result-formatting concerns out of ``_call_mcp_tool`` so the
    call path stays clean:

    - Joins all content items (text preferred, else stringified).
    - For snapshot tools, runs ``headroom.compress`` when the output is large
      (keeps big a11y/snapshot trees from blowing up the LLM context).
    - Truncates to a per-tool-type limit (evaluate gets a much larger limit
      so structured JSON stays parseable).
    """
    if hasattr(result, "content") and result.content:
        parts = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        output = "\n".join(parts)
    else:
        output = str(result)

    # Snapshot compression — large accessibility/snapshot trees can dominate
    # the LLM context window. Compress in place when the saving is meaningful.
    if tool_name in _SNAPSHOT_TOOLS and len(output) > _MAX_TOOL_OUTPUT_CHARS:
        try:
            from headroom import compress as _compress

            cr = _compress(
                [{"role": "tool", "content": output}],
                model="glm-5-turbo",
            )
            compressed = cr.messages[0]["content"]
            if len(output) - len(compressed) > 200:
                logger.info(
                    "Snapshot compressed: %d → %d chars",
                    len(output),
                    len(compressed),
                )
                output = compressed
        except Exception:
            pass

    # Truncate large outputs — but use a much higher limit for evaluate
    # calls (structured JSON data must remain valid).
    max_chars = (
        _MAX_EVALUATE_OUTPUT_CHARS
        if tool_name in _EVALUATE_TOOLS
        else _MAX_TOOL_OUTPUT_CHARS
    )
    if len(output) > max_chars:
        output = (
            output[:max_chars]
            + f"\n\n[... truncated {len(output)} → {max_chars} chars]"
        )
    return output


async def _list_tools(mcp_url: str) -> list[Any]:
    """List MCP tools via a one-shot SSE session (used at registration time).

    Uses a short SSE read timeout — ``tools/list`` is a tiny request and we
    want to fail fast if the server is slow to enumerate. The pooled session
    is not used here because this runs before any tool call; each graph
    execution calls it once via ``create_playwright_tools``.
    """
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(
        mcp_url, sse_read_timeout=_MCP_LIST_TOOLS_TIMEOUT
    ) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=20)
            result = await session.list_tools()
            return result.tools


async def _call_mcp_tool(mcp_url: str, tool_name: str, arguments: dict) -> str:
    """Call an MCP tool via the pooled session with layered timeouts.

    Flow:

    1. ``session = await _get_session(mcp_url)`` (lazy create / reuse).
    2. ``await asyncio.wait_for(session.call_tool(...), timeout=120)``.
    3. On ``asyncio.TimeoutError`` → ``_close_session()`` → return error.
       No retry — a 120s hang means something is fundamentally wrong.
    4. On any other exception → classify → ``_close_session()`` → retry
       only if ``connection_refused`` (2 retries, 2s+5s) or
       ``other_transient`` (1 retry, 2s). ``read_timeout`` and ``unknown``
       fail immediately.

    Layered timeouts:

    - ``sse_read_timeout=90`` on the SSE transport bounds a stalled
      response at the protocol level (the root-cause fix for the 20-min
      stalls; the library default was 300s).
    - ``_MCP_WALL_CLOCK_TIMEOUT=120`` is the asyncio belt-and-suspenders
      cap. The SSE timeout should fire first; this backstops any case
      where the call hangs without an SSE-level wait.
    """
    attempt = 0
    while True:
        try:
            session = await _get_session(mcp_url)
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments=arguments),
                timeout=_MCP_WALL_CLOCK_TIMEOUT,
            )
            return _format_tool_result(tool_name, result)
        except asyncio.TimeoutError:
            # Wall-clock timeout fired — either the SSE read timeout didn't
            # catch it or the call hung without an SSE-level wait. Tear down
            # the session: the underlying connection is suspect. No retry.
            logger.error(
                "Playwright MCP tool '%s' timed out after %.0fs — closing session",
                tool_name,
                _MCP_WALL_CLOCK_TIMEOUT,
            )
            await _close_session()
            return (
                f"Error: Playwright MCP tool '{tool_name}' timed out after "
                f"{_MCP_WALL_CLOCK_TIMEOUT:.0f}s"
            )
        except Exception as exc:
            category = _classify_error(exc)
            # Always close the session on error — the connection may be bad,
            # and _get_session on the next attempt will create a fresh one.
            await _close_session()
            retry_delays = _MCP_RETRY_DELAYS.get(category, ())
            if attempt < len(retry_delays):
                delay = retry_delays[attempt]
                logger.warning(
                    "Playwright MCP tool '%s' hit %s on attempt %d: %s — "
                    "closing session, retrying in %.1fs",
                    tool_name,
                    category,
                    attempt + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            logger.error(
                "Playwright MCP tool '%s' failed (%s, not retrying): %s",
                tool_name,
                category,
                exc,
                exc_info=exc,
            )
            return f"Error: Playwright MCP tool '{tool_name}' failed: {exc}"


async def create_playwright_tools(mcp_url: Optional[str] = None) -> list[BaseTool]:
    resolved_url = _resolve_mcp_url(mcp_url)
    global playwright_status

    try:
        mcp_tools = await _list_tools(resolved_url)
    except Exception as exc:
        error_type = _classify_error(exc)
        logger.error(
            "Playwright MCP connection failed at %s: %s (%s)",
            resolved_url,
            error_type,
            exc,
        )
        playwright_status.update(
            available=False,
            checked=True,
            error=error_type,
            url=resolved_url,
            tool_count=0,
        )
        return []

    tools: list[BaseTool] = []
    for mcp_tool in mcp_tools:
        tools.append(_build_tool(resolved_url, mcp_tool))

    logger.info(
        "Playwright MCP (async): %d tools registered from %s",
        len(tools),
        resolved_url,
    )
    playwright_status.update(
        available=True,
        checked=True,
        error="",
        url=resolved_url,
        tool_count=len(tools),
    )
    return tools


def create_playwright_tools_sync(mcp_url: Optional[str] = None, fresh: bool = False) -> list[BaseTool]:
    global _cached_tools, playwright_status

    if _cached_tools is not None and not fresh:
        return _cached_tools

    resolved_url = _resolve_mcp_url(mcp_url)

    try:
        mcp_tools = asyncio.run(_list_tools(resolved_url))
    except Exception as exc:
        error_type = _classify_error(exc)
        logger.error(
            "Playwright MCP connection failed at %s: %s (%s)",
            resolved_url,
            error_type,
            exc,
        )
        playwright_status.update(
            available=False,
            checked=True,
            error=error_type,
            url=resolved_url,
            tool_count=0,
        )
        _cached_tools = []
        return _cached_tools

    tools: list[BaseTool] = []
    for mcp_tool in mcp_tools:
        tool = _build_tool(resolved_url, mcp_tool)
        tools.append(tool)

    logger.info(
        "Playwright MCP: %d tools registered from %s",
        len(tools),
        resolved_url,
    )
    _cached_tools = tools
    playwright_status.update(
        available=True,
        checked=True,
        error="",
        url=resolved_url,
        tool_count=len(tools),
    )
    return _cached_tools


def _build_tool(mcp_url: str, mcp_tool: Any) -> BaseTool:

    mcp_tool_name = mcp_tool.name
    tool_name = f"playwright_{mcp_tool_name}"

    def sync_call(**kwargs: Any) -> str:
        return asyncio.run(_call_mcp_tool(mcp_url, mcp_tool_name, kwargs))

    async def async_call(**kwargs: Any) -> str:
        return await _call_mcp_tool(mcp_url, mcp_tool_name, kwargs)

    desc = mcp_tool.description or mcp_tool.name
    input_schema: dict = {}
    if mcp_tool.inputSchema and mcp_tool.inputSchema.get("properties"):
        input_schema = mcp_tool.inputSchema

    return StructuredTool(
        name=tool_name,
        description=desc,
        func=sync_call,
        coroutine=async_call,
        args_schema=input_schema if input_schema else None,
    )
