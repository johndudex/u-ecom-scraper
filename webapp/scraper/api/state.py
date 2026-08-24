"""The 4-state partner projection (sync_api.yaml JobState).

Pure functions — no I/O beyond the injectable sample-signal predicate.
Both specs' tables map identically (verified rounds 1+2):

    pending, running, waiting_approval → inprogress
    completed                          → scraper_ready
    failed, cancelled, captcha_blocked, akamai_blocked → failed

sample_ready is derived (REST) from the testing step's first completion,
STATE-GATED to non-terminal jobs: the finalize close-block stamps
completed_at on never-run steps (tasks.py step-close), which would
otherwise report sample_available:true for jobs that never tested while
the event stream correctly emits nothing (round-2 verifier's sharpest
finding). The gate makes REST agree with events.
"""
from __future__ import annotations

TERMINAL_INTERNAL = {"completed", "failed", "cancelled", "captcha_blocked", "akamai_blocked"}

_STATE_MAP = {
    "pending": "inprogress",
    "running": "inprogress",
    "waiting_approval": "inprogress",
    "completed": "scraper_ready",
    "failed": "failed",
    "cancelled": "failed",
    "captcha_blocked": "failed",
    "akamai_blocked": "failed",
}


def partner_state(internal_status: str) -> str:
    """Internal ScrapeJob.status → the 4-state projection."""
    return _STATE_MAP.get(internal_status, "failed")


def failure_code(internal_status: str) -> str:
    """failed/cancelled/captcha_blocked/akamai_blocked → distinct failure.code."""
    return {
        "failed": "pipeline_failed",
        "cancelled": "cancelled",
        "captcha_blocked": "captcha_blocked",
        "akamai_blocked": "akamai_blocked",
    }.get(internal_status, "pipeline_failed")


def sample_ready(job, testing_done) -> bool:
    """True only while the job is live AND testing has completed once.

    job: the ScrapeJob; testing_done: bool — the caller resolves the
    Step(testing).completed_at IS NOT NULL signal (kept injectable for
    tests). The status gate is the m4 fix: terminal jobs with finalize-
    stamped steps must not claim a sample they never produced.
    """
    return job.status in ("running", "waiting_approval", "pending") and bool(testing_done)


# The Phase enum published by the sync API (sync_api.yaml Phase.phase).
# Step.phase values are the enum tokens post-0035; anything else (legacy
# rows predating the merge, future phases) is normalized defensively.
PHASE_ENUM = {
    "accessibility_check", "site_analysis", "browser_traverse",
    "navigation_skill_review", "navigation_analysis", "content_analysis",
    "product_analysis", "scraper_analysis", "code_generation",
    "code_review", "testing", "field_confirmation", "execution",
    "cleanup", "skill_learning", "dagster_converter", "store_job_listings",
}


def normalize_phase(phase: str) -> str:
    """Legacy display strings → enum token; unknown → closest known."""
    fixes = {"Browser Navigation": "browser_traverse", "Code Review": "code_review"}
    p = fixes.get(phase, phase)
    return p if p in PHASE_ENUM else "cleanup"
