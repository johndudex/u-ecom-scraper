"""[wave-30 W30-2] Draft-finisher invocation before an honest fail.

Prod proof (job 569, revolveclothing.com.au): the second 1800s wall-clock
death landed 34s after the writer's LAST accepted edit — 9/9 remediation
edits were already on disk, ~300-600s short of self-validation. The
consecutive-death arm (``writer_wall_clock_timeouts >= 2``) then failed the
job with that salvageable draft still untested.

Contract:
1. At the ``_wc >= 2`` arm, ``skip_approvals`` branch ONLY: fire ONE bounded
   FINISH invocation (``WRITER_FINISH_TIMEOUT``, default 1800, fixed window —
   no activity extension; the job is already deep in budget), guarded by a
   once-per-job flag (``writer_finisher_attempted``).
2. Seed = the CURRENT on-disk draft as template_code + refreshed strategy and
   ``strategies_tried`` + tester feedback, framed as FINISHING (land the
   remaining edits, then check_syntax + one sample run) — job 569's inv-2 was
   editing an old-strategy draft because strategy switches never rotate the
   draft file.
3. Outcome contract — salvage (healthy + draft still parses) returns the
   result and the arm falls through to the normal usable-draft path with the
   death counter reset; wall-clock death, BudgetRefused, or an unparseable
   post-finisher draft each return None → the caller's honest cleanup, no
   counter increment, no crash, no loop.
4. The non-skip arm (human_approval) is untouched — no finisher before a
   human decision.
5. The finisher never raises: an exception is a None (honest cleanup).

Django trap honored: SessionLog/ToolCallLog writes are patched out — the
behavioral tests read in-memory return contracts only.
"""
from __future__ import annotations

import importlib
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

graph = importlib.import_module("agents.graph")  # noqa: E402

SLUG = "revolveclothing-com-au"
PARSEABLE = "X = 1\n"


class FakeAgent:
    def __init__(self, result=None, side_effect=None):
        self.result = result or {"messages": [types.SimpleNamespace(content="done")]}
        self.side_effect = side_effect
        self.invocations = []

    def invoke(self, messages, *a, **k):
        self.invocations.append(messages)
        if self.side_effect is not None:
            self.side_effect()
        return self.result


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Workspace with a parseable draft + hermetic graph side effects."""
    ws = tmp_path / "workspace" / SLUG
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text(PARSEABLE)
    monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(graph, "_start_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_log_event_row", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_persist_agent_logs", lambda *a, **k: None)
    import src.artifacts as art

    snaps = []
    monkeypatch.setattr(art, "write", lambda key, data: snaps.append(key))
    return {"root": tmp_path, "snaps": snaps}


def _state(**over):
    base = {
        "job_id": 569,
        "site_slug": SLUG,
        "scraper_analysis": {
            "scraping_method": "http_navigation",
            "strategies_tried": ["http_requests", "http_navigation"],
        },
        "test_report": {"feedback_for_writer": "phase-1 discovery 403"},
    }
    base.update(over)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# 1 — the finisher invocation itself
# ═══════════════════════════════════════════════════════════════════════════


class TestFinisherInvocation:
    def test_salvage_seeds_the_current_draft_and_returns_healthy(self, env, monkeypatch):
        seen = {}

        def fake_factory(*a, **k):
            seen["template_code"] = k.get("template_code") or (
                a[1] if len(a) > 1 else ""
            )
            return FakeAgent()

        monkeypatch.setattr(graph, "create_code_writer", fake_factory)
        out = graph._run_draft_finisher(_state(), {}, SLUG, 569)
        assert out is not None and out.get("messages"), "salvage must return the result"
        assert (seen["template_code"] or "").strip() == PARSEABLE.strip(), (
            "finisher must seed the CURRENT on-disk draft, not a template"
        )

    def test_seed_carries_strategy_tried_and_finish_framing(self, env, monkeypatch):
        msgs = {}

        class RecordingAgent(FakeAgent):
            def invoke(self, messages, *a, **k):
                msgs["content"] = str(messages)
                return super().invoke(messages, *a, **k)

        monkeypatch.setattr(
            graph, "create_code_writer", lambda *a, **k: RecordingAgent()
        )
        graph._run_draft_finisher(_state(), {}, SLUG, 569)
        text = msgs["content"]
        assert "http_navigation" in text, "current strategy missing from seed"
        assert "http_requests" in text, "strategies_tried missing from seed"
        assert "FINISH" in text.upper(), "seed must frame the ask as finishing"
        assert "403" in text, "tester feedback missing from seed"
        assert "adapt" in text.lower(), (
            "seed must order the writer to adapt to the (possibly switched) "
            "current strategy or justify keeping the old one (569: inv-2 "
            "edited an old-strategy draft)"
        )

    def test_fixed_window_no_activity_extension(self, env, monkeypatch):
        """The finisher is a FIXED window (env WRITER_FINISH_TIMEOUT, default
        1800) with the activity extension explicitly OFF — the job is already
        deep in budget; a fresh stamp must not buy more time."""
        seen = {}

        def fake_invoke(agent, messages, cfg, phase, job_id, timeout=None,
                        allow_activity_extension=True):
            seen["timeout"] = timeout
            seen["ext"] = allow_activity_extension
            seen["phase"] = phase
            return {"messages": [types.SimpleNamespace(content="ok")]}

        monkeypatch.setattr(graph, "create_code_writer", lambda *a, **k: FakeAgent())
        monkeypatch.setattr(graph, "_invoke_agent_with_timeout", fake_invoke)
        graph._run_draft_finisher(_state(), {}, SLUG, 569)
        assert seen["timeout"] == 1800
        assert seen["ext"] is False, "finisher must not extend on activity"
        assert seen["phase"] == "code_writer"

    def test_finish_timeout_env_override(self, env, monkeypatch):
        monkeypatch.setenv("WRITER_FINISH_TIMEOUT", "900")
        seen = {}

        def fake_invoke(agent, messages, cfg, phase, job_id, timeout=None,
                        allow_activity_extension=True):
            seen["timeout"] = timeout
            return {"messages": ["ok"]}

        monkeypatch.setattr(graph, "create_code_writer", lambda *a, **k: FakeAgent())
        monkeypatch.setattr(graph, "_invoke_agent_with_timeout", fake_invoke)
        graph._run_draft_finisher(_state(), {}, SLUG, 569)
        assert seen["timeout"] == 900


# ═══════════════════════════════════════════════════════════════════════════
# 2 — the outcome contract
# ═══════════════════════════════════════════════════════════════════════════


class TestFinisherOutcomes:
    @pytest.mark.parametrize("dead_result", [
        {"_error": "wall-clock timeout after 1800s", "messages": []},
        {"_error": "job budget exhausted (task deadline clamped to 0.0s)",
         "_error_class": "BudgetRefused", "messages": []},
        {"messages": []},
    ], ids=["wall-clock-death", "budget-refused", "empty-return"])
    def test_incomplete_invocation_returns_none(self, env, monkeypatch, dead_result):
        monkeypatch.setattr(graph, "create_code_writer", lambda *a, **k: FakeAgent())
        monkeypatch.setattr(
            graph, "_invoke_agent_with_timeout", lambda *a, **k: dead_result
        )
        assert graph._run_draft_finisher(_state(), {}, SLUG, 569) is None

    def test_unparseable_draft_never_invokes(self, env, tmp_path, monkeypatch):
        (tmp_path / "workspace" / SLUG / "scraper_draft.py").write_text(
            "def broken(:\n"
        )
        fired = []

        def boom(*a, **k):
            fired.append(1)
            raise AssertionError("LLM must not be invoked")

        monkeypatch.setattr(graph, "create_code_writer", boom)
        assert graph._run_draft_finisher(_state(), {}, SLUG, 569) is None
        assert not fired

    def test_unparseable_after_finisher_returns_none(self, env, tmp_path, monkeypatch):
        def wreck():
            (tmp_path / "workspace" / SLUG / "scraper_draft.py").write_text(
                "def broken(:\n"
            )

        agent = FakeAgent(side_effect=wreck)
        monkeypatch.setattr(graph, "create_code_writer", lambda *a, **k: agent)
        assert graph._run_draft_finisher(_state(), {}, SLUG, 569) is None

    def test_factory_exception_never_raises(self, env, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr(graph, "create_code_writer", boom)
        assert graph._run_draft_finisher(_state(), {}, SLUG, 569) is None

    def test_salvage_snapshots_draft_to_the_job_fm_key(self, env, monkeypatch):
        monkeypatch.setattr(graph, "create_code_writer", lambda *a, **k: FakeAgent())
        out = graph._run_draft_finisher(_state(), {}, SLUG, 569)
        assert out is not None
        assert any("scraper-draft-569" in str(k) for k in env["snaps"]), (
            "salvaged draft must be FM-snapshotted like every other completed "
            "draft (watchdog re-drive resumes from it)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3 — the opt-out is real: allow_activity_extension=False holds the base window
# ═══════════════════════════════════════════════════════════════════════════


class TestExtensionOptOut:
    def test_opt_out_holds_the_base_window(self, monkeypatch):
        import threading
        import time as _time

        logger = types.SimpleNamespace(last_activity=None)
        stop = threading.Event()

        class StampingAgent:
            def invoke(self, *a, **k):
                while not stop.is_set():
                    logger.last_activity = _time.monotonic()
                    _time.sleep(0.05)
                return {"messages": ["late"]}

        cfg = {"callbacks": [logger]}
        try:
            t0 = _time.monotonic()
            result = graph._invoke_agent_with_timeout(
                StampingAgent(), [], cfg, "code_writer", 0,
                timeout=1, allow_activity_extension=False,
            )
            wall = _time.monotonic() - t0
        finally:
            stop.set()
        assert result.get("_error_class") == "WallClockTimeout"
        assert wall < 4, f"opted-out finisher still extended: {wall:.2f}s"


# ═══════════════════════════════════════════════════════════════════════════
# 4 — arm wiring in _invoke_code_writer (wave-22 source-inspection idiom)
# ═══════════════════════════════════════════════════════════════════════════


class TestArmWiring:
    def _writer_src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def _arm(self) -> str:
        src = self._writer_src()
        i = src.index("if _wc >= 2:")
        j = src.index("if not _cw_dead:", i)
        return src[i:j]

    def test_arm_fires_the_finisher_once_per_job(self):
        arm = self._arm()
        assert "_run_draft_finisher(" in arm, (
            "the _wc >= 2 skip_approvals arm fails the job without attempting "
            "one bounded finish on the parseable draft (569 died 34s after "
            "its last accepted edit)"
        )
        assert "writer_finisher_attempted" in arm, (
            "the finisher is not once-per-job — a second arrival would loop"
        )

    def test_finisher_guarded_before_the_cleanup_return(self):
        arm = self._arm()
        i_flag = arm.index("writer_finisher_attempted")
        i_fin = arm.index("_run_draft_finisher(")
        # The once-per-job early-exit cleanup legitimately precedes the
        # finisher; the invariant is flag-guard → finisher → and the LAST
        # cleanup (the refused/died honest fail) only after both.
        assert i_flag < i_fin < arm.rindex('goto="cleanup"'), (
            "finisher is not guarded by the once-per-job flag before the "
            "honest-fail cleanup — order is wrong"
        )

    def test_salvage_falls_through_with_counter_reset(self):
        arm = self._arm()
        i_fin = arm.index("_run_draft_finisher(")
        tail = arm[i_fin:]
        assert 'update["writer_wall_clock_timeouts"] = 0' in tail, (
            "salvage path must reset the consecutive-death counter so the "
            "post-salvage ladder starts clean"
        )
        # the salvage branch must NOT return cleanup: the only cleanup Command
        # after the finisher call belongs to the refused/dead outcome.
        after_guard = tail.split("else:", 1)[-1]
        assert 'goto="cleanup"' in after_guard or 'goto="human_approval"' in arm

    def test_finisher_appears_exactly_once_in_the_node(self):
        src = self._writer_src()
        i_node = src.index("def _invoke_code_writer")
        j = src.index("\ndef ", i_node + 10)
        assert src[i_node:j].count("_run_draft_finisher(") == 1, (
            "the finisher must exist ONLY in the skip_approvals _wc >= 2 arm — "
            "the human_approval path must not burn pre-interrupt LLM spend"
        )

    def test_state_declares_the_once_per_job_flag(self):
        state_mod = importlib.import_module("agents.state")
        assert "writer_finisher_attempted" in state_mod.ScrapeState.__annotations__


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
