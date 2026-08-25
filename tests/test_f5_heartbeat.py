"""F5+M4: heartbeat timer chains must never leak.

F5: every _start_heartbeat call site wraps its invoke in try/finally so a
raise (DB outage mid-agent, factory failure) can't strand the
self-rescheduling Timer chain. M4: the chain self-terminates after
_HEARTBEAT_MAX_BEATS or once the job reaches a terminal status — belt and
braces for future copy-pasted call sites (prod job 333's leaked chain wrote
SessionLog rows every 5 minutes for days after its task died).

Pure-python static verification (no Django/langgraph importable here).
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _graph_src() -> str:
    return open(os.path.join(ROOT, "webapp/agents/graph.py")).read()


class TestF5CallSites:
    def test_every_start_has_finally_stop(self):
        src = _graph_src()
        lines = src.split("\n")
        sites = [i for i, ln in enumerate(lines) if "hb = _start_heartbeat" in ln]
        # Count is intentionally NOT pinned: new guarded call sites are added
        # over time (5th arrived with the CLI-contract fix loop, aa74f02).
        # The invariant under test is that EVERY site is finally-guarded.
        assert len(sites) >= 4, f"expected >= 4 call sites, found {len(sites)}"
        for i in sites:
            window = "\n".join(lines[i:i + 10])
            assert "finally:" in window, f"line {i+1}: no finally within 10 lines"
            assert "_stop_heartbeat(hb)" in window, f"line {i+1}: stop not in finally"

    def test_no_bare_start_stop_pattern_remains(self):
        src = _graph_src()
        # the old pattern: invoke directly between start and stop with no try
        bad = re.search(
            r"hb = _start_heartbeat\([^)]*\)\n"
            r"(?!\s*#)(?!\s*try:)"
            r"\s*result = ",
            src,
        )
        assert bad is None, f"unguarded start->invoke at offset {bad and bad.start()}"


class TestM4SelfTermination:
    def test_max_beats_cap_defined(self):
        src = _graph_src()
        assert "_HEARTBEAT_MAX_BEATS = 60" in src

    def test_beat_checks_cap(self):
        src = _graph_src()
        assert "handle.beats > _HEARTBEAT_MAX_BEATS" in src
        assert "self-terminating" in src

    def test_beat_checks_terminal_status(self):
        src = _graph_src()
        # the terminal-status query inside _beat
        assert "STATUS_COMPLETED, ScrapeJob.STATUS_FAILED," in src
        assert "_job_terminal" in src

    def test_handle_has_beats_counter(self):
        src = _graph_src()
        assert '__slots__ = ("stop", "timers", "beats")' in src
        assert "self.beats = 0" in src

    def test_terminal_check_skips_log_write(self):
        src = _graph_src()
        # the SessionLog write must be inside `if not _job_terminal:`
        i_check = src.index("if not _job_terminal:")
        i_write = src.index('content=f"{prefix} Agent {agent_name} still running...",')
        assert i_check < i_write

    def test_stop_before_reschedule(self):
        src = _graph_src()
        i_resched = src.index("timer = threading.Timer(interval, _beat)")
        i_stopcheck = src.index("if handle.stop.is_set() or _job_terminal:")
        assert i_stopcheck < i_resched
