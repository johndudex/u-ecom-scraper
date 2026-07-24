import json
import logging
import os
import subprocess
import time
from datetime import timedelta
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseNotFound,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render, reverse
from django.utils import timezone

from .forms import SiteForm
from .models import Approval, JobListing, ProbeCache, ScrapeJob, SessionLog, Site

logger = logging.getLogger(__name__)

try:
    import redis as redis_lib

    _redis_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    redis_client = redis_lib.from_url(_redis_url, decode_responses=True)
except Exception as e:
    logger.warning(f"Could not initialize Redis client for SSE: {e}")
    redis_client = None


def _check_site_tracker(url: str) -> dict | None:
    try:
        site = Site.objects.filter(url=url.rstrip("/")).first()
        if site:
            return {"url": site.url, "status": site.status, "dot": site.status}
    except Exception:
        pass
    return None


def _ordered_steps(job):
    """Return job's steps sorted by canonical pipeline phase order.

    Dynamically-created phases (e.g. ``navigation_analysis`` created on-the-fly
    by ``_notify_phase``) would otherwise appear at the end of the list when
    ordered by step ID, confusing the UI.
    """
    try:
        from .tasks import PIPELINE_PHASES

        order = {phase: i for i, phase in enumerate(PIPELINE_PHASES)}
    except Exception:
        order = {}
    return sorted(job.steps.all(), key=lambda s: order.get(s.phase, 999))


@login_required
def home(request):
    from .models import ContentType

    content_types = list(ContentType.objects.all())
    if request.method == "POST":
        form_data = request.POST
        url = form_data.get("url", "").strip()
        product_url = form_data.get("product_url", "").strip()
        currency = form_data.get("currency", "").strip().upper()
        page_type = form_data.get("page_type", "product").strip()
        search_criteria = form_data.get("search_criteria", "").strip()
        full_extraction = form_data.get("full_extraction") == "on"
        rescrape = form_data.get("rescrape") == "on"

        # Derive the canonical input_mode from the chosen page_type so that
        # navigation / list_page jobs route through the navigation agent.
        input_mode = "url_list"
        try:
            from src.content_types import resolve_page_type

            _, input_mode = resolve_page_type(page_type)
        except Exception:
            pass

        context = {
            "form_url": url,
            "form_product_url": product_url,
            "form_currency": currency,
            "form_page_type": page_type,
            "form_search_criteria": search_criteria,
            "recent_jobs": ScrapeJob.objects.filter(user=request.user)[:10],
            "content_types": content_types,
        }

        if not url:
            context["error"] = "URL is required"
            return render(request, "scraper/home.html", context)

        existing = ScrapeJob.objects.filter(
            url=url, status__in=[ScrapeJob.STATUS_PENDING, ScrapeJob.STATUS_RUNNING],
            user=request.user,
        ).first()
        if existing:
            context["error"] = (
                f"A job for this URL is already running (Job #{existing.id})"
            )
            return render(request, "scraper/home.html", context)

        site_info = _check_site_tracker(url)
        if site_info and not rescrape:
            context["site_exists"] = True
            context["site_exists_url"] = site_info["url"]
            context["site_status"] = site_info["status"]
            context["site_status_dot"] = site_info["dot"]
            context["error"] = (
                'Scraper already exists for this site. Check "Re-scrape" to run all steps again.'
            )
            return render(request, "scraper/home.html", context)

        job = ScrapeJob.objects.create(
            url=url,
            product_url=product_url,
            currency=currency,
            full_extraction=full_extraction,
            page_type=page_type,
            input_mode=input_mode,
            search_criteria=search_criteria,
            user=request.user,
        )

        from .tasks import run_scrape_task

        task = run_scrape_task.delay(job.id, rescrape=rescrape)
        job.celery_task_id = task.id
        job.save(update_fields=["celery_task_id"])
        return redirect("job_detail", job_id=job.id)

    recent_jobs = ScrapeJob.objects.filter(user=request.user)[:10]
    return render(request, "scraper/home.html", {"recent_jobs": recent_jobs, "content_types": content_types})


@login_required
def job_list(request):
    jobs = ScrapeJob.objects.filter(user=request.user).prefetch_related("steps", "approvals")[:]
    active_statuses = {
        ScrapeJob.STATUS_RUNNING,
        ScrapeJob.STATUS_WAITING_APPROVAL,
        ScrapeJob.STATUS_PENDING,
    }
    terminal_statuses = {
        ScrapeJob.STATUS_COMPLETED,
        ScrapeJob.STATUS_FAILED,
        ScrapeJob.STATUS_CANCELLED,
    }
    is_active_dict = {j.id: j.status in active_statuses for j in jobs}
    is_terminal_dict = {j.id: j.status in terminal_statuses for j in jobs}
    return render(
        request,
        "scraper/job_list.html",
        {
            "jobs": jobs,
            "is_active_dict": is_active_dict,
            "is_terminal_dict": is_terminal_dict,
        },
    )


@login_required
def job_detail(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    steps = _ordered_steps(job)
    for step in steps:
        if step.started_at and step.completed_at:
            delta = (step.completed_at - step.started_at).total_seconds()
            if delta < 60:
                step.duration_str = f"{delta:.0f}s"
            elif delta < 3600:
                m, s = divmod(delta, 60)
                step.duration_str = f"{m:.0f}m {s:.0f}s"
            else:
                h, rem = divmod(delta, 3600)
                m, s = divmod(rem, 60)
                step.duration_str = f"{h:.0f}h {m:.0f}m"
        else:
            step.duration_str = ""
    pending_approvals = job.approvals.filter(status=Approval.STATUS_PENDING).order_by(
        "-created_at"
    )
    all_approvals = job.approvals.all().order_by("-created_at")
    recent_logs = job.session_logs.order_by("seq")[:200]
    terminal_statuses = {
        ScrapeJob.STATUS_COMPLETED,
        ScrapeJob.STATUS_FAILED,
        ScrapeJob.STATUS_CANCELLED,
    }
    active_statuses = {
        ScrapeJob.STATUS_PENDING,
        ScrapeJob.STATUS_RUNNING,
        ScrapeJob.STATUS_WAITING_APPROVAL,
    }
    is_terminal = job.status in terminal_statuses
    is_active = job.status in active_statuses

    agent_stack = []
    for log in job.session_logs.filter(agent__gt="").order_by("seq")[:20]:
        agent_stack.append(
            {"agent": log.agent, "description": (log.content or "")[:80]}
        )

    scraper_code_display = ""
    has_scraper_code = False
    has_dagster_code = False
    sample_output = ""
    scraper_slug = ""

    output_files = []
    slug_candidates = []
    if job.site_folder:
        slug_candidates.append(job.site_folder.removeprefix("scrapers/"))
    if job.site_name:
        name_slug = job.site_name.lower().replace(" ", "-").replace(".", "-")
        for char in name_slug:
            if not char.isalnum() and char != "-":
                name_slug = name_slug.replace(char, "-")
        slug_candidates.append(name_slug)
    for slug in slug_candidates:
        scraper_path = os.path.join(
            settings.PROJECT_ROOT, "scrapers", slug, "scraper.py"
        )
        if os.path.exists(scraper_path):
            try:
                with open(scraper_path, "r") as f:
                    scraper_code_display = f.read()
                has_scraper_code = True
                scraper_slug = slug
                # Check for dagster file (scrapers/ or workspace/)
                dagster_path = os.path.join(
                    settings.PROJECT_ROOT, "scrapers", slug, f"{slug}_dagster.py"
                )
                if not os.path.exists(dagster_path):
                    dagster_path = os.path.join(
                        settings.PROJECT_ROOT, "workspace", slug, f"{slug}_dagster.py"
                    )
                if os.path.exists(dagster_path):
                    has_dagster_code = True
                break
            except Exception:
                pass

    if scraper_slug:
        site_dir = os.path.join(settings.PROJECT_ROOT, "scrapers", scraper_slug)
        if os.path.isdir(site_dir):
            job_start = job.started_at
            all_output = [
                f
                for f in os.listdir(site_dir)
                if f.startswith("output_") and f.endswith(".json")
            ]
            if job_start:
                filtered = []
                for f in all_output:
                    try:
                        ts = datetime.strptime(
                            f, "output_%Y-%m-%d_%H%M%S.json"
                        ).replace(tzinfo=dt_timezone.utc)
                        if ts >= job_start - timedelta(seconds=120):
                            filtered.append(f)
                    except ValueError:
                        filtered.append(f)
                output_files = sorted(filtered, reverse=True)
            else:
                output_files = sorted(all_output, reverse=True)

    fc_approval = (
        job.approvals.filter(approval_type=Approval.TYPE_FIELD_CONFIRM)
        .order_by("-created_at")
        .first()
    )
    if fc_approval and fc_approval.response_data:
        sample_output = fc_approval.response_data.get("sample_output", sample_output)

    tool_calls = job.tool_call_logs.order_by("call_seq")[:200]
    tool_call_agents = list(
        job.tool_call_logs.values_list("agent", flat=True).distinct()
    )

    db_site = Site.objects.filter(url=job.url.rstrip("/")).first() if job.url else None

    return render(
        request,
        "scraper/job_detail.html",
        {
            "job": job,
            "steps": steps,
            "pending_approvals": pending_approvals,
            "all_approvals": all_approvals,
            "recent_logs": recent_logs,
            "is_terminal": is_terminal,
            "is_active": is_active,
            "agent_stack": agent_stack,
            "has_scraper_code": has_scraper_code,
            "has_dagster_code": has_dagster_code,
            "scraper_code_display": scraper_code_display,
            "output_files": output_files,
            "site": db_site,
            "sample_output": sample_output,
            "tool_calls": tool_calls,
            "tool_call_agents": tool_call_agents,
        },
    )


@login_required
def job_cancel(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    if job.status in [
        ScrapeJob.STATUS_PENDING,
        ScrapeJob.STATUS_RUNNING,
        ScrapeJob.STATUS_WAITING_APPROVAL,
    ]:
        job.status = ScrapeJob.STATUS_CANCELLED
        job.save(update_fields=["status"])
        if job.celery_task_id:
            try:
                from .tasks import run_scrape_task

                run_scrape_task.AsyncResult(job.celery_task_id).revoke(terminate=True)
            except Exception as e:
                logger.warning(
                    f"Could not revoke Celery task {job.celery_task_id}: {e}"
                )
    return redirect("job_detail", job_id=job.id)


def _resolve_stored_path(stored: str) -> str | None:
    """Resolve a job's stored artifact path (scraper_file/dagster_file) to an
    absolute path that exists, or None. Handles absolute (/app/...) + relative.
    """
    if not stored:
        return None
    p = stored if os.path.isabs(stored) else os.path.join(settings.PROJECT_ROOT, stored)
    return p if os.path.isfile(p) else None


@login_required
def scraper_code(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)

    # Per-job attribution: this job's own scraper (if recorded) wins over the
    # shared scrapers/{slug}/scraper.py (which later jobs may overwrite).
    own = _resolve_stored_path(job.scraper_file)
    if own:
        with open(own, "r") as f:
            content = f.read()
        slug = _resolve_job_slug(job) or "scraper"
        resp = HttpResponse(content, content_type="text/x-python")
        resp["Content-Disposition"] = f'attachment; filename="{slug}_scraper.py"'
        return resp

    slug_candidates = []
    if job.site_folder:
        slug_candidates.append(job.site_folder.removeprefix("scrapers/"))
    if job.site_name:
        name_slug = job.site_name.lower().replace(" ", "-").replace(".", "-")
        for char in name_slug:
            if not char.isalnum() and char != "-":
                name_slug = name_slug.replace(char, "-")
        slug_candidates.append(name_slug)
    for slug in slug_candidates:
        scraper_path = os.path.join(
            settings.PROJECT_ROOT, "scrapers", slug, "scraper.py"
        )
        if os.path.exists(scraper_path):
            with open(scraper_path, "r") as f:
                content = f.read()
            response = HttpResponse(content, content_type="text/x-python")
            response["Content-Disposition"] = (
                f'attachment; filename="{slug}_scraper.py"'
            )
            return response
    return HttpResponseNotFound("Scraper code not found")


@login_required
def dagster_code(request, job_id):
    """Download the Dagster-format scraper ({slug}_dagster.py)."""
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    # Per-job attribution first (this job's own dagster file).
    own = _resolve_stored_path(job.dagster_file)
    if own:
        with open(own, "r") as f:
            content = f.read()
        slug = _resolve_job_slug(job) or "scraper"
        resp = HttpResponse(content, content_type="text/x-python")
        resp["Content-Disposition"] = f'attachment; filename="{slug}_dagster.py"'
        return resp
    slug = _resolve_job_slug(job)
    if not slug:
        return HttpResponseNotFound("Could not resolve site slug")
    dagster_path = os.path.join(
        settings.PROJECT_ROOT, "scrapers", slug, f"{slug}_dagster.py"
    )
    if not os.path.exists(dagster_path):
        # fallback: workspace
        dagster_path = os.path.join(
            settings.PROJECT_ROOT, "workspace", slug, f"{slug}_dagster.py"
        )
    if not os.path.exists(dagster_path):
        return HttpResponseNotFound("Dagster code not generated yet")
    with open(dagster_path, "r") as f:
        content = f.read()
    response = HttpResponse(content, content_type="text/x-python")
    response["Content-Disposition"] = f'attachment; filename="{slug}_dagster.py"'
    return response


def _resolve_job_slug(job):
    """Resolve the scraper folder slug for a job."""
    if job.site_folder:
        return job.site_folder.removeprefix("scrapers/")
    if job.site_name:
        name_slug = job.site_name.lower().replace(" ", "-").replace(".", "-")
        for char in name_slug:
            if not char.isalnum() and char != "-":
                name_slug = name_slug.replace(char, "-")
        return name_slug
    return ""


@login_required
def job_output_view(request, job_id, filename):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".json"):
        raise Http404("Only JSON files can be viewed")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    slug = _resolve_job_slug(job)
    if not slug:
        raise Http404("No scraper folder for this job")
    file_path = os.path.join(settings.PROJECT_ROOT, "scrapers", slug, safe_name)
    if not os.path.isfile(file_path):
        raise Http404("File not found")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, OSError):
        raise Http404("Could not read file")

    from src.content_types import get_output_key_label, count_items_in_output

    _output_key, _item_label = get_output_key_label(job.page_type)
    products = data.get(_output_key, [])
    if not products:
        products = data.get("products", [])  # back-compat: try old key
    _item_count = len(products) if products else count_items_in_output(data)
    download_url = reverse(
        "job_output_download", kwargs={"job_id": job.id, "filename": safe_name}
    )
    return render(
        request,
        "scraper/output_view.html",
        {
            "job": job,
            "filename": safe_name,
            "json_content": pretty,
            "product_count": _item_count,
            "item_count": _item_count,
            "item_label": _item_label,
            "download_url": download_url,
        },
    )


@login_required
def job_output_download(request, job_id, filename):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".json"):
        raise Http404("Only JSON files can be downloaded")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    slug = _resolve_job_slug(job)
    if not slug:
        raise Http404("No scraper folder for this job")
    file_path = os.path.join(settings.PROJECT_ROOT, "scrapers", slug, safe_name)
    if not os.path.isfile(file_path):
        raise Http404("File not found")
    return FileResponse(
        open(file_path, "rb"),
        content_type="application/json",
        as_attachment=True,
        filename=safe_name,
    )


@login_required
def job_restart(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if job.status in [
        ScrapeJob.STATUS_COMPLETED,
        ScrapeJob.STATUS_FAILED,
        ScrapeJob.STATUS_CANCELLED,
    ]:
        # Intake "Re-run" sends an optional prompt → store as notes for the
        # agents to see. Non-AJAX (job_detail button) sends nothing.
        rerun_prompt = request.POST.get("prompt", "").strip() if request.method == "POST" else ""
        new_job = ScrapeJob.objects.create(
            url=job.url,
            product_url=job.product_url,
            currency=job.currency,
            page_type=job.page_type,
            input_mode=job.input_mode,
            search_criteria=job.search_criteria,
            target_fields=job.target_fields,
            scope=job.scope,
            scope_value=job.scope_value,
            notes=(rerun_prompt or job.notes),
            full_extraction=job.full_extraction,
            user=request.user,
        )

        from .tasks import run_scrape_task

        task = run_scrape_task.delay(new_job.id, rescrape=True)
        new_job.celery_task_id = task.id
        new_job.save(update_fields=["celery_task_id"])
        if is_ajax:
            return JsonResponse(
                {
                    "job_id": new_job.id,
                    "events_url": reverse("job_events", args=[new_job.id]),
                    "api_url": reverse("job_api", args=[new_job.id]),
                    "tool_calls_url": reverse("tool_calls_api", args=[new_job.id]),
                    "scraper_code_url": reverse("scraper_code", args=[new_job.id]),
                    "dagster_code_url": reverse("dagster_code", args=[new_job.id]),
                    "cancel_url": reverse("job_cancel", args=[new_job.id]),
                }
            )
        return redirect("job_detail", job_id=new_job.id)
    if is_ajax:
        return JsonResponse(
            {"error": "job is not in a restartable state"}, status=409
        )
    return redirect("job_detail", job_id=job.id)


@login_required
def job_update(request, job_id):
    """AJAX: in-place update of a ScrapeJob's mutable fields.

    The first generic job-mutation endpoint — hosts rename (title), config-save
    (target_fields/scope/notes/nav), and bookmark (is_saved). Strict allowlist;
    unknown fields are ignored.
    """
    if request.method != "POST" or request.headers.get("x-requested-with") != "XMLHttpRequest":
        return JsonResponse({"error": "POST + AJAX required"}, status=400)
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    update_fields = []

    if "title" in request.POST:
        job.title = (request.POST.get("title") or "").strip()[:200]
        update_fields.append("title")

    if "target_fields" in request.POST:
        tf = (request.POST.get("target_fields") or "").strip()
        job.target_fields = [f.strip() for f in tf.split(",") if f.strip()] if tf else []
        update_fields.append("target_fields")

    for fld in ("scope", "scope_value", "notes", "search_criteria", "search_url"):
        if fld in request.POST:
            setattr(job, fld, (request.POST.get(fld) or "").strip())
            update_fields.append(fld)

    if "is_saved" in request.POST:
        v = (request.POST.get("is_saved") or "").strip().lower()
        job.is_saved = v in ("1", "true", "yes", "on")
        update_fields.append("is_saved")

    if update_fields:
        job.save(update_fields=update_fields)
        logger.info("job_update: job %d updated %s", job.id, update_fields)

    return JsonResponse(
        {
            "id": job.id,
            "title": job.title,
            "is_saved": job.is_saved,
            "target_fields": job.target_fields,
            "scope": job.scope,
            "scope_value": job.scope_value,
            "notes": job.notes,
            "search_criteria": job.search_criteria,
            "search_url": job.search_url,
        }
    )


def _build_resume_value(approval: Approval, choice: str, feedback: str) -> dict:
    response_data = approval.response_data or {}
    decisions = response_data.get("decisions", [])

    decision_type = "approve"
    for d in decisions:
        if d.get("label") == choice or d.get("type") == choice:
            decision_type = d.get("type", "approve")
            break

    if choice in ("Cancel", "Abort", "No", "stop", "Stop"):
        decision_type = "reject"

    human_response = {"decision": decision_type, "label": choice, "feedback": feedback}
    return human_response


@login_required
def approval_inline(request, job_id, approval_id):
    approval = get_object_or_404(Approval, pk=approval_id, job_id=job_id, job__user=request.user)
    choice = request.POST.get("choice", "")
    feedback = request.POST.get("feedback", "").strip()

    if not choice:
        return redirect("job_detail", job_id=job_id)

    human_response = _build_resume_value(approval, choice, feedback)

    approval.status = Approval.STATUS_APPROVED
    approval.human_response = choice
    approval.resolved_at = timezone.now()
    approval.save(update_fields=["status", "resolved_at", "human_response"])

    try:
        from .tasks import resume_scrape_task

        resume_scrape_task.delay(approval.job.id, human_response)
    except Exception as e:
        logger.error("Failed to resume graph for job %d: %s", approval.job.id, e)

    return redirect("job_detail", job_id=job_id)


@login_required
def pending_approvals_fragment(request, job_id):
    approvals = Approval.objects.filter(
        job_id=job_id, status=Approval.STATUS_PENDING
    ).order_by("-created_at")
    if not approvals:
        return JsonResponse({"html": ""})
    from django.template.loader import render_to_string

    html = render_to_string(
        "scraper/_approval_cards.html",
        {
            "job_id": job_id,
            "pending_approvals": approvals,
        },
        request=request,
    )
    return JsonResponse({"html": html})


@login_required
def approval_list(request):
    approvals = (
        Approval.objects.filter(status=Approval.STATUS_PENDING, job__user=request.user)
        .select_related("job")
        .order_by("-created_at")
    )
    return render(request, "scraper/approval_list.html", {"approvals": approvals})


@login_required
def approval_count(request):
    count = Approval.objects.filter(status=Approval.STATUS_PENDING, job__user=request.user).count()
    return JsonResponse({"count": count})


@login_required
def approval_detail(request, approval_id):
    approval = get_object_or_404(Approval, pk=approval_id, job__user=request.user)
    if request.method == "POST":
        choice = request.POST.get("choice", "")
        feedback = request.POST.get("feedback", "").strip()

        if not choice:
            return redirect("approval_list")

        human_response = _build_resume_value(approval, choice, feedback)

        approval.status = Approval.STATUS_APPROVED
        approval.human_response = choice
        approval.resolved_at = timezone.now()
        approval.save(update_fields=["status", "resolved_at", "human_response"])

        if approval.job.graph_thread_id:
            try:
                from .tasks import resume_scrape_task

                resume_scrape_task.delay(approval.job.id, human_response)
            except Exception as exc:
                logger.error(
                    "Failed to resume graph for job %d: %s", approval.job.id, exc
                )

        return redirect("approval_list")

    return render(request, "scraper/approval_detail.html", {"approval": approval})


@login_required
def scraper_code_json(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    scraper_path = None
    # Per-job attribution first.
    own = _resolve_stored_path(job.scraper_file)
    if own:
        scraper_path = own
    elif job.site_folder:
        candidate = os.path.join("scrapers", job.site_folder.removeprefix("scrapers/"), "scraper.py")
        if os.path.isfile(candidate):
            scraper_path = candidate

    if not scraper_path:
        return JsonResponse({"error": "Scraper file not found"}, status=404)

    try:
        with open(scraper_path, "r", encoding="utf-8") as f:
            code = f.read()
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"path": scraper_path, "code": code})


def _resolve_job_output(job):
    """Return (site_dir, filename) for THIS job's output file, or (None, None).

    All runs for a site share one scrapers/{slug}/ dir, so picking the newest
    file there would show another job's output. We prefer the job's own
    `output_file` (authoritative); else the newest output_*.json whose timestamp
    falls within this job's run window (matching job_detail's logic).
    """
    # 1. authoritative output_file
    of = (job.output_file or "").strip()
    if of:
        cand = of if os.path.isabs(of) else os.path.join(settings.PROJECT_ROOT, of)
        if os.path.isfile(cand):
            return os.path.dirname(cand), os.path.basename(cand)
    # 2. slug dir + started_at window
    slug = _resolve_job_slug(job)
    if not slug:
        try:
            from .tasks import _generate_slug

            slug = _generate_slug(job.url)
        except Exception:
            slug = ""
    if not slug:
        return None, None
    site_dir = None
    for base in ("scrapers", "workspace"):
        d = os.path.join(settings.PROJECT_ROOT, base, slug)
        if os.path.isdir(d):
            site_dir = d
            break
    if not site_dir:
        return None, None
    outs = sorted(
        [f for f in os.listdir(site_dir) if f.startswith("output_") and f.endswith(".json")],
        reverse=True,
    )
    if not outs:
        return None, None
    if job.started_at:
        cutoff = job.started_at - timedelta(seconds=120)
        in_window = []
        for f in outs:
            try:
                ts = datetime.strptime(f, "output_%Y-%m-%d_%H%M%S.json").replace(
                    tzinfo=dt_timezone.utc
                )
            except ValueError:
                continue
            if ts >= cutoff:
                in_window.append(f)
        if in_window:
            return site_dir, in_window[0]
        # started_at is known but NO file falls in this job's window → this job
        # hasn't produced output yet. Return None rather than another job's
        # newest file (which would show stale/wrong data mid-run).
        return None, None
    # No started_at (legacy/unknown start) → can't window; fall back to newest.
    return site_dir, outs[0]


def _job_output_preview(job) -> dict | None:
    """This job's extracted output, for the intake Sample Output card.

    Returns {filename, output_key, item_label, count, records (first 3),
    download_url} or None when no output exists yet. Uses _resolve_job_output so
    the preview is always THIS job's output, not another run's. Caps records so
    the job_api payload stays small (full output is via download_url).
    """
    site_dir, fname = _resolve_job_output(job)
    if not site_dir or not fname:
        return None
    try:
        with open(os.path.join(site_dir, fname), encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    from src.content_types import count_items_in_output, get_output_key_label

    out_key, label = get_output_key_label(job.page_type)
    items = data.get(out_key) or data.get("products") or []
    count = len(items) if isinstance(items, list) and items else count_items_in_output(data)
    records = items[:3] if isinstance(items, list) else []
    return {
        "filename": fname,
        "output_key": out_key,
        "item_label": label,
        "count": count,
        "records": records,
        "download_url": f"/jobs/{job.id}/output/{fname}/download/",
    }


def _job_run_history(job, limit: int = 15) -> list:
    """Recent jobs for the same site (URL) — the Run History table.

    Includes the current job, so the current run always shows (and updates to
    COMPLETED + record count when it finishes). Each row carries per-run
    scraper/output download URLs.
    """
    runs: list = []
    qs = ScrapeJob.objects.filter(url=job.url, user=job.user).order_by("-created_at")[:limit]
    for j in qs:
        # per-run output: this run's own output_file / window, not the site's latest
        _sd, out_fname = _resolve_job_output(j)
        out_url = f"/jobs/{j.id}/output/{out_fname}/download/" if out_fname else None
        dur = None
        if j.started_at and j.completed_at:
            dur = (j.completed_at - j.started_at).total_seconds()
        runs.append(
            {
                "id": j.id,
                "status": j.status,
                "when": j.created_at.isoformat() if j.created_at else None,
                "records": j.product_count,
                "duration_seconds": dur,
                "scraper_url": f"/jobs/{j.id}/scraper-code/",
                "output_url": out_url,
                "current": j.id == job.id,
            }
        )
    return runs


@login_required
def job_api(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    since_seq = int(request.GET.get("since_seq", 0))
    return JsonResponse(
        {
            "id": job.id,
            "url": job.url,
            "product_url": job.product_url,
            "currency": job.currency,
            "status": job.status,
            "site_name": job.site_name,
            "platform": job.platform,
            # Intake-revision: display title + saved bookmark.
            "title": job.title,
            "is_saved": job.is_saved,
            # Intake config (drives the Current Configuration panel on deep-link)
            "target_fields": list(job.target_fields or []),
            "input_mode": job.input_mode,
            "search_criteria": job.search_criteria,
            "search_url": job.search_url,
            "scope": job.scope,
            "scope_value": job.scope_value,
            "notes": job.notes,
            "product_count": job.product_count,
            "output_file": job.output_file,
            "site_folder": job.site_folder,
            "full_extraction": job.full_extraction,
            "error_message": job.error_message,
            "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "steps": [
                {
                    "phase": s.phase,
                    "status": s.status,
                    "notes": s.notes,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "completed_at": s.completed_at.isoformat()
                    if s.completed_at
                    else None,
                }
                for s in _ordered_steps(job)
            ],
            "approvals": [
                {
                    "id": a.id,
                    "type": a.approval_type,
                    "question": a.question,
                    "status": a.status,
                    "created_at": a.created_at.isoformat(),
                }
                for a in job.approvals.all()
            ],
            "logs": [
                {
                    "seq": l.seq,
                    "role": l.role,
                    "agent": l.agent,
                    "content": l.content,
                    "created_at": l.created_at.isoformat(),
                }
                for l in job.session_logs.filter(seq__gt=since_seq).order_by("seq")
            ],
            "total_log_count": job.session_logs.count(),
            "output_preview": _job_output_preview(job),
            "run_history": _job_run_history(job),
        }
    )


@login_required
def job_logs_api(request, job_id):
    get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    since_seq = int(request.GET.get("since_seq", 0))
    logs = [
        {
            "seq": l.seq,
            "role": l.role,
            "agent": l.agent,
            "content": l.content,
            "created_at": l.created_at.isoformat(),
        }
        for l in job.session_logs.filter(seq__gt=since_seq).order_by("seq")[:100]
    ]
    return JsonResponse({"logs": logs, "total_log_count": job.session_logs.count()})


@login_required
def job_events(request, job_id):
    get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    terminal_states = {
        ScrapeJob.STATUS_COMPLETED,
        ScrapeJob.STATUS_FAILED,
        ScrapeJob.STATUS_CANCELLED,
    }

    def event_stream():
        if redis_client is not None:
            try:
                pubsub = redis_client.pubsub()
                pubsub.subscribe(f"job:{job_id}")
                pubsub.subscribe(f"job:{job_id}:status")
                pubsub.subscribe(f"job:{job_id}:syslog")

                job = ScrapeJob.objects.get(pk=job_id)
                data = json.dumps({"type": "status", "status": job.status})
                yield f"event: status\ndata: {data}\n\n"

                if job.status in terminal_states:
                    data = json.dumps({"type": "done", "status": job.status})
                    yield f"event: done\ndata: {data}\n\n"
                    pubsub.unsubscribe()
                    pubsub.close()
                    return

                for message in pubsub.listen():
                    if message["type"] == "message":
                        yield f"data: {message['data']}\n\n"
                        try:
                            msg_data = (
                                json.loads(message["data"])
                                if isinstance(message["data"], str)
                                else message["data"]
                            )
                            if msg_data.get("type") == "done":
                                pubsub.unsubscribe()
                                pubsub.close()
                                return
                        except (json.JSONDecodeError, TypeError):
                            pass
            except Exception as e:
                logger.warning(
                    f"Redis pub/sub failed for job {job_id}, falling back to DB polling: {e}"
                )

        job = ScrapeJob.objects.get(pk=job_id)
        last_seq = 0
        last_status = job.status
        poll_count = 0

        while poll_count < 1200:
            job.refresh_from_db(fields=["status"])
            if job.status != last_status:
                data = json.dumps({"type": "status", "status": job.status})
                yield f"event: status\ndata: {data}\n\n"
                last_status = job.status

            if job.status in terminal_states:
                data = json.dumps({"type": "done", "status": job.status})
                yield f"event: done\ndata: {data}\n\n"
                break

            new_logs = job.session_logs.filter(seq__gt=last_seq).order_by("seq")
            logs_batch = []
            for log in new_logs[:20]:
                logs_batch.append(
                    {
                        "seq": log.seq,
                        "role": log.role,
                        "agent": log.agent,
                        "content": log.content,
                        "created_at": log.created_at.isoformat(),
                    }
                )
            if logs_batch:
                last_seq = logs_batch[-1]["seq"]
                data = json.dumps({"type": "logs", "logs": logs_batch})
                yield f"event: logs\ndata: {data}\n\n"

            poll_count += 1
            time.sleep(2)

    response = StreamingHttpResponse(
        event_stream(),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
def job_resume(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)

    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse(
                {"status": "error", "message": "Invalid JSON in request body"},
                status=400,
            )
        response = data.get("response")
        if not isinstance(response, dict) or "decision" not in response:
            return JsonResponse(
                {"status": "error", "message": "Response must be a dict with a 'decision' key"},
                status=400,
            )
        try:
            from .tasks import resume_scrape_task

            resume_scrape_task.delay(job.id, response)
        except ImportError:
            logger.warning("resume_scrape_task not yet implemented in tasks.py")
            return JsonResponse(
                {
                    "status": "error",
                    "message": "resume_scrape_task not yet implemented",
                },
                status=501,
            )
        except Exception as e:
            logger.error(f"Failed to resume graph for job {job.id}: {e}")
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
        return JsonResponse({"status": "resumed"})

    return JsonResponse({"thread_id": job.graph_thread_id, "status": job.status})


@login_required
def tool_calls_api(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    agent_filter = request.GET.get("agent", "")
    qs = job.tool_call_logs.order_by("call_seq")
    if agent_filter:
        qs = qs.filter(agent=agent_filter)
    data = []
    for tc in qs:
        data.append(
            {
                "id": tc.id,
                "agent": tc.agent,
                "tool_name": tc.tool_name,
                "call_seq": tc.call_seq,
                "args_summary": tc.args_summary,
                "result_summary": tc.result_summary,
            }
        )
    return JsonResponse({"tool_calls": data})


@login_required
def agent_summary(request, job_id: int):
    job = get_object_or_404(ScrapeJob, pk=job_id, user=request.user)
    agent_filter = request.GET.get("agent", "")
    logs = SessionLog.objects.filter(job=job)
    if agent_filter:
        logs = logs.filter(agent=agent_filter)
    logs = logs.order_by("seq")

    agents = {}
    for log in logs:
        agent = log.agent or "system"
        if agent not in agents:
            agents[agent] = {"name": agent, "logs": [], "assistant_msgs": []}
        role_label = {
            "assistant": "Assistant",
            "tool": "Tool",
            "user": "User",
            "system": "System",
        }.get(log.role, log.role)
        content = str(log.content)
        if log.role == "assistant":
            agents[agent]["assistant_msgs"].append(content)
        agents[agent]["logs"].append({"role": role_label, "content": content[:20000]})

    summaries = []
    # P0-18: iterate ALL agents (no hardcoded allowlist — the old list missed
    # navigation-agent, code-reviewer, dagster-converter, and the fallback
    # branch DISCARDED available assistant messages to write "(No summary
    # available)"). With canonical naming (Layer 1) and ROLE_SYSTEM for tool
    # traces (Layer 2), every agent now has clean assistant messages.
    for agent_name, agent_data in agents.items():
        summary_md = f"# {agent_name.replace('-', ' ').title()}\n\n"
        for msg in agent_data["assistant_msgs"]:
            summary_md += f"{msg}\n\n"
        _tool_calls = sum(1 for lg in agent_data["logs"] if lg["role"] == "Tool")
        if not agent_data["assistant_msgs"] and _tool_calls:
            summary_md += f"(tool-only run — {_tool_calls} tool calls)\n"
        summary_md += f"---\n**Total tool calls:** {_tool_calls}\n"
        summaries.append({"agent": agent_name, "summary": summary_md})

    return render(
        request,
        "scraper/agent_summary.html",
        {"job": job, "summaries": summaries, "agent_filter": agent_filter},
    )


@login_required
def probe_cache(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "delete":
            entry_id = request.POST.get("entry_id")
            ProbeCache.objects.filter(pk=entry_id).delete()
        elif action == "clear_expired":
            from datetime import timedelta

            cutoff = timezone.now() - timedelta(hours=4)
            ProbeCache.objects.filter(cached_at__lt=cutoff).delete()
        elif action == "clear_all":
            ProbeCache.objects.all().delete()
        return redirect("probe_cache")

    entries = ProbeCache.objects.all().order_by("-cached_at")

    return render(request, "scraper/probe_cache.html", {"entries": entries})


BROWSER_SERVICE_URL = os.environ.get(
    "BROWSER_SERVICE_URL", "http://browser_service:8001"
)


@login_required
def probe_tester(request):
    if (
        request.method != "POST"
        or request.headers.get("x-requested-with") != "XMLHttpRequest"
    ):
        return render(request, "scraper/probe_tester.html", {"initial_url": ""})

    url = request.POST.get("url", "").strip()
    method = request.POST.get("method", "")

    if not url or not method:
        return JsonResponse({"error": "url and method required"}, status=400)

    try:
        resp = httpx.post(
            f"{BROWSER_SERVICE_URL}/probe-single",
            json={"url": url, "method": method, "timeout": 60},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        return JsonResponse(data)
    except httpx.ReadTimeout:
        return JsonResponse({"success": False, "error": "Probe timed out (120s)"})
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)[:500]}, status=500)


@login_required
def probe_tester_clear_cache(request):
    if (
        request.method != "POST"
        or request.headers.get("x-requested-with") != "XMLHttpRequest"
    ):
        return JsonResponse({"error": "POST required"}, status=400)

    domain = request.POST.get("domain", "").strip()
    if not domain:
        return JsonResponse({"error": "domain required"}, status=400)

    from scraper.models import ProbeCache

    deleted = ProbeCache.objects.filter(domain=domain).delete()
    logger.info("Probe tester: cleared cache for %s (%d entries)", domain, deleted)
    return JsonResponse({"domain": domain, "deleted": deleted})


@login_required
def probe_tester_update_cache(request):
    if (
        request.method != "POST"
        or request.headers.get("x-requested-with") != "XMLHttpRequest"
    ):
        return JsonResponse({"error": "POST required"}, status=400)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, Exception):
        return JsonResponse({"error": "invalid JSON"}, status=400)

    url = data.get("url", "")
    method = data.get("method")
    success = data.get("success", False)
    needs_akamai_bypass = data.get("needs_akamai_bypass", False)

    if not url or not method:
        return JsonResponse({"error": "url and method required"}, status=400)

    domain = urlparse(url).hostname or urlparse(url).netloc
    if not domain:
        return JsonResponse({"error": "invalid url"}, status=400)

    from scraper.models import ProbeCache

    if not success:
        return JsonResponse({"error": "can only cache successful probes"})

    entry, _ = ProbeCache.objects.update_or_create(
        domain=domain,
        defaults={
            "method": method,
            "needs_akamai_bypass": needs_akamai_bypass,
        },
    )
    logger.info(
        "Probe tester: updated cache %s → method=%s (id=%d)", domain, method, entry.id
    )
    return JsonResponse({"domain": domain, "method": method, "cache_id": entry.id})


@login_required
def probe_tester_cached_method(request):
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        return JsonResponse({"error": "AJAX required"}, status=400)

    domain = request.GET.get("domain", "").strip()
    if not domain:
        return JsonResponse({"method": None})

    from scraper.models import ProbeCache

    entry = ProbeCache.objects.filter(domain=domain).order_by("-cached_at").first()
    if entry:
        return JsonResponse(
            {
                "method": entry.method,
                "needs_akamai_bypass": entry.needs_akamai_bypass,
            }
        )
    return JsonResponse({"method": None})


# ═══════════════════════════════════════════════════════════════════════════
# Site Management Views
# ═══════════════════════════════════════════════════════════════════════════


@login_required
def site_list(request):
    sites = Site.objects.all()
    return render(request, "scraper/site_list.html", {"sites": sites})


@login_required
def site_add(request):
    if request.method == "POST":
        form = SiteForm(request.POST, request.FILES)
        if form.is_valid():
            site = form.save()
            return redirect("site_detail", site_id=site.id)
    else:
        form = SiteForm()
    return render(request, "scraper/site_form.html", {"form": form, "site": Site()})


@login_required
def site_edit(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if request.method == "POST":
        form = SiteForm(request.POST, request.FILES, instance=site)
        if form.is_valid():
            form.save()
            return redirect("site_detail", site_id=site.id)
    else:
        form = SiteForm(instance=site)
    return render(request, "scraper/site_form.html", {"form": form, "site": site})


@login_required
def site_detail(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    jobs = ScrapeJob.objects.filter(url__iexact=site.url, user=request.user)
    scraper_code = ""
    if site.default_scraper_path and os.path.isfile(site.default_scraper_path):
        try:
            with open(site.default_scraper_path, "r", encoding="utf-8") as f:
                scraper_code = f.read()
        except Exception:
            pass

    output_files = []
    scraper_archives = []
    if site.slug:
        scrapers_dir = Path(settings.PROJECT_ROOT) / "scrapers" / site.slug
        if scrapers_dir.is_dir():
            for f in sorted(scrapers_dir.iterdir(), reverse=True):
                if not f.is_file():
                    continue
                try:
                    size = f.stat().st_size
                except Exception:
                    continue
                if f.name.startswith("output_") and f.name.endswith(".json"):
                    output_files.append({"name": f.name, "size": size})
                elif f.name.startswith("scraper-") and f.name.endswith(".py"):
                    scraper_archives.append({"name": f.name, "size": size})

    return render(
        request,
        "scraper/site_detail.html",
        {
            "site": site,
            "jobs": jobs,
            "scraper_code": scraper_code,
            "output_files": output_files,
            "scraper_archives": scraper_archives,
        },
    )


@login_required
def site_scrape(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if request.method != "POST":
        return redirect("site_detail", site_id=site.id)

    rescrape = request.POST.get("rescrape") == "on"
    full_extraction = request.POST.get("full_extraction") == "on"

    existing = ScrapeJob.objects.filter(
        url=site.url, status__in=[ScrapeJob.STATUS_PENDING, ScrapeJob.STATUS_RUNNING],
        user=request.user,
    ).first()
    if existing:
        return redirect("job_detail", job_id=existing.id)

    job = ScrapeJob.objects.create(
        url=site.url,
        product_url=site.sample_url,
        currency=site.currency,
        full_extraction=full_extraction,
        user=request.user,
    )

    if site.input_urls:
        scrapers_dir = os.path.join(settings.PROJECT_ROOT, "scrapers", site.slug)
        os.makedirs(scrapers_dir, exist_ok=True)
        input_urls_path = os.path.join(scrapers_dir, "input_urls.json")
        with open(input_urls_path, "w", encoding="utf-8") as f:
            json.dump({"urls": site.input_urls}, f, indent=2, ensure_ascii=False)

    from .tasks import run_scrape_task

    task = run_scrape_task.delay(job.id, rescrape=rescrape)
    job.celery_task_id = task.id
    job.save(update_fields=["celery_task_id"])
    return redirect("job_detail", job_id=job.id)


@login_required
def site_rerun(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if request.method != "POST":
        return redirect("site_detail", site_id=site.id)

    if not site.has_scraper or not site.default_scraper_path:
        return redirect("site_detail", site_id=site.id)

    scraper_path = site.default_scraper_path
    scrapers_dir = os.path.dirname(scraper_path)
    os.makedirs(scrapers_dir, exist_ok=True)

    if site.input_urls:
        input_urls_path = os.path.join(scrapers_dir, "input_urls.json")
        with open(input_urls_path, "w", encoding="utf-8") as f:
            json.dump({"urls": site.input_urls}, f, indent=2, ensure_ascii=False)

    BROWSER_METHODS = {
        "undetected_chromedriver",
        "seleniumbase_uc",
        "playwright",
        "undetected_chromedriver_scraper",
        "stealth_browser",
        "uc_chrome",
    }

    if site.scraping_method in BROWSER_METHODS:
        import httpx

        service_url = getattr(
            settings, "BROWSER_SERVICE_URL", "http://browser_service:8001"
        )
        # Anti-bot/Akamai sites: route the scraper through CloakBrowser stealth.
        scrape_json = {
            "scraper_path": scraper_path,
            "args": [],
            "timeout": 3600,
        }
        if getattr(site, "needs_akamai_bypass", False):
            scrape_json["env_overrides"] = {"STEALTH_BROWSER": "cloak"}
        try:
            resp = httpx.post(
                f"{service_url}/scrape",
                json=scrape_json,
                timeout=3660,
            )
            resp.raise_for_status()
            result = resp.json()
            output_file = result.get("output_file", "")
            product_count = result.get("product_count", 0)
        except Exception as e:
            logger.error("site_rerun: browser_service failed: %s", e)
            output_file = ""
            product_count = 0
    else:
        try:
            result = subprocess.run(
                ["python3", scraper_path],
                capture_output=True,
                text=True,
                timeout=3600,
                cwd=os.path.dirname(scraper_path),
            )
            output_file = ""
            if result.returncode == 0:
                candidates = sorted(
                    [
                        os.path.join(scrapers_dir, f)
                        for f in os.listdir(scrapers_dir)
                        if f.startswith("output_") and f.endswith(".json")
                    ]
                )
                output_file = candidates[-1] if candidates else ""
                if output_file:
                    with open(output_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    from src.content_types import count_items_in_output
                    product_count = count_items_in_output(data)
                else:
                    product_count = 0
            else:
                product_count = 0
        except Exception as e:
            logger.error("site_rerun: local execution failed: %s", e)
            output_file = ""
            product_count = 0

    site.last_scraped_at = timezone.now()
    site.product_count = product_count
    site.save(update_fields=["last_scraped_at", "product_count"])

    return redirect("site_detail", site_id=site.id)


@login_required
def site_scraper_code(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if not site.default_scraper_path or not os.path.isfile(site.default_scraper_path):
        raise Http404("Scraper code not found")
    return FileResponse(
        open(site.default_scraper_path, "rb"),
        content_type="text/x-python",
        as_attachment=True,
        filename=f"{site.slug}_scraper.py",
    )


@login_required
def site_scraper_archive_view(request, site_id, filename):
    site = get_object_or_404(Site, pk=site_id)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".py"):
        raise Http404("Only Python files can be viewed")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    file_path = os.path.join(settings.PROJECT_ROOT, "scrapers", site.slug, safe_name)
    if not os.path.isfile(file_path):
        raise Http404("File not found")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
    except OSError:
        raise Http404("Could not read file")
    download_url = reverse(
        "site_scraper_archive_download",
        kwargs={"site_id": site.id, "filename": safe_name},
    )
    return render(
        request,
        "scraper/scraper_archive_view.html",
        {
            "site": site,
            "filename": safe_name,
            "code": code,
            "download_url": download_url,
        },
    )


@login_required
def site_scraper_archive_download(request, site_id, filename):
    site = get_object_or_404(Site, pk=site_id)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".py"):
        raise Http404("Only Python files can be downloaded")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    file_path = os.path.join(settings.PROJECT_ROOT, "scrapers", site.slug, safe_name)
    if not os.path.isfile(file_path):
        raise Http404("File not found")
    return FileResponse(
        open(file_path, "rb"),
        content_type="text/x-python",
        as_attachment=True,
        filename=safe_name,
    )


@login_required
def site_delete(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if request.method == "POST":
        site.delete()
        return redirect("site_list")
    return redirect("site_detail", site_id=site.id)


@login_required
def site_output_view(request, site_id, filename):
    site = get_object_or_404(Site, pk=site_id)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".json"):
        raise Http404("Only JSON files can be viewed")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    file_path = os.path.join(settings.PROJECT_ROOT, "scrapers", site.slug, safe_name)
    if not os.path.isfile(file_path):
        raise Http404("File not found")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, OSError):
        raise Http404("Could not read file")

    from src.content_types import count_items_in_output

    _item_count = count_items_in_output(data)
    download_url = reverse(
        "site_output_download", kwargs={"site_id": site.id, "filename": safe_name}
    )
    return render(
        request,
        "scraper/output_view.html",
        {
            "site": site,
            "filename": safe_name,
            "json_content": pretty,
            "product_count": _item_count,
            "item_count": _item_count,
            "item_label": "item",
            "download_url": download_url,
        },
    )


@login_required
def site_output_download(request, site_id, filename):
    site = get_object_or_404(Site, pk=site_id)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".json"):
        raise Http404("Only JSON files can be downloaded")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    file_path = os.path.join(settings.PROJECT_ROOT, "scrapers", site.slug, safe_name)
    if not os.path.isfile(file_path):
        raise Http404("File not found")
    return FileResponse(
        open(file_path, "rb"),
        content_type="application/json",
        as_attachment=True,
        filename=safe_name,
    )


@login_required
def site_sync_urls(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if request.method != "POST":
        return redirect("site_detail", site_id=site.id)
    urls = site.input_urls or []
    if not urls:
        return redirect("site_detail", site_id=site.id)
    scrapers_dir = os.path.join(settings.PROJECT_ROOT, "scrapers", site.slug)
    os.makedirs(scrapers_dir, exist_ok=True)
    input_path = os.path.join(scrapers_dir, "input_urls.json")
    with open(input_path, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info("Synced %d URLs to %s", len(urls), input_path)
    return redirect("site_detail", site_id=site.id)


@login_required
def schedule_next(request):
    if request.method != "POST":
        return redirect("home")

    from .tasks import _do_schedule_next_site

    result = _do_schedule_next_site()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(result)

    if result.get("action") == "queued":
        return redirect("job_detail", job_id=result["job_id"])

    return redirect("home")


# ═══════════════════════════════════════════════════════════════════════════
# Agent Playground
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT_PROMPTS: dict[str, str] = {
    "site_analyzer": "Analyze the site structure of {url}. Detect the platform, scraping mechanism, anti-bot protection, and product discovery method. Write your findings to workspace/{slug}/site_analysis.json.",
    "browser_traverse": "The browser-driven agent navigates the site to find the listing page.",
    "nav_skill_review": "Read workspace/{slug}/navigation_findings.json, compare against existing skills, and apply any new reusable navigation patterns. Write your report to workspace/{slug}/nav_learning_report.json.",
    "product_analyzer": "Analyze the product page structure at {url}. Map all extractable fields with exact CSS selectors, JSON-LD paths, and meta tag fallbacks. Write to workspace/{slug}/product_analysis.json.",
    "scraper_analyzer": "Verify the scraping strategy for {url}. Read existing analysis files and confirm the extraction approach. Write to workspace/{slug}/scraper_analysis.json.",
}


def _default_prompt(agent_name: str, url: str = "", slug: str = "") -> str:
    template = _DEFAULT_PROMPTS.get(agent_name, "Run the {agent_name} agent on {url}.")
    return template.format(
        url=url or "https://example.com",
        slug=slug or "test-site",
        agent_name=agent_name,
    )


@login_required
def agent_playground(request):
    """Agent Playground — test individual agents in isolation."""
    from .models import AgentPlayground

    if (
        request.method == "POST"
        and request.headers.get("x-requested-with") == "XMLHttpRequest"
    ):
        agent_name = request.POST.get("agent_name", "").strip()
        prompt = request.POST.get("prompt", "").strip()
        url = request.POST.get("url", "").strip()
        search_criteria = request.POST.get("search_criteria", "").strip()

        if not agent_name or not prompt:
            return JsonResponse({"error": "agent_name and prompt required"}, status=400)

        from .tasks import _generate_slug

        slug = _generate_slug(url) if url else "playground"
        pg = AgentPlayground.objects.create(
            agent_name=agent_name,
            prompt=prompt,
            url=url,
            search_criteria=search_criteria,
            site_slug=slug,
        )

        from .tasks import run_agent_task

        task = run_agent_task.delay(pg.id)
        pg.celery_task_id = task.id
        pg.save(update_fields=["celery_task_id"])

        return JsonResponse({"playground_id": pg.id, "status": pg.status})

    recent_runs = AgentPlayground.objects.all()[:10]
    return render(
        request,
        "scraper/agent_playground.html",
        {
            "recent_runs": recent_runs,
            "default_prompts": json.dumps(_DEFAULT_PROMPTS),
        },
    )


@login_required
def agent_playground_detail(request, playground_id):
    """View results of a specific playground run."""
    from .models import AgentPlayground

    pg = get_object_or_404(AgentPlayground, pk=playground_id)

    # Read artifact contents
    artifacts: list[dict] = []
    for path in pg.output_artifacts or []:
        full_path = os.path.join(settings.PROJECT_ROOT, path)
        if os.path.isfile(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
                artifacts.append(
                    {
                        "path": path,
                        "name": os.path.basename(path),
                        "content": content[:50000],
                        "size": len(content),
                    }
                )
            except Exception:
                pass

    return JsonResponse(
        {
            "id": pg.id,
            "agent_name": pg.agent_name,
            "status": pg.status,
            "url": pg.url,
            "site_slug": pg.site_slug,
            "tool_call_count": pg.tool_call_count,
            "output_summary": pg.output_summary,
            "error_message": pg.error_message,
            "artifacts": artifacts,
            "created_at": pg.created_at.isoformat() if pg.created_at else None,
            "completed_at": pg.completed_at.isoformat() if pg.completed_at else None,
        }
    )


@login_required
def agent_playground_list(request):
    """Return recent playground runs as JSON for polling."""
    from .models import AgentPlayground

    runs = AgentPlayground.objects.all()[:20]
    return JsonResponse(
        {
            "runs": [
                {
                    "id": r.id,
                    "agent_name": r.agent_name,
                    "status": r.status,
                    "url": r.url,
                    "tool_call_count": r.tool_call_count,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                    "completed_at": r.completed_at.isoformat()
                    if r.completed_at
                    else "",
                }
                for r in runs
            ]
        }
    )


def _check_db():
    t0 = time.monotonic()
    try:
        from django.db import connections

        conn = connections["default"]
        conn.ensure_connection()
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "up", "latency_ms": ms, "detail": "Connected"}
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "down", "latency_ms": ms, "detail": str(exc)[:200]}


def _check_redis():
    t0 = time.monotonic()
    try:
        import redis

        url = getattr(settings, "REDIS_URL", "redis://redis:6379/0")
        r = redis.from_url(url)
        r.ping()
        ms = int((time.monotonic() - t0) * 1000)
        db_size = r.dbsize()
        return {"status": "up", "latency_ms": ms, "detail": f"{db_size} keys"}
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "down", "latency_ms": ms, "detail": str(exc)[:200]}


def _check_celery_worker():
    t0 = time.monotonic()
    try:
        from celery import current_app

        inspect = current_app.control.inspect()
        active = inspect.active()
        if not active:
            return {"status": "down", "latency_ms": 0, "detail": "No active workers"}
        worker_count = len(active)
        total_active = sum(len(tasks) for tasks in active.values())
        ms = int((time.monotonic() - t0) * 1000)
        return {
            "status": "up" if worker_count > 0 else "down",
            "latency_ms": ms,
            "detail": f"{worker_count} worker(s), {total_active} active task(s)",
        }
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "down", "latency_ms": ms, "detail": str(exc)[:200]}


def _check_celery_beat():
    t0 = time.monotonic()
    try:
        from django_celery_beat.models import PeriodicTask
        from django.utils import timezone as dj_tz

        last_run = (
            PeriodicTask.objects.filter(enabled=True)
            .order_by("-last_run_at")
            .first()
        )
        ms = int((time.monotonic() - t0) * 1000)
        if last_run and last_run.last_run_at:
            age = (dj_tz.now() - last_run.last_run_at).total_seconds()
            if age < 300:
                return {"status": "up", "latency_ms": ms, "detail": f"Last run {int(age)}s ago"}
            return {"status": "degraded", "latency_ms": ms, "detail": f"Last run {int(age)}s ago"}
        return {"status": "unknown", "latency_ms": ms, "detail": "No scheduled tasks"}
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "down", "latency_ms": ms, "detail": str(exc)[:200]}


def _check_browser_service():
    t0 = time.monotonic()
    try:
        service_url = getattr(settings, "BROWSER_SERVICE_URL", "http://browser_service:8001")
        resp = httpx.get(f"{service_url}/health", timeout=5)
        ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code in (200, 503):
            data = resp.json()
            is_ok = resp.status_code == 200
            result = {
                "status": "up" if is_ok else "degraded",
                "latency_ms": ms,
                "detail": f"{'Ready' if is_ok else 'Degraded'} — {data.get('status', '?')}",
                "components": {
                    "mcp_chrome": data.get("mcp_chrome_running"),
                    "scraper_chrome": data.get("scraper_chrome_running"),
                    "xvfb": data.get("xvfb_running"),
                    "mcp_process": data.get("mcp_process_alive"),
                },
                "cdp": {
                    "mcp_port": data.get("mcp_cdp_port"),
                    "scraper_port": data.get("scraper_cdp_port"),
                    "mcp_latency_ms": data.get("mcp_cdp_latency_ms"),
                    "scraper_latency_ms": data.get("scraper_cdp_latency_ms"),
                    "mcp_cdp_alive": data.get("mcp_cdp_alive"),
                    "scraper_cdp_alive": data.get("scraper_cdp_alive"),
                },
                "proxy": {
                    "datacenter": data.get("proxy_datacenter"),
                    "residential": data.get("proxy_residential"),
                },
                "uptime_seconds": data.get("uptime_seconds"),
            }
            return result
        return {"status": "down", "latency_ms": ms, "detail": f"HTTP {resp.status_code}"}
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "down", "latency_ms": ms, "detail": str(exc)[:200]}


@login_required
def health_api(request):
    checks = {
        "django": None,
        "postgres": _check_db,
        "redis": _check_redis,
        "celery_worker": _check_celery_worker,
        "celery_beat": _check_celery_beat,
        "browser_service": _check_browser_service,
    }
    labels = {
        "django": "Django",
        "postgres": "PostgreSQL",
        "redis": "Redis",
        "celery_worker": "Celery Worker",
        "celery_beat": "Celery Beat",
        "browser_service": "Browser Service",
    }
    services = {}
    for name, check_fn in checks.items():
        if check_fn:
            services[name] = check_fn()
        else:
            services[name] = {"status": "up", "latency_ms": 0, "detail": "OK"}
        services[name]["label"] = labels[name]
    return JsonResponse(services)


@login_required
def health_dashboard(request):
    return render(request, "scraper/health.html")


@login_required
def jobs_dashboard(request):
    """Dashboard: all scraped job listings posted in the last N days."""
    try:
        days = int(request.GET.get("days", 7))
    except (ValueError, TypeError):
        days = 7
    days = max(1, min(days, 365))

    cutoff = timezone.now().date() - timedelta(days=days)
    listings = JobListing.objects.filter(
        posted_date__gte=cutoff, scrape_job__user=request.user
    ).select_related("scrape_job", "site")

    # Optional filters
    company = request.GET.get("company", "").strip()
    if company:
        listings = listings.filter(company__icontains=company)
    location = request.GET.get("location", "").strip()
    if location:
        listings = listings.filter(location__icontains=location)
    site = request.GET.get("site", "").strip()
    if site:
        listings = listings.filter(site_slug=site)

    total = listings.count()
    unique_companies = listings.values_list("company", flat=True).exclude(company="").distinct().count()
    unique_sites = listings.values_list("site_slug", flat=True).exclude(site_slug="").distinct().count()

    # Site choices for the filter dropdown
    site_choices = (
        JobListing.objects.filter(scrape_job__user=request.user)
        .exclude(site_slug="")
        .values_list("site_slug", flat=True)
        .distinct()
        .order_by("site_slug")
    )

    context = {
        "listings": listings[:500],
        "days": days,
        "day_choices": [1, 7, 14, 30],
        "total": total,
        "unique_companies": unique_companies,
        "unique_sites": unique_sites,
        "site_choices": site_choices,
        "company_filter": company,
        "location_filter": location,
        "site_filter": site,
    }
    return render(request, "scraper/jobs_dashboard.html", context)


# ═══════════════════════════════════════════════════════════════════════════
# Intake UI (templates/scraper-intake.html wired into the framework)
#
# Three endpoints back the two-screen intake prototype:
#   intake            — renders the page (injects CSRF + URLs + known sites)
#   intake_check_site — AJAX probe → suggested fields (reuses the real probe
#                       stack so Celery/browser/proxy are all exercised; the
#                       probe auto-warms ProbeCache for the later scrape)
#   intake_create_job — AJAX job creation → JSON dashboard URLs (enqueues the
#                       full LangGraph pipeline via run_scrape_task)
# The dashboard itself reuses the existing /jobs/<id>/* endpoints verbatim.
# ═══════════════════════════════════════════════════════════════════════════


# Intake nav-method radio → canonical input_mode (see PAGE_TYPE_MAP in
# src/content_types.py). "list" = url_list, "listing" = list_page,
# "search" = search_term. The explicit input_mode wins in _build_initial_state.
_INTAKE_NAV_TO_INPUT_MODE = {
    "list": "url_list",
    "listing": "list_page",
    "search": "search_term",
}


def _types_of(value) -> list[str]:
    """Normalize a JSON-LD @type value (str | list | None) to a list of str."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []


def _infer_fields_from_probe(probe: dict) -> tuple[list[str], str]:
    """Infer extractable fields + content type from a probe result's JSON-LD.

    Walks JSON-LD ``@type`` values (including ``@graph`` nodes) and scores them
    against the CONTENT_TYPES registry's ``jsonld_types``. Returns
    ``(field_names, content_type_name)``. Falls back to ``(["title","url"],
    "page_content")`` when nothing matches. Generic across content domains
    (products / jobs / articles / forum); no hardcoded site names.
    """
    from src.content_types import CONTENT_TYPES

    type_strings: set[str] = set()
    for block in probe.get("jsonld") or []:
        if not isinstance(block, dict):
            continue
        type_strings.update(_types_of(block.get("@type")))
        graph = block.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict):
                    type_strings.update(_types_of(node.get("@type")))

    best_ct, best_score = "", 0
    for ct_name, cfg in CONTENT_TYPES.items():
        jl = getattr(cfg, "jsonld_types", ()) or ()
        if not jl:
            continue  # skip content types with no JSON-LD signal (e.g. serp)
        score = sum(1 for t in jl if t in type_strings)
        if score > best_score:
            best_ct, best_score = ct_name, score

    if best_ct:
        return list(CONTENT_TYPES[best_ct].core_field_names), best_ct
    return ["title", "url"], "page_content"


@login_required
def intake(request):
    """Render the intake page with known-site field hints injected."""
    known: dict[str, list] = {}
    for site in Site.objects.exclude(fields_extracted=[]):
        if site.url:
            known[site.url] = site.fields_extracted or []
    return render(
        request,
        "scraper/intake.html",
        {"known_site_fields": json.dumps(known)},
    )


@login_required
def intake_check_site(request):
    """AJAX: look up extractable fields from previously-run scrapers (no probe).

    Instead of hitting the website (which was slow + hit anti-bot), reads the
    union of fields extracted/requested across ALL runs + ALL users for this
    domain. Three DB sources:
    - Site.output_schema (the latest run's resolved schema)
    - Site.fields_extracted (accumulated actual output record keys across runs)
    - ScrapeJob.target_fields (all users' requested fields for this host)

    If no prior data → fields=[] → the JS shows "New site — add the fields".
    """
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        return JsonResponse({"error": "AJAX required"}, status=400)
    url = request.POST.get("url", "").strip()
    if not url:
        return JsonResponse({"error": "url required"}, status=400)

    from .tasks import _generate_slug
    from urllib.parse import urlparse as _urlparse

    slug = _generate_slug(url)
    host = (_urlparse(url).hostname or "").replace("www.", "")
    fields: set = set()
    content_type = ""
    platform = ""
    scraping_method = ""

    # 1. Site-level: output_schema (latest schema) + fields_extracted (accumulated union)
    site = Site.objects.filter(slug=slug).first()
    if site:
        platform = site.platform
        scraping_method = site.scraping_method
        if site.output_schema:
            content_type = site.output_schema.get("content_type", "")
            for f in (site.output_schema.get("fields") or []):
                if isinstance(f, dict) and f.get("name"):
                    fields.add(f["name"])
        if site.fields_extracted:
            fields.update(site.fields_extracted)

    # 2. All users' target_fields for this host (shared field knowledge)
    if host:
        for tf in ScrapeJob.objects.filter(url__icontains=host).values_list(
            "target_fields", flat=True
        ):
            if tf:
                fields.update(tf)

    if fields:
        return JsonResponse(
            {
                "known_site": True,
                "fields": sorted(fields),
                "content_type": content_type,
                "platform": platform,
                "scraping_method": scraping_method,
            }
        )

    # No prior data → the JS shows "New site — add the fields you want to extract"
    return JsonResponse({"known_site": False, "fields": [], "content_type": ""})


def _parse_url_lines(text: str) -> list[str]:
    """Parse a textarea/file of URLs (one per line) into a deduped list."""
    urls: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip().strip(",").strip('"').strip("'").strip()
        if line and line.startswith(("http://", "https://")) and line not in urls:
            urls.append(line)
    return urls


@login_required
def intake_create_job(request):
    """AJAX: create a ScrapeJob + enqueue the full pipeline.

    Returns JSON with the dashboard URLs the intake page needs (SSE, API,
    tool-calls, downloads, cancel). The actual scraping runs through the
    existing 25-node LangGraph pipeline via ``run_scrape_task`` — no new
    pipeline code.
    """
    if request.method != "POST" or request.headers.get("x-requested-with") != "XMLHttpRequest":
        return JsonResponse({"error": "POST + AJAX required"}, status=400)

    url = request.POST.get("url", "").strip()
    nav_method = request.POST.get("nav_method", "list").strip()
    content_type = request.POST.get("content_type", "").strip()
    target_fields = request.POST.get("target_fields", "").strip()
    scope = request.POST.get("scope", "all").strip()
    scope_value = request.POST.get("scope_value", "").strip()
    notes = request.POST.get("notes", "").strip()

    if not url:
        return JsonResponse({"error": "url required"}, status=400)

    input_mode = _INTAKE_NAV_TO_INPUT_MODE.get(nav_method, "url_list")
    page_type = content_type or "product"

    # nav-specific discovery data → search_criteria (agents read it).
    search_criteria = ""
    search_url = ""
    if nav_method == "listing":
        search_criteria = request.POST.get("listing_urls", "").strip()
    elif nav_method == "search":
        search_criteria = request.POST.get("search_keywords", "").strip()
        search_url = request.POST.get("search_url", "").strip()

    existing = ScrapeJob.objects.filter(
        url=url, status__in=[ScrapeJob.STATUS_PENDING, ScrapeJob.STATUS_RUNNING],
        user=request.user,
    ).first()
    if existing:
        return JsonResponse(
            {
                "error": f"A job for this URL is already running (Job #{existing.id})",
                "job_id": existing.id,
            },
            status=409,
        )

    fields_list = (
        [f.strip() for f in target_fields.split(",") if f.strip()]
        if target_fields
        else []
    )

    job = ScrapeJob.objects.create(
        url=url,
        product_url=url,
        page_type=page_type,
        input_mode=input_mode,
        search_criteria=search_criteria,
        search_url=search_url,
        target_fields=fields_list,
        scope=scope,
        scope_value=scope_value,
        notes=notes,
        full_extraction=False,  # "sample" mode
        skip_approvals=True,  # intake jobs run unattended — skip approval gates
        user=request.user,
    )

    # For "list" mode, persist the provided item URLs so the url_list pipeline
    # finds them (_build_initial_state falls back to scrapers/{slug}/input_urls.json
    # when Site.input_urls is empty).
    if nav_method == "list":
        urls = _parse_url_lines(request.POST.get("list_urls", ""))
        if urls:
            try:
                from .tasks import _generate_slug

                slug = _generate_slug(url)
                iu_dir = os.path.join(settings.PROJECT_ROOT, "scrapers", slug)
                os.makedirs(iu_dir, exist_ok=True)
                iu_path = os.path.join(iu_dir, "input_urls.json")
                with open(iu_path, "w", encoding="utf-8") as f:
                    json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
            except Exception as exc:
                logger.warning(
                    "intake: could not persist list URLs for %s: %s",
                    url[:80],
                    exc,
                )

    from .tasks import run_scrape_task

    task = run_scrape_task.delay(job.id, rescrape=False)
    job.celery_task_id = task.id
    job.save(update_fields=["celery_task_id"])

    return JsonResponse(
        {
            "job_id": job.id,
            "events_url": reverse("job_events", args=[job.id]),
            "api_url": reverse("job_api", args=[job.id]),
            "tool_calls_url": reverse("tool_calls_api", args=[job.id]),
            "scraper_code_url": reverse("scraper_code", args=[job.id]),
            "dagster_code_url": reverse("dagster_code", args=[job.id]),
            "job_detail_url": reverse("job_detail", args=[job.id]),
            "cancel_url": reverse("job_cancel", args=[job.id]),
            "restart_url": reverse("job_restart", args=[job.id]),
        }
    )


@login_required
def intake_jobs(request):
    """AJAX: recent ScrapeJobs for the Jobs & Saved library view.

    Returns a flat list (the client groups for the Saved tab by site and filters
    on is_saved). Saved jobs are always included even if older.
    """
    recent = list(ScrapeJob.objects.filter(user=request.user).order_by("-created_at")[:100])
    saved_ids = set(
        ScrapeJob.objects.filter(is_saved=True, user=request.user).values_list("id", flat=True)
    )
    # merge any saved jobs that fell outside the recent-100 window
    extra = [j for j in ScrapeJob.objects.filter(is_saved=True, user=request.user).order_by("-created_at")
             if j.id not in {j.id for j in recent}]
    jobs = recent + extra[:50]

    def _dur(j):
        if j.started_at and j.completed_at:
            return (j.completed_at - j.started_at).total_seconds()
        return None

    data = [
        {
            "id": j.id,
            "title": j.title,
            "url": j.url,
            "status": j.status,
            "product_count": j.product_count,
            "duration_seconds": _dur(j),
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "is_saved": j.is_saved,
            "target_fields": j.target_fields or [],
            "input_mode": j.input_mode,
            "site_name": j.site_name,
        }
        for j in jobs
    ]
    return JsonResponse({"jobs": data})

