"""[wave-30 W30-1] Writer-scoped invoke window + activity-aware extension.

Prod proof (job 569, 2026-09-12): the writer passed NO timeout → module default
AGENT_INVOKE_TIMEOUT (1800 in prod) ≈ 11-12 turns at the measured 145-161 s/turn
= exactly ONE draft cycle with zero slack. Both invocations died mid-progress
(a tool ran 56s / 34s before each death) and the consecutive-death counter
failed the job while real fixes were landing.

Contract under test:
1. ``_writer_invoke_timeout`` — env ``WRITER_INVOKE_TIMEOUT`` raises the
   writer's window above the module floor; unset → today's value exactly.
2. ``_join_with_activity_extension`` — the sync waiter polls instead of a
   single blocking join and EXTENDS while tool activity is fresh
   (``_ToolCallLogger.last_activity``), capped by ``WRITER_MAX_TIMEOUT`` and
   the task-scoped job deadline (finalize margin honored).
3. Stale/absent activity → the base deadline holds (a stalled writer still
   dies on time).
4. The extension is code_writer-only: other phases keep the bit-identical
   plain-join behavior even when an activity logger is present.
5. An extension-assisted death keeps the ``WallClockTimeout`` contract
   (``_error`` mentions "wall-clock timeout") so the node's consecutive-death
   counter still fires — otherwise W30-2's finisher could never arm.
6. A clamp refusal is a NAMED outcome: ``_error_class == "BudgetRefused"``
   (previously an anonymous empty-messages dict).

Django trap honored: no DB access inside running loops — assertions read the
in-memory return contract and fake clocks/threads, never SessionLog rows.
"""

from __future__ import annotations

import threading
import time
import types

from webapp.agents import graph as g


class FakeClock:
    """Controllable monotonic clock (seconds)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeThread:
    """join()-able stand-in whose lifetime the test scripts."""

    def __init__(self) -> None:
        self.alive = True
        self.joins: list[float] = []

    def join(self, timeout=None) -> None:  # noqa: ANN001
        self.joins.append(float(timeout or 0.0))

    def is_alive(self) -> bool:
        return self.alive


class ActivityLogger:
    """Duck-typed ``_ToolCallLogger`` with a stampable monotonic stamp."""

    def __init__(self, clock: FakeClock) -> None:
        self.last_activity = clock()


def _cfg_with(logger) -> dict:
    return {"callbacks": [logger, object()]}


# ═══════════════════════════════════════════════════════════════════════════
# 1 — the writer-scoped window
# ═══════════════════════════════════════════════════════════════════════════


class TestWriterWindow:
    def test_unset_env_is_exactly_the_module_floor(self, monkeypatch):
        monkeypatch.delenv("WRITER_INVOKE_TIMEOUT", raising=False)
        assert g._writer_invoke_timeout() == g._AGENT_INVOKE_TIMEOUT

    def test_env_raises_the_writer_window(self, monkeypatch):
        monkeypatch.setenv("WRITER_INVOKE_TIMEOUT", "2400")
        assert g._writer_invoke_timeout() == max(g._AGENT_INVOKE_TIMEOUT, 2400)

    def test_env_cannot_go_below_the_floor(self, monkeypatch):
        monkeypatch.setenv("WRITER_INVOKE_TIMEOUT", "10")
        assert g._writer_invoke_timeout() == g._AGENT_INVOKE_TIMEOUT

    def test_extension_cap_default_2700(self, monkeypatch):
        monkeypatch.delenv("WRITER_MAX_TIMEOUT", raising=False)
        assert g._writer_max_timeout() == 2700

    def test_extension_cap_takes_the_env_verbatim(self, monkeypatch):
        """The cap is a CEILING for tests/emergencies — no floor clamp here
        (an operator setting WRITER_MAX_TIMEOUT=2 in a test harness means 2)."""
        monkeypatch.setenv("WRITER_MAX_TIMEOUT", "2")
        assert g._writer_max_timeout() == 2

    def test_freshness_default_600(self, monkeypatch):
        monkeypatch.delenv("WRITER_ACTIVITY_FRESH_S", raising=False)
        assert g._writer_activity_fresh_s() == 600


# ═══════════════════════════════════════════════════════════════════════════
# 2/3 — the extension loop itself (pure, clock injected)
# ═══════════════════════════════════════════════════════════════════════════


class TestJoinWithExtension:
    def _run(self, *, base, fresh, cap, script, stamps, clock, poll_s=0.5):
        """Drive the helper with a scripted thread: ``script`` maps the number
        of join() calls already made → alive?  ``stamps`` maps a clock time →
        the activity logger's last stamp (recomputed on each check)."""
        thread = FakeThread()
        logger = ActivityLogger(clock)
        state = {"joins": 0}

        def alive():
            return script(state["joins"])

        def join(timeout=None):
            state["joins"] += 1
            thread.joins.append(float(timeout or 0.0))
            clock.advance(float(timeout or 0.0))

        thread.is_alive = alive  # type: ignore[method-assign]
        thread.join = join  # type: ignore[method-assign]

        def stamped_clock():
            current = None
            for threshold, value in stamps:
                if clock.t >= threshold:
                    current = value
                else:
                    break
            if current is not None:
                logger.last_activity = current
            return clock.t

        waited = g._join_with_activity_extension(
            thread,
            base_timeout=base,
            activity=logger,
            fresh_s=fresh,
            max_timeout=cap,
            clock=stamped_clock,
            poll_s=poll_s,
        )
        return waited, thread.joins

    def test_fresh_activity_extends_past_the_base_window(self):
        clock = FakeClock()
        # Thread stays alive through every join until killed explicitly.
        waited, joins = self._run(
            base=10,
            fresh=6,
            cap=16,
            script=lambda n: True,
            stamps=[(1000.0 + 3 * n + 1, 1000.0 + 3 * n + 1) for n in range(20)],
            clock=clock,
        )
        # Fresh stamps every ~3s keep extending; the cap (16) must bind.
        assert waited >= 16, f"died early at {waited:.1f}s despite fresh activity"
        assert len(joins) > 2, "never polled — a single blocking join"

    def test_stale_activity_does_not_reach_the_cap(self):
        """One stamp at start, then silence: the stamp may legitimately buy up
        to one fresh-window tail, but the run must stay near the base window —
        nowhere near the cap."""
        clock = FakeClock()
        waited, _ = self._run(
            base=10,
            fresh=6,
            cap=16,
            script=lambda n: True,
            stamps=[(1000.0, 1000.0)],
            clock=clock,
        )
        assert waited <= 10 + 6 + 1, f"stale activity ran to the cap: {waited:.1f}s"

    def test_never_stamped_logger_holds_the_base_window(self):
        clock = FakeClock()
        thread = FakeThread()
        thread.is_alive = lambda: True  # type: ignore[method-assign]

        def join(timeout=None):
            clock.advance(float(timeout or 0.0))

        thread.join = join  # type: ignore[method-assign]
        logger = ActivityLogger(clock)
        logger.last_activity = None  # production: None until the FIRST tool fires
        clock.advance(0)  # no tool ever fires
        waited = g._join_with_activity_extension(
            thread,
            base_timeout=10,
            activity=logger,
            fresh_s=6,
            max_timeout=16,
            clock=clock,
        )
        assert 9.9 <= waited <= 10.1

    def test_cap_binds_even_with_eternal_fresh_activity(self):
        clock = FakeClock()
        waited, _ = self._run(
            base=10,
            fresh=1,
            cap=14,
            script=lambda n: True,
            stamps=[(1000.0 + 0.5 * n, 1000.0 + 0.5 * n) for n in range(100)],
            clock=clock,
        )
        assert waited <= 14.5, f"cap not honored: {waited:.1f}s"

    def test_healthy_thread_returning_early_just_returns(self):
        clock = FakeClock()
        thread = FakeThread()

        def join(timeout=None):
            clock.advance(float(timeout or 0.0))
            thread.alive = False  # finished inside the first window

        thread.join = join  # type: ignore[method-assign]
        logger = ActivityLogger(clock)
        waited = g._join_with_activity_extension(
            thread,
            base_timeout=10,
            activity=logger,
            fresh_s=6,
            max_timeout=16,
            clock=clock,
            poll_s=0.5,
        )
        # Detection is bounded by the poll interval, not the base window.
        assert waited <= 0.5
        assert not thread.alive


# ═══════════════════════════════════════════════════════════════════════════
# 4/5 — integration: the gate is code_writer-only; the death contract holds
# ═══════════════════════════════════════════════════════════════════════════


class _SlowAgent:
    def __init__(self, seconds: float) -> None:
        self._s = seconds

    def invoke(self, *a, **k):  # noqa: ANN002, ANN003
        time.sleep(self._s)
        return {"messages": ["late"]}


class TestExtensionGate:
    def test_writer_phase_with_fresh_activity_extends(self, monkeypatch):
        """A code_writer agent that keeps stamping tool activity runs past the
        base window but is stopped by the cap."""
        monkeypatch.setenv("WRITER_ACTIVITY_FRESH_S", "1")
        monkeypatch.setenv("WRITER_MAX_TIMEOUT", "2")

        logger = ActivityLogger(FakeClock())
        stop = threading.Event()

        class _StampingAgent:
            def invoke(self, *a, **k):  # noqa: ANN002, ANN003
                while not stop.is_set():
                    logger.last_activity = time.monotonic()
                    time.sleep(0.1)
                return {"messages": ["late"]}

        try:
            t0 = time.monotonic()
            result = g._invoke_agent_with_timeout(
                _StampingAgent(), [], _cfg_with(logger), "code_writer", 0, timeout=1
            )
            wall = time.monotonic() - t0
        finally:
            stop.set()
        assert result.get("_error_class") == "WallClockTimeout"
        assert wall >= 2, f"fresh activity did not extend: {wall:.2f}s"
        assert wall < 6, f"cap not honored: {wall:.2f}s"

    def test_writer_phase_with_stale_activity_dies_on_time(self, monkeypatch):
        monkeypatch.setenv("WRITER_ACTIVITY_FRESH_S", "600")
        logger = ActivityLogger(FakeClock())  # never re-stamped
        t0 = time.monotonic()
        result = g._invoke_agent_with_timeout(
            _SlowAgent(6.0), [], _cfg_with(logger), "code_writer", 0, timeout=1
        )
        wall = time.monotonic() - t0
        assert result.get("_error_class") == "WallClockTimeout"
        assert "wall-clock timeout" in str(result.get("_error") or "")
        assert wall < 4, f"stale activity extended the window: {wall:.2f}s"

    def test_other_phases_never_extend_even_with_activity(self, monkeypatch):
        monkeypatch.setenv("WRITER_ACTIVITY_FRESH_S", "1")
        monkeypatch.setenv("WRITER_MAX_TIMEOUT", "30")
        logger = ActivityLogger(FakeClock())
        stop = threading.Event()

        class _StampingAgent:
            def invoke(self, *a, **k):  # noqa: ANN002, ANN003
                while not stop.is_set():
                    logger.last_activity = time.monotonic()
                    time.sleep(0.05)
                return {"messages": ["late"]}

        try:
            t0 = time.monotonic()
            result = g._invoke_agent_with_timeout(
                _StampingAgent(), [], _cfg_with(logger), "site_analyzer", 0, timeout=1
            )
            wall = time.monotonic() - t0
        finally:
            stop.set()
        assert result.get("_error_class") == "WallClockTimeout"
        assert wall < 4, f"non-writer phase extended: {wall:.2f}s"

    def test_no_activity_logger_plain_join(self, monkeypatch):
        """config={} (legacy callers/tests) → the base behavior, bit-identical."""
        t0 = time.monotonic()
        result = g._invoke_agent_with_timeout(
            _SlowAgent(6.0), [], {}, "code_writer", 0, timeout=1
        )
        wall = time.monotonic() - t0
        assert result.get("_error_class") == "WallClockTimeout"
        assert wall < 4


# ═══════════════════════════════════════════════════════════════════════════
# 6 — the named clamp refusal
# ═══════════════════════════════════════════════════════════════════════════


class TestBudgetRefusal:
    def test_clamp_refusal_is_named_budgetrefused(self):
        result = g._invoke_agent_with_timeout(
            _SlowAgent(0.1),
            [],
            {"configurable": {"task_deadline": time.time() - 10}},
            "code_writer",
            0,
            timeout=1800,
        )
        assert result.get("messages") == []
        assert result.get("_error_class") == "BudgetRefused"
        assert "budget" in str(result.get("_error") or "")

    def test_fast_fail_detector_ignores_budgetrefused(self):
        """The A5/WallClock fast-fail vocabulary must NOT claim a refusal —
        refusal means 'never ran', wall-clock means 'ran out mid-work'."""
        assert g._fast_fail_detail("code_writer", "scraper_draft.py", {}, output_exists=False) == ""


# ── helpers the tests imply ────────────────────────────────────────────────


def test_find_activity_logger_extracts_from_handler_list():
    logger = ActivityLogger(FakeClock())
    assert g._find_activity_logger(_cfg_with(logger)) is logger


def test_find_activity_logger_tolerates_manager_shape_and_absence():
    manager = types.SimpleNamespace(handlers=[ActivityLogger(FakeClock())])
    assert g._find_activity_logger({"callbacks": manager}) is manager.handlers[0]
    assert g._find_activity_logger({}) is None
    assert g._find_activity_logger({"callbacks": [object()]}) is None


def test_tool_call_logger_stamps_activity():
    logger = g._ToolCallLogger(1, "code-writer")
    assert logger.last_activity is None  # construction is not activity
    logger.on_tool_start({"name": "read_file"}, "{}")
    assert logger.last_activity is not None and logger.last_activity <= time.monotonic()
