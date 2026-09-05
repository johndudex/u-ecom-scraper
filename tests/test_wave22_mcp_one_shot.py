"""[wave-22 A4] The pooled MCP session must die — one-shot sessions per call.

The pool was dead weight with hazardous failure modes: sync tool dispatch runs
``asyncio.run`` PER CALL, so every call found a session bound to a dead loop,
tore it down (cross-loop ``aclose`` → anyio "generator didn't stop" hazards),
and rebuilt it anyway — the handshake was never saved. Meanwhile the global
``asyncio.Lock`` bought nothing and added three cross-loop hazards: a
contended-lock ``RuntimeError`` laundered as an MCP failure, a non-threadsafe
release, and a lock-free ``_close_session`` racing the holder's republish.

Contract:
- the pool globals (``_session``/``_session_stack``/``_session_loop``/
  ``_session_lock``) and ``_get_session`` are DELETED — two calls in the same
  event loop each dial a fresh SSE session (one-shot, like ``_list_tools``);
- the previously-unbounded ``enter_async_context(sse_client(...))`` is bounded
  by ``wait_for(..., _MCP_CONNECT_TIMEOUT=30)``;
- the whole call body is clamped to ``min(class budget, remaining
  get_tool_deadline())`` — 120s default, 240s for ``browser_network_requests``;
- ``asyncio.TimeoutError`` gets exactly ONE retry on a fresh session (was 0);
- a 0-remaining budget refuses to dial at all (honest skip, like shell_tools'
  run_scraper guard);
- CDP-tab continuity across navigate→snapshot is a browser-side property (the
  MCP server runs ``--cdp-endpoint``; tab state lives in Chrome, not in our
  SSE session) — asserted end-to-end, not here.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
import time
import types
from contextlib import asynccontextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

pt = importlib.import_module("agents.tools.playwright_tools")  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_deadline():
    from agents.tools.context import set_tool_deadline

    set_tool_deadline(None)
    yield
    set_tool_deadline(None)


def _call_mcp_tool_src() -> str:
    return inspect.getsource(pt._call_mcp_tool)


# --- Fakes ---------------------------------------------------------------

def _install_fake_mcp(monkeypatch) -> dict:
    """Replace the ``mcp`` imports with a counting fake rig.

    ``_call_mcp_tool`` imports ``ClientSession`` / ``sse_client`` at call time,
    so sys.modules injection covers both today's pooled path and the one-shot
    rewrite without the real package installed.
    """
    state = {"sse_entries": 0, "sessions": 0, "calls": 0, "hang": False}

    class _FakeResult:
        content = []
        isError = False

    class _FakeSession:
        def __init__(self, read_stream, write_stream):
            state["sessions"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None):
            state["calls"] += 1
            if state["hang"]:
                raise asyncio.TimeoutError()
            return _FakeResult()

    def _fake_sse_client(url, sse_read_timeout=None):
        state["sse_entries"] += 1

        @asynccontextmanager
        async def _cm():
            yield ("read", "write")

        return _cm()

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.ClientSession = _FakeSession
    sse_mod = types.ModuleType("mcp.client.sse")
    sse_mod.sse_client = _fake_sse_client
    mcp_mod.client = sse_mod
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.sse", sse_mod)
    return state


# --- The pool is gone ------------------------------------------------------


class TestPoolIsGone:
    def test_pool_globals_deleted(self):
        for name in ("_session", "_session_stack", "_session_loop", "_session_lock"):
            assert not hasattr(pt, name), (
                f"pool global {name} still exists — the cross-loop hazard "
                f"family is still armed"
            )

    def test_get_session_deleted(self):
        assert not hasattr(pt, "_get_session"), (
            "_get_session still exists — the pool's reuse/stale-dance survived"
        )

    def test_call_tool_is_one_shot_same_loop(self):
        src = _call_mcp_tool_src()
        assert "AsyncExitStack" in src, (
            "_call_mcp_tool does not hold sse_client+ClientSession in ONE "
            "per-call AsyncExitStack entered and exited in the same loop"
        )
        assert "_get_session" not in src
        assert "asyncio.Lock" not in src

    def test_connect_is_bounded_at_30s(self):
        assert getattr(pt, "_MCP_CONNECT_TIMEOUT", None) == 30.0, (
            "enter_async_context(sse_client(...)) must be bounded by a 30s "
            "connect timeout — it was the one unbounded await on the path"
        )
        src = _call_mcp_tool_src()
        i = src.index("sse_client(mcp_url")  # the actual call, not the docstring
        window = src[max(0, i - 200): i + 200]
        assert "wait_for" in src[max(0, i - 200): i], (
            "the sse_client connect await is not wrapped in wait_for"
        )
        assert "_MCP_CONNECT_TIMEOUT" in window, (
            "the connect wait_for must use the _MCP_CONNECT_TIMEOUT constant"
        )


# --- Budget math (pure) ----------------------------------------------------


class TestCallBudgetClamp:
    NOW = 1_000_000.0

    def test_default_budget_is_120(self):
        assert pt._effective_tool_budget("browser_snapshot", now=self.NOW) == 120.0

    def test_network_requests_get_240(self):
        assert (
            pt._effective_tool_budget("browser_network_requests", now=self.NOW)
            == 240.0
        )

    def test_clamps_to_remaining_deadline(self):
        from agents.tools.context import set_tool_deadline

        set_tool_deadline(self.NOW + 50.0)
        assert pt._effective_tool_budget("browser_snapshot", now=self.NOW) == 50.0

    def test_exhausted_deadline_is_zero(self):
        from agents.tools.context import set_tool_deadline

        set_tool_deadline(self.NOW - 1.0)
        assert pt._effective_tool_budget("browser_snapshot", now=self.NOW) == 0.0

    def test_no_deadline_is_passthrough(self):
        assert pt._effective_tool_budget("browser_snapshot", now=self.NOW) == 120.0


# --- Call behavior (fakes) --------------------------------------------------


class TestCallBehavior:
    def test_timeout_retries_once_on_a_fresh_session(self, monkeypatch):
        state = _install_fake_mcp(monkeypatch)
        state["hang"] = True
        result = asyncio.run(
            pt._call_mcp_tool("http://mcp/sse", "browser_navigate", {})
        )
        assert state["calls"] == 2, (
            "a wall-clock timeout must get exactly ONE retry on a fresh "
            "session (was 0 — one transient SSE stall killed the whole call)"
        )
        assert state["sessions"] == 2, "the retry must dial a FRESH session"
        assert "timed out" in result.lower()

    def test_two_calls_same_loop_dial_two_sessions(self, monkeypatch):
        state = _install_fake_mcp(monkeypatch)

        async def _two_calls():
            await pt._call_mcp_tool("http://mcp/sse", "browser_navigate", {})
            await pt._call_mcp_tool("http://mcp/sse", "browser_snapshot", {})

        asyncio.run(_two_calls())
        assert state["sse_entries"] == 2, (
            "two calls in the SAME loop shared the pooled session — the pool "
            "must be deleted (tab continuity lives in Chrome via CDP, not in "
            "our SSE session)"
        )

    def test_exhausted_budget_refuses_without_dialing(self, monkeypatch):
        from agents.tools.context import set_tool_deadline

        state = _install_fake_mcp(monkeypatch)
        set_tool_deadline(time.time() - 1)
        result = asyncio.run(
            pt._call_mcp_tool("http://mcp/sse", "browser_navigate", {})
        )
        assert state["sse_entries"] == 0, (
            "a 0-remaining budget must not dial the MCP server at all"
        )
        assert "wall clock" in result.lower()

    def test_budget_reaches_the_call(self, monkeypatch):
        from agents.tools.context import set_tool_deadline

        _install_fake_mcp(monkeypatch)
        real_wait_for = asyncio.wait_for
        seen: list = []

        async def _spy(coro, timeout=None):
            seen.append(timeout)
            return await real_wait_for(coro, timeout)

        monkeypatch.setattr(asyncio, "wait_for", _spy)
        set_tool_deadline(time.time() + 500)  # healthy — class budget applies
        asyncio.run(pt._call_mcp_tool("http://mcp/sse", "browser_snapshot", {}))
        budgets = [t for t in seen if t not in (30.0, 20.0)]
        assert budgets and budgets[0] == 120.0, (
            f"the call_tool wait_for must carry the clamped budget, saw {seen}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
