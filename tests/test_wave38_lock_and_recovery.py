"""[wave-38] Graph-side lock hygiene + wrong-site recovery.

The forced re-traverse ran OUTSIDE the lock — a guaranteed bleed window and
the exact shape that made 412 possible. TTL 1500 < walk ceiling 3600 meant a
healthy long walk outlived its own lock. This pins the fix: ONE locked,
bounded-wait recovery helper, and progress-gated TTL renewal.
"""
from __future__ import annotations

import contextlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import agents.graph as g  # noqa: E402
import pytest  # noqa: E402
from langgraph.types import Command, RunnableConfig  # noqa: E402

from experimental.nav_traversal import traversal as tv  # noqa: E402


def _tr(seed, **kw):
    """Honest TraversalResult via the same positional idiom graph uses.
    (seed may carry 8 or 9 elements — the 9th positional IS notes.)"""
    return tv.TraversalResult(*seed, **kw)


NOT_REACHED = (False, None, ["https://a.example/"], "unknown", None, {},
               ["https://a.example/"], [], "didn't reach")
CLEAN = (True, "https://a.example/collections",
         ["https://a.example/collections"], "browser_llm", None,
         {"is_listing": True}, ["https://a.example/collections"], [],
         "LLM judged listing")
# item_links keep the node tail off its HTTP url_examples fallback (which
# would fetch the goal_url for real); discovery feeds the listing_reached pin.
CLEAN_KW = dict(
    item_links=["https://a.example/collections/p/1"],
    discovery={"listing_url": "https://a.example/collections",
               "listing_reached": True,
               "pagination": {"type": "page_param"}},
)


@contextlib.contextmanager
def _lock(acquired=True):
    yield acquired


class TestRetraverseLocked:
    def test_acquired_runs_browser_traverse_locked(self, monkeypatch):
        seen = {}

        def fake_bt(url, ct, q, **kw):
            seen.update(kw)
            return _tr(CLEAN)

        monkeypatch.setattr(tv, "browser_traverse", fake_bt)
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(True))
        out = g._retraverse_locked("https://a.example/", "product", "x", 7)
        assert out.reached is True
        assert seen.get("job_id") == 7
        assert seen.get("trust_start_as_listing") is False
        assert callable(seen.get("heartbeat_fn"))

    def test_not_acquired_returns_honest_not_reached(self, monkeypatch):
        """Lock-busy = D5 boundary: NO walk, NO internal HTTP lane — the
        helper returns an honest sentinel and the NODE's existing
        `if not result.reached:` fallback (the single HTTP lane) takes over."""
        monkeypatch.setattr(tv, "browser_traverse", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not walk without the lock")))
        monkeypatch.setattr(tv, "traverse", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("HTTP lane belongs to the NODE, not the helper")))
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(False))
        out = g._retraverse_locked("https://a.example/", "product", "x", 7)
        assert out.reached is False
        assert "lock busy" in (out.notes or "")
        assert out.discovery["listing_reached"] is False

    def test_walk_exception_returns_honest_not_reached(self, monkeypatch):
        monkeypatch.setattr(tv, "browser_traverse",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        monkeypatch.setattr(tv, "traverse", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("HTTP lane belongs to the NODE, not the helper")))
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(True))
        out = g._retraverse_locked("https://a.example/", "product", "x", 7)
        assert out.reached is False
        assert "locked re-traverse failed" in (out.notes or "")
        assert out.discovery["listing_reached"] is False

    def test_contamination_branch_uses_the_helper(self):
        """The old bare browser_traverse at the T1.6 branch is the bleed
        window — source-pin its replacement."""
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py"),
                   encoding="utf-8").read()
        assert "_retry = _retraverse_locked(" in src
        assert "_retry = browser_traverse(" not in src


class TestWrongSiteRecovery:
    def _run(self, monkeypatch, tmp_path, first, second):
        calls = []

        def scripted(url, ct, q, **kw):
            calls.append(kw)
            return second if len(calls) > 1 else first

        monkeypatch.setattr(tv, "browser_traverse", scripted)
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(True))
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "_log_event_row", lambda *a, **k: None)
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        state = {"job_id": 0, "site_slug": "s", "url": "https://a.example/",
                 "page_type": "product", "input_mode": "navigation",
                 "search_criteria": "dress"}
        out = g._invoke_navigation_traverse(state, RunnableConfig())
        return out, calls

    def test_abort_triggers_exactly_one_locked_recovery(
            self, monkeypatch, tmp_path):
        # seed[:8]: the 9th positional IS notes — the abort's note goes by kw
        abort = _tr(NOT_REACHED[:8], wrong_site_abort=True,
                    wrong_site_url="https://www.westelm.com.au/bath",
                    notes="wrong-site abort: browser tab on "
                          "https://www.westelm.com.au/bath (expected "
                          "a.example) after 2 consecutive off-domain reads")
        out, calls = self._run(monkeypatch, tmp_path, abort,
                               _tr(CLEAN, **CLEAN_KW))
        assert len(calls) == 2, "initial walk + exactly ONE recovery walk"
        assert "MCP" not in (abort.notes or "")
        assert isinstance(out, Command) and out.goto == "product_analyzer"
        upd = out.update
        assert upd["navigation_analysis"]["discovery"]["listing_reached"] \
            is True

    def test_clean_first_walk_never_recovers(self, monkeypatch, tmp_path):
        out, calls = self._run(monkeypatch, tmp_path,
                               _tr(CLEAN, **CLEAN_KW), None)
        assert len(calls) == 1

    def test_recovery_lock_busy_degrades_to_node_http_lane(
            self, monkeypatch, tmp_path):
        """Abort → recovery lock busy → honest sentinel → the NODE's own
        `if not result.reached:` HTTP fallback runs traverse() EXACTLY ONCE.
        The helper runs no internal HTTP lane (D5); on a JS-listing site this
        degrades honestly instead of fabricating a second browser walk."""
        abort = _tr(NOT_REACHED[:8], wrong_site_abort=True,
                    wrong_site_url="https://www.westelm.com.au/bath",
                    notes="wrong-site abort: browser tab on "
                          "https://www.westelm.com.au/bath (expected "
                          "a.example) — off-domain page judged is_listing")
        locks = iter([True, False])

        monkeypatch.setattr(
            g, "_mcp_browser_lock",
            lambda job_id, wait_timeout=900.0, poll_interval=5.0:
                _lock(next(locks, False)))
        walks = []

        def scripted(url, ct, q, **kw):
            walks.append("walk")
            return abort

        monkeypatch.setattr(tv, "browser_traverse", scripted)
        monkeypatch.setattr(
            tv, "traverse",
            lambda *a, **k: walks.append("http") or _tr(CLEAN, **CLEAN_KW))
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "_log_event_row", lambda *a, **k: None)
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        state = {"job_id": 0, "site_slug": "s", "url": "https://a.example/",
                 "page_type": "product", "input_mode": "navigation",
                 "search_criteria": "dress"}
        out = g._invoke_navigation_traverse(state, RunnableConfig())
        assert walks == ["walk", "http"], (
            "initial walk + exactly one HTTP fallback; the lock-busy "
            "recovery must not add a second walk"
        )
        assert isinstance(out, Command) and out.goto == "product_analyzer"
        assert out.update["navigation_analysis"]["discovery"][
            "listing_reached"] is True


class TestLockRenewal:
    def _writer_env(self, monkeypatch):
        evals = []

        class _FakeRedis:
            def eval(self, script, n, key, *args):
                evals.append((key,) + tuple(args))

        monkeypatch.setattr(g, "_traversal_redis", lambda: _FakeRedis())

        rows = []

        class _Obj:
            def filter(self, **k):
                return self

            def count(self):
                return 1

            def create(self, **k):
                rows.append(k)

        class _FakeSessionLog:
            ROLE_SYSTEM = "system"
            objects = _Obj()

        import scraper.models as sm

        monkeypatch.setattr(sm, "SessionLog", _FakeSessionLog)
        return evals, rows

    def test_progress_beats_renew_ttl(self, monkeypatch):
        evals, rows = self._writer_env(monkeypatch)
        w = g._traverse_heartbeat_writer(7, renew_lock=True)
        w({"step": 1, "elapsed_s": 1, "actions": 0, "reason": "step"})
        w({"step": 2, "elapsed_s": 2, "actions": 3, "reason": "step"})
        w({"step": 3, "elapsed_s": 3, "actions": 3, "reason": "step"})
        w({"step": 4, "elapsed_s": 4, "actions": 5, "reason": "step"})
        assert rows, "SessionLog rows still written first"
        assert len(evals) == 2, (
            "renewal ONLY on progress: beats with actions 0→3→3→5 renew twice"
        )
        assert evals[0][0] == g._TRAVERSAL_LOCK_KEY
        assert evals[0][1] == "7"
        assert int(evals[0][2]) == g._TRAVERSAL_LOCK_TTL

    def test_stalled_walk_stops_renewing(self, monkeypatch):
        """D4: TTL expiry is the hung-walk self-heal — a stalled walk must
        NOT keep its lock alive."""
        evals, _ = self._writer_env(monkeypatch)
        w = g._traverse_heartbeat_writer(7, renew_lock=True)
        w({"step": 1, "elapsed_s": 1, "actions": 2, "reason": "step"})
        w({"step": 2, "elapsed_s": 301, "actions": 2, "reason": "step"})
        w({"step": 3, "elapsed_s": 601, "actions": 2, "reason": "step"})
        assert len(evals) == 1

    def test_renew_off_by_default(self, monkeypatch):
        evals, _ = self._writer_env(monkeypatch)
        g._traverse_heartbeat_writer(7)({
            "step": 1, "elapsed_s": 1, "actions": 9, "reason": "step"})
        assert evals == []

    def test_renewal_errors_never_break_the_walk(self, monkeypatch):
        evals, rows = self._writer_env(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("redis down")

        monkeypatch.setattr(g, "_traversal_redis", boom)
        w = g._traverse_heartbeat_writer(7, renew_lock=True)
        w({"step": 1, "elapsed_s": 1, "actions": 1, "reason": "step"})
        assert rows, "the SessionLog row must survive a dead redis"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
