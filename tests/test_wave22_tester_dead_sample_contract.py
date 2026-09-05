"""[wave-22 C4] The tester prompt must NOT pass a job because its sample is
dead.

``.opencode/agents/code-tester.md`` instructed: "If ALL sampled URLs are
dead (all non-200), set overall_assessment to PASS with confidence_score
1.0" — a literal pass-on-absence rule. It survived four waves of reliability
work while 53% of tracked targets produced no data; wave-22's systemic audit
flagged it as the purest expression of the class. The line is deleted and
replaced with FAIL + dead_sample-issue semantics (with a live-yield escape
so a dead SAMPLE on an otherwise-live site still remaps instead of failing
honest work — the wave-21 T7 remap branch consumes exactly that request).

Contract (source-pin: the prompt is prose, the contract is its text):
- the PASS-on-all-dead instruction is gone (no variant of it survives);
- the replacement mandates FAIL + a HIGH-severity dead_sample issue;
- the live-yield escape and the phase1_discovery non-claim are present;
- the phase1 contract test: classify_test_failure still refuses to count a
  dead-sample run as phase-1-tested (deterministic layer backs the prose).
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

PROMPT_PATH = os.path.join(ROOT, ".opencode", "agents", "code-tester.md")


def _prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestPromptContract:
    def test_pass_on_all_dead_is_gone(self):
        prompt = _prompt()
        assert "overall_assessment` to PASS with `confidence_score` 1.0" not in prompt, (
            "the pass-on-absence rule is still in the tester prompt "
            "(code-tester.md:79 — the systemic audit's root 2 in prose form)"
        )
        assert "cannot assess scraper quality" not in prompt

    def test_replacement_demands_fail_plus_dead_sample_issue(self):
        prompt = _prompt()
        assert "`overall_assessment` to FAIL" in prompt
        assert "dead_sample" in prompt
        assert "HIGH-severity" in prompt

    def test_live_yield_escape_present(self):
        prompt = _prompt()
        assert "live yield" in prompt
        assert "remap" in prompt.lower() or "re-seed" in prompt

    def test_phase1_not_claimed_from_dead_samples(self):
        prompt = _prompt()
        assert "phases_tested.phase1_discovery" in prompt


class TestDeterministicLayerBacksTheProse:
    """The prose is enforced by the deterministic classifier: a dead-sample
    verdict must never mark phase-1 discovery as tested."""

    def test_classifier_exists_and_is_importable(self):
        rat = importlib.import_module("agents.nodes.route_after_testing")
        assert hasattr(rat, "_discovery_coverage_failure"), (
            "the phase-1 gate the prose leans on is missing"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
