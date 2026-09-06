"""[wave-22 B2] A terminal verdict must never be issued about a draft the
tester never judged — one forced re-test first, capped, wall-guarded.

Prod shape (329 class): a zombie writer thread edited the draft AFTER the
tester's verdict; the cascade then terminated on a report describing a draft
that no longer exists. Also: a writer wall-clock death after writing leaves a
fresh, untested draft that the exhausted ladder still terminates on. The
router, at every terminal cleanup/human_approval, must notice
current-draft-fp ≠ last-tested-fp and spend ONE forced tester pass first.

Contract (critique-amended):
- per-JOB cap 1 on a dedicated counter (``forced_retest_count``) — a second
  untested terminal goes straight out (cannot loop);
- SKIPPED when the ``tester_wall_clock_timeouts >= 2`` arm fires (same wall
  twice is not a draft that needs testing — it's a wall);
- SKIPPED for the A5 fast-fail arm (its whole point is dying fast) and the
  park arms;
- the router is a pure routing function: it may READ the draft (hash), never
  write state; the dedicated counter is incremented by the tester node when a
  pass converts an untested draft into a judged one;
- budget-thinness is enforced DOWNSTREAM by A3's clamp: the forced pass gets
  whatever window remains (possibly ~0, dying honestly) — the router has no
  config access and must not guess.
"""
from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

rat = importlib.import_module("agents.nodes.route_after_testing")  # noqa: E402


DRAFT_A = "PRODUCTS = ['a']\n"
DRAFT_B = "PRODUCTS = ['a', 'b']\n"

import hashlib  # noqa: E402

# the fingerprint the tester LEFT BEHIND (draft A) — independent of whatever
# is on disk now, so mismatch tests can rewrite the file after _state()
FP_A = hashlib.sha1(DRAFT_A.encode()).hexdigest()


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    slug_dir = tmp_path / "workspace" / "example-com"
    slug_dir.mkdir(parents=True)
    return slug_dir


def _state(ws_dir, **over) -> dict:
    base = {
        "job_id": 0,
        "site_slug": "example-com",
        "test_report": {
            "overall_assessment": "FAIL",
            "confidence_score": 0.6,
            "issues": [],
        },
        "test_retry_count": rat.MAX_TEST_RETRIES,
        "skip_approvals": True,
        "tester_wall_clock_timeouts": 0,
        "forced_retest_count": 0,
        "last_tested_draft_fp": FP_A,
    }
    base.update(over)
    return base


class TestDraftFileFp:
    def test_matches_tester_algorithm_shape(self, ws):
        p = ws / "scraper_draft.py"
        p.write_text(DRAFT_A)
        fp = rat.draft_file_fp(str(p))
        assert len(fp) == 40 and fp == rat.draft_file_fp(str(p))
        p.write_text(DRAFT_B)
        assert rat.draft_file_fp(str(p)) != fp

    def test_missing_draft_is_empty_fp(self, ws):
        assert rat.draft_file_fp(str(ws / "nope.py")) == ""


class TestTerminalFunnel:
    def test_untested_draft_gets_one_forced_pass(self, ws):
        p = ws / "scraper_draft.py"
        p.write_text(DRAFT_B)  # differs from last-tested (DRAFT_A)
        state = _state(ws)
        assert rat._terminal_after_retest_check(state, "cleanup") == "code_tester"
        assert (
            rat._terminal_after_retest_check(state, "human_approval")
            == "code_tester"
        )

    def test_tested_draft_passes_through(self, ws):
        (ws / "scraper_draft.py").write_text(DRAFT_A)
        state = _state(ws)
        assert rat._terminal_after_retest_check(state, "cleanup") == "cleanup"

    def test_cap_one_no_second_pass(self, ws):
        (ws / "scraper_draft.py").write_text(DRAFT_B)
        state = _state(ws, forced_retest_count=1)
        assert rat._terminal_after_retest_check(state, "cleanup") == "cleanup"

    def test_wall_clock_arm_disables_the_pass(self, ws):
        (ws / "scraper_draft.py").write_text(DRAFT_B)
        state = _state(ws, tester_wall_clock_timeouts=2)
        assert rat._terminal_after_retest_check(state, "cleanup") == "cleanup"

    def test_missing_draft_passes_through(self, ws):
        state = _state(ws, last_tested_draft_fp="")
        assert rat._terminal_after_retest_check(state, "human_approval") == (
            "human_approval"
        )


class TestRouterIntegration:
    def test_exhausted_terminal_reroutes_to_tester(self, ws):
        (ws / "scraper_draft.py").write_text(DRAFT_B)
        state = _state(ws)
        # exhausted retries, real FAIL report → historically terminal cleanup
        assert rat.route_after_testing(state) == "code_tester"

    def test_exhausted_terminal_after_cap_goes_terminal(self, ws):
        (ws / "scraper_draft.py").write_text(DRAFT_B)
        state = _state(ws, forced_retest_count=1)
        assert rat.route_after_testing(state) == "cleanup"

    def test_fast_fail_arm_is_never_intercepted(self, ws):
        (ws / "scraper_draft.py").write_text(DRAFT_B)
        state = _state(ws)
        state["fast_fail_detail"] = "code_tester hit its wall clock"
        state["test_report"] = None
        state["test_retry_count"] = 0
        assert rat.route_after_testing(state) == "cleanup", (
            "A5's fast-fail must stay fast — the funnel must not sit in front "
            "of it"
        )


class TestTesterIncrement:
    def test_tester_counts_an_untested_to_judged_pass(self):
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            src = fh.read()
        i = src.index("def _invoke_code_tester")
        j = src.index("\ndef ", i + 10)
        body = src[i:j]
        assert "forced_retest_count" in body, (
            "the tester never increments the dedicated cap counter — the "
            "router's one-pass allowance could never be consumed and every "
            "terminal would bounce off an untested draft forever"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
