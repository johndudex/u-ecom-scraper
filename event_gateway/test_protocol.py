"""WSS gateway contract tests (async_api.yaml /ws/v1/jobs — Phase 2).

RED harness: these drive the gateway's handler functions directly (not
through a live socket — that's the e2e smoke later). Every message shape,
auth outcome, and timer rule comes from the frozen spec.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("EVENT_GATEWAY_TEST", "1")
from gateway import (  # noqa: E402
    HEARTBEAT_SECONDS,
    build_snapshot,
    check_ws_token,
    handle_control,
    verify_api_key,
)

import django  # noqa: E402
import pytest_django  # noqa: E402,F401

pytestmark_async = pytest.mark.asyncio

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, "/app/webapp")
django.setup()

from django.contrib.auth.models import User  # noqa: E402
from scraper import models  # noqa: E402


@pytest.fixture
def partner(transactional_db):
    import secrets

    u = User.objects.create_user("_t_ws_" + secrets.token_hex(3), password="x")
    raw = "pk_" + secrets.token_hex(16)
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    return u, raw


def _job(u, status="running", **kw):
    d = dict(url="https://e.com/i", user=u, created_via="api", status=status,
             input_mode="url_list", page_type="product", site_folder="scrapers/ws")
    d.update(kw)
    return models.ScrapeJob.objects.create(**d)


class TestAuth:
    def test_valid_key(self, partner):
        u, raw = partner
        got = verify_api_key(raw)
        assert got == u.id

    def test_unknown_key(self):
        assert verify_api_key("pk_nothing") is None

    def test_revoked_key(self, partner):
        u, raw = partner
        key = models.ApiKey.objects.get(user=u)
        from django.utils import timezone

        key.revoked_at = timezone.now()
        key.save()
        assert verify_api_key(raw) is None

    def test_superuser_rejected(self, db):
        import secrets

        su = User.objects.create_user("_t_wssu", password="x", is_superuser=True)
        raw = "pk_" + secrets.token_hex(16)
        models.ApiKey.objects.create(user=su, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
        assert verify_api_key(raw) is None  # m7: the code-level mandate

    def test_inactive_owner(self, partner):
        u, raw = partner
        u.is_active = False
        u.save()
        assert verify_api_key(raw) is None

    def test_token_consume_single_use(self, partner):
        from scraper.api.sse import mint_stream_token

        u, raw = partner
        tok = mint_stream_token(user=u)
        assert check_ws_token(tok) == u.id
        assert check_ws_token(tok) is None  # consumed


from asgiref.sync import sync_to_async  # noqa: E402


class TestControlProtocol:
    pytestmark = pytest.mark.django_db(transaction=True)

    async def _job_async(self, u, **kw):
        return await sync_to_async(_job)(u, **kw)

    async def _user_async(self, name="_t_wso"):
        return await sync_to_async(User.objects.create_user)(name + os.urandom(2).hex(), password="x")

    async def test_subscribe_ack_with_snapshot(self, partner):
        u, raw = partner
        job = await self._job_async(u, status="completed", product_count=7,
                                    scraper_file="scrapers/ws/jobs/scraper-9.py")
        out = await handle_control(u.id, json.dumps({"op": "subscribe", "data": {"job_id": job.id}}), subs=set())
        payload = json.loads(out)
        assert payload["op"] == "subscribe.ack"
        assert payload["data"]["job_id"] == job.id
        assert payload["data"]["state"] == "scraper_ready"
        assert payload["data"]["snapshot"]["output_url"] == f"/api/v1/jobs/{job.id}/output"

    async def test_subscribe_nack_not_found_or_foreign(self, partner, db):
        u, raw = partner
        other = await self._user_async()
        foreign = await self._job_async(other)
        gone = 999999
        for jid in (foreign.id, gone):
            out = await handle_control(u.id, json.dumps({"op": "subscribe", "data": {"job_id": jid}}), subs=set())
            payload = json.loads(out)
            assert payload["op"] == "subscribe.nack"
            assert payload["data"]["error_code"] == "job_not_found"

    async def test_unsubscribe_ack(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs = {job.id}
        out = await handle_control(u.id, json.dumps({"op": "unsubscribe", "data": {"job_id": job.id}}), subs=subs)
        payload = json.loads(out)
        assert payload["op"] == "unsubscribe.ack"
        assert job.id not in subs

    async def test_unsubscribe_not_subscribed_is_ack(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        out = await handle_control(u.id, json.dumps({"op": "unsubscribe", "data": {"job_id": job.id}}), subs=set())
        assert json.loads(out)["op"] == "unsubscribe.ack"  # idempotent per spec

    async def test_pong_no_output(self, partner):
        u, raw = partner
        out = await handle_control(u.id, json.dumps({"op": "heartbeat.pong", "data": {}}), subs=set())
        assert out is None

    async def test_invalid_message_keeps_connection(self, partner):
        u, raw = partner
        out = await handle_control(u.id, "not-json", subs=set())
        payload = json.loads(out)
        assert payload["op"] == "error"
        assert payload["data"]["error_code"] == "invalid_message"

    async def test_unknown_op_is_error(self, partner):
        u, raw = partner
        out = await handle_control(u.id, json.dumps({"op": "explode", "data": {}}), subs=set())
        assert json.loads(out)["op"] == "error"

    async def test_subscribe_adds_to_subs(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs = set()
        await handle_control(u.id, json.dumps({"op": "subscribe", "data": {"job_id": job.id}}), subs=subs)
        assert job.id in subs


class TestSnapshot:
    def test_terminal_completed(self, partner, transactional_db):
        u, raw = partner
        job = _job(u, status="completed")
        snap = build_snapshot(job.id, u.id)
        assert snap["state"] == "scraper_ready"
        inner = snap["snapshot"]
        assert inner["output_url"] == f"/api/v1/jobs/{job.id}/output"
        assert inner["scraper_code_url"] == f"/api/v1/jobs/{job.id}/scraper-code"

    def test_last_event_id_from_outbox(self, partner, transactional_db):
        u, raw = partner
        job = _job(u)
        from scraper.events import new_event_id

        models.EventOutbox.objects.create(
            event_id=new_event_id(), job=job, user=u,
            event_type="job.created", dedupe_key="created", payload={},
        )
        snap = build_snapshot(job.id, u.id)
        assert snap["snapshot"]["last_event_id"]


class TestTimers:
    def test_heartbeat_interval_under_30(self):
        assert HEARTBEAT_SECONDS <= 25  # spec: ping every 25s of silence
