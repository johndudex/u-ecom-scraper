"""[wave-15 PR-2b] W15-B harness — the per-phase async invoke path, hardened
to the three real traps found while planning (docs/wave-15-plan.md PR-2b).

The per-phase allowlist (``graph._async_execution_enabled``) gates real
cancellation: instead of abandoning a daemon thread on wall-clock timeout,
``agent.ainvoke`` runs under ``asyncio.wait_for`` in a manually managed loop
and the cancel CLOSES the in-flight z.ai socket. The traps:

1. **Streaming lane**: langchain's ``_should_stream`` consults
   ``model_fields_set`` — a streaming flag set as a CLASS DEFAULT silently
   routes the async lane to ``_agenerate`` (no chunks), proving nothing.
   ``get_llm`` must pass ``streaming=`` through the CONSTRUCTOR, and
   ``ClassifiedRetryChatOpenAI._astream`` must carry the same classified
   ladder + breaker as ``_stream`` (no capability lost on the async lane).
2. **Soft wall clock**: ``asyncio.run`` closes the loop with
   ``shutdown_default_executor`` — which JOINS the executor thread running an
   in-flight sync tool (measured: 2s deadline + 6s tool → 6.01s wall). The
   manual loop management must hold the deadline.
3. **Same agent, two loops**: langchain-openai caches the async httpx client
   on the instance; code_writer's re-invoke pattern (syntax fix /
   CLI-contract fix) runs the SAME object under a second loop. An
   "Event loop is closed" keep-alive surfaces as a bare RuntimeError outside
   the openai/httpx hierarchies — it must ride the transient ladder (retries
   feed the breaker → fallback model), not die with zero retries.

Django trap honored: no DB access inside any running loop — assertions read
the ``_error_class`` return, never a SessionLog row.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

import pytest

import agents.graph as g
from agents import llm as llm_mod


@pytest.fixture(autouse=True)
def _clear_tool_deadline():
    """``_invoke_agent_with_timeout`` STAMPS a tool deadline (job-81 N-B).

    A stale deadline leaks out of these tests into later ones — run_scraper
    then refuses real work with "insufficient wall clock … ~0s remain"
    (this bit webapp/tests/test_scrape_dispatch in the first full-gate run).
    Same hygiene contract as TestDeadlinePublication's try/finally.
    """
    from agents.tools.context import clear_tool_context

    yield
    try:
        clear_tool_context()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# W15-C — the per-phase allowlist gate
# ═══════════════════════════════════════════════════════════════════════════


class TestAsyncGate:
    def test_default_allowlist_is_empty(self, monkeypatch, settings):
        """DEFAULT OFF: no env, no override → every phase runs the sync
        thread path. The canary must be an explicit opt-in."""
        monkeypatch.delenv("AGENT_ASYNC_PHASES", raising=False)
        settings.LLM_ASYNC_EXECUTION = False
        # The shipped default: flipping _ASYNC_PHASES non-empty is the
        # deliberate default-flip and updates this pin.
        assert g._ASYNC_PHASES == frozenset()
        assert g._async_phase_allowlist() == set()
        for phase in ("code_writer", "code_tester", "site_analyzer", ""):
            assert g._async_execution_enabled(phase) is False

    def test_env_canary_enables_only_the_named_phase(self, monkeypatch, settings):
        monkeypatch.setenv("AGENT_ASYNC_PHASES", "code_writer")
        settings.LLM_ASYNC_EXECUTION = False
        assert g._async_execution_enabled("code_writer") is True
        assert g._async_execution_enabled("code_tester") is False
        assert g._async_execution_enabled("") is False

    def test_allowlist_parses_commas_and_normalizes_case(self, monkeypatch):
        monkeypatch.setenv("AGENT_ASYNC_PHASES", " Code_Writer , code_tester ,,")
        assert g._async_phase_allowlist() == {"code_writer", "code_tester"}

    def test_all_phases_override_wins(self, monkeypatch, settings):
        """LLM_ASYNC_EXECUTION is the operator rollback lever: it forces EVERY
        phase (even with an empty allowlist). Compose no longer passes it."""
        monkeypatch.delenv("AGENT_ASYNC_PHASES", raising=False)
        settings.LLM_ASYNC_EXECUTION = True
        assert g._async_execution_enabled("site_analyzer") is True
        assert g._async_execution_enabled("") is True


# ═══════════════════════════════════════════════════════════════════════════
# Trap 1 — the streaming lane must be constructor-set and carry the ladder
# ═══════════════════════════════════════════════════════════════════════════


class TestStreamingLane:
    def test_get_llm_constructor_sets_streaming(self):
        llm = llm_mod.get_llm(model="glm-5-turbo", temperature=0)
        assert "streaming" in llm.model_fields_set, (
            "streaming must be CONSTRUCTOR-set: langchain's _should_stream "
            "consults model_fields_set, so a class default would silently "
            "route the async lane to _agenerate (no chunks, no streaming "
            "retries) while still LOOKING streamed"
        )

    def test_class_default_streaming_is_not_fields_set(self):
        """The trap itself, pinned: constructing without the kwarg leaves
        ``streaming`` out of model_fields_set — this is exactly the shape the
        streaming stub must NOT have."""
        plain = llm_mod.ClassifiedRetryChatOpenAI(
            max_retries=0, model="m",
            openai_api_key="k", openai_api_base="http://x",
        )
        assert "streaming" not in plain.model_fields_set

    def test_astream_carries_the_classified_ladder_and_breaker(self):
        """R2 resolution pin: the async lane loses NO retry/fallback
        capability — ``_astream`` wraps the same classified retry + breaker
        bookkeeping as ``_stream``."""
        src = inspect.getsource(llm_mod.ClassifiedRetryChatOpenAI._astream)
        assert "_retry_classified_async" in src
        assert "_record_breaker" in src
        assert "record_success" in src


# ═══════════════════════════════════════════════════════════════════════════
# Trap 2 — the deadline must hold with a sync tool in flight
# ═══════════════════════════════════════════════════════════════════════════


class _ExecutorToolAgent:
    """Agent whose in-flight work is a sync sleep on the default executor —
    the ``run_in_executor`` shape of run_scraper / a sync pre_model_hook."""

    async def ainvoke(self, state, config=None):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, time.sleep, 8)
        return {"messages": ["done"]}


class TestWallClockIsHard:
    def test_tool_in_flight_does_not_extend_the_wall_clock(self, monkeypatch, settings):
        """timeout=1 with an 8s executor tool. Pre-fix (asyncio.run →
        shutdown_default_executor join) the wall was ~8s: the phase waited
        for the tool it had already cancelled. The loop close must skip the
        executor join so the deadline holds."""
        monkeypatch.setenv("AGENT_ASYNC_PHASES", "code_writer")
        settings.LLM_ASYNC_EXECUTION = False

        t0 = time.monotonic()
        result = g._invoke_agent_with_timeout(
            _ExecutorToolAgent(), [], {}, "code_writer", 0, timeout=1
        )
        wall = time.monotonic() - t0

        assert result.get("_error_class") == "WallClockTimeout"
        assert result.get("messages") == []
        assert wall < 5, f"async wall clock is soft: {wall:.1f}s (executor join)"

    def test_default_routing_is_sync_without_the_canary(self, monkeypatch, settings):
        """Empty allowlist → the sync thread path (daemon-thread abandon),
        even for a canary-sounding phase. This is the DEFAULT the canary must
        be flipped away from deliberately."""
        monkeypatch.delenv("AGENT_ASYNC_PHASES", raising=False)
        settings.LLM_ASYNC_EXECUTION = False

        started = threading.Event()

        class _SyncStuck:
            def invoke(self, *a, **k):
                started.set()
                threading.Event().wait(timeout=1.0)
                return {"messages": ["late"]}

        result = g._invoke_agent_with_timeout(
            _SyncStuck(), [], {}, "code_writer", 0, timeout=0
        )
        assert started.is_set()
        assert result.get("_error_class") == "WallClockTimeout"


# ═══════════════════════════════════════════════════════════════════════════
# Trap 3 — loop hygiene + the "Event loop is closed" transport class
# ═══════════════════════════════════════════════════════════════════════════


class _Ok:
    async def ainvoke(self, state, config=None):
        return {"messages": ["ok"]}


class TestLoopHygiene:
    def test_same_agent_across_two_sequential_loops(self, monkeypatch, settings):
        """The code_writer re-invoke pattern (syntax fix / CLI-contract fix
        call the invoke wrapper again): each invocation must get a fresh loop
        that closes cleanly, so the second one starts from a clean loop —
        not a half-shutdown one."""
        monkeypatch.setenv("AGENT_ASYNC_PHASES", "code_writer")
        settings.LLM_ASYNC_EXECUTION = False

        agent = _Ok()
        r1 = g._invoke_agent_with_timeout(agent, [], {}, "code_writer", 0, timeout=5)
        r2 = g._invoke_agent_with_timeout(agent, [], {}, "code_writer", 0, timeout=5)
        assert r1["messages"] == ["ok"]
        assert r2["messages"] == ["ok"]


class _RetryCfg:
    """Zero-backoff cfg so the ladder is fast and deterministic."""

    def __call__(self):
        return {
            "transient_max": 3, "ratelimit_max": 1,
            "backoff_base": 0.0, "backoff_cap": 0.0, "backoff_floor": 0.0,
            "ratelimit_base": 0.0,
        }


class TestLoopClosedClassification:
    cfg = _RetryCfg()

    def test_loop_closed_rides_the_transient_ladder_sync(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("Event loop is closed")
            return "ok"

        out = llm_mod._retry_classified_sync(fn, self.cfg(), sleep=lambda s: None)
        assert out == "ok"
        assert calls["n"] == 3

    def test_loop_closed_rides_the_transient_ladder_async(self):
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("event loop is closed")  # provider casing
            return "ok"

        async def _run():
            return await llm_mod._retry_classified_async(
                fn, self.cfg(), asleep=lambda d: asyncio.sleep(0)
            )

        assert asyncio.run(_run()) == "ok"
        assert calls["n"] == 3

    def test_unrelated_runtime_error_is_not_masked_sync(self):
        def fn():
            raise RuntimeError("cannot schedule new futures after shutdown")

        with pytest.raises(RuntimeError):
            llm_mod._retry_classified_sync(fn, self.cfg(), sleep=lambda s: None)

    def test_both_retry_arms_carry_the_loop_closed_class(self):
        for fn in (llm_mod._retry_classified_sync, llm_mod._retry_classified_async):
            assert "event loop is closed" in inspect.getsource(fn).lower(), fn.__name__
