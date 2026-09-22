"""Write endpoints (slice 1a-ii): create + cancel + callback GET/PATCH.

create follows the M12/R2 contract: ONE transaction wraps job + JobCallback
+ events.emit(job.created); dispatch happens in transaction.on_commit ONLY
(the celery worker must never read an uncommitted row).
"""
from __future__ import annotations

import json
import logging

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from .. import models
from ..dedupe import site_processing_history
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
    # The spec's field name is `search_keywords` (sync_api.yaml
    # CreateJobRequest: "Stored as search_criteria"); `search_criteria` is
    # kept as a legacy alias. The spec name wins when both are sent.
    search_criteria = str(body.get("search_keywords", "")).strip()
    if not search_criteria:
        search_criteria = str(body.get("search_criteria", "")).strip()
    callback_url = str(body.get("callback_url", "")).strip()
    callback_secret = str(body.get("callback_secret", "")).strip()

    # Deduped, order-preserving copies for persistence (spec: "Deduplicated
    # server-side"). list_page listings are newline-joined into
    # search_criteria — the field the browser path actually reads (the
    # CharField→TextField widening was done for exactly this; intake does
    # the same join server-side for its textarea form field).
    item_urls_deduped = list(dict.fromkeys(
        u.strip() for u in item_urls if isinstance(u, str) and u.strip()
    ))
    listing_urls_deduped = list(dict.fromkeys(
        u.strip() for u in listing_urls if isinstance(u, str) and u.strip()
    ))
    if input_mode == "list_page" and listing_urls_deduped:
        search_criteria = "\n".join(listing_urls_deduped)

    if not url or input_mode not in INPUT_MODES:
        raise errors.ApiError(
            422, "validation_failed",
            "url (sample item page) and input_mode are required; "
            f"input_mode must be one of {sorted(INPUT_MODES)}.",
        )
    # [wave-40 T5] the DEDUPED list decides: a payload whose entries are all
    # blank must not yield a url_list job (it would die ~4s into
    # setup_workspace with no URLs).
    if input_mode == "url_list" and not item_urls_deduped:
        raise errors.ApiError(422, "validation_failed", "item_urls is required for url_list.")
    if input_mode == "list_page" and not listing_urls:
        raise errors.ApiError(422, "validation_failed", "listing_urls is required for list_page.")
    if input_mode == "search_term" and not search_criteria:
        raise errors.ApiError(422, "validation_failed", "search_criteria is required for search_term.")
    if item_urls and (len(item_urls) > 10000 or any(len(u) > 1000 for u in item_urls)):
        raise errors.ApiError(422, "validation_failed", "item_urls: max 10000 items, 1000 chars each.")

    # [wave-32 B3] Host gate — the pipeline's F17 seed filter silently drops
    # cross-host listing URLs at runtime (587: a loveamika listing on a
    # marimekko job starved discovery for 2h22m); decline them here instead.
    # search_term has no URL fields in the API contract — nothing to check.
    if input_mode in ("list_page", "search_term"):
        from src.registrable import registrable_of

        job_reg = registrable_of(url)
        if job_reg:
            _offending = sorted({
                u for u in listing_urls_deduped
                if registrable_of(u) and registrable_of(u) != job_reg
            })
            if _offending:
                raise errors.ApiError(
                    422, "host_mismatch",
                    f"listing_urls must be on {job_reg} — cross-host URLs "
                    "would be silently dropped by the pipeline.",
                    {"offending_urls": _offending},
                )

    schema_text = str(body.get("schema_text", "")).strip()
    target_fields = body.get("target_fields") or []
    # [wave-36 F7] Partner contract guard: target_fields must be a list of
    # short key-like strings. A bare string today iterates per-character
    # inside schema_field_names and poisons the whole record contract.
    if target_fields and not isinstance(target_fields, list):
        raise errors.ApiError(
            422, "validation_failed",
            "target_fields must be an array of field-name strings.",
        )
    if target_fields:
        import re as _re

        for _f in target_fields:
            if not isinstance(_f, str) or not _re.fullmatch(
                r"[a-zA-Z0-9_ ]{1,64}", _f.strip()
            ):
                raise errors.ApiError(
                    422, "validation_failed",
                    "target_fields entries must be 1-64 character "
                    "field-name strings (letters, digits, spaces, "
                    "underscores).",
                )
        target_fields = [str(_f).strip() for _f in target_fields]
    field_notes: dict = {}
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
        # W27-4: schema descriptions are per-field instructions.
        from src.schema_validation import extract_field_notes

        field_notes = extract_field_notes(schema_text)

    # W27-4: field_instructions — the API surface for per-field guidance.
    # Strict (partner contract): wrong shapes/sizes are 422s, not silent drops.
    field_instructions = body.get("field_instructions")
    if field_instructions is not None:
        if not isinstance(field_instructions, dict):
            raise errors.ApiError(
                422, "validation_failed",
                "field_instructions must be an object mapping field names to instruction strings.",
            )
        if len(field_instructions) > 100:
            raise errors.ApiError(
                422, "validation_failed", "field_instructions: max 100 entries.",
            )
        for _k, _v in field_instructions.items():
            if not isinstance(_k, str) or not isinstance(_v, str) or not _v.strip():
                raise errors.ApiError(
                    422, "validation_failed",
                    "field_instructions: every key must be a field name and every value a non-empty instruction string.",
                )
            if len(_v) > 300:
                raise errors.ApiError(
                    422, "validation_failed",
                    f"field_instructions['{_k}'] is {len(_v)} chars; the limit is 300.",
                )
        field_notes = {**field_notes, **{k: v.strip() for k, v in field_instructions.items()}}

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

    # W31 tier-2: site-level duplicate gate — GLOBAL across tenants
    # (product decision 2026-09-14): any completed job on this host refuses
    # the create unless the partner explicitly re-sends with force=true.
    # force NEVER bypasses the live duplicate_running_job 409 above. Strict
    # parse: only JSON true forces (a "false" string must not).
    if body.get("force") is not True:
        hist = site_processing_history(url)
        if hist["processed"] and not hist["site_archived"]:
            raise errors.ApiError(
                409, "site_already_processed",
                f"Site {hist['host']} was already scraped. "
                "Re-send with \"force\": true to create a new job.",
                {
                    "site_slug": hist["site_slug"],
                    "prior_job_ids": [j["job_id"] for j in hist["prior_jobs"]],
                    "latest_job_id": (
                        hist["prior_jobs"][0]["job_id"] if hist["prior_jobs"] else None
                    ),
                    "status_url": (
                        f"/api/v1/jobs/{hist['prior_jobs'][0]['job_id']}"
                        if hist["prior_jobs"] else ""
                    ),
                },
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
            field_notes=field_notes,
            scope=scope,
            scope_value=str(body.get("scope_value", "")).strip(),
            notes=str(body.get("notes", "")).strip()[:4000],
            title=str(body.get("title", "")).strip()[:200],
            schema_text=schema_text,
            full_extraction=False,
            skip_approvals=True,
            # Opt-in like the intake checkbox ([dagster-opt-in]) — partners post
            # dagster_enabled=true only when they want the asset generated.
            dagster_enabled=bool(body.get("dagster_enabled", False)),
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
                # [W27-8 → async_api] lineage at creation. null for API-created
                # jobs today (re-runs are born UI-side); the field keeps the
                # event schema stable and matches JobStatus.rerun_of.
                "rerun_of": job.origin_job_id,
                "callback": ({"url": cb.url, "status": "active"} if cb else None),
            },
            dedupe_key="created",
        )

    def _dispatch():
        from ..tasks import dispatch_scrape_job

        # [wave-15 1.0] keystone: stamp BEFORE publish (see dispatch_scrape_job).
        dispatch_scrape_job(job.id, rescrape=False)

    def _persist_item_urls():
        """F1: url_list's input contract is scrapers/{slug}/input_urls.json —
        the pipeline falls back to it when Site.input_urls is empty
        (tasks.py _build_initial_state). Without this the created job runs
        with 0 URLs. Mirrors intake_create_job (views.py:2593-2607): best
        effort, an FM outage logs and never breaks create."""
        try:
            import src.artifacts as artifacts

            from ..tasks import _generate_slug

            slug = _generate_slug(url)
            artifacts.write_json(
                artifacts.scrapers_key(slug, "input_urls.json"),
                {"urls": item_urls_deduped},
            )
        except Exception as exc:
            logger.warning(
                "api create: could not persist item_urls for %s: %s", url[:80], exc
            )

    # Both side effects are post-commit: _dispatch must not observe an
    # uncommitted row, and an FM write must never leak out of a rolled-back
    # transaction. url_list jobs only — input_urls.json is not part of the
    # navigation/list_page/search_term contract.
    if input_mode == "url_list" and item_urls_deduped:
        transaction.on_commit(_persist_item_urls)
    transaction.on_commit(_dispatch)

    # 202 = the spec's JobCreated schema (sync_api.yaml): required
    # [job_id, state, created_at, status_url] + the derived artifact URLs,
    # and a Location header pointing at the status resource.
    jid = job.id
    return JsonResponse(
        {
            "job_id": jid,
            "state": "inprogress",
            "created_at": job.created_at.isoformat().replace("+00:00", "Z"),
            "status_url": f"/api/v1/jobs/{jid}",
            "sample_url": f"/api/v1/jobs/{jid}/sample",
            "output_url": f"/api/v1/jobs/{jid}/output",
            "output_download_url": f"/api/v1/jobs/{jid}/output/download",
            "scraper_code_url": f"/api/v1/jobs/{jid}/scraper-code",
        },
        status=202,
        headers={"Location": f"/api/v1/jobs/{jid}"},
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
    # m4 state-gate, wave-27 e2e correction: the gate's intent is "a job must
    # not claim a sample it never produced" — FILE EXISTENCE is the precise
    # test, so terminal jobs (completed/failed) now serve the artifact when
    # it exists (spec: terminal failed does not imply no data). Live jobs
    # still need testing stamped; anything else (pending forever, etc.) 404s
    # and the file check below backstops every branch.
    if not gate(job, testing_done) and job.status not in TERMINAL:
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


# ── output endpoints ────────────────────────────────────────────────────────

def _fm_read_text(key: str) -> str:
    import src.artifacts as artifacts

    return artifacts.read_text(key)


def _fm_read_bytes(key: str):
    try:
        import src.artifacts as artifacts

        return artifacts.read(key)  # artifacts API: read() → bytes
    except Exception:
        return None


@api_view(["GET"])
def get_job_output(request, job_id: int):
    job = _api_get_job(request, job_id)
    from .output_index import read_output_page

    try:
        page = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", 100))
    except (TypeError, ValueError):
        from . import errors as _e

        raise _e.ApiError(422, "invalid_page", "page/page_size must be integers.")
    payload = read_output_page(job, page=page, page_size=page_size)
    return JsonResponse(payload)


def _stream_fm_file(key: str):
    """A7-1 (spec NORMATIVE): stream FM bytes, never buffer the whole file —
    a full read OOM'd 1 GB containers on aya-class outputs. httpx's
    stream() gives us chunked pass-through via the generator below."""
    import httpx

    import src.artifacts as artifacts

    url = artifacts.stream_url(key)
    return httpx.stream("GET", url, timeout=30.0)


@api_view(["GET"])
def download_job_output(request, job_id: int):
    job = _api_get_job(request, job_id)
    if not job.output_file:
        raise errors.ApiError(404, "output_not_found", "No output for this job.")
    filename = job.output_file.rsplit("/", 1)[-1]

    try:
        resp_stream = _stream_fm_file(job.output_file)
    except errors.ApiError:
        raise
    except Exception as exc:  # FM down/miss = fail-fast, never hang
        raise errors.ApiError(
            404, "output_not_found", "No output for this job."
        ) from exc

    def _chunks():
        with resp_stream as r:
            if r.status_code != 200:
                raise errors.ApiError(
                    404, "output_not_found", "No output for this job."
                )
            yield from r.iter_bytes()

    from django.http import StreamingHttpResponse

    resp = StreamingHttpResponse(_chunks(), content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ── scraper-code ────────────────────────────────────────────────────────────

@api_view(["GET"])
def get_job_scraper_code(request, job_id: int):
    """GET /api/v1/jobs/{id}/scraper-code — the generated Python source.

    ?format=json (default): {code, filename, size_bytes}; ?format=raw:
    bare text/x-python with an attachment header.
    """
    job = _api_get_job(request, job_id)
    if not job.scraper_file:
        raise errors.ApiError(404, "not_found", "No scraper was produced for this job.")
    try:
        code = _fm_read_text(job.scraper_file)
    except Exception:
        raise errors.ApiError(404, "not_found", "No scraper was produced for this job.")
    filename = job.scraper_file.rsplit("/", 1)[-1]
    if request.GET.get("format") == "raw":
        resp = HttpResponse(code, content_type="text/x-python")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    return JsonResponse({
        "code": code,
        "filename": filename,
        "size_bytes": len(code.encode("utf-8")),
        "url": f"/api/v1/jobs/{job.id}/scraper-code",
    })
