"""[wave-40 T9] One chokepoint at finalize: productive evidence outranks a
stale failure verdict. Prod 770 (19 products, FAILED n=0), 762 (4-17), 765
(36/36 extraction destroyed by a 2-strike abort) all died at finalize.
_finalize_job(job) reads state from the langgraph checkpoint — the tests
freeze that read; no signature change."""

import json

import pytest

from scraper import tasks
from scraper.models import ScrapeJob

pytestmark = pytest.mark.django_db


def _freeze_state(monkeypatch, state):
    """Freeze _finalize_job's checkpoint read to `state`.

    The real call chain (tasks.py `_finalize_job`) is exactly:
    ``LangGraphService()`` → ``.build_graph()`` → ``.get_config(job.id)`` →
    ``graph.get_state(config).values`` — so the double fakes that chain, not
    the function that uses it. No signature change.
    """
    from types import SimpleNamespace

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


def _fm(monkeypatch, keys, payloads):
    """Stub the File Master client to `(keys, payloads)`; return the write log.

    ADAPT (implement-time, invocation only — the brief's stub semantics are
    unchanged): the literal string-path `monkeypatch.setattr(
    "src.artifacts.list_keys", ...)` raises AttributeError in the FULL-suite
    process, because tests/test_f8_f16_output_selection.py installs a
    2-attribute stub at COLLECTION time (`sys.modules.setdefault(
    "src.artifacts", <ModuleType ...>)`) that shadows the real module — the
    same trap T8's file documents. So: re-load the real module, pin it in
    sys.modules AND on the package (the two routes `import src.artifacts as
    artifacts` resolves through), and patch the functions on that object.
    monkeypatch restores everything after each test, so nothing leaks into
    sibling files. `write` records into a dict the caller can assert on
    (promotion is observed through it); `exists` is answered from payloads.
    """
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
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


# ── the ladder arm (pure function — no fixtures) ────────────────────────────


def test_ladder_rescue_outranks_execution_failed():
    st, diag = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0},
        already_terminal=False, was_cancelled=False,
        error_message="Phase-1 discovery crashed: re.error …",
        output_file="", rescue_count=19,
        rescue_file="scrapers/x/output_1.json")
    assert st == ScrapeJob.STATUS_COMPLETED and diag == ""


def test_ladder_never_rescues_a_cancelled_job():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0},
        already_terminal=False, was_cancelled=True,
        error_message="", output_file="", rescue_count=19,
        rescue_file="scrapers/x/output_1.json")
    assert st == ScrapeJob.STATUS_CANCELLED


def test_ladder_leaves_productive_execution_alone():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "SUCCESS", "product_count": 7},
        already_terminal=False, was_cancelled=False,
        error_message="", output_file="scrapers/x/output_1.json",
        rescue_count=19, rescue_file="scrapers/x/output_1.json")
    assert st == ScrapeJob.STATUS_COMPLETED   # normal path, no rescue involvement


def test_ladder_holds_the_min_count_line():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0,
         "input_mode": "navigation"},
        already_terminal=False, was_cancelled=False, error_message="",
        output_file="", rescue_count=2, rescue_file="scrapers/x/output_1.json")
    assert st != ScrapeJob.STATUS_COMPLETED   # 2 < 3 for navigation mode


def test_never_credits_completed_with_zero():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0},
        already_terminal=False, was_cancelled=False, error_message="boom",
        output_file="", rescue_count=0, rescue_file="")
    assert st == ScrapeJob.STATUS_FAILED      # invariant stands


# ── the finalize chokepoint ─────────────────────────────────────────────────


def test_770_finalize_completes_with_19_from_fm_output(monkeypatch, tmp_path):
    from django.conf import settings
    from django.utils import timezone

    job = ScrapeJob.objects.create(
        url="https://www.sephora.cz/",   # slug "sephora-cz" via _generate_slug
        status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=2))
    key = "scrapers/sephora-cz/output_2026-09-20_235455_000001_99.json"
    _fm(monkeypatch, [key], {key: json.dumps(
        {"products": [{"title": f"p{i}", "price": "9",
                       "url": f"https://x/p/{i}"} for i in range(19)]})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "workspace" / "sephora-cz").mkdir(parents=True)
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "last_tested_draft_fp": "abc", "input_mode": "navigation",
        "error_message": "noop draft twice"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_COMPLETED
    assert job.product_count == 19
    assert "noop draft" not in (job.error_message or "")
    assert job.output_file == key   # repointed at the evidence's FM key


def test_769_still_fails_honestly_when_only_prior_job_evidence_exists(
        monkeypatch, tmp_path):
    from django.conf import settings
    from django.utils import timezone

    job = ScrapeJob.objects.create(
        url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(minutes=30))
    _fm(monkeypatch, [], {})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "no_fresh_output": True, "input_mode": "navigation",
        "error_message": "DISCOVERY_ZERO"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_FAILED   # prior-job evidence is not this job's
    assert job.product_count == 0                  # nothing credited


def test_promotion_skips_an_uncompilable_draft(monkeypatch, tmp_path):
    """762 lesson: the crashing draft must never be promoted. compile() only
    catches SyntaxError — pair it with draft_call_violation when importable."""
    from django.conf import settings
    from django.utils import timezone

    job = ScrapeJob.objects.create(
        url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=1))
    # ADAPT (slug only): `_generate_slug("https://x.example/")` is "x-example",
    # and this checkpoint carries no state site_slug to override it — so the
    # per-job draft + output keys the evidence pass must find live under
    # scrapers/x-example/ (the brief's scrapers/x/ keys are unreachable
    # through the derived slug and would leave the fixture with no provenance).
    slug = "x-example"
    draft_key = f"scrapers/{slug}/jobs/scraper-{job.id}.py"
    out_key = f"scrapers/{slug}/output_2026-09-20_040000_000001_1.json"
    _fm(monkeypatch, [draft_key, out_key], {
        draft_key: "def broken(:\n    pass\n",   # SyntaxError
        out_key: json.dumps({"products": [
            {"title": "p", "price": "1", "url": "https://x/p/1"}]})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "input_mode": "url_list", "error_message": "crashed"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_COMPLETED   # items still rescued...
    assert job.product_count == 1
    promoted = tmp_path / "scrapers" / slug / "scraper.py"
    assert not promoted.exists()                       # ...but nothing promoted


# ── [wave-40 T9] additive implementer coverage ──────────────────────────────


def test_promotion_publishes_a_compiling_draft(monkeypatch, tmp_path):
    """The gate's positive half: the per-job FM draft compiles and carries no
    call violation → it becomes production scraper.py (FM key + local copy)."""
    from django.conf import settings
    from django.utils import timezone

    job = ScrapeJob.objects.create(
        url="https://y.example/", status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=1))
    slug = "y-example"
    draft_key = f"scrapers/{slug}/jobs/scraper-{job.id}.py"
    out_key = f"scrapers/{slug}/output_2026-09-20_040000_000001_2.json"
    source = "RESULT_LIMIT = 10\n"
    writes = _fm(monkeypatch, [draft_key, out_key], {
        draft_key: source,
        out_key: json.dumps({"products": [
            {"title": "p", "price": "1", "url": "https://y/p/1"}]})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "input_mode": "url_list", "error_message": "crashed"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_COMPLETED and job.product_count == 1
    assert writes[f"scrapers/{slug}/scraper.py"] == source.encode()
    assert (tmp_path / "scrapers" / slug / "scraper.py").read_text() == source


def test_kill_switch_disables_the_finalize_rescue(monkeypatch, tmp_path):
    """REAL_ITEMS_RESCUE_ENABLED=0 kills the whole chokepoint, not just the
    evidence helper — a switch-off job keeps its honest FAILED."""
    from django.conf import settings
    from django.utils import timezone

    monkeypatch.setenv("REAL_ITEMS_RESCUE_ENABLED", "0")
    job = ScrapeJob.objects.create(
        url="https://www.sephora.cz/", status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=2))
    key = "scrapers/sephora-cz/output_2026-09-20_235455_000001_99.json"
    _fm(monkeypatch, [key], {key: json.dumps(
        {"products": [{"title": f"p{i}", "price": "9",
                       "url": f"https://x/p/{i}"} for i in range(19)]})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "last_tested_draft_fp": "abc", "input_mode": "navigation",
        "error_message": "noop draft twice"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_FAILED
    assert job.product_count == 0
    assert job.output_file == ""


def test_rescue_never_clobbers_a_productive_run(monkeypatch, tmp_path):
    """State claims 7 items: the finalize seam must not re-point output_file
    or rewrite product_count even when evidence exists — the rescue exists
    only for the FAILED/zero verdict."""
    from django.conf import settings
    from django.utils import timezone

    job = ScrapeJob.objects.create(
        url="https://z.example/", status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=1))
    out_key = "scrapers/z-example/output_2026-09-20_050000_000001_3.json"
    _fm(monkeypatch, [out_key], {out_key: json.dumps(
        {"products": [{"title": f"p{i}", "price": "1",
                       "url": f"https://z/p/{i}"} for i in range(5)]})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "execution_status": "SUCCESS", "product_count": 7,
        "output_file": "scrapers/z-example/output_2026-09-20_050000_000001_3.json",
        "last_tested_draft_fp": "fp", "input_mode": "url_list"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_COMPLETED
    assert job.product_count == 7          # the run's own count stands
    assert job.output_file == (
        "scrapers/z-example/output_2026-09-20_050000_000001_3.json")
