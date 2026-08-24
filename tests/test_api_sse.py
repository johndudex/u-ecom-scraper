"""bisect: single test"""
import os, sys, secrets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory

from scraper import models
from scraper.api.sse import job_events_sse, mint_stream_token

rf = RequestFactory()


def _reset_budget():
    import redis as redis_lib
    from django.conf import settings as dj_settings

    try:
        conn = redis_lib.from_url(dj_settings.CELERY_BROKER_URL)
        conn.set("sse:open:global", 0)
        conn.close()
    except Exception:
        pass



@pytest.mark.django_db
def test_mint_consume_minimal():
    _reset_budget()
    u = User.objects.create_user("_min_" + secrets.token_hex(3), password="x")
    raw = "pk_" + secrets.token_hex(16)
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    job = models.ScrapeJob.objects.create(
        url="https://e.com/i", user=u, created_via="api", status="running",
        input_mode="url_list", page_type="product",
    )
    tok = mint_stream_token(user=u)
    req = rf.get(f"/api/v1/jobs/{job.id}/events?token={tok}")
    resp = job_events_sse(req, job.id)
    assert resp.status_code == 200
    req2 = rf.get(f"/api/v1/jobs/{job.id}/events?token={tok}")
    resp2 = job_events_sse(req2, job.id)
    assert resp2.status_code == 401


@pytest.mark.django_db
def test_bad_token_401():
    _reset_budget()
    u = User.objects.create_user("_bt_" + secrets.token_hex(3), password="x")
    job = models.ScrapeJob.objects.create(
        url="https://e.com/i", user=u, created_via="api", status="running",
        input_mode="url_list", page_type="product",
    )
    req = rf.get(f"/api/v1/jobs/{job.id}/events?token=forged")
    assert job_events_sse(req, job.id).status_code == 401


@pytest.mark.django_db
def test_no_creds_401():
    u = User.objects.create_user("_nc_" + secrets.token_hex(3), password="x")
    job = models.ScrapeJob.objects.create(
        url="https://e.com/i", user=u, created_via="api", status="running",
        input_mode="url_list", page_type="product",
    )
    req = rf.get(f"/api/v1/jobs/{job.id}/events")
    assert job_events_sse(req, job.id).status_code == 401


@pytest.mark.django_db
def test_key_header_stream_and_drain():
    _reset_budget()
    u = User.objects.create_user("_kh_" + secrets.token_hex(3), password="x")
    raw = "pk_" + secrets.token_hex(16)
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    job = models.ScrapeJob.objects.create(
        url="https://e.com/i", user=u, created_via="completed" and "api", status="completed",
        input_mode="url_list", page_type="product",
    )
    req = rf.get(f"/api/v1/jobs/{job.id}/events", HTTP_X_API_KEY=raw)
    resp = job_events_sse(req, job.id)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/event-stream")
    body = b"".join(resp.streaming_content).decode()
    types = [
        __import__("json").loads(line[6:])["type"]
        for line in body.splitlines() if line.startswith("data: ")
    ]
    assert "job.scraper_ready" in types
    assert "ping" in body


@pytest.mark.django_db
def test_cross_tenant_404():
    u = User.objects.create_user("_ct_" + secrets.token_hex(3), password="x")
    other = User.objects.create_user("_cto_" + secrets.token_hex(3), password="x")
    raw = "pk_" + secrets.token_hex(16)
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    job = models.ScrapeJob.objects.create(
        url="https://e.com/i", user=other, created_via="api", status="running",
        input_mode="url_list", page_type="product",
    )
    req = rf.get(f"/api/v1/jobs/{job.id}/events", HTTP_X_API_KEY=raw)
    assert job_events_sse(req, job.id).status_code == 404


@pytest.mark.django_db
def test_budget_exhausted_503(monkeypatch):
    from scraper.api import sse as sse_mod

    monkeypatch.setattr(sse_mod, "STREAM_BUDGET", 0)
    u = User.objects.create_user("_bx_" + secrets.token_hex(3), password="x")
    raw = "pk_" + secrets.token_hex(16)
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    job = models.ScrapeJob.objects.create(
        url="https://e.com/i", user=u, created_via="api", status="running",
        input_mode="url_list", page_type="product",
    )
    req = rf.get(f"/api/v1/jobs/{job.id}/events", HTTP_X_API_KEY=raw)
    assert job_events_sse(req, job.id).status_code == 503
