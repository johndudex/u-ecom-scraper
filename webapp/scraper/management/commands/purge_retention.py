"""Purge DB rows behind dead-end jobs (wave-35 retention).

The manual equivalent ran in the Railway console on 2026-09-16 after prod
postgres hit 25GB (97% LangGraph checkpoint tables). This command — and its
beat twin ``scraper.tasks.purge_retention`` — make that permanent: purge
checkpoints + SessionLog/ToolCallLog/Step rows for failed/cancelled jobs
older than ``--days-failed`` and completed jobs older than
``--days-completed``. Job rows and listings are kept (history intact).

Dry-run by default (reports counts); ``--write`` applies. Parked/live
statuses (pending, running, waiting_approval, captcha_blocked,
akamai_blocked, browser_unavailable) are never touched — resumable jobs
need their checkpoints.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from scraper.retention import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_DAYS_COMPLETED,
    DEFAULT_DAYS_FAILED,
    purge_retention,
)


class Command(BaseCommand):
    help = __doc__.splitlines()[0]

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-failed",
            type=int,
            default=getattr(settings, "RETENTION_DAYS_FAILED", DEFAULT_DAYS_FAILED),
            help="Purge failed/cancelled jobs older than this many days "
            "(default: RETENTION_DAYS_FAILED or %(default)s).",
        )
        parser.add_argument(
            "--days-completed",
            type=int,
            default=getattr(settings, "RETENTION_DAYS_COMPLETED", DEFAULT_DAYS_COMPLETED),
            help="Purge completed jobs older than this many days "
            "(default: RETENTION_DAYS_COMPLETED or %(default)s).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help="Jobs purged per committed batch (default %(default)s).",
        )
        parser.add_argument(
            "--write",
            action="store_true",
            help="Apply the purge (default is a dry-run report).",
        )

    def handle(self, *args, **options):
        report = purge_retention(
            days_failed=options["days_failed"],
            days_completed=options["days_completed"],
            dry_run=not options["write"],
            batch_size=options["batch_size"],
        )
        if report.get("dry_run"):
            self.stdout.write("DRY RUN — nothing deleted. Pass --write to apply.")
            self.stdout.write(f"jobs expired: {report['jobs_expired']}")
            self.stdout.write(f"checkpoint threads: {report['checkpoint_threads']}")
            self.stdout.write(f"sessionlog rows: {report['sessionlog_rows']}")
            self.stdout.write(f"toolcalllog rows: {report['toolcalllog_rows']}")
            self.stdout.write(f"step rows: {report['step_rows']}")
            return
        for key, value in report.items():
            self.stdout.write(f"{key}: {value}")
