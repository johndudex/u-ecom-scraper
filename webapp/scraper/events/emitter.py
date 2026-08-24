"""EventOutbox writer + ULID generator.

Design decisions (docs/plans/api-plans-fold-r2.md):
- Explicit emit() + transaction.on_commit, NOT signals — status writes are
  scattered over ~20 sites and signals cannot know previous state (B D1).
- Idempotent via (job, event_type, dedupe_key) unique constraint; a retry
  cycle re-emitting sample_ready is a no-op after the first write.
- ULID event_id: 26-char Crockford base32, lexicographically sortable —
  the spec's format and the Phase-2.5 replay cursor. Process-local
  monotonicity guard (threading.Lock + same-ms increment); cross-process
  ordering is documented as best-effort (m8) — dedupe prevents duplicates,
  the reconciler sorts by (created_at, id) on read.
"""
from __future__ import annotations

import datetime as _dt
import os
import secrets
import threading

from django.db import IntegrityError, transaction

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_LOCK = threading.Lock()
_LAST = {"ts": 0, "rand": 0}


def new_event_id() -> str:
    """Monotonic-within-process ULID (48-bit ms timestamp + 80-bit randomness)."""
    with _LOCK:
        ts = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
        if ts == _LAST["ts"]:
            _LAST["rand"] += 1
            rand = _LAST["rand"]
        else:
            # 80 bits of randomness, masked to keep it strictly under 2^80
            rand = int.from_bytes(os.urandom(10), "big") & ((1 << 80) - 1)
            _LAST["ts"], _LAST["rand"] = ts, rand
        stamp = ts
        value = rand
    out = []
    for shift in range(45, -1, -5):
        out.append(_ENCODING[(stamp >> shift) & 0x1F])
    for shift in range(75, -1, -5):
        out.append(_ENCODING[(value >> shift) & 0x1F])
    return "".join(out)


def _publish_envelope(job_id: int, envelope: dict) -> None:
    """Redis fan-out on the envelope channel (post-commit). Best-effort:
    delivery is the outbox's job; Redis only feeds live SSE."""
    try:
        import json

        from scraper.services import _get_redis

        conn = _get_redis()
        conn.publish(
            f"job:{job_id}:envelope", json.dumps(envelope, default=str)
        )
    except Exception:
        # Never let fan-out break the state change it accompanies.
        import logging

        logging.getLogger("scraper.events").exception(
            "envelope redis publish failed for job %s", job_id
        )


def emit(job, event_type: str, data: dict, dedupe_key: str = ""):
    """Write one EventOutbox row (idempotent on dedupe_key).

    Returns the row, or None when: the job is not a partner job
    (created_via != "api"), or a row with the same dedupe key exists.
    The caller MUST be inside the same transaction as the state change
    it describes (that is what makes the outbox row + the status write
    atomic; on_commit schedules the Redis publish).
    """
    if getattr(job, "created_via", "intake") != "api":
        return None
    from ..models import EventOutbox

    envelope = {
        "event_id": new_event_id(),
        "type": event_type,
        "occurred_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "job_id": job.id,
        "user_id": getattr(getattr(job, "user", None), "id", None),
        "data": data,
    }
    try:
        with transaction.atomic():
            row = EventOutbox.objects.create(
                event_id=envelope["event_id"],
                job_id=job.id,
                user_id=envelope["user_id"],
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload=envelope,
                state=EventOutbox.STATE_PENDING,
                next_attempt_at=_dt.datetime.now(_dt.timezone.utc),
            )
    except IntegrityError:
        return None  # dedupe hit — first write wins (sample_ready on retry)

    transaction.on_commit(lambda: _publish_envelope(job.id, envelope))
    return row
