"""Shared fixtures for the repo-root test suite (tests/).

The webapp suite has webapp/conftest.py (django); this root suite runs
pure-unit tests against ``webapp`` sources via PYTHONPATH, so it needs its
own conftest for cross-test process hygiene.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disarm_invocation_cancel():
    """No test may leak the invocation-cancel latch to its neighbours.

    [job-329 wall] The writer deadline guard latches a process-global cancel
    flag that — by design — survives ``clear_tool_context`` (the node's
    finally runs while the zombie thread lives) and is only re-armed by the
    NEXT ``set_tool_context``. Any test exercising a wall-clock abandon path
    can therefore latch the flag and poison every later tool-using test in
    the same pytest process (seen live: tests/test_skills_guards.py refusing
    writes with ``[invocation-cancelled]`` after an unrelated timeout test).

    Re-arm on BOTH sides of every test: setup protects against pollution from
    earlier files, teardown protects later ones. Production semantics are
    untouched — this only constrains the pytest process.

    [wave-24 W24-1] The latch gained a per-invocation cancelled-id set that
    ``set_tool_context`` deliberately never resets — purge it too, via the
    dedicated test-support reset.
    """
    from agents.tools.context import (
        is_invocation_cancelled,
        reset_invocation_latches,
        set_tool_context,
    )

    def _rearm():
        if is_invocation_cancelled():
            set_tool_context({}, agent_name="")
        reset_invocation_latches()

    _rearm()
    yield
    _rearm()
