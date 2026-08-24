"""Reconciler + beat/queue wiring (slice 1b, cycle 3).

Locks (fold M2 + B5 deploy mechanics):
- reconciler scans terminal jobs completed since the last sweep and
  guarantees a terminal outbox event EXISTS (worker SIGKILL / unwrapped
  status-write sites bypass the explicit emit paths)
- dedupe: a job that already has its terminal event is left alone
- keys on completed_at (the backfilled column) — no updated_at exists
- state mapping matches the 4-state projection exactly
- beat schedule carries dispatch_pending_callbacks at 30s
- CELERY_TASK_ROUTES sends deliver_callback to the events queue
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

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from scraper import models  # noqa: E402
from scraper.events.reconciler import reconcile_terminal_events  # noqa: E402


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_rec", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    return u, raw


def _job(u, status="completed", completed_minutes_ago=1, via="api"):
    return models.ScrapeJob.objects.create(
        url=f"https://www.example.com/{os.urandom(4).hex()}",
        user=u, created_via=via, status=status,
        input_mode="url_list", page_type="product",
        completed_at=timezone.now() - timedelta(minutes=completed_minutes_ago),
    )


class TestReconciler:
    def test_backfills_missing_terminal_event(self, partner, db):
        u, raw = partner
        job = _job(u, status="failed")
        created = reconcile_terminal_events()
        assert created >= 1
        row = models.EventOutbox.objects.filter(job=job, event_type="job.failed").first()
        assert row is not None
        assert row.payload["data"]["reason"] == "pipeline_failed"

    def test_completed_maps_to_scraper_ready(self, partner, db):
        u, raw = partner
        job = _job(u, status="completed")
        reconcile_terminal_events()
        row = models.EventOutbox.objects.filter(job=job, event_type="job.scraper_ready").first()
        assert row is not None

    def test_already_emitted_not_duplicated(self, partner, db):
        u, raw = partner
        job = _job(u, status="completed")
        from scraper.events import emit

        emit(job, "job.scraper_ready", {}, dedupe_key="scraper_ready")
        before = models.EventOutbox.objects.filter(job=job).count()
        reconcile_terminal_events()
        after = models.EventOutbox.objects.filter(job=job).count()
        assert before == after

    def test_cancelled_terminal(self, partner, db):
        u, raw = partner
        job = _job(u, status="cancelled")
        reconcile_terminal_events()
        row = models.EventOutbox.objects.filter(job=job, event_type="job.failed").first()
        assert row is not None
        assert row.payload["data"]["reason"] == "cancelled"

    def test_internal_jobs_ignored(self, partner, db):
        u, raw = partner
        job = _job(u, status="failed", via="intake")
        reconcile_terminal_events()
        assert models.EventOutbox.objects.filter(job=job).count() == 0

    def test_non_terminal_ignored(self, partner, db):
        u, raw = partner
        job = _job(u, status="running")
        job.completed_at = None
        job.save()
        reconcile_terminal_events()
        assert models.EventOutbox.objects.filter(job=job).count() == 0

    def test_window_respected(self, partner, db):
        """Jobs older than the scan window are not re-scanned (bounded work)."""
        u, raw = partner
        job = _job(u, status="failed", completed_minutes_ago=600)
        created = reconcile_terminal_events(window_minutes=60)
        assert models.EventOutbox.objects.filter(job=job).count() == 0


class TestBeatWiring:
    def test_beat_schedule_has_dispatcher(self):
        from django.conf import settings

        sched = settings.CELERY_BEAT_SCHEDULE
        assert any(
            "dispatch_pending_callbacks" in str(t) for t in sched.values()
        ), f"dispatcher missing from beat: {list(sched)}"

    def test_events_queue_routed(self):
        from django.conf import settings

        routes = getattr(settings, "CELERY_TASK_ROUTES", {})
        assert routes.get("scraper.events.dispatcher.deliver_callback") == "events"
        assert routes.get("scraper.events.dispatcher.dispatch_pending_callbacks") == "events"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
