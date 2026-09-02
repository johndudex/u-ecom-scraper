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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
