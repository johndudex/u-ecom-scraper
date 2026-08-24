"""Outbox dispatcher (async_api.yaml x-retry; fold B5/M1/M3).

claim_due_rows: SELECT FOR UPDATE SKIP LOCKED + lease CAS — concurrent
sweepers never double-claim; a dead worker's lease (locked_until past)
reclaims AND burns an attempt so poison events reach exhaustion.

Retry schedule: 6 attempts (initial + 5), backoff gaps 10s/1m/10m/1h/6h.
Legs < 1m wait for the 30s beat sweep (quantized — spec documents this);
legs >= 1m are self-scheduled with exact countdowns by the delivering
task. Celery's retry_backoff is deliberately unused (2^n, 600s default
clamp would silently break the 1h/6h legs).

The dispatcher lives on the dedicated `events` queue (fold B5): delivery
HTTP never shares the scrape workers' 2-slot pool.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..models import EventOutbox, JobCallback

logger = logging.getLogger("scraper.events")

# async_api.yaml x-retry: gaps between the 6 attempts
BACKOFFS = [10, 60, 600, 3600, 21600]  # 10s, 1m, 10m, 1h, 6h
LEASE_SECONDS = 300  # dead-worker detection window
MAX_ATTEMPTS = 6


def claim_due_rows(limit: int = 50) -> list[EventOutbox]:
    """Lease due rows atomically. Returns claimed rows (state=leased)."""
    now = timezone.now()
    with transaction.atomic():
        due = (
            EventOutbox.objects
            .select_for_update(skip_locked=True)
            .filter(
                state__in=[EventOutbox.STATE_PENDING, EventOutbox.STATE_LEASED],
                next_attempt_at__lte=now,
                job__callback__status=JobCallback.STATUS_ACTIVE,
            )
            .select_related("job", "job__callback")
            .order_by("next_attempt_at")[:limit]
        )
        claimed = []
        for row in due:
            stale = (
                row.state == EventOutbox.STATE_LEASED
                and row.locked_until and row.locked_until < now
            )
            fresh_pending = row.state == EventOutbox.STATE_PENDING
            if not (stale or fresh_pending):
                continue  # actively leased by a live worker
            if stale:
                row.attempts += 1  # the dead lease burned an attempt
            row.state = EventOutbox.STATE_LEASED
            row.locked_until = now + timedelta(seconds=LEASE_SECONDS)
            row.save(update_fields=["state", "locked_until", "attempts"])
            claimed.append(row)
        return claimed


def mark_delivered(row: EventOutbox) -> None:
    with transaction.atomic():
        row.state = EventOutbox.STATE_DELIVERED
        row.delivered_at = timezone.now()
        row.locked_until = None
        row.save(update_fields=["state", "delivered_at", "locked_until"])
        cb = row.job.callback
        cb.delivered_count += 1
        cb.last_delivered_at = timezone.now()
        cb.last_failure = ""
        cb.save(update_fields=["delivered_count", "last_delivered_at", "last_failure"])


def mark_attempt_failed(row: EventOutbox, error: str) -> None:
    """One failed attempt: schedule the next leg or exhaust."""
    reason = str(error)[:500]
    with transaction.atomic():
        row.attempts += 1
        cb = row.job.callback
        cb.last_failure = reason
        if row.attempts >= MAX_ATTEMPTS:
            row.state = EventOutbox.STATE_PERMANENTLY_FAILED
            row.next_attempt_at = None
            row.locked_until = None
            cb.status = JobCallback.STATUS_DISABLED
            cb.disabled_reason = (
                f"delivery exhausted after {row.attempts} attempts over the retry ladder"
            )
            cb.save(update_fields=["status", "disabled_reason", "last_failure"])
            logger.warning(
                "outbox %s: exhausted → callback disabled (job %s)",
                row.event_id, row.job_id,
            )
        else:
            gap = BACKOFFS[min(row.attempts - 1, len(BACKOFFS) - 1)]
            row.state = EventOutbox.STATE_PENDING
            row.next_attempt_at = timezone.now() + timedelta(seconds=gap)
            row.locked_until = None
            cb.save(update_fields=["last_failure"])
        row.save(update_fields=[
            "attempts", "state", "next_attempt_at", "locked_until",
        ])
        # legs >= 1m self-schedule with an exact countdown when the caller
        # is the delivering task (deliver_callback handles that); the sweep
        # only quantizes the < 1m legs (documented in the spec).


def sweep() -> int:
    """Beat entry: claim + enqueue deliveries on the events queue.
    Returns the number of rows dispatched. (Connection hygiene lives in the
    celery task wrappers, not here — this function is also called in tests.)"""
    rows = claim_due_rows(limit=50)
    if not rows:
        return 0
    n = 0
    for row in rows:
        try:
            # < 1m legs ride the next 30s sweep; >= 1m legs self-schedule
            # from the delivering task (exact countdown, M1)
            deliver_callback.apply_async(args=[row.pk], queue="events")
            n += 1
        except Exception as exc:
            logger.exception("sweep: enqueue failed for %s: %s", row.event_id, exc)
            # release the lease so the next sweep retries the enqueue
            row.state = EventOutbox.STATE_PENDING
            row.locked_until = None
            row.save(update_fields=["state", "locked_until"])
    return n


from ..tasks import shared_task  # noqa: E402  (register on the celery app)


@shared_task(queue="events", max_retries=0)
def deliver_callback(row_id: int) -> None:
    """Deliver one event: SSRF re-validate → HMAC-sign → POST → record."""
    from django.db import close_old_connections

    close_old_connections()
    row = (
        EventOutbox.objects.select_related("job", "job__callback")
        .filter(pk=row_id, state=EventOutbox.STATE_LEASED)
        .first()
    )
    if row is None:
        return  # lease expired + reclaimed, or already delivered
    ok, error = _deliver(row)
    if ok:
        mark_delivered(row)
    else:
        mark_attempt_failed(row, error)
        # legs >= 1m: exact countdown self-schedule (M1)
        if row.state == EventOutbox.STATE_PENDING:
            gap = BACKOFFS[min(row.attempts - 1, len(BACKOFFS) - 1)]
            if gap >= 60:
                deliver_callback.apply_async(
                    args=[row.pk], queue="events", countdown=gap,
                )


def _deliver(row: EventOutbox) -> tuple[bool, str]:
    """The POST. B4/R2 hard requirement: SSRF re-validation EVERY attempt
    (DNS rebinding between create and send; the 6h ladder is the amplifier).
    follow_redirects=False always."""
    import hashlib
    import hmac as hmac_mod
    import json as json_mod
    import time

    import httpx

    from ..api import ssrf as _ssrf

    cb = row.job.callback
    url = cb.url
    # pass the resolver explicitly — the default arg is import-time bound
    # and would ignore test patches (and future runtime resolver swaps)
    reason = _ssrf.validate_callback_url(url, resolver=_ssrf._resolve)
    if reason:
        # permanently_failed immediately — a rebinding attack or a DNS
        # change to an internal address must never be POSTed to
        with transaction.atomic():
            row.state = EventOutbox.STATE_PERMANENTLY_FAILED
            row.next_attempt_at = None
            row.locked_until = None
            cb.status = JobCallback.STATUS_DISABLED
            cb.disabled_reason = f"SSRF re-validation failed: {reason}"
            cb.save(update_fields=["status", "disabled_reason"])
            row.save(update_fields=["state", "next_attempt_at", "locked_until"])
        return False, f"ssrf: {reason}"

    body = json_mod.dumps(row.payload, separators=(",", ":"), default=str)
    ts = str(int(time.time()))
    sig = hmac_mod.new(
        cb.secret.encode(), f"{ts}.".encode() + body.encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Scraper-Signature": f"t={ts},v1={sig}",
        "X-Scraper-Event-Id": row.event_id,
        "X-Scraper-Job-Id": str(row.job_id),
        "User-Agent": "universal-scraper-events/1.0",
    }
    try:
        resp = httpx.post(
            url, content=body.encode(), headers=headers,
            timeout=httpx.Timeout(10.0, connect=10.0),
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        return False, f"transport: {exc}"
    if 200 <= resp.status_code < 300:
        return True, ""
    return False, f"http {resp.status_code}"
