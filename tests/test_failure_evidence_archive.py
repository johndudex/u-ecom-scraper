"""[pillowtalk gap → jobs 71/76 RCA] Failed jobs must keep their test report.

Cleanup archives the production scraper and (since the per-job copy) the
draft — but a FAILED job's ``test_report.json`` lived only in
``workspace/{slug}/``, which the next job's setup wipes. Both job 71 and
job 76 post-mortems had to reconstruct the cascade from truncated session
logs because the report was gone. On non-SUCCESS the report is now copied to
``scrapers/{slug}/analysis/test_report-{job_id}.json`` (corrupt-guarded, like
every FM publish). SUCCESS skips — the tracker flow already publishes it.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()


@pytest.fixture()
def fm(monkeypatch):
    """Stub the File-Master write surface; capture (key, bytes)."""
    import src.artifacts as artifacts

    written = {}
    monkeypatch.setattr(
        artifacts, "write", lambda key, data: written.__setitem__(key, data)
    )
    return written


def _workspace(tmp_path, report: dict | None):
    ws = tmp_path / "workspace" / "acme-com"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "scraper_draft.py").write_text("# draft\n")
    if report is not None:
        (ws / "test_report.json").write_text(json.dumps(report))
    return ws


def _archive(tmp_path, fm, status="FAILED", slug="acme-com", job_id=71):
    from django.test.utils import override_settings

    from webapp.agents.graph import _archive_failure_evidence

    with override_settings(PROJECT_ROOT=str(tmp_path)):
        _archive_failure_evidence(slug, job_id, status)


class TestFailureEvidenceArchive:
    def test_failed_job_archives_test_report(self, tmp_path, fm):
        report = {"overall_assessment": "FAIL", "results": {}}
        _workspace(tmp_path, report)
        _archive(tmp_path, fm, status="FAILED")
        key = "scrapers/acme-com/analysis/test_report-71.json"
        assert key in fm
        assert json.loads(fm[key])["overall_assessment"] == "FAIL"

    def test_zero_item_success_shaped_status_also_archives(self, tmp_path, fm):
        """Anything non-SUCCESS keeps evidence (the gate is SUCCESS vs rest)."""
        _workspace(tmp_path, {"overall_assessment": "PASS"})
        _archive(tmp_path, fm, status="PARTIAL")
        assert "scrapers/acme-com/analysis/test_report-71.json" in fm

    def test_success_does_not_archive(self, tmp_path, fm):
        _workspace(tmp_path, {"overall_assessment": "PASS"})
        _archive(tmp_path, fm, status="SUCCESS")
        assert not fm

    def test_missing_report_is_a_silent_noop(self, tmp_path, fm):
        _workspace(tmp_path, None)
        _archive(tmp_path, fm, status="FAILED")
        assert not fm

    def test_corrupt_report_is_not_published(self, tmp_path, fm):
        ws = tmp_path / "workspace" / "acme-com"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "test_report.json").write_bytes(b"\xff\xfe not json {\x00")
        _archive(tmp_path, fm, status="FAILED")
        assert not fm


class TestCleanupCallsTheArchive:
    def test_cleanup_invokes_archive_after_promotion(self):
        """Wire-up pin: cleanup must pass THIS job's status to the archive."""
        import re

        src_path = os.path.join(ROOT, "webapp", "agents", "graph.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"^def _invoke_cleanup\(.*?(?=^def )", src, re.M | re.S)
        assert m, "_invoke_cleanup not found"
        body = m.group(0)
        assert "_archive_failure_evidence(slug, job_id," in body
        assert body.index("_promote_scraper(") < body.index("_archive_failure_evidence(")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
