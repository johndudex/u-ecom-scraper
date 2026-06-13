"""Tests for Playwright MCP tool connection.

Run locally (no Django required)::

    PLAYWRIGHT_MCP_URL=http://localhost:8111/sse pytest webapp/tests/test_playwright_mcp.py -v

Run inside Docker (requires playwright-mcp + chrome services up)::

    pytest webapp/tests/test_playwright_mcp.py -v
"""

import os
import sys

import pytest

MCP_URL = os.environ.get(
    "PLAYWRIGHT_MCP_URL",
    "http://localhost:8111/sse",
)


class TestMCPConnection:
    """Test SSE connection to the Playwright MCP server."""

    def test_list_tools_connects(self):
        """SSE client can connect and list tools without error."""
        import asyncio
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _list():
            async with sse_client(MCP_URL) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return result.tools

        tools = asyncio.run(_list(), debug=False)
        assert isinstance(tools, list)
        assert len(tools) > 0, "MCP server returned no tools"

    def test_tool_names_are_strings(self):
        """Every tool has a non-empty string name."""
        import asyncio
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _list():
            async with sse_client(MCP_URL) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return result.tools

        tools = asyncio.run(_list(), debug=False)
        for t in tools:
            assert isinstance(t.name, str) and t.name.strip(), f"Empty tool name: {t}"

    def test_call_tool_navigate(self):
        """Can call browser_navigate to a simple page and get content back."""
        import asyncio
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _call():
            async with sse_client(MCP_URL) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "browser_navigate",
                        arguments={"url": "https://example.com"},
                    )
                    return result

        result = asyncio.run(_call(), debug=False)
        assert result is not None
        assert hasattr(result, "content")
        assert len(result.content) > 0

    def test_fresh_connection_per_call(self):
        """Each call opens a fresh SSE connection (no shared session)."""
        import asyncio
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _call_twice():
            results = []
            for _ in range(2):
                async with sse_client(MCP_URL) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        results.append(len(result.tools))
            return results

        counts = asyncio.run(_call_twice(), debug=False)
        assert counts[0] == counts[1], "Tool count changed between connections"
        assert counts[0] > 0


class TestCreatePlaywrightTools:
    """Test the LangChain tool wrapper factory."""

    def test_sync_factory_returns_tools(self):
        """create_playwright_tools_sync returns a non-empty list."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        from agents.tools.playwright_tools import create_playwright_tools_sync

        _cached = create_playwright_tools_sync.__globals__.get("_cached_tools")
        if _cached is not None:
            create_playwright_tools_sync.__globals__["_cached_tools"] = None

        tools = create_playwright_tools_sync(mcp_url=MCP_URL)
        assert isinstance(tools, list)
        assert len(tools) > 0, "No Playwright tools created"

    def test_tool_has_name_and_description(self):
        """Each wrapped tool has a name and description."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        from agents.tools.playwright_tools import create_playwright_tools_sync

        create_playwright_tools_sync.__globals__["_cached_tools"] = None
        tools = create_playwright_tools_sync(mcp_url=MCP_URL)
        for t in tools:
            assert t.name, f"Tool missing name: {t}"
            assert t.description, f"Tool '{t.name}' missing description"

    def test_tool_invoke_navigate(self):
        """Can invoke playwright_browser_navigate through the LangChain wrapper."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        from agents.tools.playwright_tools import create_playwright_tools_sync

        create_playwright_tools_sync.__globals__["_cached_tools"] = None
        tools = create_playwright_tools_sync(mcp_url=MCP_URL)

        nav_tools = [t for t in tools if t.name == "playwright_browser_navigate"]
        assert len(nav_tools) == 1, "playwright_browser_navigate tool not found"

        result = nav_tools[0].invoke({"url": "https://example.com"})
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0
