"""[wave-50 T4] Rescue-arm honesty: credit what the NAMED evidence file
holds (never a sum across cycle files), judge rows by the core-value bar, and
mark scope shortfalls on the rescue completion arm.

Job 1116 (prod wonderbly): 9 in-window cycle outputs summed to 15 (5+0+5+0+…)
while the named file held 5 — the job recorded COMPLETED/15 shipping 5 junk
rows, with the tester's honest FAIL overridden and no [SCOPE-SHORTFALL]
(wired only at the override arm). Books-473 is the local instance (15 vs 5).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]


def _fm(monkeypatch, keys, payloads):
    """The test_wave40_finalize_rescue FM stub (module-reload variant)."""
    spec = importlib.util.spec_from_file_location(
        "src.artifacts", ROOT / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    writes: dict = {}
    monkeypatch.setattr(real, "list_keys", lambda prefix="": keys)
    monkeypatch.setattr(real, "read_text", lambda key: payloads.get(key, ""))
    monkeypatch.setattr(real, "exists", lambda key: key in payloads)
    monkeypatch.setattr(real, "write",
                        lambda key, data: writes.update({key: bytes(data)}))
    return writes


def _freeze_state(monkeypatch, state):
    from types import SimpleNamespace

    from scraper import tasks

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


def _job(scope="", scope_value=0):
    from django.utils import timezone

    from scraper.models import ScrapeJob, Step

    job = ScrapeJob.objects.create(
        url="https://www.wonderbly.com/",
        status=ScrapeJob.STATUS_RUNNING,
        scope=scope, scope_value=scope_value,
        started_at=timezone.now() - timezone.timedelta(hours=1))
    # [wave-55 C4] Every scenario here is "execution ran and wrote rows" —
    # the rescue now reads the `execution` Step's start as the production
    # window's existence proof, so the fixture carries it.
    started = timezone.now() - timezone.timedelta(minutes=5)
    Step.objects.create(
        job=job, phase="execution", status=Step.STATUS_DONE,
        started_at=started, completed_at=started)
    return job


def _stamped_rows(n):
    from django.utils import timezone

    return [{"title": f"p{i}", "price": "29.99",
             "url": f"https://x/p/{i}"} for i in range(n)]


def _payload(n):
    from django.utils import timezone

    return json.dumps({
        "site": {"scraped_at": timezone.now().isoformat()},
        "products": _stamped_rows(n)})


class TestNamedFileCount:
    def test_two_cycle_files_credit_the_best_single_file(self, monkeypatch,
                                                         tmp_path):
        """9-file wonderbly shape, minimized: two in-window cycle files
        (5 rows + 3 rows). Old behavior credited the SUM (8); the honest
        count is what the named file holds (5)."""
        from django.conf import settings

        from scraper import tasks
        from scraper.models import ScrapeJob

        job = _job()
        k5 = "scrapers/wonderbly-com/output_2026-09-29_120000_000001_1.json"
        k3 = "scrapers/wonderbly-com/output_2026-09-29_120100_000001_2.json"
        _fm(monkeypatch, [k5, k3], {k5: _payload(5), k3: _payload(3)})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        (tmp_path / "workspace" / "wonderbly-com").mkdir(parents=True)
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 0,
            "last_tested_draft_fp": "abc", "input_mode": "navigation",
            "error_message": "DISCOVERY_ZERO"})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 5          # the NAMED file, not 8
        named = json.loads(_payload_bytes(job.output_file))
        assert len(named["products"]) == job.product_count

    def test_output_file_always_describes_the_recorded_count(self, monkeypatch,
                                                             tmp_path):
        from django.conf import settings

        from scraper import tasks

        job = _job()
        key = "scrapers/wonderbly-com/output_2026-09-29_121500_000001_4.json"
        _fm(monkeypatch, [key], {key: _payload(4)})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 0,
            "last_tested_draft_fp": "abc", "input_mode": "url_list"})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.product_count == 4 and job.output_file == key


def _payload_bytes(key):
    import src.artifacts as artifacts

    return artifacts.read_text(key)


class TestRescueShortfallMarker:
    def test_rescue_marks_asked_10_shipped_5(self, monkeypatch, tmp_path):
        from django.conf import settings

        from scraper import tasks
        from scraper.models import ScrapeJob

        job = _job(scope="firstn", scope_value=10)
        key = "scrapers/wonderbly-com/output_2026-09-29_130000_000001_5.json"
        _fm(monkeypatch, [key], {key: _payload(5)})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 0,
            "last_tested_draft_fp": "abc", "input_mode": "navigation"})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 5
        assert "[SCOPE-SHORTFALL] asked 10, shipped 5" in (job.notes or "")

    def test_no_marker_when_scope_met(self, monkeypatch, tmp_path):
        from django.conf import settings

        from scraper import tasks

        job = _job(scope="firstn", scope_value=5)
        key = "scrapers/wonderbly-com/output_2026-09-29_131000_000001_6.json"
        _fm(monkeypatch, [key], {key: _payload(5)})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _freeze_state(monkeypatch, {
            "execution_status": "FAILED", "product_count": 0,
            "last_tested_draft_fp": "abc", "input_mode": "navigation"})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.product_count == 5
        assert "[SCOPE-SHORTFALL]" not in (job.notes or "")

    def test_override_arm_marker_still_fires(self, monkeypatch, tmp_path):
        """The wave-48 override arm keeps its marker (non-rescue path)."""
        from django.conf import settings

        from scraper import tasks

        job = _job(scope="firstn", scope_value=10)
        ws = tmp_path / "workspace" / "wonderbly-com"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("RESULT_LIMIT = 10\n")
        stamp = "2026-09-29_140000_000001_7"
        out = ws / f"output_{stamp}.json"
        out.write_text(_payload(5))
        _fm(monkeypatch, [], {})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _freeze_state(monkeypatch, {
            "execution_status": "SUCCESS", "product_count": 5,
            "output_file": str(out), "last_tested_draft_fp": "abc",
            "input_mode": "navigation", "site_slug": "wonderbly-com"})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert "[SCOPE-SHORTFALL]" in (job.notes or "")


class TestGoodRowsCoreValueBar:
    def test_currency_only_junk_rows_do_not_qualify(self):
        from scraper.tasks import _good_rows

        junk = [{"title": f"nav {i}", "price": "", "currency": "USD"}
                for i in range(5)]
        assert _good_rows({"products": junk}, ["price", "availability"],
                          "products") == []

    def test_zero_price_rows_qualify(self):
        from scraper.tasks import _good_rows

        rows = [{"title": f"s{i}", "price": 0.0, "status_code": 200}
                for i in range(5)]
        assert len(_good_rows({"products": rows}, ["price", "availability"],
                              "products")) == 5
