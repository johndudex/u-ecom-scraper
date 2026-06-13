"""Playwright MCP tools for LangGraph agents.

Connects to the Playwright MCP server (running in Docker) via SSE transport
using the ``mcp`` library directly, then converts MCP tools to LangChain
BaseTool instances.

The MCP server connects to browser-service's no-proxy Chrome instance via CDP
(`http://browser-service:9222`).

Each tool call opens a fresh SSE connection so that closed sessions never
cause errors.  Tools are cached for the process lifetime.

The module tracks browser availability in ``playwright_status`` so that
agent factories can make informed decisions when the MCP server is
unreachable.
"""

import asyncio
import logging
from typing import Any, Optional

from langchain_core.tools import BaseTool
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://localhost:8111/sse"

_MAX_TOOL_OUTPUT_CHARS = 3000

_SNAPSHOT_TOOLS = {"browser_snapshot", "browser_accessibility", "browser_full_snapshot"}

_cached_tools: list[BaseTool] | None = None
_PREFIX = "playwright_"

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
    name = type(exc).__name__
    msg = str(exc).lower()

    if "connection refused" in msg or "connectionreseterror" in msg or "connecterror" in msg:
        return "connection_refused"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "ssl" in msg or "certificate" in msg:
        return "ssl_error"
    if "resolve" in msg or "getaddrinfo" in msg:
        return "dns_error"
    return f"{name}: {str(exc)[:200]}"


async def _list_tools(mcp_url: str) -> list[Any]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(mcp_url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


async def _call_mcp_tool(mcp_url: str, tool_name: str, arguments: dict) -> str:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    try:
        async with sse_client(mcp_url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
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

                if len(output) > _MAX_TOOL_OUTPUT_CHARS:
                    output = (
                        output[:_MAX_TOOL_OUTPUT_CHARS]
                        + f"\n\n[... truncated {len(output)} → {_MAX_TOOL_OUTPUT_CHARS} chars]"
                    )
                return output
    except Exception:
        logger.exception("Playwright MCP tool '%s' failed", tool_name)
        return f"Error: Playwright MCP tool '{tool_name}' failed"


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


def create_playwright_tools_sync(mcp_url: Optional[str] = None) -> list[BaseTool]:
    global _cached_tools, playwright_status

    if _cached_tools is not None:
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
