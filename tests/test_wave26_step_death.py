"""[wave-26 W26-5] Step-death fast path in the codegen loop.

Prod 418/419/420: the writer died on langgraph's step budget AFTER the
tester had already failed the draft — the invocation wrote zero bytes, the
draft stayed byte-identical to the tested version, and the node still
routed to code_tester, burning a full tester cycle (5-15 min) re-testing
code that could not have changed. Two defects:

1. the no-op gate read the PRE-node ``state["test_retry_count"]`` while the
   node had already bumped ``update["test_retry_count"]`` — the "escalate on
   the first no-op of the final round" rule fired one round late;
2. a step-budget death with an unchanged fingerprint was treated exactly
   like a healthy no-op — guaranteed-identical re-test.
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

from agents.constants import (  # noqa: E402
    FINAL_RETRY_SENTINEL,
    MAX_TEST_RETRIES,
)
import agents.graph as graph_mod  # noqa: E402
import pytest  # noqa: E402

ROUTER_SRC = os.path.join(ROOT, "webapp", "agents", "graph.py")


def _block_between(src: str, start_marker: str, end_marker: str) -> str:
    i = src.find(start_marker)
    assert i != -1, f"start marker missing: {start_marker!r}"
    j = src.find(end_marker, i)
    assert j != -1, f"end marker missing: {end_marker!r}"
    return src[i:j]


class TestStepDeathEscalation:
    def test_step_death_with_unchanged_draft_escalates_immediately(self):
        """418's cycle 2: writer died of steps, draft byte-identical to the
        tested version — the next tester cycle is guaranteed waste."""
        assert (
            graph_mod._noop_should_escalate(1, 0, step_death=True) is True
        ), "step-death + unchanged fingerprint must escalate on the FIRST cycle"

    def test_step_death_without_noop_does_not_escalate(self):
        """A step death only matters when the draft didn't change; the gate
        call site passes noop>=1 inside the unchanged-fingerprint branch, so
        noop=0 + step_death is a misuse — keep the guard."""
        assert graph_mod._noop_should_escalate(0, 0, step_death=True) is False

    def test_healthy_noop_semantics_unchanged(self):
        """No regression: without a step death, escalation stays on the
        second consecutive no-op, or the first of the final round."""
        assert graph_mod._noop_should_escalate(1, 0) is False
        assert graph_mod._noop_should_escalate(1, MAX_TEST_RETRIES) is True
        assert graph_mod._noop_should_escalate(2, 0) is True
        assert graph_mod._noop_should_escalate(0, FINAL_RETRY_SENTINEL) is False


class TestCallSiteWiring:
    """The gate must see the BUMPED retry count and the step-death signal."""

    def setup_method(self):
        self.src = open(ROUTER_SRC).read()

    def test_gate_receives_bumped_retry_count(self):
        block = _block_between(
            self.src,
            "if _noop_should_escalate(",
            "_noop_note = (",
        )
        assert 'update.get("test_retry_count"' in block, (
            "the no-op gate reads the PRE-node state retry count; the node "
            "bumps update['test_retry_count'] first — the final-round rule "
            "fires one round late on the stale value (prod 418/419/420)"
        )

    def test_gate_receives_step_death_signal(self):
        block = _block_between(
            self.src,
            "if _noop_should_escalate(",
            "_noop_note = (",
        )
        assert "step_death=" in block, (
            "the no-op gate must know the invocation ended on step-budget "
            "exhaustion to skip the guaranteed-identical re-test"
        )
        assert "_writer_hit_step_budget" in block, (
            "step_death must come from the W25-c detector, not a guess"
        )

    def test_escalation_note_names_step_death(self):
        """Honesty: when the fast path fires, the escalation note must say
        WHY (dead writer, unchanged draft) — not a generic no-op message."""
        block = _block_between(
            self.src,
            "_noop_note = (",
            '_notify_phase(job_id, "code_writer", "failed")',
        )
        assert "step" in block.lower(), (
            "the escalation note must mention the step-budget death so the "
            "human approval (or cleanup report) shows the real mechanism"
        )
