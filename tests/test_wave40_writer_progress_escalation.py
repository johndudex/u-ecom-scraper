"""[wave-40 T12] WRITER_PROGRESS_ESCALATION: one capped, clamped extra writer
window when the run shows verifiable convergence (prod 765: cycle-3 PASS
36/36 destroyed by the 2-strike abort). Gate signal is the tester-stamped
last_tested_draft_fp — tested_draft_sha256 is EMPTY on exactly these runs
(it is only written at execution launch). Default OFF.

Run: docker compose exec django sh -c "cd /app/webapp && pytest ../tests/test_wave40_writer_progress_escalation.py -q"

ADAPT notes (vs the plan's test text, both mechanical):
- the ROOT/sys.path/django.setup() boilerplate every sibling file in tests/
  carries — without it ``from agents import graph`` cannot resolve when the
  suite is invoked as ``pytest ../tests .`` from /app/webapp;
- ``test_grant_clamped_by_remaining_job_budget`` uses a 900s-out deadline
  instead of 30s: _effective_timeout reserves JOB_BUDGET_FINALIZE_MARGIN
  (360s), so a deadline 30s out leaves a NEGATIVE remaining budget and the
  clamp correctly refuses to grant at all (pinned separately below, as the
  exhaustion arm). The clamp mechanism under test — the real
  _effective_timeout, margin honored — is unchanged.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()  # django.utils.timezone + agents.graph need configured settings

from django.utils import timezone  # noqa: E402

from agents import graph  # noqa: E402
from agents.state import ScrapeState  # noqa: E402


def _deadline(seconds):
    return timezone.now() + timedelta(seconds=seconds)


def _progress_state(**over):
    st = {"job_id": 0, "site_slug": "nike-in", "test_retry_count": 2,
          "last_tested_draft_fp": "fp-abc123",
          "draft_mutated_during_test": False,
          "noop_fix_cycles": 0, "writer_wall_clock_timeouts": 2,
          "writer_escalation_used": False}
    st.update(over)
    return st


def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "0")
    assert graph._writer_escalation_window(
        _progress_state(), _deadline(600)) is None


def test_flag_on_grants_one_doubled_window(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    win = graph._writer_escalation_window(_progress_state(), _deadline(600))
    assert win and win > 0


def test_gate_signal_is_last_tested_draft_fp(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    # the 765 shape: execution never ran -> no sha, but the tester stamped
    # the fingerprint on the converging cycle-3
    assert graph._writer_escalation_window(
        _progress_state(tested_draft_sha256=""), _deadline(600)) is not None
    assert graph._writer_escalation_window(
        _progress_state(last_tested_draft_fp=""), _deadline(600)) is None


def test_no_progress_signals_no_grant(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    assert graph._writer_escalation_window(
        _progress_state(draft_mutated_during_test=True), _deadline(600)) is None
    assert graph._writer_escalation_window(
        _progress_state(noop_fix_cycles=2), _deadline(600)) is None


def test_grant_clamped_by_remaining_job_budget(monkeypatch):
    """[ADAPT] 900s out so the finalize margin leaves a real remainder: the
    grant must come back clamped to ``deadline - now - margin`` — strictly
    below both the raw remaining budget and the unclamped doubled window."""
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    win = graph._writer_escalation_window(_progress_state(), _deadline(900))
    assert 0 < win <= 900  # never past the job's remaining budget ...
    assert win <= 900 - graph.JOB_BUDGET_FINALIZE_MARGIN  # ... margin honored
    assert win < 2 * graph._writer_invoke_timeout()  # ... actually clamped


def test_grant_refused_when_only_the_finalize_margin_remains(monkeypatch):
    """[ADAPT companion] 30s out is INSIDE JOB_BUDGET_FINALIZE_MARGIN — the
    clamp's exhaustion arm returns 0.0 and the grant must be None, never a
    window that eats the finalize reserve (this is the honest version of the
    plan's ``0 < win <= 30`` arithmetic, which the margin makes impossible)."""
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    assert graph._writer_escalation_window(
        _progress_state(), _deadline(30)) is None


def test_second_strike_never_escalates(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    assert graph._writer_escalation_window(
        _progress_state(writer_escalation_used=True), _deadline(600)) is None


# ───────────── wiring pins (T12: latch, abort-block wiring, hardening) ─────────────


def test_writer_escalation_used_channel_is_declared():
    """The latch rides graph state between cycles — an undeclared key would be
    stripped by langgraph and read False forever (unbounded re-grant)."""
    assert "writer_escalation_used" in ScrapeState.__annotations__


def _writer_two_strike_arm() -> str:
    """The ``_wc >= 2`` abort arm of _invoke_code_writer (boundary = the
    healthy-return counter reset that follows it)."""
    with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
        src = fh.read()
    i = src.index("if _wc >= 2:")
    j = src.index("if not _cw_dead:", i)
    return src[i:j]


def test_two_strike_arm_wires_the_opt_in_gate():
    """The abort arm must consult the helper BEFORE escalating, stamp the
    agent-log row and the latch — and the wiring must live behind the helper,
    so with the flag off the arm is byte-identical to today."""
    arm = _writer_two_strike_arm()
    assert "_writer_escalation_window(" in arm, (
        "the 2-strike abort never consults the opt-in escalation gate"
    )
    assert "[WRITER-PROGRESS-ESCALATION]" in arm, (
        "the grant must stamp a job-visible [WRITER-PROGRESS-ESCALATION] row"
    )
    assert 'update["writer_escalation_used"] = True' in arm, (
        "the latch must be stamped when the window is granted — the second "
        "strike must never escalate"
    )


def test_no_report_branch_clears_fast_fail_detail():
    """[T1 parked finding, ledgered for T12] The tester's REPORT branch clears
    fast_fail_detail; the NO-REPORT cycle that does not qualify for the A5
    fast-fail never did, so a detail stamped in cycle N survived into a later
    no-report cycle and misfired the (now-declared, SCRAPER_FAST_FAIL-gated)
    arm. Clearing it is inert while the flags are off and de-mines the arm."""
    with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
        src = fh.read()
    i = src.index("def _invoke_code_tester")
    j = src.index("\ndef ", i + 10)
    tester = src[i:j]
    k = tester.index('update["test_report"] = None')
    window = tester[k:k + 700]
    assert 'update["fast_fail_detail"] = ""' in window, (
        "the no-report branch leaves a prior cycle's fast_fail_detail "
        "standing — a stale detail rides into the next cycle's A5 decision"
    )
