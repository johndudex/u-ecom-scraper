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
import subprocess
import time
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
        logger.warning("Job %d: skipping duplicate dispatch (status=%s)", job_id, job.status)
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

    # ── Stream graph events ─────────────────────────────────────────────
    try:
        service.stream_graph(graph, initial_state, config, job)
    except Exception as exc:
        from langgraph.errors import GraphInterrupt

        if isinstance(exc, GraphInterrupt):
            logger.info(
                "Job %d: graph interrupted, waiting for human input", job.id
            )
            job.status = ScrapeJob.STATUS_WAITING_APPROVAL
            job.save(update_fields=["status"])
            _publish_job_status(job.id, ScrapeJob.STATUS_WAITING_APPROVAL)
            return
        raise

    # ── Check if the graph ended at an interrupt (stream_events may
    #    exit without raising). ───────────────────────────────────────────
    if _graph_is_interrupted(graph, config):
        logger.info(
            "Job %d: graph paused at interrupt, waiting for approval", job.id
        )
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
        logger.warning("Job %d: skipping duplicate resume dispatch (status=%s)", job_id, job.status)
        return

    service = LangGraphService()
    graph = service.build_graph()
    config = service.get_config(job.id)

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
    job.site_folder = (
        f"scrapers/{site_slug}" if site_slug else job.site_folder
    )
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
                        prod for prod in products
                        if prod.get("title") and prod.get("status_code", 0) > 0
                    ]
                    job.product_count = len(successful)
                if site_block.get("name"):
                    job.site_name = site_block["name"]
                job.output_file = str(p)
                logger.info(
                    "Job %d: updated from output file — platform=%s, method=%s, products=%d",
                    job.id, job.platform, job.scraping_method, job.product_count,
                )
        except Exception as exc:
            logger.warning("Job %d: could not read output file for overrides: %s", job.id, exc)

    # ── Move analysis artifacts to scrapers folder (preserve for debugging) ──
    if site_slug:
        try:
            import shutil
            ws = Path(settings.PROJECT_ROOT) / "workspace" / site_slug
            analysis_dir = Path(settings.PROJECT_ROOT) / "scrapers" / site_slug / "analysis"
            if ws.is_dir():
                analysis_dir.mkdir(parents=True, exist_ok=True)
                for artifact in ["site_analysis.json", "product_analysis.json",
                                 "scraper_analysis.json", "test_report.json"]:
                    src = ws / artifact
                    if src.is_file():
                        shutil.copy2(src, analysis_dir / artifact)
                        logger.info("Job %d: preserved %s to analysis/", job.id, artifact)
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
                db_site.status = "complete" if job.status == ScrapeJob.STATUS_COMPLETED else "failed"
                db_site.last_scraped_at = timezone.now()
                if job.site_name:
                    db_site.name = job.site_name

                scraper_path = os.path.join(settings.PROJECT_ROOT, "scrapers", site_slug, "scraper.py")
                if os.path.isfile(scraper_path):
                    db_site.has_scraper = True
                    db_site.default_scraper_path = scraper_path

                db_site.save()
                logger.info(
                    "Job %d: updated Site (method=%s, products=%d, has_scraper=%s)",
                    job.id, job.scraping_method, job.product_count, db_site.has_scraper,
                )
        except Exception as exc:
            logger.warning("Job %d: Site update failed: %s", job.id, exc)

    # ── Close any running steps ─────────────────────────────────────────
    try:
        for step_obj in job.steps.filter(status=Step.STATUS_RUNNING):
            step_obj.status = Step.STATUS_DONE
            step_obj.completed_at = timezone.now()
            step_obj.save()
    except Exception as exc:
        logger.warning("Failed to close running steps for job %d: %s", job.id, exc)

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
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return len(data.get("products", []))
    except Exception:
        return 0
