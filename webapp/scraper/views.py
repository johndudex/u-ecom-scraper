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
from django.db.models import Q
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

from src.schema_validation import validate_user_schema

logger = logging.getLogger(__name__)


def _get_job(request, job_id):
    """Fetch a ScrapeJob. Superusers can access ANY job (regardless of owner);
    regular users can only access their own. Returns the job or raises Http404."""
    if request.user.is_superuser:
        return get_object_or_404(ScrapeJob, pk=job_id)
    return get_object_or_404(ScrapeJob, pk=job_id, user=request.user)


def _user_jobs(request):
    """Return the job queryset: ALL jobs for superusers, own jobs for regular users."""
    if request.user.is_superuser:
        return ScrapeJob.objects.all()
    return ScrapeJob.objects.filter(user=request.user)


# ── File Master helpers (cross-service artifact store) ──────────────────────
# Published artifacts (scrapers/{slug}/...) live in the File Master, not on a
# shared disk. These wrappers keep views terse and centralize the locator logic.


def _fm_key_for(stored: str) -> str | None:
    """Normalize a DB-stored artifact locator to a File Master key (or None).

    Handles legacy absolute paths under PROJECT_ROOT, relative ``scrapers/...``
    (already a key), and bare paths. Returns None for non-scrapers/workspace or
    empty input.
    """
    if not stored:
        return None
    s = stored.strip()
    try:
        pr = str(settings.PROJECT_ROOT)
        if s.startswith(pr):
            s = s[len(pr):].lstrip("/")
    except Exception:
        pass
    if s.startswith("scrapers/") or s.startswith("workspace/"):
        return s
    return None


def _fm_read_text(key: str) -> str | None:
    try:
        import src.artifacts as artifacts
        return artifacts.read_text(key)
    except Exception:
        return None


def _fm_read_json(key: str):
    try:
        import src.artifacts as artifacts
        return artifacts.read_json(key)
    except Exception:
        return None


def _fm_exists(key: str) -> bool:
    try:
        import src.artifacts as artifacts
        return artifacts.exists(key)
    except Exception:
        return False


def _fm_list(prefix: str) -> list[str]:
    try:
        import src.artifacts as artifacts
        return artifacts.list_keys(prefix)
    except Exception:
        return []


@login_required
def fm_artifact(request, key):
    """Stream an artifact from the File Master (users never hit FM directly)."""
    import src.artifacts as artifacts
    try:
        data = artifacts.read(key)
    except FileNotFoundError:
        raise Http404("artifact not found")
    except Exception as exc:
        return HttpResponse(status=502, content=str(exc)[:200])
    resp = HttpResponse(data, content_type="application/octet-stream")
    resp["Content-Disposition"] = f'attachment; filename="{os.path.basename(key)}"'
    return resp


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


def _url_slug(url: str) -> str:
    """Hostname slug (mirrors tasks._generate_slug) — for the F10 existence check."""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return "".join(c if c.isalnum() else "-" for c in host) or "site"
    except Exception:
        return "site"


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
            "recent_jobs": _user_jobs(request)[:10],
            "content_types": content_types,
        }

        if not url:
            context["error"] = "URL is required"
            return render(request, "scraper/home.html", context)

        # F10: guard the legacy form. url/product_url are URLFields (varchar
        # 200) — a multi-URL paste into the single-URL field DataErrors into
        # a 500 (prod 255+: four POST / → 500 tracebacks), and the surviving
        # single-URL submissions created doomed url_list jobs that failed at
        # setup_workspace's fail-fast. Reject at POST time with guidance
        # instead of failing ~5 nodes into the graph.
        if len(url) > 200 or len(product_url) > 200:
            context["error"] = (
                "That looks like multiple URLs pasted into a single-URL field. "
                'Use the <a href="/intake/">intake form</a> and choose '
                '"I have the exact list" to submit a URL list.'
            )
            return render(request, "scraper/home.html", context)
        if input_mode == "url_list":
            # This form has no URL-list input; a url_list-mode job from here
            # has no URLs to extract. Point the user at the intake form.
            from .models import Site

            _slug_site = Site.objects.filter(url=url.rstrip("/")).first()
            _has_urls = bool(
                _slug_site and getattr(_slug_site, "input_urls", None)
            ) or os.path.isfile(
                os.path.join(
                    str(getattr(settings, "PROJECT_ROOT", os.getcwd())),
                    "scrapers",
                    _url_slug(url),
                    "input_urls.json",
                )
            )
            if not _has_urls:
                context["error"] = (
                    "This quick form can't submit a product-URL list. Use the "
                    '<a href="/intake/">intake form</a> with '
                    '"I have the exact list", or pick a page type that '
                    "navigates (e.g. Listing page)."
                )
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

        from .tasks import dispatch_scrape_job

        # [wave-15 1.0] keystone: stamp BEFORE publish so "" strictly means
        # "never dispatched" (redispatch sweep + /health stranded gauge).
        dispatch_scrape_job(job.id, rescrape=rescrape)
        return redirect("job_detail", job_id=job.id)

    recent_jobs = _user_jobs(request)[:10]
    return render(request, "scraper/home.html", {"recent_jobs": recent_jobs, "content_types": content_types})


@login_required
def job_list(request):
    jobs = _user_jobs(request).prefetch_related("steps", "approvals")[:]
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
            "show_owner": request.user.is_superuser,
        },
    )


@login_required
def job_detail(request, job_id):
    job = _get_job(request, job_id)
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
        name_slug = "".join(c if c.isalnum() or c == "-" else "-" for c in name_slug)
        slug_candidates.append(name_slug)
    for slug in slug_candidates:
        code = _fm_read_text(f"scrapers/{slug}/scraper.py")
        if code is not None:
            scraper_code_display = code
            has_scraper_code = True
            scraper_slug = slug
            has_dagster_code = _fm_exists(f"scrapers/{slug}/{slug}_dagster.py")
            break

    if scraper_slug:
        all_output = [
            k.split("/")[-1]
            for k in _fm_list(f"scrapers/{scraper_slug}/")
            if k.split("/")[-1].startswith("output_") and k.endswith(".json")
        ]
        job_start = job.started_at
        if all_output:
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

    # Owner info: surfaced when an admin is viewing another user's job.
    is_admin = request.user.is_superuser
    show_owner = is_admin and job.user_id and job.user_id != request.user.id
    owner_display = None
    if show_owner and job.user_id:
        owner_display = job.user.get_username()
        if job.user.email:
            owner_display = f"{owner_display} ({job.user.email})"

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
            "show_owner": show_owner,
            "owner_display": owner_display,
        },
    )


@login_required
def job_cancel(request, job_id):
    job = _get_job(request, job_id)
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
    """Resolve a job's stored artifact locator (scraper_file/dagster_file) to a
    File Master KEY that exists, or None. Handles legacy absolute paths + keys."""
    key = _fm_key_for(stored)
    if key and _fm_exists(key):
        return key
    return None


def _slug_candidates(job) -> list[str]:
    """Candidate site slugs for resolving a job's artifacts (site_folder, name, slug)."""
    cands: list[str] = []
    if job.site_folder:
        cands.append(job.site_folder.removeprefix("scrapers/"))
    if getattr(job, "site_slug", ""):
        cands.append(job.site_slug)
    if job.site_name:
        name_slug = job.site_name.lower().replace(" ", "-").replace(".", "-")
        name_slug = "".join(c if c.isalnum() or c == "-" else "-" for c in name_slug)
        cands.append(name_slug)
    return [c for c in cands if c]


@login_required
def scraper_code(request, job_id):
    job = _get_job(request, job_id)

    # Per-job attribution: this job's own scraper (if recorded) wins over the
    # shared scrapers/{slug}/scraper.py (which later jobs may overwrite).
    own = _resolve_stored_path(job.scraper_file)
    if own:
        content = _fm_read_text(own)
        if content is not None:
            slug = _resolve_job_slug(job) or "scraper"
            resp = HttpResponse(content, content_type="text/x-python")
            resp["Content-Disposition"] = f'attachment; filename="{slug}_scraper.py"'
            return resp

    for slug in _slug_candidates(job):
        content = _fm_read_text(f"scrapers/{slug}/scraper.py")
        if content is not None:
            response = HttpResponse(content, content_type="text/x-python")
            response["Content-Disposition"] = (
                f'attachment; filename="{slug}_scraper.py"'
            )
            return response
    return HttpResponseNotFound("Scraper code not found")


@login_required
def dagster_code(request, job_id):
    """Download the Dagster-format scraper ({slug}_dagster.py)."""
    job = _get_job(request, job_id)
    # Per-job attribution first (this job's own dagster file).
    own = _resolve_stored_path(job.dagster_file)
    if own:
        content = _fm_read_text(own)
        if content is not None:
            slug = _resolve_job_slug(job) or "scraper"
            resp = HttpResponse(content, content_type="text/x-python")
            resp["Content-Disposition"] = f'attachment; filename="{slug}_dagster.py"'
            return resp
    slug = _resolve_job_slug(job)
    if not slug:
        return HttpResponseNotFound("Could not resolve site slug")
    content = _fm_read_text(f"scrapers/{slug}/{slug}_dagster.py")
    if content is None:
        return HttpResponseNotFound("Dagster code not generated yet")
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
    job = _get_job(request, job_id)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".json"):
        raise Http404("Only JSON files can be viewed")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    slug = _resolve_job_slug(job)
    if not slug:
        raise Http404("No scraper folder for this job")
    key = f"scrapers/{slug}/{safe_name}"
    data = _fm_read_json(key)
    if data is None:
        raise Http404("File not found")
    try:
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
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
    job = _get_job(request, job_id)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".json"):
        raise Http404("Only JSON files can be downloaded")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    slug = _resolve_job_slug(job)
    if not slug:
        raise Http404("No scraper folder for this job")
    key = f"scrapers/{slug}/{safe_name}"
    import src.artifacts as artifacts
    try:
        data = artifacts.read(key)
    except FileNotFoundError:
        raise Http404("File not found")
    resp = HttpResponse(data, content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return resp


@login_required
def job_restart(request, job_id):
    job = _get_job(request, job_id)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if job.status in [
        ScrapeJob.STATUS_COMPLETED,
        ScrapeJob.STATUS_FAILED,
        ScrapeJob.STATUS_CANCELLED,
    ]:
        # Intake "Re-run" sends an optional prompt → store as notes for the
        # agents to see. It ALSO sends the edited config (target_fields, scope,
        # etc.) so the new job picks up the user's dashboard edits. Non-AJAX
        # (job_detail button) sends nothing → falls back to old job's config.
        rerun_prompt = request.POST.get("prompt", "").strip() if request.method == "POST" else ""

        # Full re-run (the "Full re-run" button/link): bypass the selective
        # rescrape diff — wipe the stale workspace + analysis archive and
        # regenerate EVERY phase, even when a prior completed job exists.
        force_full = (
            request.POST.get("force_full") == "1" or request.GET.get("full") == "1"
        )

        def _post_or_old(field, old_val):
            v = request.POST.get(field)
            return v if v is not None else old_val

        _tf = request.POST.get("target_fields", "")
        if _tf:
            _tf = [f.strip() for f in _tf.split(",") if f.strip()]
        else:
            _tf = job.target_fields

        new_job = ScrapeJob.objects.create(
            url=job.url,
            product_url=job.product_url,
            currency=job.currency,
            page_type=job.page_type,
            input_mode=job.input_mode,
            search_criteria=_post_or_old("search_criteria", job.search_criteria),
            search_url=_post_or_old("search_url", job.search_url),
            target_fields=_tf,
            scope=_post_or_old("scope", job.scope),
            scope_value=_post_or_old("scope_value", job.scope_value),
            schema_text=_post_or_old("schema_text", job.schema_text or ""),
            notes=(rerun_prompt or _post_or_old("notes", job.notes)),
            full_extraction=job.full_extraction,
            skip_approvals=job.skip_approvals,
            # Dagster opt-in carries to the re-run: the intake re-run form posts
            # the checkbox state explicitly ('1'/'0'); a bare job_detail re-run
            # (no checkbox in that form) copies the old job's choice.
            dagster_enabled=(
                request.POST.get("dagster_enabled") == "1"
                if "dagster_enabled" in request.POST
                else job.dagster_enabled
            ),
            user=request.user,
        )

        from .tasks import dispatch_scrape_job

        # [wave-15 1.0] keystone: stamp BEFORE publish (see dispatch_scrape_job).
        dispatch_scrape_job(new_job.id, rescrape=True, force_full=force_full)
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
    job = _get_job(request, job_id)
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
def _approval_scope(request):
    """F11: the approval visibility scope. Superusers see ALL pending approvals;
    regular users see their own PLUS ownerless ones (auto-queued jobs have
    user=None and were invisible to everyone but Django admin — prod 257 sat
    pending for days)."""
    if request.user.is_superuser:
        # Match-all Q, NOT None: callers do .filter(_approval_scope(request)),
        # and filter(None) raised TypeError — every /approvals/ and
        # /approvals/count/ request by a SUPERUSER 500'd since 41f2828
        # (prod: badge silently zeroed by the dashboard's .catch(), approvals
        # page unreachable).
        return Q()
    return Q(job__user=request.user) | Q(job__user__isnull=True)


def _approval_visible(approval, request) -> bool:
    """F11 companion: can this user act on this approval?"""
    return (
        request.user.is_superuser
        or approval.job_id is None
        or approval.job is None
        or approval.job.user_id == request.user.id
        or approval.job.user_id is None
    )


def approval_inline(request, job_id, approval_id):
    approval = get_object_or_404(Approval, pk=approval_id, job_id=job_id)
    if not _approval_visible(approval, request):
        raise Http404("approval not found")
    choice = request.POST.get("choice", "")
    feedback = request.POST.get("feedback", "").strip()

    if not choice:
        return redirect("job_detail", job_id=job_id)

    human_response = _build_resume_value(approval, choice, feedback)

    # N3: record the user's actual decision — a Cancel is a rejection, not an
    # approval (prod approvals 398/399 permanently said 'approved' on cancels).
    approval.status = (
        Approval.STATUS_REJECTED
        if human_response.get("decision") == "reject"
        else Approval.STATUS_APPROVED
    )
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
        Approval.objects.filter(status=Approval.STATUS_PENDING).filter(_approval_scope(request))
        .select_related("job")
        .order_by("-created_at")
    )
    return render(request, "scraper/approval_list.html", {"approvals": approvals})


@login_required
def approval_count(request):
    count = Approval.objects.filter(status=Approval.STATUS_PENDING).filter(_approval_scope(request)).count()
    return JsonResponse({"count": count})


@login_required
def approval_detail(request, approval_id):
    approval = get_object_or_404(Approval, pk=approval_id)
    if not _approval_visible(approval, request):
        raise Http404("approval not found")
    if request.method == "POST":
        choice = request.POST.get("choice", "")
        feedback = request.POST.get("feedback", "").strip()

        if not choice:
            return redirect("approval_list")

        human_response = _build_resume_value(approval, choice, feedback)

        # N3 (approval_detail): same rejection recording as approval_inline.
        approval.status = (
            Approval.STATUS_REJECTED
            if human_response.get("decision") == "reject"
            else Approval.STATUS_APPROVED
        )
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
    job = _get_job(request, job_id)
    # Per-job attribution first; then the shared production scraper.
    key = _resolve_stored_path(job.scraper_file)
    if not key and job.site_folder:
        _k = f"scrapers/{job.site_folder.removeprefix('scrapers/')}/scraper.py"
        if _fm_exists(_k):
            key = _k
    if not key:
        return JsonResponse({"error": "Scraper file not found"}, status=404)
    code = _fm_read_text(key)
    if code is None:
        return JsonResponse({"error": "Scraper file not readable"}, status=500)
    return JsonResponse({"path": key, "code": code})


def _resolve_job_output(job):
    """Return (fm_key, filename) for THIS job's output file, or (None, None).

    All runs for a site share one scrapers/{slug}/ prefix in the File Master, so
    picking the newest there would show another job's output. We prefer the job's
    own ``output_file`` (authoritative, now an FM key); else the newest
    output_*.json whose timestamp falls within this job's run window.
    """
    if job.status == ScrapeJob.STATUS_PENDING:
        return None, None
    # 1. authoritative output_file (FM key now)
    of = (job.output_file or "").strip()
    key = _fm_key_for(of)
    if key and _fm_exists(key):
        return key, os.path.basename(key)
    # 2. slug → outputs in FM within the run window
    slug = _resolve_job_slug(job)
    if not slug:
        try:
            from .tasks import _generate_slug

            slug = _generate_slug(job.url)
        except Exception:
            slug = ""
    if not slug:
        return None, None
    outs = [
        k for k in _fm_list(f"scrapers/{slug}/")
        if k.split("/")[-1].startswith("output_") and k.endswith(".json")
    ]
    if not outs:
        return None, None

    def _fname(k):
        return k.split("/")[-1]

    if job.started_at:
        cutoff = job.started_at - timedelta(seconds=120)
        in_window = []
        for k in outs:
            ts = _output_key_ts(_fname(k))
            if ts is None:
                continue
            if ts >= cutoff:
                in_window.append(k)
        if in_window:
            # job-76 myhouse: a --discover-only artifact (URL stubs) must not
            # be surfaced as the job's output ("40 products" on a FAILED job).
            extracted = [k for k in in_window if not _is_discovery_output_key(k)]
            pick = extracted[0] if extracted else None
            if pick:
                return pick, _fname(pick)
            return None, None
        return None, None
    newest = [k for k in sorted(outs, key=_fname) if not _is_discovery_output_key(k)]
    if not newest:
        return None, None
    return newest[-1], _fname(newest[-1])


def _output_key_ts(fname: str):
    """Parse the run timestamp from an ``output_*.json`` FM key.

    Handles both naming generations: ``output_%Y-%m-%d_%H%M%S.json`` and the
    unique-per-process ``output_%Y-%m-%d_%H%M%S_%f_<pid>.json`` (job-71 — the
    old strict strptime silently skipped every new-format name, which would
    have blanked the run-window output lookup).
    """
    stem = fname.split("/")[-1]
    if not (stem.startswith("output_") and stem.endswith(".json")):
        return None
    body = stem[len("output_"): -len(".json")]
    try:
        return datetime.strptime(body, "%Y-%m-%d_%H%M%S").replace(tzinfo=dt_timezone.utc)
    except ValueError:
        pass
    # Unique-per-process name: output_<date>_<time>_<micros>_<pid> — the run
    # stamp is the date + time segments.
    segs = body.split("_")
    if len(segs) >= 4:
        try:
            return datetime.strptime("_".join(segs[:2]), "%Y-%m-%d_%H%M%S").replace(
                tzinfo=dt_timezone.utc
            )
        except ValueError:
            return None
    return None


def _is_discovery_output_key(key: str) -> bool:
    """True when the FM artifact is a ``--discover-only`` output (URL stubs).

    Mirrors ``agents.nodes.route_after_testing._is_discovery_output`` — kept
    local so views don't import the agent graph. Files without the
    ``metadata.phase`` tag are treated as extraction (pre-tagging outputs).
    """
    try:
        data = _fm_read_json(key)
    except Exception:
        return False
    return isinstance(data, dict) and (data.get("metadata") or {}).get("phase") == "discovery"


def _job_output_preview(job) -> dict | None:
    """This job's extracted output, for the intake Sample Output card.

    Returns {filename, output_key, item_label, count, records (first 3),
    download_url} or None when no output exists yet. Uses _resolve_job_output so
    the preview is always THIS job's output, not another run's. Caps records so
    the job_api payload stays small (full output is via download_url).
    """
    key, fname = _resolve_job_output(job)
    if not key:
        return None
    data = _fm_read_json(key)
    if data is None:
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
    job = _get_job(request, job_id)
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
            # Owner info (only populated when an admin is viewing another user's job).
            "owner_username": (job.user.username if (request.user.is_superuser and job.user_id) else None),
            "owner_email": (job.user.email if (request.user.is_superuser and job.user_id) else None),
            # Intake config (drives the Current Configuration panel on deep-link)
            "target_fields": list(job.target_fields or []),
            "input_mode": job.input_mode,
            "search_criteria": job.search_criteria,
            "search_url": job.search_url,
            "scope": job.scope,
            "scope_value": job.scope_value,
            "notes": job.notes,
            "schema_text": job.schema_text or "",
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
    job = _get_job(request, job_id)
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
    # Authorize: admins see any job, regular users only their own.
    _get_job(request, job_id)
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
    job = _get_job(request, job_id)

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
    job = _get_job(request, job_id)
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
    job = _get_job(request, job_id)
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
    jobs = _user_jobs(request).filter(url__iexact=site.url)
    scraper_code = ""
    if site.default_scraper_path:
        scraper_code = _fm_read_text(site.default_scraper_path) or ""

    output_files = []
    scraper_archives = []
    if site.slug:
        for k in sorted(_fm_list(f"scrapers/{site.slug}/"), reverse=True):
            name = k.split("/")[-1]
            if name.startswith("output_") and name.endswith(".json"):
                output_files.append({"name": name, "size": 0})
            elif name.startswith("scraper-") and name.endswith(".py"):
                scraper_archives.append({"name": name, "size": 0})

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

    if site.input_urls and site.slug:
        import src.artifacts as artifacts
        artifacts.write_json(
            artifacts.scrapers_key(site.slug, "input_urls.json"),
            {"urls": site.input_urls},
        )

    from .tasks import dispatch_scrape_job

    # [wave-15 1.0] keystone: stamp BEFORE publish (see dispatch_scrape_job).
    dispatch_scrape_job(job.id, rescrape=rescrape)
    return redirect("job_detail", job_id=job.id)


@login_required
def site_rerun(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if request.method != "POST":
        return redirect("site_detail", site_id=site.id)

    if not site.has_scraper or not site.default_scraper_path:
        return redirect("site_detail", site_id=site.id)

    import src.artifacts as artifacts
    scraper_key = site.default_scraper_path  # FM key now
    slug = site.slug or ""
    _source = _fm_read_text(scraper_key) or ""

    if site.input_urls and slug:
        # [wave-14 job-133] Same full-host seed filter as intake — the FM
        # file is a seed contract, and a re-run must not resurrect a poison
        # link that a later job's filter would drop.
        try:
            from src.seed_urls import filter_seed_urls

            _seed_urls = filter_seed_urls(site.input_urls, site.url or "")
        except Exception:
            _seed_urls = site.input_urls
        artifacts.write_json(
            artifacts.scrapers_key(slug, "input_urls.json"),
            {"urls": _seed_urls},
        )

    BROWSER_METHODS = {
        "undetected_chromedriver",
        "seleniumbase_uc",
        "playwright",
        "undetected_chromedriver_scraper",
        "stealth_browser",
        "uc_chrome",
    }

    if site.scraping_method in BROWSER_METHODS:
        service_url = getattr(
            settings, "BROWSER_SERVICE_URL", "http://browser_service:8001"
        )
        # Anti-bot/Akamai sites: route the scraper through CloakBrowser stealth.
        # Stateless /scrape: POST the scraper SOURCE (read from FM) + persist the
        # returned output back to FM.
        # Read sibling files (input_urls.json, discovery_config.json) from FM for staging
        _rerun_extra = {}
        for _sf in ("input_urls.json", "discovery_config.json"):
            _txt = _fm_read_text(f"scrapers/{site.slug}/{_sf}")
            if _txt is not None:
                # [wave-14 job-133] the staged seed gets the same full-host
                # filter as every other surface — staging reads the file
                # verbatim, which would otherwise bypass intake entirely.
                if _sf == "input_urls.json":
                    try:
                        import json as _json

                        from src.seed_urls import filter_seed_payload

                        _payload, _drops = filter_seed_payload(
                            _json.loads(_txt), site.url or ""
                        )
                        if _drops:
                            _txt = _json.dumps(_payload)
                            logger.warning(
                                "re-run staging for %s: filtered seed file — %s",
                                site.slug, _drops,
                            )
                    except Exception:
                        pass
                _rerun_extra[_sf] = _txt
        scrape_json = {
            "scraper_source": _source,
            "scraper_name": os.path.basename(scraper_key),
            "extra_files": _rerun_extra,
            "args": [],
            "timeout": 3600,
        }
        if getattr(site, "needs_akamai_bypass", False):
            scrape_json["env_overrides"] = {"STEALTH_BROWSER": "cloak"}
        try:
            # W8: cap the synchronous hold — this request occupies a gunicorn
            # worker (and, post-W4, a SCRAPE slot) for the whole duration; a
            # 3600s HTTP call was its own hazard. 1200s still covers full
            # re-runs of the largest verified catalogs.
            _rerun_timeout = min(int(scrape_json.get("timeout") or 0), 1200)
            scrape_json["timeout"] = _rerun_timeout
            from agents.tools.browser_http import post_scrape_with_retry

            _res = post_scrape_with_retry(
                f"{service_url}/scrape",
                scrape_json,
                timeout=_rerun_timeout + 60,
            )
            if not _res.ok:
                raise RuntimeError(_res.error or "browser_service scrape failed")
            result = _res.data
            _content = result.get("output_content") or ""
            _name = result.get("output_name") or ""
            if _content and _name and slug:
                try:
                    artifacts.write(artifacts.scrapers_key(slug, _name), _content.encode("utf-8"))
                except Exception:
                    pass
            product_count = result.get("product_count", 0)
        except Exception as e:
            logger.error("site_rerun: browser_service failed: %s", e)
            product_count = 0
    else:
        # Local exec: stage the FM source to a tmp dir, run it, publish the output.
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as _td:
                _local = os.path.join(_td, os.path.basename(scraper_key) or "scraper.py")
                with open(_local, "w", encoding="utf-8") as _f:
                    _f.write(_source)
                result = subprocess.run(
                    ["python3", _local],
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    cwd=_td,
                )
                product_count = 0
                if result.returncode == 0:
                    outs = sorted(
                        f for f in os.listdir(_td)
                        if f.startswith("output_") and f.endswith(".json")
                    )
                    if outs:
                        with open(os.path.join(_td, outs[-1]), "r", encoding="utf-8") as f:
                            _data = json.load(f)
                        if slug:
                            try:
                                artifacts.write_json(artifacts.scrapers_key(slug, outs[-1]), _data)
                            except Exception:
                                pass
                        from src.content_types import count_items_in_output
                        product_count = count_items_in_output(_data)
        except Exception as e:
            logger.error("site_rerun: local execution failed: %s", e)
            product_count = 0

    site.last_scraped_at = timezone.now()
    site.product_count = product_count
    site.save(update_fields=["last_scraped_at", "product_count"])

    return redirect("site_detail", site_id=site.id)


@login_required
def site_scraper_code(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if not site.default_scraper_path:
        raise Http404("Scraper code not found")
    import src.artifacts as artifacts
    try:
        data = artifacts.read(site.default_scraper_path)
    except FileNotFoundError:
        raise Http404("Scraper code not found")
    resp = HttpResponse(data, content_type="text/x-python")
    resp["Content-Disposition"] = f'attachment; filename="{site.slug}_scraper.py"'
    return resp


@login_required
def site_scraper_archive_view(request, site_id, filename):
    site = get_object_or_404(Site, pk=site_id)
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".py"):
        raise Http404("Only Python files can be viewed")
    if "/" in safe_name or "\\" in safe_name:
        raise Http404("Invalid filename")
    key = f"scrapers/{site.slug}/{safe_name}"
    code = _fm_read_text(key)
    if code is None:
        raise Http404("File not found")
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
    key = f"scrapers/{site.slug}/{safe_name}"
    import src.artifacts as artifacts
    try:
        data = artifacts.read(key)
    except FileNotFoundError:
        raise Http404("File not found")
    resp = HttpResponse(data, content_type="text/x-python")
    resp["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return resp


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
    key = f"scrapers/{site.slug}/{safe_name}"
    data = _fm_read_json(key)
    if data is None:
        raise Http404("File not found")
    try:
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
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
    key = f"scrapers/{site.slug}/{safe_name}"
    import src.artifacts as artifacts
    try:
        data = artifacts.read(key)
    except FileNotFoundError:
        raise Http404("File not found")
    resp = HttpResponse(data, content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return resp


@login_required
def site_sync_urls(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    if request.method != "POST":
        return redirect("site_detail", site_id=site.id)
    urls = site.input_urls or []
    if not urls:
        return redirect("site_detail", site_id=site.id)
    import src.artifacts as artifacts
    _key = artifacts.scrapers_key(site.slug, "input_urls.json")
    artifacts.write_json(_key, {"urls": urls})
    logger.info("Synced %d URLs to %s", len(urls), _key)
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
                    # W6: WHY is it degraded — lazy-aware scraper state, the
                    # recent /navigate outcome window, and the memory gauge.
                    "scraper_chrome_state": data.get("scraper_chrome_state"),
                    "navigate_recent": data.get("navigate_recent"),
                    "memory": data.get("gauges", {}).get("memory")
                    if isinstance(data.get("gauges"), dict)
                    else None,
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



def _check_file_master():
    """FM health — critical since skills + artifacts live there (plan §observability)."""
    t0 = time.monotonic()
    try:
        import src.artifacts as artifacts

        base = artifacts._base().rstrip("/")
        resp = httpx.get(f"{base}/health", timeout=5)
        ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200:
            return {"status": "up", "latency_ms": ms, "detail": "Ready"}
        return {"status": "degraded", "latency_ms": ms,
                "detail": f"HTTP {resp.status_code}"}
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "down", "latency_ms": ms, "detail": str(exc)[:80]}


# [wave-15 1.3] /health queue gauges. The stranded-PENDING floor itself is
# tasks.PENDING_CLAIM_MINUTES (single source of truth — the same signature the
# redispatch sweep recovers; the floor exists because the same-site serializer
# legitimately parks PENDING rows for hours).
# Event-queue depth that means callbacks aren't draining (events pool wedged).
QUEUE_BACKLOG_THRESHOLD = 10
# A RUNNING row with no SessionLog activity of ANY kind for this long (even
# heartbeats/probes) is silent — normally the watchdog's territory; here it is
# just a visible gauge, not a verdict.
QUEUE_RUNNING_SILENCE_MINUTES = 90


def _check_queue() -> dict:
    """Queue-honesty gauges for /health (wave-15 1.3).

    Complements the per-service checks with the queue's actual state: broker
    depths, oldest queued row, and the two failure signatures that motivated
    wave-15 — stranded PENDING (never dispatched) and silent RUNNING.
    Returns status "up" unless an alert fires ("warn" keeps the dashboard
    green-vs-amber distinction; nothing here is hard-down by itself).
    """
    from django.core.cache import cache
    from scraper.models import SessionLog
    from scraper.tasks import PENDING_CLAIM_MINUTES  # single source of truth

    out: dict = {"status": "up", "latency_ms": 0, "alerts": [], "gauges": {}}
    t0 = time.monotonic()
    alerts: list = []
    try:
        import redis as redis_lib

        r = redis_lib.from_url(getattr(settings, "REDIS_URL", "redis://redis:6379/0"))
        pending_len = int(r.llen("celery") or 0)
        events_len = int(r.llen("events") or 0)
        # Kombu's unacked index: messages delivered to a worker but not yet
        # acked. Inert under acks_late=False (wave-16 enablement) — collected
        # so the baseline exists before that flip.
        unacked = int(r.zcard("unacked_index") or 0)
        out["gauges"].update(
            celery_queue_depth=pending_len,
            events_queue_depth=events_len,
            unacked_index=unacked,
        )
        if events_len > QUEUE_BACKLOG_THRESHOLD:
            alerts.append(f"events queue backlog: {events_len} > {QUEUE_BACKLOG_THRESHOLD}")

        now = timezone.now()
        oldest_pending = (
            ScrapeJob.objects.filter(status=ScrapeJob.STATUS_PENDING)
            .order_by("created_at")
            .first()
        )
        if oldest_pending is not None:
            age_s = int((now - oldest_pending.created_at).total_seconds())
            out["gauges"]["oldest_pending_age_s"] = age_s
            out["gauges"]["oldest_pending_job"] = oldest_pending.id
            # Stranded = never dispatched (keystone signature) AND past the
            # claim floor. A PENDING row WITH an id is queued normally.
            stranded = (
                ScrapeJob.objects.filter(
                    status=ScrapeJob.STATUS_PENDING, celery_task_id=""
                )
                .order_by("created_at")
                .first()
            )
            if stranded is not None:
                strand_s = int((now - stranded.created_at).total_seconds())
                out["gauges"]["oldest_undispatched_age_s"] = strand_s
                out["gauges"]["oldest_undispatched_job"] = stranded.id
                if strand_s > PENDING_CLAIM_MINUTES * 60:
                    alerts.append(
                        f"job {stranded.id} stranded PENDING undispatched "
                        f"{strand_s // 60} min (sweep signature)"
                    )

        oldest_running = (
            ScrapeJob.objects.filter(status=ScrapeJob.STATUS_RUNNING)
            .order_by("created_at")
            .first()
        )
        if oldest_running is not None:
            last_log = (
                SessionLog.objects.filter(job=oldest_running)
                .order_by("-created_at")
                .values_list("created_at", flat=True)
                .first()
            )
            # Oldest row only (per-row silence would be an N+1); anchor on the
            # last session log, falling back the way the watchdog does.
            anchor = last_log or oldest_running.started_at or oldest_running.created_at
            silent_s = int((now - anchor).total_seconds())
            out["gauges"]["oldest_running_job"] = oldest_running.id
            out["gauges"]["oldest_running_silent_s"] = silent_s
            if silent_s > QUEUE_RUNNING_SILENCE_MINUTES * 60:
                alerts.append(
                    f"job {oldest_running.id} RUNNING silent {silent_s // 60} min"
                )

        # Unacked that persists across two consecutive polls means messages
        # stuck mid-delivery, not an in-flight task (cache TTL = poll cadence).
        if unacked:
            prev = cache.get("health:unacked_seen")
            if prev:
                alerts.append(f"unacked_index stuck at {unacked} across polls")
            cache.set("health:unacked_seen", unacked, timeout=310)

        out["status"] = "warn" if alerts else "up"
        out["alerts"] = alerts
        out["detail"] = "; ".join(alerts) if alerts else "queue draining"
    except Exception as exc:
        out["status"] = "down"
        out["detail"] = str(exc)[:200]
    finally:
        out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out


@login_required
def health_api(request):
    checks = {
        "django": None,
        "postgres": _check_db,
        "redis": _check_redis,
        "celery_worker": _check_celery_worker,
        "celery_beat": _check_celery_beat,
        "browser_service": _check_browser_service,
        "file_master": _check_file_master,
        "queue": _check_queue,
    }
    labels = {
        "django": "Django",
        "postgres": "PostgreSQL",
        "redis": "Redis",
        "celery_worker": "Celery Worker",
        "celery_beat": "Celery Beat",
        "browser_service": "Browser Service",
        "file_master": "File Master",
        "queue": "Task Queue",
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
def intake_validate_schema(request):
    """AJAX: validate a user-pasted/uploaded JSON schema and return derived fields.

    Accepts a pasted JSON string (POST ``schema_text``) or an uploaded file
    (FILES ``schema_file``); file wins if both are present. Pure function of
    its inputs (no DB writes) so the front-end can call it on every edit.

    Returns 200 with ``{valid, issues, derived_fields, detected_content_type}``
    even when the schema is invalid (mirrors ``intake_check_site``'s
    200-with-``known_site:false`` convention) — only transport/auth failures 4xx.
    """
    if request.method != "POST" or request.headers.get("x-requested-with") != "XMLHttpRequest":
        return JsonResponse({"error": "POST + AJAX required"}, status=400)

    raw = ""
    upload = request.FILES.get("schema_file")
    if upload is not None:
        # read() is bounded by Django's DATA_UPLOAD_MAX_MEMORY_SIZE (default 2.5MB)
        raw = upload.read().decode("utf-8", errors="replace")
    else:
        raw = request.POST.get("schema_text", "")

    result = validate_user_schema(raw)
    logger.info(
        "intake schema validation: valid=%s shape=%s fields=%d issues=%s",
        result.valid, result.shape, len(result.derived_fields),
        [i.code for i in result.issues],
    )
    return JsonResponse({
        "valid": result.valid,
        "issues": [
            {"code": i.code, "message": i.message, "severity": i.severity, "path": i.path}
            for i in result.issues
        ],
        "derived_fields": result.derived_fields,
        "detected_content_type": result.detected_content_type,
    })


@login_required
def intake_discover_fields(request):
    """AJAX: browse the actual website + discover all extractable fields via LLM.

    Calls browser_service ``/navigate`` (cloak, direct — skip the 90s probe) →
    extracts page content → one-shot LLM → returns field candidates + JSON schema.
    Best-effort: all errors return 200 with a user-friendly message.
    """
    if request.method != "POST" or request.headers.get("x-requested-with") != "XMLHttpRequest":
        return JsonResponse({"error": "POST + AJAX required"}, status=400)

    url = request.POST.get("url", "").strip()
    if not url:
        return JsonResponse({"error": "url required"}, status=400)
    from urllib.parse import urlparse as _up
    _p = _up(url)
    if _p.scheme not in ("http", "https") or not _p.netloc:
        return JsonResponse({"error": "absolute http(s) URL required"}, status=400)
    if not _p.path.strip("/"):
        return JsonResponse(
            {"error": "Paste a sample item page (e.g. one product/job/article), not the site homepage."},
            status=400,
        )

    # Step 1: browse the site via /navigate (cloak, direct, ~5-13s).
    bs_url = getattr(settings, "BROWSER_SERVICE_URL", "http://browser_service:8001")
    try:
        nav_resp = httpx.post(
            f"{bs_url}/navigate",
            json={
                "url": url,
                "stealth": "cloak",
                "return_what": "all",
                "wait_until": "domcontentloaded",
                "timeout": 25,
            },
            timeout=30,
        )
    except httpx.ConnectError:
        return JsonResponse({"fields": [], "json_schema": None, "source": "browser",
                              "error": "browser_unreachable",
                              "message": "Couldn't reach the browser service. Add fields manually."})
    except httpx.ReadTimeout:
        return JsonResponse({"fields": [], "json_schema": None, "source": "browser",
                              "error": "navigate_timeout",
                              "message": "The page took too long to load. Try a different URL."})
    except httpx.HTTPError as exc:
        return JsonResponse({"fields": [], "json_schema": None, "source": "browser",
                              "error": "browser_error",
                              "message": f"Browser request failed: {str(exc)[:160]}"})

    if nav_resp.status_code != 200:
        return JsonResponse({"fields": [], "json_schema": None, "source": "browser",
                              "error": "browser_error",
                              "message": f"Browser service error (HTTP {nav_resp.status_code})."})

    nav_data = nav_resp.json()
    if nav_data.get("blocked") or nav_data.get("blocked_type"):
        bt = nav_data.get("blocked_type") or "antibot"
        return JsonResponse({"fields": [], "json_schema": None, "source": "browser",
                              "error": "",
                              "message": f"This page is blocking automated access ({bt}). "
                                         "Add fields manually, or try another URL."})

    html = nav_data.get("html") or ""
    title = nav_data.get("title") or ""
    if len(html) < 500:
        return JsonResponse({"fields": [], "json_schema": None, "source": "browser",
                              "error": "",
                              "message": "The page loaded but had no usable content. Try another URL."})

    # Step 2 + 3: extract content + LLM discovery (with JSON-LD fallback).
    try:
        from src.field_discovery import discover_fields_from_html
        result = discover_fields_from_html(url=url, html=html, title=title, llm_timeout=20)
    except Exception as exc:
        logger.warning("intake discover_fields failed for %s: %s", url[:120], exc)
        return JsonResponse({"fields": [], "json_schema": None, "source": "llm",
                              "error": "discovery_failed",
                              "message": f"Couldn't analyze the page: {str(exc)[:160]}"})

    if not result["fields"]:
        return JsonResponse({"fields": [], "json_schema": None, "source": result["source"],
                              "error": "",
                              "message": "No extractable fields found automatically. Add them manually."})

    logger.info("intake discover_fields: %s → %d fields (source=%s, ct=%s)",
                url[:60], len(result["fields"]), result["source"], result.get("content_type", ""))
    return JsonResponse({
        "fields": result["fields"],
        "json_schema": result["json_schema"],
        "source": result["source"],
        "content_type": result.get("content_type", ""),
        "error": "",
        "message": "",
    })


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

    # Defensive re-validation + persistence: if a raw schema was posted, validate
    # it (gate) AND keep it so the dashboard deep-link / re-run can re-display it.
    # The derived field names (target_fields) are what the pipeline enforces; the
    # raw schema_text is advisory (re-display only).
    schema_raw = request.POST.get("schema_text", "")
    schema_upload = request.FILES.get("schema_file")
    schema_text = ""
    if schema_raw or schema_upload:
        schema_text = schema_upload.read().decode("utf-8", errors="replace") if schema_upload else schema_raw
        result = validate_user_schema(schema_text)
        if not result.valid:
            return JsonResponse(
                {"error": "Schema invalid", "issues": [i.message for i in result.errors]},
                status=422,
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
        schema_text=schema_text,
        full_extraction=False,  # "sample" mode
        skip_approvals=True,  # intake jobs run unattended — skip approval gates
        # Opt-in only — the intake checkbox posts '1' when ticked. [dagster-opt-in]
        dagster_enabled=request.POST.get("dagster_enabled") == "1",
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
                import src.artifacts as artifacts
                artifacts.write_json(
                    artifacts.scrapers_key(slug, "input_urls.json"),
                    {"urls": urls},
                )
            except Exception as exc:
                logger.warning(
                    "intake: could not persist list URLs for %s: %s",
                    url[:80],
                    exc,
                )

    from .tasks import dispatch_scrape_job

    # [wave-15 1.0] keystone: stamp BEFORE publish (see dispatch_scrape_job).
    dispatch_scrape_job(job.id, rescrape=False)

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
    recent = list(_user_jobs(request).order_by("-created_at")[:100])
    saved_ids = set(
        _user_jobs(request).filter(is_saved=True).values_list("id", flat=True)
    )
    # merge any saved jobs that fell outside the recent-100 window
    extra = [j for j in _user_jobs(request).filter(is_saved=True).order_by("-created_at")
             if j.id not in {j.id for j in recent}]
    jobs = recent + extra[:50]

    def _dur(j):
        if j.started_at and j.completed_at:
            return (j.completed_at - j.started_at).total_seconds()
        return None

    is_admin = request.user.is_superuser
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
            # Owner info: only meaningful when an admin is viewing everyone's jobs.
            "owner_username": j.user.username if (is_admin and j.user_id) else None,
            "owner_email": j.user.email if (is_admin and j.user_id) else None,
        }
        for j in jobs
    ]
    return JsonResponse({"jobs": data})



# ── Learnt Skills admin (/learnt-skills) ────────────────────────────────────
# View/edit the File Master's live skills: delete or edit a learned section.
# Baseline content (above the first `## Learned:`) is read-only here — it is
# managed in git (the seed). All writes go through src.skills_store (flock'd).

import re as _re_learned_split

_LEARNED_SPLIT = _re_learned_split.compile(r"(?=^## Learned: )", _re_learned_split.MULTILINE)
_LEARNED_META = _re_learned_split.compile(
    r"^## Learned: (.+?)\n(.*?)(?:\n\n|\n)(.*)$", _re_learned_split.DOTALL
)


def _parse_skill_for_ui(name: str) -> dict:
    """Split a skill into baseline + learned sections for the template."""
    from src.skills_store import read_skill, read_skill_description

    row = {
        "name": name,
        "description": "",
        "learned": [],
        "baseline": "",
        "baseline_len": 0,
        "learned_count": 0,
        "error": "",
    }
    try:
        text = read_skill(name) or ""
    except Exception as exc:
        row["error"] = f"read failed: {exc}"
        return row
    row["description"] = read_skill_description(name)
    parts = [p for p in _LEARNED_SPLIT.split(text) if p.strip()]
    baseline_parts = [p for p in parts if not p.startswith("## Learned:")]
    learned_parts = [p for p in parts if p.startswith("## Learned:")]
    row["baseline"] = baseline_parts[0].strip() if baseline_parts else ""
    row["baseline_len"] = len(row["baseline"])
    for lp in learned_parts:
        m = _LEARNED_META.match(lp.strip())
        if m:
            title, meta, body = m.group(1).strip(), m.group(2).strip(), m.group(3)
        else:
            title, meta, body = lp.strip().splitlines()[0].lstrip("# ").strip(), "", lp
        row["learned"].append({"title": title, "meta": meta, "body": body.strip()})
    row["learned_count"] = len(row["learned"])
    return row


@login_required
def learnt_skills(request):
    """List skills with their learned sections (GET ?skill=&title= enters edit)."""
    from src.skills_store import list_skills, read_skill

    names = list_skills()
    skills = [_parse_skill_for_ui(n) for n in names]
    context = {
        "skills": skills,
        "selected": request.GET.get("skill", ""),
        "editing": bool(request.GET.get("title")),
        "edit_title": request.GET.get("title", ""),
        "save_msg": "",
    }
    if context["editing"] and context["selected"]:
        # Pre-fill the edit area with the section's raw text (title+meta+body).
        text = read_skill(context["selected"]) or ""
        parts = [p for p in _LEARNED_SPLIT.split(text) if p.startswith("## Learned:")]
        want = context["edit_title"]
        for p in parts:
            first = p.strip().splitlines()[0]
            if first.lstrip("# ").strip().lstrip("Learned: ").strip() == want:
                context["edit_body"] = p.strip()
                break
        else:
            context["edit_body"] = ""
    return render(request, "scraper/learnt_skills.html", context)


@login_required
def learnt_skill_update(request, skill_name):
    """Save an edited learned section (full-section replace, one section)."""
    if request.method != "POST":
        return redirect("learnt_skills")
    from src.skills_store import replace_learned_section
    from django.contrib import messages

    title = request.POST.get("title", "").strip()
    body = (request.POST.get("body") or "").strip()
    # Lock-safe, title-scoped replace: reads CURRENT text under the flock and
    # swaps only this section — a concurrent agent append to another section
    # survives (the old full-text replace could silently erase it).
    result = replace_learned_section(
        skill_name, title, body, actor=f"ui:{request.user.username}"
    )
    if result.get("ok"):
        messages.success(request, f"Updated '{title}' in {skill_name}.")
    else:
        messages.error(request, result.get("error", "update failed"))
    return redirect("learnt_skills")


@login_required
def learnt_skill_delete(request, skill_name):
    """Delete ONE learned section by title."""
    if request.method != "POST":
        return redirect("learnt_skills")
    from src.skills_store import delete_learned_section
    from django.contrib import messages

    title = request.POST.get("title", "").strip()
    result = delete_learned_section(skill_name, title, actor=f"ui:{request.user.username}")
    if result.get("ok"):
        messages.success(request, f"Deleted '{title}' from {skill_name}.")
    else:
        messages.error(request, result.get("error", "delete failed"))
    return redirect("learnt_skills")


def _yaml_safe_load(text: str):
    import yaml

    return yaml.safe_load(text)


# ── Partner API specification documents (login-gated) ────────────────────────

_SPECS_DIR = os.path.join(settings.PROJECT_ROOT, "docs", "specs")
_SPEC_FILES = {"sync": "sync_api.yaml", "async": "async_api.yaml"}


# Vendored renderer assets (docs/assets/, see its README): whitelisted
# name -> MIME. Served same-origin so the docs pages load no third-party
# scripts, and the AsyncAPI component's shadow-root
# @import 'assets/default.min.css' — relative to the PAGE, it ignores the
# cssImport attribute — resolves here instead of to an HTML 404.
_DOC_ASSETS = {
    "swagger-ui.css": "text/css; charset=utf-8",
    "swagger-ui-bundle.js": "application/javascript; charset=utf-8",
    "asyncapi-web-component.js": "application/javascript; charset=utf-8",
    "default.min.css": "text/css; charset=utf-8",
}


def _serve_doc_asset(request, filename: str):
    """Static renderer asset, login-gated with the docs pages. 404 on any
    name outside the whitelist (no directory traversal surface)."""
    mime = _DOC_ASSETS.get(os.path.basename(filename))
    if not mime:
        raise Http404("unknown asset")
    path = os.path.join(settings.PROJECT_ROOT, "docs", "assets", os.path.basename(filename))
    if not os.path.isfile(path):
        raise Http404(f"asset missing: {filename}")
    with open(path, "r", encoding="utf-8") as fh:
        return HttpResponse(fh.read(), content_type=mime)


def _serve_spec(request, which: str):
    """Serve an API spec. Login required (enforced by the callers) — these
    documents are for the internal team + partners under NDA, not public.

    Default renders the full interactive docs page (Swagger UI for the sync
    spec, the AsyncAPI web component for the event spec) — standalone
    documents with NO system styling. ?view=raw = plain-YAML fallback,
    ?format=yaml = download for tooling, ?format=json = machine-readable
    copy (what the on-page renderers fetch, same-origin with the session).
    """
    fname = _SPEC_FILES.get(which)
    if not fname:
        raise Http404("unknown spec")
    path = os.path.join(_SPECS_DIR, fname)
    if not os.path.isfile(path):
        raise Http404(f"spec file missing: {fname}")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    fmt = request.GET.get("format", "")

    if fmt == "yaml":
        resp = HttpResponse(content, content_type="application/yaml; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp

    spec_obj = _yaml_safe_load(content)
    if which == "sync" and isinstance(spec_obj, dict):
        # Swagger UI refuses to render endpoints when jsonSchemaDialect is
        # set to a non-default value (verified: warning + 0 operation
        # blocks). The field is optional in OpenAPI 3.1 — strip it from the
        # RENDERED copies only; the raw YAML download keeps it.
        spec_obj.pop("jsonSchemaDialect", None)

    if fmt == "json":
        return JsonResponse(spec_obj)

    sibling = "async" if which == "sync" else "sync"
    ctx = {
        "spec_name": "Sync API (OpenAPI 3.1)" if which == "sync" else "Event API (AsyncAPI 3.0)",
        "raw_url": f"/docs/{which}_api?format=yaml",
        "json_url": f"/docs/{which}_api?format=json",
        "sibling_url": f"/docs/{sibling}_api",
        "line_count": content.count("\n") + 1,
    }
    nav = (
        f"<div style='display:flex;gap:18px;align-items:center;padding:10px 18px;"
        f"background:#0B1120;border-bottom:1px solid #1E293B;font-family:system-ui,sans-serif;'>"
        f"<strong style='color:#F1F5F9;font-size:15px;'>{ctx['spec_name']}</strong>"
        f"<span style='color:#64748B;font-size:12px;'>{ctx['line_count']} lines · login-gated</span>"
        f"<span style='flex:1'></span>"
        f"<a href='{ctx['raw_url']}' style='color:#22D3EE;font-size:13px;text-decoration:none;'>raw YAML</a>"
        f"<a href='?view=raw' style='color:#22D3EE;font-size:13px;text-decoration:none;'>plain view</a>"
        f"<a href='{ctx['sibling_url']}' style='color:#22D3EE;font-size:13px;text-decoration:none;'>sibling spec</a>"
        f"</div>"
    )
    if request.GET.get("view") == "raw":
        # ?view=raw — plain readable YAML (no CDN dependency), still standalone
        page = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>" + ctx["spec_name"] + "</title></head><body style='margin:0;background:#0B1120;'>"
            + nav +
            "<pre style='color:#CBD5E1;padding:18px;font-size:12.5px;line-height:1.55;"
            "white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,monospace;'>"
            + content.replace("&", "&amp;").replace("<", "&lt;") +
            "</pre></body></html>"
        )
        return HttpResponse(page)

    # Full renderer, via CDN. STANDALONE documents — no base.html, no Tailwind,
    # no system styling (the system's CDN Tailwind preflight + dark CSS vars
    # fought both renderers; these pages are deliberately independent).
    if which == "sync":
        # Load by URL (same-origin ?format=json). The inline `spec:`
        # constructor option drops operations in swagger-ui-dist v5 builds,
        # and calling updateSpec right after construction races the store
        # init ("No API definition provided") — both verified by bisection.
        # URL fetch is the path every real deployment uses.
        body = (
            "<link rel='stylesheet' href='/docs/assets/swagger-ui.css'>"
            "<div id='swagger-ui'></div>"
            "<script src='/docs/assets/swagger-ui-bundle.js'></script>\n"
            "<script>\n"
            "window.ui = SwaggerUIBundle({\n"
            f"  url: '{ctx['json_url']}',\n"
            "  dom_id: '#swagger-ui', deepLinking: true, docExpansion: 'list'\n"
            "});\n"
            "</script>"
        )
    else:
        import json as _json

        schema_attr = (
            _json.dumps(spec_obj).replace("&", "&amp;").replace("'", "&#39;")
        )
        # The web component boots React inside a shadow root; cssImport is how
        # it pulls its stylesheet. Deliberately NO `configuration` attribute —
        # defaults render sidebar + content, and a malformed configuration
        # value silently blanked the component in earlier testing.
        # v3.x of the web component — the 1.4.x line bundles an AsyncAPI
        # parser that predates 3.0 documents (registers the element, renders
        # a bare shadow root, draws nothing). Assets vendored under
        # /docs/assets/ (same-origin): the shadow root hardcodes
        # @import 'assets/default.min.css' relative to the page, so the CSS
        # must be reachable at /docs/assets/default.min.css.
        # font-family on the HOST element: inheritable properties cross
        # the shadow boundary, but the stylesheet's own body/html font
        # selectors cannot match inside it — without this, un-inherited
        # elements fall back to the browser serif default.
        body = (
            "<style>asyncapi-component{font-family:system-ui,-apple-system,"
            "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}</style>\n"
            "<script src='/docs/assets/asyncapi-web-component.js'></script>\n"
            "<asyncapi-component schema='" + schema_attr + "'>"
            "</asyncapi-component>"
        )
    page = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>" + ctx["spec_name"] + "</title>"
        "<link rel='icon' href='data:,'>"  # suppress favicon 404 noise
        "<style>body{margin:0;background:#fff;}</style></head><body>"
        + nav + body + "</body></html>"
    )
    return HttpResponse(page)


@login_required
def docs_sync_api(request):
    """Partner sync API spec (OpenAPI 3.1). Logged-in users only."""
    return _serve_spec(request, "sync")


@login_required
def docs_async_api(request):
    """Partner event/callback spec (AsyncAPI 3.0). Logged-in users only."""
    return _serve_spec(request, "async")
