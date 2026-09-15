"""Wave-33 Tier B: caller-visible ephemeral registry + kill on caller
timeout (T33-4), per-PID orphan sweep (T33-5).

Plan: docs/plans/wave33-browser-oom-plan.md (v2 §Tier B). The sole genuine-
OOM producer: caller-timeout arms (navigate 408, probe 504) returned with no
worker signal — the executor thread kept its browser RUNNING — and the orphan
reaper skipped its WHOLE cycle while any navigate was live. Up to 10 unreaped
navigate browsers stacked on ~4 legitimate ones (prod restarts 09-14 17:01Z).
Fix: every browser-launching call registers (pid, starttime) in a reaper-
visible registry; the timeout arm hard-kills the call's roots (PID-reuse
guarded) and flags the call abandoned so the probe ladder stops launching new
browsers for a caller that is gone.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave33_tier_b.py -q
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import threading
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")
SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")


def _read(path: str) -> str:
    return pathlib.Path(path).read_text()


def _grab(src: str, name: str) -> str:
    m = re.search(
        rf"^(def {name}\(.*?)(?=^def |^@|^class |^async def |\Z)", src, re.M | re.S
    )
    assert m, f"{name} not found"
    return m.group(1)


def _load_probe():
    import importlib.util

    pkg_name = "browser_service"
    saved_pkg = sys.modules.get(pkg_name)
    saved_probe = sys.modules.pop("browser_service.probe", None)
    saved_cfg = sys.modules.pop("browser_service.config", None)

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules[pkg_name] = pkg
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_service.probe", PROBE_PATH
        )
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


def _install_fake_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture()
def probe():
    return _load_probe()


# ── T33-4: _launch_page exposes the root pid; hooks flow down the ladder ────


class _FakePage:
    def set_default_timeout(self, ms):
        pass


class TestT33_4LaunchPidAndHooks:
    def test_launch_page_sets_root_pid(self, probe, monkeypatch):
        """The registry can only protect what it can see: _launch_page must
        record the driver root PID on the context (both launch paths use the
        same attribution _hard_kill_partial_launch already trusts)."""
        fake_browser = types.SimpleNamespace(
            new_page=lambda: _FakePage(),
            _impl_obj=types.SimpleNamespace(
                _connection=types.SimpleNamespace(
                    _transport=types.SimpleNamespace(
                        _proc=types.SimpleNamespace(pid=424242)
                    )
                )
            ),
        )
        pw = types.SimpleNamespace(
            stop=lambda: None,
            chromium=types.SimpleNamespace(launch=lambda **kw: fake_browser),
            _transport=types.SimpleNamespace(_proc=types.SimpleNamespace(pid=424242)),
        )
        _install_fake_module(monkeypatch, "playwright")
        _install_fake_module(
            monkeypatch,
            "playwright.sync_api",
            sync_playwright=lambda: types.SimpleNamespace(start=lambda: pw),
        )
        ctx = probe._launch_page(method="playwright", proxy_tier="none", timeout=5)
        assert ctx.root_pid == 424242

    def test_try_playwright_invokes_launch_hook(self, probe, monkeypatch):
        seen = []
        fake_browser = types.SimpleNamespace(
            new_page=lambda: _FakePage(),
            _impl_obj=types.SimpleNamespace(
                _connection=types.SimpleNamespace(
                    _transport=types.SimpleNamespace(
                        _proc=types.SimpleNamespace(pid=424242)
                    )
                )
            ),
        )

        class _Resp:
            status = 200

        fake_browser.new_page = lambda: types.SimpleNamespace(
            set_default_timeout=lambda ms: None,
            goto=lambda *a, **kw: _Resp(),
            wait_for_timeout=lambda ms: None,
            context=types.SimpleNamespace(add_cookies=lambda c: None),
            evaluate=lambda *a, **kw: "",
        )
        pw = types.SimpleNamespace(
            stop=lambda: None,
            chromium=types.SimpleNamespace(launch=lambda **kw: fake_browser),
            _transport=types.SimpleNamespace(_proc=types.SimpleNamespace(pid=424242)),
        )
        _install_fake_module(monkeypatch, "playwright")
        _install_fake_module(
            monkeypatch,
            "playwright.sync_api",
            sync_playwright=lambda: types.SimpleNamespace(start=lambda: pw),
        )
        monkeypatch.setattr(probe, "_settle_past_challenge", lambda page: "<html></html>")
        monkeypatch.setattr(probe, "_safe_title", lambda page: "t")
        monkeypatch.setattr(probe, "_detect_akamai", lambda html, code: False)
        result = probe._try_playwright(
            "https://x.test/", "none", timeout=5, launch_hook=lambda ctx: seen.append(ctx)
        )
        assert seen and seen[0].root_pid == 424242, (
            "the /probe timeout arm can only kill roots a hook told it about"
        )
        assert result is not None

    def test_run_probe_stops_between_rungs_when_abandoned(self, probe, monkeypatch):
        """A caller that timed out must not get NEW browsers: the ladder
        checks the stop predicate before each rung."""
        launched = []
        monkeypatch.setattr(
            probe, "_try_playwright", lambda *a, **kw: launched.append("pw") or None
        )
        monkeypatch.setattr(
            probe, "_try_cloak", lambda *a, **kw: launched.append("cloak") or None
        )
        calls = {"n": 0}

        def _stop():
            calls["n"] += 1
            return calls["n"] > 1  # first rung allowed, then abandoned

        result = probe.run_probe(
            "https://x.test/",
            render_js=True,
            timeout=5,
            should_stop=_stop,
            start_method="playwright_none",
        )
        assert launched == ["pw"], "first rung allowed; nothing after abandonment"
        assert "caller_abandoned" in (result.get("error") or ""), (
            f"ladder must stop with the abandonment signal, got {result}"
        )

    def test_dispatch_step_passes_hook_to_browser_rungs_only(self, probe, monkeypatch):
        seen = {"cloak": None, "http": None}
        monkeypatch.setattr(
            probe, "_try_cloak", lambda *a, **kw: seen.__setitem__("cloak", kw) or None
        )
        monkeypatch.setattr(
            probe,
            "_try_direct_http",
            lambda *a, **kw: seen.__setitem__("http", kw) or None,
        )
        probe._dispatch_step("cloak_none", "https://x.test/", 5, launch_hook="HOOK")
        probe._dispatch_step("direct_http", "https://x.test/", 5, launch_hook="HOOK")
        assert seen["cloak"] is not None and seen["cloak"].get("launch_hook") == "HOOK"
        assert seen["http"] is not None and "launch_hook" not in seen["http"], (
            "HTTP rungs launch nothing — no hook plumbing"
        )


# ── T33-4: the per-call registry in server.py ───────────────────────────────


def _registry_ns(starttimes=None, killed=None):
    """Exec the registry functions from server.py with fake /proc + kills."""
    starttimes = starttimes or {}
    killed = killed if killed is not None else []

    ns = {
        "__name__": "t33b",
        "threading": threading,
        "time": __import__("time"),
        "logger": __import__("logging").getLogger("t33b"),
        "_EPHEMERAL_CALLS": {},
        "_EPHEM_LOCK": threading.Lock(),
        "_proc_starttime": lambda pid: starttimes.get(pid),
        "_hard_kill_tree": lambda pid: killed.append(pid) or 3,
    }
    src = _read(SERVER_PATH)
    for name in (
        "_ephemeral_register",
        "_ephemeral_track",
        "_ephemeral_release",
        "_ephemeral_is_abandoned",
        "_ephemeral_abandon",
        "_ephemeral_snapshot",
        "_kill_tracked_roots",
    ):
        exec(compile(_grab(src, name), "<g>", "exec"), ns)
    return ns, killed


class TestT33_4Registry:
    def test_lifecycle_register_track_release(self):
        ns, killed = _registry_ns(starttimes={4242: 100})
        ns["_ephemeral_register"]("c1", "probe")
        ns["_ephemeral_track"]("c1", [4242])
        snap = ns["_ephemeral_snapshot"]()
        assert snap["c1"]["roots"] == {4242: 100}, "starttime recorded at track"
        assert snap["c1"]["abandoned"] is False
        ns["_ephemeral_release"]("c1")
        assert ns["_ephemeral_snapshot"]() == {}

    def test_abandon_kills_roots_with_reuse_guard(self):
        ns, killed = _registry_ns(starttimes={4242: 100, 4243: 200, 4244: 999})
        ns["_ephemeral_register"]("c1", "navigate")
        ns["_ephemeral_track"]("c1", [4242, 4243, 4244])
        # 4243's starttime changed → PID reused by an innocent process
        ns["_proc_starttime"] = lambda pid: {4242: 100, 4243: 555, 4244: None}.get(pid)
        killed_count = ns["_ephemeral_abandon"]("c1")
        assert killed == [4242], (
            "only the unchanged-starttime root is killed: reused PIDs and "
            "already-dead PIDs must be spared"
        )
        assert killed_count == 3  # what the stub kill reported
        assert ns["_ephemeral_is_abandoned"]("c1") is True

    def test_abandon_unknown_call_is_a_noop(self):
        ns, killed = _registry_ns()
        assert ns["_ephemeral_abandon"]("ghost") == 0
        assert killed == []

    def test_release_unknown_call_is_a_noop(self):
        ns, _ = _registry_ns()
        ns["_ephemeral_release"]("ghost")  # must not raise

    def test_snapshot_is_reaper_visible(self):
        ns, _ = _registry_ns(starttimes={4242: 100})
        ns["_ephemeral_register"]("c1", "navigate")
        ns["_ephemeral_register"]("c2", "probe")
        ns["_ephemeral_track"]("c1", [4242])
        snap = ns["_ephemeral_snapshot"]()
        assert set(snap) == {"c1", "c2"}
        assert snap["c1"]["label"] == "navigate"
        assert snap["c2"]["roots"] == {}

    def test_track_records_missing_starttime_as_none(self):
        """A PID whose starttime cannot be read is tracked but NEVER killed on
        the timeout path (unverifiable identity — the reaper owns it)."""
        ns, killed = _registry_ns(starttimes={})
        ns["_ephemeral_register"]("c1", "probe")
        ns["_ephemeral_track"]("c1", [4242])
        assert ns["_ephemeral_snapshot"]()["c1"]["roots"] == {4242: None}
        ns["_ephemeral_abandon"]("c1")
        assert killed == [], "unverifiable starttime → no kill"
