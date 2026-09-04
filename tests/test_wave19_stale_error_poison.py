"""[wave-19 T1.5] Stale-error poison: a recovered interrupt must not kill a
productive job. (323/D3)

The poison chain: ``validate_coverage``'s missing-file interrupt arm sets
``state["error_message"] = "analysis not found in workspace"`` → the human
picks "Retry content analysis" → the re-run succeeds and the node proceeds —
but NOTHING clears the stale error → execution extracts 4 real items → the
finalizer syncs state error_message onto the job (tasks.py ``job.error_message
= final_state.get(...)``) and the ladder's ``elif job.error_message:`` ranks
it FAILED. A job that did its whole purpose dies of a note left on the
fridge.

Two walls, per the plan row:
1. every non-interrupt exit of ``validate_coverage`` clears error_message
   (the recovery IS the answer to the note);
2. the finalizer ladder demotes a stale error below a productive execution:
   execution_status==FAILED / never-executed / zero-items still fail, but
   output with real items completes — and the stale note is scrubbed.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

STALE = "analysis not found in workspace"

FULL_COVERAGE_ANALYSIS = {
    "fields": {
        k: {"method": "css", "selector": f".{k}"}
        for k in ("title", "price", "availability", "original_price", "currency", "url", "src_url")
    }
}


# ── wall 1: validate_coverage recovery clears the note ───────────────────────


class TestValidateCoverageClearsStaleError:
    @pytest.fixture()
    def vc(self):
        # agents.nodes re-exports the function, shadowing the submodule —
        # importlib guarantees the MODULE for patching.
        return importlib.import_module("agents.nodes.validate_coverage")

    def _state(self, **extra):
        return {
            "site_slug": "poison-test",
            "page_type": "product",
            "navigation_analysis": {},
            "error_message": STALE,
            **extra,
        }

    def test_success_path_clears_stale_error(self, vc, monkeypatch):
        monkeypatch.setattr(vc, "_load_product_analysis", lambda slug: FULL_COVERAGE_ANALYSIS)
        cmd = vc.validate_coverage(self._state())
        assert cmd.goto == "scraper_analyzer"
        assert (cmd.update or {}).get("error_message") == "", (
            "a successful coverage pass must clear interrupt-era error_message"
        )

    def test_api_bypass_clears_stale_error(self, vc, monkeypatch):
        monkeypatch.setattr(vc, "_load_product_analysis", lambda slug: FULL_COVERAGE_ANALYSIS)
        nav = {
            "data_source": "api",
            "api_endpoint": {"url": "https://t.com/api/search", "items_per_page": 20},
        }
        cmd = vc.validate_coverage(self._state(navigation_analysis=nav))
        assert cmd.goto == "scraper_analyzer"
        assert (cmd.update or {}).get("error_message") == ""

    def test_missing_file_interrupt_still_sets_the_note(self, vc, monkeypatch):
        """Guard: the fix clears on RECOVERY — the interrupt arm itself must
        keep recording why it stopped (the approval UI's evidence)."""
        monkeypatch.setattr(vc, "_load_product_analysis", lambda slug: None)
        cmd = vc.validate_coverage(self._state())
        assert cmd.goto == "human_approval"
        assert (cmd.update or {}).get("error_message") == STALE


# ── wall 2: the finalizer ladder demotes stale errors ────────────────────────


class TestFinalizerLadder:
    @pytest.fixture()
    def ladder(self):
        # exec-extract (f3 style): importing webapp.scraper.tasks registers a
        # conflicting Django app; the ladder is pure stdlib + ScrapeJob constants.
        src = open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")).read()
        m = re.search(r"^def _final_status_ladder\(.*?(?=^def |\Z)", src, re.M | re.S)
        assert m, "_final_status_ladder not found in tasks.py"
        m0 = re.search(
            r"^def _output_file_has_zero_items\(.*?(?=^def |\Z)", src, re.M | re.S
        )
        assert m0, "_output_file_has_zero_items not found in tasks.py"
        from scraper.models import ScrapeJob  # real status constants

        TestFinalizerLadder.SJ = ScrapeJob
        ns: dict = {"__name__": "t_wave19_ladder", "ScrapeJob": ScrapeJob}
        exec(m0.group(0), ns)
        exec(m.group(0), ns)
        return ns["_final_status_ladder"]

    @pytest.fixture()
    def output_with_4(self, tmp_path):
        p = tmp_path / "out.json"
        p.write_text(json.dumps({"products": [{"title": f"p{i}"} for i in range(4)]}))
        return str(p)

    @pytest.fixture()
    def output_with_0(self, tmp_path):
        p = tmp_path / "out0.json"
        p.write_text(json.dumps({"products": []}))
        return str(p)

    def _run(self, ladder, final_state, output_file, error_message=STALE, **kw):
        return ladder(
            final_state,
            already_terminal=kw.get("already_terminal", False),
            was_cancelled=kw.get("was_cancelled", False),
            error_message=error_message,
            output_file=output_file,
        )

    def test_productive_execution_beats_stale_error(self, ladder, output_with_4):
        """THE 323/D3 case: 4 real items + a stale interrupt note → COMPLETED."""
        status, _diag = self._run(
            ladder, {"execution_status": "COMPLETED"}, output_with_4
        )
        assert status == self.SJ.STATUS_COMPLETED

    def test_stale_error_with_zero_items_still_fails(self, ladder, output_with_0):
        status, diag = self._run(ladder, {"execution_status": "COMPLETED"}, output_with_0)
        assert status == self.SJ.STATUS_FAILED
        assert "0 items" in diag

    def test_stale_error_with_no_execution_still_fails(self, ladder):
        status, diag = self._run(ladder, {}, "")
        assert status == self.SJ.STATUS_FAILED
        assert diag, "never-executed arm must supply its own diagnostic"

    def test_execution_failed_flag_still_fails(self, ladder, output_with_4):
        status, _diag = self._run(ladder, {"execution_status": "FAILED"}, output_with_4)
        assert status == self.SJ.STATUS_FAILED

    def test_clean_completion_with_no_error(self, ladder, output_with_4):
        status, diag = self._run(
            ladder, {"execution_status": "COMPLETED"}, output_with_4, error_message=""
        )
        assert status == self.SJ.STATUS_COMPLETED
        assert diag == ""

    def test_cancel_beats_stale_error(self, ladder, output_with_4):
        status, _diag = self._run(
            ladder, {"execution_status": "COMPLETED"}, output_with_4, was_cancelled=True
        )
        assert status == self.SJ.STATUS_CANCELLED

    def test_already_terminal_is_passthrough(self, ladder, output_with_4):
        status, _diag = self._run(
            ladder, {"execution_status": "COMPLETED"}, output_with_4, already_terminal=True
        )
        assert status == ""

    def test_output_items_without_execution_status_and_stale_error_fails(
        self, ladder, output_with_4
    ):
        """Conservative holdover: a stale error plus no recorded execution
        still fails (the error is the only evidence about what happened)."""
        status, _diag = self._run(ladder, {}, output_with_4)
        assert status == self.SJ.STATUS_FAILED


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
