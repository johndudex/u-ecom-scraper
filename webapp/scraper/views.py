import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlparse

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
from django.utils.decorators import method_decorator

from .forms import SiteForm
from .models import Approval, ProbeCache, ScrapeJob, SessionLog, Site

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


@login_required
def home(request):
    if request.method == "POST":
        form_data = request.POST
        url = form_data.get("url", "").strip()
        product_url = form_data.get("product_url", "").strip()
        currency = form_data.get("currency", "").strip().upper()
        full_extraction = form_data.get("full_extraction") == "on"
        rescrape = form_data.get("rescrape") == "on"

        context = {
            "form_url": url,
            "form_product_url": product_url,
            "form_currency": currency,
            "recent_jobs": ScrapeJob.objects.all()[:10],
        }

        if not url:
            context["error"] = "URL is required"
            return render(request, "scraper/home.html", context)

        existing = ScrapeJob.objects.filter(
            url=url, status__in=[ScrapeJob.STATUS_PENDING, ScrapeJob.STATUS_RUNNING]
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
        )

        from .tasks import run_scrape_task

        task = run_scrape_task.delay(job.id, rescrape=rescrape)
        job.celery_task_id = task.id
        job.save(update_fields=["celery_task_id"])
        return redirect("job_detail", job_id=job.id)

    recent_jobs = ScrapeJob.objects.all()[:10]
    return render(request, "scraper/home.html", {"recent_jobs": recent_jobs})


@login_required
def job_list(request):
    jobs = ScrapeJob.objects.prefetch_related("steps", "approvals")[:]
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
    job = get_object_or_404(ScrapeJob, pk=job_id)
    steps = job.steps.all()
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
    sample_output = ""
    scraper_slug = ""

    output_files = []
    slug_candidates = []
    if job.site_folder:
        slug_candidates.append(job.site_folder)
    if job.site_name:
        name_slug = job.name.lower().replace(" ", "-").replace(".", "-")
        for char in name_slug:
            if not char.isalnum() and char != "-":
                name_slug = name_slug.replace(char, "-")
        slug_candidates.append(name_slug)
    for slug in slug_candidates:
        scraper_path = f"/app/scrapers/{slug}/scraper.py"
        if os.path.exists(scraper_path):
            try:
                with open(scraper_path, "r") as f:
                    scraper_code_display = f.read()
                has_scraper_code = True
                scraper_slug = slug
                break
            except Exception:
                pass

    if scraper_slug:
        site_dir = os.path.join(settings.PROJECT_ROOT, scraper_slug)
        if os.path.isdir(site_dir):
            output_files = sorted(
                [
                    f for f in os.listdir(site_dir)
                    if f.startswith("output_") and f.endswith(".json")
                ],
                reverse=True,
            )

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
    job = get_object_or_404(ScrapeJob, pk=job_id)
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


@login_required
def scraper_code(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id)

    slug_candidates = []
    if job.site_folder:
        slug_candidates.append(job.site_folder)
    if job.site_name:
        name_slug = job.site_name.lower().replace(" ", "-").replace(".", "-")
        for char in name_slug:
            if not char.isalnum() and char != "-":
                name_slug = name_slug.replace(char, "-")
        slug_candidates.append(name_slug)
    for slug in slug_candidates:
        scraper_path = f"/app/scrapers/{slug}/scraper.py"
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
def job_restart(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id)
    if job.status in [
        ScrapeJob.STATUS_COMPLETED,
        ScrapeJob.STATUS_FAILED,
        ScrapeJob.STATUS_CANCELLED,
    ]:
        new_job = ScrapeJob.objects.create(
            url=job.url,
            product_url=job.product_url,
            currency=job.currency,
            full_extraction=job.full_extraction,
        )

        from .tasks import run_scrape_task

        task = run_scrape_task.delay(new_job.id)
        new_job.celery_task_id = task.id
        new_job.save(update_fields=["celery_task_id"])
        return redirect("job_detail", job_id=new_job.id)
    return redirect("job_detail", job_id=job.id)


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
    approval = get_object_or_404(Approval, pk=approval_id, job_id=job_id)
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
        Approval.objects.filter(status=Approval.STATUS_PENDING)
        .select_related("job")
        .order_by("-created_at")
    )
    return render(request, "scraper/approval_list.html", {"approvals": approvals})


@login_required
def approval_count(request):
    count = Approval.objects.filter(status=Approval.STATUS_PENDING).count()
    return JsonResponse({"count": count})


@login_required
def approval_detail(request, approval_id):
    approval = get_object_or_404(Approval, pk=approval_id)
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
    job = get_object_or_404(ScrapeJob, pk=job_id)
    scraper_path = None
    if job.site_folder:
        candidate = os.path.join("scrapers", job.site_folder, "scraper.py")
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


@login_required
def job_api(request, job_id):
    job = get_object_or_404(ScrapeJob, pk=job_id)
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
                for s in job.steps.all()
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
        }
    )


@login_required
def job_logs_api(request, job_id):
    get_object_or_404(ScrapeJob, pk=job_id)
    job = get_object_or_404(ScrapeJob, pk=job_id)
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
    get_object_or_404(ScrapeJob, pk=job_id)
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
    job = get_object_or_404(ScrapeJob, pk=job_id)

    if request.method == "POST":
        data = json.loads(request.body)
        response = data.get("response", {})
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
    job = get_object_or_404(ScrapeJob, pk=job_id)
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
    job = get_object_or_404(ScrapeJob, pk=job_id)
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
    agent_order = [
        "site-analyzer",
        "product-analyzer",
        "code-writer",
        "code-tester",
        "cleanup",
        "skill-learner",
    ]
    for agent_name in agent_order:
        if agent_name not in agents:
            continue
        agent_data = agents[agent_name]
        summary_md = f"# {agent_name.replace('-', ' ').title()}\n\n"
        for msg in agent_data["assistant_msgs"]:
            summary_md += f"{msg}\n\n"
        summary_md += f"---\n**Total tool calls:** {sum(1 for lg in agent_data['logs'] if lg['role'] == 'Tool')}\n"
        summaries.append({"agent": agent_name, "summary": summary_md})

    for agent_name, agent_data in agents.items():
        if agent_name not in [s["agent"] for s in summaries]:
            summaries.append(
                {
                    "agent": agent_name,
                    "summary": f"# {agent_name}\n\n(No summary available)",
                }
            )

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
    jobs = ScrapeJob.objects.filter(url__iexact=site.url)
    scraper_code = ""
    if site.default_scraper_path and os.path.isfile(site.default_scraper_path):
        try:
            with open(site.default_scraper_path, "r", encoding="utf-8") as f:
                scraper_code = f.read()
        except Exception:
            pass

    output_files = []
    if site.slug:
        scrapers_dir = Path(settings.PROJECT_ROOT) / "scrapers" / site.slug
        if scrapers_dir.is_dir():
            for f in sorted(scrapers_dir.iterdir(), reverse=True):
                if f.name.startswith("output_") and f.name.endswith(".json") and f.is_file():
                    try:
                        size = f.stat().st_size
                        output_files.append({"name": f.name, "size": size, "path": str(f)})
                    except Exception:
                        pass

    return render(
        request,
        "scraper/site_detail.html",
        {
            "site": site,
            "jobs": jobs,
            "scraper_code": scraper_code,
            "output_files": output_files,
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
        url=site.url, status__in=[ScrapeJob.STATUS_PENDING, ScrapeJob.STATUS_RUNNING]
    ).first()
    if existing:
        return redirect("job_detail", job_id=existing.id)

    job = ScrapeJob.objects.create(
        url=site.url,
        product_url=site.sample_url,
        currency=site.currency,
        full_extraction=full_extraction,
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
            settings, "BROWSER_SERVICE_URL", "http://browser-service:8001"
        )
        try:
            resp = httpx.post(
                f"{service_url}/scrape",
                json={
                    "scraper_path": scraper_path,
                    "args": [],
                    "timeout": 3600,
                },
                timeout=3660,
            )
            resp.raise_for_status()
            result = resp.json()
            output_file = result.get("output_file", "")
            product_count = result.get("product_count", 0)
        except Exception as e:
            logger.error("site_rerun: browser-service failed: %s", e)
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
                    product_count = len(data.get("products", []))
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

    products = data.get("products", [])
    download_url = reverse("site_output_download", kwargs={"site_id": site.id, "filename": safe_name})
    return render(request, "scraper/output_view.html", {
        "site": site,
        "filename": safe_name,
        "json_content": pretty,
        "product_count": len(products),
        "download_url": download_url,
    })


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
