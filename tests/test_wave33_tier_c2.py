"""Wave-33 T33-7 (C2): cancel at the caller deadline + cancel on terminal jobs.

Two halves (docs/plans/wave33-browser-oom-plan.md §C2):

- server: /scrape arms a ``loop.call_later(request.timeout + 30)`` per-run
  timer that ``request_cancel(rid)``s the run once its caller's HTTP read is
  dead — the run used to keep a Chrome + SCRAPE_EXECUTOR slot for its whole
  retry ladder after the caller left (the orphan-storm mechanism). rid-ONLY
  on purpose: ``request_cancel`` fans ``job_id`` out to every in-flight rid
  of the job, and a same-job retry POST (a NEW rid, fired by
  post_scrape_with_retry's ladder) must survive the previous run's timer.
  A run that already finished answers ``unknown`` — entries are
  unregistered at reap (scraper_runner ``_unregister_run`` in run_scraper's
  finally) — so the timer can never killpg a recycled PID.

- webapp: when run_execution's browser-service leg ends FAILED (including
  the infra-park normalization), cancel every in-flight rid of the job —
  today only the RUNNING-status watchdog cancels (tasks.py cleanup_stuck_jobs),
  so a job that failed client-side left its server-side run browsing for
  nobody.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave33_tier_c2.py -q
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pathlib  # noqa: E402
import re  # noqa: E402

SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")
RUN_EXEC_PATH = os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")


def _scrape_body() -> str:
    src = pathlib.Path(SERVER_PATH).read_text()
    return src.split('@app.post("/scrape")')[1].split('@app.post("/cancel")')[0]


def _grab(name: str, path: str = SERVER_PATH) -> str:
    src = pathlib.Path(path).read_text()
    m = re.search(
        rf"^((?:async )?def {name}\(.*?)(?=^(?:async )?def |^@|^class |\Z)",
        src,
        re.M | re.S,
    )
    assert m, f"{name} not found in {path}"
    return m.group(1)


# ── server: the caller-deadline timer ───────────────────────────────────────


class TestScrapeDeadlineTimer:
    def test_scrape_arms_a_caller_deadline_timer(self):
        body = _scrape_body()
        m = re.search(r"cancel_handle = loop\.call_later\(\s*request\.timeout \+ 30", body)
        assert m, "/scrape must arm a call_later(request.timeout + 30) deadline timer"
        i_arm = m.start()
        i_wait = body.index("result = await asyncio.wait_for(")
        assert i_arm < i_wait, "the timer must be armed before the wait_for"

    def test_deadline_timer_fires_rid_only(self):
        """job_id must NOT reach request_cancel from the timer — it fans out to
        every in-flight rid of the job and would kill the caller's own retry
        POST (new rid, same job)."""
        fn = _grab("_cancel_late_scrape")
        assert "request_cancel(rid=rid)" in fn
        assert "job_id=job_id" not in fn.split("request_cancel(")[1].split(")")[0]

    def test_deadline_timer_never_raises(self):
        import logging

        reports = []

        def fake_request_cancel(rid):
            reports.append(rid)
            raise RuntimeError("boom")

        fn_src = _grab("_cancel_late_scrape")
        # contract: the request_cancel call is wrapped in try/except
        assert "try:" in fn_src and "except Exception" in fn_src
        # behavioural: run the helper with the lazy import swapped for a
        # raising recorder — the fire must be swallowed
        body = fn_src.replace(
            "from .scraper_runner import request_cancel",
            "request_cancel = fake_request_cancel",
        )
        ns: dict = {
            "logger": logging.getLogger("t33c2"),
            "fake_request_cancel": fake_request_cancel,
        }
        exec(compile(body, "<cancel_late_live>", "exec"), ns)
        ns["_cancel_late_scrape"]("rid-1", 7)  # must not raise
        assert reports == ["rid-1"]

    def test_504_arm_cancels_as_belt(self):
        """The timer normally fires 90s BEFORE the 504 arm (timeout+30 vs
        +120); the direct call covers a starved event loop where the timer
        never got a slot."""
        body = _scrape_body()
        arm504 = body[body.index("except asyncio.TimeoutError:"):]
        head = arm504[: arm504.index("return JSONResponse(")]
        assert "_cancel_late_scrape(rid" in head, "504 arm must cancel before answering"

    def test_timer_released_in_finally(self):
        """A prompt result must release the timer — and the guard keeps the
        release safe when staging failed before the arming line ran."""
        body = _scrape_body()
        assert "cancel_handle = None" in body, "init before the outer try"
        fin = body[body.rindex("    finally:"):]
        assert "if cancel_handle is not None:" in fin
        assert "cancel_handle.cancel()" in fin

    def test_arm_uses_partial_with_rid_and_job(self):
        body = _scrape_body()
        assert re.search(
            r"cancel_handle = loop\.call_later\(\s*request\.timeout \+ 30,\s*"
            r"functools\.partial\(\s*_cancel_late_scrape,\s*rid,\s*request\.job_id,?\s*\),?\s*\)",
            body,
        ), "the timer carries the run's own rid (job_id is for the log line only)"


# ── webapp: terminal-job cancel ─────────────────────────────────────────────


class TestTerminalJobCancel:
    @staticmethod
    def _module():
        # `from agents.nodes import run_execution` binds the FUNCTION (the
        # graph's imports shadow the submodule attr) — resolve the module.
        import importlib

        return importlib.import_module("agents.nodes.run_execution")

    def _helper_ns(self, monkeypatch):
        re_mod = self._module()
        calls: list[int] = []
        monkeypatch.setattr(
            "agents.tools.browser_http.cancel_scrape",
            lambda job_id: calls.append(job_id) or {"requested": True},
        )
        return re_mod, calls

    def test_helper_guards_zero_job_id(self, monkeypatch):
        re_mod, calls = self._helper_ns(monkeypatch)
        re_mod._cancel_job_scrapes(0)
        assert calls == [], "job_id=0 (local/test runs) must not hit the service"

    def test_helper_cancels_by_job_id(self, monkeypatch):
        re_mod, calls = self._helper_ns(monkeypatch)
        re_mod._cancel_job_scrapes(7)
        assert calls == [7]

    def test_helper_swallows_transport_errors(self, monkeypatch):
        re_mod = self._module()

        def boom(job_id):
            raise RuntimeError("service down")

        monkeypatch.setattr("agents.tools.browser_http.cancel_scrape", boom)
        re_mod._cancel_job_scrapes(7)  # must not raise

    def test_failed_dispatch_wiring(self):
        """The browser-service leg's final result — through listing fallback,
        category merge, redispatch — hits the FAILED guard + cancel before
        return; the in-process leg (no /scrape in flight) does not."""
        src = pathlib.Path(RUN_EXEC_PATH).read_text()
        i_bs = src.index(
            "result = _maybe_retry_execution_listing(\n"
            "            result, state, _listing_url_env or _working_url, _redispatch_browser"
        )
        i_inproc = src.index(
            "return _maybe_retry_execution_listing(\n"
            "        result, state, _listing_url_env or _working_url, _redispatch_inprocess"
        )
        assert i_bs < i_inproc
        bs_leg = src[i_bs:i_inproc]
        assert 'get("execution_status") == "FAILED"' in bs_leg
        assert "_cancel_job_scrapes(job_id)" in bs_leg
        assert "return result" in bs_leg
        # and the helper is defined with the fire-and-forget contract
        helper = _grab("_cancel_job_scrapes", RUN_EXEC_PATH)
        assert "if not job_id:" in helper
        assert "except Exception" in helper
