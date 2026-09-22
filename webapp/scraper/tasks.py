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
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded
from celery.signals import task_failure
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from .models import Approval, JobListing, ScrapeJob, Step
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


def _finalize_job_failed(job_id: int, error_message: str, close_steps: bool = False) -> None:
    """Persist an honest FAILED row for *job_id* (F4-safe).

    Shared by the in-task ``except`` path and the ``task_failure`` signal
    handler (worker-child process death): recycle DB connections first, then
    guard every write so the row always finalizes even on a dead connection
    (prod 284/332/333 — the failure is often the DB connection itself).
    """
    try:
        from django.db import close_old_connections

        close_old_connections()
    except Exception:
        pass
    message = (error_message or "unknown failure")[-4000:]
    try:
        job = ScrapeJob.objects.get(pk=job_id)
    except Exception:
        logger.error("Job %s: cannot finalize FAILED — row missing", job_id)
        return
    job.status = ScrapeJob.STATUS_FAILED
    job.error_message = message  # tail: keep the exception, not the banner
    job.completed_at = timezone.now()
    try:
        job.save(update_fields=["status", "error_message", "completed_at"])
    except Exception:
        try:
            job = ScrapeJob.objects.get(pk=job_id)  # fresh instance+connection
            job.status = ScrapeJob.STATUS_FAILED
            job.error_message = message
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "error_message", "completed_at"])
        except Exception:
            logger.error("Job %s: could not persist FAILED status (job stranded)", job_id)
    try:
        _publish_job_status(job_id, ScrapeJob.STATUS_FAILED)
    except Exception:
        pass
    if close_steps:
        # A SIGKILLed child leaves Step rows RUNNING forever (UI shows a phase
        # that will never finish); the in-task path doesn't need this because
        # _run_graph_job's own finalization closes them.
        try:
            Step.objects.filter(
                job_id=job_id, status__in=(Step.STATUS_RUNNING, Step.STATUS_PENDING)
            ).update(status=Step.STATUS_FAILED, completed_at=timezone.now())
        except Exception:
            pass
    try:
        from scraper.models import Site

        db_site = Site.objects.filter(url=(job.url or "").rstrip("/")).first()
        if db_site and db_site.status == "in_progress":
            db_site.status = "failed"
            db_site.save(update_fields=["status"])
            logger.info("Job %s: reset Site '%s' to failed", job_id, db_site.slug)
    except Exception:
        pass


def _output_artifact_item_count(job: ScrapeJob) -> int:
    """[wave-37 W37-3a] Count real records in the job's output artifact.

    The universal execution evidence (any content type): the record-array
    the runner wrote, read through the artifacts store with a direct-path
    fallback. Unreadable/missing/empty → 0 (never invents evidence).
    """
    if not job.output_file:
        return 0
    out: Any = None
    try:
        import json

        import src.artifacts as artifacts

        out = json.loads(artifacts.read_text(job.output_file))
    except Exception:
        try:
            import json

            with open(job.output_file) as f:
                out = json.load(f)
        except Exception:
            return 0
    if not isinstance(out, dict):
        return 0
    for value in out.values():
        if isinstance(value, list):
            return len(value)
    return 0


def finalize_from_artifacts(job_id: int, fallback_message: str) -> str:
    """[wave-37 W37-3a] Resolve the final status from on-disk evidence.

    Prod 593 died AFTER execution wrote real items but BEFORE the COMPLETED
    write; the generic failure finalize then lied about the run. Evidence
    order: (1) real records in the job's output artifact (universal; count
    = actual records, never the stale counter, steps closed like the
    success path); (2) real JobListing rows (jobs-domain dashboard table);
    (3) otherwise the honest failure path with the original cause. Only a
    RUNNING row is ever touched — parked/resumable (browser_unavailable,
    captcha/akamai) and already-terminal rows keep their status.
    """
    job = ScrapeJob.objects.filter(pk=job_id).first()
    if job is None:
        return ""
    if job.status != ScrapeJob.STATUS_RUNNING:
        return job.status
    items = _output_artifact_item_count(job)
    if items <= 0:
        items = JobListing.objects.filter(scrape_job=job).count()
    if items > 0:
        job.status = ScrapeJob.STATUS_COMPLETED
        job.product_count = items
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "product_count", "completed_at"])
        logger.warning(
            "Job %d: finalize_from_artifacts — %d record(s) on disk after a "
            "mid-flight kill; finalized COMPLETED from evidence (was RUNNING)",
            job_id, items,
        )
        _close_open_steps(job)
        _publish_job_status(job_id, ScrapeJob.STATUS_COMPLETED)
        return job.status
    _finalize_job_failed(job_id, fallback_message)
    return ScrapeJob.STATUS_FAILED


# [wave-14 job-133] Process-death honesty. When a prefork CHILD is SIGKILLed
# (container OOM / hard time_limit), no Python ever runs again in that child —
# the task body's own except-block cannot fire, and with acks_late=False there
# is no redelivery, so the job row stays RUNNING until the 30-min watchdog
# *guesses* from silence. The ``task_failure`` signal fires in the worker
# PARENT, which is exactly the survivor that receives WorkerLostError /
# TimeLimitExceeded. (``worker_lost`` is not a Celery 5.x signal, and
# ``Task.on_failure`` runs in the dying child — both dead ends.)
try:
    from billiard.exceptions import TimeLimitExceeded as _BilliardTimeLimitExceeded
    from billiard.exceptions import WorkerLostError as _BilliardWorkerLostError

    _PROCESS_DEATH_EXCUSES: tuple[type[BaseException], ...] = (
        _BilliardWorkerLostError,
        _BilliardTimeLimitExceeded,
    )
except Exception:  # pragma: no cover — billiard is a hard celery dependency
    _PROCESS_DEATH_EXCUSES = ()

_TASK_FAILURE_SENDERS = {
    "scraper.tasks.run_scrape_task",
    "scraper.tasks.resume_scrape_task",
}


@task_failure.connect
def _on_task_process_death(
    sender=None, task_id=None, args=None, exception=None, **_extra: Any
) -> None:
    """Mark the job FAILED the moment the worker parent reports child death."""
    if not _PROCESS_DEATH_EXCUSES or not isinstance(
        exception, _PROCESS_DEATH_EXCUSES
    ):
        return  # everything else is finalized by the task body itself
    if getattr(sender, "name", None) not in _TASK_FAILURE_SENDERS:
        return
    try:
        job = None
        if args:
            try:
                job = ScrapeJob.objects.filter(pk=int(args[0])).first()
            except (TypeError, ValueError):
                job = None
        if job is None and task_id:
            job = ScrapeJob.objects.filter(celery_task_id=task_id).first()
        if job is None or job.status != ScrapeJob.STATUS_RUNNING:
            # Not ours, already finalized, or waiting on a human (WAITING_
            # APPROVAL jobs are continued by a NEW resume task after approval,
            # so a dead child there strands nothing — don't clobber that row).
            # Idempotent if the watchdog marked it first.
            return
        exitcode = getattr(exception, "exitcode", None)
        detail = f"{type(exception).__name__}: {exception}"
        if exitcode is not None:
            detail += f" (exitcode={exitcode})"
        message = (
            f"Worker child process died mid-job ({detail}). This class means "
            f"the celery worker's child was SIGKILLed — almost always container "
            f"memory pressure (OOM) or the {_RUN_TASK_TIME_LIMIT}s hard time "
            f"limit. No Python ran after the kill, so no phase could finalize. "
            f"Failed by the task-failure handler; acks_late=False means no "
            f"redelivery — re-drive manually."
        )
        logger.error("Job %s: process-death finalize — %s", job.id, detail)
        _finalize_job_failed(job.id, message, close_steps=True)
    except Exception:
        logger.exception("task_failure handler could not finalize job")


# Celery-level deadline for a scrape job. The dominant job-level failure is
# LLM-phase hangs (code_generation / product_analysis): the per-phase
# _AGENT_INVOKE_TIMEOUT abandons its daemon thread on timeout, but the thread
# KEEPS RUNNING, so only a process-level kill reclaims it (and the worker slot).
# soft_time_limit → SoftTimeLimitExceeded (task can catch + finalize the job);
# time_limit → Celery SIGKILLs the worker (reclaims the abandoned thread + any
# leaked resources). [wave-22 A1] The values are REAL settings now
# (settings.py:CELERY_TASK_*_TIME_LIMIT, env-overridable) — previously the
# getattr fallbacks here were the only values ever in force because settings
# never defined the attributes (prod 365/370/371/372 died at the silent 3h
# default). Defaults (3.6h / 3.7h) clear EXECUTION_MAX_TIMEOUT (9600s) plus
# the worst observed pre-exec window plus finalize grace [job-68 theiconic
# died at exactly 2h mid-code-tester under the old 2h default; job-315
# citybeach needs ~110 min total].
from django.conf import settings as _settings  # noqa: E402

_RUN_TASK_SOFT_TIME_LIMIT = int(_settings.CELERY_TASK_SOFT_TIME_LIMIT)
_RUN_TASK_TIME_LIMIT = int(_settings.CELERY_TASK_TIME_LIMIT)


# ═══════════════════════════════════════════════════════════════════════════
# Dispatch keystone — the ONLY sanctioned way to publish run_scrape_task
# ═══════════════════════════════════════════════════════════════════════════

# [wave-15 1.2] How long a PENDING row with no task id may sit before the
# redispatch sweep (and the /health stranded gauge, which imports this)
# considers it abandoned. Floor chosen ABOVE the worst legitimate queue wait:
# the same-site serializer legitimately holds a PENDING row up to the task
# time limit (EXECUTION_MAX_TIMEOUT + retries ≈ 11160s ≈ 3.1h) while a sibling
# runs, so a naive 15-min floor would "recover" healthy queued work into a
# double dispatch.
PENDING_CLAIM_MINUTES = 15
# [wave-15 1.2] Redispatch attempts before a poison row is failed honestly
# instead of looping forever (precedent: Approval.resume_attempts).
PENDING_REDISPATCH_CAP = 2
# [wave-15 R1] How long a claimed-but-never-started row may sit before the
# stuck-job watchdog fails it on first sighting (see cleanup_stuck_jobs).
PRE_GRAPH_FAIL_GRACE_SECONDS = 300


def dispatch_scrape_job(job_id: int, **kwargs) -> str:
    """Publish ``run_scrape_task`` with a client-generated task id, stamped on
    the job row BEFORE the publish.

    Every dispatch site (views.py ×4, api/writers.py, the scrape command, the
    redispatch sweep) MUST go through this helper. Two properties it buys:

    * ``celery_task_id=""`` strictly means "never dispatched". The old
      publish-then-stamp order left "" also covering "queued, waiting for a
      slot" — and permanently so if the web process died between publish and
      save. The redispatch sweep (1.2) and /health stranded gauge (1.3)
      discriminate on "", so they are only sound with this order.
    * The task id the worker sees equals the id on the row, which is what
      lets the entry claim's same-id arm (1.1) tell "a crashed worker
      re-entering its own task" apart from "a second, different dispatch".

    If the broker publish raises, the stamp is reverted so the row keeps the
    recoverable "" signature, then the exception propagates to the caller.

    Maintenance lock: when the admin lock is ON, nothing is stamped and
    nothing is published — the row keeps the pristine "never dispatched"
    signature and simply waits. ``resume_maintenance_held_jobs`` drains
    those rows when the lock is lifted. Returning "" is safe for every
    caller: the return value has always been advisory.
    """
    from .models import MaintenanceLock

    if MaintenanceLock.is_enabled():
        logger.warning(
            "Job %d: maintenance lock is ON — staying PENDING (never dispatched)",
            job_id,
        )
        return ""
    task_id = str(uuid4())
    ScrapeJob.objects.filter(pk=job_id).update(celery_task_id=task_id)
    try:
        run_scrape_task.apply_async(
            args=(job_id,), kwargs=kwargs, task_id=task_id
        )
    except Exception:
        ScrapeJob.objects.filter(pk=job_id, celery_task_id=task_id).update(
            celery_task_id=""
        )
        raise
    return task_id


def resume_maintenance_held_jobs() -> list[int]:
    """Drain every row the maintenance lock held back: PENDING rows with the
    pristine "never dispatched" signature. Called by the toggle view when an
    admin lifts the lock, so queued work resumes without anyone re-clicking.
    Rows already dispatched/running/terminal are untouched — the signature
    "" is what makes "held" decidable. Returns the job ids drained."""
    held = list(
        ScrapeJob.objects.filter(
            status=ScrapeJob.STATUS_PENDING, celery_task_id=""
        ).order_by("created_at").values_list("id", flat=True)
    )
    for job_id in held:
        dispatch_scrape_job(job_id, rescrape=False)
    if held:
        logger.warning(
            "Maintenance lock lifted: resumed %d held job(s): %s",
            len(held), held,
        )
    return held


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
    # [wave-15 1.1] Claim-by-rowcount replaces the old read-then-judge dedup
    # guard: two workers racing the same dispatch both READ PENDING and both
    # ran. The atomic UPDATE lets exactly one claimant win; a loser sees
    # claimed=0 and returns. The same-id arm is what makes celery's own
    # redelivery (self.retry republishes with the SAME task id; worker-level
    # redelivery re-executes the same message) reclaim its own row instead of
    # being dropped as a "duplicate". WAITING_APPROVAL rows are excluded:
    # they are continued by resume_scrape_task, which owns their RUNNING
    # write — and the stuck-approved watchdog + auto-approver manage that
    # state deliberately.
    # [wave-14 job-133] The claim sets celery_task_id atomically WITH the
    # status, so a skipped duplicate can no longer steal the stamp from the
    # live task — the watchdog used to inspect a task that runs nowhere,
    # conclude "absent", and revoke the healthy run (false corpse).
    _task_id = getattr(self.request, "id", "") or ""
    _claim_pred = Q(status=ScrapeJob.STATUS_PENDING)
    _claim_values = {"status": ScrapeJob.STATUS_RUNNING}
    if _task_id:  # eager / always_eager test runs have no real task id
        _claim_pred |= Q(status=ScrapeJob.STATUS_RUNNING, celery_task_id=_task_id)
        _claim_values["celery_task_id"] = _task_id
    claimed = (
        ScrapeJob.objects.filter(pk=job_id)
        .filter(_claim_pred)
        .exclude(status=ScrapeJob.STATUS_WAITING_APPROVAL)
        .update(**_claim_values)
    )
    if not claimed:
        logger.warning(
            "Job %d: skipping duplicate dispatch (status=%s)", job_id, job.status
        )
        return
    # The claim UPDATE wrote behind the instance — refresh so the sibling
    # check and _run_graph_job see the claimed row, not a stale PENDING one.
    job.refresh_from_db(fields=["status", "celery_task_id"])

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
        try:
            raise self.retry(
                exc=RuntimeError("same-site job already running"),
                countdown=60,
                max_retries=None,
            )
        except MaxRetriesExceededError:
            # max_retries=None means "use the decorator default" (=1 here),
            # NOT unlimited: once the requeue budget is spent, self.retry
            # RAISES instead of republishing. That raise escapes past
            # _run_graph_job's try/except and is not a process-death excuse,
            # so nothing else would finalize the row — it used to strand
            # claimed-RUNNING forever, and (pre-1.1) PENDING-with-id forever,
            # which also blocked _do_schedule_next_site's auto-scheduling.
            _finalize_job_failed(
                job_id,
                "Same-site requeue exhausted: another job for this URL stayed "
                "RUNNING through every 60s retry. Failed honestly by the "
                "dispatch guard instead of stranding the row.",
            )
            return

    try:
        _run_graph_job(job, rescrape=rescrape, force_full=force_full)
    except SoftTimeLimitExceeded:
        # [wave-37 W37-3a] The soft-limit kill lands mid-graph; finalize from
        # on-disk evidence so a run that already wrote real items is not
        # marked FAILED (prod 593: every step done, count=10, marked failed).
        logger.error("Job %d: SoftTimeLimitExceeded — artifact-evidence finalize", job_id)
        finalize_from_artifacts(job_id, "Soft time limit exceeded mid-graph")
    except Exception as exc:
        logger.exception("Scrape job %d failed: %s", job_id, exc)
        # F4: the original failure is often a dead DB connection (postgres
        # OOM restart). Saving on the same dead connection re-raises and the
        # task dies with the job stranded RUNNING (prod 284/332/333).
        # _finalize_job_failed recycles connections and guards every save so
        # the row always finalizes.
        _finalize_job_failed(job_id, str(exc))


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
    # [wave-22 A3] task-scoped job budget: every phase wall clock clamps to
    # what is left of this deadline (graph._effective_timeout). Stamped HERE
    # — per task, not per job — so an approval-resumed job (a NEW task)
    # gets a fresh clock by construction instead of insta-failing on a
    # created_at-based one.
    config["configurable"]["task_deadline"] = (
        time.time() + _RUN_TASK_SOFT_TIME_LIMIT
    )
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
    # [wave-22 A3] FRESH task-scoped deadline on resume — this is a new task
    # with its own soft limit; reusing the original run's clock would resume
    # the job with ~0s of budget.
    config["configurable"]["task_deadline"] = (
        time.time() + _RUN_TASK_SOFT_TIME_LIMIT
    )

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

        if isinstance(exc, SoftTimeLimitExceeded):
            # [wave-37 W37-3a] A resume that dies mid-flight is the same 593
            # shape — finalize from on-disk evidence, not a blind FAILED.
            logger.error(
                "Job %d: SoftTimeLimitExceeded in resume — artifact-evidence finalize",
                job_id,
            )
            finalize_from_artifacts(job_id, "Soft time limit exceeded mid-resume")
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


def _llm_field_map_adapter(
    unresolved: list[str], page_type: str, registry_block: str = "",
    site_context: str = "",
) -> dict:
    """[wave-36 §1b] One-shot small-LLM mapping leg — SINGLE attempt, by design
    bypassing the ClassifiedRetryChatOpenAI 6-attempt ladder (round-1 F6: the
    ladder's worst case is ≈2 min on the job-start critical path). Returns
    ``{chip: {target, confidence, rationale}}`` or {} on ANY failure — the
    resolver falls through to fuzzy/verbatim."""
    try:
        import json as _json

        from agents.llm import get_small_llm
        from agents.nodes.url_judge import _strip_fences

        llm = get_small_llm(temperature=0.0, timeout=20)
        system = (
            "You map user-requested data fields to canonical field names for "
            "a web-scraping output contract.\nCanonical fields for this "
            f"content type:\n{registry_block}\n"
            "For EACH user field, return the canonical target it describes. "
            "If it is a genuine domain-specific field with no canonical "
            'equivalent, return the literal string "CUSTOM" (the field will '
            "be kept under its own name). Never invent a field name that is "
            "not listed and not CUSTOM.\n"
            "Respond with ONLY a JSON object (no markdown, no backticks), "
            "exactly this shape:\n"
            '{"<user field>": {"target": "<canonical-or-CUSTOM>", '
            '"confidence": 0.0-1.0, "rationale": "short"}}'
        )
        human = f"USER FIELDS: {unresolved!r}\nCONTENT TYPE: {page_type}\n"
        if site_context:
            human += f"SITE: {site_context}\n"
        resp = llm.invoke([("system", system), ("human", human)])
        text = getattr(resp, "content", "") or ""
        data = _json.loads(_strip_fences(text))
        out: dict = {}
        if isinstance(data, dict):
            for chip, entry in data.items():
                if isinstance(entry, dict) and entry.get("target"):
                    out[str(chip)] = {
                        "target": str(entry.get("target")),
                        "confidence": entry.get("confidence") or 0.5,
                        "rationale": str(entry.get("rationale") or "")[:200],
                    }
        return out
    except Exception as exc:
        logger.info(
            "field-mapping LLM leg unavailable (%s) — fuzzy/verbatim fallback",
            exc,
        )
        return {}


def _persist_field_mapping(job: ScrapeJob, blob: dict) -> None:
    """Stamp the resolved contract on the job row — the finalize prune reads
    the SAME blob the pipeline enforced (plan §1b). Emits the silent-but-
    logged ``[FIELD-MAP]`` audit row (job.notes + SessionLog; surfaced at
    /jobs/<id>/api/) — the 658 signal must never again be invisible."""
    job.field_mapping = blob
    try:
        job.save(update_fields=["field_mapping"])
    except Exception as exc:
        logger.warning("Job %s: field_mapping persist failed: %s", job.id, exc)
    try:
        mapping = blob.get("mapping") or {}
        n_can = sum(
            1 for e in mapping.values()
            if isinstance(e, dict)
            and e.get("target") not in (None, "CUSTOM")
            and e.get("source") != "verbatim"
        )
        n_custom = sum(
            1 for e in mapping.values()
            if isinstance(e, dict) and e.get("target") == "CUSTOM"
        )
        n_verb = sum(
            1 for e in mapping.values()
            if isinstance(e, dict) and e.get("source") == "verbatim"
        )
        per_chip = "; ".join(
            f"{chip}→{e.get('target_key') or e.get('target')}"
            f" ({e.get('source')})"
            for chip, e in mapping.items() if isinstance(e, dict)
        )
        warnings = blob.get("warnings") or []
        row = (
            f"[FIELD-MAP] {n_can} canonical / {n_custom} custom / "
            f"{n_verb} verbatim — {per_chip}"
            + (f" — WARNINGS: {'; '.join(warnings)}" if warnings else "")
        )
        try:
            job.notes = (job.notes or "") + f"\n{row[:1800]}"
            job.save(update_fields=["notes"])
        except Exception:
            pass
        try:
            from scraper.models import SessionLog

            seq = SessionLog.objects.filter(job_id=job.id).count()
            SessionLog.objects.create(
                job_id=job.id,
                role=SessionLog.ROLE_SYSTEM,
                agent="check_tracker",
                content=row[:4000],
                seq=seq,
            )
        except Exception:
            pass
    except Exception as exc:
        logger.info("Job %s: field-map audit row skipped: %s", job.id, exc)


def _resolve_field_mapping(
    job: ScrapeJob,
) -> tuple[dict, list[str], dict]:
    """[wave-36 §1b] Resolve intake chips → the record contract. Returns
    ``(blob, resolved_fields, rekeyed_field_notes)``.

    Identity (empty blob, raw chips, raw notes) when: the kill-switch is off,
    the job is partner-API authored (``created_via == "api"`` — the request
    body IS the partner's contract, round-2 M4), there are no chips, or the
    resolution is a no-op (resolved set == raw set, no warnings). NEVER
    raises. Hash reuse (F3) and the per-site cache (F6) make re-drives free;
    ``job_update`` chip edits clear the blob so the hash mismatch re-resolves.
    """
    raw_chips = [str(c).strip() for c in (job.target_fields or [])]
    raw_notes = dict(getattr(job, "field_notes", None) or {})

    def _identity():
        return {}, list(job.target_fields or []), raw_notes

    try:
        from src.field_mapping import (
            content_hash_for,
            mapping_enabled,
            rekey_field_notes,
            rename_map_from_mapping,
            resolve_mapping,
            resolved_fields_from_mapping,
        )
    except Exception as exc:
        logger.warning("field_mapping module unavailable (identity): %s", exc)
        return _identity()

    if (getattr(job, "created_via", "") or "") == "api":
        return _identity()
    if not mapping_enabled() or not raw_chips:
        return _identity()

    try:
        page_type = job.page_type or "product"
        want_hash = content_hash_for(raw_chips, page_type)

        existing = (
            job.field_mapping
            if isinstance(getattr(job, "field_mapping", None), dict)
            else None
        )
        if existing and existing.get("content_hash") == want_hash:
            resolved = resolved_fields_from_mapping(existing)
            if resolved:
                return (
                    existing, resolved,
                    rekey_field_notes(
                        raw_notes, existing.get("mapping") or existing,
                    ),
                )

        # Site cache: same contract resolved for this site before (F6).
        site = None
        cache: dict = {}
        cached_blob = None
        try:
            from scraper.models import Site

            site = Site.objects.filter(url=job.url.rstrip("/")).first()
            if site is not None and isinstance(
                getattr(site, "field_mapping_cache", None), dict
            ):
                cache = dict(site.field_mapping_cache)
                cached_blob = cache.get(want_hash)
        except Exception:
            site = None
        if isinstance(cached_blob, dict):
            resolved = resolved_fields_from_mapping(cached_blob)
            if resolved:
                _persist_field_mapping(job, cached_blob)
                return (
                    cached_blob, resolved,
                    rekey_field_notes(
                        raw_notes, cached_blob.get("mapping") or cached_blob,
                    ),
                )

        mapping, warnings = resolve_mapping(
            raw_chips, page_type,
            llm_fn=_llm_field_map_adapter,
            site_context=job.url or "",
        )
        resolved_names: list[str] = []
        for entry in mapping.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("target") == "CUSTOM":
                resolved_names.append(
                    str(entry.get("target_key")
                        or entry.get("target"))
                )
            elif entry.get("target"):
                resolved_names.append(str(entry["target"]))
        if not resolved_names or (
            set(resolved_names) == set(raw_chips) and not warnings
        ):
            return _identity()  # no-op contract → legacy byte-compat

        blob = {
            "mapping": mapping,
            "resolved_fields": resolved_names,
            "content_hash": want_hash,
            "warnings": warnings,
        }
        try:
            if site is not None:
                cache[want_hash] = blob
                site.field_mapping_cache = cache
                site.save(update_fields=["field_mapping_cache"])
        except Exception as exc:
            logger.info("field_mapping site cache write skipped: %s", exc)
        _persist_field_mapping(job, blob)
        logger.info(
            "Job %d: field mapping resolved — %d chip(s), %d warning(s), "
            "%d rename(s)", job.id, len(mapping), len(warnings),
            len(rename_map_from_mapping(mapping)),
        )
        return blob, resolved_names, rekey_field_notes(raw_notes, mapping)
    except Exception as exc:
        logger.warning(
            "Job %s: field mapping resolve failed (identity): %s", job.id, exc,
        )
        return _identity()


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

    # [wave-36 §1a/§1b] Two-vocabulary seeding: the resolver maps raw chips →
    # canonical names (alias → one-shot LLM → fuzzy → verbatim; identity for
    # partner-API jobs and when disabled). target_fields stays chip-verbatim
    # for skip-economics — NEVER repurposed (check_tracker contract diff).
    try:
        _mapping_blob, _resolved_seed, _mapped_notes = _resolve_field_mapping(
            job
        )
    except Exception as exc:
        logger.warning("Job %s: resolver seam failed (identity): %s",
                       job.id, exc)
        _mapping_blob = {}
        _resolved_seed = list(job.target_fields or [])
        _mapped_notes = dict(getattr(job, "field_notes", None) or {})

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
        # [wave-36 §1a] Two-vocabulary seeding (computed above the return):
        # resolved identity-or-persisted, target chip-verbatim.
        "resolved_fields": _resolved_seed,
        "field_mapping": _mapping_blob,
        # W27-4: per-field instructions — rendered as "### Field guidance" in
        # the product_analyzer + code_writer prompts. [wave-36 F4] re-keyed
        # through the mapping so they match the RESOLVED record keys.
        "field_notes": _mapped_notes,
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


def _prune_output_to_schema(
    output_file: str, allowed: set[str], schema_nested: dict | None = None,
    order: list[str] | None = None,
) -> bool:
    """Drop any per-record key not in ``allowed`` from the output JSON; when
    ``schema_nested`` (a nested tree from ``parse_nested_schema``) is present,
    also inner-prune nested objects/arrays to the schema's children.

    ``order`` (W27-5) — the user's field order; keys emit in that order first
    (bookkeeping appended). See ``prune_record_to_schema``.

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
            before = [list(rec.keys()) for rec in val]
            # W27-5: both branches route through prune_record_to_schema so the
            # user's field order (order=) is honored on every prune path.
            data[key] = [
                prune_record_to_schema(rec, allowed, schema_nested, order=order)
                for rec in val
            ]
            # C5 fix: detect change across ALL records, not just the first.
            # W27-5: ordered comparison — a pure reorder is also a change
            # worth rewriting (key-set equality would swallow it).
            if any(list(rec.keys()) != pre for rec, pre in zip(data[key], before)):
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


def _rename_output_keys(output_file: str, blob: dict | None) -> bool:
    """[wave-36 §1a] Record-key normalization at finalize: rename record keys
    raw→resolved per the persisted mapping (rename-then-keep — the 658
    name-loss fix). First top-level list-of-dicts only (site/metadata intact),
    like ``_prune_output_to_schema``. Identity when the blob carries no
    renames. Returns True when the file changed."""
    import src.artifacts as artifacts
    from src.field_mapping import rename_map_from_mapping

    rename = rename_map_from_mapping(blob)
    if not rename or not output_file:
        return False
    # [wave-36] A real local file (workspace-phase output) is read/written
    # DIRECTLY — routing it through artifacts.* would silently store it in the
    # FM under the raw path and leave the local file stale.
    _is_local = os.path.isfile(output_file)
    data = None
    if _is_local:
        try:
            with open(output_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return False
    else:
        try:
            data = artifacts.read_json(output_file)
        except Exception:
            try:
                with open(output_file, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError, FileNotFoundError):
                return False
    if not isinstance(data, dict):
        return False
    changed = False
    for key, val in data.items():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            for rec in val:
                if not isinstance(rec, dict):
                    continue
                for raw, resolved in rename.items():
                    if raw in rec:
                        rec[resolved] = rec.pop(raw)
                        changed = True
            break  # only the records list
    if changed:
        if _is_local:
            try:
                with open(output_file, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
            except OSError:
                return False
        else:
            try:
                artifacts.write_json(output_file, data)
            except Exception:
                try:
                    with open(output_file, "w", encoding="utf-8") as fh:
                        json.dump(data, fh, indent=2, ensure_ascii=False)
                except Exception:
                    return False
        logger.info(
            "field-mapping rename: %d record key(s) normalized", len(rename),
        )
    return changed


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


def _diagnose_no_execution(site_slug: str, job_id: int) -> str:
    """Job-74 class: say WHY a job reached finalize without ever executing.

    The catch-all below fires for two very different failures, and the old
    message called both "testing cascade exhausted": (a) the cascade actually
    exhausted (job-73 madewell), and (b) a draft that PASSED testing with real
    items but execution never ran at all (job-74 thenile cycle 1: tester PASS
    0.85, 5/5 fields, then the pipeline surfaced at cleanup with no
    execution_status — an approval/interrupt-resume gap, not a scraper
    defect). Diagnosing (b) as (a) sent the RCA down the wrong path.

    Reads the test report from wherever it survived: the F7 failure archive
    (cleanup archives it for every non-SUCCESS job), the FM site folder, or
    the workspace. Best-effort — falls back to the generic message.
    """
    import json as _json
    import os as _os

    try:
        import src.artifacts as artifacts

        candidates: list[Any] = []
        if site_slug:
            candidates.append(
                artifacts.scrapers_key(site_slug, "analysis", f"test_report-{job_id}.json")
            )
            candidates.append(artifacts.scrapers_key(site_slug, "test_report.json"))
        root = _os.environ.get("PROJECT_ROOT", "/app")
        if site_slug:
            candidates.append(
                _os.path.join(root, "workspace", site_slug, "test_report.json")
            )
        for key in candidates:
            try:
                if hasattr(artifacts, "exists") and not str(key).startswith("/"):
                    if not artifacts.exists(key):
                        continue
                    report = artifacts.read_json(key)
                else:
                    if not _os.path.isfile(str(key)):
                        continue
                    with open(str(key), encoding="utf-8", errors="replace") as f:
                        report = _json.load(f)
            except Exception:
                continue
            if not isinstance(report, dict):
                continue
            assessment = str(report.get("overall_assessment") or "").upper()
            try:
                confidence = float(report.get("confidence_score") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if assessment == "PASS":
                # [W26-8/prod-410] the old text claimed an interrupt/resume
                # gap — false for 410, where the ROUTER's exhausted-cascade
                # arm chose cleanup. Point at the [CASCADE] rows instead.
                return (
                    f"Tested PASS (confidence={confidence:.2f}) but execution "
                    "never ran — the router ended the job after testing "
                    "(exhausted-cascade / evidence arm); see the [CASCADE] "
                    "rows for the arm that fired"
                )[:2000]
            # [wave-22 B5] A non-PASS report IS the exhausted-cascade case —
            # but the pipeline already KNOWS why it failed; quote the latest
            # report's verdict and its first HIGH-severity issue instead of
            # the canned sentence (338's headline hid a full verdict that
            # named the exact extraction defect).
            _first_high = ""
            for _issue in report.get("issues") or []:
                if (
                    isinstance(_issue, dict)
                    and str(_issue.get("severity", "")).lower() == "high"
                    and (_issue.get("message") or _issue.get("description"))
                ):
                    _first_high = str(
                        _issue.get("message") or _issue.get("description")
                    )
                    break
            # Keep the historical job-77 phrase as the prefix (downstream
            # greps and the job-77 test pin it), then append the REAL verdict.
            _verdict = (
                "Pipeline ended before execution (testing cascade exhausted "
                "without a passing run) — latest test verdict: "
                f"{assessment or 'UNJUDGED'} (confidence={confidence:.2f})."
            )
            if _first_high:
                _verdict += f" First high-severity issue: {_first_high}"
            return _verdict[:2000]
    except Exception:
        pass
    return (
        "Pipeline ended before execution (testing cascade exhausted "
        "without a passing run)"
    )[:2000]


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
        with open(path, encoding="utf-8", errors="replace") as f:
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


def _final_status_ladder(
    final_state: dict,
    *,
    already_terminal: bool,
    was_cancelled: bool,
    error_message: str,
    output_file: str,
    diagnose_no_execution=None,
) -> tuple[str, str]:
    """The finalize status decision, as a pure function. [wave-19 T1.5]

    Returns ``(status, diagnostic)``: ``status`` is "" when the caller must
    leave the job untouched (``already_terminal``); ``diagnostic`` is a NEW
    failure reason for an otherwise-empty error_message (never-executed /
    zero-items arms), "" when the job's existing error_message already tells
    the story.

    The wave-19 ordering: the arms that PROVE the pipeline broke (explicit
    FAILED status, never executed, zero-item output) outrank a carried
    error_message — because by the time we get here the commonest
    error_message is a STALE interrupt-era note (323/D3): validate_coverage's
    missing-file interrupt set it, a later recovery answered it, execution
    extracted real items, and the old ladder's ``elif job.error_message:``
    killed the productive run anyway. A productive execution now outranks the
    note; the caller scrubs the stale note on COMPLETED.
    """
    if already_terminal:
        return "", ""
    if was_cancelled:
        # F3: a user Cancel ends the graph with no error_message and no FAILED
        # execution_status — the old ladder blessed it COMPLETED with 0
        # products (prod jobs 263/266/327).
        return ScrapeJob.STATUS_CANCELLED, ""
    if final_state.get("execution_status") == "FAILED":
        return ScrapeJob.STATUS_FAILED, ""
    if not final_state.get("execution_status") and not output_file:
        # Job 304: the testing-cascade FAIL route lands on cleanup from a
        # conditional edge (no state-update channel). Any job that reaches
        # finalize without EVER executing did not succeed; say so.
        if diagnose_no_execution is not None:
            try:
                return ScrapeJob.STATUS_FAILED, diagnose_no_execution()
            except Exception:
                pass
        return ScrapeJob.STATUS_FAILED, "Pipeline never executed (no execution status, no output)"
    if output_file and _output_file_has_zero_items(output_file):
        # Jobs 309/310 (pillowtalk e2e): an execution that RUNS but extracts
        # nothing is a failure of the job's purpose, not a success with an
        # empty file.
        return ScrapeJob.STATUS_FAILED, "Execution produced 0 items (output file contains no records)"
    if error_message:
        if final_state.get("execution_status") and output_file:
            # Productive execution (ran, and its output held real records —
            # the zero-items arm above already failed on empty) + a carried
            # error: the error is interrupt-era residue, not a verdict. The
            # job's purpose was served; the caller scrubs the note.
            return ScrapeJob.STATUS_COMPLETED, ""
        # No productive evidence contradicts it — the recorded error stands.
        return ScrapeJob.STATUS_FAILED, ""
    return ScrapeJob.STATUS_COMPLETED, ""


# ═══════════════════════════════════════════════════════════════════════════
# [wave-40 T8] Job-scoped real-items evidence — the evidence layer under the
# finalize rescue ladder. `_real_items_evidence` + `_rescue_min_count` are
# consumed by `_finalize_job`/`_final_status_ladder` (wave-40 T9).
# ═══════════════════════════════════════════════════════════════════════════

# Top-level record keys a content-type output may use besides the job's own
# output_key (mirrors the key list `_scraper_has_real_items`'s file scan uses).
_RESCUE_ROW_KEYS = ("products", "jobs", "articles", "results", "items",
                    "threads", "pages")

# output_%Y-%m-%d_%H%M%S[_%f]_{pid}.json — the optional middle field is the
# templates' 6-digit %f stamp (e.g. 000001), not a sub-second claim.
_RESCUE_OUTPUT_NAME_PATTERN = (
    r"^output_(?P<date>\d{4}-\d{2}-\d{2})_(?P<clock>\d{6})"
    r"(?:_(?P<micro>\d{6}))?_(?P<pid>\d+)\.json$"
)


def _rescue_min_count(input_mode: str) -> int:
    """Minimum real rows a rescue must prove, by input mode.

    Identical to the wave-37 adaptive ladder rule: url_list/list_page jobs
    extract from user-supplied URLs — 1 rich row IS a success — while
    navigation/search_term jobs need 3+ to prove discovery worked.
    """
    return 1 if (input_mode or "").strip() in ("url_list", "list_page") else 3


def _output_name_epoch(name: str) -> float | None:
    """``output_%Y-%m-%d_%H%M%S[_%f]_{pid}.json`` → UTC epoch, else None.

    The filename IS the write time: the File Master /list endpoint returns keys
    without mtimes, so an FM key's own name is the only freshness signal it
    has. Both the ``_%f`` and the no-``%f`` shapes are matched.
    """
    import re

    match = re.match(_RESCUE_OUTPUT_NAME_PATTERN, name or "")
    if not match:
        return None
    from datetime import datetime as _datetime
    from datetime import timezone as _dttz

    try:
        stamp = _datetime.strptime(
            f"{match.group('date')}_{match.group('clock')}", "%Y-%m-%d_%H%M%S"
        )
    except ValueError:
        return None
    return stamp.replace(tzinfo=_dttz.utc).timestamp()


def _rescue_dead_row(row: dict) -> bool:
    """Dead-row predicate — `route_after_testing._is_dead_product`, imported so
    the rescue can never drift from the router's definition (redirect/404/410
    status codes + soft-404 markers). If that module is unimportable, the same
    two checks run off agents.constants; if even those fail the row is KEPT —
    a rescue slightly over-counting a real file beats a decode hiccup
    discarding the whole file.
    """
    try:
        from agents.nodes.route_after_testing import _is_dead_product

        return bool(_is_dead_product(row))
    except Exception:
        pass
    try:
        from agents.constants import DEAD_STATUS_CODES, SOFT_404_MARKERS

        if row.get("status_code", 200) in DEAD_STATUS_CODES:
            return True
        remarks = (row.get("remarks") or "").lower()
        return any(marker in remarks for marker in SOFT_404_MARKERS)
    except Exception:
        return False


def _good_rows(data: object, fields: list[str], output_key: str) -> list[dict]:
    """Rows of one output payload that qualify as real items.

    Same predicate chain as the wave-37 rescue guard
    (`route_after_testing._scraper_has_real_items`, :623-646, with the job-118
    schema union the caller folds into ``fields``): the payload's top-level
    list under ``output_key`` (then the known content-type keys), minus dead
    rows, keeping rows that carry at least one filter/schema field — or, when
    the job declares none, any substantive (non-bookkeeping) field. A
    ``metadata.phase == "discovery"`` payload (job-76's URL stubs) is never
    extraction truth. Any shape problem degrades to [] — never raises.
    """
    try:
        if isinstance(data, list):
            # Bare top-level array — the shape `_output_file_has_zero_items`
            # also admits.
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            metadata = data.get("metadata")
            if isinstance(metadata, dict) and metadata.get("phase") == "discovery":
                return []
            rows = []
            for key in [output_key] + [k for k in _RESCUE_ROW_KEYS
                                       if k != output_key]:
                value = data.get(key)
                if isinstance(value, list) and value:
                    rows = [r for r in value if isinstance(r, dict)]
                    break
        else:
            return []
        live = [r for r in rows if not _rescue_dead_row(r)]
        if fields:
            return [r for r in live if any(r.get(field) for field in fields)]
        try:
            from src.content_types import has_substantive_field
        except Exception:

            def has_substantive_field(item):  # type: ignore[misc]
                return bool(item.get("title"))

        return [r for r in live if has_substantive_field(r)]
    except Exception:
        return []


def _real_items_evidence(
    slug: str, job: ScrapeJob, final_state: dict | None = None
) -> tuple[int, str]:
    """How many real rows did THIS job demonstrably produce, and in which file?

    Returns ``(good_row_count, locator)`` — locator is the local path or File
    Master key of the qualifying output that carried the most rows — or
    ``(0, "")`` when nothing qualifies. Three sources, best-of-N: (a)
    ``workspace/{slug}/output_*.json`` (mtime), (b) local
    ``scrapers/{slug}/output_*.json`` (mtime — dev bind-mount only), (c) FM
    keys ``scrapers/{slug}/output_*.json`` (filename epoch) via
    ``src.artifacts``.

    Deliberately JOB-scoped, not attempt-scoped: the wave-37 rescue predicate
    (`_scraper_has_real_items`) scans workspace-only and gates on the current
    draft / last_tested_at, so prod 762 (8 workspace outputs from earlier
    cycles, all older than the crashed attempt's draft floor → honest FAIL
    with 4+ real products on disk) and prod 770 (its own cleanup agent had
    already moved the 19-product output to ``scrapers/``, so a workspace-only
    scan saw nothing) both escaped it. Here the window is the JOB's
    (``started_at - 5s``, the 5s tolerating same-second writes).

    HONEST LIMITS. (1) Attribution is job-scoped, not output-to-sha: output
    files carry no draft sha, so sha-level attribution is impossible
    retroactively — the trace guard instead requires draft provenance for this
    job (state ``tested_draft_sha256`` / ``last_tested_draft_fp``, or this
    job's per-job FM draft key ``scrapers/{slug}/jobs/scraper-{job_id}.py``).
    (2) An FM key is admitted on its name alone, not window-gated: prod 770's
    output was published by the job's own cleanup agent yet the FM exposes no
    mtime, and a same-run publish can carry an earlier-stamped name — gating
    FM keys on the name epoch would have re-killed 770. Cross-job exclusion on
    the FM path therefore rests entirely on the trace guard (state provenance
    is this job's checkpoint; the per-job draft key is this job's). (3) A
    local output whose stamped name already proves it predates this job is a
    prior job's leftover re-hydrated into this workspace — its mtime is only
    the copy time — so it counts only when this job's own draft sits beside it,
    which vouches for the SAME job's earlier-cycle outputs (762's shape).
    (4) Sources (b) are dev-bind-mount only; in prod only (a) pre-publish and
    (c) exist. Every read error degrades to ``(0, "")`` — a rescue must never
    fail a job on OUR OWN read error.

    Kill switch: ``REAL_ITEMS_RESCUE_ENABLED=0`` (default on).
    """
    if os.getenv("REAL_ITEMS_RESCUE_ENABLED", "1") == "0":
        return 0, ""
    state = final_state or {}
    started = getattr(job, "started_at", None)
    if not slug or started is None:
        # No start time → no attribution window → no honest attribution.
        return 0, ""
    job_id = getattr(job, "id", None)

    # ── Trace guard: a draft must be traceable to THIS job ──────────────────
    draft_key = f"scrapers/{slug}/jobs/scraper-{job_id}.py" if job_id else ""
    artifacts = None
    fm_keys: list[str] = []
    try:
        import src.artifacts as artifacts

        fm_keys = [
            key for key in (artifacts.list_keys(f"scrapers/{slug}/") or [])
            if isinstance(key, str)
        ]
    except Exception as exc:
        logger.warning(
            "Job %s: real-items FM listing unavailable (%s) — local sources only",
            job_id, exc,
        )
        fm_keys = []
    has_provenance = any(
        str(state.get(key) or "").strip()
        for key in ("tested_draft_sha256", "last_tested_draft_fp")
    ) or bool(draft_key and draft_key in fm_keys)
    if not has_provenance:
        return 0, ""

    # ── Job window + the content-type field contract ────────────────────────
    from datetime import timedelta as _timedelta
    from datetime import timezone as _dttz

    try:
        started_at = started
        if timezone.is_naive(started_at):
            started_at = started_at.replace(tzinfo=_dttz.utc)
        floor = (started_at - _timedelta(seconds=5)).timestamp()
    except Exception:
        return 0, ""
    ct_config = state.get("content_type_config") or {}
    output_key = (ct_config.get("output_key", "products") if ct_config
                  else "products")
    fields: list[str] = []
    try:
        from src.content_types import output_filter_fields

        fields = list(output_filter_fields(ct_config.get("content_type", "")) or [])
    except Exception:
        fields = []
    try:
        # [wave-36 B1 / job-118] schema union (resolved ∪ raw ∪ custom): when
        # the job's own schema asks for NONE of the content type's filter
        # fields, judge rows by the fields the user actually requested.
        from src.field_mapping import union_output_fields

        schema_fields = [
            str(f).strip().lower()
            for f in union_output_fields(state) if str(f).strip()
        ]
        output_schema = state.get("output_schema")
        if not schema_fields and isinstance(output_schema, dict):
            schema_fields = [
                str(k).strip().lower() for k in output_schema.keys()
                if str(k).strip()
            ]
        schema_fields = [
            f for f in schema_fields if f not in ("title", "url", "src_url")
        ]
        if schema_fields and fields and not (set(fields) & set(schema_fields)):
            fields = schema_fields
    except Exception:
        pass

    from pathlib import Path as _P

    root = _P(getattr(settings, "PROJECT_ROOT", os.getcwd()))
    ws_dir = root / "workspace" / slug
    local_scrapers_dir = root / "scrapers" / slug
    local_draft = False
    draft_candidates = [ws_dir / "scraper_draft.py"]
    if job_id:
        draft_candidates.append(
            local_scrapers_dir / "jobs" / f"scraper-{job_id}.py")
    for candidate in draft_candidates:
        try:
            if candidate.is_file():
                local_draft = True
                break
        except OSError:
            continue

    best_count, best_fresh, best_loc = 0, 0.0, ""

    def _consider(count: int, fresh: float, locator: str) -> None:
        nonlocal best_count, best_fresh, best_loc

        if count > 0 and (count > best_count
                          or (count == best_count and fresh > best_fresh)):
            best_count, best_fresh, best_loc = count, fresh, locator

    # ── Sources (a) + (b): local output files, freshness-gated by mtime ────
    for directory in (ws_dir, local_scrapers_dir):
        try:
            names = sorted(os.listdir(directory)) if directory.is_dir() else []
        except OSError:
            names = []
        source_total, source_rows, source_loc, source_fresh = 0, 0, "", 0.0
        for name in names:
            if not (name.startswith("output_") and name.endswith(".json")):
                continue
            path = directory / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < floor:
                continue  # predates this job's window — not this job's output
            name_epoch = _output_name_epoch(name)
            if name_epoch is not None and name_epoch < floor and not local_draft:
                continue  # see HONEST LIMITS (3)
            try:
                data = json.loads(
                    path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue  # unreadable/unparseable — one file is not evidence
            rows = _good_rows(data, fields, output_key)
            if not rows:
                continue
            source_total += len(rows)
            fresh = name_epoch if name_epoch is not None else mtime
            if len(rows) > source_rows or (
                len(rows) == source_rows and fresh > source_fresh
            ):
                source_rows, source_loc, source_fresh = len(rows), str(path), fresh
        if source_total:
            _consider(source_total, source_fresh, source_loc)

    # ── Source (c): File Master output keys, freshness from the name ────────
    if artifacts is not None and fm_keys:
        fm_total, fm_rows, fm_loc, fm_fresh = 0, 0, "", 0.0
        for key in fm_keys:
            name = key.rsplit("/", 1)[-1]
            if not (name.startswith("output_") and name.endswith(".json")):
                continue
            name_epoch = _output_name_epoch(name)
            if name_epoch is None:
                continue  # not a stamped output name — cannot attribute it
            try:
                payload = json.loads(artifacts.read_text(key))
            except Exception:
                continue  # one unreadable key must not discard the others
            rows = _good_rows(payload, fields, output_key)
            if not rows:
                continue
            fm_total += len(rows)
            if len(rows) > fm_rows or (
                len(rows) == fm_rows and name_epoch > fm_fresh
            ):
                fm_rows, fm_loc, fm_fresh = len(rows), key, name_epoch
        if fm_total:
            _consider(fm_total, fm_fresh, fm_loc)

    if best_count:
        return best_count, best_loc
    return 0, ""


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


def _close_open_steps(job: ScrapeJob) -> None:
    """Close steps the graph left open when it finished.

    [wave-30 W30-9] never-run ≠ done: a PENDING step at job end is a phase
    that NEVER STARTED (resume skips, early death, skipped deterministic
    nodes) — it finalizes SKIPPED, not DONE. RUNNING steps keep the existing
    contract (their phase began, so DONE stands).
    """
    try:
        for step_obj in job.steps.filter(status=Step.STATUS_PENDING):
            step_obj.status = Step.STATUS_SKIPPED
            step_obj.completed_at = timezone.now()
            step_obj.save()
        for step_obj in job.steps.filter(status=Step.STATUS_RUNNING):
            step_obj.status = Step.STATUS_DONE
            step_obj.completed_at = timezone.now()
            step_obj.save()
    except Exception as exc:
        logger.warning("Failed to close steps for job %d: %s", job.id, exc)


def _finalize_job(job: ScrapeJob) -> None:
    """Read the final graph checkpoint and persist results to the job.

    Extracts ``platform``, ``scraping_method``, ``product_count``,
    ``output_file``, ``site_name``, ``site_slug``, and ``error_message``
    from the graph state.  Closes any still-running Step rows and sets
    the job to COMPLETED or FAILED.

    Skipped entirely for captcha_blocked and akamai_blocked jobs (already
    finalized in check_accessibility) and for browser_unavailable jobs
    [wave-16 B3] — already parked by the park node; _finalize_job's
    COMPLETED/FAILED ladder must not overwrite a resumable park.
    """
    job.refresh_from_db()
    if job.status in (
        ScrapeJob.STATUS_CAPTCHA_BLOCKED,
        ScrapeJob.STATUS_AKAMAI_BLOCKED,
        ScrapeJob.STATUS_BROWSER_UNAVAILABLE,
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
                with open(p, encoding="utf-8") as fh:
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
            from pathlib import Path as _P

            import src.artifacts as artifacts

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
    # The decision lives in _final_status_ladder (pure, tested); this site
    # keeps only the side effects (writes, logs). [wave-19 T1.5]
    _status, _diag = _final_status_ladder(
        final_state,
        already_terminal=job.status in (
            ScrapeJob.STATUS_CAPTCHA_BLOCKED,
            ScrapeJob.STATUS_AKAMAI_BLOCKED,
            ScrapeJob.STATUS_CANCELLED,  # M3: a view-level cancel must not be resurrected
        ),
        was_cancelled=_finalize_was_cancelled(final_state),
        error_message=job.error_message or "",
        output_file=job.output_file or "",
        diagnose_no_execution=lambda: _diagnose_no_execution(site_slug, job.id),
    )
    if _status:
        job.status = _status
    if _diag:
        if not job.error_message:
            job.error_message = _diag[:2000]
        logger.warning(
            "Job %d: finalised %s at finalize ladder: %s", job.id, _status, _diag,
        )
    if _status == ScrapeJob.STATUS_CANCELLED:
        logger.info("Job %d: finalised as CANCELLED (user cancelled)", job.id)
    if _status == ScrapeJob.STATUS_COMPLETED and job.error_message:
        # [wave-19 T1.5] The stale interrupt-era note loses to a productive
        # execution — scrub it so the record doesn't read COMPLETED+error.
        logger.info(
            "Job %d: clearing stale error_message at COMPLETED finalize: %r",
            job.id, (job.error_message or "")[:120],
        )
        job.error_message = ""

    # ── Enforce the requested schema (prune output + resolve for DB persist) ──
    # target_fields is authoritative; falls back to the Site's stored DB schema
    # (so re-runs of a schema'd site stay pruned). None → no schema → no prune.
    # [wave-36 §1a] A persisted field_mapping blob is MORE authoritative than
    # raw target_fields: record keys are renamed raw→resolved FIRST (the 658
    # name-loss fix), then pruning uses the resolved names — the ONE
    # resolved-only consumer (post-rename, safe). No blob → identity path,
    # byte-identical to today.
    _schema_fields: list[str] = []
    _allowed_fields: set[str] | None = None
    if job.status == ScrapeJob.STATUS_COMPLETED:
        try:
            from scraper.models import Site as _Site
            from src.content_types import (
                BOOKKEEPING_FIELDS,
                resolve_allowed_fields,
                schema_field_names,
            )
            from src.field_mapping import resolved_fields_from_mapping

            _site_for_schema = _Site.objects.filter(
                url=job.url.rstrip("/")
            ).first()
            _db_os = (
                (_site_for_schema.output_schema or {})
                if _site_for_schema
                else (final_state.get("output_schema") or {})
            )
            _mapping_blob = (
                job.field_mapping
                if isinstance(getattr(job, "field_mapping", None), dict)
                else None
            )
            _resolved = resolved_fields_from_mapping(_mapping_blob)
            if _resolved:
                _schema_fields = _resolved
                _allowed_fields = set(_resolved) | set(BOOKKEEPING_FIELDS)
                if job.output_file:
                    _rename_output_keys(job.output_file, _mapping_blob)
            else:
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
            # W27-5: the user's chip order (target_fields) becomes the record
            # key order; bookkeeping keys append after.
            _prune_output_to_schema(
                job.output_file, _allowed_fields, _nested,
                order=_schema_fields or None,
            )
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
                        from src.content_types import (
                            get_content_type,
                            get_output_key_label,
                            merge_field_notes,
                        )

                        _ct = get_content_type(job.page_type)
                        _out_key, _ = get_output_key_label(job.page_type)
                        # W27-4: per-field instructions ride into the stored
                        # schema so re-runs (and check-site readers) keep them.
                        db_site.output_schema = {
                            "output_key": _out_key,
                            "content_type": (_ct.name if _ct else ""),
                            "fields": merge_field_notes(
                                _schema_fields, getattr(job, "field_notes", None) or {}
                            ),
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
    _close_open_steps(job)

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
# [jobs 79/80] A task that Celery still reports ACTIVE is alive — log silence
# then means a long quiet phase (browser run, probe ladder), not a corpse.
# Only revoke such a task after a much longer silence: the wedge backstop.
ACTIVE_SILENCE_REVOKE_MINUTES = 90


def _task_liveness(task_id: str) -> str:
    """Is this Celery task executing anywhere? ``active`` / ``absent`` /
    ``unknown``.

    [jobs 79/80] Log silence is NOT proof of death: both re-drive tasks had
    their worker children destroyed at ~14:00:01 (probable container OOM),
    and the watchdog's 14:38 revoke was post-mortem cleanup mislabelled as a
    hung scrape. Asking Celery which tasks are actually running turns
    "silent" into a real discriminator:

    - ``absent``  — workers REPLIED and the task runs nowhere → the worker
      child is gone (``acks_late=False`` → no redelivery); fail immediately
      with an honest message instead of waiting out the silence rule.
      [wave-14 job-133] ``absent`` is only trusted when the SAME worker set
      also answers a second probe: a partial inspect reply (one PidBox —
      often just the events pool's self-reply — answering ``active()`` while
      the pool holding the task is too busy to respond) is indistinguishable
      from a complete "runs nowhere" answer. When the probe sets disagree,
      the verdict degrades to ``unknown`` and the caller falls back to the
      silence rule instead of revoking a live run.
    - ``active``  — a worker owns the task; silence means a long quiet
      phase. The caller only revokes past ``ACTIVE_SILENCE_REVOKE_MINUTES``.
    - ``unknown`` — inspect failed or no worker replied (broker hiccup,
      saturated pool). No evidence either way — the caller falls back to
      the silence rule.
    """
    if not task_id:
        return "unknown"
    try:
        from celery import current_app

        insp = current_app.control.inspect(timeout=5.0)
        reply = insp.active()
    except Exception:
        return "unknown"
    if reply is None:
        # Nobody answered — NOT evidence the task is gone.
        return "unknown"
    for worker_tasks in reply.values():
        for t in worker_tasks or ():
            if isinstance(t, dict) and t.get("id") == task_id:
                return "active"
    # Not in any reported active set — but a PARTIAL reply is not proof. Ask
    # a second probe: only if the same workers that answered active() also
    # answer ping() do we trust "runs nowhere".
    try:
        ping = insp.ping()
    except Exception:
        return "unknown"
    if ping is None or set(ping.keys()) != set(reply.keys()):
        return "unknown"
    return "absent"


@shared_task
def cleanup_stuck_jobs() -> None:
    """Detect and fail jobs whose worker has crashed (no recent activity).

    A healthy running job continuously produces SessionLog entries (tool
    calls, LLM responses).  If there are no new entries for longer than
    ``STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES``, the worker almost certainly
    crashed (OOM, segfault, etc.) and the job must be manually marked
    as failed — otherwise it stays RUNNING forever.

    Silence alone is no longer the verdict ([jobs 79/80]): before revoking,
    ``_task_liveness`` asks Celery whether the task still executes. An
    ``absent`` task fails immediately with an honest "worker process lost"
    message; an ``active`` task is left alone until the much longer wedge
    backstop, since silence there means a long quiet phase, not a corpse.

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
        # [wave-15 R1] A row claimed by celery but killed BEFORE the graph
        # started is provably pre-graph: RUNNING + started_at=None (stamped
        # only when _run_graph_job begins) + zero SessionLog rows of any kind
        # (probes and heartbeats included). The silence rule below reaches it
        # only via the created_at fallback (~30+ min) and then mislabels it
        # "worker process lost" from a liveness probe; fail it on first
        # sighting past a short grace instead. The grace keeps a just-claimed
        # job safe — it looks identical for its first seconds, but
        # _run_graph_job stamps started_at within moments of the claim.
        _pre_graph = (
            job.started_at is None
            and not SessionLog.objects.filter(job=job).exists()
            and (
                (timezone.now() - job.created_at).total_seconds()
                > PRE_GRAPH_FAIL_GRACE_SECONDS
            )
        )
        if _pre_graph:
            _task_id = getattr(job, "celery_task_id", "") or ""
            error_msg = (
                f"Job was claimed by celery task {_task_id} but never started "
                f"the graph (no started_at and no session logs after "
                f"{PRE_GRAPH_FAIL_GRACE_SECONDS}s grace): the worker process "
                f"died between the dispatch claim and graph start. Failed by "
                f"the stuck-job watchdog (pre-graph fast-fail)."
            )
            logger.error(
                "Stuck job %d: pre-graph fast-fail (age %ds, task %s)",
                job.id,
                int((timezone.now() - job.created_at).total_seconds()),
                _task_id,
            )
            _liveness = "pre-graph"
            idle_minutes = int(
                (timezone.now() - job.created_at).total_seconds() / 60
            )
        else:
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
                # started_at can be None (shell-dispatched/test rows);
                # created_at is always set — fall back rather than crash the
                # watchdog
                last_activity = job.started_at or job.created_at

            if last_activity >= threshold:
                continue

            idle_minutes = int(
                (timezone.now() - last_activity).total_seconds() / 60
            )

            # Liveness check BEFORE revoking ([jobs 79/80] class): silent ≠ dead.
            _task_id = getattr(job, "celery_task_id", "") or ""
            _liveness = _task_liveness(_task_id)
            if _liveness == "active" and idle_minutes < ACTIVE_SILENCE_REVOKE_MINUTES:
                logger.warning(
                    "Stuck job %d: silent %d min but celery task %s is ACTIVE — "
                    "not revoking (long quiet phase, not a corpse)",
                    job.id, idle_minutes, _task_id,
                )
                continue

            # Actually terminate the Celery task, not just the DB row. Without
            # this, the worker keeps running the hung graph (LLM-phase hangs,
            # abandoned agent threads, an in-process 200-page discovery loop)
            # while the DB row says failed — the worker slot stays occupied
            # until the (separate) task time_limit backstop fires.
            # terminate=True + SIGKILL reclaims it.
            #
            # ORDER: mark the job FAILED BEFORE revoking. Under acks_late (if
            # enabled) the terminated task is redelivered, and the entry claim
            # (1.1) skips redelivery while status==RUNNING — so marking FAILED
            # first lets the redelivery resume from the langgraph checkpoint
            # instead of being silently dropped (and the job stuck RUNNING
            # forever). Harmless when acks_late is off (today's default).
            if _liveness == "absent":
                # Report the ACTUAL broker config instead of a hardcoded
                # assumption.
                try:
                    from celery import current_app as _celery_app

                    _acks_late = bool(_celery_app.conf.task_acks_late)
                except Exception:
                    _acks_late = False
                _redelivery = (
                    "acks_late=True — a broker redelivery may still re-run it"
                    if _acks_late
                    else "acks_late=False means no redelivery"
                )
                error_msg = (
                    f"Worker process lost: celery task {_task_id} is not active "
                    f"on any worker (silent {idle_minutes} min; {_redelivery}). "
                    f"Failed by the stuck-job watchdog."
                )
            elif _liveness == "active":
                error_msg = (
                    f"No activity for {idle_minutes} min while the task still "
                    f"reports ACTIVE — treated as wedged (stalled agent phase "
                    f"or hung scrape); celery task revoked."
                )
            else:
                error_msg = (
                    f"No activity for {idle_minutes} min — job appears hung "
                    f"(stalled agent phase or wedged scrape); celery task "
                    f"revoked."
                )
            logger.error(
                "Stuck job %d: liveness=%s, no activity for %d min (last: %s), "
                "marking failed + revoking",
                job.id,
                _liveness,
                idle_minutes,
                last_activity.isoformat(timespec="seconds"),
            )
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = error_msg
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])

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

        # [wave-14] The celery revoke kills THIS worker's task — but the wedge
        # is often the /scrape SUBPROCESS inside browser_service, which has no
        # celery parent and would keep burning the shared Scraper Chrome until
        # its own timeout. Cancel it by job id (lock-free, short timeout, never
        # raises). Runs AFTER the job is marked FAILED so a wedge that already
        # ended still just gets an honest "nothing in flight".
        try:
            from agents.tools.browser_http import cancel_scrape

            _cancel = cancel_scrape(job.id)
            if _cancel.get("requested") is False and _cancel.get("error"):
                logger.warning(
                    "Stuck job %d: browser_service cancel unavailable: %s",
                    job.id,
                    _cancel["error"],
                )
            elif _cancel.get("flagged"):
                logger.info(
                    "Stuck job %d: browser_service cancelled run(s) %s",
                    job.id,
                    _cancel.get("flagged"),
                )
        except Exception as exc:
            logger.warning("Stuck job %d: browser_service cancel failed: %s", job.id, exc)

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
# [wave-35] DB retention — daily purge behind dead-end jobs
# ═══════════════════════════════════════════════════════════════════════════


@shared_task
def purge_retention() -> dict:
    """[wave-35] Daily retention sweep (beat: ``purge-retention``).

    Purges langgraph checkpoint rows + SessionLog/ToolCallLog/Step for
    failed/cancelled jobs past RETENTION_DAYS_FAILED (default 7) and
    completed jobs past RETENTION_DAYS_COMPLETED (default 90), and folds in
    expired django_session cleanup. Never touches parked/live statuses —
    resumable jobs need their checkpoints (see scraper/retention.py).
    """
    if not settings.RETENTION_ENABLED:
        return {"disabled": True}
    from scraper.retention import purge_retention as _sweep

    return _sweep(
        days_failed=settings.RETENTION_DAYS_FAILED,
        days_completed=settings.RETENTION_DAYS_COMPLETED,
    )


# ═══════════════════════════════════════════════════════════════════════════
# [wave-16 B3] Dependency-park resumer
# ═══════════════════════════════════════════════════════════════════════════


@shared_task
def resume_browser_unavailable_jobs() -> dict:
    """Re-dispatch jobs parked on browser_service unavailability, once it's back.

    The park (check_accessibility → akamai/captcha precedent, but NON-terminal)
    ends the graph with STATUS_BROWSER_UNAVAILABLE when browser_service itself
    cannot serve — /health degraded or unreachable. This beat task is the other
    half: poll /health, and while it reports "ok", flip parked rows back to
    PENDING and dispatch them through dispatch_scrape_job (the same re-entry
    path as a manual re-drive — check_tracker's skip flags resume from the
    right phase instead of redoing analysis).

    Deliberately CONSERVATIVE: one /health sample gates the whole tick, batch
    capped (oldest first) so a large park queue doesn't stampede the worker
    pool, and each flip is claimed by status rowcount so a concurrent tick (or
    a manual cancel→failed of the same row) can't double-dispatch. A job that
    parks again after resume just parks again — flapping self-corrects when
    the gateway stays healthy long enough for a full run.
    """
    from django.conf import settings as _dj_settings

    if not getattr(_dj_settings, "BROWSER_RESUME_ENABLED", True):
        return {"resumed": 0, "reason": "disabled"}

    from agents.tools.browser_http import browser_service_strict_ok

    # [wave-33 T33-2] The RESUMER keeps the literal-"ok" gate: pre-flights may
    # proceed against a degraded-but-browsable gateway, but resuming parked
    # jobs into one is a choice, not a necessity — conservative by design.
    if not browser_service_strict_ok():
        return {"resumed": 0, "reason": "browser_service unhealthy"}

    from agents.tools.browser_http import BROWSER_RESUME_BATCH

    # [wave-37 W37-3b] Cumulative parked budget: park/resume exists to
    # survive SHORT outages, not to fund unbounded park→resume→park flapping
    # across repeated saturation windows. Past the budget, finalize honestly.
    budget = int(os.environ.get("PARKED_TIME_BUDGET_S", "7200"))

    parked = list(
        ScrapeJob.objects.filter(
            status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE
        ).values("id", "parked_seconds", "last_parked_at").order_by("id")[
            :BROWSER_RESUME_BATCH
        ]
    )
    if not parked:
        return {"resumed": 0, "reason": "none parked"}

    resumed = []
    exhausted: list[dict] = []
    for row in parked:
        job_id = row["id"]
        # Current episode counts toward the budget even before it finishes.
        episode = max(0.0, time.time() - row["last_parked_at"]) if row["last_parked_at"] else 0.0
        total = (row["parked_seconds"] or 0) + episode
        if total >= budget:
            # Claim-by-rowcount finalize: only a still-parked row may flip.
            claimed = ScrapeJob.objects.filter(
                pk=job_id, status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE
            ).update(
                status=ScrapeJob.STATUS_FAILED,
                error_message=(
                    f"Browser-service park budget exhausted ({int(total)}s "
                    f"cumulative across outages; budget {budget}s). Finalized "
                    f"honestly — re-drive manually when the service is stable."
                )[:4000],
                completed_at=timezone.now(),
            )
            if not claimed:
                continue
            logger.warning(
                "browser-park resumer: job %d EXHAUSTED park budget "
                "(%ds >= %ds) — finalized failed instead of re-dispatching",
                job_id, int(total), budget,
            )
            _publish_job_status(job_id, ScrapeJob.STATUS_FAILED)
            try:
                from scraper.models import Site

                job_url = ScrapeJob.objects.filter(pk=job_id).values_list(
                    "url", flat=True
                ).first()
                db_site = Site.objects.filter(url=(job_url or "").rstrip("/")).first()
                if db_site and db_site.status == "in_progress":
                    db_site.status = "failed"
                    db_site.save(update_fields=["status"])
            except Exception:
                pass
            exhausted.append({"job_id": job_id, "parked_seconds": int(total)})
            continue
        # Claim-by-rowcount: only a still-parked row may flip. A row that
        # changed underneath (manual cancel/re-drive) is skipped untouched.
        claimed = ScrapeJob.objects.filter(
            pk=job_id, status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE
        ).update(
            status=ScrapeJob.STATUS_PENDING,
            parked_seconds=(row["parked_seconds"] or 0) + int(episode),
            last_parked_at=None,
        )
        if not claimed:
            continue
        try:
            dispatch_scrape_job(job_id)
            resumed.append(job_id)
            logger.info(
                "browser-park resumer: job %d re-dispatched (browser_service healthy)",
                job_id,
            )
        except Exception as exc:
            # The row is PENDING with a stamped task id only if publish
            # succeeded; dispatch_scrape_job reverts the stamp on failure, so
            # put the row back where the next tick can find it.
            ScrapeJob.objects.filter(
                pk=job_id, status=ScrapeJob.STATUS_PENDING
            ).update(status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE)
            logger.warning(
                "browser-park resumer: job %d dispatch failed (%s) — still parked",
                job_id, exc,
            )
    return {"resumed": len(resumed), "jobs": resumed, "exhausted": exhausted}


@shared_task
def redispatch_abandoned_pending() -> dict:
    """Recover PENDING rows that were never dispatched — ONE row per sweep.

    Signature (sound only with the 1.0 keystone, which stamps celery_task_id
    BEFORE publishing): ``status=PENDING ∧ celery_task_id=""`` means "created
    but the publish never happened or was rolled back". Without the keystone
    "" also covers "queued, waiting for a same-site slot", so this task ships
    env-gated OFF (REDISPATCH_SWEEP_ENABLED) — enabling it before every
    dispatch site goes through dispatch_scrape_job() would double-dispatch
    healthy queued rows.

    Claims on the redispatch_count COUNTER, not the status: claiming by
    flipping status to RUNNING here would make the republished task's own
    entry claim (1.1) see RUNNING + no matching id and drop the recovery.
    Cap 2 → honest FAILED, so a permanently poison row can't loop forever.
    One row per sweep bounds the blast radius of a bad republish.

    Maintenance lock: checked BEFORE both active arms — including the
    exhausted honest-fail arm. A row held in maintenance looks exactly like a
    poison row (PENDING, no task id, older than the claim window,
    redispatch_count possibly ≥ cap); failing it while the operator is just
    holding the system would be the opposite of honest.
    """
    # Env gate FIRST: a disabled sweep must stay a no-op with ZERO db access
    # (wave-15 contract). The lock guard protects the ACTIVE arms below.
    if not getattr(settings, "REDISPATCH_SWEEP_ENABLED", False):
        return {"action": "disabled"}
    from .models import MaintenanceLock

    if MaintenanceLock.is_enabled():
        return {"action": "maintenance_hold"}

    cutoff = timezone.now() - timezone.timedelta(minutes=PENDING_CLAIM_MINUTES)
    _signature = dict(
        status=ScrapeJob.STATUS_PENDING,
        celery_task_id="",
        created_at__lt=cutoff,
    )

    exhausted = (
        ScrapeJob.objects.filter(redispatch_count__gte=PENDING_REDISPATCH_CAP)
        .filter(**_signature)
        .order_by("created_at")
        .first()
    )
    if exhausted is not None:
        ScrapeJob.objects.filter(pk=exhausted.pk).update(
            status=ScrapeJob.STATUS_FAILED,
            error_message=(
                "Abandoned in PENDING with no celery task id: the redispatch "
                f"sweep re-published it {PENDING_REDISPATCH_CAP} times and it "
                "never claimed a worker. Failed honestly instead of looping "
                "forever."
            ),
            completed_at=timezone.now(),
        )
        logger.warning(
            "Redispatch sweep: job %d exhausted %d redispatches — failed",
            exhausted.pk, PENDING_REDISPATCH_CAP,
        )
        return {"action": "failed", "job_id": exhausted.pk}

    candidate = (
        ScrapeJob.objects.filter(redispatch_count__lt=PENDING_REDISPATCH_CAP)
        .filter(**_signature)
        .order_by("created_at")
        .first()
    )
    if candidate is None:
        return {"action": "idle"}

    claimed = ScrapeJob.objects.filter(
        pk=candidate.pk,
        status=ScrapeJob.STATUS_PENDING,
        celery_task_id="",
        redispatch_count__lt=PENDING_REDISPATCH_CAP,
    ).update(redispatch_count=F("redispatch_count") + 1)
    if not claimed:
        # A concurrent sweep (or the row's own dispatch) won the race.
        return {"action": "contested"}

    dispatch_scrape_job(candidate.pk, rescrape=False)
    logger.warning(
        "Redispatch sweep: republished abandoned PENDING job %d "
        "(redispatch attempt %d)",
        candidate.pk, candidate.redispatch_count + 1,
    )
    return {"action": "redispatched", "job_id": candidate.pk}


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
        # [wave-16 B3] Parked jobs are unfinished work the resumer will bring
        # back — counting them holds the auto-scheduler so an outage doesn't
        # manufacture a fresh parked job every 5-min tick.
        ScrapeJob.STATUS_BROWSER_UNAVAILABLE,
    }
    active_count = ScrapeJob.objects.filter(status__in=active_statuses).count()
    if active_count:
        return {
            "action": "skipped",
            "reason": f"{active_count} active job(s) "
            "(RUNNING/PENDING/WAITING_APPROVAL/BROWSER_UNAVAILABLE)",
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

    dispatch_scrape_job(job.id, rescrape=False)

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
        build_code_tester_message,
        build_code_writer_message,
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
        from agents.tools.context import clear_tool_context, set_tool_context

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
