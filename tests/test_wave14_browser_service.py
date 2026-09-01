"""[wave-14 PR-3] browser_service: run registry + /cancel, MCP serving
liveness, MCP tab reaper, stale run-dir sweep, PYTHONSAFEPATH.

Job-133/317 cluster: (a) django had NO way to stop a wedged /scrape subprocess
— the watchdog revoked the celery task but the subprocess (which has no celery
parent) kept burning the shared Scraper Chrome until its own timeout; (b) MCP
"liveness" only checked process-alive + Chrome-CDP, never "does the MCP server
actually SERVE" (a wedged SSE server burned agent budgets with 0 tools);
(c) abandoned MCP tabs (browser_tab_new without close) leaked renderers for the
life of the container; (d) hard-killed runs left /tmp/scrape_* behind forever;
(e) Python auto-prepends the script dir to sys.path, so job-317's stray
/tmp/bisect.py shadowed stdlib for every later run.

server.py imports fastapi (absent in the django test image), so its contracts
are asserted against SOURCE with the pure helpers extracted and executed (the
test_w4_w5/test_f1 pattern). scraper_runner.py imports only httpx → loaded
directly via a stub package (the test_browser_resilience pattern).

Run: docker compose exec -T -w /app/webapp django sh -c 'cd /app/webapp && PYTHONPATH=/app:/app/webapp python -m pytest ../tests/test_wave14_browser_service.py -q'
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")
RUNNER_PATH = os.path.join(ROOT, "browser_service", "scraper_runner.py")


# ─── loaders ────────────────────────────────────────────────────────────────


def _load_runner():
    """Load scraper_runner.py without the real package __init__ (fastapi)."""
    pkg_name = "browser_service"
    saved = sys.modules.get(pkg_name)
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules[pkg_name] = pkg
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_service.scraper_runner", RUNNER_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["browser_service.scraper_runner"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is not None:
            sys.modules[pkg_name] = saved
        else:
            sys.modules.pop(pkg_name, None)


@pytest.fixture()
def runner():
    mod = _load_runner()
    mod._ACTIVE_RUNS.clear()
    yield mod
    mod._ACTIVE_RUNS.clear()


def _src() -> str:
    with open(SERVER_PATH, encoding="utf-8") as fh:
        return fh.read()


def _grab(name: str) -> str:
    src = _src()
    m = re.search(rf"^def {name}\(.*?(?=^def |^class |^@)", src, re.M | re.S)
    assert m, f"{name} not found in server.py"
    return m.group(0)


class _FakeLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _exec_helper(name: str, extra_ns: dict | None = None):
    ns = {"__name__": "t_wave14_bs", "logger": _FakeLogger(), **(extra_ns or {})}
    exec(compile(_grab(name), f"<{name}>", "exec"), ns)
    return ns[name]


# ─── run registry + cancellation (scraper_runner) ───────────────────────────


class TestRunRegistry:
    def test_register_snapshot_unregister(self, runner):
        runner._register_run("scrape_aa", job_id=317, scraper_name="s.py")
        snap = runner.active_runs_snapshot()
        assert len(snap) == 1
        assert snap[0]["rid"] == "scrape_aa"
        assert snap[0]["job_id"] == 317
        assert snap[0]["cancelling"] is False
        assert "age_s" in snap[0]
        runner._unregister_run("scrape_aa")
        assert runner.active_runs_snapshot() == []

    def test_rids_for_job(self, runner):
        runner._register_run("scrape_a", job_id=9)
        runner._register_run("scrape_b", job_id=9)
        runner._register_run("scrape_c", job_id=8)
        assert sorted(runner._rids_for_job(9)) == ["scrape_a", "scrape_b"]
        assert runner._rids_for_job(0) == []

    def test_cancel_unknown_rid_is_reported_not_raised(self, runner):
        report = runner.request_cancel(rid="scrape_nope")
        assert report["unknown"] == ["scrape_nope"]
        assert report["flagged"] == []

    def test_cancel_by_job_id_flags_every_match(self, runner):
        runner._register_run("scrape_a", job_id=9)
        runner._register_run("scrape_b", job_id=9)
        report = runner.request_cancel(job_id=9)
        assert sorted(report["flagged"]) == ["scrape_a", "scrape_b"]
        assert runner._run_cancelled("scrape_a") and runner._run_cancelled("scrape_b")

    def test_cancel_sigkills_the_current_attempt(self, runner):
        """THE mechanism: a cancel must actually kill the running subprocess —
        the wedge we are fixing is a live subprocess, not just a flag."""
        proc = subprocess.Popen(
            ["sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            runner._register_run("scrape_kill", job_id=5)
            runner._ACTIVE_RUNS["scrape_kill"]["pid"] = proc.pid
            report = runner.request_cancel(job_id=5)
            assert report["killed"] == ["scrape_kill"]
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            assert proc.poll() is not None, "SIGKILL to the process group must reap the subprocess"
        finally:
            if proc.poll() is None:
                proc.kill()
            runner._unregister_run("scrape_kill")

    def test_cancelled_run_ends_promptly_end_to_end(self, runner):
        """Full path: run a slow script under a job id, cancel mid-flight, and
        the wrapper returns quickly instead of at the script's own pace."""
        import threading

        tmp = "/tmp/wave14_cancel_test"
        os.makedirs(tmp, exist_ok=True)
        script = os.path.join(tmp, "slow.py")
        with open(script, "w") as fh:
            fh.write("import time\nprint('start', flush=True)\ntime.sleep(25)\n")

        out: dict = {}

        def _go():
            out["result"] = runner.run_scraper_script(
                script, timeout=60, max_retries=1, rid="scrape_e2e", job_id=77,
            )

        th = threading.Thread(target=_go, daemon=True)
        th.start()
        for _ in range(50):  # wait until the run is registered + pid published
            snap = [r for r in runner.active_runs_snapshot() if r["rid"] == "scrape_e2e"]
            if snap and runner._ACTIVE_RUNS["scrape_e2e"].get("pid"):
                break
            time.sleep(0.1)
        t0 = time.monotonic()
        report = runner.request_cancel(job_id=77)
        th.join(timeout=15)
        elapsed = time.monotonic() - t0
        assert report["flagged"] == ["scrape_e2e"]
        assert not th.is_alive(), "cancel must end the run, not leave the wrapper hanging"
        assert elapsed < 10, f"cancel took {elapsed:.1f}s — subprocess not killed"
        assert out["result"]["returncode"] != 0
        runner._unregister_run("scrape_e2e")

    def test_wrapper_unregisters_after_return(self, runner):
        tmp = "/tmp/wave14_cancel_test"
        os.makedirs(tmp, exist_ok=True)
        script = os.path.join(tmp, "quick.py")
        with open(script, "w") as fh:
            fh.write("print('done')\n")
        runner.run_scraper_script(script, timeout=60, max_retries=1, rid="scrape_gone")
        assert runner.active_runs_snapshot() == []


class TestRunEnvHardening:
    def test_safe_path_is_set_and_pythonpath_is_deliberate(self, runner, tmp_path):
        """job-317: Python auto-prepends the script dir, letting a stray
        module shadow stdlib. The subprocess must run with -P semantics AND
        an explicit PYTHONPATH that names exactly /app + the run dir."""
        script = tmp_path / "probe_env.py"
        script.write_text(
            "import os, sys\n"
            "print('safe_path=%d' % sys.flags.safe_path)\n"
            "print('pypath=' + os.environ.get('PYTHONPATH', ''))\n"
        )
        result = runner._run_scraper_script_impl(str(script), timeout=60, max_retries=1)
        assert result["returncode"] == 0, result["stderr"][:300]
        assert "safe_path=1" in result["stdout"]
        line = [ln for ln in result["stdout"].splitlines() if ln.startswith("pypath=")][0]
        parts = line[len("pypath="):].split(os.pathsep)
        assert "/app" in parts
        assert str(tmp_path) in parts

    def test_source_pins_env_and_cancel_checks(self):
        src = open(RUNNER_PATH, encoding="utf-8").read()
        assert 'env["PYTHONSAFEPATH"] = "1"' in src
        assert 'env["PYTHONPATH"] = os.pathsep.join(' in src
        # cancel between attempts: the retry loop checks the flag BEFORE spawning
        i_loop = src.index("for attempt in range(1, max_retries + 1):")
        i_cancel = src.index("if rid and _run_cancelled(rid):", i_loop)
        i_spawn = src.index("subprocess.Popen(", i_loop)
        assert i_loop < i_cancel < i_spawn
        # and the pid is published so /cancel can killpg the CURRENT attempt
        assert '_ACTIVE_RUNS[rid]["pid"] = proc.pid' in src


# ─── MCP serving liveness + log rotation (server helpers, exec'd) ───────────


class TestMcpHttpProbe:
    def test_up_when_httpx_answers(self, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: object())
        # os is in the ns because the grabbed source also executes the
        # module-level constant assignments that follow the def (RUN_DIR_MAX_AGE_S).
        ns = {
            "os": os,
            "deque": __import__("collections").deque,
            "MCP_HTTP_PORT": 8111,
            "MCP_HTTP_PROBE_TIMEOUT_S": 0.2,
            "_MCP_HTTP_CACHE": {"state": "unknown", "checked_at": 0.0, "error": ""},
            "time": time,
            "logger": _FakeLogger(),
        }
        exec(compile(_grab("_probe_mcp_http"), "<p>", "exec"), ns)
        ns["_probe_mcp_http"]()
        assert ns["_MCP_HTTP_CACHE"]["state"] == "up"
        assert ns["_MCP_HTTP_CACHE"]["error"] == ""
        assert ns["_MCP_HTTP_CACHE"]["checked_at"] > 0

    def test_down_when_httpx_refuses(self, monkeypatch):
        import httpx

        def _boom(*a, **k):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "get", _boom)
        ns = {
            "os": os,
            "deque": __import__("collections").deque,
            "MCP_HTTP_PORT": 8111,
            "MCP_HTTP_PROBE_TIMEOUT_S": 0.2,
            "_MCP_HTTP_CACHE": {"state": "up", "checked_at": 1.0, "error": ""},
            "time": time,
            "logger": _FakeLogger(),
        }
        exec(compile(_grab("_probe_mcp_http"), "<p>", "exec"), ns)
        ns["_probe_mcp_http"]()
        assert ns["_MCP_HTTP_CACHE"]["state"] == "down"
        assert "ConnectError" in ns["_MCP_HTTP_CACHE"]["error"]

    def test_wired_into_the_liveness_loop_not_health(self):
        src = _src()
        i_loop = src.index("async def _periodic_cdp_liveness")
        i_next = src.index("def _cleanup_chrome_artifacts_sync", i_loop)
        block = src[i_loop:i_next]
        assert "_probe_mcp_http" in block, "probe must run inside the 15s liveness loop"
        i_probe_def = src.index("def _probe_mcp_http(")
        i_health = src.index("async def health(")
        # health() must only READ the cache (the probe def sits before health,
        # and health's body never calls the probe).
        assert i_probe_def < i_health
        health_block = src[i_health:i_health + 4000]
        assert "_probe_mcp_http(" not in health_block


class TestMcpLogRotation:
    def test_appends_and_rotates_past_budget(self, tmp_path):
        log = tmp_path / "mcp-stdout.log"
        log.write_text("old-stdout-must-not-be-destroyed\n")
        rotated = tmp_path / "mcp-stdout.log.1"

        ns = {
            "MCP_LOG_PATH": str(log),
            "MCP_LOG_MAX_BYTES": 5,  # tiny → forces the rotate branch
            "os": os,
            "logger": _FakeLogger(),
        }
        fn = _exec_helper("_rotate_mcp_log", ns)
        target = fn()
        assert target == str(log)
        assert rotated.exists()
        assert "old-stdout-must-not-be-destroyed" in rotated.read_text()

        # append mode: new content lands in the base file, history survives
        with open(target, "a") as fh:
            fh.write("new-generation\n")
        assert "old-stdout-must-not-be-destroyed" not in log.read_text()
        assert "new-generation" in log.read_text()

    def test_missing_log_is_not_an_error(self, tmp_path):
        ns = {
            "MCP_LOG_PATH": str(tmp_path / "absent.log"),
            "MCP_LOG_MAX_BYTES": 5,
            "os": os,
            "logger": _FakeLogger(),
        }
        fn = _exec_helper("_rotate_mcp_log", ns)
        assert fn() == str(tmp_path / "absent.log")

    def test_pin_is_the_installed_version_and_reaches_the_command(self):
        src = _src()
        assert 'MCP_PACKAGE_SPEC = os.environ.get("MCP_PLAYWRIGHT_SPEC", "@playwright/mcp@0.0.78")' in src
        i_def = src.index("MCP_PACKAGE_SPEC =")
        i_start = src.index("async def _start_mcp_process")
        block = src[i_start:]
        assert "MCP_PACKAGE_SPEC," in block, "mcp_cmd must launch the pinned spec, not a hardcoded @playwright/mcp"


# ─── MCP tab reaper + stale run-dir sweep (server helpers, exec'd) ──────────


class _FakeUrlopen:
    """Routes /json/list and /json/close/{id}; records closes."""

    def __init__(self, targets):
        self.targets = targets
        self.closed: list[str] = []

    def __call__(self, url, timeout=None):
        import io

        if url.endswith("/json/list"):
            return io.BytesIO(json.dumps(self.targets).encode())
        tid = url.rsplit("/json/close/", 1)[-1]
        self.closed.append(tid)
        return io.BytesIO(b'"Target is closing"')


def _tab(url, type_="page", tid=None):
    return {"id": tid or url.rsplit("/", 1)[-1], "type": type_, "url": url}


def _reap_fn(extra):
    ns = {
        "MCP_CDP_PORT": 9222,
        "MCP_TAB_KEEP": 4,
        "_MCP_PAGE_COUNT": {"count": None, "checked_at": 0.0},
        "_mcp_client_connected": lambda: False,
        "time": time,
        "logger": _FakeLogger(),
        "urllib": urllib,
        **extra,
    }
    exec(compile(_grab("_reap_mcp_tabs_sync"), "<reap>", "exec"), ns)
    return ns


class TestTabReaper:
    def test_closes_newest_excess_keeps_oldest_and_first_tab(self):
        targets = [
            _tab("https://x.com/1", tid="t1"),   # the MCP server's original tab
            _tab("chrome://version", type_="page", tid="t2"),  # non-http — never counted
            _tab("https://x.com/3", tid="t3"),
            _tab("https://x.com/4", tid="t4"),
            _tab("https://x.com/5", tid="t5"),
            _tab("https://x.com/6", tid="t6"),
            _tab("https://x.com/7", tid="t7"),   # excess (newest)
            _tab("service_worker", type_="service_worker", tid="t8"),
        ]
        fake = _FakeUrlopen(targets)
        ns = _reap_fn({})
        # the helper imports urllib.request itself — patch the module attr
        import urllib.request

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            report = ns["_reap_mcp_tabs_sync"]()
        finally:
            urllib.request.urlopen = orig

        assert report == {"kept": 4, "closed": 2}
        assert fake.closed == ["t6", "t7"]
        # gauge counts http(s) page targets only
        assert ns["_MCP_PAGE_COUNT"]["count"] == 6

    def test_under_budget_is_a_noop(self):
        targets = [_tab(f"https://x.com/{i}", tid=f"t{i}") for i in range(3)]
        fake = _FakeUrlopen(targets)
        ns = _reap_fn({"_fake": fake})
        import urllib.request

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            report = ns["_reap_mcp_tabs_sync"]()
        finally:
            urllib.request.urlopen = orig
        assert report == {"kept": 3, "closed": 0}
        assert fake.closed == []
        assert ns["_MCP_PAGE_COUNT"]["count"] == 3

    def test_skips_while_an_mcp_client_is_connected(self):
        targets = [_tab(f"https://x.com/{i}", tid=f"t{i}") for i in range(9)]
        fake = _FakeUrlopen(targets)
        ns = _reap_fn({"_mcp_client_connected": lambda: True, "_fake": fake})
        import urllib.request

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            report = ns["_reap_mcp_tabs_sync"]()
        finally:
            urllib.request.urlopen = orig
        assert report["skipped"] == "mcp_client_connected"
        assert fake.closed == []
        # the gauge is STILL refreshed even on skip
        assert ns["_MCP_PAGE_COUNT"]["count"] == 9

    def test_cdp_unavailable_is_a_clean_skip(self):
        def _refuse(*a, **k):
            raise OSError("no chrome")

        ns = _reap_fn({})
        import urllib.request

        orig = urllib.request.urlopen
        urllib.request.urlopen = _refuse
        try:
            report = ns["_reap_mcp_tabs_sync"]()
        finally:
            urllib.request.urlopen = orig
        assert report == {"skipped": "cdp_unavailable"}
        assert ns["_MCP_PAGE_COUNT"]["count"] is None

    def test_wired_into_periodic_cleanup_with_client_gate(self):
        src = _src()
        i_cleanup = src.index("async def _periodic_cleanup")
        i_liveness = src.index("async def _periodic_cdp_liveness")
        block = src[i_cleanup:i_liveness]
        assert "_reap_mcp_tabs_sync" in block
        assert "_sweep_stale_run_dirs" in block
        # the client-connected gate is fail-CLOSED (never reap under uncertainty)
        gate = _grab("_mcp_client_connected")
        assert "return True" in gate


class TestStaleRunDirSweep:
    def test_old_dirs_removed_fresh_kept(self):
        import shutil as sh

        old = "/tmp/scrape_test_old_dir"
        fresh = "/tmp/scrape_test_fresh_dir"
        for d in (old, fresh):
            sh.rmtree(d, ignore_errors=True)
            os.makedirs(d)
        week_ago = time.time() - 7 * 86400
        os.utime(old, (week_ago, week_ago))

        ns = {"os": os, "shutil": sh, "time": time, "logger": _FakeLogger()}
        fn = _exec_helper("_sweep_stale_run_dirs", ns)
        report = fn(max_age_s=3600)  # 1h — old is 7d, fresh is now
        assert report["swept"] >= 1
        assert not os.path.exists(old)
        assert os.path.exists(fresh)
        sh.rmtree(fresh, ignore_errors=True)

    def test_default_age_is_six_hours_via_source(self):
        src = _grab("_sweep_stale_run_dirs")
        assert "RUN_DIR_MAX_AGE_S" in src
        assert 'RUN_DIR_MAX_AGE_S = float(os.environ.get("RUN_DIR_MAX_AGE_S", str(6 * 3600)))' in _src()


# ─── /cancel endpoint + /health + job_id plumbing (source contracts) ────────


class TestCancelEndpointContract:
    def test_endpoint_is_lock_free_and_wired(self):
        src = _src()
        i_ep = src.index('@app.post("/cancel")')
        i_end = src.index("@app.post", i_ep + 10)
        block = src[i_ep:i_end]
        assert "async with PROBE_LOCK" not in block, (
            "cancel must never wait on a lock a wedged probe holds"
        )
        assert "request_cancel(" in block
        assert "active_runs_snapshot()" in block

    def test_cancel_request_requires_rid_or_job_id(self):
        src = _src()
        i_ep = src.index('@app.post("/cancel")')
        block = src[i_ep:i_ep + 2000]
        assert "if not request.rid and not request.job_id:" in block
        assert "status_code=400" in block

    def test_scrape_job_id_reaches_the_runner(self):
        src = _src()
        assert "job_id: int = 0" in src  # ScrapeRequest field
        i_partial = src.index("functools.partial(")
        block = src[i_partial:i_partial + 600]
        assert "job_id=request.job_id" in block

    def test_runner_accepts_job_id_and_reports_cancelled(self):
        src = open(RUNNER_PATH, encoding="utf-8").read()
        assert "job_id: int = 0," in src
        assert '"cancelled": True' in src
        assert "def request_cancel(" in src


class TestHealthAndWatchdogSurfaces:
    def test_health_reports_mcp_state_tabs_and_runs_without_probing(self):
        src = _src()
        i_health = src.index("async def health(")
        block = src[i_health:i_health + 4000]
        assert '"mcp_http_state": dict(_MCP_HTTP_CACHE)' in block
        assert '"mcp_page_count": dict(_MCP_PAGE_COUNT)' in block
        assert '"scraper_runs": active_runs_snapshot()' in block

    def test_stale_run_dir_gauge_is_deadline_guarded(self):
        block = _grab("_health_gauges")
        assert '"stale_run_dirs"' in block
        i_gauge = block.index('"stale_run_dirs"')
        # the expensive part must sit under a monotonic deadline check
        assert "time.monotonic() < deadline" in block[i_gauge:i_gauge + 400]

    def test_watchdog_cancels_browser_service_runs(self):
        src = open(
            os.path.join(ROOT, "webapp", "scraper", "tasks.py"), encoding="utf-8"
        ).read()
        i_revoke = src.index('current_app.control.revoke(_task_id, terminate=True, signal="SIGKILL")')
        i_cancel = src.index("cancel_scrape(job.id)", i_revoke)
        assert i_revoke < i_cancel, "browser_service cancel must ride the watchdog revoke path"

    def test_cancel_client_is_fire_and_forget(self):
        src = open(
            os.path.join(ROOT, "webapp", "agents", "tools", "browser_http.py"),
            encoding="utf-8",
        ).read()
        assert "def cancel_scrape(" in src
        block = src[src.index("def cancel_scrape("):]
        assert "timeout=10.0" in block, "a cancel must use a SHORT timeout"
        assert "f\"{BROWSER_SERVICE_URL}/cancel\"" in block


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
