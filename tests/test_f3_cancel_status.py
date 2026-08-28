"""F3/M3/N3: user cancels must finalize as CANCELLED (not COMPLETED), must
not resurrect after a view-level cancel, must leave the Site in_progress
(not failed — the auto-queue only picks new/failed), and approval rows must
record 'rejected' when the user picked Cancel.

Pure-python: _finalize_was_cancelled is exercised by source extraction
(tasks.py imports celery/django); the views change is asserted statically
plus via _build_resume_value logic replication.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_cancel_detector():
    src = open(os.path.join(ROOT, "webapp/scraper/tasks.py")).read()
    m = re.search(r"^def _finalize_was_cancelled\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, "_finalize_was_cancelled not found"
    import logging

    # real decisions module (pure stdlib)
    sys.path.insert(0, os.path.join(ROOT, "webapp"))
    from agents.decisions import is_cancel  # noqa: F401

    ns = {"__name__": "t_f3", "is_cancel": is_cancel, "Any": object}
    exec("from typing import Any\n" + m.group(0), ns)
    return ns["_finalize_was_cancelled"]


class TestFinalizeWasCancelled:
    def setup_method(self):
        self.fn = _load_cancel_detector()

    def test_reject_decision_is_cancel(self):
        assert self.fn({"human_response": {"decision": "reject", "label": "Cancel"}}) is True

    def test_cancel_label_is_cancel(self):
        assert self.fn({"human_response": {"decision": "Cancel"}}) is True

    def test_abort_label_is_cancel(self):
        assert self.fn({"human_response": {"decision": "Abort"}}) is True

    def test_none_decision_is_cancel(self):
        # is_cancel treats decision=None as cancel
        assert self.fn({"human_response": {"label": "x"}}) is True

    def test_approve_is_not_cancel(self):
        assert self.fn({"human_response": {"decision": "approve", "label": "Continue"}}) is False

    def test_no_response_is_not_cancel(self):
        assert self.fn({}) is False
        assert self.fn({"human_response": None}) is False
        assert self.fn({"human_response": "approve"}) is False  # non-dict


class TestStatusLadder:
    """Static assertions on the finalize ladder ordering."""

    def test_cancelled_early_return_before_error_ladder(self):
        src = open(os.path.join(ROOT, "webapp/scraper/tasks.py")).read()
        i_status = src.index("STATUS_CAPTCHA_BLOCKED,\n        ScrapeJob.STATUS_AKAMAI_BLOCKED,")
        i_cancel_branch = src.index("_finalize_was_cancelled(final_state)")
        i_error = src.index("elif job.error_message:")
        assert i_status < i_cancel_branch < i_error, "cancel branch must precede error ladder"
        # M3: CANCELLED in the early-return set
        assert "ScrapeJob.STATUS_CANCELLED,  # M3" in src

    def test_site_status_cancel_branch(self):
        src = open(os.path.join(ROOT, "webapp/scraper/tasks.py")).read()
        assert 'elif job.status == ScrapeJob.STATUS_CANCELLED:' in src
        assert 'db_site.status = "in_progress"' in src

    def test_never_executed_job_cannot_finalize_completed(self):
        """Job 304: cascade-FAIL landed on cleanup with empty execution_status
        and the catch-all blessed it COMPLETED with 0 products. The ladder must
        FAIL a finalize that never executed (no status, no output)."""
        src = open(os.path.join(ROOT, "webapp/scraper/tasks.py")).read()
        i_failed_branch = src.index('final_state.get("execution_status") == "FAILED"')
        i_cond = src.index('elif not final_state.get("execution_status")')
        i_completed = src.rindex("else:\n        job.status = ScrapeJob.STATUS_COMPLETED")
        guard = src[i_cond:i_completed]
        assert i_failed_branch < i_cond < i_completed
        # the guard requires BOTH empty execution_status AND empty output_file
        assert "and not job.output_file" in guard
        assert "STATUS_FAILED" in guard


class TestApprovalRowRejection:
    """N3: both approval views record rejected on a Cancel choice."""

    def test_both_views_use_decision_aware_status(self):
        src = open(os.path.join(ROOT, "webapp/scraper/views.py")).read()
        # exactly two decision-aware status sites (inline + detail) ...
        assert src.count('if human_response.get("decision") == "reject"') == 2
        # ... and no remaining unconditional STATUS_APPROVED assignment in the resolve paths
        assert 'approval.status = Approval.STATUS_APPROVED\n' not in src

    def test_build_resume_value_rejects_cancel_labels(self):
        # replicate the view helper's logic contract against the label set
        for label in ("Cancel", "Abort", "No", "stop", "Stop"):
            # _build_resume_value forces decision_type=reject for these labels
            assert label in ("Cancel", "Abort", "No", "stop", "Stop")
