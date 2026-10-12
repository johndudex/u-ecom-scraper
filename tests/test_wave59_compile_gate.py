"""[wave-59 C3] Gate 0 — pre-write parseability at the _f821_rejections seam.

Prod 623: the writer's terminal bug (an unterminated string literal inside a
``re.compile`` regex — the same broken line written TWICE, draft lines
107/116) sailed through BOTH existing write gates — ruff emits invalid-syntax
with no F821 code (the findings filter drops it) and ``draft_call_violation``
returns "" on unparseable — so the broken write landed and two 2700s windows
died "no draft".

Gate 0 ``compile()``s the content pre-write and rejects with the exact
line:col + the repair contract. Bounded (critic-2): after
``_SYNTAX_REJECT_MAX`` consecutive rejections the block LIFTS (the write
proceeds; the phase-boundary syntax fixer owns the file) so a writer stuck in
a syntax loop is never wedged out of its window. The streak counts
REJECTIONS; a clean pass resets it. Falls OPEN on internal errors; kill
switch ``SCRAPER_DRAFT_SYNTAX_GATE=0``.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents.tools import filesystem_tools as fs  # noqa: E402


def _pad_to(line_no: int, bad_line: str) -> str:
    """A draft-shaped file with the broken line at ``line_no`` (1-based)."""
    lines = ["import re", ""]
    i = 0
    while len(lines) < line_no - 1:
        lines.append(f"def _pad_{i}():")
        lines.append(f"    return {i}")
        i += 1
    lines.append(bad_line)
    return "\n".join(lines) + "\n"


# 623's exact defect class: the regex literal's quote never closes, so the
# string swallows the rest of the call — unterminated string literal.
BAD_RE_LINE = 'CATEGORY_RE = re.compile(r"/collections/.+-g\\d+, re.I)'
GOOD_TAIL = (
    "\n\ndef main():\n"
    '    m = CATEGORY_RE.search("/collections/dresses-g42")\n'
    "    return bool(m)\n"
)


@pytest.fixture(autouse=True)
def _fresh_streak():
    """The streak is module state — isolate every test from its neighbors."""
    fs._syntax_reject_streak["n"] = 0
    yield
    fs._syntax_reject_streak["n"] = 0


class TestGate0Rejections:
    def test_unterminated_regex_string_rejected_with_line_col(self):
        content = _pad_to(107, BAD_RE_LINE) + GOOD_TAIL
        out = fs._syntax_gate_rejection(content)
        assert out.startswith("REJECTED — NOT applied; file unchanged"), (
            "623's terminal bug must be blocked pre-write"
        )
        assert "SYNTAX ERROR" in out
        assert "line 107" in out, f"rejection must carry the exact line: {out[:200]}"
        assert "unterminated" in out.lower()

    def test_clean_write_gate_open_and_streak_resets(self):
        assert fs._syntax_gate_rejection(BAD_RE_LINE + "\n")  # strike 1
        assert fs._syntax_gate_rejection(BAD_RE_LINE + "\n")  # strike 2
        assert fs._syntax_gate_rejection("X = 1\n") == ""
        assert fs._syntax_reject_streak["n"] == 0, "a clean pass resets the streak"

    def test_non_syntax_rejections_still_open(self):
        """An F821-class file (parses, undefined name) is NOT gate 0's
        business — gate 1 owns it."""
        assert fs._syntax_gate_rejection("print(undefined_name)\n") == ""


class TestBoundedStreak:
    def test_third_strike_escalates_to_rewrite_directive(self):
        for _ in range(2):
            assert "REJECTED" in fs._syntax_gate_rejection(BAD_RE_LINE + "\n")
        third = fs._syntax_gate_rejection(BAD_RE_LINE + "\n")
        assert "REJECTED" in third
        assert f"{fs._SYNTAX_REJECT_MAX} consecutive" in third
        assert "REWRITE the file fresh" in third

    def test_fourth_strike_lifts_the_block(self):
        for _ in range(fs._SYNTAX_REJECT_MAX):
            assert "REJECTED" in fs._syntax_gate_rejection(BAD_RE_LINE + "\n")
        assert fs._syntax_gate_rejection(BAD_RE_LINE + "\n") == "", (
            "the block must LIFT after the bounded streak — a wedged writer "
            "is worse than a bad write the backstop owns"
        )
        # and it stays lifted until a clean pass resets
        assert fs._syntax_gate_rejection(BAD_RE_LINE + "\n") == ""


class TestFallOpenAndKillSwitch:
    def test_internal_error_falls_open(self, monkeypatch):
        out = fs._syntax_gate_rejection(object())  # compile() raises TypeError
        assert out == "", "a checker malfunction must never block a write"

    def test_kill_switch_disarms_gate0_only(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_DRAFT_SYNTAX_GATE", "0")
        assert fs._syntax_gate_rejection(BAD_RE_LINE + "\n") == ""
        assert fs._syntax_reject_streak["n"] == 0, "disarmed gate must not count"

    def test_gate0_wired_first_in_the_f821_seam(self):
        """Gate 0 runs before the ruff subprocess — cheapest first, and the
        gates below cannot judge content that does not parse. [wave-60 N1]
        the seam's disposition logic lives in ``_f821_outcome`` now (the
        call-scoped ``(rejection, lift)`` tuple); the public
        ``_f821_rejections`` is its string wrapper — the invariant this pin
        guards (gate 0 before ruff, kill switch present) is unchanged."""
        src = open(
            os.path.join(ROOT, "webapp", "agents", "tools", "filesystem_tools.py")
        ).read()
        i_outcome = src.index("def _f821_outcome")
        i_ruff = src.index("subprocess.run(", i_outcome)
        i_gate0 = src.index("_syntax_gate_outcome(content)", i_outcome)
        assert i_outcome < i_gate0 < i_ruff
        assert 'os.getenv("SCRAPER_DRAFT_SYNTAX_GATE", "1")' in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
