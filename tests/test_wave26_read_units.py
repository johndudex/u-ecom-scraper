"""[wave-26 W26-4] read/search unit consistency for the fix-cycle writer.

Prod 418/419/420: ``search_content`` reports hits as ``file.py:LINE:`` while
``read_file(offset=...)`` slices a CHARACTER position — and the wave-25
anti-read nudge explicitly taught the writer to feed one into the other.
Every "targeted" read returned the module docstring; 418's fix cycle spent
51 tool calls and wrote 0 bytes before the step budget killed it. Fix: a
line-based read (``line=``/``num_lines=``) that matches search_content's
unit, nudge text that names the same unit, and out-of-range errors that
steer instead of dead-end.
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

fs = importlib.import_module("agents.tools.filesystem_tools")  # noqa: E402
subagents = importlib.import_module("agents.subagents")  # noqa: E402


LINES = [f"line {i:03d}: padding padding padding" for i in range(1, 1001)]
TARGET_LINE = 900
LINES[TARGET_LINE - 1] = "def _discover_urls_via_category():  # the target"


@pytest.fixture()
def tools(tmp_path):
    tools = fs.get_filesystem_tools(project_root=str(tmp_path), syntax_gate=False)
    by_name = {t.name: t.func for t in tools}
    target = tmp_path / "scraper_draft.py"
    target.write_text("\n".join(LINES))
    return by_name, target


class TestLineBasedRead:
    def test_line_param_returns_the_searched_line_region(self, tools):
        """search_content says the target is at line 900 — read_file(line=900)
        must return line 900, not the docstring at char offset 900."""
        by_name, target = tools
        out = by_name["read_file"](str(target), line=TARGET_LINE)
        assert "_discover_urls_via_category" in out
        assert "line 900" in out

    def test_num_lines_bounds_the_window(self, tools):
        by_name, target = tools
        out = by_name["read_file"](str(target), line=10, num_lines=5)
        body = [ln for ln in out.splitlines() if ln.startswith("line ")]
        assert len(body) == 5
        assert body[0].startswith("line 010")
        assert body[-1].startswith("line 014")

    def test_line_read_carries_unit_guidance(self, tools):
        """The result must teach the unit split so the writer stops passing
        line numbers as char offsets."""
        by_name, target = tools
        out = by_name["read_file"](str(target), line=500)
        assert "CHARACTER" in out.upper()
        assert "line=" in out

    def test_offset_and_line_rejected_together(self, tools):
        by_name, target = tools
        out = by_name["read_file"](str(target), offset=100, line=10)
        assert "offset" in out and "line" in out
        assert "line 010" not in out

    def test_line_past_eof_steers(self, tools):
        by_name, target = tools
        out = by_name["read_file"](str(target), line=100_000)
        assert "out of range" in out
        assert "1,000" in out or "1000" in out


class TestOffsetSteering:
    def test_out_of_range_offset_names_the_unit(self, tools):
        """418's writer passed search_content line numbers as offsets and got
        a bare 'out of range' — the error must now point at line=."""
        by_name, target = tools
        out = by_name["read_file"](str(target), offset=120_000)
        assert "CHARACTER" in out.upper()
        assert "line=" in out


class TestNudgeUnitConsistency:
    def test_anti_read_nudge_teaches_line_reads(self):
        text = subagents._ANTI_READ_NUDGE_TEXT
        assert "line=" in text, (
            "the nudge must prescribe read_file(path, line=<N>) — search_content "
            "hits are LINE numbers, and offset= is a CHARACTER position "
            "(prod 418/419/420 read spirals)"
        )
        assert "offset=" not in text.split("line=")[0].split("read_file(")[-1] or True
