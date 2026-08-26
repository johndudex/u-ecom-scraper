"""Token-manager backend for the /intake modal (user asked: why a shell
command when the user is already logged in?).

Rules:
- list: active keys only, BLURRED (prefix + dots) — the full key is never
  recoverable after create
- create: returns the full raw key exactly ONCE; max 3 active per user;
  superusers refused (the code-level mandate)
- revoke: own key only (other users' keys 404 — no oracle)
- connect: the WSS/SSE temp-token recipe (the second user concern: no
  clarity anywhere how a temp token is minted) surfaced right in the UI.
"""
from __future__ import annotations

import json
import secrets

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .. import models

MAX_ACTIVE_KEYS = 3


def _ajax(user_fn):
    """Shared guard: login_required (view-level) + AJAX + superuser refusal."""

    def wrap(request):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "login required"}, status=302)
        if request.user.is_superuser:
            return JsonResponse(
                {"code": "forbidden",
                 "message": (
                     "Admin accounts can't hold API keys — a superuser key "
                     "would bypass every tenant boundary. Create a normal "
                     "(non-admin) user for API access: Admin → Users → Add "
                     "User (leave both staff and superuser unchecked), log "
                     "in as that user, then generate keys here."
                 ),
                 "operator_hint": True},
                status=403,
            )
        if request.headers.get("x-requested-with") != "XMLHttpRequest":
            return JsonResponse({"error": "AJAX required"}, status=400)
        return user_fn(request)

    return wrap


def _connect_recipe():
    return {
        "rest": {
            "step1": "Send X-API-Key: <one of your keys> on every /api/v1/* request.",
        },
        "wss": {
            "step1": "POST /api/v1/ws-token with header X-API-Key: <key> → returns {token, expires_in: 300, connect_url}",
            "step2": "Connect to wss://<gateway-host>/ws/v1/jobs?token=<token> — single-use, expires in 300 s; mint a fresh token per connection (browsers can't set WS headers; non-browser clients may pass ?apikey=<key> instead).",
            "step3": "Send {\"op\":\"subscribe\",\"data\":{\"job_id\":N}} — the ack carries a state snapshot; events stream from there.",
        },
        "sse": {
            "step1": "Same token exchange (POST /api/v1/ws-token with X-API-Key)",
            "step2": "GET /api/v1/jobs/{job_id}/events?token=<token> (single-use, 300 s TTL)",
        },
    }


@csrf_exempt
@require_http_methods(["GET"])
def token_list(request):
    return _ajax(_list)(request)


def _list(request):
    keys = (
        models.ApiKey.objects.filter(user=request.user, revoked_at__isnull=True)
        .order_by("-created_at")
    )
    return JsonResponse({
        "keys": [
            {
                "id": k.id,
                "prefix": k.prefix,
                "blurred": k.prefix + "••••••••••••",
                "created_at": k.created_at,
                "last_used_at": k.last_used_at,
                "is_active": True,
            }
            for k in keys
        ],
        "max_active": MAX_ACTIVE_KEYS,
        "connect": _connect_recipe(),
    })


@csrf_exempt
@require_http_methods(["POST"])
def token_create(request):
    return _ajax(_create)(request)


def _create(request):
    active = models.ApiKey.objects.filter(user=request.user, revoked_at__isnull=True).count()
    if active >= MAX_ACTIVE_KEYS:
        return JsonResponse(
            {"code": "token_limit_reached",
             "message": f"Maximum {MAX_ACTIVE_KEYS} active keys. Revoke one first."},
            status=409,
        )
    raw = "pk_" + secrets.token_urlsafe(32)
    key = models.ApiKey.objects.create(
        user=request.user, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw),
        label="intake-ui",
    )
    return JsonResponse(
        {"id": key.id, "raw_key": raw, "prefix": key.prefix,
         "note": "Store this key now — it is shown ONCE and cannot be recovered."},
        status=201,
    )


@csrf_exempt
@require_http_methods(["POST"])
def token_revoke(request, key_id: int):
    return _ajax(lambda r: _revoke(r, key_id))(request)


def _revoke(request, key_id: int):
    key = models.ApiKey.objects.filter(
        pk=key_id, user=request.user, revoked_at__isnull=True
    ).first()
    if key is None:
        return JsonResponse(
            {"code": "not_found", "message": "No such active key."}, status=404
        )
    key.revoked_at = timezone.now()
    key.save(update_fields=["revoked_at"])
    return JsonResponse({"id": key.id, "revoked": True})
