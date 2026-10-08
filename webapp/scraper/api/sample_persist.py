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


def _job_contract_fields(job) -> list[str]:
    """The fields THIS job asked for — the persisted mapping when the intake
    resolver wrote one, else the raw chip list. Same precedence the finalize
    stamps use, so a gate and a stamp can never read two different contracts."""
    try:
        from src.field_mapping import resolved_fields_from_mapping

        resolved = resolved_fields_from_mapping(job.field_mapping)
    except Exception:
        resolved = None
    return list(resolved) if resolved else list(job.target_fields or [])


def _null_core_fields(job, records: list) -> list[str]:
    """[wave-55 GAP-4] Core columns empty on EVERY record (the 1219 shape).

    The router's own predicate — the same helper that fails the run's coverage
    gate a moment later — so a column this gate calls dead is the column the
    router calls dead. [round-8 GAP-1b] The judged set is the job's OWN
    contract intersected with the registry core (``core_judged_set``), exactly
    what the router gate and both finalize stamps seed it with: the wholesale
    registry core vetoed ``availability``/``currency`` on a title+price+url job
    and suppressed an honest sample. ``[]`` when the helpers cannot be
    imported (the sample then keeps today's behavior rather than blinding the
    partner lane).
    """
    try:
        from agents.nodes.route_after_testing import null_core_columns

        from src.field_mapping import core_judged_set
    except Exception:
        return []
    page_type = getattr(job, "page_type", "") or "product"
    return null_core_columns(
        records, page_type,
        core_field_names=core_judged_set(_job_contract_fields(job), page_type),
    )


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
        with open(src, encoding="utf-8") as fh:
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
    # [wave-55 GAP-4] This hook runs inside _invoke_code_tester — BEFORE
    # route_after_testing — and _is_pass trusts the verdict alone, so the 1219
    # shape (10/10 rows with price=None under a "100% CORRECT" claim) published
    # 5 null-price records to the partner FM and emitted job.sample_ready; the
    # M1 veto that fired moments later could not recall them. A core column
    # empty on every record is not a sample.
    nulls = _null_core_fields(job, records)
    if nulls:
        for _field in nulls:
            logger.info(
                "partner sample: skipped, core field %s null on %d/%d rows "
                "(job %s)", _field, len(records), len(records), job.id,
            )
        return False
    # [round-10 GAP-6] The sample is a publishing surface too, and a string
    # price is non-null — the audit's 1208/1215/1176 `{"price": "$3.99"}` row
    # cleared the null-core gate and shipped a string to the partner. Same gate
    # the finalize stamps apply: repair the price in place, drop the rows that
    # cannot be honoured, disclose the duplicate-title shape — and a sample
    # with nothing left is not a sample.
    gate_notes: list = []
    try:
        from scraper.tasks import _gate_output_rows, _variant_collapse_note

        records, gate_notes, _changed = _gate_output_rows(records)
        collapse = _variant_collapse_note(records)
        if collapse:
            gate_notes.append(collapse)
    except Exception as exc:
        logger.warning(
            "partner sample: price gate skipped for job %s: %s", job.id, exc)
        gate_notes = []
    else:
        if not records:
            logger.info(
                "partner sample: every record failed the price gate (job %s)",
                job.id,
            )
            return False
    sample = records[:MAX_SAMPLE_RECORDS]
    key = f"scrapers/{slug}/samples/sample-{job.id}.json"
    payload = {"records": sample, "source_output": os.path.basename(src)}
    if gate_notes:
        payload["gate_notes"] = gate_notes
    try:
        _fm_write(key, payload)
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
