"""Spec-gap closures: scraper-code endpoint + missing event emissions.

Locks (async_api.yaml message catalog vs emit inventory):
- GET /api/v1/jobs/{id}/scraper-code: json (metadata + code string) and
  raw (text/x-python attachment) formats; 404 when never promoted;
  cross-tenant 404
- job.inprogress emitted at the RUNNING transition (tasks.py), exactly
  once (resume paths re-emit idempotently via dedupe)
- job.phase.updated emitted from _notify_phase (the single choke point)
- artifact(scraper_code) + job.scraper_ready emitted at cleanup
  promotion (in-graph, NOT reconciler-late) on SUCCESS with a real
  per-job key
- failed promotion emits nothing (no scraper_ready without a scraper)
"""
from __future__ import annotations

import json
import os
import sys
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
from scraper.api.writers import get_job_scraper_code  # noqa: E402

rf = RequestFactory()


@pytest.fixture
def partner(db):
    import secrets

    u = User.objects.create_user("_t_gap_" + secrets.token_hex(3), password="x")
    raw = "pk_" + secrets.token_hex(16)
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    return u, raw


def _job(u, **kw):
    d = dict(
        url="https://e.com/i", user=u, created_via="api", status="completed",
        input_mode="url_list", page_type="product",
        site_folder="scrapers/gap", scraper_file="scrapers/gap/jobs/scraper-9.py",
    )
    d.update(kw)
    return models.ScrapeJob.objects.create(**d)


class TestScraperCodeEndpoint:
    def test_json_format(self, partner, db):
        u, raw = partner
        job = _job(u)
        code = "def main():\n    pass\n"
        with patch("scraper.api.writers._fm_read_text", return_value=code):
            req = rf.get(f"/api/v1/jobs/{job.id}/scraper-code", HTTP_X_API_KEY=raw)
            r = get_job_scraper_code(req, job.id)
        assert r.status_code == 200
        body = json.loads(r.content)
        assert body["code"] == code
        assert body["filename"] == "scraper-9.py"

    def test_raw_format(self, partner, db):
        u, raw = partner
        job = _job(u)
        code = "print('x')\n"
        with patch("scraper.api.writers._fm_read_text", return_value=code):
            req = rf.get(f"/api/v1/jobs/{job.id}/scraper-code?format=raw", HTTP_X_API_KEY=raw)
            r = get_job_scraper_code(req, job.id)
        assert r.status_code == 200
        assert "python" in r["Content-Type"]
        assert "attachment" in r.get("Content-Disposition", "")

    def test_404_never_promoted(self, partner, db):
        u, raw = partner
        job = _job(u, scraper_file="")
        req = rf.get(f"/api/v1/jobs/{job.id}/scraper-code", HTTP_X_API_KEY=raw)
        r = get_job_scraper_code(req, job.id)
        assert r.status_code == 404

    def test_404_file_missing_in_fm(self, partner, db):
        u, raw = partner
        job = _job(u)
        with patch("scraper.api.writers._fm_read_text", side_effect=Exception("fm down")):
            req = rf.get(f"/api/v1/jobs/{job.id}/scraper-code", HTTP_X_API_KEY=raw)
            r = get_job_scraper_code(req, job.id)
        assert r.status_code == 404

    def test_cross_tenant_404(self, partner, db):
        u, raw = partner
        other = User.objects.create_user("_t_gapo", password="x")
        job = _job(other)
        req = rf.get(f"/api/v1/jobs/{job.id}/scraper-code", HTTP_X_API_KEY=raw)
        assert get_job_scraper_code(req, job.id).status_code == 404


class TestInprogressEmission:
    def test_running_transition_emits(self, partner, db):
        """_emit_running_transition (called at the RUNNING transition)
        writes exactly one job.inprogress outbox row, deduped."""
        import scraper.tasks as tasks_mod

        u, raw = partner
        job = _job(u, status="running")
        tasks_mod._emit_running_transition(job)
        tasks_mod._emit_running_transition(job)  # resume path: idempotent
        rows = models.EventOutbox.objects.filter(
            job=job, event_type="job.inprogress"
        )
        assert rows.count() == 1
        assert rows.first().dedupe_key == "inprogress"


class TestPhaseEmission:
    def test_notify_phase_emits(self, partner, db):
        """_notify_phase emits job.phase.updated with the enum token."""
        u, raw = partner
        job = _job(u, status="running")
        from agents import graph as graph_mod

        rows_before = models.EventOutbox.objects.filter(job=job).count()
        graph_mod._notify_phase(job.id, "code_tester", "done")
        row = models.EventOutbox.objects.filter(
            job=job, event_type="job.phase.updated"
        ).order_by("-id").first()
        assert row is not None, "phase event not emitted"
        assert row.payload["data"]["phase"] == "testing"
        assert row.payload["data"]["phase_status"] == "done"


class TestPromotionEmissions:
    def test_promotion_emits_scraper_ready_and_artifact(self, partner, db, tmp_path):
        """_promote_scraper (SUCCESS) emits job.scraper_ready + artifact(scraper_code)."""
        u, raw = partner
        job = _job(u, status="running")
        ws = tmp_path / "workspace" / "gap"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("def main(): pass\n")

        import agents.graph as graph_mod

        # Collection-time stubs (test_f8 installs a 2-attr src.artifacts in
        # sys.modules BEFORE any test runs) shadow the real module for every
        # later direct import. Re-load the REAL module and pin it in
        # sys.modules for the duration so the code-under-test + the patches
        # target the same object.
        import importlib.util
        import sys as _sys

        _spec = importlib.util.spec_from_file_location(
            "src.artifacts", os.path.join(ROOT, "src", "artifacts.py")
        )
        _real = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_real)
        _sys.modules["src.artifacts"] = _real
        try:
            with patch.object(graph_mod, "_get_project_root", return_value=str(tmp_path)), \
                 patch.object(_real, "write"), patch.object(_real, "read", return_value=b"x"):
                key = graph_mod._promote_scraper(
                    "gap", job.id, "SUCCESS", archive_key=None
                )
        finally:
            _sys.modules["src.artifacts"] = _real  # leave the REAL one behind
        assert key is not None
        sr = models.EventOutbox.objects.filter(
            job=job, event_type="job.scraper_ready"
        ).first()
        assert sr is not None, "job.scraper_ready not emitted in-graph"
        art = models.EventOutbox.objects.filter(
            job=job, event_type="job.artifact.available",
            payload__data__kind="scraper_code",
        ).first()
        assert art is not None, "artifact(scraper_code) not emitted"

    def test_failed_promotion_emits_nothing(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u, status="running")
        import agents.graph as graph_mod
        import importlib.util
        import sys as _sys

        ws = tmp_path / "workspace" / "gap"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "scraper_draft.py").write_text("def main(): pass\n")
        _spec = importlib.util.spec_from_file_location(
            "src.artifacts", os.path.join(ROOT, "src", "artifacts.py")
        )
        _real = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_real)
        _sys.modules["src.artifacts"] = _real
        try:
            with patch.object(graph_mod, "_get_project_root", return_value=str(tmp_path)), \
                 patch.object(_real, "write"), patch.object(_real, "read", side_effect=Exception("gone")):
                graph_mod._promote_scraper("gap", job.id, "FAILED", archive_key=None)
        finally:
            _sys.modules["src.artifacts"] = _real
        assert models.EventOutbox.objects.filter(
            job=job, event_type="job.scraper_ready"
        ).count() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestDagsterSkipGuard:
    def test_skips_on_failed_execution(self, partner, db):
        """P2 from the Railway job-1 forensics: dagster_converter burned 6
        minutes converting a scraper for a FAILED job. Same guard pattern
        as skill_learner/store_job_listings."""
        import agents.graph as graph_mod

        u, raw = partner
        job = _job(u, status="failed")
        from scraper.models import Step

        before = Step.objects.filter(job=job, phase="dagster_converter").count()
        out = graph_mod._invoke_dagster_converter(
            {"job_id": job.id, "site_slug": "gap", "execution_status": "FAILED"}, None
        )
        # no Step row was created (the running notify never fired)
        after = Step.objects.filter(job=job, phase="dagster_converter").count()
        assert before == after
        assert out == {"messages": []}

    def test_skips_when_not_opted_in(self, partner, db):
        """[dagster-opt-in] the phase is opt-in now: no ``dagster_enabled`` in
        state → short-circuit BEFORE the running notify, even on a SUCCESS
        job (the old unconditional behavior). Bare dict on purpose — mirrors
        how the graph harness drives this node."""
        import agents.graph as graph_mod

        u, _raw = partner
        job = _job(u, status="completed")
        from scraper.models import Step

        before = Step.objects.filter(job=job, phase="dagster_converter").count()
        out = graph_mod._invoke_dagster_converter(
            {"job_id": job.id, "site_slug": "gap", "execution_status": "SUCCESS"},
            None,
        )
        after = Step.objects.filter(job=job, phase="dagster_converter").count()
        assert before == after
        assert out == {"messages": []}
