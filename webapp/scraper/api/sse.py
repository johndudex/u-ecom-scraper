"""Partner SSE: GET /api/v1/jobs/{id}/events (async_api.yaml jobEventsSse).

Auth: X-API-Key header (non-browser) OR ?token= single-use stream token
(browser EventSource cannot set headers — same limitation as WebSocket).
Tokens: 300s TTL, consumed atomically on first successful auth (Redis
SET EX / GETDEL — B D11's proven pattern).

Framing: every data: frame is one EventEnvelope JSON (the same payloads
callbacks deliver); : ping comment frames keep intermediaries from
idle-killing the stream (spec hard requirement — the gunicorn
WORKER-TIMEOUT storm is documented history).

Budget (fold M9): a GLOBAL Redis counter caps concurrent streams across
internal + partner surfaces — 2 sync workers means 2 streams = 0% HTTP
capacity. Over budget → 503 immediately, never a silent hang.
"""
from __future__ import annotations

import json
import logging
import secrets
import time

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .. import models
from ..services import _get_redis
from . import errors

logger = logging.getLogger("scraper.api")

# Global concurrent-stream budget (internal + partner share the workers).
# 2 gunicorn sync workers: 2 streams = zero remaining HTTP capacity, and
# the healthcheck death-spiral restarts the service. 1 keeps a worker
# alive; Railway can raise via env when the dedicated gateway ships.
import os

STREAM_BUDGET = int(os.environ.get("PARTNER_STREAM_BUDGET", "1"))
# hard lifetime of one stream; tests shrink this to drain promptly
STREAM_DEADLINE_SECONDS = int(os.environ.get("PARTNER_STREAM_DEADLINE", "3600"))
KEEPALIVE_SECONDS = 25  # < 30s silence rule
TOKEN_TTL = 300

TERMINAL = {
    models.ScrapeJob.STATUS_COMPLETED,
    models.ScrapeJob.STATUS_FAILED,
    models.ScrapeJob.STATUS_CANCELLED,
    models.ScrapeJob.STATUS_CAPTCHA_BLOCKED,
    models.ScrapeJob.STATUS_AKAMAI_BLOCKED,
}
# internal status → terminal envelope type (the 4-state projection)
_TERMINAL_EVENT = {
    models.ScrapeJob.STATUS_COMPLETED: "job.scraper_ready",
    models.ScrapeJob.STATUS_FAILED: "job.failed",
    models.ScrapeJob.STATUS_CANCELLED: "job.failed",
    models.ScrapeJob.STATUS_CAPTCHA_BLOCKED: "job.failed",
    models.ScrapeJob.STATUS_AKAMAI_BLOCKED: "job.failed",
}


def mint_stream_token(user) -> str:
    """Single-use, 300s stream token (spec ws-token machinery, reused)."""
    token = secrets.token_urlsafe(24)
    conn = _get_redis()
    conn.set(f"streamtoken:{token}", user.id, ex=TOKEN_TTL)
    return token


def _consume_stream_token(token: str):
    """Atomic single-use consume → user_id or None."""
    try:
        conn = _get_redis()
        user_id = conn.getdel(f"streamtoken:{token}")
        if user_id is None:
            return None
        return int(user_id)
    except Exception:
        return None


def _budget_take() -> bool:
    try:
        conn = _get_redis()
        current = conn.incr("sse:open:global")
        if current > STREAM_BUDGET:
            conn.decr("sse:open:global")
            return False
        return True
    except Exception:
        return True  # Redis down: fail-open on the budget (auth still holds)


def _budget_release() -> None:
    try:
        _get_redis().decr("sse:open:global")
    except Exception:
        pass


@csrf_exempt
def job_events_sse(request, job_id: int):
    """The partner event stream. Not routed through api_view — the wrapper
    wraps JsonResponses; a stream needs its own error handling."""
    # ── auth: header key OR single-use token ──
    api_user = None
    header_key = request.headers.get("X-API-Key", "").strip()
    if header_key:
        from .auth import resolve_api_key

        req_clone = request
        try:
            api_user, _key = resolve_api_key(req_clone)
        except errors.ApiError as e:
            return JsonResponse(e.body(), status=e.status)
    else:
        token = request.GET.get("token", "").strip()
        if not token:
            e = errors.unauthorized()
            return JsonResponse(e.body(), status=e.status)
        user_id = _consume_stream_token(token)
        if user_id is None:
            e = errors.unauthorized()
            return JsonResponse(e.body(), status=e.status)
        from django.contrib.auth.models import User

        api_user = User.objects.filter(pk=user_id, is_active=True).first()
        if api_user is None or api_user.is_superuser:
            e = errors.forbidden()
            return JsonResponse(e.body(), status=e.status)

    # ── tenant scope: own job or 404 ──
    job = models.ScrapeJob.objects.filter(pk=job_id, user=api_user).first()
    if job is None:
        e = errors.not_found(f"Job {job_id}")
        return JsonResponse(e.body(), status=e.status)

    # ── global stream budget ──
    if not _budget_take():
        return JsonResponse(
            {"code": "rate_limited", "message": "Stream budget exhausted.",
             "details": {"retry_after": 30}},
            status=503,
        )

    def _envelope(event_type: str, data: dict, event_id: str) -> str:
        env = {
            "event_id": event_id,
            "type": event_type,
            "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "job_id": job.id,
            "user_id": api_user.id,
            "data": data,
        }
        return f"data: {json.dumps(env, default=str)}\n\n"

    def event_stream():
        from scraper.events import new_event_id

        channel = f"job:{job.id}:envelope"
        pubsub = None
        terminal_seen = False
        try:
            yield ": ping\n\n"  # immediate keepalive (proxy handshake window)
            # open with the current state
            job.refresh_from_db()
            if job.status in TERMINAL:
                yield _envelope(
                    _TERMINAL_EVENT[job.status],
                    {"reason": job.status},
                    new_event_id(),
                )
                return
            yield _envelope("job.inprogress", {"internal_status": job.status}, new_event_id())
            try:
                pubsub = _get_redis().pubsub()
                pubsub.subscribe(channel)
            except Exception:
                pubsub = None
            last_ping = time.monotonic()
            last_status_check = time.monotonic()
            deadline = time.monotonic() + STREAM_DEADLINE_SECONDS
            while time.monotonic() < deadline:
                frame = None
                if pubsub is not None:
                    msg = pubsub.get_message(timeout=1.0)
                    if msg and msg.get("type") == "message":
                        frame = msg["data"]
                        if isinstance(frame, bytes):
                            frame = frame.decode("utf-8", "replace")
                if frame:
                    yield f"data: {frame}\n\n"
                    try:
                        env = json.loads(frame)
                        if str(env.get("type", "")).startswith("job.") and env.get("type") in (
                            "job.scraper_ready", "job.failed",
                        ):
                            terminal_seen = True
                    except json.JSONDecodeError:
                        pass
                    if terminal_seen:
                        return
                else:
                    if time.monotonic() - last_ping >= KEEPALIVE_SECONDS:
                        yield ": ping\n\n"
                        last_ping = time.monotonic()
                    # silent-channel terminal check (envelope channel is
                    # emit-only; a terminal status without our event still
                    # closes the stream honestly)
                    if time.monotonic() - last_status_check >= 10:
                        last_status_check = time.monotonic()
                        job.refresh_from_db()
                        if job.status in TERMINAL:
                            yield _envelope(
                                _TERMINAL_EVENT[job.status],
                                {"reason": job.status},
                                new_event_id(),
                            )
                            return
        finally:
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception:
                    pass
            _budget_release()

    from django.http import StreamingHttpResponse

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    resp["Connection"] = "keep-alive"
    return resp


@csrf_exempt
def ws_token(request):
    """POST /api/v1/ws-token — mint a single-use stream token (spec).

    X-API-Key auth (browser clients then use ?token= on the stream)."""
    if request.method != "POST":
        return JsonResponse(
            {"code": "validation_failed", "message": "POST required."}, status=405
        )
    from .auth import resolve_api_key

    try:
        user, _key = resolve_api_key(request)
    except errors.ApiError as e:
        return JsonResponse(e.body(), status=e.status)
    from ..models import ScrapeJob

    job_id = None
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
        job_id = body.get("job_id")
    except json.JSONDecodeError:
        pass
    if job_id is not None:
        job = ScrapeJob.objects.filter(pk=job_id, user=user).first()
        if job is None:
            e = errors.not_found(f"Job {job_id}")
            return JsonResponse(e.body(), status=e.status)
    token = mint_stream_token(user)
    return JsonResponse(
        {
            "token": token,
            "expires_in": TOKEN_TTL,
            "connect_url": f"/api/v1/jobs/{job_id}/events?token={token}" if job_id
            else None,
        },
        status=201,
    )
