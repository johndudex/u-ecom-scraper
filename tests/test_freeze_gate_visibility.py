"""Wave-23 W23-5.2: the identical-draft freeze gate must be job-log visible.

RC-5 of docs/plans/wave23-writer-convergence-plan.md (prod 374 forensics):
the gate's only trace was a ``logger.warning`` — container stdout, NOT the
job log. The RCA agent scanning /jobs/<id>/logs/ + /tool-calls/ found ZERO
trace of the hash comparison and had to infer byte-identity from the absence
of mutation calls. A job-visible SessionLog row makes every future 374-style
plateau self-documenting.

Convention: pure-python source extraction, per test_job73_freshness_and_noop
(the gate sits deep in _invoke_code_writer and is not drivable without the
full graph).

Run from repo root:  python3 -m pytest tests/test_freeze_gate_visibility.py -v
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPH = os.path.join(ROOT, "webapp", "agents", "graph.py")


def _invoke_writer_body() -> str:
    src = open(GRAPH).read()
    m = re.search(r"^def _invoke_code_writer\(", src, re.M)
    assert m, "_invoke_code_writer not found in graph.py"
    # cut the function body: from its def to the next top-level def
    nxt = re.search(r"^def ", src[m.start() + 1:], re.M)
    end = m.start() + 1 + nxt.start() if nxt else len(src)
    return src[m.start():end]


class TestFreezeGateJobLogVisibility:
    def test_noop_gate_emits_sessionlog_row(self):
        body = _invoke_writer_body()
        gate = re.search(
            r"draft is UNCHANGED from the last\s*\n?\s*.*tested version", body
        ) or re.search(r"UNCHANGED from the last", body)
        assert gate, "freeze gate warning not found — gate moved?"
        window = body[gate.start(): gate.start() + 2500]
        assert "_log_event_row(" in window, (
            "the no-op gate must write a SessionLog row (_log_event_row), "
            "not only logger.warning (container stdout is not the job log)"
        )
        assert "[NO-OP FIX CYCLE" in window, (
            "the row must carry the NO-OP FIX CYCLE marker so /jobs/<id>/logs/ "
            "self-documents the plateau"
        )
        assert "_new_fp" in window, (
            "the row must include the draft fingerprint for diffing cycles"
        )

    def test_wall_clock_reaper_emits_sessionlog_row(self):
        # Control (existing behavior): the INVOKE-TIMEOUT row already lands in
        # the job log — pin it so the no-op gate matches its precedent.
        src = open(GRAPH).read()
        assert "INVOKE-TIMEOUT" in src
        assert "_log_event_row(" in src


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
