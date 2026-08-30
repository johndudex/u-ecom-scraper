"""Celery tasks for executing the LangGraph scrape pipeline.

The primary task ``run_scrape_task`` builds the compiled StateGraph, streams
events via ``LangGraphService.stream_graph``, and finalises the job.

A secondary task ``resume_scrape_task`` re-invokes the graph with a
``Command(resume=...)`` after a human approval is resolved.

Browser-based scraper execution is handled by browser_service via HTTP,
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

from .models import Approval, ScrapeJob, Step
from .services import LangGraphService

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants — kept from the old tasks.py; still useful for Step population
# ═══════════════════════════════════════════════════════════════════════════

PHASE_MAP: dict[str, str] = {
    "accessibility_check": "Accessibility Check",
    "site_analysis": "Site Analysis",
    "browser_traverse": "Browser Navigation",
    "navigation_skill_review": "Navigation Skill Review",
    "navigation_analysis": "Navigation Analysis",
    "content_analysis": "Content Analysis",
    "product_analysis": "Content Analysis",
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
    "browser-traverse": "browser_traverse",
    "nav-skill-review": "navigation_skill_review",
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


# Celery-level deadline for a scrape job. The dominant job-level failure is
# LLM-phase hangs (code_generation / product_analysis): the per-phase
# _AGENT_INVOKE_TIMEOUT abandons its daemon thread on timeout, but the thread
# KEEPS RUNNING, so only a process-level kill reclaims it (and the worker slot).
# soft_time_limit → SoftTimeLimitExceeded (task can catch + finalize the job);
# time_limit → Celery SIGKILLs the worker (reclaims the abandoned thread + any
# leaked resources). Defaults (2h / 2h6m) sit well above any legitimate run
# (aya's 26k-job extraction finishes <1h); tune via settings if a site needs more.
try:
    from django.conf import settings as _settings
    _RUN_TASK_SOFT_TIME_LIMIT = int(
        getattr(_settings, "CELERY_TASK_SOFT_TIME_LIMIT", 7200)
    )
    _RUN_TASK_TIME_LIMIT = int(getattr(_settings, "CELERY_TASK_TIME_LIMIT", 7560))
except Exception:
    _RUN_TASK_SOFT_TIME_LIMIT, _RUN_TASK_TIME_LIMIT = 7200, 7560


@shared_task(
    bind=True,
    max_retries=1,
    soft_time_limit=_RUN_TASK_SOFT_TIME_LIMIT,
    time_limit=_RUN_TASK_TIME_LIMIT,
)
def run_scrape_task(self, job_id: int, rescrape: bool = False, force_full: bool = False) -> None:
    """Celery entry-point: execute the full scrape graph for *job_id*.

    ``force_full`` (Full re-run button) implies rescrape semantics AND wipes
    the stale workspace + analysis archive so every phase regenerates.
    """
    job = ScrapeJob.objects.get(pk=job_id)

    # Record this task's id so external monitors (e.g. the regression monitor)
    # can revoke+terminate it on a per-phase timeout — otherwise a DB-only
    # "cancel" leaves the celery task running as a zombie, clogging the worker.
    _task_id = getattr(self.request, "id", "") or ""
    if _task_id and job.celery_task_id != _task_id:
        job.celery_task_id = _task_id
        job.save(update_fields=["celery_task_id"])

    if job.status in (ScrapeJob.STATUS_RUNNING, ScrapeJob.STATUS_WAITING_APPROVAL):
        logger.warning(
            "Job %d: skipping duplicate dispatch (status=%s)", job_id, job.status
        )
        return

    # Same-site serialization: if another job is already RUNNING for this URL,
    # requeue with a delay (workspace/{slug}/ is shared → concurrent writes
    # race, and _finalize_job's rmtree would destroy the sibling's artifacts).
    # The stuck-job watchdog (30 min) ensures the blocking job eventually ends.
    running_sibling = (
        ScrapeJob.objects
        .filter(url=job.url, status=ScrapeJob.STATUS_RUNNING)
        .exclude(pk=job.id)
        .exists()
    )
    if running_sibling:
        logger.info(
            "Job %d: another job is already running for %s — requeueing in 60s",
            job_id, job.url[:60],
        )
        raise self.retry(
            exc=RuntimeError("same-site job already running"),
            countdown=60,
            max_retries=None,
        )

    try:
        _run_graph_job(job, rescrape=rescrape, force_full=force_full)
    except Exception as exc:
        logger.exception("Scrape job %d failed: %s", job_id, exc)
        # F4: the original failure is often a dead DB connection (postgres
        # OOM restart). Saving on the same dead connection re-raises and the
        # task dies with the job stranded RUNNING (prod 284/332/333). Recycle
        # connections first, and guard every save so the row always finalizes.
        try:
            from django.db import close_old_connections

            close_old_connections()
        except Exception:
            pass
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = str(exc)[-4000:]  # tail: keep the exception, not the banner
        job.completed_at = timezone.now()
        try:
            job.save(update_fields=["status", "error_message", "completed_at"])
        except Exception:
            try:
                job = ScrapeJob.objects.get(pk=job_id)  # fresh instance+connection
                job.status = ScrapeJob.STATUS_FAILED
                job.error_message = str(exc)[-4000:]  # tail: keep the exception, not the banner
                job.completed_at = timezone.now()
                job.save(update_fields=["status", "error_message", "completed_at"])
            except Exception:
                logger.error(
                    "Job %d: could not persist FAILED status (job stranded)", job_id
                )
        try:
            _publish_job_status(job_id, ScrapeJob.STATUS_FAILED)
        except Exception:
            pass

        try:
            from scraper.models import Site

            db_site = Site.objects.filter(url=job.url.rstrip("/")).first()
            if db_site and db_site.status == "in_progress":
                db_site.status = "failed"
                db_site.save(update_fields=["status"])
                logger.info(
                    "Job %d: reset Site '%s' to failed", job_id, db_site.slug
                )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Graph execution core
# ═══════════════════════════════════════════════════════════════════════════


PIPELINE_PHASES = [
    "accessibility_check",
    "site_analysis",
    "browser_traverse",
    "product_analysis",
    "scraper_analysis",
    "code_generation",
    "code_review",
    "testing",
    "field_confirmation",
    "execution",
    "cleanup",
    "skill_learning",
    "dagster_converter",
    "store_job_listings",
]


def _seed_pipeline_steps(job: ScrapeJob) -> None:
    # dagster_converter is opt-in ([dagster-opt-in]): no Step row for jobs that
    # didn't ask for it, so the pipeline UI never shows a permanently-pending
    # phase that will never run. _finalize_job's close-lingering-steps loop is
    # step-status based, so a missing row is harmless there too.
    phases = PIPELINE_PHASES
    if not job.dagster_enabled:
        phases = [p for p in PIPELINE_PHASES if p != "dagster_converter"]
    for phase in phases:
        Step.objects.get_or_create(job=job, phase=phase)


def _emit_running_transition(job: ScrapeJob) -> None:
    """Partner event for the RUNNING transition (async_api.yaml JobInprogress).
    Best-effort inside the save transaction — emit's dedupe (inprogress)
    keeps resume paths exactly-once."""
    try:
        from scraper.events import emit as _emit

        _emit(job, "job.inprogress", {"internal_status": job.status}, dedupe_key="inprogress")
    except Exception as exc:
        logger.warning("job %s: inprogress emit: %s", job.id, exc)


def _run_graph_job(job: ScrapeJob, rescrape: bool = False, force_full: bool = False) -> None:
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
    _emit_running_transition(job)

    config = service.get_config(job.id)
    initial_state = _build_initial_state(job)
    if rescrape:
        initial_state["rescrape"] = True
    if force_full:
        # Implies rescrape (check_tracker's force_full arm lives inside the
        # rescrape gate) + the archive wipe / no-selective-reuse behavior.
        initial_state["rescrape"] = True
        initial_state["force_full"] = True

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


@shared_task(
    bind=True,
    soft_time_limit=_RUN_TASK_SOFT_TIME_LIMIT,
    time_limit=_RUN_TASK_TIME_LIMIT,
)
def resume_scrape_task(self, job_id: int, human_response: Any) -> None:
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
        logger.warning(
            "resume INVOKE job=%s recursion_limit=%s", job.id, config.get("recursion_limit")
        )
        # LangGraph v1: interrupts accumulate in the checkpoint across the
        # pipeline. When the user approves a specific gate, we must resume ONLY
        # that gate's interrupt — not all pending ones (stale interrupts from
        # earlier nodes would get the wrong response). The Approval carries the
        # interrupt_id; we resume as {interrupt_id: response} for a targeted
        # resume. If interrupt_id is missing (old approval), fall back to
        # resuming all pending with the same value (the dict approach).
        snapshot = graph.get_state(config)
        all_interrupt_ids = []
        for task in getattr(snapshot, "tasks", []):
            for intr in (getattr(task, "interrupts", None) or []):
                # LangGraph's Interrupt exposes `.id` (the resume key).
                # `interrupt_id` is a deprecated alias removed in V2 — avoid it.
                iid = getattr(intr, "id", None)
                if iid:
                    all_interrupt_ids.append(str(iid))

        # Find the interrupt_id of the gate the user actually approved. Pick
        # the most-recently-resolved approval whose interrupt_id is STILL
        # pending — this skips approvals whose interrupt was already consumed
        # by an earlier resume (stale) and handles a rapid double-approve
        # (two approvals before either resume fires): each resume targets the
        # one still left pending.
        target_iid = ""
        try:
            pending_set = set(all_interrupt_ids)
            approved_qs = Approval.objects.filter(
                job_id=job_id, status=Approval.STATUS_APPROVED
            ).exclude(interrupt_id="").order_by("-resolved_at")
            for a in approved_qs:
                if a.interrupt_id in pending_set:
                    target_iid = a.interrupt_id
                    break
        except Exception as exc:
            # Was a bare `pass` — silently turned every target-iid lookup
            # failure into a non-targeted resume, making stuck interrupts
            # impossible to diagnose. Log it so the fallback is visible.
            logger.warning(
                "Job %d: target interrupt_id lookup failed, falling back "
                "to non-targeted resume: %s",
                job_id,
                exc,
            )

        if target_iid and target_iid in all_interrupt_ids:
            # Targeted resume: only the approved interrupt.
            logger.info(
                "Job %d: targeted resume interrupt_id=%s (%d total pending)",
                job.id, target_iid, len(all_interrupt_ids),
            )
            resume_value = {target_iid: human_response}
        elif len(all_interrupt_ids) > 1:
            # Fallback: resume all pending with the same value.
            logger.info(
                "Job %d: no target interrupt_id — resuming all %d pending",
                job.id, len(all_interrupt_ids),
            )
            resume_value = {iid: human_response for iid in all_interrupt_ids}
        else:
            resume_value = human_response

        graph.invoke(Command(resume=resume_value), config)
    except Exception as exc:
        from langgraph.errors import GraphInterrupt, GraphRecursionError

        if isinstance(exc, GraphInterrupt):
            logger.info("Job %d: interrupted again after resume", job.id)
            LangGraphService._check_and_create_approval(graph, config, job)
            job.status = ScrapeJob.STATUS_WAITING_APPROVAL
            job.save(update_fields=["status"])
            _publish_job_status(job.id, ScrapeJob.STATUS_WAITING_APPROVAL)
            return

        if isinstance(exc, GraphRecursionError):
            logger.warning(
                "Job %d: GraphRecursionError after resume -> pausing for approval",
                job.id,
            )
            LangGraphService.create_recursion_approval(job, str(exc))
            _publish_job_status(job.id, ScrapeJob.STATUS_WAITING_APPROVAL)
            return

        logger.exception("Job %d resume failed: %s", job_id, exc)
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = str(exc)[-4000:]  # tail: keep the exception, not the banner
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
    output_schema: dict[str, Any] = {}
    site_type = ""
    try:
        from scraper.models import Site

        db_site = Site.objects.filter(url=job.url.rstrip("/")).first()
        if db_site:
            if db_site.input_urls:
                site_input_urls = list(db_site.input_urls)
            if db_site.output_schema:
                output_schema = db_site.output_schema
            if db_site.site_type:
                site_type = db_site.site_type
    except Exception as exc:
        logger.warning("Could not load Site for %s: %s", job.url, exc)

    page_type = job.page_type or "product"
    search_criteria = job.search_criteria or ""

    content_type_config: dict[str, Any] = {}
    try:
        from src.content_types import get_content_type, resolve_page_type

        # Derive canonical (content_type_name, input_mode) from page_type so
        # navigation/list page types route correctly even if job.input_mode
        # was not set when the job was created (backward compatibility).
        resolved_content_type, resolved_input_mode = resolve_page_type(page_type)
        # Prefer explicit job.input_mode if it matches a valid mode, else use
        # the mode derived from page_type.
        if job.input_mode and job.input_mode in (
            "url_list",
            "list_page",
            "navigation",
            "search_term",
        ):
            input_mode = job.input_mode
        else:
            input_mode = resolved_input_mode

        ct = get_content_type(page_type)
        if ct:
            content_type_config = ct.output_schema
            if not output_schema:
                output_schema = ct.output_schema
            if not site_type:
                site_type = ct.site_type
    except Exception:
        input_mode = job.input_mode or "url_list"

    # url_list fallback: when the Site row has no input_urls persisted (the
    # common case — Site.input_urls is empty for most sites), load them from the
    # production scrapers/{slug}/input_urls.json the user pre-populated. Without
    # this, url_list jobs run with 0 URLs and silently under-extract (the
    # "1 of N coverage gap" was actually "given ~0-1 URLs"). [data integrity]
    if not site_input_urls and input_mode == "url_list":
        try:
            import src.artifacts as artifacts

            _slug = _generate_slug(job.url)
            _iu_key = artifacts.scrapers_key(_slug, "input_urls.json")
            if artifacts.exists(_iu_key):
                _data = artifacts.read_json(_iu_key)
                _urls = [u for u in (_data.get("urls") or []) if isinstance(u, str) and u]
                if _urls:
                    site_input_urls = _urls
                    logger.info(
                        "Loaded %d input_urls from production file for %s "
                        "(Site.input_urls empty)",
                        len(_urls), _slug,
                    )
        except Exception as _exc:
            logger.warning("input_urls file fallback failed for %s: %s", job.url, _exc)

    sample_url = job.product_url or ""
    skip_product = False

    # If the user provided a custom schema (target_fields), make it AUTHORITATIVE:
    # override the content_type_config so every downstream stage (content_type_context,
    # normalize_fields, validate_coverage) uses the user's fields, not the registry
    # defaults. This prevents the pipeline from defaulting to product fields (title,
    # price, availability) when the user asked for something different.
    _target_fields = list(job.target_fields or [])
    if _target_fields and content_type_config:
        content_type_config = dict(content_type_config)
        content_type_config["fields"] = [
            {"name": str(f), "label": str(f), "type": "text", "required": True}
            for f in _target_fields
        ]

    # Nested schema tree (only when the user supplied a nested JSON Schema).
    # Drives recursive output pruning + is surfaced to code_writer so it emits
    # nested extraction. None for manual field-chip / flat-schema / legacy jobs.
    _nested_schema = None
    if job.schema_text:
        try:
            from src.schema_validation import parse_nested_schema
            _nested_schema = parse_nested_schema(job.schema_text)
        except Exception as exc:
            logger.warning("Job %s: nested schema parse failed: %s", job.id, exc)
            _nested_schema = None

    return {
        "job_id": job.id,
        "url": job.url,
        "sample_url": sample_url,
        "product_url": sample_url,
        "currency": job.currency or "",
        "sample_only": not job.full_extraction,
        "rescrape": False,
        "skip_approvals": bool(job.skip_approvals),
        "dagster_enabled": bool(job.dagster_enabled),
        "page_type": page_type,
        "input_mode": input_mode,
        "site_type": site_type,
        "content_type_config": content_type_config,
        "search_criteria": search_criteria,
        "output_schema": output_schema,
        "nested_schema": _nested_schema,
        # Intake-UI knobs (advisory; surfaced to product_analyzer / code_writer).
        "target_fields": list(job.target_fields or []),
        "scope": job.scope or "",
        "scope_value": job.scope_value or "",
        "user_notes": job.notes or "",
        "site_slug": _generate_slug(job.url),
        "site_name": "",
        "site_status": "new",
        "skip_site_analysis": False,
        "skip_product_analysis": skip_product,
        "skip_code_generation": False,
        "site_analysis_retries": 0,
        "content_analysis_retries": 0,
        "product_analysis_retries": 0,
        "test_retry_count": 0,
        "reanalyze_count": 0,
        "execution_status": "",
        "output_file": "",
        "item_count": 0,
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


def _prune_output_to_schema(output_file: str, allowed: set[str], schema_nested: dict | None = None) -> bool:
    """Drop any per-record key not in ``allowed`` from the output JSON; when
    ``schema_nested`` (a nested tree from ``parse_nested_schema``) is present,
    also inner-prune nested objects/arrays to the schema's children.

    Operates on the first top-level key whose value is a list of dicts (the
    records — e.g. ``products``/``jobs``); top-level ``site``/``metadata`` are
    left intact. Rewrites the artifact. Returns True if any keys were dropped.
    This is the deterministic, LLM-independent schema guarantee.

    ``output_file`` is now a File Master key (post-finalize); falls back to a
    local path for the workspace-phase case.
    """
    import src.artifacts as artifacts
    from src.content_types import prune_record_to_schema

    data = None
    try:
        data = artifacts.read_json(output_file)
    except Exception:
        try:
            with open(output_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError, FileNotFoundError):
            return False
    if data is None:
        return False
    pruned = False
    for key, val in list(data.items()):
        if isinstance(val, list) and val and isinstance(val[0], dict):
            before = [set(rec.keys()) for rec in val]
            if schema_nested:
                data[key] = [prune_record_to_schema(rec, allowed, schema_nested) for rec in val]
            else:
                data[key] = [{k: v for k, v in rec.items() if k in allowed} for rec in val]
            # C5 fix: detect change across ALL records, not just the first.
            if any(set(rec.keys()) != pre for rec, pre in zip(data[key], before)):
                pruned = True
            break  # only the records list
    if pruned:
        try:
            artifacts.write_json(output_file, data)
        except Exception:
            try:
                with open(output_file, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
            except Exception:
                pass
        logger.info("schema prune: trimmed output records to %d allowed fields", len(allowed))
    return pruned


def _finalize_was_cancelled(final_state: dict[str, Any]) -> bool:
    """F3: did the graph end because the user cancelled a human gate?

    Mirrors decisions.is_cancel semantics (decision None, legacy Cancel/
    Abort labels, or reject) against final_state['human_response'] — the
    value every cancel path writes (route_from_human_approval's
    cancel_values check, check_tracker's _handle_failed/_handle_complete).
    """
    try:
        from agents.decisions import is_cancel
    except Exception:
        return False
    response = final_state.get("human_response")
    if not isinstance(response, dict):
        return False
    return is_cancel(response)


def _output_file_has_zero_items(output_file: str) -> bool:
    """Jobs 309/310: True when the execution output parses but holds 0 records.

    Content-type-agnostic: any top-level list-of-dicts counts (products/jobs/
    articles/…), and a bare top-level array counts too. Deliberately
    conservative — any read/parse problem returns False (the job then takes
    the normal ladder) so we never fail a job on OUR OWN read error, only on
    a demonstrably empty extraction.
    """
    if not output_file:
        return False
    try:
        import json as _json
        import os as _os

        path = output_file
        if not _os.path.isabs(path):
            path = _os.path.join(_os.environ.get("PROJECT_ROOT", "/app"), path)
        if not _os.path.isfile(path):
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = _json.load(f)
    except Exception:
        return False
    if isinstance(data, list):
        return len([r for r in data if isinstance(r, dict)]) == 0
    if not isinstance(data, dict):
        return False
    for v in data.values():
        if isinstance(v, list):
            if any(isinstance(r, dict) for r in v):
                return False
    # No list-of-dicts anywhere (or all empty) → zero records.
    return True


def _publish_analysis_artifacts(job_id: int, site_slug: str, ws) -> None:
    """Copy the analysis artifacts from the LOCAL workspace to the File Master.

    M4 copy-path guard (artifact-corruption plan): the finalize loop previously
    copied raw bytes with no parse check, which is how the corrupt priceline
    ``test_report.json`` reached the FM — and ``setup_workspace`` then faithfully
    re-hydrates those bytes into the NEXT job's workspace, so corruption was
    durable across jobs. Each .json is now validated first (strict, then
    lenient → canonical redump from the PARSED value); unparseable bytes go
    through the in-memory repair; if still bad the copy is SKIPPED with an
    ERROR log rather than propagating corrupt bytes.
    """
    import src.artifacts as artifacts

    try:
        from agents.tools.filesystem_tools import guard_json_bytes
    except Exception as exc:  # pragma: no cover - import env problem
        logger.error("Job %s: guard_json_bytes unavailable: %s", job_id, exc)
        return

    for artifact in [
        "site_analysis.json",
        "navigation_analysis.json",
        "product_analysis.json",
        "scraper_analysis.json",
        "test_report.json",
    ]:
        src = ws / artifact
        if not src.is_file():
            continue
        try:
            _bytes = src.read_bytes()
            guarded, note = guard_json_bytes(_bytes)
            if guarded is None:
                logger.error(
                    "Job %s: %s is corrupt and unrepairable (%s) — SKIPPED, not "
                    "published to the File Master", job_id, artifact, note,
                )
                continue
            if note:
                logger.warning(
                    "Job %s: %s was corrupt — publishing REPAIRED version (%s)",
                    job_id, artifact, note,
                )
            artifacts.write(
                artifacts.scrapers_key(site_slug, "analysis", artifact), guarded
            )
            logger.info("Job %s: preserved %s to analysis/", job_id, artifact)
        except Exception as exc:
            logger.warning(
                "Job %s: preserve %s failed: %s", job_id, artifact, exc
            )


def _finalize_job(job: ScrapeJob) -> None:
    """Read the final graph checkpoint and persist results to the job.

    Extracts ``platform``, ``scraping_method``, ``product_count``,
    ``output_file``, ``site_name``, ``site_slug``, and ``error_message``
    from the graph state.  Closes any still-running Step rows and sets
    the job to COMPLETED or FAILED.

    Skipped entirely for captcha_blocked and akamai_blocked jobs (already
    finalized in check_accessibility).
    """
    job.refresh_from_db()
    if job.status in (
        ScrapeJob.STATUS_CAPTCHA_BLOCKED,
        ScrapeJob.STATUS_AKAMAI_BLOCKED,
    ):
        logger.info("Job %d: %s, skipping _finalize_job", job.id, job.status)
        return
    service = LangGraphService()
    graph = service.build_graph()
    config = service.get_config(job.id)

    final_state: dict[str, Any] = {}
    try:
        snapshot = graph.get_state(config)
        final_state = snapshot.values  # type: ignore[assignment]
    except Exception as exc:
        # F4/M5: a dead connection here previously fell through with
        # final_state={} → the status ladder landed on COMPLETED and the Site
        # flipped complete (silent success on an unreadable checkpoint). The
        # checkpointer owns a dedicated psycopg pool (min_size=0) — one retry
        # after recycling the ORM connections genuinely heals it.
        logger.warning("get_state failed for job %d (%s) — retrying once", job.id, exc)
        try:
            from django.db import close_old_connections

            close_old_connections()
        except Exception:
            pass
        try:
            snapshot = graph.get_state(config)
            final_state = snapshot.values  # type: ignore[assignment]
        except Exception as exc2:
            logger.error(
                "get_state failed twice for job %d — marking FAILED: %s", job.id, exc2
            )
            job.status = ScrapeJob.STATUS_FAILED
            job.error_message = f"finalizer could not read graph state: {exc2}"[:2000]
            job.completed_at = timezone.now()
            try:
                job.save(update_fields=["status", "error_message", "completed_at"])
                _publish_job_status(job.id, ScrapeJob.STATUS_FAILED)
            except Exception:
                pass
            return

    # ── Pull fields from graph state ────────────────────────────────────
    site_slug = final_state.get("site_slug", "")
    job.platform = final_state.get("platform", job.platform)
    job.scraping_method = final_state.get("scraping_method", job.scraping_method)
    job.product_count = final_state.get("product_count", job.product_count)
    job.output_file = final_state.get("output_file", job.output_file)
    # Per-job scraper/dagster attribution (set by _invoke_cleanup /
    # _invoke_dagster_converter). Each job remembers its own generated files.
    if final_state.get("scraper_path"):
        job.scraper_file = final_state.get("scraper_path")
    if final_state.get("dagster_path"):
        job.dagster_file = final_state.get("dagster_path")
    job.site_name = final_state.get("site_name", job.site_name)
    job.site_folder = f"scrapers/{site_slug}" if site_slug else job.site_folder
    job.error_message = final_state.get("error_message", job.error_message)

    # ── Ground-truth override from the LOCAL workspace output (Phase 4 wrote it
    #    there). The output is published to the File Master (and job.output_file
    #    repointed to its key) by the publish block below.
    if job.output_file:
        try:
            import pathlib

            p = pathlib.Path(job.output_file)
            out_data = None
            if p.is_file():
                with open(p, "r", encoding="utf-8") as fh:
                    out_data = json.load(fh)
            if out_data:
                # T0.5/H1: two templates emitted `site` as a bare host string —
                # `.get` on it AttributeError'd and the bare except below
                # discarded EVERY ground-truth override (count/name/platform/
                # method). Normalize any shape; platform falls back to the
                # site_analysis verdict (never to product_analysis — no such key).
                from src.output_site import ground_truth_platform, normalize_site_block

                _fallback_platform = ground_truth_platform(
                    final_state.get("site_analysis") or {}
                )
                site_block = normalize_site_block(
                    out_data.get("site"), fallback_platform=_fallback_platform
                )
                if site_block.get("platform"):
                    job.platform = site_block["platform"]
                if site_block.get("scraping_method") and not job.scraping_method:
                    job.scraping_method = site_block["scraping_method"]
                # count items across content types (products/jobs/articles/...)
                items = []
                for _ck in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
                    _v = out_data.get(_ck)
                    if isinstance(_v, list) and _v:
                        items = _v
                        break
                if items:
                    from src.content_types import has_substantive_field
                    successful = [
                        prod
                        for prod in items
                        if has_substantive_field(prod) and prod.get("status_code", 0) > 0
                    ]
                    job.product_count = len(successful)
                if site_block.get("name"):
                    job.site_name = site_block["name"]
                logger.info(
                    "Job %d: ground-truth from output — platform=%s, method=%s, products=%d",
                    job.id,
                    job.platform,
                    job.scraping_method,
                    job.product_count,
                )
        except Exception as exc:
            logger.warning(
                "Job %d: could not read output file for overrides: %s", job.id, exc
            )

    # ── Publish outputs + analysis to the File Master; repoint job.output_file ──
    if site_slug:
        try:
            import src.artifacts as artifacts
            from pathlib import Path as _P

            ws = _P(settings.PROJECT_ROOT) / "workspace" / site_slug
            _matched_key = None
            if ws.is_dir():
                # outputs → FM
                for f in ws.glob("output_*.json"):
                    _key = artifacts.scrapers_key(site_slug, f.name)
                    try:
                        artifacts.write(_key, f.read_bytes())
                        logger.info(
                            "Job %d: preserved %s to scrapers/%s/",
                            job.id, f.name, site_slug,
                        )
                    except Exception as exc:
                        logger.warning("Job %d: preserve output %s failed: %s", job.id, f.name, exc)
                    if job.output_file and _P(job.output_file).name == f.name:
                        _matched_key = _key
                # analysis → FM (M4: validated — corruption never reaches the FM)
                _publish_analysis_artifacts(job.id, site_slug, ws)
                # Defense-in-depth: don't rmtree if another job for this URL is
                # mid-flight (the dispatch guard should prevent this, but a
                # bypassed/played-with workspace would lose its artifacts).
                import shutil

                other_running = (
                    ScrapeJob.objects
                    .filter(url=job.url, status=ScrapeJob.STATUS_RUNNING)
                    .exclude(pk=job.id)
                    .exists()
                )
                if other_running:
                    logger.warning(
                        "Job %d: NOT removing workspace/%s/ — another job is running",
                        job.id, site_slug,
                    )
                else:
                    shutil.rmtree(ws, ignore_errors=True)
                logger.info("Job %d: cleaned workspace/%s/", job.id, site_slug)
            if _matched_key:
                job.output_file = _matched_key
        except Exception as exc:
            logger.warning("Job %d: workspace publish failed: %s", job.id, exc)

    # ── Prune old outputs in the File Master (keep newest 5 per site) ──
    if site_slug:
        try:
            import src.artifacts as artifacts
            _outs = [
                k for k in artifacts.list_keys(f"scrapers/{site_slug}/")
                if k.split("/")[-1].startswith("output_") and k.endswith(".json")
            ]
            # M5/R2: partner-job outputs are exempt (spec: fetchable via the
            # sync API forever). Unowned/legacy files keep pruning.
            try:
                from scraper.api.output_index import partner_owned_keys

                _protected = partner_owned_keys(site_slug)
            except Exception:
                _protected = set()
            _outs = [k for k in _outs if k not in _protected]
            if len(_outs) > 5:
                for _old in sorted(_outs)[:-5]:
                    try:
                        artifacts.delete(_old)
                    except Exception:
                        pass
                logger.info(
                    "Job %d: pruned %d old output(s) from FM (kept newest 5)",
                    job.id, len(_outs) - 5,
                )
        except Exception:
            pass

    # ── Guard production input_urls.json against silent shrinkage ───────
    # A job's workspace input_urls.json can be a subset (e.g. a sample/manual
    # run), and the cleanup agent may copy it over the production file —
    # silently shrinking the canonical URL list.  If the Site model carries
    # MORE URLs than the production file, re-derive the file from the Site
    # (source of truth).  Never wipes a larger file; no-op for navigation
    # jobs whose Site has no input_urls.  Generic. [data integrity]
    if site_slug:
        try:
            import src.artifacts as artifacts
            from scraper.models import Site as _Site

            _site = _Site.objects.filter(slug=site_slug).first()
            if _site and _site.input_urls:
                _iu_key = artifacts.scrapers_key(site_slug, "input_urls.json")
                _existing = []
                if artifacts.exists(_iu_key):
                    try:
                        _existing = (
                            artifacts.read_json(_iu_key) or {}
                        ).get("urls", []) or []
                    except Exception:
                        _existing = []
                if len(_site.input_urls) > len(_existing):
                    artifacts.write_json(_iu_key, {"urls": _site.input_urls})
                    logger.info(
                        "Job %d: re-synced input_urls.json from Site "
                        "(%d → %d URLs, was shrunk)",
                        job.id, len(_existing), len(_site.input_urls),
                    )
        except Exception as exc:
            logger.warning("Job %d: input_urls re-sync guard failed: %s", job.id, exc)

    # ── Determine final status ──────────────────────────────────────────
    if job.status in (
        ScrapeJob.STATUS_CAPTCHA_BLOCKED,
        ScrapeJob.STATUS_AKAMAI_BLOCKED,
        ScrapeJob.STATUS_CANCELLED,  # M3: a view-level cancel must not be resurrected
    ):
        pass
    elif _finalize_was_cancelled(final_state):
        # F3: a user Cancel ends the graph with no error_message and no FAILED
        # execution_status — the old ladder blessed it COMPLETED and flipped
        # the Site complete with 0 products (prod jobs 263/266/327).
        job.status = ScrapeJob.STATUS_CANCELLED
        logger.info("Job %d: finalised as CANCELLED (user cancelled)", job.id)
    elif job.error_message:
        job.status = ScrapeJob.STATUS_FAILED
    elif final_state.get("execution_status") == "FAILED":
        job.status = ScrapeJob.STATUS_FAILED
    elif not final_state.get("execution_status") and not job.output_file:
        # Job 304: the testing-cascade FAIL route lands on cleanup from a
        # conditional edge (no state-update channel), leaving execution_status
        # empty — and the old catch-all blessed that COMPLETED with 0 products.
        # Any job that reaches finalize without EVER executing (no status, no
        # output) did not succeed; say so.
        job.status = ScrapeJob.STATUS_FAILED
        if not job.error_message:
            job.error_message = (
                "Pipeline ended before execution (testing cascade exhausted "
                "without a passing run)"
            )[:2000]
        logger.warning(
            "Job %d: no execution_status and no output at finalize — marking FAILED "
            "(pipeline never executed)", job.id,
        )
    elif job.output_file and _output_file_has_zero_items(job.output_file):
        # Jobs 309/310 (pillowtalk e2e): an execution that RUNS but extracts
        # nothing still lands here — execution_status set + output file
        # present → the catch-all below blessed it COMPLETED. Job 309: the
        # stale-rescue executed a draft the tester had failed (0 items). Job
        # 310: a forced listing URL the draft's discovery couldn't parse
        # (0 URLs → 0 items). A zero-record output is a failure of the job's
        # purpose, not a success with an empty file — say so.
        job.status = ScrapeJob.STATUS_FAILED
        if not job.error_message:
            job.error_message = (
                "Execution produced 0 items (output file contains no records)"
            )[:2000]
        logger.warning(
            "Job %d: output file has 0 items at finalize — marking FAILED",
            job.id,
        )
    else:
        job.status = ScrapeJob.STATUS_COMPLETED

    # ── Enforce the requested schema (prune output + resolve for DB persist) ──
    # target_fields is authoritative; falls back to the Site's stored DB schema
    # (so re-runs of a schema'd site stay pruned). None → no schema → no prune.
    _schema_fields: list[str] = []
    _allowed_fields: set[str] | None = None
    if job.status == ScrapeJob.STATUS_COMPLETED:
        try:
            from src.content_types import resolve_allowed_fields, schema_field_names
            from scraper.models import Site as _Site

            _site_for_schema = _Site.objects.filter(
                url=job.url.rstrip("/")
            ).first()
            _db_os = (
                (_site_for_schema.output_schema or {})
                if _site_for_schema
                else (final_state.get("output_schema") or {})
            )
            _schema_fields = schema_field_names(job.target_fields or [], _db_os)
            _allowed_fields = resolve_allowed_fields(job.target_fields or [], _db_os)
        except Exception as exc:
            logger.warning("Job %d: schema resolve failed: %s", job.id, exc)

    # Deterministic prune — the guarantee that the output matches the schema,
    # regardless of what the agents extracted. Pass the nested tree (if any) for
    # recursive inner-pruning of objects/arrays.
    if _allowed_fields and job.output_file:
        try:
            _nested = final_state.get("nested_schema") or None
            _prune_output_to_schema(job.output_file, _allowed_fields, _nested)
        except Exception as exc:
            logger.warning("Job %d: schema prune failed: %s", job.id, exc)

    # Partner API (fold M10): build the byte page-index AFTER the schema
    # prune (which rewrites the FM artifact — the index must describe the
    # FINAL bytes, or page reads slice stale offsets) and emit the output
    # artifact event. Reads the FM copy; the workspace is already gone.
    if job.created_via == "api" and job.output_file:
        try:
            from scraper.api.output_index import finalize_output_index

            finalize_output_index(job)
        except Exception as exc:
            logger.warning("Job %d: output index hook: %s", job.id, exc)

    # ── Update Site model with ground truth ───────────────────────────
    if site_slug:
        try:
            from scraper.models import Site

            db_site = Site.objects.filter(url=job.url.rstrip("/")).first()
            if db_site:
                db_site.platform = job.platform or db_site.platform
                db_site.scraping_method = job.scraping_method or db_site.scraping_method
                db_site.product_count = job.product_count
                if job.status == ScrapeJob.STATUS_COMPLETED:
                    db_site.status = "complete"
                elif job.status == ScrapeJob.STATUS_CANCELLED:
                    # F3: a cancel is a user decision, not a site failure —
                    # leave in_progress so _do_schedule_next_site (which only
                    # picks new/failed) can't auto-resurrect the cancellation.
                    db_site.status = "in_progress"
                else:
                    db_site.status = "failed"
                db_site.last_scraped_at = timezone.now()
                if job.site_name:
                    db_site.name = job.site_name

                import src.artifacts as artifacts
                _prod_key = artifacts.scrapers_key(site_slug, "scraper.py")
                if artifacts.exists(_prod_key):
                    db_site.has_scraper = True
                    db_site.default_scraper_path = _prod_key  # store the KEY

                # Persist the resolved schema to the DB (revives Site.output_schema
                # as the integration point the older framework + future re-runs read).
                if job.status == ScrapeJob.STATUS_COMPLETED and _schema_fields:
                    try:
                        from src.content_types import get_content_type, get_output_key_label

                        _ct = get_content_type(job.page_type)
                        _out_key, _ = get_output_key_label(job.page_type)
                        db_site.output_schema = {
                            "output_key": _out_key,
                            "content_type": (_ct.name if _ct else ""),
                            "fields": [{"name": f} for f in _schema_fields],
                        }
                    except Exception as exc:
                        logger.warning("Job %d: Site.output_schema persist failed: %s", job.id, exc)

                # Accumulate actual extracted fields across ALL runs (grows
                # Site.fields_extracted with each completed job's output record
                # keys — the union that check-site reads for historical lookup).
                try:
                    import src.artifacts as artifacts
                    _out = None
                    if job.output_file:
                        try:
                            _out = artifacts.read_json(job.output_file)
                        except Exception:
                            _out = None
                    if _out:
                        for _ck in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
                            _items = _out.get(_ck)
                            if isinstance(_items, list) and _items and isinstance(_items[0], dict):
                                _existing = set(db_site.fields_extracted or [])
                                db_site.fields_extracted = sorted(
                                    _existing | set(_items[0].keys())
                                )
                                logger.info(
                                    "Job %d: accumulated %d fields into Site.fields_extracted (total %d)",
                                    job.id, len(_items[0]), len(db_site.fields_extracted),
                                )
                                break
                except Exception:
                    pass

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

STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES = 30


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

    threshold = timezone.now() - timezone.timedelta(
        minutes=STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES
    )
    stuck_jobs = ScrapeJob.objects.filter(
        status=ScrapeJob.STATUS_RUNNING,
    )

    if not stuck_jobs.exists():
        return

    failed = 0
    for job in stuck_jobs:
        latest_activity_qs = (
            SessionLog.objects
            .filter(job=job)
            .exclude(content__startswith="[HEARTBEAT]")
            .exclude(content__startswith="[PROBE]")
            .order_by("-created_at")
        )
        if latest_activity_qs.exists():
            last_activity = latest_activity_qs.first().created_at
        else:
            # started_at can be None (shell-dispatched/test rows); created_at
            # is always set — fall back rather than crash the watchdog
            last_activity = job.started_at or job.created_at

        if last_activity >= threshold:
            continue

        idle_minutes = int((timezone.now() - last_activity).total_seconds() / 60)

        # Actually terminate the Celery task, not just the DB row. Without this,
        # the worker keeps running the hung graph (LLM-phase hangs, abandoned
        # agent threads, an in-process 200-page discovery loop) while the DB row
        # says failed — the worker slot stays occupied until the (separate)
        # task time_limit backstop fires. terminate=True + SIGKILL reclaims it.
        #
        # ORDER: mark the job FAILED BEFORE revoking. Under acks_late (if enabled)
        # the terminated task is redelivered, and the dispatch dedup guard skips
        # redelivery while status==RUNNING — so marking FAILED first lets the
        # redelivery resume from the langgraph checkpoint instead of being
        # silently dropped (and the job stuck RUNNING forever). Harmless when
        # acks_late is off (today's default).
        error_msg = (
            f"No activity for {idle_minutes} min — job appears hung "
            f"(stalled agent phase or wedged scrape); celery task revoked."
        )
        logger.error(
            "Stuck job %d: no activity for %d min (last: %s), marking failed + revoking",
            job.id,
            idle_minutes,
            last_activity.isoformat(timespec="seconds"),
        )
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = error_msg
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])

        _task_id = getattr(job, "celery_task_id", "") or ""
        if _task_id:
            try:
                from celery import current_app

                current_app.control.revoke(_task_id, terminate=True, signal="SIGKILL")
                logger.info(
                    "Stuck job %d: revoked celery task %s (terminate)",
                    job.id,
                    _task_id,
                )
            except Exception as exc:
                logger.warning("Stuck job %d: revoke failed: %s", job.id, exc)

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

    active_statuses = {
        ScrapeJob.STATUS_RUNNING,
        ScrapeJob.STATUS_PENDING,
        ScrapeJob.STATUS_WAITING_APPROVAL,
    }
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
    if new_site.input_urls:
        import src.artifacts as artifacts
        artifacts.write_json(
            artifacts.scrapers_key(slug, "input_urls.json"),
            {"urls": new_site.input_urls},
        )

    job = ScrapeJob.objects.create(
        url=new_site.url,
        product_url=new_site.sample_url or "",
        currency=new_site.currency or "",
        full_extraction=True,
        auto_queued=True,
        user=None,  # system-queued job, no owner
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


STUCK_APPROVED_MAX_RETRIES = 3
STUCK_APPROVED_MIN_AGE_MINUTES = 5


@shared_task
def redispatch_stuck_approved_interrupts() -> None:
    """Watchdog (P0-10): re-dispatch resume for APPROVED approvals whose
    interrupt is still pending in the checkpoint (resume failed to consume it).

    Complements the dedup guard: the guard stops the 505-row runaway; this
    watchdog un-sticks the silent hang that replaces it. Capped at
    STUCK_APPROVED_MAX_RETRIES per interrupt_id; then the job is FAILED.
    """
    from scraper.models import Approval
    from scraper.services import LangGraphService

    threshold = timezone.now() - timezone.timedelta(minutes=STUCK_APPROVED_MIN_AGE_MINUTES)
    candidates = (
        Approval.objects.filter(
            status=Approval.STATUS_APPROVED,
            interrupt_id__gt="",
            job__status=ScrapeJob.STATUS_WAITING_APPROVAL,
            resolved_at__lt=threshold,
        )
        .select_related("job")
        .order_by("resolved_at")
    )
    if not candidates.exists():
        return

    service = LangGraphService()
    graph = service.build_graph()
    redispatched = 0

    for approval in candidates:
        job = approval.job
        # Check if the interrupt_id is still in the checkpoint (truly stuck).
        try:
            config = service.get_config(job.id)
            snapshot = graph.get_state(config)
        except Exception as exc:
            logger.warning("stuck-approved watchdog: job %d state read failed: %s", job.id, exc)
            continue

        pending_ids = set()
        for task in getattr(snapshot, "tasks", []):
            for intr in (getattr(task, "interrupts", None) or []):
                iid = getattr(intr, "id", None)
                if iid:
                    pending_ids.add(str(iid))

        if approval.interrupt_id not in pending_ids:
            continue  # interrupt was consumed — not stuck

        if approval.resume_attempts >= STUCK_APPROVED_MAX_RETRIES:
            logger.error(
                "stuck-approved watchdog: job %d failed — resume could not "
                "consume interrupt %s after %d attempts",
                job.id, approval.interrupt_id, approval.resume_attempts,
            )
            job.status = ScrapeJob.STATUS_FAILED
            job.error_message = (
                f"Resume failed to consume interrupt {approval.interrupt_id} "
                f"after {approval.resume_attempts} attempts (checkpoint may be corrupt)."
            )
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "error_message", "completed_at"])
            continue

        approval.resume_attempts += 1
        approval.save(update_fields=["resume_attempts"])
        # Replay the stored decision; fall back to approve if not stored.
        _value = approval.resume_value or {"decision": "approve", "label": "auto", "feedback": ""}
        logger.warning(
            "stuck-approved watchdog: job %d re-dispatching resume "
            "(interrupt=%s, attempt %d/%d)",
            job.id, approval.interrupt_id[:12],
            approval.resume_attempts, STUCK_APPROVED_MAX_RETRIES,
        )
        resume_scrape_task.delay(job.id, _value)
        redispatched += 1

    if redispatched:
        logger.info("stuck-approved watchdog: re-dispatched %d stuck resume(s)", redispatched)


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

        # Pass a proper decision dict (not a bare string) so the resume
        # value is consistent with the manual-approval path (views.py) and
        # the admin batch-action path — _parse_decision handles both, but a
        # bare string here was the only inconsistent trigger source.
        resume_scrape_task.delay(job.id, {"decision": "approve", "label": "auto-approved", "feedback": ""})
        approved += 1

    if approved:
        logger.info("Auto-approve: approved %d stale job(s)", approved)


# ═══════════════════════════════════════════════════════════════════════════
# Agent Playground — run individual agents in isolation
# ═══════════════════════════════════════════════════════════════════════════


def _build_playground_messages(agent_name: str, state: dict, user_prompt: str) -> list:
    """Build context-aware messages for playground agent runs.

    Uses the same message builders as the pipeline (subagents.py) so
    agents get connectivity info, workspace paths, budget limits, and
    tool usage guidance. Appends the user's custom prompt as additional
    context when provided.
    """
    from agents.subagents import (
        build_code_writer_message,
        build_code_tester_message,
        build_product_analyzer_message,
        build_site_analyzer_message,
    )

    builders = {
        "site_analyzer": build_site_analyzer_message,
        "product_analyzer": build_product_analyzer_message,
        "code_writer": build_code_writer_message,
        "code_tester": build_code_tester_message,
    }
    builder = builders.get(agent_name)
    if builder:
        messages = builder(state)
    else:
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content=user_prompt)]

    if user_prompt and builder:
        from langchain_core.messages import HumanMessage

        existing = messages[0].content if messages else ""
        augmented = f"{existing}\n\n### Additional User Instructions\n{user_prompt}"
        messages = [HumanMessage(content=augmented)]

    return messages


@shared_task(bind=True)
def run_agent_task(self, playground_id: int) -> None:
    """Run a single agent in isolation for the Agent Playground.

    Creates a minimal state dict, builds the agent, invokes it with the
    user-provided prompt, and records tool calls + messages.
    """
    from scraper.models import AgentPlayground

    pg = AgentPlayground.objects.get(pk=playground_id)
    pg.status = AgentPlayground.STATUS_RUNNING
    pg.started_at = timezone.now()
    pg.celery_task_id = self.request.id or ""
    pg.save(update_fields=["status", "started_at", "celery_task_id"])

    logger.info(
        "Agent Playground #%d: running %s (slug=%s, url=%s)",
        pg.id,
        pg.agent_name,
        pg.site_slug,
        pg.url,
    )

    try:
        # Build minimal state
        slug = pg.site_slug or _generate_slug(pg.url) if pg.url else "playground"
        state: dict[str, Any] = {
            "job_id": 0,  # No job — playground mode
            "url": pg.url,
            "site_slug": slug,
            "site_name": "",
            "input_mode": (
                "search_term"
                if pg.search_criteria
                else ("navigation" if "navigation" in pg.agent_name else "url_list")
            ),
            "search_criteria": pg.search_criteria or "",
            "page_type": "product",
            "sample_url": pg.url,
            "product_url": pg.url,
            "messages": [],
        }

        # Create workspace dir
        root = getattr(settings, "PROJECT_ROOT", os.getcwd())
        ws_dir = os.path.join(root, "workspace", slug)
        os.makedirs(ws_dir, exist_ok=True)

        # Set tool context for guards
        from agents.tools.context import set_tool_context, clear_tool_context

        set_tool_context(state, agent_name=pg.agent_name)

        try:
            result = None

            # Use context-aware message builder for LLM agents
            from agents.subagents import _build_agent

            agent = _build_agent(pg.agent_name, site_slug=slug)
            messages = _build_playground_messages(pg.agent_name, state, pg.prompt)
            budget = AGENT_MAX_ITERATIONS_LOOKUP.get(pg.agent_name, 25)
            agent_cfg: dict[str, Any] = {"recursion_limit": budget}
            agent_result = agent.invoke({"messages": messages}, config=agent_cfg)
            result = {"messages": agent_result.get("messages", [])}

            # Collect output artifacts
            artifacts: list[str] = []
            ws_path = os.path.join(root, "workspace", slug)
            if os.path.isdir(ws_path):
                for fname in sorted(os.listdir(ws_path)):
                    fpath = os.path.join(ws_path, fname)
                    if os.path.isfile(fpath) and fname.endswith(".json"):
                        artifacts.append(f"workspace/{slug}/{fname}")

            pg.output_artifacts = artifacts
            pg.output_summary = _summarize_agent_result(result)
            pg.tool_call_count = _count_tool_calls(result)
            pg.status = AgentPlayground.STATUS_COMPLETED
            logger.info(
                "Agent Playground #%d: completed (%d artifacts, %d tool calls)",
                pg.id,
                len(artifacts),
                pg.tool_call_count,
            )
        finally:
            clear_tool_context()

    except Exception as exc:
        logger.exception("Agent Playground #%d failed: %s", pg.id, exc)
        pg.status = AgentPlayground.STATUS_FAILED
        pg.error_message = str(exc)[-4000:]  # tail: keep the exception, not the banner
    finally:
        pg.completed_at = timezone.now()
        pg.save()


# T1.7: PLAYGROUND-ONLY per-agent cap — counts LLM turns, NOT graph recursion
# steps (graph.py's AGENT_RECURSION_MAP counts those; the two maps are
# different units and deliberately NOT unified — the playground's smaller
# budget is its only bound; inheriting graph values would make it unbounded).
# dagster_converter is absent here on purpose: the playground doesn't exercise
# it; the graph caps it in AGENT_RECURSION_MAP (120) + the 900s wall.
AGENT_MAX_ITERATIONS_LOOKUP: dict[str, int] = {
        "site_analyzer": 25,
    "product_analyzer": 50,
    "browser_traverse": 50,
    "nav_skill_review": 30,
    "scraper_analyzer": 25,
    "code_writer": 30,
    "code_tester": 30,
    "cleanup": 20,
    "skill_learner": 20,
}


def _summarize_agent_result(result: dict | None) -> str:
    """Extract a short text summary from an agent result."""
    if not result:
        return "(no result)"
    parts: list[str] = []
    messages = result.get("messages") or []
    for msg in messages[-5:]:
        cls = msg.__class__.__name__
        content = str(getattr(msg, "content", ""))[:300]
        if content.strip():
            parts.append(f"[{cls}] {content}")
    if not parts:
        for key in ("navigation_findings", "navigation_analysis"):
            if key in result:
                return f"Produced {key}"
    return "\n".join(parts[-5:]) if parts else "(completed)"


def _count_tool_calls(result: dict | None) -> int:
    """Count ToolMessage entries in result."""
    if not result:
        return 0
    messages = result.get("messages") or []
    return sum(1 for m in messages if m.__class__.__name__ == "ToolMessage")
