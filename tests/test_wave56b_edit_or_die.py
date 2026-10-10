"""[wave-56b EDIT-OR-DIE] A writer that reads without editing is an honest
no-op strike, not a patient.

Deep-audit proof (nordstrom job 581, 2026-10-09): the writer burned 125
read-only tool calls across 3 invocations with ZERO edits after cycle 0 —
it re-read the template until the wall clock died. The ``_ToolCallLogger``
already records every call (``.calls``); the writer path must classify the
spiral, inject a force-edit directive once, and abort as a no-op strike
when the directive is ignored.
"""
from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

READS = [
    {"name": "read_file", "args": "templates/navigation_scraper.py"},
    {"name": "search_content", "args": "def phase1"},
    {"name": "search_files", "args": "*.py"},
]


def _calls(reads, edits=0, writes=0):
    # Exactly `reads` read-only calls (READS * reads would triple-count —
    # READS itself holds 3 entries).
    out = [dict(READS[i % len(READS)]) for i in range(reads)]
    out += [{"name": "edit_file", "args": "scraper_draft.py"}] * edits
    out += [{"name": "write_file", "args": "scraper_draft.py"}] * writes
    return out


class TestSpiralClassifier:
    def test_reads_only_beyond_warn_threshold_is_warn(self):
        from agents.graph import _writer_read_only_spiral

        assert _writer_read_only_spiral(_calls(reads=25)) == "warn"

    def test_reads_only_beyond_abort_threshold_is_abort(self):
        from agents.graph import _writer_read_only_spiral

        assert _writer_read_only_spiral(_calls(reads=50)) == "abort"

    def test_any_edit_resets_the_spiral(self):
        from agents.graph import _writer_read_only_spiral

        assert _writer_read_only_spiral(_calls(reads=60, edits=1)) == "ok"

    def test_any_write_resets_the_spiral(self):
        from agents.graph import _writer_read_only_spiral

        assert _writer_read_only_spiral(_calls(reads=60, writes=1)) == "ok"

    def test_quiet_writer_is_ok(self):
        from agents.graph import _writer_read_only_spiral

        assert _writer_read_only_spiral(_calls(reads=5)) == "ok"

    def test_missing_edit_is_counted_not_ignored(self):
        """read_file with no matching edit counts as read-only — the
        nordstrom pattern (reads: 125, edits: 0)."""
        from agents.graph import _writer_read_only_spiral

        calls = [{"name": "read_file", "args": "t.py"}] * 125
        assert _writer_read_only_spiral(calls) == "abort"


class TestForceEditDirective:
    def test_warn_directive_names_edit_file(self):
        from agents.graph import _writer_spiral_directive

        msg = _writer_spiral_directive(25, 0)
        assert "edit_file" in msg or "write_file" in msg
        assert "zero edits" in msg.lower() or "0 edits" in msg.lower()

    def test_directive_reports_the_counts(self):
        from agents.graph import _writer_spiral_directive

        msg = _writer_spiral_directive(125, 0)
        assert "125" in msg


class TestWiringExists:
    """The guard must be live on the writer invocation path, and an ignored
    warn must convert to an abort that records an honest no-op strike."""

    def test_invoke_code_writer_classifies_the_spiral(self):
        import agents.graph as g

        src = inspect.getsource(g._invoke_code_writer)
        assert "_writer_read_only_spiral" in src

    def test_abort_records_noop_strike_state(self):
        import agents.graph as g

        src = inspect.getsource(g._invoke_code_writer)
        assert "noop_fix_cycles" in src or "no-op" in src.lower()
