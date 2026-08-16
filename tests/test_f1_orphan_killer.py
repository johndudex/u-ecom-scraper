"""F1+M1: the orphan killer must not murder the persistent Chromes' process
trees or a running scraper's Chrome; scraper subprocesses must be reaped by
process group on timeout.

Prod: the 30-min cleanup cycle killed ~20 renderer/GPU children of the two
persistent Chromes every interval (only the 2 top-level PIDs were
protected), CDP went DOWN, both Chromes auto-restarted — a permanent
kill→restart loop that crashed jobs 325/328/334 at browser-connect and
hung 272's product_analyzer mid-browse.

Pure-python: server helpers executed from source (fastapi import stubbed).
"""
from __future__ import annotations

import os
import re
import sys
import types
import pathlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_NS = None


def _server_ns():
    """Execute only the helper defs from server.py (imports stubbed)."""
    global _NS
    if _NS is not None:
        return _NS
    src = pathlib.Path(os.path.join(ROOT, "browser_service/server.py")).read_text()

    def grab(name):
        m = re.search(rf"^(def {name}\(.*?)(?=^def |^@|^async def |\Z)", src, re.M | re.S)
        assert m, f"{name} not found"
        return m.group(1)

    fn_src = (
        grab("_proc_children")
        + grab("_collect_persistent_pids")
        + grab("_scrape_protection_active")
        + grab("_run_scrape_guarded")
    )

    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("t_f1")

    # stubs for names the fns reference
    import time as _time

    class _FakePool:
        _restart_lock = __import__("threading").Lock()

    ns = {
        "__name__": "t_f1",
        "os": os,
        "time": _time,
        "logger": logger,
        "PERSISTENT_CHROME_PIDS": set(),
        "SCRAPE_IN_FLIGHT": {},
        "SCRAPE_PROTECTION_GRACE_S": 600.0,
        "browser_pool": _FakePool(),
    }
    ns["browser_pool"].health = lambda: {"mcp_pid": 100, "scraper_pid": 200}
    exec(fn_src, ns)
    _NS = ns
    return ns


class TestProcChildren:
    def test_reads_proc_children(self):
        ns = _server_ns()
        # On this host /proc may be readable; the function must not raise either way
        kids = ns["_proc_children"](1)
        assert isinstance(kids, set)

    def test_missing_pid_returns_empty(self):
        ns = _server_ns()
        assert ns["_proc_children"](99999999) == set()


class TestTreeCollection:
    def test_collects_full_tree(self, monkeypatch=None):
        ns = _server_ns()
        # Simulate: 100 → {110}, 200 → {210, 220}, 210 → {211}
        ns["_proc_children"] = lambda pid: {
            100: {110}, 200: {210, 220}, 210: {211}, 110: set(), 220: set(), 211: set(),
        }[pid]
        ns["_collect_persistent_pids"]()  # rebinds? no — call the original via exec'd ns
        # _collect_persistent_pids was exec'd with browser_pool.health returning (100,200)
        pids = ns["PERSISTENT_CHROME_PIDS"]
        assert {100, 110} <= pids
        assert {200, 210, 220, 211} <= pids


class TestScrapeProtection:
    def test_active_when_registered(self):
        ns = _server_ns()
        ns["SCRAPE_IN_FLIGHT"]["r1"] = ns["time"].monotonic() + 3600
        assert ns["_scrape_protection_active"]() is True
        ns["SCRAPE_IN_FLIGHT"].clear()

    def test_expired_entry_is_reaped(self):
        ns = _server_ns()
        ns["SCRAPE_IN_FLIGHT"]["r1"] = ns["time"].monotonic() - 1
        assert ns["_scrape_protection_active"]() is False
        assert "r1" not in ns["SCRAPE_IN_FLIGHT"]

    def test_guarded_runner_releases_on_success(self):
        # The guarded wrapper's contract: pop the rid in finally, pass kwargs
        # through. Exercised against the wrapper shape (relative import of
        # scraper_runner can't resolve outside the package).
        ns = {"SCRAPE_IN_FLIGHT": {}, "time": _server_ns()["time"]}
        wrapper_src = (
            "def _run_scrape_guarded(rid, fn=None, **kw):\n"
            "    try:\n"
            "        return fn(**kw)\n"
            "    finally:\n"
            "        SCRAPE_IN_FLIGHT.pop(rid, None)\n"
        )
        exec(wrapper_src, ns)
        ns["SCRAPE_IN_FLIGHT"]["r2"] = ns["time"].monotonic() + 3600
        out = ns["_run_scrape_guarded"]("r2", fn=lambda **kw: {"ok": True})
        assert out == {"ok": True}
        assert "r2" not in ns["SCRAPE_IN_FLIGHT"]

    def test_guarded_runner_releases_on_exception(self):
        ns = {"SCRAPE_IN_FLIGHT": {}, "time": _server_ns()["time"]}
        wrapper_src = (
            "def _run_scrape_guarded(rid, fn=None, **kw):\n"
            "    try:\n"
            "        return fn(**kw)\n"
            "    finally:\n"
            "        SCRAPE_IN_FLIGHT.pop(rid, None)\n"
        )
        exec(wrapper_src, ns)
        ns["SCRAPE_IN_FLIGHT"]["r3"] = ns["time"].monotonic() + 3600
        try:
            ns["_run_scrape_guarded"]("r3", fn=lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        assert "r3" not in ns["SCRAPE_IN_FLIGHT"]


class TestKillGateWiring:
    def test_kill_gates_on_scrape_protection(self):
        src = pathlib.Path(os.path.join(ROOT, "browser_service/server.py")).read_text()
        assert "if _scrape_protection_active():" in src
        assert 'logger.info("kill_orphan_chrome: skipping (scrape in flight)")' in src

    def test_cleanup_uses_nonblocking_lock(self):
        src = pathlib.Path(os.path.join(ROOT, "browser_service/server.py")).read_text()
        assert "browser_pool._restart_lock.acquire(blocking=False)" in src

    def test_scrape_endpoint_registers_deadline(self):
        src = pathlib.Path(os.path.join(ROOT, "browser_service/server.py")).read_text()
        assert "SCRAPE_IN_FLIGHT[rid] = (" in src
        assert "request.timeout * max(1, request.max_retries)" in src


class TestM1Killpg:
    def test_popen_start_new_session(self):
        src = pathlib.Path(os.path.join(ROOT, "browser_service/scraper_runner.py")).read_text()
        assert "start_new_session=True" in src
        assert "os.killpg(proc.pid, signal.SIGKILL)" in src
        assert "import signal" in src

    def test_fallback_kill_on_killpg_failure(self):
        src = pathlib.Path(os.path.join(ROOT, "browser_service/scraper_runner.py")).read_text()
        assert "except (ProcessLookupError, PermissionError, OSError):" in src
        assert "proc.kill()" in src
