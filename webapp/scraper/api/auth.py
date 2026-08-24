"""X-API-Key authentication (sync_api.yaml securityScheme apiKeyHeader).

resolve_api_key() enforces the full auth-state machine:
- missing/malformed header        → 401
- unknown key (hash miss)         → 401 (indistinguishable from missing)
- revoked                         → 403
- owner is a superuser            → 403 (CODE-LEVEL mandate: superuser keys
                                     must never authenticate the API — the
                                     internal _get_job bypass would leak
                                     every tenant; fold B2)
- owner inactive                  → 403
Success returns (user, api_key); the view's job ownership checks do the rest
(cross-tenant job reads are 404 via _api_get_job, never 403).
"""
from __future__ import annotations

from django.utils import timezone

from ..models import ApiKey
from .errors import forbidden, unauthorized


def resolve_api_key(request):
    raw = request.headers.get("X-API-Key", "").strip()
    if not raw:
        raise unauthorized()
    key = ApiKey.objects.filter(key_hash=ApiKey.hash_key(raw)).select_related("user").first()
    if key is None:
        raise unauthorized()
    if key.revoked_at is not None:
        raise forbidden("API key has been revoked.")
    user = key.user
    if user.is_superuser:
        raise forbidden("Superuser accounts may not hold API keys.")
    if not user.is_active:
        raise forbidden("Owning account is inactive.")
    # Throttled last_used_at stamp (per-process dict; n3 — harmless)
    now = timezone.now()
    if not key.last_used_at or (now - key.last_used_at).total_seconds() > 300:
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
    return user, key
