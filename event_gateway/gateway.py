"""Partner WSS gateway — async_api.yaml /ws/v1/jobs (Phase 2).

Standalone FastAPI service (the file-master/browser_service pattern): the
Django tier stays WSGI; this owns the websocket surface only.

Auth (critique m7 — the FULL state machine, not hash-lookup-only):
  ?token=<single-use stream token>  (browser clients; GETDEL on Redis)
  subprotocol header X-API-Key      (non-browser; revoked/superuser/
                                     inactive all rejected)
Job scoping mirrors _api_get_job: own job or job_not_found (never a
tenant oracle).

Transport: one multiplexed connection; subscribe/unsubscribe by job_id;
subscribe.ack carries the state snapshot (the ONLY reconnect guarantee —
no replay buffer in 2.0); job.* envelopes fanned out from the emit()
Redis channel job:{id}:envelope; heartbeat.ping every <=25s of silence
(app-level, per the spec's proxy-idle rationale).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings as dj_settings  # noqa: E402
logger = logging.getLogger("event_gateway")

HEARTBEAT_SECONDS = 25  # spec x-heartbeat: <=25s of silence
TOKEN_TTL = 300

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _redis():
    import redis as redis_lib

    return redis_lib.from_url(REDIS_URL)


def _pg():
    """Raw psycopg (read-only auth + job lookups — no Django ORM here).

    Reads the connection parameters from Django settings AT CALL TIME so
    pytest-django's test-database swap is respected in tests.
    """
    import psycopg

    cfg = dj_settings.DATABASES["default"]
    return psycopg.connect(
        host=cfg["HOST"], port=cfg["PORT"], dbname=cfg["NAME"],
        user=cfg["USER"], password=cfg["PASSWORD"],
    )


# ── auth ────────────────────────────────────────────────────────────────────

def verify_api_key(raw_key: str) -> int | None:
    """X-API-Key path → user_id. Encodes the full auth state machine:
    unknown/missing → None; revoked → None; superuser owner → None
    (code-level mandate); inactive owner → None."""
    if not raw_key:
        return None
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id
            FROM scraper_apikey k
            JOIN auth_user u ON u.id = k.user_id
            WHERE k.key_hash = %s
              AND k.revoked_at IS NULL
              AND u.is_superuser = FALSE
              AND u.is_active = TRUE
            LIMIT 1
            """,
            (digest,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def check_ws_token(token: str) -> int | None:
    """Single-use stream token (shared with SSE) — atomic GETDEL."""
    if not token:
        return None
    try:
        v = _redis().getdel(f"streamtoken:{token}")
        if v is None:
            return None
        user_id = int(v)
    except Exception:
        return None
    # token → user must still be an eligible principal
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT is_superuser, is_active FROM auth_user WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row or row[0] or not row[1]:
        return None
    return user_id


# ── job lookup + snapshot (read-only) ──────────────────────────────────────

_TERMINAL_EVENT = {
    "completed": "scraper_ready",
    "failed": "failed",
    "cancelled": "failed",
    "captcha_blocked": "failed",
    "akamai_blocked": "failed",
}
_STATE_MAP = {
    "pending": "inprogress", "running": "inprogress", "waiting_approval": "inprogress",
    **{k: "failed" for k in ("failed", "cancelled", "captcha_blocked", "akamai_blocked")},
    "completed": "scraper_ready",
}


def _job_row(user_id: int, job_id: int):
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, product_count,
                   (output_file <> '')      AS has_output,
                   (scraper_file <> '')     AS has_scraper
            FROM scraper_scrapejob
            WHERE id = %s AND user_id = %s
            """,
            (job_id, user_id),
        )
        return cur.fetchone()


def _last_event_id(job_id: int) -> str | None:
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_id FROM scraper_eventoutbox WHERE job_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def build_snapshot(job_id: int, user_id: int) -> dict | None:
    """subscribe.ack's data — the late-joiner recovery snapshot."""
    row = _job_row(user_id, job_id)
    if row is None:
        return None
    _id, status, item_count, has_output, has_scraper = row
    state = _STATE_MAP.get(status, "failed")
    snapshot = {
        "last_event_id": _last_event_id(job_id),
        "item_count": item_count if state != "inprogress" else None,
        "sample_url": f"/api/v1/jobs/{job_id}/sample",
        "output_url": f"/api/v1/jobs/{job_id}/output",
        "scraper_code_url": f"/api/v1/jobs/{job_id}/scraper-code",
    }
    return {"job_id": job_id, "state": state, "snapshot": snapshot}


# ── control protocol ────────────────────────────────────────────────────────

def handle_control_sync(user_id: int, raw: str, subs: set) -> str | None:
    """Sync core — see handle_control (the async wrapper) for the contract.
    Runs psycopg; call from a worker thread in async contexts."""
    return _handle_control_body(user_id, raw, subs)


async def handle_control(user_id: int, raw: str, subs: set) -> str | None:
    """Async wrapper (tests + any loop-context caller): delegates to the
    sync core. The app's WS handler runs the core in an executor instead."""
    return _handle_control_body(user_id, raw, subs)


def _handle_control_body(user_id: int, raw: str, subs: set) -> str | None:
    """One inbound frame → zero or one outbound control frame.

    Returns None for frames that produce no reply (heartbeat.pong).
    Mutates `subs` for subscribe/unsubscribe.
    """
    try:
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError
        op = msg.get("op")
        data = msg.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return json.dumps({"op": "error", "data": {
            "error_code": "invalid_message", "message": "malformed control frame"}})

    if op == "subscribe":
        jid = data.get("job_id")
        if not isinstance(jid, int):
            return json.dumps({"op": "error", "data": {
                "error_code": "invalid_message", "message": "job_id must be an integer"}})
        snap = build_snapshot(jid, user_id)
        if snap is None:
            return json.dumps({"op": "subscribe.nack", "data": {
                "job_id": jid, "error_code": "job_not_found",
                "message": f"Job {jid} does not exist."}})
        subs.add(jid)
        return json.dumps({"op": "subscribe.ack", "data": snap})

    if op == "unsubscribe":
        jid = data.get("job_id")
        if not isinstance(jid, int):
            return json.dumps({"op": "error", "data": {
                "error_code": "invalid_message", "message": "job_id must be an integer"}})
        subs.discard(jid)  # idempotent per spec
        return json.dumps({"op": "unsubscribe.ack", "data": {"job_id": jid}})

    if op == "heartbeat.pong":
        return None

    return json.dumps({"op": "error", "data": {
        "error_code": "invalid_message", "message": f"unknown op {op!r}"}})


def _error_frame(error_code: str, message: str) -> dict:
    return {"op": "error", "data": {"error_code": error_code, "message": message}}
