"""api_view wrapper + the v1 endpoint views (slice 1a-i: read-only).

api_view enforces the whole request pipeline the spec assumes:
csrf-exempt (token auth, no cookies) → X-API-Key resolution → per-key
rate limit → handler → ApiError envelope conversion.
"""
from __future__ import annotations

import functools
import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import errors
from .auth import resolve_api_key
from .ratelimit import check_rate_limit

logger = logging.getLogger("scraper.api")


def api_view(methods):
    """Decorate a partner-API handler: auth + rate limit + error envelope."""
    def decorator(fn):
        @csrf_exempt
        @require_http_methods(methods)
        @functools.wraps(fn)
        def wrapped(request, *args, **kwargs):
            try:
                request.api_user, request.api_key = resolve_api_key(request)
                # identity = the UNIQUE key_hash — the display prefix is
                # only 8 chars and would pool every partner into one bucket
                retry_after = check_rate_limit(request.api_key.key_hash[:16])
                if retry_after is not None:
                    resp = JsonResponse(
                        errors.rate_limited("10 req/s (burst 30)").body(), status=429
                    )
                    resp["Retry-After"] = str(retry_after)
                    return resp
                return fn(request, *args, **kwargs)
            except errors.ApiError as e:
                resp = JsonResponse(e.body(), status=e.status)
                if e.status == 429:
                    resp["Retry-After"] = "1"
                return resp
            except Exception:
                logger.exception("api 500 on %s %s", request.method, request.path)
                e = errors.internal_error()
                return JsonResponse(e.body(), status=500)
        return wrapped
    return decorator


def _api_get_job(request, job_id):
    """Tenant-scoped job fetch: own job or 404 (never 403 — no oracle).

    Raises the API's ApiError envelope, not Django's Http404 — the
    partner surface never renders an HTML 404 page.
    """
    from ..models import ScrapeJob

    from . import errors as _e

    job = ScrapeJob.objects.filter(pk=job_id, user=request.api_user).first()
    if job is None:
        raise _e.not_found(f"Job {job_id}")
    return job
