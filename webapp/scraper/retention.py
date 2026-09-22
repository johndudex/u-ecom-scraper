"""[wave-35] Database retention: purge heavy rows behind dead-end jobs.

Why: prod postgres hit 25GB (2026-09-16) — ~97% of it the LangGraph
checkpoint tables. PostgresSaver writes the FULL serialized ScrapeState per
superstep (``checkpoints`` + ``checkpoint_blobs`` + ``checkpoint_writes``)
for every job and nothing ever deleted them; SessionLog / ToolCallLog / Step
and django_session grow the same way. All the byte hogs anchor on the job:
checkpoints by ``thread_id = "job-{ScrapeJob.pk}"``
(``scraper/services.get_thread_id``), the log tables by job FK.

Retention windows (dead-end only — see ``_PURGEABLE_FAILED_GROUP``):
  failed, cancelled  → after ``days_failed``  (default 7)
  completed          → after ``days_completed`` (default 90)

Never touched, ever: pending / running / waiting_approval and the parked
statuses (captcha_blocked, akamai_blocked, browser_unavailable). These are
NOT the model's ``_TERMINAL_JOB_STATUSES`` — that set counts captcha/akamai
as "job ended", but those jobs get re-driven (park/resume, manual re-drive),
so retention must keep their state. Interrupt/approval resume reads
checkpoints back too.

Deliberately kept: the ScrapeJob row itself + JobListing rows (tiny — job
history, dashboards and site stats stay intact). This purges the byte hogs,
not the record.

Batched with a cursor: every cycle claims one id-ordered page of expired
jobs, deletes its children, commits, advances ``id > last_id``. Since the
ScrapeJob rows survive, a plain "take the first N" loop would re-claim the
same page forever; the id cursor is what makes the sweep terminate. Batches
also stay under statement timeouts — a single unbounded DELETE died live in
the Railway console against prod data (2026-09-16).

The checkpoint tables are created by langgraph's saver at runtime, not by a
migration — a fresh database may not have them yet. The sweep skips them
(``to_regclass``) until they exist instead of crashing the beat entry.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from scraper.models import ScrapeJob, SessionLog, Step, ToolCallLog

logger = logging.getLogger(__name__)

# Dead-end statuses that share the short window. Deliberately narrower than
# models._TERMINAL_JOB_STATUSES (which includes the parked captcha/akamai
# states — see module docstring).
_PURGEABLE_FAILED_GROUP = (ScrapeJob.STATUS_FAILED, ScrapeJob.STATUS_CANCELLED)

_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

DEFAULT_DAYS_FAILED = 7
DEFAULT_DAYS_COMPLETED = 90
DEFAULT_BATCH_SIZE = 500

_THREAD_PREFIX = "job-"


def _thread_id(job_id: int) -> str:
    return f"{_THREAD_PREFIX}{job_id}"


def checkpoint_tables_present() -> bool:
    """True only if all three langgraph tables exist (runtime-created)."""
    with connection.cursor() as cur:
        for table in _CHECKPOINT_TABLES:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"public.{table}"])
            if not cur.fetchone()[0]:
                return False
    return True


def _expired_page(
    now,
    days_failed: int,
    days_completed: int,
    after_id: int,
    limit: int,
) -> list[int]:
    """Next id-ordered page of expired dead-end jobs (cursor-paginated)."""
    failed_cutoff = now - timezone.timedelta(days=days_failed)
    completed_cutoff = now - timezone.timedelta(days=days_completed)
    qs = (
        ScrapeJob.objects.filter(
            Q(status__in=_PURGEABLE_FAILED_GROUP, created_at__lt=failed_cutoff)
            | Q(
                status=ScrapeJob.STATUS_COMPLETED,
                created_at__lt=completed_cutoff,
            )
        )
        .filter(id__gt=after_id)
        .order_by("id")
        .values_list("id", flat=True)[:limit]
    )
    return list(qs)


def _delete_checkpoint_threads(thread_ids: list[str]) -> dict[str, int]:
    """Delete the checkpoint trio rows for the given threads (blobs first —
    the bulk carriers — then writes, then the parent rows)."""
    counts: dict[str, int] = {}
    with connection.cursor() as cur:
        for table in ("checkpoint_blobs", "checkpoint_writes", "checkpoints"):
            cur.execute(
                f"DELETE FROM {table} WHERE thread_id = ANY(%s)",  # noqa: S608
                [thread_ids],
            )
            counts[table] = max(cur.rowcount, 0)
    return counts


def _delete_job_children(job_ids: list[int]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, manager in (
        ("sessionlog_rows", SessionLog.objects),
        ("toolcalllog_rows", ToolCallLog.objects),
        ("step_rows", Step.objects),
    ):
        total, _ = manager.filter(job_id__in=job_ids).delete()
        counts[name] = total
    return counts


def _dry_run_report(
    now,
    days_failed: int,
    days_completed: int,
) -> dict:
    """Count what a live sweep would remove — delete nothing."""
    failed_cutoff = now - timezone.timedelta(days=days_failed)
    completed_cutoff = now - timezone.timedelta(days=days_completed)
    selector = Q(
        status__in=_PURGEABLE_FAILED_GROUP, created_at__lt=failed_cutoff
    ) | Q(status=ScrapeJob.STATUS_COMPLETED, created_at__lt=completed_cutoff)
    expired = ScrapeJob.objects.filter(selector)
    job_ids = list(expired.values_list("id", flat=True))

    report: dict = {
        "dry_run": True,
        "jobs_expired": len(job_ids),
        "checkpoint_threads": 0,
        "sessionlog_rows": SessionLog.objects.filter(job_id__in=job_ids).count(),
        "toolcalllog_rows": ToolCallLog.objects.filter(job_id__in=job_ids).count(),
        "step_rows": Step.objects.filter(job_id__in=job_ids).count(),
    }
    if job_ids and checkpoint_tables_present():
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id = ANY(%s)",
                [[_thread_id(i) for i in job_ids]],
            )
            report["checkpoint_threads"] = cur.fetchone()[0]
    return report


def purge_retention(
    days_failed: int = DEFAULT_DAYS_FAILED,
    days_completed: int = DEFAULT_DAYS_COMPLETED,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """Sweep heavy rows behind expired dead-end jobs. Returns a report dict.

    Live mode deletes in id-ordered batches (see module docstring for why
    the id cursor is load-bearing). Dry-run mode deletes nothing.
    """
    now = timezone.now()
    if dry_run:
        return _dry_run_report(now, days_failed, days_completed)

    tables_present = checkpoint_tables_present()
    report: dict = {
        "dry_run": False,
        "checkpoint_tables_present": tables_present,
        "jobs_purged": 0,
        "checkpoint_rows_purged": 0,
        "sessionlog_rows_purged": 0,
        "toolcalllog_rows_purged": 0,
        "step_rows_purged": 0,
    }

    after_id = 0
    while True:
        job_ids = _expired_page(now, days_failed, days_completed, after_id, batch_size)
        if not job_ids:
            break
        after_id = job_ids[-1]
        with transaction.atomic():
            if tables_present:
                threads = [_thread_id(i) for i in job_ids]
                report["checkpoint_rows_purged"] += sum(
                    _delete_checkpoint_threads(threads).values()
                )
            for name, total in _delete_job_children(job_ids).items():
                report[f"{name}_purged"] += total
            report["jobs_purged"] += len(job_ids)
        logger.info(
            "retention batch: %d job(s) purged (through id %d)",
            len(job_ids),
            after_id,
        )

    report["django_sessions_purged"] = _clear_django_sessions(now)
    report["trash_entries_purged"] = _sweep_workspace_trash(
        now, days_failed, days_completed
    )
    return report


def _clear_django_sessions(now) -> int:
    """Fold the clearsessions ritual into the same sweep (prod had 59k
    stale django_session rows / 50MB — same never-cleaned story)."""
    try:
        from django.contrib.sessions.models import Session
    except ImportError:  # sessions app not installed
        return 0
    total, _ = Session.objects.filter(expire_date__lt=now).delete()
    return total


def _sweep_workspace_trash(now, days_failed: int, days_completed: int) -> int:
    """[wave-40 T11] Age out tombstoned workspaces (``workspace/_trash/``).

    A tombstone is the preserved directory a blocked delete renamed aside
    (``invocation_registry.tombstone_into_trash``), so it is evidence, not
    live scratch — but it is also the only copy of what a zombie or a sibling
    had in flight. Same window semantics as the DB rows above: the owner's
    COMPLETED tombstone lives ``days_completed``, anything else
    ``days_failed``, and a tombstone whose job is still in a resumable status
    is NEVER swept (that sibling may still come back for it). The stamp in
    the name (``{slug}-{job_id}-{ns}``) is the age; a directory whose name
    the registry did not write is left alone.
    """
    from agents.invocation_registry import TRASH_DIRNAME, parse_trash_name

    trash_root = Path(str(settings.PROJECT_ROOT)) / "workspace" / TRASH_DIRNAME
    if not trash_root.is_dir():
        return 0
    purged = 0
    for entry in sorted(trash_root.iterdir()):
        if not entry.is_dir():
            continue
        parsed = parse_trash_name(entry.name)
        if parsed is None:
            continue
        _slug, job_id, stamp = parsed
        age_days = (now.timestamp() - stamp) / 86400
        window_days = days_failed
        if job_id:
            status = (
                ScrapeJob.objects.filter(pk=job_id)
                .values_list("status", flat=True)
                .first()
            )
            if status == ScrapeJob.STATUS_COMPLETED:
                window_days = days_completed
            elif status is not None and status not in _PURGEABLE_FAILED_GROUP:
                continue  # pending/running/waiting_approval/parked — hands off
        if age_days < window_days:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            purged += 1
            logger.info(
                "retention: swept tombstone %s (%.1fd old, window %dd)",
                entry.name, age_days, window_days,
            )
    return purged
