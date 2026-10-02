"""Wave-52 candidate (8) — the in-job writer window gate.

Four consecutive writer deaths (521 jjshouse, 524 citychic, 525 jayjays,
526 hobbylobby) share one shape: the fire-time TTFB gate was GREEN, the
window opened MID-JOB, and both stretched writer invocations (4 classified
retries each, ~12-18 min exposure) died inside a 10-30 min Z.AI window.
The retry stretch rides out ~60s middlebox resets; it cannot ride out a
window that stays closed longer than the stretch's total exposure.

Fix: gate EVERY writer invocation (the node re-enters via the
scraper_analyzer bounce) on a WRITER-SHAPED canary — small calls sail
through windows (proven live: cleanup's 1.8-5.5K-char calls 200-OK'd
seconds after writer calls died, and the 521 fire-time gate was green
while in-app calls died), so the canary must reproduce the writer's big
diverse prefill and measure real TTFB. Red → sleep WRITER_WINDOW_GATE
_SLEEP (default 300s) and re-canary, up to WRITER_WINDOW_GATE_MAX_CYCLES
(default 3); still red → proceed anyway (bounded — the gate must never
hang the worker forever) and let the stretch do what it can.
"""

import ast
import os
from unittest import mock

import pytest


def _gate(canary, sleep=mock.MagicMock(), env=None):
    import webapp.agents.graph as g

    real_env = {
        k: os.environ.get(k) for k in (
            "WRITER_WINDOW_GATE", "WRITER_WINDOW_GATE_MAX_CYCLES",
            "WRITER_WINDOW_GATE_SLEEP", "WRITER_WINDOW_GATE_TTFB_MAX",
        )
    }
    try:
        for k, v in (env or {}).items():
            os.environ[k] = v
        for k, v in (env or {}).items():
            if v is None:
                os.environ.pop(k, None)
        with mock.patch.object(g, "_writer_canary_ttfb", canary), \
                mock.patch.object(g.time, "sleep", sleep):
            return g._writer_window_gate(999)
    finally:
        for k, v in real_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestWriterWindowGate:
    def test_green_gate_proceeds_without_sleeping(self):
        calls = []

        def canary(payload):
            calls.append(1)
            return 5.6

        assert _gate(canary) is True
        assert len(calls) == 1

    def test_red_then_green_sleeps_once(self):
        seq = [ConnectionError("reset by peer"), 8.0]

        def canary(payload):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        sleep = mock.MagicMock()
        assert _gate(canary, sleep=sleep) is True
        assert sleep.call_count == 1

    def test_red_through_all_cycles_proceeds_bounded(self):
        def canary(payload):
            raise ConnectionError("still closed")

        sleep = mock.MagicMock()
        assert _gate(canary, sleep=sleep) is True
        assert sleep.call_count == 2  # 3 canaries, 2 inter-cycle waits

    def test_slow_ttfb_counts_as_red(self):
        seq = [70.0, 6.0]

        def canary(payload):
            return seq.pop(0)

        sleep = mock.MagicMock()
        assert _gate(canary, sleep=sleep) is True
        assert sleep.call_count == 1

    def test_disabled_gate_is_invisible(self):
        def canary(payload):
            raise AssertionError("canary must not run when the gate is off")

        assert _gate(canary, env={"WRITER_WINDOW_GATE": "0"}) is True

    def test_max_cycles_env_knob(self):
        def canary(payload):
            raise ConnectionError("still closed")

        sleep = mock.MagicMock()
        _gate(canary, sleep=sleep, env={"WRITER_WINDOW_GATE_MAX_CYCLES": "5"})
        assert sleep.call_count == 4

    def test_sleep_env_knob_value(self):
        def canary(payload):
            raise ConnectionError("x")

        sleep = mock.MagicMock()
        _gate(canary, sleep=sleep, env={"WRITER_WINDOW_GATE_SLEEP": "120"})
        assert sleep.call_args_list == [mock.call(120), mock.call(120)]

    def test_payload_is_writer_shaped(self):
        from webapp.agents.graph import _writer_canary_payload

        payload = _writer_canary_payload()
        assert payload["stream"] is True
        assert payload["max_tokens"] <= 16
        assert len(payload["tools"]) >= 5
        system = payload["messages"][0]["content"]
        assert len(system) > 40000  # writer-sized prefill, not a ping


class TestGateIsWired:
    def test_invoke_code_writer_gates_before_the_invocation(self):
        """Source contract: the gate call sits in _invoke_code_writer before
        _invoke_agent_with_timeout — every writer entry (including the
        scraper_analyzer bounce re-entry) waits out a live window."""
        import inspect

        import webapp.agents.graph as g

        src = inspect.getsource(g._invoke_code_writer)
        assert "_writer_window_gate(job_id)" in src
        gate_at = src.index("_writer_window_gate(job_id)")
        invoke_at = src.index("_invoke_agent_with_timeout(")
        assert gate_at < invoke_at

    def test_canary_uses_the_zai_endpoint(self):
        """Source contract: the canary hits the same provider base the
        writer's raw endpoint uses (ZAI_BASE_URL), not a different door."""
        import inspect

        import webapp.agents.graph as g

        src = inspect.getsource(g._writer_canary_ttfb)
        assert "ZAI_BASE_URL" in src
