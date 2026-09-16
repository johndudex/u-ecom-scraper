"""[wave-34] The tester-side inconclusive probe must not crash the node.

Prod-shaped local hit (job 372 paige): the tester wrote a GOOD report, then
``_probe_phase1_discovery`` returned the wave-22 C1 inconclusive stamp
(``discovered_urls: None, stop_reason: inconclusive_blank_coverage``) because
the draft's ``--discover-only`` output carried blank coverage. The wave-16
deterministic-stamp arm assumed ``discovered_urls`` is always an int and did
``None > 0`` → TypeError → the whole job died with
"'>' not supported between instances of 'NoneType' and 'int'" AFTER testing
had succeeded.

Doctrine (wave-22 C1 extended): blank/inconclusive evidence manufactures
evidence in NEITHER direction — no zero-yield FAIL, and no phase1_discovery
boolean either. The tester's own report stands untouched; routing judges on
that.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))


def _graph_src() -> str:
    with open(os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8") as fh:
        return fh.read()


class TestInconclusiveProbeGuard:
    def test_deterministic_stamp_arm_requires_int_yield(self):
        """The real-yield arm must type-gate discovered_urls — the wave-22 C1
        inconclusive stamp carries None and would crash ``> 0``."""
        src = _graph_src()
        needle = 'elif probe_yield is not None and isinstance(\n            probe_yield.get("discovered_urls"), int\n        ):'
        assert needle in src, (
            "deterministic phase1 stamp arm lost its int type-gate — the "
            "inconclusive probe (discovered_urls=None) crashes the node again"
        )

    def test_inconclusive_still_owned_by_dead_tester_arm(self):
        """The dead-tester arm (report unset) must keep reading the yield via
        .get() — it already tolerates the inconclusive shape."""
        src = _graph_src()
        start = src.find('probe_yield is not None and not isinstance(report, dict)')
        assert start != -1, "dead-tester inconclusive arm moved"
        window = src[start : start + 1400]
        assert "probe_yield.get(\"discovered_urls\")" in window

    def test_real_yield_arm_never_indexes_discovered_urls_unchecked(self):
        """Inside the real-yield arm every discovered_urls use is via the
        already-gated key — but the comparison itself must be against the
        int we just type-checked."""
        src = _graph_src()
        start = src.find('probe_yield.get("discovered_urls"), int')
        assert start != -1
        end = src.find("phase1_discovery set \"\n", start)
        window = src[start : end if end != -1 else start + 4000]
        assert 'probe_yield["discovered_urls"] > 0' in window
        assert window.count("isinstance") == 1, (
            "guard drifted — re-check the arm still type-gates once"
        )


class TestProbeShapeContract:
    """The inconclusive stamp itself is wave-22 C1 doctrine — pin its shape
    so a future change re-arms the crash."""

    def test_inconclusive_stamp_carries_none_discovered_urls(self):
        src = _graph_src()
        start = src.find('"stop_reason": "inconclusive_blank_coverage"')
        assert start != -1, "inconclusive stamp moved"
        window = src[max(0, start - 300) : start + 100]
        assert '"discovered_urls": None' in window, (
            "inconclusive stamp must carry discovered_urls=None (never 0 — "
            "blank evidence must not arm the zero-yield FAIL)"
        )

    def test_inconclusive_stamp_flags_itself(self):
        src = _graph_src()
        start = src.find('"stop_reason": "inconclusive_blank_coverage"')
        window = src[start : start + 400]
        assert '"inconclusive": True' in window


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
