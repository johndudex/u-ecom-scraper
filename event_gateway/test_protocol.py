"""WSS gateway contract tests (async_api.yaml /ws/v1/jobs — Phase 2).

RED harness: these drive the gateway's handler functions directly (not
through a live socket — that's the e2e smoke later). Every message shape,
auth outcome, and timer rule comes from the frozen spec.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("EVENT_GATEWAY_TEST", "1")
from gateway import (  # noqa: E402
    DEFAULT_EVENT_FILTER,
    HEARTBEAT_SECONDS,
    build_snapshot,
    check_ws_token,
    event_allowed,
    handle_control,
    heartbeat_ping_frame,
    retire_subscription,
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


class TestEventFilter:
    """B6-2: subscribe.events must actually filter the pump's fan-out."""

    pytestmark = pytest.mark.django_db(transaction=True)

    async def _job_async(self, u, **kw):
        return await sync_to_async(_job)(u, **kw)

    async def _subscribe(self, u, job, subs, filters, events=None):
        frame = {"op": "subscribe", "data": {"job_id": job.id}}
        if events is not None:
            frame["data"]["events"] = events
        return await handle_control(u.id, json.dumps(frame), subs=subs, filters=filters)

    async def test_filtered_subscribe_stores_filter(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        out = await self._subscribe(u, job, subs, filters, ["job.scraper_ready"])
        assert json.loads(out)["op"] == "subscribe.ack"
        assert filters[job.id] == {"job.scraper_ready"}

    async def test_plain_subscribe_stores_default_filter(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        await self._subscribe(u, job, subs, filters)
        assert filters[job.id] is None  # None = the spec's default set

    async def test_unknown_event_name_is_invalid_message(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        out = await self._subscribe(u, job, subs, filters, ["job.scraper_ready", "job.nonsense"])
        payload = json.loads(out)
        assert payload["op"] == "error"
        assert payload["data"]["error_code"] == "invalid_message"
        assert job.id not in subs and job.id not in filters  # not half-subscribed

    async def test_events_not_a_list_is_invalid_message(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        out = await self._subscribe(u, job, subs, filters, "job.scraper_ready")
        assert json.loads(out)["data"]["error_code"] == "invalid_message"

    async def test_filter_scopes_fanout(self, partner):
        """The TDD core: phase.updated filtered out, scraper_ready let through."""
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        await self._subscribe(u, job, subs, filters, ["job.scraper_ready"])
        assert not event_allowed(filters, job.id, "job.phase.updated")
        assert event_allowed(filters, job.id, "job.scraper_ready")

    async def test_default_filter_lets_state_events_through(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        await self._subscribe(u, job, subs, filters)
        for et in DEFAULT_EVENT_FILTER:
            assert event_allowed(filters, job.id, et)
        # the two opt-in types are excluded by default
        assert not event_allowed(filters, job.id, "job.log.appended")
        assert not event_allowed(filters, job.id, "job.phase.updated")

    async def test_unsubscribe_clears_filter(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        await self._subscribe(u, job, subs, filters, ["job.scraper_ready"])
        await handle_control(u.id, json.dumps({"op": "unsubscribe", "data": {"job_id": job.id}}),
                             subs=subs, filters=filters)
        assert job.id not in filters  # H3: no leak

    async def test_resubscribe_replaces_filter(self, partner):
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = set(), {}
        await self._subscribe(u, job, subs, filters, ["job.scraper_ready"])
        await self._subscribe(u, job, subs, filters, ["job.failed"])
        assert filters[job.id] == {"job.failed"}

    async def test_terminal_retire_clears_filter(self, partner):
        """Pump-side subscription retirement must not leak the filter either."""
        u, raw = partner
        job = await self._job_async(u)
        subs, filters = {job.id}, {job.id: {"job.scraper_ready"}}
        retire_subscription(job.id, subs, filters)
        assert job.id not in subs and job.id not in filters


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


class TestHeartbeatFrame:
    """B6-3/H2: HeartbeatData.server_time (ISO 8601 date-time), not `ts`."""

    def test_ping_frame_shape_matches_spec(self):
        frame = json.loads(heartbeat_ping_frame())
        assert frame["op"] == "heartbeat.ping"
        assert set(frame["data"]) == {"server_time"}
        assert "ts" not in frame["data"]
        # format: date-time → parseable by fromisoformat after the Z fixup
        import datetime as dt

        raw = frame["data"]["server_time"]
        assert raw.endswith("Z")
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
