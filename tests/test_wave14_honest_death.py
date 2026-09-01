"""[wave-14 job-133] Honest death + honest liveness — PR-1 contract tests.

Job 133 (athleta.gap.com) died when a prefork worker child was SIGKILLed
(probably container OOM) mid code_tester. No Python ran afterwards, so:

- the task body's except-path could not finalize the job row (it stayed
  RUNNING for 33 minutes until the watchdog *guessed* from silence),
- the watchdog's ``_task_liveness`` read a partial inspect reply (one PidBox
  self-reply) as a complete fleet answer and said "absent",
- the celery_task_id stamp sat ABOVE the duplicate-dispatch guard, so a
  skipped duplicate stole the live task's id and manufactured a false corpse,
- the [EXEC-ALIVE] heartbeat's first beat was due exactly when the child died
  and its default interval (240s) exceeded the dispatch's early window, so
  "died mid-run" was indistinguishable from "first beat not due yet",
- a transient MCP outage poisoned ``_cached_tools=[]`` for the worker's whole
  remaining lifetime, and ``isError`` results were formatted as if they were
  successes.

The tests here pin the fixes. They deliberately follow the repo's
test-at-end doctrine: no TDD, contract locks after the behavior exists.

Run from repo root:
    PYTHONPATH=/app:/app/webapp python -m pytest tests/test_wave14_honest_death.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


# ─── task_failure process-death handler ──────────────────────────────────────


class TestProcessDeathHandler:
    def test_process_death_excuses_are_billiard_classes(self):
        import scraper.tasks as wt

        assert wt._PROCESS_DEATH_EXCUSES, (
            "billiard's WorkerLostError/TimeLimitExceeded must resolve — the "
            "handler is a no-op without them"
        )
        names = {e.__name__ for e in wt._PROCESS_DEATH_EXCUSES}
        assert names == {"WorkerLostError", "TimeLimitExceeded"}

    def test_handler_registered_on_task_failure_signal(self):
        from celery.signals import task_failure

        import scraper.tasks as wt  # noqa: F401 — registers the receiver

        receivers = task_failure._live_receivers(None)
        assert any(r.__name__ == "_on_task_process_death" for r in receivers)

    def _make_running_job(self):
        from scraper.models import ScrapeJob

        job = ScrapeJob.objects.create(
            url="https://example.com/",
            search_criteria="",
            status=ScrapeJob.STATUS_RUNNING,
        )
        return job

    @pytest.mark.django_db
    def test_worker_lost_failure_finalizes_the_job(self, monkeypatch):
        import scraper.tasks as wt

        try:
            from billiard.exceptions import WorkerLostError
        except ImportError:  # pragma: no cover
            pytest.skip("billiard unavailable")
        # _finalize_job_failed recycles DB connections (F4, prod-real behavior)
        # — under pytest that would close the TEST's transactional connection,
        # so neutralize it here; the handler's finalization logic is what's
        # under test, not Django's connection hygiene.
        monkeypatch.setattr("django.db.close_old_connections", lambda: None)
        from scraper.models import ScrapeJob

        job = self._make_running_job()
        job.celery_task_id = "dead-beef"
        job.save(update_fields=["celery_task_id"])

        exc = WorkerLostError("process exited with signal 9")
        wt._on_task_process_death(
            sender=SimpleNamespace(name="scraper.tasks.run_scrape_task"),
            task_id="dead-beef",
            args=[job.id],
            exception=exc,
        )
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        # The message names the real cause class, not a phantom "wedged scrape"
        assert "WorkerLostError" in (job.error_message or "")
        assert "signal 9" in (job.error_message or "")
        assert "re-drive" in (job.error_message or "")
        # Step rows a killed child left behind must not spin forever
        from scraper.models import Step

        assert not job.steps.filter(status=Step.STATUS_RUNNING).exists()

    @pytest.mark.django_db
    def test_non_process_death_exception_is_ignored(self):
        import scraper.tasks as wt

        job = self._make_running_job()
        before = job.status
        wt._on_task_process_death(
            sender=SimpleNamespace(name="scraper.tasks.run_scrape_task"),
            task_id="t",
            args=[job.id],
            exception=ValueError("ordinary in-task bug — the task body handles it"),
        )
        job.refresh_from_db()
        assert job.status == before

    @pytest.mark.django_db
    def test_unknown_sender_is_ignored(self):
        import scraper.tasks as wt

        job = self._make_running_job()
        wt._on_task_process_death(
            sender=SimpleNamespace(name="some.other.module.task"),
            task_id="t",
            args=[job.id],
            exception=wt._PROCESS_DEATH_EXCUSES[0]("died"),
        )
        job.refresh_from_db()
        assert job.status != "failed"

    @pytest.mark.django_db
    def test_waiting_approval_job_is_not_clobbered(self):
        """A WAITING_APPROVAL row is continued by a NEW resume task after the
        human answers — a dead child there strands nothing, so the handler
        must not flip the row to FAILED (it would kill a live approval)."""
        import scraper.tasks as wt
        from scraper.models import ScrapeJob

        job = self._make_running_job()
        job.status = ScrapeJob.STATUS_WAITING_APPROVAL
        job.save(update_fields=["status"])
        wt._on_task_process_death(
            sender=SimpleNamespace(name="scraper.tasks.resume_scrape_task"),
            task_id="t",
            args=[job.id],
            exception=wt._PROCESS_DEATH_EXCUSES[0]("died"),
        )
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_WAITING_APPROVAL

    @pytest.mark.django_db
    def test_idempotent_after_watchdog_already_failed_it(self):
        import scraper.tasks as wt
        from scraper.models import ScrapeJob

        job = self._make_running_job()
        job.status = ScrapeJob.STATUS_FAILED
        job.error_message = "watchdog got here first"
        job.save(update_fields=["status", "error_message"])
        wt._on_task_process_death(
            sender=SimpleNamespace(name="scraper.tasks.run_scrape_task"),
            task_id="t",
            args=[job.id],
            exception=wt._PROCESS_DEATH_EXCUSES[0]("died"),
        )
        job.refresh_from_db()
        assert job.error_message == "watchdog got here first"


# ─── celery_task_id stamp placement ──────────────────────────────────────────


class TestStampBelowGuard:
    def test_stamp_sits_below_the_duplicate_dispatch_guard(self):
        """[wave-15 1.1 superseded the placement pin] A skipped duplicate used
        to steal the stamp and overwrite the LIVE task's id — the watchdog
        then inspected a task that runs nowhere and revoked the healthy run
        (false corpse). The read-then-judge guard is GONE: status + task id
        are now written by ONE atomic claim UPDATE, so there is no
        instance-level stamp left to steal."""
        src = open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")).read()
        # The atomic claim exists...
        assert '_claim_values["celery_task_id"] = _task_id' in src
        # ...and the steable instance-level stamp shape is gone.
        assert "job.celery_task_id = _task_id" not in src
        # The duplicate-dispatch log contract is kept verbatim.
        assert "skipping duplicate dispatch" in src

    def test_stamp_comment_names_the_false_corpse_class(self):
        src = open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")).read()
        assert "false corpse" in src


# ─── heartbeat budget + t=0 beat ─────────────────────────────────────────────


class TestHeartbeatBudget:
    def test_dispatch_alive_passes_its_bound_as_beat_budget(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "tools", "shell_tools.py"
        )).read()
        assert "beat_budget=timeout" in src

    def test_budget_shrinks_interval_never_lengthens(self, monkeypatch):
        """interval = min(interval, max(30, budget//3)) — pinned by reading
        the arithmetic off the source (a live _beat would touch the DB)."""
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        assert "interval = min(interval, max(30, beat_budget // 3))" in src

    def test_first_beat_fires_synchronously(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        assert "_beat()  # t=0 beat" in src

    def test_beat_failure_is_logged_not_swallowed(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        assert '"heartbeat for job %s (%s) beat %d failed: %s"' in src


# ─── invoke-timeout SessionLog rows ──────────────────────────────────────────


class TestInvokeTimeoutRows:
    def test_both_timeout_paths_write_sessionlog_rows(self):
        """Postmortems read the job log (SessionLog), not container stdout —
        both the async and the abandoned-thread path must leave a row."""
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        assert src.count("[INVOKE-TIMEOUT]") >= 2


# ─── MCP negative cache + isError propagation ────────────────────────────────


class TestMcpNegativeCache:
    def _reset(self):
        import agents.tools.playwright_tools as pt

        pt._cached_tools = None
        pt._cached_tools_at = 0.0
        return pt

    def test_failed_probe_is_negative_cached_then_reprobed(self, monkeypatch):
        import time as _time

        pt = self._reset()
        calls = {"n": 0}

        def fake_run(coro):
            calls["n"] += 1
            coro.close()  # never started — avoid un-awaited coroutine warnings
            raise ConnectionError("MCP down")

        monkeypatch.setattr(pt.asyncio, "run", fake_run)
        assert pt.create_playwright_tools_sync() == []
        assert calls["n"] == 1
        # Inside the TTL: no second probe.
        assert pt.create_playwright_tools_sync() == []
        assert calls["n"] == 1
        # Past the TTL: the probe runs again (recovery is picked up). The
        # sync wrapper still catches the failure — it just re-probes first.
        pt._cached_tools_at = _time.monotonic() - (pt._MCP_NEGATIVE_CACHE_TTL + 1)
        assert pt.create_playwright_tools_sync() == []
        assert calls["n"] == 2
        self._reset()

    def test_non_empty_cache_is_kept_forever(self, monkeypatch):
        pt = self._reset()
        sentinel = [object()]
        pt._cached_tools = sentinel  # type: ignore[assignment]
        monkeypatch.setattr(
            pt.asyncio, "run",
            lambda coro: (_ for _ in ()).throw(AssertionError("must not probe")),
        )
        assert pt.create_playwright_tools_sync() is sentinel
        self._reset()


class TestIsErrorPropagation:
    def test_is_error_result_is_marked_for_the_agent(self):
        from agents.tools.playwright_tools import _format_tool_result

        result = SimpleNamespace(
            content=[SimpleNamespace(text="Navigation failed: net::ERR_ABORTED")],
            isError=True,
        )
        out = _format_tool_result("browser_navigate", result)
        assert out.startswith("[TOOL ERROR] ")
        assert "net::ERR_ABORTED" in out

    def test_successful_result_is_untouched(self):
        from agents.tools.playwright_tools import _format_tool_result

        result = SimpleNamespace(
            content=[SimpleNamespace(text="- page content")], isError=False
        )
        assert _format_tool_result("browser_snapshot", result) == "- page content"

    def test_result_without_is_error_attr_is_untouched(self):
        """Older mcp versions / fakes without the flag behave exactly as before."""
        from agents.tools.playwright_tools import _format_tool_result

        result = SimpleNamespace(content=[SimpleNamespace(text="ok")])
        assert _format_tool_result("browser_click", result) == "ok"


# ─── /scrape payload correlation ─────────────────────────────────────────────


class TestScrapePayloadCorrelation:
    def test_payload_carries_job_id(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "tools", "shell_tools.py"
        )).read()
        assert '"job_id": _scrape_job_id' in src

    def test_job_id_hoisted_before_its_first_try_block(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "tools", "shell_tools.py"
        )).read()
        i_hoist = src.index("_scrape_job_id = 0  # hoisted")
        i_payload = src.index('"job_id": _scrape_job_id')
        assert i_hoist < i_payload


# ─── watchdog honesty extras ─────────────────────────────────────────────────


class TestWatchdogHonesty:
    def test_absent_message_reports_actual_acks_late_config(self):
        src = open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")).read()
        assert "conf.task_acks_late" in src
        assert "acks_late=True — a broker redelivery may still re-run it" in src

    def test_finalize_helper_used_by_both_failure_paths(self):
        """One F4-safe finalize sequence, shared by the in-task except path
        and the task_failure handler — no drift between the two."""
        src = open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")).read()
        assert src.count("_finalize_job_failed(") >= 3  # def + 2 call sites


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
