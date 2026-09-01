"""POST /api/v1/jobs — the create endpoint (slice 1a-ii).

Locks (sync_api.yaml create + fold M12/R2):
- 202 with job_id + status_url; state inprogress
- FIXED creation flags: full_extraction=False, skip_approvals=True,
  created_via="api" (emit gate)
- atomic block: job + JobCallback + events.emit(job.created) in ONE
  transaction; dispatch via on_commit ONLY (the M3-race fix — worker must
  never read an uncommitted row)
- 409 duplicate_running_job for a live job on the same URL + same user
- 422 schema_invalid when schema_text fails the gate
- 422 invalid_callback_url when the callback URL hits the SSRF gate
- callback_secret NEVER in any response
- url_list requires item_urls; search_term requires search_criteria
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from scraper import models  # noqa: E402
from scraper.api.writers import create_job  # noqa: E402

rf = RequestFactory()


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_create", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    key = models.ApiKey.objects.create(
        user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
    )
    return u, raw, key


def _req(u, raw, body):
    return rf.post(
        "/api/v1/jobs", data=json.dumps(body), content_type="application/json",
        HTTP_X_API_KEY=raw,
    )


VALID = {
    "url": "https://www.rmwilliams.com.au/comfort-craftsman-boot.html",
    "input_mode": "url_list",
    "content_type": "product",
    "item_urls": ["https://www.rmwilliams.com.au/comfort-craftsman-boot.html"],
    "target_fields": ["title", "price"],
}


class TestCreateHappy:
    def test_202_shape(self, partner, db):
        u, raw, key = partner
        r = create_job(_req(u, raw, VALID))
        assert r.status_code == 202
        body = json.loads(r.content)
        assert set(body) >= {"job_id", "status_url"}
        assert body["status_url"] == f"/api/v1/jobs/{body['job_id']}"
        job = models.ScrapeJob.objects.get(pk=body["job_id"])
        assert job.created_via == "api"
        assert job.full_extraction is False
        assert job.skip_approvals is True
        assert job.user == u

    def test_job_created_event_emitted(self, partner, db):
        u, raw, key = partner
        r = create_job(_req(u, raw, VALID))
        job_id = json.loads(r.content)["job_id"]
        row = models.EventOutbox.objects.filter(
            job_id=job_id, event_type="job.created"
        ).first()
        assert row is not None
        assert row.payload["data"].get("state") == "inprogress"

    def test_dispatch_exactly_once_via_on_commit(self, partner, db):
        """M12/R2: dispatch rides transaction.on_commit — captured callbacks
        prove it registered (never inline), then fire exactly once."""
        u, raw, key = partner
        from unittest.mock import patch

        class Capture:
            def __init__(self):
                self.callbacks = []

            def capture(self, fn):
                self.callbacks.append(fn)

        cap = Capture()
        calls = {"n": 0}

        seen_ids = []

        def fake_apply(*a, **k):
            # [wave-15 1.0] dispatch_scrape_job stamps the client-generated id
            # BEFORE publishing, then hands it to apply_async(task_id=...).
            calls["n"] += 1
            seen_ids.append(k.get("task_id"))
            return type("T", (), {"id": k.get("task_id")})

        with patch("django.db.transaction.on_commit", side_effect=cap.capture):
            with patch("scraper.tasks.run_scrape_task.apply_async", side_effect=fake_apply):
                r = create_job(_req(u, raw, VALID))
                assert r.status_code == 202
                assert calls["n"] == 0  # NEVER inline — only after commit
                # fire INSIDE the patch — commit happens under the same world
                for c in cap.callbacks:
                    try:
                        c()
                    except Exception:
                        pass
        # emit's redis publish also rode on_commit — both registered, none inline
        assert any("dispatch" in getattr(c, "__qualname__", "") for c in cap.callbacks)
        assert calls["n"] == 1
        job = models.ScrapeJob.objects.get(url=VALID["url"])
        # The id on the row IS the id that was published (stamp BEFORE publish).
        assert seen_ids and job.celery_task_id == seen_ids[0]
        assert job.celery_task_id  # a real uuid4-shaped stamp, never ""


class TestCreateValidation:
    def test_409_duplicate_running(self, partner, db):
        u, raw, key = partner
        models.ScrapeJob.objects.create(
            url=VALID["url"], user=u, created_via="api", status="running",
            input_mode="url_list", page_type="product",
        )
        r = create_job(_req(u, raw, VALID))
        assert r.status_code == 409
        body = json.loads(r.content)
        assert body["code"] == "duplicate_running_job"
        assert "existing_job_id" in body["details"]

    def test_409_not_for_other_users_job(self, partner, db):
        u, raw, key = partner
        other = User.objects.create_user(username="_t_other2", password="x")
        models.ScrapeJob.objects.create(
            url=VALID["url"], user=other, created_via="api", status="running",
            input_mode="url_list", page_type="product",
        )
        r = create_job(_req(u, raw, VALID))
        assert r.status_code == 202  # another tenant's job is not ours

    def test_422_bad_schema(self, partner, db):
        u, raw, key = partner
        body = {**VALID, "schema_text": "not json at all {"}
        r = create_job(_req(u, raw, body))
        assert r.status_code == 422
        assert json.loads(r.content)["code"] == "schema_invalid"

    def test_422_url_list_requires_item_urls(self, partner, db):
        u, raw, key = partner
        body = {k: v for k, v in VALID.items() if k != "item_urls"}
        r = create_job(_req(u, raw, body))
        assert r.status_code == 422

    def test_422_search_term_requires_criteria(self, partner, db):
        u, raw, key = partner
        body = {"url": VALID["url"], "input_mode": "search_term"}
        r = create_job(_req(u, raw, body))
        assert r.status_code == 422

    def test_400_malformed_json(self, partner, db):
        u, raw, key = partner
        req = rf.post("/api/v1/jobs", data="{broken", content_type="application/json", HTTP_X_API_KEY=raw)
        r = create_job(req)
        assert r.status_code == 400


class TestCreateCallback:
    @pytest.fixture
    def public_dns(self, monkeypatch):
        """hooks.partner.example resolves publicly in tests (no network)."""
        from scraper.api import ssrf as ssrf_mod

        monkeypatch.setattr(ssrf_mod, "_resolve", lambda h: ["93.184.216.34"])

    def test_callback_registered_and_secret_never_returned(self, partner, db, public_dns):
        u, raw, key = partner
        body = {**VALID, "callback_url": "https://hooks.partner.example/cb",
                "callback_secret": "x" * 40}
        r = create_job(_req(u, raw, body))
        assert r.status_code == 202
        job_id = json.loads(r.content)["job_id"]
        cb = models.JobCallback.objects.get(job_id=job_id)
        assert cb.url == "https://hooks.partner.example/cb"
        assert cb.secret == "x" * 40
        assert b"secret" not in r.content  # never in the response

    def test_callback_ssrf_blocked(self, partner, db):  # literal IP — no DNS needed
        u, raw, key = partner
        body = {**VALID, "callback_url": "https://192.168.1.5/cb",
                "callback_secret": "x" * 40}
        r = create_job(_req(u, raw, body))
        assert r.status_code == 422
        assert json.loads(r.content)["code"] == "invalid_callback_url"
        assert models.JobCallback.objects.count() == 0

    def test_callback_secret_too_short(self, partner, db, public_dns):
        u, raw, key = partner
        body = {**VALID, "callback_url": "https://hooks.partner.example/cb",
                "callback_secret": "short"}
        r = create_job(_req(u, raw, body))
        assert r.status_code == 422

    def test_job_created_payload_echoes_callback(self, partner, db, public_dns):
        u, raw, key = partner
        body = {**VALID, "callback_url": "https://hooks.partner.example/cb",
                "callback_secret": "x" * 40}
        create_job(_req(u, raw, body))
        row = models.EventOutbox.objects.get(event_type="job.created")
        assert row.payload["data"]["callback"]["url"] == "https://hooks.partner.example/cb"
        assert "secret" not in json.dumps(row.payload)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
