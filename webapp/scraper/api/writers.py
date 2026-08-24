"""Write endpoints (slice 1a-ii): create + cancel + callback GET/PATCH.

create follows the M12/R2 contract: ONE transaction wraps job + JobCallback
+ events.emit(job.created); dispatch happens in transaction.on_commit ONLY
(the celery worker must never read an uncommitted row).
"""
from __future__ import annotations

import json
import logging
import secrets

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from .. import models
from ..events import emit
from . import errors
from .ssrf import validate_callback_url
from .views import _api_get_job, api_view

logger = logging.getLogger("scraper.api")


def _body(request) -> dict:
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise errors.ApiError(400, "validation_failed", "Request body is not valid JSON.")


INPUT_MODES = {"url_list", "list_page", "navigation", "search_term"}
TERMINAL = {"completed", "failed", "cancelled", "captcha_blocked", "akamai_blocked"}


@api_view(["POST"])
def create_job(request):
    body = _body(request)
    url = str(body.get("url", "")).strip()
    input_mode = str(body.get("input_mode", "")).strip()
    content_type = str(body.get("content_type", "product")).strip() or "product"
    item_urls = body.get("item_urls") or []
    listing_urls = body.get("listing_urls") or []
    search_criteria = str(body.get("search_criteria", "")).strip()
    callback_url = str(body.get("callback_url", "")).strip()
    callback_secret = str(body.get("callback_secret", "")).strip()

    if not url or input_mode not in INPUT_MODES:
        raise errors.ApiError(
            422, "validation_failed",
            "url (sample item page) and input_mode are required; "
            f"input_mode must be one of {sorted(INPUT_MODES)}.",
        )
    if input_mode == "url_list" and not item_urls:
        raise errors.ApiError(422, "validation_failed", "item_urls is required for url_list.")
    if input_mode == "list_page" and not listing_urls:
        raise errors.ApiError(422, "validation_failed", "listing_urls is required for list_page.")
    if input_mode == "search_term" and not search_criteria:
        raise errors.ApiError(422, "validation_failed", "search_criteria is required for search_term.")
    if item_urls and (len(item_urls) > 10000 or any(len(u) > 1000 for u in item_urls)):
        raise errors.ApiError(422, "validation_failed", "item_urls: max 10000 items, 1000 chars each.")

    schema_text = str(body.get("schema_text", "")).strip()
    target_fields = body.get("target_fields") or []
    if schema_text:
        from src.schema_validation import validate_user_schema

        result = validate_user_schema(schema_text)
        if not result.valid:
            raise errors.ApiError(
                422, "schema_invalid", "schema_text failed validation.",
                {"issues": [{"code": i.code, "message": i.message} for i in result.issues]},
            )
        if not target_fields:
            target_fields = result.derived_fields

    cb = None
    if callback_url:
        # resolver=None → real DNS (create-time gate); tests inject via ssrf module
        from . import ssrf as _ssrf

        reason = validate_callback_url(callback_url, resolver=_ssrf._resolve)
        if reason:
            raise errors.ApiError(422, "invalid_callback_url", reason)
        if not (32 <= len(callback_secret) <= 256):
            raise errors.ApiError(
                422, "validation_failed", "callback_secret must be 32-256 characters."
            )

    # 409: live duplicate for THIS partner on the same URL
    existing = models.ScrapeJob.objects.filter(
        url=url, status__in=[models.ScrapeJob.STATUS_PENDING, models.ScrapeJob.STATUS_RUNNING],
        user=request.api_user,
    ).first()
    if existing:
        raise errors.ApiError(
            409, "duplicate_running_job", f"A job for this URL is already running (Job #{existing.id}).",
            {"existing_job_id": existing.id},
        )

    scope = str(body.get("scope", "all")).strip() or "all"
    with transaction.atomic():
        job = models.ScrapeJob.objects.create(
            url=url,
            product_url=url,
            page_type=content_type,
            input_mode=input_mode,
            search_criteria=search_criteria,
            search_url=str(body.get("search_url", "")).strip(),
            target_fields=target_fields,
            scope=scope,
            scope_value=str(body.get("scope_value", "")).strip(),
            notes=str(body.get("notes", "")).strip()[:4000],
            title=str(body.get("title", "")).strip()[:200],
            schema_text=schema_text,
            full_extraction=False,
            skip_approvals=True,
            created_via="api",
            user=request.api_user,
        )
        if callback_url:
            cb = models.JobCallback.objects.create(
                job=job, url=callback_url, secret=callback_secret
            )
        emit(
            job, "job.created",
            {
                "state": "inprogress",
                "url": url,
                "content_type": content_type,
                "input_mode": input_mode,
                "callback": ({"url": cb.url, "status": "active"} if cb else None),
            },
            dedupe_key="created",
        )

    def _dispatch():
        from ..tasks import run_scrape_task

        task = run_scrape_task.delay(job.id, rescrape=False)
        models.ScrapeJob.objects.filter(pk=job.pk).update(celery_task_id=task.id)

    transaction.on_commit(_dispatch)

    return JsonResponse(
        {"job_id": job.id, "status_url": f"/api/v1/jobs/{job.id}"}, status=202
    )


# ── cancel ──────────────────────────────────────────────────────────────────

_CANCELLABLE = {
    models.ScrapeJob.STATUS_PENDING,
    models.ScrapeJob.STATUS_RUNNING,
    models.ScrapeJob.STATUS_WAITING_APPROVAL,
}


@api_view(["POST"])
def cancel_job(request, job_id: int):
    job = _api_get_job(request, job_id)
    if job.status not in _CANCELLABLE:
        if job.status == models.ScrapeJob.STATUS_CANCELLED:
            return JsonResponse({"job_id": job.id, "state": "failed",
                                 "failure": {"code": "cancelled", "message": None}})
        raise errors.ApiError(
            409, "not_cancellable", f"Job {job.id} is terminal ({job.status}).",
            {"state": "completed" if job.status == "completed" else "failed"},
        )
    with transaction.atomic():
        job.status = models.ScrapeJob.STATUS_CANCELLED
        job.completed_at = job.completed_at or timezone.now()  # reconciler key
        job.save(update_fields=["status", "completed_at"])
        emit(job, "job.failed", {"reason": "cancelled"}, dedupe_key="failed")
    if job.celery_task_id:
        try:
            from ..tasks import run_scrape_task

            run_scrape_task.AsyncResult(job.celery_task_id).revoke(terminate=True)
        except Exception as e:
            logger.warning("api cancel: could not revoke %s: %s", job.celery_task_id, e)
    return JsonResponse({"job_id": job.id, "state": "failed",
                         "failure": {"code": "cancelled", "message": None}})


# ── callback read/patch ─────────────────────────────────────────────────────

def _callback_payload(cb) -> dict:
    pending = models.EventOutbox.objects.filter(job_id=cb.job_id).exclude(
        state=models.EventOutbox.STATE_DELIVERED
    ).count()
    return {
        "status": cb.status,
        "url": cb.url,
        "disabled_reason": cb.disabled_reason or None,
        "last_failure": cb.last_failure or None,
        "delivered_count": cb.delivered_count,
        "pending_count": pending,
        "created_at": cb.created_at,
        "last_delivered_at": cb.last_delivered_at,
    }


@api_view(["GET"])
def get_job_callback(request, job_id: int):
    job = _api_get_job(request, job_id)
    cb = getattr(job, "callback", None)
    if cb is None:
        return JsonResponse({"callback": None})
    return JsonResponse(_callback_payload(cb))


@api_view(["PATCH"])
def patch_job_callback(request, job_id: int):
    job = _api_get_job(request, job_id)
    cb = getattr(job, "callback", None)
    if cb is None:
        raise errors.not_found(f"Callback registration for job {job.id}")
    body = _body(request)
    action = body.get("action")
    if action not in ("reenable", "rotate"):
        raise errors.ApiError(422, "validation_failed", "action must be reenable|rotate.")

    if action == "reenable":
        if cb.status == models.JobCallback.STATUS_ACTIVE:
            raise errors.ApiError(
                409, "callback_already_active", "Callback is active; use action=rotate to change it."
            )
        # 60 s cooldown per job (spec: re-enable hardening)
        last = models.EventOutbox.objects.filter(
            job=job, event_type="callback.reenabled"
        ).order_by("-created_at").first()
        if last and (timezone.now() - last.created_at).total_seconds() < 60:
            raise errors.ApiError(429, "rate_limited", "Re-enable cooldown: 60 s between attempts.",
                                  {"retry_after": 60})
        with transaction.atomic():
            cb.status = models.JobCallback.STATUS_ACTIVE
            cb.disabled_reason = ""
            cb.save(update_fields=["status", "disabled_reason"])
            emit(job, "callback.reenabled", {})  # no dedupe: cooldown counts these
        return JsonResponse(_callback_payload(cb))

    # rotate
    new_url = str(body.get("callback_url", "")).strip()
    new_secret = str(body.get("callback_secret", "")).strip()
    if not new_url and not new_secret:
        raise errors.ApiError(422, "validation_failed", "rotate requires callback_url and/or callback_secret.")
    if new_url:
        from . import ssrf as _ssrf

        reason = validate_callback_url(new_url, resolver=_ssrf._resolve)
        if reason:
            raise errors.ApiError(422, "invalid_callback_url", reason)
    if new_secret and not (32 <= len(new_secret) <= 256):
        raise errors.ApiError(422, "validation_failed", "callback_secret must be 32-256 characters.")
    with transaction.atomic():
        if new_url:
            cb.url = new_url
        if new_secret:
            cb.secret = new_secret
        cb.status = models.JobCallback.STATUS_ACTIVE  # rotation re-arms
        cb.disabled_reason = ""
        cb.save(update_fields=["url", "secret", "status", "disabled_reason"])
    return JsonResponse(_callback_payload(cb))


# ── sample read ─────────────────────────────────────────────────────────────

def _fm_read_json(key: str):
    try:
        import src.artifacts as artifacts

        return artifacts.read_json(key)
    except Exception:
        return None


@api_view(["GET"])
def get_job_sample(request, job_id: int):
    job = _api_get_job(request, job_id)
    from ..models import Step
    from .state import sample_ready as gate

    testing_done = Step.objects.filter(
        job=job, phase="testing", completed_at__isnull=False
    ).exists()
    # m4 state-gate at the endpoint: live jobs only (a terminal job with a
    # finalize-stamped testing step must not claim a sample)
    if not gate(job, testing_done):
        raise errors.ApiError(404, "not_ready", "Sample is not available for this job.")
    slug = (job.site_folder or "").strip("/").split("/")[-1] if job.site_folder else ""
    if not slug:
        raise errors.ApiError(404, "not_ready", "Sample is not available for this job.")
    data = _fm_read_json(f"scrapers/{slug}/samples/sample-{job.id}.json")
    if not data or not data.get("records"):
        raise errors.ApiError(404, "not_ready", "Sample is not available for this job.")
    return JsonResponse({
        "job_id": job.id,
        "records": data["records"],
        "record_count": len(data["records"]),
    })
