"""[wave-59 C4] Strike-ledger truth + the no-draft REPAIR arm.

Three truths pinned here:

1. ``writer_step_budget_deaths`` is a DECLARED channel. It was stamped at the
   death site since wave-25 but never declared in ScrapeState — langgraph
   strips undeclared keys — so every read saw 0 and step-budget deaths never
   reached the strike arithmetic. Declared, it joins the F7 ceiling's summed
   strike total (a window spent to langgraph's step ceiling burns exactly
   like a hang).

2. The no-draft strike-2 arm (skip_approvals) gained a bounded REPAIR window
   before the honest cleanup: when the death ledgers show a window death AND
   an unparsable-but-substantial (≥200-line) draft is on disk — 623's exact
   shape: ~1,045 correct lines, one bad regex line, two 2700s windows died
   "no draft" — ONE finisher window runs with ``require_parses=False`` and
   the death's SyntaxError in the seed. Provider-dead still parks
   unconditionally ABOVE the arm (the finisher runs no provider gate); the
   once-per-job latch is shared with the draft-exists arm; salvage falls
   through to the usable-draft tail with clean ledgers; refusal → honest
   cleanup. NO second ledger increment (the stamps above stand for
   telemetry; only the salvage clears them).

3. Two consecutive ALIVE no-ops (no window death on the ledgers) still go
   straight to cleanup — nothing on disk worth repairing.
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

SLUG = "jjshouse-com"

# 623's shape: ~1,045 lines of correct-shape scraper with ONE bad line.
SUBSTANTIAL_UNPARSABLE = (
    "import re\n\n"
    + "".join(f"def _pad_{i}():\n    return {i}\n\n" for i in range(340))
    + 'CATEGORY_RE = re.compile(r"/collections/.+-g\\d+, re.I)\n'
)
assert SUBSTANTIAL_UNPARSABLE.count("\n") >= 200  # 623-shaped: ~1,023 lines
TINY_UNPARSABLE = "def broken(:\n    pass\n"


class FakeAgent:
    def __init__(self):
        self.invocations = []

    def invoke(self, messages, *a, **k):
        self.invocations.append(messages)
        return {"messages": [types.SimpleNamespace(content="done")]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Workspace with the 623-shaped draft + hermetic graph side effects."""
    ws = tmp_path / "workspace" / SLUG
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text(SUBSTANTIAL_UNPARSABLE)
    monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(graph, "_start_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_log_event_row", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_persist_agent_logs", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_log_writer_embed_row", lambda *a, **k: None)
    import src.artifacts as art

    monkeypatch.setattr(art, "write", lambda key, data: None)
    return {"root": tmp_path}


def _state(**over):
    base = {
        "job_id": 623,
        "site_slug": SLUG,
        "scraper_analysis": {
            "scraping_method": "http_navigation",
            "strategies_tried": ["http_requests", "http_navigation"],
        },
        "test_report": {"feedback_for_writer": ""},
    }
    base.update(over)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# 1 — the step-budget ledger is real state and counts toward strikes
# ═══════════════════════════════════════════════════════════════════════════


class TestStepBudgetLedger:
    def test_state_declares_the_step_budget_ledger(self):
        state_mod = importlib.import_module("agents.state")
        assert "writer_step_budget_deaths" in (
            state_mod.ScrapeState.__annotations__
        ), "undeclared keys are stripped by langgraph — the counter would read 0 forever"

    def test_ceiling_degrades_after_two_step_deaths(self, monkeypatch):
        monkeypatch.setenv("WRITER_MAX_TIMEOUT", "2700")
        monkeypatch.setenv("WRITER_INVOKE_TIMEOUT", "700")
        healthy = graph._writer_extension_ceiling(_state())
        assert healthy == 2700
        one = graph._writer_extension_ceiling(
            _state(writer_step_budget_deaths=1)
        )
        assert one == 2700, "one strike must not degrade the ceiling"
        two = graph._writer_extension_ceiling(
            _state(writer_step_budget_deaths=2)
        )
        assert two == min(2700, max(graph._AGENT_INVOKE_TIMEOUT, 700)), (
            "two step-budget strikes must buy the fail-fast window"
        )

    def test_strike_total_is_the_sum_of_all_three_ledgers(self, monkeypatch):
        monkeypatch.setenv("WRITER_MAX_TIMEOUT", "2700")
        monkeypatch.setenv("WRITER_INVOKE_TIMEOUT", "700")
        # wall=1 alone: NOT degraded (never was) …
        assert graph._writer_extension_ceiling(
            _state(writer_wall_clock_timeouts=1)
        ) == 2700
        # … but wall=1 + step=1 IS (summed arithmetic, wave-58 guardian rule)
        assert graph._writer_extension_ceiling(
            _state(writer_wall_clock_timeouts=1, writer_step_budget_deaths=1)
        ) < 2700
        assert graph._writer_extension_ceiling(
            _state(writer_recursion_deaths=2)
        ) < 2700


# ═══════════════════════════════════════════════════════════════════════════
# 2 — the repair arm's behavioral contract (_run_draft_finisher)
# ═══════════════════════════════════════════════════════════════════════════


class TestRepairWindowBehavior:
    def test_substantial_unparsable_draft_gets_the_window(self, env, monkeypatch):
        """The pre-gate admits an UNPARSABLE ≥200-line draft — 623's class —
        and the death's SyntaxError rides the seed."""
        seed_text = {}
        built = {}

        def fake_factory(*a, **k):
            built["template_code"] = k.get("template_code") or ""
            return Recorder()

        class Recorder(FakeAgent):
            def invoke(self, messages, *a, **k):
                seed_text["content"] = str(messages)
                return super().invoke(messages, *a, **k)

        monkeypatch.setattr(graph, "create_code_writer", fake_factory)
        out = graph._run_draft_finisher(
            _state(), {}, SLUG, 623,
            require_parses=False,
            error_note="unterminated string literal (detected at line 683)",
        )
        assert built["template_code"] == SUBSTANTIAL_UNPARSABLE, (
            "the repair window seeds the CURRENT on-disk draft"
        )
        assert "SYNTAX ERROR" in seed_text["content"]
        assert "line 683" in seed_text["content"]
        # the post-invocation gate still owns salvage: the draft never became
        # parseable (FakeAgent writes nothing) → honest None
        assert out is None

    def test_small_unparsable_draft_refused_without_a_window(
        self, tmp_path, monkeypatch
    ):
        ws = tmp_path / "workspace" / SLUG
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text(TINY_UNPARSABLE)
        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))

        def boom(*a, **k):
            raise AssertionError("a stub must not buy a finisher window")

        monkeypatch.setattr(graph, "create_code_writer", boom)
        assert graph._run_draft_finisher(
            _state(), {}, SLUG, 623, require_parses=False,
            error_note="syntax error",
        ) is None

    def test_absent_draft_refused_without_a_window(self, tmp_path, monkeypatch):
        (tmp_path / "workspace" / SLUG).mkdir(parents=True)
        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))

        def boom(*a, **k):
            raise AssertionError("a missing draft must not buy a window")

        monkeypatch.setattr(graph, "create_code_writer", boom)
        assert graph._run_draft_finisher(
            _state(), {}, SLUG, 623, require_parses=False,
        ) is None

    def test_default_still_requires_a_parse(self, env, monkeypatch):
        """The draft-exists salvage arm's contract is untouched: default
        args refuse an unparsable draft before any window is spent."""
        def boom(*a, **k):
            raise AssertionError("default mode must keep the parse gate")

        monkeypatch.setattr(graph, "create_code_writer", boom)
        assert graph._run_draft_finisher(_state(), {}, SLUG, 623) is None

    def test_salvage_requires_a_parse_after_the_window(self, env, monkeypatch):
        """Healthy return + STILL-unparsable draft → None (the honest
        cleanup), even in repair mode."""

        class Writer(FakeAgent):
            def invoke(self, messages, *a, **k):
                # "fix" nothing — the draft stays broken on disk
                return super().invoke(messages, *a, **k)

        monkeypatch.setattr(graph, "create_code_writer", lambda *a, **k: Writer())
        assert graph._run_draft_finisher(
            _state(), {}, SLUG, 623, require_parses=False,
            error_note="syntax error",
        ) is None


# ═══════════════════════════════════════════════════════════════════════════
# 3 — the no-draft arm wiring (source-inspection idiom, wave-22)
# ═══════════════════════════════════════════════════════════════════════════


class TestNoDraftArmWiring:
    def _writer_src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def _nodraft_arm(self) -> str:
        src = self._writer_src()
        i = src.index("if not _draft_ok:")
        j = src.index("\n        if _cw_dead:", i)
        return src[i:j]

    def test_repair_sits_between_provider_park_and_cleanup(self):
        arm = self._nodraft_arm()
        i_park = arm.index('goto="park_provider_dead"')
        i_fin = arm.index("_run_draft_finisher(")
        i_cleanup = arm.index('goto="cleanup"')
        assert i_park < i_fin < i_cleanup, (
            "the repair window must run INSIDE the non-provider-dead branch "
            "(the finisher runs no provider gate) and BEFORE the honest cleanup"
        )

    def test_repair_is_latched_once_per_job(self):
        arm = self._nodraft_arm()
        i_check = arm.index('state.get("writer_finisher_attempted")')
        i_stamp = arm.index('update["writer_finisher_attempted"] = True')
        i_fin = arm.index("_run_draft_finisher(")
        assert i_check < i_stamp < i_fin

    def test_repair_keys_on_death_ledgers_and_substance(self):
        arm = self._nodraft_arm()
        assert "_window_death" in arm
        for ledger in (
            "writer_wall_clock_timeouts",
            "writer_recursion_deaths",
            "writer_step_budget_deaths",
        ):
            assert ledger in arm.split("if not _salvaged:")[0], (
                f"the repair trigger must consult the {ledger} ledger"
            )
        assert ">= 200" in arm, "the substance floor must gate the window"

    def test_salvage_falls_through_and_refusal_cleans_up(self):
        arm = self._nodraft_arm()
        i_salvage = arm.index("_salvaged = True")
        guards = arm[i_salvage:].count("if not _salvaged:")
        assert guards >= 3, (
            "after a salvage, ALL no-draft returns (cleanup, human_approval, "
            "scraper_analyzer retry) must be guarded so control falls through "
            "to the usable-draft tail (test ladder)"
        )

    def test_no_second_ledger_increment_in_the_repair_arm(self):
        """The no-draft arm ALREADY increments the ledgers above the strike
        gate; the repair arm must only CLEAR on salvage — a second increment
        would double-count and abort salvageable jobs early."""
        arm = self._nodraft_arm()
        repair = arm[arm.index("[wave-59 C4] REPAIR arm"):]
        repair = repair[: repair.index("if not _salvaged:")]
        assert 'writer_wall_clock_timeouts"] = int(' not in repair
        assert 'writer_recursion_deaths"] = int(' not in repair
        assert 'update["writer_wall_clock_timeouts"] = 0' in repair
        assert 'update["writer_recursion_deaths"] = 0' in repair

    def test_salvage_resets_error_counters_and_ledgers(self):
        arm = self._nodraft_arm()
        i_salvage = arm.index("_salvaged = True")
        tail = arm[i_salvage: arm.index("if not _salvaged:", i_salvage)]
        assert 'update["code_writer_error_count"] = 0' in tail
        assert 'update["code_writer_error"] = ""' in tail
        assert '_cw_dead = False' in tail and "result = _fin" in tail

    def test_repair_uses_the_reparse_refusal_mode(self):
        arm = self._nodraft_arm()
        call = arm[arm.index("_run_draft_finisher("):]
        assert "require_parses=False" in call[:200]
        assert "error_note=_err_note" in call[:200], (
            "the death's SyntaxError must ride the repair seed"
        )

    def test_two_alive_noops_still_go_straight_to_cleanup(self):
        """No window death on the ledgers → no repair → the honest cleanup
        (the pre-wave-59 contract for the alive no-op class)."""
        arm = self._nodraft_arm()
        i_gate = arm.index("_window_death = bool(")
        guard = arm[i_gate: arm.index("if _repair_lines >= 200")]
        assert "writer_wall_clock_timeouts" in guard
        assert 'if _window_death and not state.get("writer_finisher_attempted")' in arm


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
