"""Wave-23 W23-1: per-LLM-call no-chunk watchdog.

RC-1 of docs/plans/wave23-writer-convergence-plan.md: a streaming LLM call
whose server sends keepalive bytes but no chunks never trips httpx's read
timeout, so the call hangs invisibly until the PHASE wall-clock reaper kills
it (prod 343 attempt 2: 1800s, zero model output; 377: two consecutive 1800s
writer phases → job dead). The breaker can't see the hang either — it only
counts calls that RAISE.

Fix: bound the NO-CHUNK INTERVAL. Sync `_stream` consumes in a worker thread
and abandons the attempt when no new chunk arrives within
LLM_NO_CHUNK_TIMEOUT, raising a transient-class error the existing classified
retry ladder (and, exhausted, the breaker → fallback swap) already handles.
Async `_astream` gets the identical bound via per-chunk ``asyncio.wait_for``.

No network: the parent ``ChatOpenAI._stream``/``._astream`` is patched.

Run from repo root:  python3 -m pytest tests/test_llm_no_chunk_watchdog.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest.mock as mock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))


class _FakeSettings:
    def __init__(self, **overrides):
        self.ZAI_BASE_URL = "https://zai.example/v4"
        self.ZAI_API_KEY = "zai-key"
        self.LLM_CLASSIFIED_RETRY = True
        self.LLM_REQUEST_TIMEOUT = 300
        for k, v in overrides.items():
            setattr(self, k, v)


def _llm_mod():
    import importlib

    import agents.llm as llm

    importlib.reload(llm)
    return llm


def _silent_generator():
    """Blocks forever on first next() — the keepalive-fed hang, distilled."""
    import threading

    blocker = threading.Event()

    def gen():
        blocker.wait()  # never set — hangs without burning CPU
        yield "never"  # pragma: no cover

    return gen()


class TestNoChunkDeadlineHelper:
    def test_silent_stream_aborts_with_transient_error(self):
        mod = _llm_mod()
        t0 = time.monotonic()
        with pytest.raises(mod.LLMNoChunkTimeout):
            mod._consume_with_no_chunk_deadline(
                _silent_generator(), timeout=0.3, poll=0.05
            )
        assert time.monotonic() - t0 < 5, "abort must be near the deadline, not the read"

    def test_stalling_stream_aborts_after_last_chunk(self):
        mod = _llm_mod()

        def gen():
            for i in range(3):
                yield i
                time.sleep(0.01)
            import threading

            threading.Event().wait()  # stall forever after real progress

        with pytest.raises(mod.LLMNoChunkTimeout):
            mod._consume_with_no_chunk_deadline(gen(), timeout=0.3, poll=0.05)

    def test_healthy_stream_passes_through_all_chunks_in_order(self):
        mod = _llm_mod()

        def gen():
            for i in range(100):
                yield i
                time.sleep(0.001)

        out = mod._consume_with_no_chunk_deadline(gen(), timeout=5.0, poll=0.05)
        assert out == list(range(100))

    def test_worker_error_propagates_unchanged(self):
        mod = _llm_mod()

        def gen():
            yield "first"
            raise ValueError("mid-stream transport death")

        with pytest.raises(ValueError):
            mod._consume_with_no_chunk_deadline(gen(), timeout=5.0, poll=0.05)

    def test_zero_timeout_disables_guard(self):
        mod = _llm_mod()

        def gen():
            yield "a"
            yield "b"

        # timeout <= 0 → plain buffered consumption, no watchdog thread.
        out = mod._consume_with_no_chunk_deadline(gen(), timeout=0, poll=0.05)
        assert out == ["a", "b"]

    def test_error_is_transient_classified(self):
        mod = _llm_mod()
        err = mod.LLMNoChunkTimeout("no chunk for 240s")
        assert isinstance(err, mod._TRANSIENT_ERRORS), (
            "must ride the existing classified-retry ladder without classification changes"
        )

    def test_default_timeout_is_240s(self):
        mod = _llm_mod()
        with mock.patch.object(mod, "settings", _FakeSettings()):
            assert mod._no_chunk_timeout() == 240.0

    def test_settings_override_honored(self):
        mod = _llm_mod()
        with mock.patch.object(mod, "settings", _FakeSettings(LLM_NO_CHUNK_TIMEOUT="90")):
            assert mod._no_chunk_timeout() == 90.0


class TestStreamOverrideWiring:
    """The `_stream` override must bound each attempt's no-chunk interval and,
    on exhaustion, feed the breaker — turning the invisible hang into a
    counted failure that swaps to the fallback model on the next get_llm."""

    def _make(self, mod, model="litellm/standardcompute"):
        llm = mod.ClassifiedRetryChatOpenAI(
            model_name=model,
            openai_api_base="https://litellm.example/v1",
            openai_api_key="k",
            max_retries=0,
        )
        llm._breaker_name = model
        return llm

    def test_hung_stream_raises_no_chunk_and_records_breaker(self):
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)
        with mock.patch.object(ChatOpenAI, "_stream", lambda self, *a, **k: _silent_generator()), \
             mock.patch.object(mod, "settings", _FakeSettings(LLM_NO_CHUNK_TIMEOUT=0.3)), \
             mock.patch.object(mod, "_retry_settings", return_value={
                 "transient_max": 0, "ratelimit_max": 0,
                 "backoff_base": 0.0, "backoff_cap": 0.0,
                 "backoff_floor": 0.0}), \
             mock.patch.object(mod, "record_failure") as rf:
            with pytest.raises(mod.LLMNoChunkTimeout):
                list(llm._stream([("user", "hi")]))
            rf.assert_called_once_with("litellm/standardcompute")

    def test_healthy_stream_yields_chunks_and_records_success(self):
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)

        def gen():
            yield from range(5)

        with mock.patch.object(ChatOpenAI, "_stream", lambda self, *a, **k: gen()), \
             mock.patch.object(mod, "settings", _FakeSettings(LLM_NO_CHUNK_TIMEOUT=5.0)), \
             mock.patch.object(mod, "record_success") as rs:
            assert list(llm._stream([("user", "hi")])) == [0, 1, 2, 3, 4]
            rs.assert_called_once_with("litellm/standardcompute")


class TestAstreamOverrideWiring:
    def _make(self, mod, model="litellm/standardcompute"):
        llm = mod.ClassifiedRetryChatOpenAI(
            model_name=model,
            openai_api_base="https://litellm.example/v1",
            openai_api_key="k",
            max_retries=0,
        )
        llm._breaker_name = model
        return llm

    def test_hung_astream_raises_no_chunk(self):
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)

        async def silent():
            await asyncio.Event().wait()  # never yields
            yield "never"  # pragma: no cover

        def fake_astream(self, *a, **k):
            return silent()

        async def run():
            return [c async for c in llm._astream([("user", "hi")])]

        with mock.patch.object(ChatOpenAI, "_astream", fake_astream), \
             mock.patch.object(mod, "settings", _FakeSettings(LLM_NO_CHUNK_TIMEOUT=0.3)), \
             mock.patch.object(mod, "_retry_settings", return_value={
                 "transient_max": 0, "ratelimit_max": 0,
                 "backoff_base": 0.0, "backoff_cap": 0.0,
                 "backoff_floor": 0.0}):
            with pytest.raises(mod.LLMNoChunkTimeout):
                asyncio.run(run())

    def test_healthy_astream_passes_through(self):
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)

        async def healthy():
            for i in range(5):
                yield i

        def fake_astream(self, *a, **k):
            return healthy()

        async def run():
            return [c async for c in llm._astream([("user", "hi")])]

        with mock.patch.object(ChatOpenAI, "_astream", fake_astream), \
             mock.patch.object(mod, "settings", _FakeSettings(LLM_NO_CHUNK_TIMEOUT=5.0)):
            assert asyncio.run(run()) == [0, 1, 2, 3, 4]


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
