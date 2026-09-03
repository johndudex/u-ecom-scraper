"""Browser-service resilience: W1 launch-failure orphan guard + W2 process
groups/tini (docs/plans/browser-service-resilience-plan.md, v2).

W1: a browser launch that raises between driver-start and page-ready used to
leak the node driver AND its whole Chrome tree — the caller's finally closes a
still-None context. Under prod's memory pressure this was the doom-loop link
(failed launch → deeper pressure → next launch fails). The guard SIGKILLs the
partial tree (never a graceful close: under that same pressure the driver may
be unresponsive and a blocking close would hold a NAVIGATE executor slot) and
re-raises.

The django/celery test image installs neither playwright nor cloakbrowser
(they live in browser_service/requirements.txt, baked only into the
browser-service image), so the launch paths run against fake modules. probe.py
imports playwright INSIDE _launch_page, so a sys.modules fake is picked up
cleanly. probe.py itself is loaded under a stubbed package: the real
browser_service/__init__.py imports server.py → fastapi, which the test image
does not have.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_browser_resilience.py -q
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import signal
import sys
import threading
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")

FAKE_DRIVER_PID = 424242  # nothing lives behind this PID on a test host


def _load_probe():
    """Load probe.py as browser_service.probe without triggering the real
    package __init__ (which imports server.py → fastapi)."""
    pkg_name = "browser_service"
    saved_pkg = sys.modules.get(pkg_name)
    saved_probe = sys.modules.pop("browser_service.probe", None)
    saved_cfg = sys.modules.pop("browser_service.config", None)

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules[pkg_name] = pkg
    try:
        spec = importlib.util.spec_from_file_location("browser_service.probe", PROBE_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["browser_service.probe"] = mod
        spec.loader.exec_module(mod)
    finally:
        if saved_pkg is not None:
            sys.modules[pkg_name] = saved_pkg
        else:
            sys.modules.pop(pkg_name, None)
        sys.modules.pop("browser_service.probe", None)
        if saved_probe is not None:
            sys.modules["browser_service.probe"] = saved_probe
        if saved_cfg is not None:
            sys.modules["browser_service.config"] = saved_cfg
    return mod


# ── fake playwright / cloakbrowser machinery ────────────────────────────────


class _FakeProc:
    pid = FAKE_DRIVER_PID


class _FakeTransport:
    _proc = _FakeProc()


class _FakePage:
    def __init__(self):
        self.default_timeout = None

    def set_default_timeout(self, ms):
        self.default_timeout = ms


class _FakeBrowser:
    """A launched browser: page creation succeeds. The driver PID is
    reachable via the sync-Browser private chain the guard falls back to."""

    def __init__(self):
        self._impl_obj = types.SimpleNamespace(
            _connection=types.SimpleNamespace(_transport=_FakeTransport())
        )
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


class _FakeBrowserPageFails(_FakeBrowser):
    def new_page(self):
        raise RuntimeError("new_page: Resource temporarily unavailable")


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, **kwargs):
        return self._browser


class _FakePw:
    """The sync_playwright().start() handle: transport + chromium."""

    def __init__(self, browser):
        self._transport = _FakeTransport()
        self.chromium = _FakeChromium(browser)


class _UnshapedPw:
    """A Playwright handle whose private layout changed → PID unattributable."""

    _transport = object()

    class chromium:  # minimal namespace, not a real class API
        @staticmethod
        def launch(**kwargs):
            raise RuntimeError("Resource temporarily unavailable")


def _install_fake_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    mod.__path__ = []  # importable as a package parent without a real dir
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture()
def probe(monkeypatch):
    mod = _load_probe()
    # Keep the proxy config out of the picture: _launch_page only asks it to
    # build a proxy URL when proxy_tier != "none", and these tests use "none".
    monkeypatch.setattr(
        mod,
        "get_proxy_config",
        lambda: types.SimpleNamespace(build_playwright_proxy=lambda *a, **k: None),
    )
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: kills.append((pid, sig)), raising=True
    )
    mod._KILLS = kills  # test handle
    return mod


class TestW1LaunchFailureGuard:
    def test_playwright_launch_failure_kills_driver_tree_and_reraises(self, probe, monkeypatch):
        """launch() raising → SIGKILL to the driver PID (tree root) + the
        original error still propagates to the caller."""
        _install_fake_module(
            monkeypatch,
            "playwright",
        )
        _install_fake_module(
            monkeypatch,
            "playwright.sync_api",
            sync_playwright=lambda: types.SimpleNamespace(
                start=lambda: _FakePw(_FakeBrowserPageFails())
            ),
        )
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            probe._launch_page(method="playwright", proxy_tier="none", timeout=5)
        assert (FAKE_DRIVER_PID, signal.SIGKILL) in probe._KILLS

    def test_playwright_start_failure_is_survivable_without_kill(self, probe, monkeypatch):
        """If sync_playwright().start() itself raises, nothing was spawned on
        our side — re-raise without signalling anything (the residual leak is
        the orphan killer's job; documented, not hidden)."""
        _install_fake_module(monkeypatch, "playwright")

        def _boom():
            raise RuntimeError("driver spawn failed")

        _install_fake_module(
            monkeypatch,
            "playwright.sync_api",
            sync_playwright=lambda: types.SimpleNamespace(start=_boom),
        )
        with pytest.raises(RuntimeError, match="driver spawn failed"):
            probe._launch_page(method="playwright", proxy_tier="none", timeout=5)
        assert probe._KILLS == []

    def test_cloak_new_page_failure_kills_browser_driver(self, probe, monkeypatch):
        """The cloak path has no pw handle — the guard must attribute the
        driver through the browser object instead."""
        _install_fake_module(
            monkeypatch,
            "cloakbrowser",
            launch=lambda **kwargs: _FakeBrowserPageFails(),
        )
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            probe._launch_page(method="cloak", proxy_tier="none", timeout=5)
        assert (FAKE_DRIVER_PID, signal.SIGKILL) in probe._KILLS

    def test_unattributable_driver_kills_nothing_and_still_reraises(self, probe, monkeypatch):
        """Private-API shape changed → no kill (wrong-process kills are worse
        than a reaped-by-orphan-killer leak)."""
        _install_fake_module(monkeypatch, "playwright")
        _install_fake_module(
            monkeypatch,
            "playwright.sync_api",
            sync_playwright=lambda: types.SimpleNamespace(start=lambda: _UnshapedPw()),
        )
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            probe._launch_page(method="playwright", proxy_tier="none", timeout=5)
        assert probe._KILLS == []

    def test_hard_kill_tree_is_depth_bounded_and_counts(self, probe):
        """The BFS must terminate on cycles (A→B→A) and report signalled PIDs."""
        children = {1: {2}, 2: {3}, 3: {1}}  # deliberate cycle
        n = probe._hard_kill_tree(1, _list_children=lambda pid: children.get(pid, set()))
        assert n == 3
        assert [pid for pid, sig in probe._KILLS] == [1, 2, 3]
        assert all(sig == signal.SIGKILL for _, sig in probe._KILLS)

    def test_playwright_driver_pid_guarded_chains(self, probe):
        assert probe._playwright_driver_pid(_FakePw(None)) == FAKE_DRIVER_PID
        assert probe._playwright_driver_pid(_FakeBrowser()) == FAKE_DRIVER_PID
        assert probe._playwright_driver_pid(object()) is None


# ── W2: process groups + tini (source contracts; the real processes only
#    exist inside the browser-service image) ────────────────────────────────


class TestW2ProcessGroups:
    def test_pool_children_get_own_session(self):
        src = pathlib.Path(
            os.path.join(ROOT, "browser_service", "browser_pool.py")
        ).read_text()
        # Xvfb + MCP Chrome + Scraper Chrome: each in its own group so the
        # restart escalation can killpg the whole tree.
        assert src.count("start_new_session=True,") == 3

    def test_restart_and_shutdown_escalate_to_killpg(self):
        src = pathlib.Path(
            os.path.join(ROOT, "browser_service", "browser_pool.py")
        ).read_text()
        assert "import signal" in src
        assert "def _kill_process_tree" in src
        assert "os.killpg(os.getpgid(proc.pid), signal.SIGKILL)" in src
        # all three wedged-process paths (W9 lazy-stop, generic stop, shutdown)
        # route through it — [wave-16 B1] added the third with the lazy Chrome
        assert src.count("self._kill_process_tree(proc)") == 3
        # the only per-PID kill left is _kill_process_tree's group-gone fallback
        helper = re.search(
            r"def _kill_process_tree.*?(?=\n    def |\Z)", src, re.DOTALL
        ).group(0)
        assert "proc.kill()" not in src.replace(helper, "")


class TestW2Tini:
    def test_tini_installed_in_first_apt_layer(self):
        path = os.path.join(ROOT, "browser_service", "Dockerfile")
        src = pathlib.Path(path).read_text()
        # tini must join the FIRST apt RUN — the apt lists are rm'd at the end
        # of it, so a later bare `apt-get install tini` would fail the build.
        first_run = src.split("linux_signing_key")[0]
        assert "tini" in first_run

    def test_cmd_execs_tini_as_pid1(self):
        path = os.path.join(ROOT, "browser_service", "Dockerfile")
        src = pathlib.Path(path).read_text()
        assert "exec tini -- uvicorn" in src
        # preamble (core-dump ulimit, LD_LIBRARY_PATH/PYTHONPATH) must be
        # exported BEFORE the exec so tini's children inherit it
        assert src.index("ulimit -c 0") < src.index("exec tini -- uvicorn")
        assert src.index("export PYTHONPATH=/app") < src.index("exec tini -- uvicorn")


# ── [poison RCA 2026-09-03] ephemeral-launch thread poisoning ───────────────
# _PageContext.close() ran the graceful teardown on a dedicated worker thread.
# Playwright sync objects are bound to the greenlet of their creating thread,
# so browser.close()/pw.stop() from the worker raised greenlet.error, the bare
# excepts swallowed it, and the dispatcher loop stayed suspended on the
# creating thread — its running-loop marker never cleared. Every later
# sync_playwright().start() on that executor thread raised "using Playwright
# Sync API inside the asyncio loop" in ~1ms. Prod 09-02/03: one failed cloak
# session + one successful one poisoned both PROBE_EXECUTOR threads; every
# browser rung of /probe, /render and /probe-single returned None while every
# liveness gauge stayed green for ~11h across two deployments, and a container
# restart re-poisoned on the second browser session. Reproduced locally:
# attempt 1 OK, attempts 2-3 instant-guard-error.


class _RecordingBrowser(_FakeBrowser):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, int]] = []

    def close(self):
        self.calls.append(("close", threading.get_ident()))
        self.closed = True


class _RecordingPw(_FakePw):
    def __init__(self, browser):
        super().__init__(browser)
        self.calls: list[tuple[str, int]] = []

    def stop(self):
        self.calls.append(("stop", threading.get_ident()))


class TestPoisonFix:
    def _ctx(self, probe, monkeypatch):
        browser = _RecordingBrowser()
        pw = _RecordingPw(browser)
        ctx = probe._PageContext(
            page=None, browser=browser, pw=pw, stealth_used=False, method="playwright"
        )
        return ctx, browser, pw

    def test_close_stops_pw_on_the_calling_thread(self, probe, monkeypatch):
        """THE regression: teardown must run on the session thread. On a
        different thread the greenlet switch is impossible and the loop leaks."""
        ctx, browser, pw = self._ctx(probe, monkeypatch)
        ctx.close()
        assert ("stop", threading.get_ident()) in pw.calls
        assert ("close", threading.get_ident()) in browser.calls

    def test_close_spawns_no_close_worker(self, probe, monkeypatch):
        """The old design's worker thread name must be gone from the process —
        any playwright call from it is a poison incident by definition."""
        seen_threads: list[str] = []
        real_stop = _RecordingPw.stop

        def spying_stop(pw_self):
            seen_threads.extend(t.name for t in threading.enumerate())
            real_stop(pw_self)

        monkeypatch.setattr(_RecordingPw, "stop", spying_stop)
        ctx, _b, _p = self._ctx(probe, monkeypatch)
        ctx.close()
        # exact name: the SIGKILL watchdog is deliberately named
        # page-context-close-watchdog (it never touches playwright handles'
        # greenlet — PID reads + kills only) and is fine.
        assert not any(n == "page-context-close" for n in seen_threads), (
            "a close worker thread appeared — cross-thread teardown poisons "
            "the creating executor thread"
        )

    def test_watchdog_kills_driver_tree_when_graceful_close_hangs(self, probe, monkeypatch):
        """The bounded-close property survives: a hang past PAGE_CLOSE_GRACE_S
        gets the driver tree SIGKILLed (which unblocks the inline close)."""
        monkeypatch.setattr(probe, "PAGE_CLOSE_GRACE_S", 0.2)
        hang_open = threading.Event()
        killed = threading.Event()

        class _HangingBrowser(_RecordingBrowser):
            def close(self):
                self.calls.append(("close", threading.get_ident()))
                killed.wait(5)  # released by the (stubbed) watchdog kill

        browser = _HangingBrowser()
        pw = _RecordingPw(browser)
        ctx = probe._PageContext(
            page=None, browser=browser, pw=pw, stealth_used=False, method="playwright"
        )
        monkeypatch.setattr(
            probe, "_hard_kill_tree", lambda pid: killed.set() or 1
        )
        ctx.close()  # must return — the kill unblocks the hang
        assert killed.is_set()
        assert ("stop", threading.get_ident()) in pw.calls

    def test_launch_failure_unwinds_pw_inline_after_the_kill(self, probe, monkeypatch):
        """A launch that fails AFTER start() must also stop() the sync context
        (same thread) — W1's kill alone leaves the dispatcher loop suspended
        and poisons the thread (prod's original poisoning event)."""
        pw = _RecordingPw(_FakeBrowserPageFails())
        pw.chromium = types.SimpleNamespace(
            launch=lambda **kw: (_ for _ in ()).throw(
                RuntimeError("Resource temporarily unavailable")
            )
        )
        _install_fake_module(
            monkeypatch,
            "playwright",
        )
        _install_fake_module(
            monkeypatch,
            "playwright.sync_api",
            sync_playwright=lambda: types.SimpleNamespace(start=lambda: pw),
        )
        before = probe._LAUNCH_HEALTH["launch_failed"]
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            probe._launch_page(method="playwright", proxy_tier="none", timeout=5)
        assert (FAKE_DRIVER_PID, signal.SIGKILL) in probe._KILLS  # W1 tree kill
        assert ("stop", threading.get_ident()) in pw.calls  # inline unwind
        assert probe._LAUNCH_HEALTH["launch_failed"] == before + 1

    def test_poison_guard_error_is_counted_and_reraised(self, probe, monkeypatch):
        """A thread already carrying a leaked loop fails at start() with the
        guard error — it must be counted as poison (visible in /health) and
        still propagate."""
        _install_fake_module(monkeypatch, "playwright")

        def _poisoned():
            raise RuntimeError(
                "It looks like you are using Playwright Sync API inside the "
                "asyncio loop. Please use the Async API instead."
            )

        _install_fake_module(
            monkeypatch,
            "playwright.sync_api",
            sync_playwright=lambda: types.SimpleNamespace(start=_poisoned),
        )
        before = probe._LAUNCH_HEALTH["poison_guard"]
        with pytest.raises(RuntimeError, match="asyncio loop"):
            probe._launch_page(method="playwright", proxy_tier="none", timeout=5)
        assert probe._LAUNCH_HEALTH["poison_guard"] == before + 1

    def test_poison_degrades_health_status_and_is_gauged(self):
        """The outage's missing visibility: poison_guard must degrade /health
        (restart is the only cure) and the counters must be gauged."""
        src = pathlib.Path(
            os.path.join(ROOT, "browser_service", "server.py")
        ).read_text()
        assert 'and not launch_health.get("poison_guard")' in src, (
            "/health must degrade while a poisoned executor thread exists"
        )
        assert '"launch_health"' in src

    def test_poison_vector_dependencies_are_pinned(self):
        """playwright + cloakbrowser own the sync-context machinery at the
        center of this outage — the unpinned >= specs that let prod drift
        must not come back."""
        req = pathlib.Path(
            os.path.join(ROOT, "browser_service", "requirements.txt")
        ).read_text()
        assert "playwright==" in req
        assert "cloakbrowser==" in req
        assert "cloakbrowser>=" not in req


class TestXvfbRepairImport:
    def test_repair_xvfb_imports_both_names_it_reads(self):
        """[prod 2026-09-02/03] _repair_xvfb imported only MCP_HEADLESS while
        its body also reads the bare DISPLAY global — every 30-min cleanup
        cycle crashed NameError before repairing anything, for the whole
        poison-outage window. The import must cover every name the body
        reads (server.py can't be imported in this image — no fastapi — so
        this is a source contract)."""
        src = pathlib.Path(
            os.path.join(ROOT, "browser_service", "server.py")
        ).read_text()
        fn = re.search(
            r"^def _repair_xvfb\(.*?(?=^def |^class |^@)", src, re.M | re.S
        )
        assert fn, "_repair_xvfb not found"
        body = fn.group(0)
        for name in re.findall(r"\b(DISPLAY|MCP_HEADLESS)\b", body):
            assert f"import {name}" in body or f", {name}" in body, (
                f"_repair_xvfb reads {name} without importing it — NameError "
                "every cleanup cycle"
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
