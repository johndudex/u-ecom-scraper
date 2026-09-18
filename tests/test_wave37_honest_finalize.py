"""[wave-37 W37-3a] Artifact-evidence honest finalize.

Prod 593: every step done, product_count=10, outputs on disk — the
SoftTimeLimit kill landed before the status write and the generic
except-Exception finalize marked the job FAILED. finalize_from_artifacts
resolves the verdict from what exists: real records in the job's OUTPUT
ARTIFACT (the universal execution evidence — the plan's JobListing leg was
wrong, that model is jobs-dashboard-only) or, for the jobs domain, real
JobListing rows → completed (count = actual records, never the stale
counter); anything else → the honest failure path. Never invents a
COMPLETED with 0 items, never touches non-RUNNING rows.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from scraper.models import JobListing, ScrapeJob  # noqa: E402
from scraper.tasks import finalize_from_artifacts  # noqa: E402

pytestmark = pytest.mark.django_db


def _job(**kw) -> ScrapeJob:
    kw.setdefault("status", ScrapeJob.STATUS_RUNNING)
    return ScrapeJob.objects.create(
        url="https://example.com/p/1",
        site_name="example-com",
        **kw,
    )


def _output_file(records: list) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"products": records, "metadata": {}}, f)
    return path  # container /tmp: ephemeral, no cleanup needed


def test_output_artifact_with_records_finalize_completed():
    job = _job(product_count=10)  # stale counter must not win
    job.output_file = _output_file([{"title": "x", "price": "$1"}])
    job.save(update_fields=["output_file"])
    status = finalize_from_artifacts(job.id, "SoftTimeLimitExceeded")
    job.refresh_from_db()
    assert status == ScrapeJob.STATUS_COMPLETED
    assert job.status == ScrapeJob.STATUS_COMPLETED
    assert job.product_count == 1  # actual records, not the stale counter
    assert "SoftTimeLimitExceeded" not in (job.error_message or "")


def test_joblisting_rows_count_as_evidence_too():
    # jobs-domain evidence: store_job_listings rows without an artifact.
    job = _job()
    JobListing.objects.create(scrape_job=job, url="https://example.com/j/1", title="x")
    assert finalize_from_artifacts(job.id, "SoftTimeLimitExceeded") == (
        ScrapeJob.STATUS_COMPLETED
    )


def test_empty_artifact_is_not_evidence():
    job = _job(product_count=10)
    job.output_file = _output_file([])
    job.save(update_fields=["output_file"])
    assert finalize_from_artifacts(job.id, "SoftTimeLimitExceeded") == (
        ScrapeJob.STATUS_FAILED
    )


def test_no_items_finalize_failed_with_cause(monkeypatch):
    # The FAILED arm delegates to _finalize_job_failed, whose F4
    # close_old_connections() would kill the test's transactional
    # connection mid-atomic — spy on it instead of running it.
    calls: list[tuple] = []
    monkeypatch.setattr(
        "scraper.tasks._finalize_job_failed",
        lambda job_id, message, close_steps=False: calls.append((job_id, message)),
    )
    job = _job()
    status = finalize_from_artifacts(job.id, "SoftTimeLimitExceeded mid-graph")
    assert status == ScrapeJob.STATUS_FAILED
    assert calls == [(job.id, "SoftTimeLimitExceeded mid-graph")]


def test_never_overwrite_non_running_rows():
    for st in (
        ScrapeJob.STATUS_BROWSER_UNAVAILABLE,
        ScrapeJob.STATUS_COMPLETED,
        ScrapeJob.STATUS_CAPTCHA_BLOCKED,
    ):
        job = _job(status=st)
        assert finalize_from_artifacts(job.id, "x") == st
        job.refresh_from_db()
        assert job.status == st


def test_missing_row_is_noop():
    assert finalize_from_artifacts(999999, "x") == ""
