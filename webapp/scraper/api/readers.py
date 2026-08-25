"""Read-only partner endpoints (slice 1a-i).

check-site  POST /api/v1/check-site        — Site metadata only, NO
                                             cross-tenant field lists
                                             (internal intake_check_site's
                                             leak, by design absent here)
validate-schema  POST /api/v1/validate-schema
status      GET  /api/v1/jobs/{id}
list        GET  /api/v1/jobs
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from django.core.paginator import Paginator
from django.http import JsonResponse

from ..models import ScrapeJob, Site
from . import errors
from .state import partner_state, sample_ready as sample_ready_fn
from .views import _api_get_job, api_view

logger = logging.getLogger("scraper.api")


def _parse_body(request) -> dict:
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise errors.ApiError(400, "validation_failed", "Request body is not valid JSON.")


@api_view(["POST"])
def check_site(request):
    body = _parse_body(request)
    url = str(body.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise errors.ApiError(422, "validation_failed", "url must be an absolute http(s) URL.")
    host = parsed.netloc.lower()

    site = (
        Site.objects.filter(url__icontains=host)
        .only("platform", "scraping_method", "site_type", "name", "last_scraped_at")
        .first()
    )
    # Security note (sync spec): known_site is an accepted, bounded
    # cross-tenant signal — boolean + platform string only, never fields.
    if site is None:
        return JsonResponse({"known_site": False, "platform": None})
    return JsonResponse(
        {
            "known_site": True,
            "platform": site.platform or None,
            "site_type": site.site_type or None,
            "site_name": site.name or None,
            "scraping_method": site.scraping_method or None,
            "last_scraped_at": site.last_scraped_at,
        }
    )


@api_view(["POST"])
def validate_schema(request):
    body = _parse_body(request)
    schema = body.get("schema")
    if not isinstance(schema, (dict, list)):
        raise errors.ApiError(
            422, "schema_invalid", "schema must be a JSON object (the schema document itself)."
        )
    from src.schema_validation import validate_user_schema

    result = validate_user_schema(json.dumps(schema))
    return JsonResponse(
        {
            "valid": result.valid,
            "issues": [
                {"code": i.code, "message": i.message, "severity": i.severity, "path": i.path}
                for i in result.issues
            ],
            "derived_fields": result.derived_fields,
            "detected_content_type": result.detected_content_type,
        }
    )


def _job_status_payload(job) -> dict:
    """The JobStatus projection (sync_api.yaml). Shared by status + list."""
    steps = list(job.steps.order_by("id").values("phase", "status", "started_at", "completed_at"))
    testing_done = any(
        s["phase"] == "testing" and s["completed_at"] is not None for s in steps
    )
    state = partner_state(job.status)
    # m4 state-gate: sample_available only while live (REST ≡ events)
    sample_available = sample_ready_fn(job, testing_done)
    phases = [
        {
            "phase": s["phase"],
            "status": s["status"],
            "started_at": s["started_at"],
            "completed_at": s["completed_at"],
        }
        for s in steps
    ]
    failure = None
    if state == "failed":
        from .state import failure_code

        failure = {
            "code": failure_code(job.status),
            "message": (job.error_message or "")[:500] or None,
        }
    return {
        "job_id": job.id,
        "state": state,
        "internal_status": job.status,
        "url": job.url,
        "input_mode": job.input_mode,
        "content_type": job.page_type,
        "title": job.title or None,
        "site_name": job.site_name or None,
        "platform": job.platform or None,
        "scraping_method": job.scraping_method or None,
        "current_phase": next((s["phase"] for s in reversed(steps) if s["status"] == "running"), None),
        "phases": phases,
        "sample_available": sample_available,
        "output_available": bool(job.output_file),
        "scraper_available": bool(job.scraper_file),
        "item_count": job.product_count if state != "inprogress" else None,
        "output_filename": job.output_file or None,
        "callback": _callback_summary(job),
        "failure": failure,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


def _callback_summary(job) -> dict | None:
    cb = getattr(job, "callback", None)
    if cb is None:
        return None
    from ..models import EventOutbox

    pending = EventOutbox.objects.filter(job=job).exclude(
        state=EventOutbox.STATE_DELIVERED
    ).count()
    return {
        "status": cb.status,
        "disabled_reason": cb.disabled_reason or None,
        "last_failure": cb.last_failure or None,
        "pending_count": pending,
    }


@api_view(["GET"])
def job_status(request, job_id: int):
    job = _api_get_job(request, job_id)
    return JsonResponse(_job_status_payload(job))


@api_view(["GET"])
def list_jobs(request):
    qs = (
        ScrapeJob.objects.filter(user=request.api_user)
        .select_related("callback")
        .order_by("-created_at")
    )
    try:
        page = max(1, int(request.GET.get("page", 1)))
        page_size = int(request.GET.get("page_size", 20))
        if not 1 <= page_size <= 100:
            raise ValueError
    except (TypeError, ValueError):
        raise errors.ApiError(422, "invalid_page_size", "page must be >= 1 and page_size in [1, 100].")
    created_since = request.GET.get("created_since", "").strip()
    if created_since:
        from django.utils.dateparse import parse_datetime

        dt = parse_datetime(created_since)
        if dt is None:
            raise errors.ApiError(422, "invalid_created_since", "created_since must be ISO-8601.")
        qs = qs.filter(created_at__gte=dt)

    paginator = Paginator(qs, page_size)
    if page > paginator.num_pages and paginator.num_pages > 0:
        raise errors.ApiError(422, "invalid_page", f"page must be <= {paginator.num_pages}.")
    rows = paginator.page(page).object_list
    # list rows are summaries (spec JobList) — no phases[] here
    payloads = []
    for job in rows:
        p = _job_status_payload(job)
        p.pop("phases", None)
        payloads.append(p)
    return JsonResponse(
        {
            "jobs": payloads,
            "page": page,
            "page_size": page_size,
            "total_items": paginator.count,
            "total_pages": paginator.num_pages,
        }
    )
