"""W9: lazy Scraper Chrome as a first-class state (+ honest headless option)
(docs/plans/browser-service-resilience-plan.md v2, commit 5).

The core locked fact: a naive lazy start is silently defeated ~45-90s after
boot — check_cdp_liveness probes BOTH ports unconditionally, 3 consecutive
failures later the liveness loop auto-restarts a Chrome that was never
started, and the "saved" ~250-400MB is bought back from inside a blocking
restart-lock hold. Lazy only works if EVERY consumer (liveness probe,
auto-restart, /health AND, restart endpoint) understands the lazy_idle
state; these tests pin each one.

browser_pool.py imports only stdlib, so it is loaded here BY PATH
(importlib), bypassing browser_service/__init__ (which imports server →
fastapi, absent in this image).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_w9_lazy_scraper_chrome.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

POOL_PATH = os.path.join(ROOT, "browser_service", "browser_pool.py")
SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")


class _FakeProc:
    def __init__(self):
        self.pid = 4242

    def poll(self):
        return None  # alive


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load(monkeypatch, lazy="1", headless="0", display=":99"):
    """Fresh browser_pool module with pinned env (module reads env at import)."""
    monkeypatch.setenv("SCRAPER_CHROME_LAZY", lazy)
    monkeypatch.setenv("CHROME_HEADLESS", headless)
    monkeypatch.setenv("DISPLAY", display)
    name = f"_bp_w9_{lazy}_{headless}_{display}"
    spec = importlib.util.spec_from_file_location(name, POOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_starters(bp, launches):
    bp._start_xvfb = lambda errors: None
    bp._start_mcp_chrome = lambda errors: None

    def _start_scraper(errors):
        launches.append(1)
        bp._scraper_chrome_proc = _FakeProc()  # launch succeeds

    bp._start_scraper_chrome = _start_scraper


class TestLazyBoot:
    def test_lazy_boot_never_starts_the_scraper_chrome(self, monkeypatch):
        mod = _load(monkeypatch, lazy="1")
        assert mod.SCRAPER_CHROME_LAZY is True, "W9 default: lazy ON"
        bp = mod.BrowserPool()
        launches: list = []
        _stub_starters(bp, launches)
        result = bp.startup()
        assert launches == [], "boot must not pay ~250-400MB for a /scrape-only Chrome"
        assert result["errors"] == []
        assert bp._ready is True, "lazy-idle is a HEALTHY boot state, not an error"
        assert bp.scraper_chrome_state() == "lazy_idle"
        assert bp.scraper_chrome_required() is False

    def test_eager_boot_opt_out_still_works(self, monkeypatch):
        mod = _load(monkeypatch, lazy="0")
        assert mod.SCRAPER_CHROME_LAZY is False
        bp = mod.BrowserPool()
        launches: list = []
        _stub_starters(bp, launches)
        bp.startup()
        assert launches == [1], "SCRAPER_CHROME_LAZY=0 restores eager start"
        assert bp.scraper_chrome_state() == "up"

    def test_default_flag_is_lazy_on(self):
        src = _read(POOL_PATH)
        assert 'os.environ.get("SCRAPER_CHROME_LAZY", "1")' in src, (
            "default ON is acceptable ONLY because the F1/F2-class guards are "
            "test-locked in the same commit"
        )


class TestEnsure:
    def test_ensure_launches_exactly_once(self, monkeypatch):
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()
        launches: list = []
        _stub_starters(bp, launches)
        assert bp.ensure_scraper_chrome() is True
        assert bp.ensure_scraper_chrome() is True
        assert bp.ensure_scraper_chrome() is True
        assert len(launches) == 1, "idempotent — every /scrape calls ensure"

    def test_ensure_is_lock_guarded(self, monkeypatch):
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()
        launches: list = []
        _stub_starters(bp, launches)
        barrier = threading.Barrier(4)
        results = []

        def worker():
            barrier.wait()
            results.append(bp.ensure_scraper_chrome())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(results)
        assert len(launches) == 1, "4 racing ensures → exactly one launch"

    def test_ensure_failure_reports_false_and_down(self, monkeypatch):
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()

        def _failing_start(errors):
            bp._scraper_chrome_started = True  # real method sets this on entry
            errors.append("Scraper Chrome exited immediately")
            bp._scraper_chrome_proc = None

        bp._start_scraper_chrome = _failing_start
        assert bp.ensure_scraper_chrome() is False
        assert bp.scraper_chrome_state() == "down", (
            "a FAILED launch is down, never lazy_idle (the started flag is set "
            "on any attempt)"
        )

    def test_restart_lock_is_reentrant(self):
        src = _read(POOL_PATH)
        assert "threading.RLock()" in src, (
            "restart_chrome→ensure_scraper_chrome nests a second acquire on "
            "the same thread — a plain Lock self-deadlocks"
        )


class TestLivenessUnderstandsLazy:
    def test_probe_skips_lazy_leg(self, monkeypatch):
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()
        out = bp.check_cdp_liveness()
        assert out["scraper_cdp_alive"] is None, "not-applicable, not False"
        assert out.get("scraper_chrome_state") == "lazy_idle"
        assert out["mcp_cdp_alive"] is False  # probed (nothing listening here)

    def test_probe_resumes_after_ensure(self, monkeypatch):
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()
        launches: list = []
        _stub_starters(bp, launches)
        bp.ensure_scraper_chrome()
        out = bp.check_cdp_liveness()
        assert out["scraper_cdp_alive"] is False, (
            "launched once → its CDP is probed again (nothing listens here)"
        )
        assert "scraper_chrome_state" not in out

    def test_four_liveness_ticks_never_restart_a_lazy_chrome(self, monkeypatch):
        """The plan's test: lazy boot + 4 ticks → restart never called, no PID.

        Mirrors server.py's _periodic_cdp_liveness guard sequence: the W7 skip
        (`scraper_not_required() → continue`) fires BEFORE the failure counter
        can reach the auto-restart threshold.
        """
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()
        restarts: list = []
        original_restart = mod.BrowserPool.restart_chrome

        def _spy_restart(label="all"):
            restarts.append(label)
            return original_restart(bp, label)

        bp.restart_chrome = _spy_restart
        for _ in range(4):  # > CDP_MAX_CONSECUTIVE_FAILURES (3)
            liveness = bp.check_cdp_liveness()
            alive = liveness.get("scraper_cdp_alive")
            if bp.scraper_not_required():
                continue  # the W7 skip (as in server.py)
            if not alive:
                bp.restart_chrome("scraper")
        assert restarts == []
        assert bp._scraper_chrome_proc is None, "the saved memory stays saved"

    def test_dead_after_ensure_still_restarts(self, monkeypatch):
        """The flip side: once ensured, a dead scraper Chrome is an incident."""
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()
        launches: list = []
        _stub_starters(bp, launches)
        bp.ensure_scraper_chrome()
        assert bp.scraper_not_required() is False
        assert bp.scraper_chrome_required() is True

    def test_health_none_safe_on_lazy_pool(self, monkeypatch):
        mod = _load(monkeypatch)
        bp = mod.BrowserPool()
        h = bp.health()  # must not raise — None.poll() was the trap
        assert h["scraper_chrome_running"] is False
        assert h["scraper_pid"] is None
        assert h["scraper_chrome_state"] == "lazy_idle"


class TestHeadlessOption:
    def test_headless_boot_skips_xvfb_and_starts_mcp(self, monkeypatch):
        mod = _load(monkeypatch, headless="1", display=":98")
        assert mod.MCP_HEADLESS is True
        bp = mod.BrowserPool()
        calls: list = []
        bp._start_xvfb = lambda errors: calls.append("xvfb")
        bp._start_mcp_chrome = lambda errors: calls.append("mcp")
        bp._start_scraper_chrome = lambda errors: calls.append("scraper")
        result = bp.startup()
        assert "xvfb" not in calls, "CHROME_HEADLESS=1 skips Xvfb"
        assert "mcp" in calls, "the MCP Chrome still starts (headless)"
        assert result["errors"] == []

    def test_headless_guard_matrix_in_source(self):
        src = _read(POOL_PATH)
        # both starters bail only when unheaded AND headless not requested
        assert src.count("not self._xvfb_proc and not MCP_HEADLESS") == 2
        # --display is conditional, never unconditional
        assert 'args.append(f"--display={DISPLAY}")' in src
        assert "mcp_headless = MCP_HEADLESS or not DISPLAY" in src
        # headless strips DISPLAY from the child env (no stray empty var)
        assert 'if not (mcp_headless and k == "DISPLAY")' in src
        # UA override present in the MCP args (headless must not advertise
        # HeadlessChrome)
        assert '"--user-agent=Mozilla/5.0 (X11; Linux x86_64)' in src

    def test_scrape_path_owns_the_lazy_launch(self):
        src = _read(SERVER_PATH)
        m = __import__("re").search(
            r"def _run_scrape_guarded\(.*?(?=\nasync def |\ndef )", src, __import__("re").S
        )
        assert m and "ensure_scraper_chrome" in m.group(0), (
            "first /scrape launches the lazy Chrome ON THE EXECUTOR (multi-"
            "second blocking start must never run on the event loop)"
        )

    def test_restart_of_lazy_chrome_is_ensure_not_force(self):
        src = _read(POOL_PATH)
        assert 'if self.scraper_chrome_state() == "lazy_idle":' in src
        assert "if self.ensure_scraper_chrome():" in src

    def test_docs_checkpoint_documents_the_lazy_log_shape(self):
        docs = _read(os.path.join(ROOT, "docs", "railway-migration.md"))
        assert "scraper_chrome_state=lazy_idle" in docs, (
            "the boot-log checkpoint must not read as a failure post-W9"
        )
        assert "CHROME_HEADLESS=1" in docs


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
