"""[wave-58 class 3] pc coherence + cleanup honesty + promotion receipts.

Batch 613-616 evidence:

- job 613's cleanup_report claimed 11 extracted against Job.product_count=3
  — and cleanup's "extracted" is FREE LLM TEXT (build_cleanup_message
  injects no counts), so nothing ever reconciled it.
- A FAILED row's product_count is never reconciled against the final
  artifact: the COMPLETED arm (wave-55 C4) re-reads the file, the FAILED
  arm keeps whatever the state/override/rescue last credited. pc must
  describe the final bytes in BOTH directions — an inflated 3 over a 0-row
  artifact and a deflated 1 over a 5-row artifact are the same lie.
- Completed url_list jobs promoted no scraper artifact and no artifact
  anywhere says why — `_promote_scraper` records non-promotion ONLY in
  log lines.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from scraper import tasks  # noqa: E402
from scraper.models import ScrapeJob  # noqa: E402  (django setup first)

pytestmark = pytest.mark.django_db


# ── harness (mirrors test_wave40_finalize_rescue) ────────────────────────


def _freeze_state(monkeypatch, state):
    class _FakeGraph:
        def get_state(self, config):
            return SimpleNamespace(values=state)

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def build_graph(self):
            return _FakeGraph()

        @staticmethod
        def get_config(thread_id):
            return {"configurable": {"thread_id": thread_id}}

    monkeypatch.setattr(tasks, "LangGraphService", _FakeService)


def _fm(monkeypatch, keys=(), payloads=None):
    """Stub the File Master client; return the write log.

    Mirrors test_wave40_finalize_rescue._fm: re-load the REAL src/artifacts
    module (test_f8_f16_output_selection installs a collection-time stub that
    shadows it), pin it in sys.modules AND on the package, patch there.
    """
    import importlib.util
    import pathlib

    payloads = payloads or {}
    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        pathlib.Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    writes: dict = {}
    monkeypatch.setattr(real, "list_keys", lambda prefix="": list(keys))
    monkeypatch.setattr(real, "read_text", lambda key: payloads.get(key, ""))
    monkeypatch.setattr(real, "exists", lambda key: key in payloads)
    monkeypatch.setattr(
        real, "write", lambda key, data: writes.update({key: bytes(data)}))
    return writes


def _no_fm(monkeypatch):
    """No File-Master evidence: the rescue arm stays out of the way."""
    _fm(monkeypatch)


def _job(**kw):
    kw.setdefault("status", ScrapeJob.STATUS_RUNNING)
    kw.setdefault("url", "https://x.example/p/1")
    kw.setdefault("site_name", "x-com")
    return ScrapeJob.objects.create(**kw)


def _output_file(records: list) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"products": records, "metadata": {}}, f)
    return path


def _row(i):
    return {"title": f"Dress {i}", "price": "$99",
            "url": f"https://x.example/p/{i}"}


def _no_close(monkeypatch):
    """`_finalize_job_failed`'s F4 `close_old_connections()` kills the test's
    transactional connection (the wave-37 note at test_no_items_finalize_
    failed_with_cause documents the same trap) — neutralize it for direct
    crash-path runs."""
    import django.db

    monkeypatch.setattr(django.db, "close_old_connections", lambda: None)


# ── 3a. a FAILED row's pc describes the final bytes ──────────────────────


class TestFailedRowPcCoherence:
    def test_failed_pc_corrects_an_fm_key_artifact(self, monkeypatch):
        """The wave-55 C4 correction reads the file for COMPLETED only, and
        the ground-truth override only follows a LOCAL path — a FAILED job
        whose output_file is a File Master key keeps whatever the state
        claimed. A deflated pc=1 over a 5-row artifact is the same lie as an
        inflated one."""
        key = "scrapers/x-com/output_2026-10-11_080000_000000_001.json"
        payload = json.dumps({"products": [_row(i) for i in range(5)]})
        _fm(monkeypatch, keys=[key], payloads={key: payload})
        job = _job(product_count=1, output_file=key)
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 1,
            "output_file": key, "input_mode": "url_list",
            "error_message": "scope violation",
        })
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 5

    def test_failed_local_rows_already_describe_bytes(self, monkeypatch):
        """Existing behavior (tasks.py ground-truth override) — a readable
        local artifact with rows already rewrites a FAILED count. Pinned so
        the new arm never regresses it."""
        _no_fm(monkeypatch)
        job = _job(product_count=1)
        path = _output_file([_row(i) for i in range(5)])
        job.output_file = path
        job.save(update_fields=["output_file"])
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 1,
            "output_file": path, "input_mode": "url_list",
            "error_message": "scope violation",
        })
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 5

    def test_failed_pc_zeroes_on_a_readable_empty_artifact(self, monkeypatch):
        _no_fm(monkeypatch)
        job = _job(product_count=3)
        path = _output_file([])
        job.output_file = path
        job.save(update_fields=["output_file"])
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 3,
            "output_file": path, "input_mode": "url_list",
            "error_message": "boom",
        })
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0

    def test_failed_pc_survives_an_unreadable_artifact(self, monkeypatch):
        """Unreadable bytes are not evidence of zero — the credited count
        stays (never invent evidence in either direction)."""
        _no_fm(monkeypatch)
        job = _job(product_count=4)
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("{not json at all")
        job.output_file = path
        job.save(update_fields=["output_file"])
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 4,
            "output_file": path, "input_mode": "url_list",
            "error_message": "boom",
        })
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.product_count == 4

    def test_pars_helper_distinguishes_readable_from_parsed(self):
        from scraper.tasks import _output_file_parses

        good = _output_file([_row(0)])
        assert _output_file_parses(good) is True
        assert _output_file_parses("") is False
        assert _output_file_parses(
            os.path.join(tempfile.gettempdir(), "nope-does-not-exist.json")
        ) is False

    def test_completed_arm_untouched(self, monkeypatch):
        """The wave-55 C4 COMPLETED behavior keeps its pins (a truthy
        file-count correction; an unreadable file never zeroes)."""
        _no_fm(monkeypatch)
        job = _job(product_count=7)
        path = _output_file([_row(i) for i in range(3)])
        job.output_file = path
        job.save(update_fields=["output_file"])
        _freeze_state(monkeypatch, {
            "execution_status": "SUCCESS", "product_count": 7,
            "output_file": path, "input_mode": "url_list",
            "error_message": "",
        })
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 3

    def test_failed_reconciles_on_the_crash_path_too(self, monkeypatch):
        """[critique S4] `_finalize_job_failed` (the worker-death /
        task_failure path) writes FAILED rows and never touches pc — the
        second finalize population. Same describe-the-bytes rule: a readable
        empty artifact drops the credited count to 0, real rows lift it."""
        _no_fm(monkeypatch)
        _no_close(monkeypatch)
        job = _job(product_count=2)
        path = _output_file([])
        job.output_file = path
        job.save(update_fields=["output_file"])
        tasks._finalize_job_failed(job.id, "worker died")
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0

    def test_crash_path_lifts_a_deflated_count(self, monkeypatch):
        _no_fm(monkeypatch)
        _no_close(monkeypatch)
        job = _job(product_count=1)
        path = _output_file([_row(i) for i in range(4)])
        job.output_file = path
        job.save(update_fields=["output_file"])
        tasks._finalize_job_failed(job.id, "worker died")
        job.refresh_from_db()
        assert job.product_count == 4

    def test_crash_path_keeps_count_when_artifact_unreadable(self, monkeypatch):
        _no_fm(monkeypatch)
        _no_close(monkeypatch)
        job = _job(product_count=3)
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("{broken")
        job.output_file = path
        job.save(update_fields=["output_file"])
        tasks._finalize_job_failed(job.id, "worker died")
        job.refresh_from_db()
        assert job.product_count == 3


# ── 3b. cleanup can no longer invent a count ─────────────────────────────


class TestCleanupCountHonesty:
    def test_cleanup_message_carries_the_deterministic_count(self):
        from agents.subagents import build_cleanup_message

        content = str(build_cleanup_message({
            "site_slug": "jjhouse",
            "url": "https://www.jjshouse.com/x",
            "product_count": 913,
        })[0].content)
        assert "913" in content
        assert "extracted" in content.lower()
        assert "deterministic" in content.lower()

    def test_cleanup_report_extracted_is_stamped(self, tmp_path):
        """Whatever the LLM wrote, the deterministic pc lands on the report
        (613's "11" against 3 becomes impossible)."""
        from agents.graph import _stamp_cleanup_report_extracted
        from django.test.utils import override_settings

        ws = tmp_path / "workspace" / "jjhouse"
        ws.mkdir(parents=True)
        (ws / "cleanup_report.json").write_text(json.dumps({
            "actions": ["copied files"], "extracted": 11,
        }), encoding="utf-8")
        with override_settings(PROJECT_ROOT=str(tmp_path)):
            _stamp_cleanup_report_extracted("jjhouse", 3)
        report = json.loads((ws / "cleanup_report.json").read_text())
        assert report["extracted"] == 3

    def test_stamp_is_best_effort_without_a_report(self, tmp_path):
        from agents.graph import _stamp_cleanup_report_extracted
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            # no report on disk — must not raise
            _stamp_cleanup_report_extracted("jjhouse", 3)

    def test_invoke_cleanup_calls_the_stamp(self):
        from agents import graph

        src = inspect.getsource(graph._invoke_cleanup)
        assert "_stamp_cleanup_report_extracted" in src


# ── 3c. every promotion outcome leaves a receipt ─────────────────────────


class TestPromotionReceipt:
    def _promote(self, tmp_path, monkeypatch, status, pc, with_draft=True):
        import src.artifacts as artifacts

        written = {}
        monkeypatch.setattr(
            artifacts, "write",
            lambda key, data: written.__setitem__(key, bytes(data)))
        monkeypatch.setattr(artifacts, "read", lambda key: b"# draft\n")
        monkeypatch.setattr(artifacts, "exists", lambda key: False)
        if with_draft:
            ws = tmp_path / "workspace" / "acme-com"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "scraper_draft.py").write_text("# draft\n")
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            from agents.graph import _promote_scraper

            _promote_scraper("acme-com", 581, status, None, product_count=pc)
        return written

    def test_non_success_writes_a_reason_receipt(self, tmp_path, monkeypatch):
        written = self._promote(tmp_path, monkeypatch, "FAILED", 0)
        receipts = [
            k for k in written if k.endswith("scraper-581.promotion.json")
        ]
        assert receipts, f"expected a promotion receipt, got {sorted(written)}"
        receipt = json.loads(written[receipts[0]].decode())
        assert receipt["promoted"] is False
        assert receipt["execution_status"] == "FAILED"
        assert receipt["reason"]

    def test_zero_yield_success_writes_a_reason_receipt(
            self, tmp_path, monkeypatch):
        written = self._promote(tmp_path, monkeypatch, "SUCCESS", 0)
        receipt = json.loads(
            written["scrapers/acme-com/jobs/scraper-581.promotion.json"]
            .decode())
        assert receipt["promoted"] is False
        assert "zero" in receipt["reason"].lower()

    def test_real_promotion_writes_a_positive_receipt(
            self, tmp_path, monkeypatch):
        written = self._promote(tmp_path, monkeypatch, "SUCCESS", 12)
        receipt = json.loads(
            written["scrapers/acme-com/jobs/scraper-581.promotion.json"]
            .decode())
        assert receipt["promoted"] is True
        assert receipt["product_count"] == 12

    def test_missing_draft_writes_a_receipt_too(self, tmp_path, monkeypatch):
        written = self._promote(
            tmp_path, monkeypatch, "SUCCESS", 12, with_draft=False)
        receipts = [
            k for k in written if k.endswith("scraper-581.promotion.json")
        ]
        assert receipts, "no draft must still be recorded, not silent"
        receipt = json.loads(written[receipts[0]].decode())
        assert receipt["promoted"] is False
        assert "draft" in receipt["reason"].lower()

    def test_production_promotion_gate_unchanged(self, tmp_path, monkeypatch):
        """The job-77 gate keeps its semantics — the receipt rides ALONGSIDE
        it, never instead of it."""
        written = self._promote(tmp_path, monkeypatch, "SUCCESS", 0)
        assert not any(
            k.endswith("/acme-com/scraper.py") for k in written)
        written = self._promote(tmp_path, monkeypatch, "SUCCESS", 12)
        assert any(k.endswith("/acme-com/scraper.py") for k in written)
