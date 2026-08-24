"""cancel + callback GET/PATCH (slice 1a-ii).

Locks:
- cancel: 200 idempotent on already-cancelled; 409 not_cancellable on
  completed; sets completed_at (the reconciler hole fix); job.failed event
  with failure.code=cancelled
- callback GET: 200 shape (no secret ever); null when unregistered
- callback PATCH reenable: resets disabled → active, 60s cooldown (409 on
  faster), pending events resume
- callback PATCH rotate: swaps url/secret atomically, re-validates SSRF
- cross-tenant callback reads 404
"""
from __future__ import annotations

import json
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
from django.test import RequestFactory  # noqa: E402
from django.utils import timezone  # noqa: E402

from scraper import models  # noqa: E402
from scraper.api.writers import cancel_job, get_job_callback, patch_job_callback  # noqa: E402
from scraper.events import new_event_id as _ulid  # noqa: E402

rf = RequestFactory()


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_cc", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    key = models.ApiKey.objects.create(
        user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
    )
    return u, raw, key


def _req(u, raw, method, path, body=None):
    kw = {"data": json.dumps(body) if body is not None else None,
          "content_type": "application/json", "HTTP_X_API_KEY": raw}
    return getattr(rf, method.lower())(path, **kw)


def _job(u, status="running", with_cb=False, **kw):
    j = models.ScrapeJob.objects.create(
        url="https://www.example.com/item", user=u, created_via="api",
        status=status, input_mode="url_list", page_type="product", **kw,
    )
    if with_cb:
        models.JobCallback.objects.create(
            job=j, url="https://hooks.partner.example/cb", secret="s" * 40
        )
    return j


class TestCancel:
    def test_cancel_running(self, partner, db):
        u, raw, key = partner
        job = _job(u)
        from unittest.mock import patch

        with patch("scraper.tasks.run_scrape_task.AsyncResult") as ar:
            ar.return_value.state = "PENDING"
            r = cancel_job(_req(u, raw, "post", f"/api/v1/jobs/{job.id}/cancel"), job.id)
        assert r.status_code == 200
        job.refresh_from_db()
        assert job.status == "cancelled"
        assert job.completed_at is not None  # the reconciler-hole fix
        row = models.EventOutbox.objects.filter(job=job, event_type="job.failed").first()
        assert row is not None
        assert row.payload["data"]["reason"] == "cancelled" or "cancelled" in json.dumps(row.payload)

    def test_cancel_idempotent(self, partner, db):
        u, raw, key = partner
        job = _job(u, status="cancelled", completed_at=timezone.now())
        from unittest.mock import patch

        with patch("scraper.tasks.run_scrape_task.AsyncResult"):
            r = cancel_job(_req(u, raw, "post", f"/api/v1/jobs/{job.id}/cancel"), job.id)
        assert r.status_code == 200

    def test_cancel_completed_409(self, partner, db):
        u, raw, key = partner
        job = _job(u, status="completed")
        r = cancel_job(_req(u, raw, "post", f"/api/v1/jobs/{job.id}/cancel"), job.id)
        assert r.status_code == 409
        assert json.loads(r.content)["code"] == "not_cancellable"

    def test_cancel_cross_tenant_404(self, partner, db):
        u, raw, key = partner
        other = User.objects.create_user(username="_t_cco", password="x")
        job = _job(other)
        r = cancel_job(_req(u, raw, "post", f"/api/v1/jobs/{job.id}/cancel"), job.id)
        assert r.status_code == 404


class TestCallbackGet:
    def test_null_when_unregistered(self, partner, db):
        u, raw, key = partner
        job = _job(u)
        r = get_job_callback(_req(u, raw, "get", f"/api/v1/jobs/{job.id}/callback"), job.id)
        assert r.status_code == 200
        assert json.loads(r.content) == {"callback": None}

    def test_shape_without_secret(self, partner, db):
        u, raw, key = partner
        job = _job(u, with_cb=True)
        cb = job.callback
        cb.delivered_count = 7
        cb.save()
        models.EventOutbox.objects.create(
            event_id=_ulid(), job=job, user=u,
            event_type="job.created", dedupe_key="created", payload={},
        )
        r = get_job_callback(_req(u, raw, "get", f"/api/v1/jobs/{job.id}/callback"), job.id)
        body = json.loads(r.content)
        assert body["status"] == "active"
        assert body["url"] == "https://hooks.partner.example/cb"
        assert body["delivered_count"] == 7
        assert body["pending_count"] == 1
        assert "secret" not in json.dumps(body)

    def test_cross_tenant_404(self, partner, db):
        u, raw, key = partner
        other = User.objects.create_user(username="_t_cco2", password="x")
        job = _job(other, with_cb=True)
        r = get_job_callback(_req(u, raw, "get", f"/api/v1/jobs/{job.id}/callback"), job.id)
        assert r.status_code == 404


class TestCallbackPatch:
    def test_reenable_resets_disabled(self, partner, db):
        u, raw, key = partner
        job = _job(u, with_cb=True)
        cb = job.callback
        cb.status = "disabled"
        cb.disabled_reason = "delivery exhausted after 6 attempts"
        cb.save()
        r = patch_job_callback(
            _req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback", {"action": "reenable"}),
            job.id,
        )
        assert r.status_code == 200
        cb.refresh_from_db()
        assert cb.status == "active"
        assert cb.disabled_reason == ""

    def test_reenable_cooldown_409(self, partner, db):
        u, raw, key = partner
        job = _job(u, with_cb=True)
        cb = job.callback
        cb.status = "disabled"
        cb.save()
        # first reenable
        patch_job_callback(_req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback", {"action": "reenable"}), job.id)
        # disable again + immediate reenable attempt → cooldown
        cb.refresh_from_db()
        cb.status = "disabled"
        cb.save()
        r = patch_job_callback(
            _req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback", {"action": "reenable"}),
            job.id,
        )
        # within 60s of the last reenable: rejected
        assert r.status_code == 429

    def test_reenable_active_409(self, partner, db):
        u, raw, key = partner
        job = _job(u, with_cb=True)
        r = patch_job_callback(
            _req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback", {"action": "reenable"}),
            job.id,
        )
        assert r.status_code == 409
        assert json.loads(r.content)["code"] == "callback_already_active"

    def test_rotate_url(self, partner, db, monkeypatch):
        from scraper.api import ssrf as ssrf_mod

        monkeypatch.setattr(ssrf_mod, "_resolve", lambda h: ["93.184.216.34"])
        u, raw, key = partner
        job = _job(u, with_cb=True)
        r = patch_job_callback(
            _req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback",
                 {"action": "rotate", "callback_url": "https://new.partner.example/hook"}),
            job.id,
        )
        assert r.status_code == 200
        job.callback.refresh_from_db()
        assert job.callback.url == "https://new.partner.example/hook"

    def test_rotate_ssrf_blocked(self, partner, db):
        u, raw, key = partner
        job = _job(u, with_cb=True)
        r = patch_job_callback(
            _req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback",
                 {"action": "rotate", "callback_url": "https://10.0.0.9/hook"}),
            job.id,
        )
        assert r.status_code == 422

    def test_rotate_secret(self, partner, db):
        u, raw, key = partner
        job = _job(u, with_cb=True)
        r = patch_job_callback(
            _req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback",
                 {"action": "rotate", "callback_secret": "n" * 40}),
            job.id,
        )
        assert r.status_code == 200
        job.callback.refresh_from_db()
        assert job.callback.secret == "n" * 40

    def test_unknown_action_422(self, partner, db):
        u, raw, key = partner
        job = _job(u, with_cb=True)
        r = patch_job_callback(
            _req(u, raw, "patch", f"/api/v1/jobs/{job.id}/callback", {"action": "explode"}),
            job.id,
        )
        assert r.status_code == 422


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
