"""Partner sample persistence + endpoint (slice 1a-ii).

Locks (fold B1 + A §6):
- _persist_partner_sample: writes scrapers/{slug}/samples/sample-{job_id}.json
  to the File Master from the workspace output file — ONLY on a PASS report
  (the pass-gate: FAILED retry reports must not write/overwrite)
- emit of job.sample_ready + artifact(sample) rides the same hook, dedupe
  key sample:{job_id} → retry cycles idempotent (first pass wins)
- GET /api/v1/jobs/{id}/sample: 200 records while live with a persisted
  sample; 404 not_ready before; never after failure (m4 gate at REST level)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from scraper import models  # noqa: E402
from scraper.api.writers import get_job_sample  # noqa: E402
from scraper.api.sample_persist import persist_partner_sample  # noqa: E402

rf = RequestFactory()


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_smp", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    return u, raw


def _job(u, status="running", slug_site="sampletest"):
    return models.ScrapeJob.objects.create(
        url="https://www.example.com/i", user=u, created_via="api",
        status=status, input_mode="url_list", page_type="product",
        site_folder=f"workspace/{slug_site}",
    )


PASS_REPORT = {"overall_assessment": "PASS", "ready_for_execution": True}
FAIL_REPORT = {"overall_assessment": "FAIL", "ready_for_execution": False}


class TestPersistHook:
    def test_pass_report_writes_sample_and_emits(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output_x.json").write_text(json.dumps(
            {"site": "x", "products": [{"title": "Boot", "price": "10"}]}
        ))
        written = {}

        def fake_write(key, payload):
            written[key] = payload

        with patch("scraper.api.sample_persist._workspace_output", return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write", side_effect=fake_write):
            persist_partner_sample(job, slug="sampletest", report=PASS_REPORT)
        assert f"scrapers/sampletest/samples/sample-{job.id}.json" in written
        recs = written[f"scrapers/sampletest/samples/sample-{job.id}.json"]["records"]
        assert recs[0]["title"] == "Boot"
        row = models.EventOutbox.objects.filter(
            job=job, event_type="job.sample_ready"
        ).first()
        assert row is not None
        assert row.payload["data"]["item_count"] == 1

    def test_fail_report_writes_nothing(self, partner, db):
        u, raw = partner
        job = _job(u)
        with patch("scraper.api.sample_persist._fm_write") as fw:
            persist_partner_sample(job, slug="sampletest", report=FAIL_REPORT)
        assert fw.call_count == 0
        assert models.EventOutbox.objects.filter(job=job).count() == 0

    def test_retry_idempotent_first_wins(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u)
        ws = tmp_path / "ws"; ws.mkdir()
        (ws / "output_x.json").write_text(json.dumps({"products": [{"title": "A"}]}))
        (ws / "output_x.json").write_text(json.dumps({"products": [{"title": "B"}]}))
        with patch("scraper.api.sample_persist._workspace_output", return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write"):
            persist_partner_sample(job, slug="sampletest", report=PASS_REPORT)
            persist_partner_sample(job, slug="sampletest", report=PASS_REPORT)
        assert models.EventOutbox.objects.filter(job=job, event_type="job.sample_ready").count() == 1

    def test_internal_job_no_emit(self, partner, db):
        u, raw = partner
        job = _job(u)
        job.created_via = "intake"
        job.save()
        with patch("scraper.api.sample_persist._fm_write"):
            persist_partner_sample(job, slug="s", report=PASS_REPORT)
        assert models.EventOutbox.objects.count() == 0


class TestSampleEndpoint:
    def _req(self, u, raw, job_id):
        return rf.get(f"/api/v1/jobs/{job_id}/sample", HTTP_X_API_KEY=raw)

    def test_200_with_records(self, partner, db):
        u, raw = partner
        job = _job(u)
        models.Step.objects.create(job=job, phase="testing", status="done",
                                   completed_at=job.created_at)
        with patch("scraper.api.writers._fm_read_json") as fr:
            fr.return_value = {"records": [{"title": "Boot"}]}
            r = get_job_sample(self._req(u, raw, job.id), job.id)
        assert r.status_code == 200
        body = json.loads(r.content)
        assert body["records"][0]["title"] == "Boot"
        assert "job_id" in body and "record_count" in body

    def test_404_not_ready_before_testing(self, partner, db):
        u, raw = partner
        job = _job(u)  # no testing step
        r = get_job_sample(self._req(u, raw, job.id), job.id)
        assert r.status_code == 404
        assert json.loads(r.content)["code"] == "not_ready"

    def test_404_terminal_failed(self, partner, db):
        """m4: terminal failed job with stamped testing step — REST must NOT
        claim a sample (state-gate at the endpoint level)."""
        u, raw = partner
        job = _job(u, status="failed")
        models.Step.objects.create(job=job, phase="testing", status="done",
                                   completed_at=job.created_at)
        r = get_job_sample(self._req(u, raw, job.id), job.id)
        assert r.status_code == 404

    def test_404_when_no_file_persisted(self, partner, db):
        u, raw = partner
        job = _job(u)
        models.Step.objects.create(job=job, phase="testing", status="done",
                                   completed_at=job.created_at)
        with patch("scraper.api.writers._fm_read_json", return_value=None):
            r = get_job_sample(self._req(u, raw, job.id), job.id)
        assert r.status_code == 404


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
