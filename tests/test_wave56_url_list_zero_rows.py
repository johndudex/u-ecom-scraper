"""[wave-56 F4] url_list execution-zero honesty — the books-579 hole.

Job 579 (books-toscrape url_list, 2026-10-09): the tester PASSED the draft
(1 live seed validated, 2 written off under the dead-URL rule), then
execution processed the seeds and extracted ZERO rows — and every gate let it
through to ``execution_status=SUCCESS, product_count=0``:

- F9 exempts url_list by design ("a wholesale seed failure is code_tester's
  signal" — but the tester had just NOT caught it);
- ``_url_list_scope_gate`` returns "" on a zero-row output ("no rows to
  judge" — a scope assertion has nothing to compare, true, but zero-yield is
  not a scope question; it is the exact wholesale-seed-failure shape the
  tester-side assumption trusted);
- cleanup then recorded ``outcome=success, item_count 0`` writer memory and
  skill_learner ran on it. Only the Django finalize ladder failed the job,
  after the false SUCCESS had already ridden the session.

Three fixes, all deterministic:
- the scope gate fires on zero rows when seeds were dispatched (the output
  file EXISTS — this is not the no_fresh_output shape);
- a url_list zero-row execution recycles ONCE through the strategy ladder
  (``_EXECUTION_RECYCLE_MAX`` already bounds it) with url_list-specific
  feedback — books 579 was a hydration/transport shape, and the nav-mode
  zero-item recycle has existed since job-65 for exactly this class;
- ``_record_writer_memory`` never stamps ``outcome="success"`` over a
  zero-item run — the entry lands as failure with an honest zero-yield note.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


class TestScopeGateZeroRows:
    def _write_output(self, tmp_path, payload):
        p = tmp_path / "output_test.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_zero_rows_with_seeds_fires(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = self._write_output(tmp_path, {"products": [], "failed_products": 11})
        diag = _url_list_scope_gate(
            out,
            ["https://books-toscrape.com/catalogue/book_1/index.html",
             "https://books-toscrape.com/catalogue/book_2/index.html"],
            "url_list",
        )
        assert diag, "zero-row url_list output must fail, not silently pass"
        assert "0 of 2" in diag
        assert "url_list" in diag

    def test_nonzero_rows_in_scope_still_passes(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = self._write_output(tmp_path, {"products": [
            {"url": "https://books-toscrape.com/catalogue/book_1/index.html"}
        ]})
        assert _url_list_scope_gate(out, [
            "https://books-toscrape.com/catalogue/book_1/index.html",
        ], "url_list") == ""

    def test_zero_rows_without_seeds_still_noop(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = self._write_output(tmp_path, {"products": []})
        assert _url_list_scope_gate(out, [], "url_list") == ""

    def test_missing_output_file_still_noop(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        # A MISSING output file is the no_fresh_output arm's subject — the
        # gate must not double-fire on a file that never existed.
        missing = str(tmp_path / "never_written.json")
        assert _url_list_scope_gate(missing, ["https://x.com/p/1"], "url_list") == ""

    def test_non_url_list_mode_untouched(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = self._write_output(tmp_path, {"products": []})
        assert _url_list_scope_gate(out, ["https://x.com/p/1"], "list_page") == ""


class TestUrlListZeroRowRecycle:
    """The mode gate that sent books 579 straight to cleanup."""

    def test_url_list_zero_rows_recycles(self):
        from agents.graph import _url_list_zero_row_should_recycle

        assert _url_list_zero_row_should_recycle("url_list", "SUCCESS", 0) is True

    def test_url_list_failed_extraction_zero_recycles(self):
        from agents.graph import _url_list_zero_row_should_recycle

        # The F4 scope gate turns zero-row runs into FAILED — its diagnostic
        # is the marker that admits a FAILED run into the recycle (a raw
        # traceback FAILED run stays a code problem: cleanup, no ladder).
        assert (
            _url_list_zero_row_should_recycle(
                "url_list", "FAILED", 0,
                error_message="url_list zero-yield: 0 of 11 submitted seeds ...",
            )
            is True
        )

    def test_url_list_failed_traceback_never_recycles(self):
        from agents.graph import _url_list_zero_row_should_recycle

        assert (
            _url_list_zero_row_should_recycle(
                "url_list", "FAILED", 0,
                error_message="Traceback (most recent call last): ...",
            )
            is False
        )

    def test_url_list_with_items_never_recycles(self):
        from agents.graph import _url_list_zero_row_should_recycle

        assert _url_list_zero_row_should_recycle("url_list", "SUCCESS", 4) is False

    def test_nav_modes_unchanged(self):
        from agents.graph import _url_list_zero_row_should_recycle

        # Nav modes have their own recycle arm; the helper is url_list-only.
        assert _url_list_zero_row_should_recycle("navigation", "SUCCESS", 0) is False


class TestWriterMemoryZeroYieldHonesty:
    def test_success_with_zero_items_recorded_as_failure(self, monkeypatch):
        import src.writer_memory as wmem
        from agents.graph import _record_writer_memory

        captured = {}
        monkeypatch.setattr(
            wmem, "upsert_memory",
            lambda slug, entry, probe_fingerprint=None: captured.update(entry),
        )
        state = {
            "site_slug": "books-toscrape-com",
            "job_id": 579,
            "product_count": 0,
            "scraper_analysis": {"strategy": "http_requests"},
            "probe_result": {"connectivity": {"method_that_worked": "direct_http"}},
        }
        _record_writer_memory(state, outcome="success")
        assert captured, "memory entry must still be written"
        assert captured["outcome"] == "failure"
        assert "zero" in str(captured.get("note", "")).lower()

    def test_success_with_items_stays_success(self, monkeypatch):
        import src.writer_memory as wmem
        from agents.graph import _record_writer_memory

        captured = {}
        monkeypatch.setattr(
            wmem, "upsert_memory",
            lambda slug, entry, probe_fingerprint=None: captured.update(entry),
        )
        state = {
            "site_slug": "tatcha-com",
            "job_id": 577,
            "product_count": 3,
            "scraper_analysis": {"strategy": "http_requests"},
        }
        _record_writer_memory(state, outcome="success")
        assert captured["outcome"] == "success"
        assert captured["item_count"] == 3
