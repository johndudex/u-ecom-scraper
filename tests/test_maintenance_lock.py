"""[maintenance-lock] Admin maintenance hold — new jobs stay PENDING.

User need (2026-09-05): teammates keep dispatching jobs while the operator
waits for a quiet system to merge+deploy (a Railway deploy restarts celery
and kills in-flight jobs). An admin needs a switch that:

- makes ``dispatch_scrape_job`` refuse to publish while the lock is ON —
  the row keeps the "never dispatched" signature (PENDING + celery_task_id="")
  so nothing downstream can mistake it for queued work;
- makes the redispatch sweep HOLD (not redispatch, and critically NOT
  honestly-fail exhausted rows) while the lock is ON;
- drains every held row the moment the lock is lifted, so pending work
  resumes automatically without anyone re-clicking anything.

The lock is DB-backed (survives deploys) and enforced at the ONE choke point
every dispatch site already routes through (wave-15 1.0 keystone contract).
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import Client  # noqa: E402

from scraper.models import ScrapeJob  # noqa: E402


# ─── helpers (wave-15 test patterns) ─────────────────────────────────────────


def _make_job(**kw):
    return ScrapeJob.objects.create(
        url=kw.pop("url", "https://example.com/p/1"),
        product_url=kw.pop("product_url", "https://example.com/p/1"),
        **kw,
    )


def _backdate(job, minutes):
    from django.utils import timezone

    ScrapeJob.objects.filter(pk=job.pk).update(
        created_at=timezone.now() - timezone.timedelta(minutes=minutes)
    )
    job.refresh_from_db()


@pytest.fixture
def lock():
    from scraper.models import MaintenanceLock

    row = MaintenanceLock.load()
    row.enabled = False
    row.reason = ""
    row.save(update_fields=["enabled", "reason", "updated_at"])
    return row


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("maint_admin", password="x")


@pytest.fixture
def regular_user(db):
    return User.objects.create_user("maint_peasant", password="x")


@pytest.fixture
def admin_client(superuser):
    c = Client()
    c.force_login(superuser)
    return c


# ─── model ───────────────────────────────────────────────────────────────────


class TestMaintenanceLockModel:
    @pytest.mark.django_db
    def test_defaults_to_disabled_and_load_is_singleton(self, lock):
        from scraper.models import MaintenanceLock

        assert MaintenanceLock.is_enabled() is False
        again = MaintenanceLock.load()
        assert again.pk == lock.pk  # same singleton row, not a new one

    @pytest.mark.django_db
    def test_enable_flips_is_enabled(self, lock):
        from scraper.models import MaintenanceLock

        lock.enabled = True
        lock.save(update_fields=["enabled", "updated_at"])
        assert MaintenanceLock.is_enabled() is True


# ─── dispatch gate ───────────────────────────────────────────────────────────


class TestDispatchGate:
    @pytest.mark.django_db
    def test_locked_dispatch_never_publishes_and_row_stays_never_dispatched(
        self, lock, monkeypatch
    ):
        import scraper.tasks as wt

        lock.enabled = True
        lock.save(update_fields=["enabled", "updated_at"])
        job = _make_job()

        publish = Mock()
        monkeypatch.setattr(wt.run_scrape_task, "apply_async", publish)

        task_id = wt.dispatch_scrape_job(job.id, rescrape=False)

        publish.assert_not_called()  # nothing reached the broker
        assert task_id == ""
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_PENDING
        # "never dispatched" signature stays pristine — the sweep/health
        # contracts discriminate on exactly this.
        assert job.celery_task_id == ""

    @pytest.mark.django_db
    def test_unlocked_dispatch_publishes_normally(self, lock, monkeypatch):
        import scraper.tasks as wt

        job = _make_job()
        monkeypatch.setattr(
            wt.run_scrape_task,
            "apply_async",
            lambda *a, **k: SimpleNamespace(id=k["task_id"]),
        )
        task_id = wt.dispatch_scrape_job(job.id)
        assert task_id != ""
        job.refresh_from_db()
        assert job.celery_task_id == task_id


# ─── sweep hold ──────────────────────────────────────────────────────────────


class TestSweepHold:
    @pytest.mark.django_db
    def test_sweep_holds_while_locked(self, lock, monkeypatch, settings):
        import scraper.tasks as wt

        settings.REDISPATCH_SWEEP_ENABLED = True
        lock.enabled = True
        lock.save(update_fields=["enabled", "updated_at"])

        abandoned = _make_job()
        _backdate(abandoned, wt.PENDING_CLAIM_MINUTES + 5)

        publish = Mock()
        monkeypatch.setattr(wt.run_scrape_task, "apply_async", publish)

        result = wt.redispatch_abandoned_pending()
        assert result["action"] == "maintenance_hold"
        publish.assert_not_called()
        abandoned.refresh_from_db()
        assert abandoned.status == ScrapeJob.STATUS_PENDING
        assert abandoned.redispatch_count == 0

    @pytest.mark.django_db
    def test_locked_sweep_never_fails_exhausted_rows(self, lock, monkeypatch, settings):
        """The honest-fail arm must not burn maintenance-held rows: a row held
        longer than the redispatch cap looks identical to a poison row."""
        import scraper.tasks as wt

        settings.REDISPATCH_SWEEP_ENABLED = True
        lock.enabled = True
        lock.save(update_fields=["enabled", "updated_at"])

        held_too_long = _make_job(redispatch_count=wt.PENDING_REDISPATCH_CAP)
        _backdate(held_too_long, wt.PENDING_CLAIM_MINUTES + 5)

        result = wt.redispatch_abandoned_pending()
        assert result["action"] == "maintenance_hold"
        held_too_long.refresh_from_db()
        assert held_too_long.status == ScrapeJob.STATUS_PENDING

    @pytest.mark.django_db
    def test_sweep_resumes_after_unlock(self, lock, monkeypatch, settings):
        import scraper.tasks as wt

        settings.REDISPATCH_SWEEP_ENABLED = True
        abandoned = _make_job()
        _backdate(abandoned, wt.PENDING_CLAIM_MINUTES + 5)
        monkeypatch.setattr(
            wt.run_scrape_task,
            "apply_async",
            lambda *a, **k: SimpleNamespace(id=k["task_id"]),
        )
        result = wt.redispatch_abandoned_pending()
        assert result["action"] == "redispatched"


# ─── resume drain ────────────────────────────────────────────────────────────


class TestResumeDrain:
    @pytest.mark.django_db
    def test_resume_dispatches_every_held_row(self, lock, monkeypatch):
        import scraper.tasks as wt

        held_a = _make_job(url="https://a.com/p/1")
        held_b = _make_job(url="https://b.com/p/1")
        published = []
        monkeypatch.setattr(
            wt.run_scrape_task,
            "apply_async",
            lambda *a, **k: published.append(k["task_id"]),
        )

        resumed = wt.resume_maintenance_held_jobs()

        assert sorted(resumed) == sorted([held_a.id, held_b.id])
        assert len(published) == 2
        held_a.refresh_from_db()
        held_b.refresh_from_db()
        assert held_a.celery_task_id and held_b.celery_task_id
        # the two publishes carry distinct task ids (keystone contract)
        assert published[0] != published[1]

    @pytest.mark.django_db
    def test_resume_leaves_non_held_rows_alone(self, lock, monkeypatch):
        import scraper.tasks as wt

        running = _make_job(
            status=ScrapeJob.STATUS_RUNNING, celery_task_id="live-task"
        )
        dispatched_pending = _make_job(celery_task_id="queued-task")
        done = _make_job(status=ScrapeJob.STATUS_COMPLETED, product_count=3)

        published = []
        monkeypatch.setattr(
            wt.run_scrape_task,
            "apply_async",
            lambda *a, **k: published.append(k["task_id"]),
        )

        resumed = wt.resume_maintenance_held_jobs()

        assert resumed == []
        assert published == []
        for j in (running, dispatched_pending, done):
            j.refresh_from_db()
            assert j.status != ScrapeJob.STATUS_FAILED


# ─── toggle endpoint ─────────────────────────────────────────────────────────


class TestToggleEndpoint:
    @pytest.mark.django_db
    def test_regular_user_is_forbidden(self, regular_user, lock):
        from django.urls import reverse

        c = Client()
        c.force_login(regular_user)
        r = c.post(
            reverse("intake_maintenance"),
            {"enabled": "1"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 403

    @pytest.mark.django_db
    def test_superuser_enables_then_disables_with_drain(
        self, admin_client, lock, monkeypatch
    ):
        from django.urls import reverse

        import scraper.tasks as wt

        held = _make_job(url="https://held.com/p/1")
        monkeypatch.setattr(
            wt.run_scrape_task,
            "apply_async",
            lambda *a, **k: SimpleNamespace(id=k["task_id"]),
        )

        r1 = admin_client.post(
            reverse("intake_maintenance"),
            {"enabled": "1", "reason": "waiting to merge wave-19"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r1.status_code == 200
        assert r1.json()["enabled"] is True

        # a teammate's job created during maintenance — held, never dispatched
        job = _make_job(url="https://new.com/p/1")
        wt.dispatch_scrape_job(job.id)
        job.refresh_from_db()
        assert job.celery_task_id == ""

        r2 = admin_client.post(
            reverse("intake_maintenance"),
            {"enabled": "0"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["enabled"] is False
        assert held.id in body["resumed_job_ids"]
        assert job.id in body["resumed_job_ids"]
        job.refresh_from_db()
        assert job.celery_task_id != ""


# ─── library API exposes the state ──────────────────────────────────────────


class TestIntakeJobsExposesState:
    @pytest.mark.django_db
    def test_intake_jobs_includes_maintenance_flag(self, admin_client, lock):
        from django.urls import reverse

        lock.enabled = True
        lock.reason = "deploy window"
        lock.save(update_fields=["enabled", "reason", "updated_at"])
        r = admin_client.get(reverse("intake_jobs"))
        assert r.status_code == 200
        assert r.json()["maintenance"] == {
            "enabled": True,
            "reason": "deploy window",
        }


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
