"""Wave-16 browser-service resilience: dependency park + pre-flight + self-heal.

Prod #210: 71/71 /navigate calls 502'd while a zombie MCP Chrome pinned the
container — and the pipeline treated every one of those runs as a SITE
failure (strategy switches, honest FAILs) because nothing downstream could
distinguish "the gateway is down" from "the site won".

Three layers pinned here:
- B2 (browser_service): the zombie-heal helper restarts a CDP-dead
  persistent Chrome (cooldown-gated); site-shaped failures never match the
  launch-stage markers, so a heal round is never burned on a site that won.
- B3 (caller): the client surfaces the server's classified error_class;
  pre-flight waits bounded for /health before testing/execution; infra
  verdicts PARK the job (non-terminal browser_unavailable) instead of
  recycling the strategy ladder; a beat task re-dispatches parked jobs once
  /health recovers.
- Routing: _route_after_execution + route_after_testing route infra verdicts
  to the park node; _finalize_job and the auto-scheduler treat the park as
  unfinished-but-held work.

NOTE on patching: run_execution / route_after_testing / tasks all import the
browser_http helpers FUNCTION-LEVEL (at call time), so monkeypatch targets
are the attributes on ``agents.tools.browser_http`` — patching the caller
module is a no-op.

Run: docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.settings -e PYTHONPATH=/app:/app/webapp django sh -c "cd /app && pytest tests/test_wave16_browser_park.py -q"
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

BH = "agents.tools.browser_http"


def _goto(result):
    return getattr(result, "goto", None)


# ───────────────────────────── B2: self-heal ──────────────────────────────


class _FakeLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _server_src() -> str:
    with open(os.path.join(ROOT, "browser_service", "server.py"), encoding="utf-8") as fh:
        return fh.read()


def _grab_fn(name: str) -> str:
    src = _server_src()
    pat = rf"^(?:async )?def {name}\(.*?(?=^(?:async )?def |^class |^@)"
    m = re.search(pat, src, re.M | re.S)
    assert m, f"{name} not found in server.py"
    return m.group(0)


def _patch_bh(monkeypatch, name: str, value) -> None:
    """Patch ``name`` on EVERY loaded browser_http module object.

    The repo has two package roots (``agents.*`` used by scraper/tasks absolute
    imports, ``webapp.agents.*`` used by graph's relative imports) — they are
    distinct module objects with separate attrs, so a patch on one can silently
    miss the code under test.
    """
    import importlib

    for mod_name in ("agents.tools.browser_http", "webapp.agents.tools.browser_http"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        monkeypatch.setattr(mod, name, value, raising=False)


class TestNavigateSelfHeal:
    def _load_ns(self, extra=None):
        """Exec the B2 helpers without importing the whole server module."""
        src = _server_src()
        ns = {
            "time": time,
            "asyncio": asyncio,
            "logger": _FakeLogger(),
            "MAINT_EXECUTOR": None,      # None → loop's default executor
            "RESTART_EXECUTOR": None,
            "_maint_task": lambda label, fn: fn,
            "browser_pool": types.SimpleNamespace(
                check_cdp_liveness=lambda: {},
                scraper_not_required=lambda: True,
                restart_chrome=lambda label: {"errors": []},
            ),
            "_CDP_LIVENESS_CACHE": {},
            "_last_self_heal": [0.0],
            **(extra or {}),
        }
        # The helpers read module-level constants — exec the REAL ones so the
        # test tracks the marker list instead of a stale copy.
        m_cd = re.search(r"^_SELF_HEAL_COOLDOWN_S = .*$", src, re.M)
        m_mk = re.search(r"^_LAUNCH_STAGE_MARKERS = \(.*?\)", src, re.M | re.S)
        assert m_cd and m_mk
        exec(m_cd.group(0), ns)
        exec(m_mk.group(0), ns)
        for name in (
            "_is_launch_stage_failure", "_navigate_self_heal",
            "_navigate_self_heal_if_zombie",
        ):
            exec(compile(_grab_fn(name), f"<{name}>", "exec"), ns)
        return ns

    def test_site_failures_never_look_like_launch_stage(self):
        f = self._load_ns()["_is_launch_stage_failure"]
        # Site verdicts: block pages, captchas, timeouts — never a heal round.
        assert not f("page.goto: Timeout 30000ms exceeded")
        assert not f("Access denied | example.com used Cloudflare to restrict access")
        assert not f("captcha challenge detected")
        assert not f("")

    def test_launch_stage_markers_match(self):
        f = self._load_ns()["_is_launch_stage_failure"]
        assert f("browser failed to launch: Page.goto")
        assert f("TargetClosedError: browser has been closed")
        assert f("OSError: [Errno 11] Resource temporarily unavailable")
        assert f("Cannot allocate memory while forking chrome")
        assert f("Executable doesn't exist at /ms-playwright/chrome")

    def test_zombie_heal_respects_cooldown(self):
        # A fresh cooldown stamp (heal just ran) → no second attempt.
        ns = self._load_ns({"_last_self_heal": [time.monotonic()]})
        assert asyncio.run(ns["_navigate_self_heal_if_zombie"]("test")) is False

    def test_zombie_heal_needs_a_dead_chrome(self):
        # Cooldown elapsed AND liveness cache shows both Chromes alive → no-op.
        ns = self._load_ns({
            "_last_self_heal": [0.0],
            "_CDP_LIVENESS_CACHE": {"mcp_cdp_alive": True, "scraper_cdp_alive": True},
        })
        assert asyncio.run(ns["_navigate_self_heal_if_zombie"]("test")) is False

    def test_heal_fires_on_cdp_dead_mcp(self):
        ns = self._load_ns({
            "_last_self_heal": [0.0],
            "_CDP_LIVENESS_CACHE": {"mcp_cdp_alive": False, "scraper_cdp_alive": True},
        })
        restarted = []
        ns["browser_pool"].restart_chrome = (
            lambda label: restarted.append(label) or {"errors": []}
        )
        ns["browser_pool"].check_cdp_liveness = lambda: {
            "mcp_cdp_alive": False, "scraper_cdp_alive": True,
        }
        fired = asyncio.run(ns["_navigate_self_heal_if_zombie"]("test"))
        assert fired is True
        assert restarted == ["mcp"]

    def test_unknown_liveness_defaults_to_alive(self):
        """A sparse cache (probe not run yet) must NOT manufacture a restart."""
        ns = self._load_ns({"_last_self_heal": [0.0], "_CDP_LIVENESS_CACHE": {}})
        assert asyncio.run(ns["_navigate_self_heal_if_zombie"]("test")) is False


# ─────────────────── B3: client surfaces + park helpers ───────────────────


class TestScrapeResultUnavailable:
    def _result(self, **kw):
        from agents.tools.browser_http import ScrapeResult

        return ScrapeResult(**kw)

    def test_infra_error_class_is_unavailable(self):
        assert self._result(ok=False, status=503, server_error_class="memory_pressure").unavailable
        assert self._result(ok=False, status=502, server_error_class="chrome_crash").unavailable
        assert self._result(ok=False, status=503, server_error_class="resource").unavailable

    def test_transport_dead_is_unavailable(self):
        assert self._result(ok=False, status=None).unavailable

    def test_site_shaped_failures_are_not_unavailable(self):
        assert not self._result(ok=False, status=502).unavailable  # no infra class named
        assert not self._result(ok=False, status=404).unavailable
        assert not self._result(ok=False, status=429, throttled=True).unavailable
        assert not self._result(ok=True, status=200).unavailable

    def test_post_scrape_captures_server_diagnosis(self, monkeypatch):
        import agents.tools.browser_http as bh

        class _Resp:
            status_code = 503
            headers: dict = {}

            def json(self):
                return {"error_class": "chrome_crash", "error": "child chrome died"}

        monkeypatch.setattr(bh.httpx, "post", lambda *a, **k: _Resp(), raising=True)
        out = bh.post_scrape_with_retry("http://x/scrape", {}, timeout=5, max_attempts=1)
        assert out.ok is False
        assert out.server_error_class == "chrome_crash"
        assert "child chrome died" in out.body_error
        assert out.unavailable is True
        # The summarized error names the server's OWN class — RCA is one line.
        assert "chrome_crash" in out.error

    def test_plain_503_without_body_class_stays_site_shaped(self, monkeypatch):
        import agents.tools.browser_http as bh

        class _Resp:
            status_code = 503
            headers: dict = {}

            def json(self):
                return {}

        monkeypatch.setattr(bh.httpx, "post", lambda *a, **k: _Resp(), raising=True)
        out = bh.post_scrape_with_retry("http://x/scrape", {}, timeout=5, max_attempts=1)
        assert out.unavailable is False
        assert out.status == 503


class TestBrowserServiceHealthy:
    def test_ok_true_only_on_200_ok(self, monkeypatch):
        import agents.tools.browser_http as bh

        class _Resp:
            def __init__(self, code, body):
                self.status_code = code
                self._b = body

            def json(self):
                return self._b

        monkeypatch.setattr(bh.httpx, "get", lambda *a, **k: _Resp(200, {"status": "ok"}))
        assert bh.browser_service_healthy() is True
        monkeypatch.setattr(bh.httpx, "get", lambda *a, **k: _Resp(503, {"status": "degraded"}))
        assert bh.browser_service_healthy() is False
        monkeypatch.setattr(bh.httpx, "get", lambda *a, **k: _Resp(200, {"status": "starting"}))
        assert bh.browser_service_healthy() is False

    def test_transport_error_reads_unhealthy(self, monkeypatch):
        import agents.tools.browser_http as bh

        def _boom(*a, **k):
            raise bh.httpx.ConnectError("refused")

        monkeypatch.setattr(bh.httpx, "get", _boom)
        assert bh.browser_service_healthy() is False

    def test_wait_returns_false_within_a_tiny_window(self, monkeypatch):
        """Bounded pre-flight: an outage must exhaust the WINDOW, not retry
        forever — a 0s window proves the loop exits."""
        import agents.tools.browser_http as bh

        monkeypatch.setattr(bh, "browser_service_healthy", lambda: False)
        t0 = time.monotonic()
        assert bh.wait_for_browser_service(max_wait_s=0.0, poll_s=0.01) is False
        assert time.monotonic() - t0 < 5.0

    def test_wait_returns_true_on_first_healthy_poll(self, monkeypatch):
        import agents.tools.browser_http as bh

        monkeypatch.setattr(bh, "browser_service_healthy", lambda: True)
        assert bh.wait_for_browser_service(max_wait_s=5.0, poll_s=0.01) is True


@pytest.mark.django_db
class TestParkJob:
    def test_park_writes_non_terminal_status(self):
        from model_bakery import baker

        from agents.tools.browser_http import park_job_for_browser_service
        from scraper.models import ScrapeJob

        job = baker.make(ScrapeJob, url="https://example.com", status=ScrapeJob.STATUS_RUNNING)
        assert park_job_for_browser_service(job.id, "gateway down") is True
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_BROWSER_UNAVAILABLE
        assert "gateway down" in job.error_message
        # NON-terminal: completed_at unset, and the model's terminal set
        # excludes the park (approvals stay actionable, resumer can claim it).
        assert job.completed_at is None
        from scraper.models import _TERMINAL_JOB_STATUSES

        assert ScrapeJob.STATUS_BROWSER_UNAVAILABLE not in _TERMINAL_JOB_STATUSES

    def test_park_missing_job_is_a_clean_false(self):
        from agents.tools.browser_http import park_job_for_browser_service

        assert park_job_for_browser_service(99999999, "gone") is False


@pytest.mark.django_db
class TestResumeTask:
    def test_disabled_flag_is_a_noop(self, settings):
        from scraper.tasks import resume_browser_unavailable_jobs

        settings.BROWSER_RESUME_ENABLED = False
        assert resume_browser_unavailable_jobs() == {"resumed": 0, "reason": "disabled"}

    def test_unhealthy_gateway_resumes_nothing(self, settings, monkeypatch):
        import agents.tools.browser_http as bh
        from scraper.tasks import resume_browser_unavailable_jobs

        settings.BROWSER_RESUME_ENABLED = True
        monkeypatch.setattr(f"{BH}.browser_service_healthy", lambda: False)
        assert resume_browser_unavailable_jobs() == {
            "resumed": 0, "reason": "browser_service unhealthy",
        }

    def test_parked_rows_redispatch_oldest_first(self, settings, monkeypatch):
        from model_bakery import baker

        import agents.tools.browser_http as bh
        from scraper import tasks as st
        from scraper.models import ScrapeJob

        settings.BROWSER_RESUME_ENABLED = True
        monkeypatch.setattr(f"{BH}.browser_service_healthy", lambda: True)
        monkeypatch.setattr(f"{BH}.BROWSER_RESUME_BATCH", 1)
        dispatched = []
        monkeypatch.setattr(st, "dispatch_scrape_job", lambda jid: dispatched.append(jid))
        old = baker.make(ScrapeJob, url="https://a.com", status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE)
        new = baker.make(ScrapeJob, url="https://b.com", status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE)
        running = baker.make(ScrapeJob, url="https://c.com", status=ScrapeJob.STATUS_RUNNING)

        out = st.resume_browser_unavailable_jobs()
        assert out["resumed"] == 1 and dispatched == [old.id]
        old.refresh_from_db()
        assert old.status == ScrapeJob.STATUS_PENDING  # claimed → dispatched
        new.refresh_from_db()
        assert new.status == ScrapeJob.STATUS_BROWSER_UNAVAILABLE  # batch bound
        running.refresh_from_db()
        assert running.status == ScrapeJob.STATUS_RUNNING  # untouched

    def test_dispatch_failure_reverts_the_claim(self, settings, monkeypatch):
        from model_bakery import baker

        import agents.tools.browser_http as bh
        from scraper import tasks as st
        from scraper.models import ScrapeJob

        settings.BROWSER_RESUME_ENABLED = True
        monkeypatch.setattr(f"{BH}.browser_service_healthy", lambda: True)
        monkeypatch.setattr(f"{BH}.BROWSER_RESUME_BATCH", 3)
        monkeypatch.setattr(
            st, "dispatch_scrape_job",
            lambda jid: (_ for _ in ()).throw(RuntimeError("broker down")),
        )
        job = baker.make(ScrapeJob, url="https://a.com", status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE)
        out = st.resume_browser_unavailable_jobs()
        assert out["resumed"] == 0
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_BROWSER_UNAVAILABLE

    def test_no_parked_rows_is_a_clean_noop(self, settings, monkeypatch):
        import agents.tools.browser_http as bh
        from scraper.tasks import resume_browser_unavailable_jobs

        settings.BROWSER_RESUME_ENABLED = True
        monkeypatch.setattr(f"{BH}.browser_service_healthy", lambda: True)
        assert resume_browser_unavailable_jobs() == {"resumed": 0, "reason": "none parked"}


@pytest.mark.django_db
class TestFinalizeAndSchedulerHold:
    def test_finalize_skips_parked_jobs(self):
        from model_bakery import baker

        from scraper.models import ScrapeJob
        from scraper.tasks import _finalize_job

        job = baker.make(
            ScrapeJob, url="https://example.com",
            status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE,
            error_message="parked",
        )
        _finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_BROWSER_UNAVAILABLE

    def test_scheduler_holds_while_jobs_are_parked(self):
        from model_bakery import baker

        from scraper.models import ScrapeJob
        from scraper.tasks import _do_schedule_next_site

        baker.make(ScrapeJob, url="https://parked.com", status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE)
        out = _do_schedule_next_site()
        assert out["action"] == "skipped"
        assert "BROWSER_UNAVAILABLE" in out["reason"]


# ───────────────────── Routing: infra verdicts park ──────────────────────


def _exec_state(**over):
    state = {
        "job_id": 1,
        "execution_status": "FAILED",
        "product_count": 0,
        "input_mode": "list_page",
        "discovery_coverage": {
            "ran_phase1": True,
            "stop_reason": "navigate_unavailable",
            "discovered_urls": 0,
        },
        "scraper_analysis": {"strategy": "http_navigation"},
        "execution_recycle_count": 0,
        "no_fresh_output": True,
    }
    state.update(over)
    return state


@pytest.mark.django_db
class TestRouteAfterExecutionPark:
    def _route(self, state):
        from webapp.agents.graph import _route_after_execution

        return _route_after_execution(state)

    def test_infra_verdict_parks(self):
        assert _goto(self._route(_exec_state())) == "park_browser_unavailable"

    def test_preflight_unavailable_parks(self):
        res = self._route(_exec_state(
            discovery_coverage={"ran_phase1": False, "stop_reason": "navigate_unavailable"},
            browser_unavailable_detail="waited 120s for /health",
        ))
        assert _goto(res) == "park_browser_unavailable"

    def test_items_extracted_never_park(self):
        """Transient nav noise that recovered is a REAL result — cleanup, not park."""
        res = self._route(_exec_state(execution_status="SUCCESS", product_count=448))
        assert _goto(res) == "cleanup"

    def test_site_shaped_zero_still_recycles(self):
        """navigate_unavailable must not swallow the ordinary strategy recycle."""
        res = self._route(_exec_state(
            discovery_coverage={
                "ran_phase1": True, "stop_reason": "empty_first_page", "discovered_urls": 0,
            },
        ))
        assert _goto(res) == "scraper_analyzer"


@pytest.mark.django_db
class TestParkNode:
    def test_park_node_writes_status_and_ends(self, monkeypatch):
        from model_bakery import baker

        import webapp.agents.graph as g
        from scraper.models import ScrapeJob

        job = baker.make(ScrapeJob, url="https://example.com", status=ScrapeJob.STATUS_RUNNING)
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None, raising=True)
        res = g._park_browser_unavailable({
            "job_id": job.id,
            "browser_unavailable_detail": "gateway unhealthy before testing",
        })
        assert _goto(res) == "__end__"
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_BROWSER_UNAVAILABLE
        assert "gateway unhealthy before testing" in job.error_message

    def test_park_node_derives_detail_from_test_report(self, monkeypatch):
        """route_after_testing cannot mutate state — the node recovers the
        reason from the tester's report itself."""
        from model_bakery import baker

        import webapp.agents.graph as g
        from scraper.models import ScrapeJob

        job = baker.make(ScrapeJob, url="https://example.com", status=ScrapeJob.STATUS_RUNNING)
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None, raising=True)
        res = g._park_browser_unavailable({
            "job_id": job.id,
            "test_report": {"discovery_coverage": {"stop_reason": "navigate_unavailable"}},
        })
        assert _goto(res) == "__end__"
        job.refresh_from_db()
        assert "navigate_unavailable" in job.error_message


class TestClassifyParkUnhealthy:
    def test_navigate_unavailable_classifies_park(self):
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        report = {
            "overall_assessment": "FAIL",
            "discovery_coverage": {"ran_phase1": True, "stop_reason": "navigate_unavailable"},
        }
        action, reason = classify_test_failure(report, "http_navigation")
        assert action == "park_unhealthy"
        assert "browser-service" in reason.lower() or "browser_service" in reason.lower()

    def test_precedes_zero_item_strategy_branch(self):
        """A downed gateway on an http strategy must NOT read as 'no items'."""
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        report = {
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "discovery_coverage": {"ran_phase1": True, "stop_reason": "navigate_unavailable"},
        }
        action, _ = classify_test_failure(report, "http_navigation")
        assert action == "park_unhealthy"

    def test_throttle_still_classifies_retest(self):
        """The park must not absorb the pre-existing backpressure retest."""
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        report = {
            "overall_assessment": "FAIL",
            "discovery_coverage": {"ran_phase1": True, "stop_reason": "navigate_throttled"},
        }
        action, _ = classify_test_failure(report, "http_navigation")
        assert action == "retest"


@pytest.mark.django_db
class TestRouteAfterTestingParks:
    def _state(self, **over):
        from webapp.agents.constants import MAX_TEST_RETRIES

        state = {
            "job_id": 1,
            "input_mode": "list_page",
            "test_retry_count": MAX_TEST_RETRIES + 5,  # exhausted: park still wins
            "test_report": {
                "overall_assessment": "FAIL",
                "discovery_coverage": {
                    "ran_phase1": True, "stop_reason": "navigate_unavailable",
                },
            },
            "scraper_analysis": {"strategy": "http_navigation"},
        }
        state.update(over)
        return state

    def _rat(self, monkeypatch):
        import importlib

        rat = importlib.import_module("webapp.agents.nodes.route_after_testing")
        # _log_cascade writes a SessionLog FK row — unit tests carry no job 1.
        monkeypatch.setattr(rat, "_log_cascade", lambda *a, **k: None, raising=True)
        return rat

    def test_router_parks_when_gateway_still_down(self, monkeypatch):
        rat = self._rat(monkeypatch)
        _patch_bh(monkeypatch, "browser_service_healthy", lambda: False)
        assert rat.route_after_testing(self._state()) == "park_browser_unavailable"

    def test_router_retests_when_gateway_recovered(self, monkeypatch):
        rat = self._rat(monkeypatch)
        _patch_bh(monkeypatch, "browser_service_healthy", lambda: True)
        assert rat.route_after_testing(self._state()) == "code_tester"

    def test_preflight_flag_parks_above_no_report_arms(self, monkeypatch):
        rat = self._rat(monkeypatch)
        state = self._state(test_report=None, browser_unavailable_detail="waited 120s")
        assert rat.route_after_testing(state) == "park_browser_unavailable"


# ───────────────── run_execution: infra verdict normalization ────────────


@pytest.mark.django_db
class TestRunExecutionInfraPaths:
    def _invoke(self, monkeypatch, tmp_path, *, healthy=True, scrape_result=None):
        import importlib

        import webapp.agents.graph as g

        # nodes/__init__ re-exports the FUNCTION `run_execution` — import the
        # MODULE itself.
        re_mod = importlib.import_module("webapp.agents.nodes.run_execution")

        # Function-level imports resolve from the SOURCE module at call time.
        _patch_bh(monkeypatch, "wait_for_browser_service", lambda *a, **k: healthy)
        if scrape_result is not None:
            _patch_bh(monkeypatch, "post_scrape_with_retry", lambda *a, **k: scrape_result)
        # Keep the phase notifier off the DB for synthetic job_id 0.
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None, raising=True)
        # The source is read BEFORE the POST — it must exist on disk.
        script = tmp_path / "scraper_draft.py"
        script.write_text("print('hi')\n")
        return re_mod._run_via_browser_service(
            str(script), [], str(tmp_path), {"job_id": 0},
        )

    def test_preflight_failure_is_normalized(self, monkeypatch, tmp_path):
        out = self._invoke(monkeypatch, tmp_path, healthy=False)
        assert out["execution_status"] == "FAILED"
        assert out["discovery_coverage"]["stop_reason"] == "navigate_unavailable"
        assert "unhealthy" in out["browser_unavailable_detail"]

    def test_unavailable_scrape_result_is_normalized(self, monkeypatch, tmp_path):
        from agents.tools.browser_http import ScrapeResult

        res = ScrapeResult(
            ok=False, status=503, attempts=3, server_error_class="memory_pressure",
        )
        res.error = (
            "browser_service returned HTTP 503 after 3 attempt(s) "
            "error_class=memory_pressure"
        )
        out = self._invoke(monkeypatch, tmp_path, scrape_result=res)
        assert out["execution_status"] == "FAILED"
        assert out["discovery_coverage"]["stop_reason"] == "navigate_unavailable"
        assert "memory_pressure" in out["browser_unavailable_detail"]

    def test_discovery_zero_marker_is_parsed(self, monkeypatch, tmp_path):
        from agents.tools.browser_http import ScrapeResult

        res = ScrapeResult(ok=True, status=200)
        res.data = {
            "returncode": 3,
            "stderr": "NAVIGATE_UNAVAILABLE: browser-service unavailable during discovery "
                      "(status=503 error_class=memory_pressure)\nDISCOVERY_ZERO",
        }
        out = self._invoke(monkeypatch, tmp_path, scrape_result=res)
        assert out["execution_status"] == "FAILED"
        assert out["no_fresh_output"] is True
        assert out["discovery_coverage"]["stop_reason"] == "navigate_unavailable"
        assert "memory_pressure" in out["browser_unavailable_detail"]

    def test_plain_discovery_zero_stays_empty_first_page(self, monkeypatch, tmp_path):
        from agents.tools.browser_http import ScrapeResult

        res = ScrapeResult(ok=True, status=200)
        res.data = {"returncode": 3, "stderr": "DISCOVERY_ZERO: no item urls found"}
        out = self._invoke(monkeypatch, tmp_path, scrape_result=res)
        assert out["discovery_coverage"]["stop_reason"] == "empty_first_page"
        assert "browser_unavailable_detail" not in out


# ───────────────────────── B4: dead-tester probe arm ─────────────────────


class TestDeadTesterProbeArm:
    """job-217 crash: the tester invocation died (Z.AI 429) → report was
    None; the deterministic Phase-1 probe still produced a healthy yield and
    the unguarded ``report["discovery_coverage"] = _pcov`` crashed the node
    with a bare ``'NoneType' object does not support item assignment`` that
    masked the real death on the job row. The sibling arms' ``report =
    report or {}`` is deliberately NOT replicated here — a synthesized
    verdict would bypass route_after_testing's no-report arms."""

    def _tester_src(self) -> str:
        src = open(
            os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8"
        ).read()
        m = re.search(r"^def _invoke_code_tester\(.*?(?=^def |^class )", src, re.M | re.S)
        assert m, "_invoke_code_tester not found"
        return m.group(0)

    def test_none_report_arm_precedes_the_unguarded_write(self):
        src = self._tester_src()
        i_guard = src.find(
            "elif probe_yield is not None and not isinstance(report, dict):"
        )
        i_unguarded = src.find("elif probe_yield is not None:")
        assert i_guard != -1, "the dead-verdict logging arm must exist"
        assert i_unguarded != -1
        assert i_guard < i_unguarded, (
            "the dead-verdict arm must run BEFORE the healthy-yield arm, or "
            "the unguarded report[...] write crashes on a dead invocation again"
        )

    def test_healthy_arm_does_not_synthesize_a_verdict(self):
        src = self._tester_src()
        i_unguarded = src.find("elif probe_yield is not None:")
        block = src[i_unguarded:i_unguarded + 1800]  # the arm body, not the fn
        assert "report = report or {}" not in block, (
            "resurrecting a dead tester's report as {} would read as a judged "
            "run in route_after_testing (the job-81 stale-verdict class)"
        )


# ───────────────────────── Model + partner projection ────────────────────


class TestStatusSurface:
    def test_status_choice_registered(self):
        from scraper.models import ScrapeJob

        assert ScrapeJob.STATUS_BROWSER_UNAVAILABLE == "browser_unavailable"
        assert any(c[0] == "browser_unavailable" for c in ScrapeJob.STATUS_CHOICES)

    def test_partner_projection_is_inprogress(self):
        from scraper.api.state import partner_state

        assert partner_state("browser_unavailable") == "inprogress"

    def test_beat_schedule_registered(self):
        from django.conf import settings

        entry = settings.CELERY_BEAT_SCHEDULE.get("resume-browser-unavailable")
        assert entry and entry["task"] == "scraper.tasks.resume_browser_unavailable_jobs"
        assert settings.CELERY_TASK_ROUTES.get(
            "scraper.tasks.resume_browser_unavailable_jobs"
        ) == "events"


# ─────────────────────────── Template stderr marker ──────────────────────


class TestTemplateMarker:
    def test_template_emits_navigate_unavailable_marker(self):
        src = open(
            os.path.join(ROOT, "templates", "http_navigation_scraper.py"),
            encoding="utf-8",
        ).read()
        assert "NAVIGATE_UNAVAILABLE:" in src, (
            "zero-discovery exit must carry the infra marker so run_execution "
            "can park instead of strategy-recycling"
        )
        assert '"navigate_unavailable"' in src
        assert src.count("_nav_unavailable_status") >= 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
