"""Terminal-event reconciler (fold M2) — the outbox's safety net.

The explicit emit() sites cover the happy path, but status writes are
scattered (~20 sites) and a worker SIGKILL mid-finalize can leave a
terminal job with no terminal event — the partner would poll inprogress
forever. This beat-driven pass scans terminal jobs completed since the
last sweep and guarantees the terminal event EXISTS (idempotent via the
outbox's dedupe constraint; first-write-wins).

Keys on completed_at (backfilled by migration 0033-era data pass —
cancelled/failed rows had NULLs, which would have been invisible here).
Only created_via="api" jobs (emit's own gate also enforces this).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from ..models import ScrapeJob
from .emitter import emit

logger = logging.getLogger("scraper.events")

TERMINAL_EVENT_FOR = {
    ScrapeJob.STATUS_COMPLETED: ("job.scraper_ready", "scraper_ready"),
    ScrapeJob.STATUS_FAILED: ("job.failed", "pipeline_failed"),
    ScrapeJob.STATUS_CANCELLED: ("job.failed", "cancelled"),
    ScrapeJob.STATUS_CAPTCHA_BLOCKED: ("job.failed", "captcha_blocked"),
    ScrapeJob.STATUS_AKAMAI_BLOCKED: ("job.failed", "akamai_blocked"),
}
DEDUPE_FOR = {
    "job.scraper_ready": "scraper_ready",
    "job.failed": "failed",
}


def reconcile_terminal_events(window_minutes: int = 30) -> int:
    """Ensure every recently-terminal partner job has its terminal event.
    Returns the number of events created."""
    since = timezone.now() - timedelta(minutes=window_minutes)
    jobs = ScrapeJob.objects.filter(
        created_via="api",
        status__in=TERMINAL_EVENT_FOR.keys(),
        completed_at__gte=since,
    )
    created = 0
    for job in jobs.iterator():
        event_type, reason = TERMINAL_EVENT_FOR[job.status]
        dedupe = DEDUPE_FOR[event_type]
        # emit() is idempotent on (job, type, dedupe_key) — existing rows no-op
        if emit(job, event_type, {"reason": reason}, dedupe_key=dedupe) is not None:
            created += 1
            logger.info(
                "reconciler: backfilled %s for job %s (status=%s)",
                event_type, job.id, job.status,
            )
    return created


from ..tasks import shared_task  # noqa: E402


@shared_task(queue="events")
def dispatch_pending_callbacks() -> int:
    """Beat entry every 30s: claim due rows + enqueue deliveries, then run
    the reconciler in the same pass (cheap, bounded window)."""
    from .dispatcher import sweep

    dispatched = sweep()
    reconciled = reconcile_terminal_events()
    if dispatched or reconciled:
        logger.info(
            "events sweep: dispatched=%d reconciled=%d", dispatched, reconciled
        )
    return dispatched
