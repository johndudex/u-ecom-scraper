"""Partner API slice 1a-i: models, emitter, auth, state projection, read endpoints.

Locks the fold's mandated invariants:
- auth state machine (401 unknown / 403 revoked|superuser|inactive / success)
- cross-tenant reads are 404, never 403 (no oracle)
- created_via gate: internal jobs NEVER emit outbox rows (M4)
- dedupe: same (job, type, dedupe_key) is a no-op (B1 retry-cycle safety)
- sample_available state-gate: REST agrees with events post-m4
- 4-state projection completeness both directions
- rate limiting: 429 + Retry-After after burst

Run: docker compose exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings \
     celery-worker bash -c "cd /app && python -m pytest tests/test_partner_api.py -v"
"""
from __future__ import annotations

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
from django.test import RequestFactory, override_settings  # noqa: E402

from scraper import models  # noqa: E402
from scraper.api import errors, state  # noqa: E402
from scraper.api.auth import resolve_api_key  # noqa: E402
from scraper.api.readers import check_site, job_status, list_jobs  # noqa: E402
from scraper.events import emitter  # noqa: E402

rf = RequestFactory()


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def partner_user(db):
    return User.objects.create_user(username="_t_partner", password="x")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username="_t_other", password="x")


@pytest.fixture
def partner_key(partner_user):
    raw = "pk_test_" + os.urandom(16).hex()
    return models.ApiKey.objects.create(
        user=partner_user, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
    ), raw


@pytest.fixture
def api_request(partner_key):
    """Requests that pass the FULL api_view pipeline (real header auth)."""
    key_obj, raw = partner_key

    def make(method="GET", path="/api/v1/jobs", body=None, **kw):
        headers = kw.pop("headers", {})
        req = getattr(rf, method.lower())(
            path, data=body, content_type="application/json",
            HTTP_X_API_KEY=raw, **{k: v for k, v in kw.items() if k.startswith("HTTP")},
            **headers,
        )
        return req

    return make


def _partner_job(user, **kw):
    defaults = dict(
        url="https://www.example.com/item",
        user=user,
        created_via="api",
        page_type="product",
        input_mode="url_list",
        status="running",
    )
    defaults.update(kw)
    return models.ScrapeJob.objects.create(**defaults)


# ── ULID ────────────────────────────────────────────────────────────────────

class TestULID:
    def test_shape_and_sortability(self):
        ids = [emitter.new_event_id() for _ in range(100)]
        assert all(len(i) == 26 for i in ids)
        assert sorted(ids) == ids  # monotonic within process
        allowed = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        assert all(set(i) <= allowed for i in ids)


# ── state projection ────────────────────────────────────────────────────────

class TestStateProjection:
    @pytest.mark.parametrize("internal,expected", [
        ("pending", "inprogress"),
        ("running", "inprogress"),
        ("waiting_approval", "inprogress"),
        ("completed", "scraper_ready"),
        ("failed", "failed"),
        ("cancelled", "failed"),
        ("captcha_blocked", "failed"),
        ("akamai_blocked", "failed"),
    ])
    def test_complete_both_directions(self, internal, expected):
        assert state.partner_state(internal) == expected

    def test_every_model_status_is_mapped(self):
        # no dead statuses, no unmapped ones (verifier's both-directions check)
        model_statuses = {c[0] for c in models.ScrapeJob.STATUS_CHOICES}
        assert model_statuses == set(state._STATE_MAP)

    def test_failure_codes(self):
        assert state.failure_code("cancelled") == "cancelled"
        assert state.failure_code("captcha_blocked") == "captcha_blocked"

    def test_sample_gate_live_job(self, db, partner_user):
        job = _partner_job(partner_user)
        assert state.sample_ready(job, testing_done=True) is True

    def test_sample_gate_terminal_job_m4(self, db, partner_user):
        """The m4 fix: finalize stamps completed_at on never-run steps; a
        terminal job must NOT claim a sample even when testing_done says yes."""
        job = _partner_job(partner_user, status="failed")
        assert state.sample_ready(job, testing_done=True) is False

    def test_phase_normalization(self):
        assert state.normalize_phase("Browser Navigation") == "browser_traverse"
        assert state.normalize_phase("testing") == "testing"
        assert state.normalize_phase("weird-new-phase") in state.PHASE_ENUM


# ── emitter ─────────────────────────────────────────────────────────────────

class TestEmitter:
    def test_created_via_gate_internal(self, db, partner_user):
        job = _partner_job(partner_user, created_via="intake")
        assert emitter.emit(job, "job.created", {}) is None
        assert models.EventOutbox.objects.count() == 0

    def test_created_via_gate_api(self, db, partner_user):
        job = _partner_job(partner_user)
        row = emitter.emit(job, "job.created", {"state": "inprogress"})
        assert row is not None
        assert row.event_type == "job.created"
        assert len(row.event_id) == 26
        assert row.state == models.EventOutbox.STATE_PENDING

    def test_dedupe_first_wins(self, db, partner_user):
        job = _partner_job(partner_user)
        first = emitter.emit(job, "job.sample_ready", {}, dedupe_key="sample:1")
        second = emitter.emit(job, "job.sample_ready", {}, dedupe_key="sample:1")
        assert first is not None and second is None
        assert models.EventOutbox.objects.filter(job=job).count() == 1

    def test_no_dedupe_key_allows_multiples(self, db, partner_user):
        job = _partner_job(partner_user)
        emitter.emit(job, "job.phase.updated", {})
        emitter.emit(job, "job.phase.updated", {})
        assert models.EventOutbox.objects.filter(job=job).count() == 2

    def test_envelope_shape(self, db, partner_user):
        job = _partner_job(partner_user)
        row = emitter.emit(job, "job.created", {"state": "inprogress"})
        p = row.payload
        assert set(p) == {"event_id", "type", "occurred_at", "job_id", "user_id", "data"}
        assert p["event_id"] == row.event_id
        assert p["job_id"] == job.id


# ── auth machine ────────────────────────────────────────────────────────────

class TestAuth:
    def test_missing_header(self, api_request):
        req = rf.post("/api/v1/check-site")
        with pytest.raises(errors.ApiError) as e:
            resolve_api_key(req)
        assert e.value.status == 401

    def test_unknown_key(self, db):
        req = rf.post("/api/v1/check-site", headers={"X-API-Key": "pk_nope"})
        with pytest.raises(errors.ApiError) as e:
            resolve_api_key(req)
        assert e.value.status == 401

    def test_revoked(self, db, partner_key):
        key, raw = partner_key
        key.revoked_at = key.created_at
        key.save()
        req = rf.post("/api/v1/check-site", headers={"X-API-Key": raw})
        with pytest.raises(errors.ApiError) as e:
            resolve_api_key(req)
        assert e.value.status == 403

    def test_superuser_rejected(self, db):
        su = User.objects.create_user(username="_t_su", password="x", is_superuser=True)
        raw = "pk_test_" + os.urandom(8).hex()
        models.ApiKey.objects.create(
            user=su, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
        )
        req = rf.post("/api/v1/check-site", headers={"X-API-Key": raw})
        with pytest.raises(errors.ApiError) as e:
            resolve_api_key(req)
        assert e.value.status == 403  # the code-level superuser mandate

    def test_inactive_owner(self, db, partner_user, partner_key):
        key, raw = partner_key
        partner_user.is_active = False
        partner_user.save()
        req = rf.post("/api/v1/check-site", headers={"X-API-Key": raw})
        with pytest.raises(errors.ApiError) as e:
            resolve_api_key(req)
        assert e.value.status == 403

    def test_success_returns_user(self, db, partner_key):
        key, raw = partner_key
        req = rf.post("/api/v1/check-site", headers={"X-API-Key": raw})
        user, k = resolve_api_key(req)
        assert user == key.user and k == key


# ── endpoints ───────────────────────────────────────────────────────────────

class TestEndpoints:
    def test_check_site_known(self, db, api_request, partner_user):
        models.Site.objects.create(
            url="https://www.known.example/", platform="Shopify", name="Known"
        )
        req = api_request(
            "POST", "/api/v1/check-site",
            body='{"url": "https://www.known.example/product"}',
        )
        r = check_site(req)
        assert r.status_code == 200
        import json

        body = json.loads(r.content)
        assert body["known_site"] is True
        assert body["platform"] == "Shopify"
        assert "fields" not in body  # NEVER cross-tenant field lists

    def test_check_site_unknown(self, db, api_request):
        req = api_request("POST", "/api/v1/check-site", body='{"url": "https://nope.example/"}')
        r = check_site(req)
        import json

        assert json.loads(r.content)["known_site"] is False

    def test_check_site_bad_url(self, db, api_request):
        req = api_request("POST", "/api/v1/check-site", body='{"url": "garbage"}')
        r = check_site(req)
        assert r.status_code == 422

    def test_status_cross_tenant_404(self, db, api_request, other_user):
        job = _partner_job(other_user)  # someone ELSE's job
        req = api_request("GET", f"/api/v1/jobs/{job.id}")
        r = job_status(req, job.id)
        assert r.status_code == 404
        import json

        assert json.loads(r.content)["code"] == "not_found"

    def test_status_own_job(self, db, api_request, partner_user):
        job = _partner_job(partner_user)
        models.Step.objects.create(job=job, phase="testing", status="done",
                                   completed_at=job.created_at)
        req = api_request("GET", f"/api/v1/jobs/{job.id}")
        r = job_status(req, job.id)
        assert r.status_code == 200
        import json

        body = json.loads(r.content)
        assert body["state"] == "inprogress"
        assert body["sample_available"] is True  # live + testing done

    def test_status_failed_job_no_sample_claim(self, db, api_request, partner_user):
        """m4 lock: terminal failed job with a finalize-stamped testing step
        must NOT claim sample_available (REST ≡ events)."""
        job = _partner_job(partner_user, status="failed")
        models.Step.objects.create(job=job, phase="testing", status="done",
                                   completed_at=job.created_at)
        req = api_request("GET", f"/api/v1/jobs/{job.id}")
        r = job_status(req, job.id)
        import json

        body = json.loads(r.content)
        assert body["state"] == "failed"
        assert body["sample_available"] is False

    def test_list_pagination_and_validation(self, db, api_request, partner_user):
        for i in range(3):
            _partner_job(partner_user)
        req = api_request("GET", "/api/v1/jobs?page=1&page_size=2")
        r = list_jobs(req)
        import json

        body = json.loads(r.content)
        assert body["total_items"] == 3
        assert len(body["jobs"]) == 2
        assert "phases" not in body["jobs"][0]  # summaries only

        req_bad = api_request("GET", "/api/v1/jobs?page_size=500")
        assert list_jobs(req_bad).status_code == 422

    def test_list_created_since_invalid(self, db, api_request):
        req = api_request("GET", "/api/v1/jobs?created_since=not-a-date")
        assert list_jobs(req).status_code == 422


# ── rate limiting ───────────────────────────────────────────────────────────

class TestRateLimit:
    def test_burst_429(self, db, api_request, partner_key):
        key_prefix = partner_key[0].prefix
        from scraper.api.ratelimit import check_rate_limit

        # simulate an already-bursting window
        import time

        from scraper.services import _get_redis

        conn = _get_redis()
        burst_key = f"rl:{key_prefix}:b:{int(time.time())}"
        conn.set(burst_key, 30)
        assert check_rate_limit(key_prefix) is not None  # over → Retry-After

    def test_fail_open_on_redis_down(self, db, monkeypatch):
        from scraper.api import ratelimit

        def boom():
            raise ConnectionError("redis down")

        monkeypatch.setattr("scraper.services._get_redis", boom)
        assert ratelimit.check_rate_limit("xyz") is None  # fail-open
        assert ratelimit.check_rate_limit("xyz") is None  # fail-open


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
