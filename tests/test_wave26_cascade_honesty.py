"""[wave-26 W26-8] Cascade honesty: every routing decision leaves a trace.

Prod 410 (karenmillen): the tester PASSED (0.82, 1 real product, 48 URLs)
but the exhausted-cascade skip_approvals arm returned ``cleanup`` SILENTLY —
no ``[CASCADE]`` row, so the jump from testing-end to cleanup looked
unexplained and cost a forensic session to reconstruct. The wave-22 B5 rule
says every routing decision leaves a forensic trace; three terminal arms
still only ``logger.error``. Also: the anti-bot strategy downgrade rewrote
the cascade reason to blame the bot wall even when the tester's own
remediation.target said "scraper" (prod 420/419 mislabels), and
``_diagnose_no_execution``'s PASS branch claimed an "interrupt/resume gap"
that never happened (410's error string was a lie about the mechanism).
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTER = os.path.join(ROOT, "webapp", "agents", "nodes", "route_after_testing.py")
TASKS = os.path.join(ROOT, "webapp", "scraper", "tasks.py")


def _block_between(src: str, start_marker: str, end_marker: str) -> str:
    i = src.find(start_marker)
    assert i != -1, f"start marker missing: {start_marker!r}"
    j = src.find(end_marker, i)
    assert j != -1, f"end marker missing: {end_marker!r}"
    return src[i:j]


class TestSilentArmsEmitCascadeRows:
    """The three terminal arms that only logger.error must call _log_cascade."""

    def setup_method(self):
        self.src = open(ROUTER).read()

    def test_contract_exhausted_arm_logs(self):
        block = _block_between(
            self.src,
            "CLI contract violation + retries",
            "if is_final_attempt:",
        )
        assert "_log_cascade(" in block, (
            "the CLI-contract-exhausted arm returns cleanup/human_approval "
            "silently — every routing decision needs a [CASCADE] row (prod 410)"
        )

    def test_final_attempt_arm_logs(self):
        block = _block_between(
            self.src,
            "if is_final_attempt:",
            "── Strategy cascade ──",
        )
        assert "_log_cascade(" in block, (
            "the final-attempt arm returns cleanup/human_approval silently"
        )

    def test_cascade_exhausted_arm_logs_with_rescue_numbers(self):
        block = _block_between(
            self.src,
            "retries exhausted in cascade",
            '[A1/QW-3] "retest"',
        )
        assert "_log_cascade(" in block, (
            "the cascade-exhausted skip_approvals arm (prod 410's silent "
            "cleanup) must emit a [CASCADE] row"
        )
        # the reason must carry the rescue's numeric result (items seen vs
        # min_count) so the next RCA doesn't re-derive it
        assert "min_count" in block, (
            "the exhausted-cleanup row must name the rescue min_count"
        )


class TestAntiBotDowngradeHonesty:
    def test_downgrade_honors_remediation_target(self):
        """Prod 420 cycle 2: tester said remediation.target=scraper (a
        routing bug in the draft) but the cascade row said 'anti-bot site' —
        inviting a cloak/proxy detour instead of the code fix."""
        src = open(ROUTER).read()
        i = src.find('_action = "scraper"')
        assert i != -1, "anti-bot downgrade site moved"
        window = src[max(0, i - 600): i + 600]
        assert "remediation" in window, (
            "the anti-bot downgrade must consult the tester's "
            "remediation.target before blaming the bot wall"
        )
        assert 'target=scraper' in window or "_rem_target_down" in window, (
            "the downgrade must have a remediation.target=scraper branch"
        )


class TestDiagnosePassWording:
    def _load(self):
        src = open(TASKS).read()
        m = re.search(r"^def _diagnose_no_execution\(.*?(?=^def |\Z)", src, re.M | re.S)
        assert m, "_diagnose_no_execution not found"
        import json as _json
        import types

        art = types.SimpleNamespace(
            scrapers_key=lambda *a: os.path.join(*a),
            exists=lambda p: os.path.isfile(str(p)),
            read_json=lambda p: _json.load(open(p)),
        )
        ns = {
            "json": _json,
            "os": os,
            "Any": object,
            "artifacts": art,
            "_json": _json,
            "_os": os,
        }
        exec(m.group(0), ns)
        return ns["_diagnose_no_execution"]

    def test_pass_branch_does_not_claim_interrupt_resume_gap(
        self, tmp_path, monkeypatch
    ):
        """Prod 410's error string said 'interrupt/resume gap' — false: the
        ROUTER chose cleanup. The wording must point at the cascade rows."""
        ws = tmp_path / "workspace" / "acme-com"
        ws.mkdir(parents=True)
        import json

        (ws / "test_report.json").write_text(
            json.dumps(
                {
                    "overall_assessment": "PASS",
                    "confidence_score": 0.82,
                    "issues": [],
                }
            )
        )
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        fn = self._load()
        msg = fn("acme-com", 424242)
        assert "interrupt/resume gap" not in msg
        assert "cascade" in msg or "router" in msg.lower()
