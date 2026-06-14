"""Celery tasks for executing the LangGraph scrape pipeline.

The primary task ``run_scrape_task`` builds the compiled StateGraph, streams
events via ``LangGraphService.stream_graph``, and finalises the job.

A secondary task ``resume_scrape_task`` re-invokes the graph with a
``Command(resume=...)`` after a human approval is resolved.

Browser-based scraper execution is handled by browser-service via HTTP,
not by a separate Celery queue.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import ScrapeJob, Step
from .services import LangGraphService

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants — kept from the old tasks.py; still useful for Step population
# ═══════════════════════════════════════════════════════════════════════════

PHASE_MAP: dict[str, str] = {
    "accessibility_check": "Accessibility Check",
    "site_analysis": "Site Analysis",
    "product_analysis": "Product Analysis",
    "scraper_analysis": "Scraper Analysis",
    "code_generation": "Code Generation",
    "testing": "Testing Loop",
    "field_confirmation": "Field Confirmation",
    "execution": "Execution",
    "cleanup": "Cleanup",
    "skill_learning": "Skill Learning",
}

AGENT_PHASE_MAP: dict[str, str] = {
    "site-analyzer": "site_analysis",
    "product-analyzer": "product_analysis",
    "scraper-analyzer": "scraper_analysis",
    "code-writer": "code_generation",
    "code-tester": "testing",
    "cleanup": "cleanup",
    "skill-learner": "skill_learning",
}

# ═══════════════════════════════════════════════════════════════════════════
# Primary Celery task
# ═══════════════════════════════════════════════════════════════════════════


def _publish_job_status(job_id: int, status: str) -> None:
    try:
        LangGraphService._publish_redis(job_id, {"type": "status", "status": status})
    except Exception:
        pass


@shared_task(bind=True, max_retries=1)
def run_scrape_task(self, job_id: int, rescrape: bool = False) -> None:
    """Celery entry-point: execute the full scrape graph for *job_id*."""
    job = ScrapeJob.objects.get(pk=job_id)

    if job.status in (ScrapeJob.STATUS_RUNNING, ScrapeJob.STATUS_WAITING_APPROVAL):
        logger.warning(
            "Job %d: skipping duplicate dispatch (status=%s)", job_id, job.status
        )
        return

    try:
        _run_graph_job(job, rescrape=rescrape)
    except Exception as exc:
        logger.exception("Scrape job %d failed: %s", job_id, exc)
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = str(exc)[:2000]
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        _publish_job_status(job_id, ScrapeJob.STATUS_FAILED)


# ═══════════════════════════════════════════════════════════════════════════
# Graph execution core
# ═══════════════════════════════════════════════════════════════════════════


PIPELINE_PHASES = [
    "accessibility_check",
    "site_analysis",
    "product_analysis",
    "scraper_analysis",
    "code_generation",
    "testing",
    "field_confirmation",
    "execution",
    "cleanup",
    "skill_learning",
]


def _seed_pipeline_steps(job: ScrapeJob) -> None:
    for phase in PIPELINE_PHASES:
        Step.objects.get_or_create(job=job, phase=phase)


def _run_graph_job(job: ScrapeJob, rescrape: bool = False) -> None:
    """Build the graph, stream events, and handle interrupts."""
    _seed_pipeline_steps(job)
    service = LangGraphService()
    graph = service.build_graph()

    # ── Transition to RUNNING ──────────────────────────────────────────
    job.status = ScrapeJob.STATUS_RUNNING
    job.started_at = timezone.now()

    # Store thread id on the model if the field exists (added in Phase 10).
    thread_id = service.get_thread_id(job.id)
    job.graph_thread_id = thread_id
    job.save(update_fields=["status", "started_at", "graph_thread_id"])
    _publish_job_status(job.id, ScrapeJob.STATUS_RUNNING)

    config = service.get_config(job.id)
    initial_state = _build_initial_state(job)
    if rescrape:
        initial_state["rescrape"] = True

    # ── Attach RedisLogHandler for system log streaming ────────────────
    from .log_handler import RedisLogHandler

    syslog_handler = RedisLogHandler()
    syslog_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", datefmt="%H:%M:%S"
        )
    )
    RedisLogHandler.set_job_id(job.id)
    root_logger = logging.getLogger()
    _saved_root_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(syslog_handler)

    # ── Stream graph events ─────────────────────────────────────────────
    try:
        service.stream_graph(graph, initial_state, config, job)
    except Exception as exc:
        from langgraph.errors import GraphInterrupt

        if isinstance(exc, GraphInterrupt):
            logger.info("Job %d: graph interrupted, waiting for human input", job.id)
            job.status = ScrapeJob.STATUS_WAITING_APPROVAL
            job.save(update_fields=["status"])
            _publish_job_status(job.id, ScrapeJob.STATUS_WAITING_APPROVAL)
            return
        raise
    finally:
        RedisLogHandler.clear_job_id()
        root_logger.setLevel(_saved_root_level)
        root_logger.removeHandler(syslog_handler)
        syslog_handler.close()

    # ── Check if the graph ended at an interrupt (stream_events may
    #    exit without raising). ───────────────────────────────────────────
    if _graph_is_interrupted(graph, config):
        logger.info("Job %d: graph paused at interrupt, waiting for approval", job.id)
        job.status = ScrapeJob.STATUS_WAITING_APPROVAL
        job.save(update_fields=["status"])
        _publish_job_status(job.id, ScrapeJob.STATUS_WAITING_APPROVAL)
        return

    _finalize_job(job)


# ═══════════════════════════════════════════════════════════════════════════
# Resume task (human-in-the-loop)
# ═══════════════════════════════════════════════════════════════════════════


@shared_task
def resume_scrape_task(job_id: int, human_response: Any) -> None:
    """Resume a graph that was interrupted for human approval.

    *human_response* is the value to pass to ``Command(resume=...)``.  It
    typically mirrors the ``Approval.response_data`` that the user approved
    or a dict like ``{"choice": "Yes"}``.
    """
    job = ScrapeJob.objects.get(pk=job_id)

    if job.status == ScrapeJob.STATUS_RUNNING:
        logger.warning(
            "Job %d: skipping duplicate resume dispatch (status=%s)", job_id, job.status
        )
        return

    service = LangGraphService()
    graph = service.build_graph()
    config = service.get_config(job.id)

    # ── Attach RedisLogHandler for system log streaming ────────────────
    from .log_handler import RedisLogHandler

    syslog_handler = RedisLogHandler()
    syslog_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", datefmt="%H:%M:%S"
        )
    )
    RedisLogHandler.set_job_id(job.id)
    root_logger = logging.getLogger()
    _saved_root_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(syslog_handler)

    try:
        from langgraph.types import Command

        job.status = ScrapeJob.STATUS_RUNNING
        job.save(update_fields=["status"])
        _publish_job_status(job.id, ScrapeJob.STATUS_RUNNING)
        graph.invoke(Command(resume=human_response), config)
    except Exception as exc:
        from langgraph.errors import GraphInterrupt

        if isinstance(exc, GraphInterrupt):
            logger.info("Job %d: interrupted again after resume", job.id)
            LangGraphService._check_and_create_approval(graph, config, job)
            job.status = ScrapeJob.STATUS_WAITING_APPROVAL
            job.save(update_fields=["status"])
            _publish_job_status(job.id, ScrapeJob.STATUS_WAITING_APPROVAL)
            return

        logger.exception("Job %d resume failed: %s", job_id, exc)
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = str(exc)[:2000]
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        _publish_job_status(job.id, ScrapeJob.STATUS_FAILED)
        return
    finally:
        RedisLogHandler.clear_job_id()
        root_logger.setLevel(_saved_root_level)
        root_logger.removeHandler(syslog_handler)
        syslog_handler.close()

    # Check for post-resume interrupt (stream_events may not raise).
    if _graph_is_interrupted(graph, config):
        logger.info("Job %d: interrupted again after resume", job.id)
        LangGraphService._check_and_create_approval(graph, config, job)
        job.status = ScrapeJob.STATUS_WAITING_APPROVAL
        job.save(update_fields=["status"])
        _publish_job_status(job.id, ScrapeJob.STATUS_WAITING_APPROVAL)
        return

    _finalize_job(job)


# ═══════════════════════════════════════════════════════════════════════════
# State initialisation
# ═══════════════════════════════════════════════════════════════════════════


def _build_initial_state(job: ScrapeJob) -> dict[str, Any]:
    """Build the initial ``ScrapeState`` from a ``ScrapeJob`` instance.

    Every key in ``ScrapeState`` is provided so the graph starts with a
    fully-defined state.  Keys that are annotated with reducers
    (``messages``, ``agent_logs``) use empty containers that the reducers
    handle correctly.
    """
    site_input_urls: list[str] = []
    try:
        from scraper.models import Site

        db_site = Site.objects.filter(url=job.url.rstrip("/")).first()
        if db_site and db_site.input_urls:
            site_input_urls = list(db_site.input_urls)
    except Exception as exc:
        logger.warning("Could not load Site input_urls for %s: %s", job.url, exc)

    return {
        "job_id": job.id,
        "url": job.url,
        "product_url": job.product_url or "",
        "currency": job.currency or "",
        "sample_only": not job.full_extraction,
        "rescrape": False,
        "site_slug": _generate_slug(job.url),
        "site_name": "",
        "site_status": "new",
        "skip_site_analysis": False,
        "skip_product_analysis": False,
        "skip_code_generation": False,
        "current_phase": "",
        "phases_completed": [],
        "site_analysis_retries": 0,
        "product_analysis_retries": 0,
        "test_retry_count": 0,
        "reanalyze_count": 0,
        "execution_status": "",
        "output_file": "",
        "product_count": 0,
        "scraping_method": "",
        "platform": "",
        "fields_extracted": [],
        "input_urls": site_input_urls,
        "error_message": "",
        "messages": [],
        "agent_logs": [],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Job finalisation
# ═══════════════════════════════════════════════════════════════════════════


def _finalize_job(job: ScrapeJob) -> None:
    """Read the final graph checkpoint and persist results to the job.

    Extracts ``platform``, ``scraping_method``, ``product_count``,
    ``output_file``, ``site_name``, ``site_slug``, and ``error_message``
    from the graph state.  Closes any still-running Step rows and sets
    the job to COMPLETED or FAILED.

    Skipped entirely for captcha_blocked jobs (already finalized in
    check_accessibility).
    """
    job.refresh_from_db()
    if job.status == ScrapeJob.STATUS_CAPTCHA_BLOCKED:
        logger.info("Job %d: captcha_blocked, skipping _finalize_job", job.id)
        return
    service = LangGraphService()
    graph = service.build_graph()
    config = service.get_config(job.id)

    final_state: dict[str, Any] = {}
    try:
        snapshot = graph.get_state(config)
        final_state = snapshot.values  # type: ignore[assignment]
    except Exception as exc:
        logger.warning("Could not read final graph state for job %d: %s", job.id, exc)

    # ── Pull fields from graph state ────────────────────────────────────
    site_slug = final_state.get("site_slug", "")
    job.platform = final_state.get("platform", job.platform)
    job.scraping_method = final_state.get("scraping_method", job.scraping_method)
    job.product_count = final_state.get("product_count", job.product_count)
    job.output_file = final_state.get("output_file", job.output_file)
    job.site_name = final_state.get("site_name", job.site_name)
    job.site_folder = f"scrapers/{site_slug}" if site_slug else job.site_folder
    job.error_message = final_state.get("error_message", job.error_message)

    # ── Override from output file (ground truth from scraper) ───────────
    if job.output_file:
        try:
            import pathlib

            root = pathlib.Path(settings.PROJECT_ROOT)
            p = pathlib.Path(job.output_file)
            scrapers_p = root / "scrapers" / site_slug / p.name if site_slug else p
            if scrapers_p.is_file():
                p = scrapers_p
            elif not p.is_file():
                p = scrapers_p
            if p.is_file():
                with open(p, "r", encoding="utf-8") as fh:
                    out_data = json.load(fh)
                site_block = out_data.get("site", {})
                if site_block.get("platform"):
                    job.platform = site_block["platform"]
                if site_block.get("scraping_method") and not job.scraping_method:
                    job.scraping_method = site_block["scraping_method"]
                products = out_data.get("products", [])
                if products:
                    successful = [
                        prod
                        for prod in products
                        if prod.get("title") and prod.get("status_code", 0) > 0
                    ]
                    job.product_count = len(successful)
                if site_block.get("name"):
                    job.site_name = site_block["name"]
                job.output_file = str(p)
                logger.info(
                    "Job %d: updated from output file — platform=%s, method=%s, products=%d",
                    job.id,
                    job.platform,
                    job.scraping_method,
                    job.product_count,
                )
        except Exception as exc:
            logger.warning(
                "Job %d: could not read output file for overrides: %s", job.id, exc
            )

    # ── Move analysis artifacts to scrapers folder (preserve for debugging) ──
    if site_slug:
        try:
            import shutil

            ws = Path(settings.PROJECT_ROOT) / "workspace" / site_slug
            analysis_dir = (
                Path(settings.PROJECT_ROOT) / "scrapers" / site_slug / "analysis"
            )
            if ws.is_dir():
                analysis_dir.mkdir(parents=True, exist_ok=True)
                for artifact in [
                    "site_analysis.json",
                    "product_analysis.json",
                    "scraper_analysis.json",
                    "test_report.json",
                ]:
                    src = ws / artifact
                    if src.is_file():
                        shutil.copy2(src, analysis_dir / artifact)
                        logger.info(
                            "Job %d: preserved %s to analysis/", job.id, artifact
                        )
                shutil.rmtree(ws, ignore_errors=True)
                logger.info("Job %d: cleaned workspace/%s/", job.id, site_slug)
        except Exception as exc:
            logger.warning("Job %d: workspace cleanup failed: %s", job.id, exc)

    # ── Determine final status ──────────────────────────────────────────
    if job.status == ScrapeJob.STATUS_CAPTCHA_BLOCKED:
        pass
    elif job.error_message:
        job.status = ScrapeJob.STATUS_FAILED
    elif final_state.get("execution_status") == "FAILED":
        job.status = ScrapeJob.STATUS_FAILED
    else:
        job.status = ScrapeJob.STATUS_COMPLETED

    # ── Update Site model with ground truth ───────────────────────────
    if site_slug:
        try:
            from scraper.models import Site

            db_site = Site.objects.filter(url=job.url.rstrip("/")).first()
            if db_site:
                db_site.platform = job.platform or db_site.platform
                db_site.scraping_method = job.scraping_method or db_site.scraping_method
                db_site.product_count = job.product_count
                db_site.status = (
                    "complete" if job.status == ScrapeJob.STATUS_COMPLETED else "failed"
                )
                db_site.last_scraped_at = timezone.now()
                if job.site_name:
                    db_site.name = job.site_name

                scraper_path = os.path.join(
                    settings.PROJECT_ROOT, "scrapers", site_slug, "scraper.py"
                )
                if os.path.isfile(scraper_path):
                    db_site.has_scraper = True
                    db_site.default_scraper_path = scraper_path

                db_site.save()
                logger.info(
                    "Job %d: updated Site (method=%s, products=%d, has_scraper=%s)",
                    job.id,
                    job.scraping_method,
                    job.product_count,
                    db_site.has_scraper,
                )
        except Exception as exc:
            logger.warning("Job %d: Site update failed: %s", job.id, exc)

    # ── Close any running or pending steps (graph finished but some
    #    deterministic nodes like field_confirmation/execution never update
    #    their own step status). ────────────────────────────────────────────
    try:
        for step_obj in job.steps.filter(
            status__in=(Step.STATUS_RUNNING, Step.STATUS_PENDING)
        ):
            step_obj.status = Step.STATUS_DONE
            step_obj.completed_at = timezone.now()
            step_obj.save()
    except Exception as exc:
        logger.warning("Failed to close steps for job %d: %s", job.id, exc)

    job.completed_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "completed_at",
            "site_name",
            "platform",
            "scraping_method",
            "product_count",
            "output_file",
            "site_folder",
            "error_message",
        ]
    )
    _publish_job_status(job.id, job.status)
    logger.info(
        "Job %d: finalised with status=%s, products=%d, platform=%s",
        job.id,
        job.status,
        job.product_count,
        job.platform,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _generate_slug(url: str) -> str:
    """Derive a filesystem-safe slug from a URL's hostname."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    # Strip ``www.`` prefix and port number.
    domain = domain.replace("www.", "").split(":")[0]
    slug = ""
    for ch in domain:
        if ch.isalnum():
            slug += ch
        elif ch in (".", "-"):
            slug += "-"
        else:
            slug += "-"
    return slug.strip("-")


def _graph_is_interrupted(graph: Any, config: dict[str, Any]) -> bool:
    """Check whether the compiled graph is paused at an interrupt."""
    try:
        snapshot = graph.get_state(config)
        for task in getattr(snapshot, "tasks", []):
            if getattr(task, "interrupts", None):
                return True
    except Exception as exc:
        logger.debug("Could not check interrupt state: %s", exc)
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Stuck-job watchdog
# ═══════════════════════════════════════════════════════════════════════════

STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES = 15


@shared_task
def cleanup_stuck_jobs() -> None:
    """Detect and fail jobs whose worker has crashed (no recent activity).

    A healthy running job continuously produces SessionLog entries (tool
    calls, LLM responses).  If there are no new entries for longer than
    ``STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES``, the worker almost certainly
    crashed (OOM, segfault, etc.) and the job must be manually marked
    as failed — otherwise it stays RUNNING forever.

    Jobs in WAITING_APPROVAL are untouched — they are genuinely waiting
    for human input.
    """
    from scraper.models import SessionLog

    threshold = timezone.now() - timezone.timedelta(minutes=STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES)
    stuck_jobs = ScrapeJob.objects.filter(
        status=ScrapeJob.STATUS_RUNNING,
    )

    if not stuck_jobs.exists():
        return

    failed = 0
    for job in stuck_jobs:
        latest_activity_qs = SessionLog.objects.filter(job=job).order_by("-created_at")
        if latest_activity_qs.exists():
            last_activity = latest_activity_qs.first().created_at
        else:
            last_activity = job.started_at

        if last_activity >= threshold:
            continue

        idle_minutes = int((timezone.now() - last_activity).total_seconds() / 60)
        error_msg = f"Worker process crashed (no activity for {idle_minutes} min). Likely OOM killed."

        logger.error(
            "Stuck job %d: no activity for %d min (last: %s), marking as failed",
            job.id,
            idle_minutes,
            last_activity.isoformat(timespec="seconds"),
        )
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = error_msg
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])

        Step.objects.filter(
            job=job, status__in=(Step.STATUS_RUNNING, Step.STATUS_PENDING)
        ).update(
            status=Step.STATUS_FAILED,
            completed_at=timezone.now(),
        )

        _publish_job_status(job.id, ScrapeJob.STATUS_FAILED)
        failed += 1

    if failed:
        logger.warning("Stuck-job watchdog: marked %d job(s) as failed", failed)


# ═══════════════════════════════════════════════════════════════════════════
# Periodic scheduler
# ═══════════════════════════════════════════════════════════════════════════

AUTO_APPROVE_MINUTES = 10


def _do_schedule_next_site() -> dict:
    """Core scheduling logic — pick next site and queue a scrape job.

    Returns a dict describing what happened, suitable for logging or
    rendering in the UI::

        {"action": "queued", "site": "<slug>", "site_url": "<url>",
         "job_id": 42, "url_count": 15}
        {"action": "skipped", "reason": "..."}
        {"action": "idle", "reason": "..."}
    """
    from scraper.models import Site

    _auto_approve_stale_jobs()

    active_statuses = {ScrapeJob.STATUS_RUNNING, ScrapeJob.STATUS_PENDING, ScrapeJob.STATUS_WAITING_APPROVAL}
    active_count = ScrapeJob.objects.filter(status__in=active_statuses).count()
    if active_count:
        return {
            "action": "skipped",
            "reason": f"{active_count} active job(s) (RUNNING/PENDING/WAITING_APPROVAL)",
        }

    new_site = (
        Site.objects.filter(status="new")
        .exclude(input_urls=[])
        .order_by("created_at")
        .first()
    )

    if new_site is None:
        failed_site = (
            Site.objects.filter(status="failed")
            .exclude(input_urls=[])
            .order_by("updated_at")
            .first()
        )
        if failed_site is None:
            return {
                "action": "idle",
                "reason": "no new or failed sites with input_urls",
            }
        new_site = failed_site

    slug = new_site.slug or _generate_slug(new_site.url)
    scrapers_dir = os.path.join(settings.PROJECT_ROOT, "scrapers", slug)
    os.makedirs(scrapers_dir, exist_ok=True)
    input_urls_path = os.path.join(scrapers_dir, "input_urls.json")
    if new_site.input_urls:
        with open(input_urls_path, "w", encoding="utf-8") as f:
            json.dump({"urls": new_site.input_urls}, f, indent=2, ensure_ascii=False)

    job = ScrapeJob.objects.create(
        url=new_site.url,
        product_url=new_site.sample_url or "",
        currency=new_site.currency or "",
        full_extraction=True,
        auto_queued=True,
    )

    new_site.status = "in_progress"
    new_site.save(update_fields=["status"])

    celery_task = run_scrape_task.delay(job.id, rescrape=False)
    job.celery_task_id = celery_task.id
    job.save(update_fields=["celery_task_id"])

    return {
        "action": "queued",
        "site": slug,
        "site_url": new_site.url,
        "job_id": job.id,
        "url_count": len(new_site.input_urls),
    }


@shared_task
def schedule_next_site() -> None:
    """Periodic beat task — pick next site and queue a scrape job."""
    result = _do_schedule_next_site()
    action = result.get("action")
    if action == "queued":
        logger.info(
            "Scheduler: queued %s (%d urls) → job #%d",
            result["site_url"],
            result["url_count"],
            result["job_id"],
        )
    elif action == "skipped":
        logger.info("Scheduler: skipped — %s", result["reason"])
    else:
        logger.info("Scheduler: idle — %s", result["reason"])


def _auto_approve_stale_jobs() -> None:
    """Auto-approve WAITING_APPROVAL jobs that were auto-queued.

    Only affects jobs where ``auto_queued=True`` and the approval has been
    pending for longer than ``AUTO_APPROVE_MINUTES`` minutes.
    """
    from scraper.models import Approval

    threshold = timezone.now() - timezone.timedelta(minutes=AUTO_APPROVE_MINUTES)
    stale_approvals = (
        Approval.objects.filter(
            status=Approval.STATUS_PENDING,
            job__status=ScrapeJob.STATUS_WAITING_APPROVAL,
            job__auto_queued=True,
            created_at__lt=threshold,
        )
        .select_related("job")
        .order_by("created_at")
    )

    approved = 0
    for approval in stale_approvals:
        job = approval.job
        logger.info(
            "Auto-approve: job #%d approval %s (waiting since %s)",
            job.id,
            approval.get_approval_type_display(),
            approval.created_at.isoformat(timespec="seconds"),
        )
        approval.status = Approval.STATUS_APPROVED
        approval.human_response = "auto-approved"
        approval.resolved_at = timezone.now()
        approval.save()

        resume_scrape_task.delay(job.id, "approve")
        approved += 1

    if approved:
        logger.info("Auto-approve: approved %d stale job(s)", approved)
