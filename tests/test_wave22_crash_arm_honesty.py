"""[wave-22 B4] The discovery-probe crash arm must be as honest as its
siblings — and no force-FAIL arm may destroy the tester's confidence.

Job 338: the crash arm is the only force-FAIL lane that never sets
``error_message``/``execution_status``, so when the cascade died the job row
carried the generic "Pipeline ended before execution" (tasks.py's fallback)
instead of the actual discovery traceback. The same arm — and the zero-yield
and CLI/ladder arms — also overwrite ``confidence_score`` to 0.0 outright,
which erases the tester's own verdict (338's tester had scored the draft 0.97
on real phase-2 evidence before the probe zeroed it) and leaves downstream
routing unable to distinguish "tester failed it" from "the probe overrode a
passing verdict".

Contract (arm-by-arm, inside ``_invoke_code_tester``):
- crash arm: preserve the pre-overwrite ``confidence_score`` as
  ``phase2_confidence`` BEFORE zeroing; tag ``discovery_unvalidated``; on the
  final attempt set ``error_message`` + ``execution_status="FAILED"`` exactly
  like the zero-yield arm right below it;
- zero-yield arm: same preservation + tag (its error_message arm exists);
- CLI/ladder arm: same preservation (its error_message arm exists).
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

graph = importlib.import_module("agents.graph")  # noqa: E402


def _tester_src() -> str:
    with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
        return fh.read()


def _crash_arm() -> str:
    src = _tester_src()
    i = src.index("if crashed:")
    j = src.index("# Job-311 lesson", i)
    return src[i:j]


def _zero_yield_arm() -> str:
    src = _tester_src()
    i = src.index("elif probe_yield is not None and _probe_yield_dead(probe_yield):")
    j = src.index("elif probe_yield is not None and not isinstance(report, dict):", i)
    return src[i:j]


def _cli_arm() -> str:
    src = _tester_src()
    i = src.index("if _cli_violation:")
    j = src.index("# [job-329 wall]", i)
    return src[i:j]


class TestCrashArmHonesty:
    def test_preserves_confidence_before_zeroing(self):
        arm = _crash_arm()
        i_pres = arm.find("phase2_confidence")
        i_zero = arm.index('report["confidence_score"] = 0.0')
        assert i_pres != -1 and i_pres < i_zero, (
            "crash arm zeroes confidence_score without preserving the "
            "tester's own verdict as phase2_confidence (338: a 0.97 PASS "
            "was erased before routing could see it)"
        )

    def test_final_attempt_sets_named_error(self):
        arm = _crash_arm()
        assert 'update["error_message"]' in arm, (
            "crash arm never names the failure — job rows die with the "
            "generic 'Pipeline ended before execution' fallback (338)"
        )
        assert 'update["execution_status"] = "FAILED"' in arm

    def test_final_attempt_gate_mirrors_siblings(self):
        arm = _crash_arm()
        assert "FINAL_RETRY_SENTINEL" in arm and "MAX_TEST_RETRIES" in arm, (
            "crash-arm honesty must fire on the SAME final-attempt gate the "
            "zero-yield arm uses — not on every crash (early crashes must "
            "still route back to code_writer with the traceback)"
        )

    def test_tags_discovery_unvalidated(self):
        assert "discovery_unvalidated" in _crash_arm()


class TestZeroYieldAndCliArms:
    def test_zero_yield_preserves_confidence(self):
        arm = _zero_yield_arm()
        i_pres = arm.find("phase2_confidence")
        i_zero = arm.index('report["confidence_score"] = 0.0')
        assert i_pres != -1 and i_pres < i_zero

    def test_zero_yield_tags_discovery_unvalidated(self):
        assert "discovery_unvalidated" in _zero_yield_arm()

    def test_cli_ladder_arm_preserves_confidence(self):
        arm = _cli_arm()
        i_pres = arm.find("phase2_confidence")
        i_zero = arm.index('report["confidence_score"] = 0.0')
        assert i_pres != -1 and i_pres < i_zero, (
            "the CLI/ladder arm also zeroes confidence — the other 338-shape "
            "escape remains dead unless it preserves too"
        )


class TestPreservationShape:
    def test_preservation_keeps_original_value(self):
        """The preserved value must be the REPORT's pre-overwrite confidence
        (read before the zero), not a hardcoded constant."""
        for arm in (_crash_arm(), _zero_yield_arm(), _cli_arm()):
            i_pres = arm.index("phase2_confidence")
            snippet = arm[max(0, i_pres - 200): i_pres + 200]
            assert "confidence_score" in snippet, (
                "phase2_confidence must be assigned FROM confidence_score "
                "before the overwrite; got: " + snippet[:160]
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
