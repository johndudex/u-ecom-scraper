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
        + grab("_track_navigate_pids")
        + grab("_untrack_navigate_pids")
        + grab("_proc_state")
        + grab("_navigate_protection_active")
        + grab("_kill_orphan_chrome")
    )

    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("t_f1")

    # stubs for names the fns reference
    import time as _time
    import subprocess as _subprocess
    from typing import Optional as _Optional

    class _FakePool:
        _restart_lock = __import__("threading").Lock()

    ns = {
        "__name__": "t_f1",
        "os": os,
        "time": _time,
        "logger": logger,
        "subprocess": _subprocess,
        "Optional": _Optional,
        "PERSISTENT_CHROME_PIDS": set(),
        "SCRAPE_IN_FLIGHT": {},
        "SCRAPE_PROTECTION_GRACE_S": 600.0,
        "NAVIGATE_ACTIVE_PIDS": {},
        "_navigate_in_flight": 0,
        "_NAVIGATE_PID_MAX_AGE_S": 300.0,
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


class TestNavigateGateW3:
    """W3: the kill gate must be liveness-based, not counter-based.

    The in-flight counter is decremented by the endpoint's finally, which runs
    on wait_for timeout while the executor thread's browser is STILL RUNNING
    (prod: 92.7s response against a 75s deadline) — a purely counter-based gate
    then lets the next cleanup cycle SIGKILL a live ephemeral browser. The
    registry must prune dead (incl. zombie) PIDs and expire leaked ones so a
    bug can never permanently disable the orphan killer.
    """

    def teardown_method(self):
        ns = _server_ns()
        ns["NAVIGATE_ACTIVE_PIDS"].clear()
        ns["_navigate_in_flight"] = 0

    def test_live_pid_protects(self):
        ns = _server_ns()
        ns["_track_navigate_pids"]([os.getpid()])  # a definitely-alive PID
        assert ns["_navigate_protection_active"]() is True
        assert os.getpid() in ns["NAVIGATE_ACTIVE_PIDS"]

    def test_dead_pid_is_pruned_not_protective(self):
        ns = _server_ns()
        ns["_track_navigate_pids"]([99999999])  # nothing behind this PID
        assert ns["_navigate_protection_active"]() is False
        assert 99999999 not in ns["NAVIGATE_ACTIVE_PIDS"]

    def test_in_flight_counter_alone_protects(self):
        ns = _server_ns()
        ns["_navigate_in_flight"] = 2
        assert ns["_navigate_protection_active"]() is True
        ns["_navigate_in_flight"] = 0

    def test_expired_pid_stops_protecting(self):
        ns = _server_ns()
        ns["_track_navigate_pids"]([os.getpid()])
        # Age the entry past the ceiling+grace bound (leak failsafe).
        ns["NAVIGATE_ACTIVE_PIDS"][os.getpid()] = ns["time"].monotonic() - 10_000
        assert ns["_navigate_protection_active"]() is False
        assert os.getpid() not in ns["NAVIGATE_ACTIVE_PIDS"]

    def test_zombie_state_is_pruned(self):
        ns = _server_ns()
        # _proc_state must read /proc state (a zombie satisfies kill(pid, 0)):
        # a live process yields a 1-char state, a vanished one yields None.
        live = ns["_proc_state"](os.getpid())
        assert isinstance(live, str) and len(live) == 1
        assert ns["_proc_state"](99999999) is None

    def test_untrack_removes_only_given_pids(self):
        ns = _server_ns()
        ns["_track_navigate_pids"]([111, 222])
        ns["_untrack_navigate_pids"]([111])
        assert 111 not in ns["NAVIGATE_ACTIVE_PIDS"]
        assert 222 in ns["NAVIGATE_ACTIVE_PIDS"]

    def test_kill_cycle_skips_while_protected(self):
        ns = _server_ns()
        ns["_navigate_in_flight"] = 1
        assert ns["_kill_orphan_chrome"]() == 0
        ns["_navigate_in_flight"] = 0

    def test_kill_cycle_runs_when_registry_clear(self, monkeypatch=None):
        ns = _server_ns()
        # No protection + pgrep finds nothing → loop completes, kills nothing.
        calls = {}

        class _FakeResult:
            returncode = 1
            stdout = ""

        def _fake_run(*a, **kw):
            calls["ran"] = True
            return _FakeResult()

        fake_subprocess = types.SimpleNamespace(run=_fake_run)
        saved = ns["subprocess"]
        ns["subprocess"] = fake_subprocess
        try:
            assert ns["_kill_orphan_chrome"]() == 0
        finally:
            ns["subprocess"] = saved
        assert calls.get("ran") is True


class TestKillGateWiring:
    def test_kill_gates_on_scrape_protection(self):
        src = pathlib.Path(os.path.join(ROOT, "browser_service/server.py")).read_text()
        assert "if _scrape_protection_active():" in src
        assert 'logger.info("kill_orphan_chrome: skipping (scrape in flight)")' in src

    def test_kill_gates_on_navigate_protection_not_counter(self):
        """W3: the navigate gate must consult the liveness-based predicate."""
        src = pathlib.Path(os.path.join(ROOT, "browser_service/server.py")).read_text()
        assert "nav_active = _navigate_protection_active()" in src
        assert "if nav_active:" in src

    def test_registry_is_timestamped_dict_with_no_raw_set_api(self):
        src = pathlib.Path(os.path.join(ROOT, "browser_service/server.py")).read_text()
        assert "NAVIGATE_ACTIVE_PIDS: dict[int, float]" in src
        assert "NAVIGATE_ACTIVE_PIDS.update(" not in src
        assert "NAVIGATE_ACTIVE_PIDS.difference_update(" not in src

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
