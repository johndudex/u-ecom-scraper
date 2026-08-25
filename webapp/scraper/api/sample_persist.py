"""Partner sample persistence (the relocated C1/B1 hook).

Called from _invoke_code_tester (graph.py) after _preserve_test_report —
NOT from field_confirmation, which is dead code for partner jobs
(sample_only=True bounces to run_execution before its sample block; the
fold's B1 fix). Writes the first PASS's records to the File Master and
emits job.sample_ready + artifact(sample) idempotently (dedupe
sample:{job_id} — first pass wins across retry cycles).
"""
from __future__ import annotations

import json
import logging
import os

from ..events import emit

logger = logging.getLogger("scraper.api")

MAX_SAMPLE_RECORDS = 5  # spec: FIELD_CONFIRMATION_SAMPLE_COUNT parity


def _workspace_output(slug: str):
    """Newest output_*.json in the job's workspace dir, or None."""
    import glob

    from django.conf import settings

    pattern = os.path.join(str(settings.PROJECT_ROOT), "workspace", slug, "output_*.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    return files[-1] if files else None


def _fm_write(key: str, payload: dict) -> None:
    import src.artifacts as artifacts

    artifacts.write_json(key, payload)


def _is_pass(report: dict) -> bool:
    if not isinstance(report, dict):
        return False
    if str(report.get("overall_assessment", "")).upper() == "PASS":
        return True
    return bool(report.get("ready_for_execution"))


def persist_partner_sample(job, slug: str, report: dict) -> bool:
    """On a PASS report: persist sample records + emit sample_ready.

    Returns True when the sample was persisted this call. Idempotent across
    retry cycles (dedupe key); internal jobs never emit (emit's gate).
    """
    if not _is_pass(report):
        return False
    src = _workspace_output(slug)
    if not src:
        logger.info("partner sample: no workspace output for %s (slug=%s)", job.id, slug)
        return False
    try:
        with open(src, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("partner sample: unreadable output for job %s: %s", job.id, exc)
        return False
    records = (
        data.get("products") or data.get("jobs") or data.get("articles")
        or data.get("threads") or data.get("results") or data.get("pages")
        or (data if isinstance(data, list) else [])
    )
    if not isinstance(records, list) or not records:
        logger.info("partner sample: no records in output for job %s", job.id)
        return False
    sample = records[:MAX_SAMPLE_RECORDS]
    key = f"scrapers/{slug}/samples/sample-{job.id}.json"
    try:
        _fm_write(key, {"records": sample, "source_output": os.path.basename(src)})
    except Exception as exc:  # FM down must not break the graph
        logger.warning("partner sample: FM write failed for job %s: %s", job.id, exc)
        return False
    emit(
        job, "job.sample_ready",
        {"item_count": len(sample), "sample_url": f"/api/v1/jobs/{job.id}/sample"},
        dedupe_key=f"sample:{job.id}",
    )
    emit(
        job, "job.artifact.available",
        {"kind": "sample", "records": sample, "url": f"/api/v1/jobs/{job.id}/sample"},
        dedupe_key=f"artifact:sample:{job.id}",
    )
    return True
