"""[wave-55 R4 / C4] Rescue-arm provenance — product_count must describe the
bytes `output_file` names, and only execution-phase rows may be credited.

Prod audit 2026-10-05: `_real_items_evidence` floored its window at JOB start
(tasks.py:1987) instead of EXECUTION start, so the finalize rescue
(tasks.py:2744) credited code_tester's sample rows as production output.
Prod 879/1217 shipped an `output_file` 4.4/8.8 min OLDER than execution; prod
1225 finished COMPLETED pc=9 with code_generation FAILED and execution
SKIPPED; prod 966 recorded pc=14 over a 2-row artifact; prod 896 carried three
disagreeing counts (10 / 548 / 618). The F8 precedent (run_execution.py,
`_find_newest_output(mtime_floor=start)`) has floored at subprocess start on
the execution path since wave-40 — this wave mirrors it on the rescue path.

Three sub-fixes, one file:
1. the evidence window opens at the execution Step's `started_at`;
2. the COMPLETED `product_count` is re-derived from the file `output_file`
   points at (one source of truth for the three writers);
3. a rescue requires an execution verdict — no `execution_status` in the
   final state means execution never ran, so there is no production output to
   attribute and the honest terminal (FAILED) stands.

The wave-19 stale-error scrub keeps its position (after the status decision,
before the rescue side effects) — a rescued COMPLETED still loses the stale
note, and a FAILED still keeps its reason.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from scraper import tasks  # noqa: E402
from scraper.models import ScrapeJob, Step  # noqa: E402

pytestmark = pytest.mark.django_db


# ── fixtures ────────────────────────────────────────────────────────────────


def _fm(monkeypatch, keys, payloads):
    """Stub the File Master client to `(keys, payloads)`.

    Same reload-and-pin dance the wave-40 files document: a string-path
    monkeypatch on `src.artifacts` loses to the collection-time stub that
    tests/test_f8_f16_output_selection.py installs in the FULL-suite process,
    so the real module is re-loaded and patched in place (sys.modules AND the
    package attribute — the two routes `import src.artifacts as artifacts`
    resolves through).
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    monkeypatch.setattr(real, "list_keys", lambda prefix="": keys)
    monkeypatch.setattr(real, "read_text", lambda key: payloads.get(key, ""))
    monkeypatch.setattr(real, "exists", lambda key: key in payloads)
    monkeypatch.setattr(real, "read_json",
                        lambda key: json.loads(payloads[key]) if key in
                        payloads else (_ for _ in ()).throw(ValueError(key)))
    # [wave-55 R6b] the finalize row gates rewrite a changed artifact through
    # `artifacts.write_json` — without this the suite PUTs test rows into the
    # real File Master (a leaked `scrapers/x-example/output_…json` key).
    monkeypatch.setattr(real, "write", lambda key, data: len(data))
    return payloads


def _rows(n):
    return [{"title": f"p{i}", "price": "$1", "url": f"https://x/p/{i}"}
            for i in range(n)]


def _job(started_at, **kw):
    kw.setdefault("url", "https://x.example/")
    kw.setdefault("status", ScrapeJob.STATUS_RUNNING)
    return ScrapeJob.objects.create(started_at=started_at, **kw)


def _execution_step(job, started_at, status=Step.STATUS_DONE):
    """The `execution` Step row, the phase clock `_notify_phase` stamps."""
    return Step.objects.create(
        job=job, phase="execution", status=status,
        started_at=started_at,
        completed_at=started_at if status == Step.STATUS_DONE else None,
    )


def _workspace_output(tmp_path, slug, name, rows, *, mtime=None):
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True, exist_ok=True)
    path = ws / name
    path.write_text(json.dumps({"products": rows}))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    (ws / "scraper_draft.py").write_text("# this job's draft")
    return path


def _window_name(minutes_ago, pid=1):
    base = (timezone.now() - timezone.timedelta(minutes=minutes_ago)).strftime(
        "output_%Y-%m-%d_%H%M%S_%f_")
    return f"{base}{pid}.json"


def _freeze_state(monkeypatch, state):
    """Freeze `_finalize_job`'s checkpoint read to `state` (wave-40 chain)."""
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


# ── sub-fix 1: the window opens at EXECUTION start, not job start ───────────


class TestWindowFloorsAtExecutionStart:
    def test_tester_rows_before_execution_start_are_not_credited(
            self, monkeypatch, tmp_path):
        """The C4 shape: code_tester wrote its sample rows 5 min before the
        execution subprocess started. Job-scoped they are (in the job's
        window); production output they are not."""
        now = timezone.now()
        job = _job(now - timezone.timedelta(hours=1))
        _execution_step(job, now - timezone.timedelta(minutes=5))
        # written 10 min ago — INSIDE the job window, BEFORE execution began.
        _workspace_output(
            tmp_path, "x-example", _window_name(10), _rows(5),
            mtime=(now - timezone.timedelta(minutes=10)).timestamp())
        _fm(monkeypatch, [], {})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))

        n, loc = tasks._real_items_evidence(
            "x-example", job, {"last_tested_draft_fp": "fp"})
        assert n == 0 and loc == ""

    def test_rows_written_after_execution_start_are_credited(
            self, monkeypatch, tmp_path):
        """The positive half — execution-phase rows keep rescuing (prod 770)."""
        now = timezone.now()
        job = _job(now - timezone.timedelta(hours=1))
        _execution_step(job, now - timezone.timedelta(minutes=5))
        _workspace_output(
            tmp_path, "x-example", _window_name(2), _rows(5),
            mtime=(now - timezone.timedelta(minutes=2)).timestamp())
        _fm(monkeypatch, [], {})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))

        n, loc = tasks._real_items_evidence(
            "x-example", job, {"last_tested_draft_fp": "fp"})
        assert n == 5 and loc.endswith(".json")

    def test_no_execution_step_keeps_the_job_window(self, monkeypatch, tmp_path):
        """A job with no execution Step row (legacy rows, unsaved instances in
        the wave-40 fixtures) keeps today's job-scoped window."""
        now = timezone.now()
        job = _job(now - timezone.timedelta(hours=1))
        assert job.steps.count() == 0
        _workspace_output(
            tmp_path, "x-example", _window_name(10), _rows(5),
            mtime=(now - timezone.timedelta(minutes=10)).timestamp())
        _fm(monkeypatch, [], {})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))

        n, _ = tasks._real_items_evidence(
            "x-example", job, {"last_tested_draft_fp": "fp"})
        assert n == 5


# ── sub-fix 3: no execution verdict → no rescue ─────────────────────────────


class TestRescueRequiresAnExecutionVerdict:
    def test_no_execution_status_in_state_never_rescues(self):
        assert tasks._rescue_fires(
            {"product_count": 0, "input_mode": "list_page"}, 9,
            "scrapers/x-example/output_1.json") is False
        assert tasks._rescue_fires({}, 9,
                                   "scrapers/x-example/output_1.json") is False

    def test_a_failed_execution_verdict_still_rescues(self):
        # A FAILED verdict the execution Step backs (the Step started) keeps
        # rescuing — the flag is DB truth, not another state string.
        assert tasks._rescue_fires(
            {"execution_status": "FAILED", "product_count": 0,
             "input_mode": "navigation"}, 19,
            "scrapers/x-example/output_1.json", execution_started=True) is True

    def test_no_execution_step_start_blocks_the_rescue(
            self, monkeypatch, tmp_path):
        """The state-string gate only half closed the class: graph.py writes
        `execution_status: "FAILED"` from eight fast-fail arms (writer
        timeout, tester wall-clock death, discovery auth wall, …) with
        `goto="cleanup"` — no execution Step row is ever started, so no
        production window exists and the code_tester's own rows stay test
        material. The gate reads the DB, not the prompt-authored verdict."""
        now = timezone.now()
        job = _job(now - timezone.timedelta(hours=1))
        # the TESTING phase ran and wrote its rows; execution never began.
        Step.objects.create(
            job=job, phase="testing", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        key = f"scrapers/x-example/{_window_name(3)}"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"site": {"scraped_at": (now - timezone.timedelta(
                minutes=3)).isoformat()}, "products": _rows(9)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "list_page",
            "execution_status": "FAILED", "product_count": 0,
            "error_message": "auth wall", "last_tested_draft_fp": "fp"})
        assert tasks._execution_started_epoch(job) is None
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0
        assert job.output_file == ""

    def test_execution_skipped_with_writer_rows_is_not_completed(
            self, monkeypatch, tmp_path):
        """Prod 1225: code_generation FAILED, execution SKIPPED, 9 rows in the
        workspace from the test phase — COMPLETED pc=9. The honest terminal is
        FAILED with the reason; nothing is credited, `output_file` is not
        repointed at the test-phase file."""
        now = timezone.now()
        job = _job(now - timezone.timedelta(hours=1))
        _execution_step(job, None, status=Step.STATUS_SKIPPED)
        key = f"scrapers/x-example/{_window_name(3)}"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"site": {"scraped_at": now.isoformat()}, "products": _rows(9)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "list_page",
            "last_tested_draft_fp": "fp", "product_count": 0,
            "error_message": ""})   # ← no execution_status: it never ran
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0
        assert job.output_file == ""     # the test-phase file is not production
        assert job.error_message         # the honest reason is on the record


# ── sub-fix 2: the COMPLETED count is the output file's own row count ───────


class TestProductCountIsTheFileRowCount:
    def test_state_claim_yields_to_the_files_row_count(
            self, monkeypatch, tmp_path):
        key = "scrapers/x-example/output_2026-09-20_050000_000001_3.json"
        _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(3)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(timezone.now() - timezone.timedelta(hours=1))
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "url_list",
            "execution_status": "SUCCESS", "product_count": 7,
            "output_file": key})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 3    # the file's rows, not the state's 7

    def test_rescued_count_yields_to_the_files_row_count(
            self, monkeypatch, tmp_path):
        """The rescue arm's count is re-derived too: `product_count` and
        `output_file` describe the same bytes (the prod-966 vice)."""
        now = timezone.now()
        job = _job(now - timezone.timedelta(hours=1),
                   scope="firstn", scope_value="6")
        _execution_step(job, now - timezone.timedelta(minutes=5))
        key = f"scrapers/x-example/{_window_name(2)}"
        _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(4) + [
            # a written fetch failure: the rescue's dead-row predicate keeps
            # it (0 ∉ DEAD_STATUS_CODES), the ground-truth counter drops it
            {"title": "dead", "price": "$1", "status_code": 0}]})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "url_list",
            "execution_status": "FAILED", "product_count": 0,
            "last_tested_draft_fp": "fp", "error_message": "crashed"})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.output_file == key
        assert job.product_count == 4
        # the [SCOPE-SHORTFALL] row cites the corrected count, not the 5 the
        # rescue credited before the file's row count won.
        assert "[SCOPE-SHORTFALL]" in (job.notes or "")
        assert "4" in job.notes
        assert "5" not in job.notes.split("[SCOPE-SHORTFALL]")[1]

    def test_soft_time_limit_stamp_uses_the_same_row_count(
            self, monkeypatch, tmp_path):
        """`finalize_from_artifacts` is a SECOND COMPLETED stamp (soft-time-
        limit kills, mid-graph and mid-resume) — it counted a raw
        `len(first list value)`, so a 4-good-row + 1-dead artifact recorded 5
        on this path and 4 on the main one. [C4-e] the stamp now also needs the
        execution Step its scenario implies (the kill landed mid-graph, i.e.
        after execution had already begun writing)."""
        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_7.json"
        _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(4) + [
            {"title": "dead", "price": "$1", "status_code": 0}]})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(now - timezone.timedelta(hours=1),
                   status=ScrapeJob.STATUS_RUNNING, output_file=key)
        _execution_step(job, now - timezone.timedelta(minutes=5))
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 4

    def test_unreadable_output_file_keeps_the_credited_count(
            self, monkeypatch, tmp_path):
        """A read failure must never zero a COMPLETED job."""
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(timezone.now() - timezone.timedelta(hours=1))
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "url_list",
            "execution_status": "SUCCESS", "product_count": 6,
            "output_file": "scrapers/x-example/gone.json"})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 6

    def test_reader_shapes(self, monkeypatch):
        """The reader: known row key, bare top-level array, non-dict rows and
        an unreadable locator all degrade honestly."""
        key = "scrapers/x-example/output_1.json"
        _fm(monkeypatch, [key], {key: json.dumps({
            "site": {"host": "x"},
            "products": _rows(2) + ["not-a-dict"]})})
        assert tasks._output_file_product_count(key) == 2

        bare = "scrapers/x-example/output_2.json"
        _fm(monkeypatch, [bare], {bare: json.dumps(_rows(3))})
        assert tasks._output_file_product_count(bare) == 3

        empty = "scrapers/x-example/output_3.json"
        _fm(monkeypatch, [empty], {empty: json.dumps({"products": []})})
        assert tasks._output_file_product_count(empty) == 0

        _fm(monkeypatch, [], {})
        assert tasks._output_file_product_count("scrapers/x/gone.json") == 0
        assert tasks._output_file_product_count("") == 0


# ── C4-e: the SECOND COMPLETED stamp honours the execution window too ──────


class TestTheSecondStampHonesty:
    """`finalize_from_artifacts` is a second COMPLETED stamp (soft-time-limit
    kills: mid-graph tasks.py:622, mid-resume tasks.py:912) with no execution
    start behind its evidence. `_run_graph_job` stamps RUNNING with
    `update_fields=["status", "started_at", "graph_thread_id"]` and never
    clears `output_file`, so a re-driven job is RUNNING while still carrying
    the PRIOR attempt's artifact — the stamp credited those 9 rows as this
    attempt's production output (the audit's OQ3 census class).
    """

    def test_second_stamp_does_not_credit_a_prior_attempts_artifact(
            self, monkeypatch, tmp_path):
        """RUNNING + a 9-row `output_file` + zero execution Steps: nothing
        started a production window in ANY attempt, so the artifact is
        test-phase or prior-attempt material — the honest terminal stands."""
        key = f"scrapers/x-example/{_window_name(3)}"
        _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(9)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(timezone.now() - timezone.timedelta(hours=1),
                   status=ScrapeJob.STATUS_RUNNING, output_file=key)
        assert job.steps.count() == 0
        assert tasks._execution_started_epoch(job) is None
        # the FAILED arm's F4 guard would close the test's transactional
        # connection mid-atomic (test_wave37 documents the same seam).
        monkeypatch.setattr("django.db.close_old_connections", lambda: None)

        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_FAILED
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0
        assert job.error_message == "SoftTimeLimitExceeded"

    def test_second_stamp_still_completes_when_execution_ran(
            self, monkeypatch, tmp_path):
        """The 593 half: the kill landed AFTER execution wrote its rows — the
        Step says so, and the evidence finalize still completes."""
        now = timezone.now()
        key = f"scrapers/x-example/{_window_name(2)}"
        _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(9)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(now - timezone.timedelta(hours=1),
                   status=ScrapeJob.STATUS_RUNNING, output_file=key)
        _execution_step(job, now - timezone.timedelta(minutes=5))

        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 9


# ── the happy path + the wave-19 scrub, unchanged ───────────────────────────


def test_rescued_happy_path_unchanged_and_scrub_intact(monkeypatch, tmp_path):
    """770-with-a-clock: execution RAN (the Step says when), wrote nothing the
    state kept, and 19 production rows sit in the FM. The rescue still fires,
    the count still describes the file, and the wave-19 stale-error scrub
    still clears the interrupt-era note (sub-fix 4: ordering untouched)."""
    now = timezone.now()
    job = _job(now - timezone.timedelta(hours=2))
    _execution_step(job, now - timezone.timedelta(minutes=30))
    key = f"scrapers/x-example/{_window_name(20)}"
    _fm(monkeypatch, [key], {key: json.dumps(
        {"site": {"scraped_at": (now - timezone.timedelta(minutes=20)).isoformat()},
         "products": _rows(19)})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "site_slug": "x-example", "input_mode": "navigation",
        "execution_status": "FAILED", "product_count": 0,
        "last_tested_draft_fp": "abc", "error_message": "noop draft twice"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_COMPLETED
    assert job.product_count == 19
    assert job.output_file == key
    assert "noop draft" not in (job.error_message or "")
