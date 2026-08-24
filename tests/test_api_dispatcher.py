"""Outbox dispatcher (slice 1b): lease state machine + retry scheduling.

Locks (fold B5/M1/M3 + async_api.yaml x-retry):
- claim_due_rows: SELECT ... FOR UPDATE SKIP LOCKED with lease CAS — two
  concurrent sweepers never claim the same row
- legs >= 1m self-schedule (countdown), < 1m left to the 30s sweep —
  the spec's stated backoff is honored where partners build retry UX
- attempts increments on EVERY failure INCLUDING lease expiry (poison
  events reach disable-on-exhaustion)
- 6 attempts total → callback marked disabled + disabled_reason, rows
  go permanently_failed (never retried again)
- disabled callback: PENDING rows stay queued (delivered after re-enable)
- dispatch enqueues via on_commit ONLY (M3 discipline in our own dispatcher)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from unittest.mock import patch

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
from scraper.events.dispatcher import (  # noqa: E402
    BACKOFFS,
    claim_due_rows,
    mark_attempt_failed,
    mark_delivered,
    sweep,
)

BACKOFF_SECONDS = {"10s": 10, "1m": 60, "10m": 600, "1h": 3600, "6h": 21600}


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_disp", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    return u, raw


def _job(u, with_cb=True):
    j = models.ScrapeJob.objects.create(
        url="https://www.example.com/i", user=u, created_via="api",
        status="running", input_mode="url_list", page_type="product",
        site_folder="scrapers/disp",
    )
    if with_cb:
        models.JobCallback.objects.create(
            job=j, url="https://hooks.partner.example/cb", secret="s" * 40
        )
    return j


def _row(job, **kw):
    from scraper.events import new_event_id

    defaults = dict(
        event_id=new_event_id(), job=job, user=job.user,
        event_type="job.created", dedupe_key="created",
        payload={"event_id": "x", "type": "job.created", "data": {}},
        state=models.EventOutbox.STATE_PENDING,
        next_attempt_at=timezone.now() - timedelta(seconds=1),
    )
    defaults.update(kw)
    return models.EventOutbox.objects.create(**defaults)


class TestClaim:
    def test_claims_due_pending(self, partner, db):
        u, raw = partner
        job = _job(u)
        row = _row(job)
        claimed = claim_due_rows(limit=10)
        assert row.pk in [r.pk for r in claimed]
        row.refresh_from_db()
        assert row.state == models.EventOutbox.STATE_LEASED
        assert row.locked_until is not None

    def test_skips_future_rows(self, partner, db):
        u, raw = partner
        job = _job(u)
        row = _row(job, next_attempt_at=timezone.now() + timedelta(minutes=5))
        assert claim_due_rows(limit=10) == []

    def test_skips_disabled_callback(self, partner, db):
        u, raw = partner
        job = _job(u)
        job.callback.status = "disabled"
        job.callback.save()
        row = _row(job)
        assert claim_due_rows(limit=10) == []

    def test_concurrent_claim_no_overlap(self, partner, db, django_db_blocker):
        """Two claims in sequence (simulating two sweepers): the second must
        not re-claim a row still under lease."""
        u, raw = partner
        job = _job(u)
        row = _row(job)
        first = claim_due_rows(limit=10)
        second = claim_due_rows(limit=10)  # lease still held → nothing due
        assert row.pk in [r.pk for r in first]
        assert second == []

    def test_stale_lease_reclaimable_and_counts(self, partner, db):
        """Lease expiry (worker died): row reclaimable, attempts +1 (poison
        events still reach exhaustion)."""
        u, raw = partner
        job = _job(u)
        row = _row(job, attempts=0)
        row.state = models.EventOutbox.STATE_LEASED
        row.locked_until = timezone.now() - timedelta(minutes=6)
        row.save()
        claimed = claim_due_rows(limit=10)
        assert row.pk in [r.pk for r in claimed]
        row.refresh_from_db()
        assert row.attempts == 1  # the dead lease burned an attempt


class TestOutcome:
    def test_mark_delivered(self, partner, db):
        u, raw = partner
        job = _job(u)
        row = _row(job)
        claim_due_rows(limit=10)
        mark_delivered(row)
        row.refresh_from_db()
        assert row.state == models.EventOutbox.STATE_DELIVERED
        assert row.delivered_at is not None
        job.callback.refresh_from_db()
        assert job.callback.delivered_count == 1
        assert job.callback.last_delivered_at is not None

    @pytest.mark.parametrize("attempt,expected_key", [
        (0, "10s"), (1, "1m"), (2, "10m"), (3, "1h"), (4, "6h"),
    ])
    def test_backoff_schedule(self, partner, db, attempt, expected_key):
        u, raw = partner
        job = _job(u)
        row = _row(job, attempts=attempt)
        mark_attempt_failed(row, error="conn refused")
        row.refresh_from_db()
        assert row.state == models.EventOutbox.STATE_PENDING
        delta = (row.next_attempt_at - timezone.now()).total_seconds()
        expected = BACKOFF_SECONDS[expected_key]
        assert abs(delta - expected) < 30, f"attempt {attempt}: ~{delta:.0f}s, expected ~{expected}s"
        assert row.attempts == attempt + 1
        job.callback.refresh_from_db()
        assert "conn refused" in job.callback.last_failure

    def test_exhaustion_disables_callback(self, partner, db):
        u, raw = partner
        job = _job(u)
        row = _row(job, attempts=5)  # 6th failure = exhausted
        mark_attempt_failed(row, error="dead endpoint")
        row.refresh_from_db()
        assert row.state == models.EventOutbox.STATE_PERMANENTLY_FAILED
        job.callback.refresh_from_db()
        assert job.callback.status == "disabled"
        assert "exhaust" in job.callback.disabled_reason.lower()

    def test_permanently_failed_not_reclaimed(self, partner, db):
        u, raw = partner
        job = _job(u)
        row = _row(job, state=models.EventOutbox.STATE_PERMANENTLY_FAILED,
                   next_attempt_at=timezone.now() - timedelta(hours=1))
        assert claim_due_rows(limit=10) == []


class TestSweep:
    def test_sweep_claims_and_delivers(self, partner, db):
        u, raw = partner
        job = _job(u)
        row = _row(job)
        with patch("scraper.events.dispatcher.deliver_callback") as task:
            n = sweep()
        assert n >= 1
        assert task.apply_async.called or task.delay.called

    def test_sweep_skips_without_callback(self, partner, db):
        """Jobs without a callback registration have no delivery target —
        rows stay pending (SSE/replay still consume them), not an error."""
        u, raw = partner
        job = _job(u, with_cb=False)
        row = _row(job)
        n = sweep()
        row.refresh_from_db()
        assert row.state == models.EventOutbox.STATE_PENDING


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ─────────────────────────────────────────────────────────────────────────────
# Delivery semantics (cycle 2)
# ─────────────────────────────────────────────────────────────────────────────

from scraper.events.dispatcher import _deliver  # noqa: E402


class TestDeliverHmac:
    @pytest.fixture(autouse=True)
    def public_dns(self, monkeypatch):
        from scraper.api import ssrf as ssrf_mod

        monkeypatch.setattr(ssrf_mod, "_resolve", lambda h: ["93.184.216.34"])

    def _row(self, u):
        job = _job(u)
        return _row(job)

    def test_signature_verifiable(self, partner, db):
        """The signed body must verify against the registered secret with
        the documented formula (async_api.yaml X-Scraper-Signature)."""
        import hashlib
        import hmac as hmac_mod

        u, raw = partner
        row = self._row(u)
        cb = row.job.callback
        posted = {}

        def fake_post(url, content=None, headers=None, timeout=None, follow_redirects=None):
            posted["url"] = url
            posted["content"] = content
            posted["headers"] = headers
            return httpx.Response(200, request=httpx.Request("POST", url))

        import httpx

        with patch("httpx.post", side_effect=fake_post):
            ok, err = _deliver(row)
        assert ok, err
        sig = posted["headers"]["X-Scraper-Signature"]
        assert sig.startswith("t=") and ",v1=" in sig
        ts, v1 = sig[2:].split(",v1=")
        expected = hmac_mod.new(
            cb.secret.encode(), f"{ts}.".encode() + posted["content"], hashlib.sha256
        ).hexdigest()
        assert v1 == expected
        assert posted["headers"]["X-Scraper-Event-Id"] == row.event_id

    def test_2xx_success_non_2xx_fail(self, partner, db):
        import httpx

        u, raw = partner
        row = self._row(u)

        def resp_500(url, **kw):
            return httpx.Response(500, request=httpx.Request("POST", url))

        with patch("httpx.post", side_effect=resp_500):
            ok, err = _deliver(row)
        assert not ok and "500" in err

    def test_no_redirects_followed(self, partner, db):
        """follow_redirects=False is passed explicitly (redirect SSRF)."""
        import httpx

        u, raw = partner
        row = self._row(u)
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw)
            return httpx.Response(200, request=httpx.Request("POST", url))

        with patch("httpx.post", side_effect=fake_post):
            _deliver(row)
        assert seen.get("follow_redirects") is False

    def test_ssrf_revalidation_blocks_rebinding(self, partner, db):
        """B4: URL validated at create resolves public; at delivery it
        resolves INTERNAL (rebinding) → permanently_failed, callback
        disabled, and NO POST happens."""
        u, raw = partner
        row = self._row(u)
        posted = []

        with patch("scraper.api.ssrf.validate_callback_url",
                   return_value="resolves to a non-public address"), \
             patch("httpx.post", side_effect=lambda *a, **k: posted.append(1)):
            ok, err = _deliver(row)
        assert not ok and "ssrf" in err
        assert posted == []  # never POSTed
        row.refresh_from_db()
        assert row.state == models.EventOutbox.STATE_PERMANENTLY_FAILED
        row.job.callback.refresh_from_db()
        assert row.job.callback.status == "disabled"
        assert "SSRF" in row.job.callback.disabled_reason

    def test_transport_error_is_retryable(self, partner, db):
        import httpx

        u, raw = partner
        row = self._row(u)
        with patch("httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
            ok, err = _deliver(row)
        assert not ok and "transport" in err
        row.refresh_from_db()
        assert row.state == models.EventOutbox.STATE_PENDING  # scheduled retry
