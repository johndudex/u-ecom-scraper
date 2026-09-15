"""Wave-33 Tier A: poison STATE (T33-1) + four-state /health (T33-2).

Plan: docs/plans/wave33-browser-oom-plan.md (v2). The prod killer was not the
poison itself but the availability latch: a LIFETIME poison counter made
/health 503 forever (nothing in the stack recycles an unhealthy container),
and celery's literal-"ok" check read that 503 as "skip the tester" — 166
skips / 12 dead jobs in two storms. Fixes locked here:

- T33-1: poison is STATE — a guard event blacklists the thread at the current
  pool generation; a successful launch on the thread or an A3 executor swap
  (generation bump) clears it. Lifetime counters become telemetry only.
- T33-2: /health has four states — ok(200) / degraded(200, bounded transient,
  browsable flag drives celery) / degraded_persistent(503, condition held >
  2×nav-window+5min) / dead(503 immediately). browsable = launch capability.
  Celery pre-flights accept 200+browsable; the beat park-RESUMER keeps the
  strict literal-"ok" gate (conservative by design).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave33_tier_a.py -q
"""

from __future__ import annotations

import os
import pathlib
import re
import threading
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")
SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")
BHTTP_PATH = os.path.join(ROOT, "webapp", "agents", "tools", "browser_http.py")
TASKS_PATH = os.path.join(ROOT, "webapp", "scraper", "tasks.py")
VIEWS_PATH = os.path.join(ROOT, "webapp", "scraper", "views.py")


def _read(path: str) -> str:
    return pathlib.Path(path).read_text()


def _grab(src: str, name: str) -> str:
    m = re.search(
        rf"^(def {name}\(.*?)(?=^def |^@|^class |^async def |\Z)", src, re.M | re.S
    )
    assert m, f"{name} not found"
    return m.group(1)


def _load_probe():
    """Load probe.py fresh (same trick as test_browser_resilience: the real
    package __init__ imports server.py → fastapi, absent on this image)."""
    import importlib.util
    import sys

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
    monkeypatch.setitem(__import__("sys").modules, name, mod)
    return mod


@pytest.fixture()
def probe():
    return _load_probe()


# ── T33-1: poison is STATE, not a lifetime count ────────────────────────────


class TestT33_1PoisonState:
    def test_seam_exists_and_blacklists_thread(self, probe):
        """_record_poison_event is a module-level seam (tests inject): it must
        blacklist the thread for the CURRENT generation and bump telemetry."""
        probe._record_poison_event(
            "navigate_0", RuntimeError("inside the asyncio loop")
        )
        snap = probe.launch_poison_snapshot()
        assert snap["poisoned_threads"] == ["navigate_0"]
        assert snap["poison_guard_lifetime"] == 1
        assert snap["poison_events_1h"] == 1
        assert snap["generations"]["navigate"] == 0

    def test_seam_defaults_to_current_thread(self, probe):
        t = threading.current_thread()
        old = t.name
        t.name = "probe_1"
        try:
            probe._record_poison_event()
            assert probe.launch_poison_snapshot()["poisoned_threads"] == ["probe_1"]
        finally:
            t.name = old

    def test_successful_launch_clears_the_blacklist(self, probe):
        """A 'poisoned' thread that launches successfully was never truly
        poisoned (the 09-14 storm class) — the state must clear on success."""
        probe._record_poison_event("probe_0", RuntimeError("inside the asyncio loop"))
        t = threading.current_thread()
        old = t.name
        t.name = "probe_0"
        try:
            probe._note_launch_ok()
        finally:
            t.name = old
        assert probe.launch_poison_snapshot()["poisoned_threads"] == []
        assert probe.launch_poison_snapshot()["poison_guard_lifetime"] == 1, (
            "lifetime counter stays (telemetry), state is what matters"
        )

    def test_launch_page_failure_goes_through_the_seam(self, probe, monkeypatch):
        """The _launch_page failure path must record STATE via the seam — a
        guard-signature error on a named executor thread blacklists it."""
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
        t = threading.current_thread()
        old = t.name
        t.name = "navigate_0"
        try:
            with pytest.raises(RuntimeError, match="asyncio loop"):
                probe._launch_page(method="playwright", proxy_tier="none", timeout=5)
        finally:
            t.name = old
        assert probe.launch_poison_snapshot()["poisoned_threads"] == ["navigate_0"]

    def test_generation_bump_unblacklists_recycled_names(self, probe):
        """A3's executor swap bumps the pool generation: replacement threads
        own recycled names and must not be born-guilty."""
        probe._record_poison_event(
            "navigate_0", RuntimeError("inside the asyncio loop")
        )
        assert probe.launch_poison_snapshot()["poisoned_threads"] == ["navigate_0"]
        gen = probe.bump_launch_generation("navigate")
        assert gen == 1
        snap = probe.launch_poison_snapshot()
        assert snap["poisoned_threads"] == []
        assert snap["poison_guard_lifetime"] == 1, "telemetry survives the swap"
        # a NEW event on the recycled name blacklists again (new generation)
        probe._record_poison_event(
            "navigate_0", RuntimeError("inside the asyncio loop")
        )
        assert probe.launch_poison_snapshot()["poisoned_threads"] == ["navigate_0"]

    def test_generations_are_per_pool(self, probe):
        """Swapping the probe pool must not un-blacklist a poisoned navigate
        thread (one bump per pool, not a global counter)."""
        probe._record_poison_event(
            "navigate_0", RuntimeError("x inside the asyncio loop")
        )
        probe.bump_launch_generation("probe")
        assert probe.launch_poison_snapshot()["poisoned_threads"] == ["navigate_0"]

    def test_counters_are_lock_serialized(self, probe):
        """Concurrent guard events + successes must not lose updates."""
        errors = []

        def _worker(i):
            try:
                for _ in range(50):
                    probe._record_poison_event(f"navigate_{i % 2}", RuntimeError("x"))
                    probe._note_launch_ok()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        snap = probe.launch_poison_snapshot()
        assert snap["poison_guard_lifetime"] == 8 * 50
        assert probe._launch_health_snapshot()["ok"] == 8 * 50

    def test_launch_window_stats_feed_browsable(self, probe):
        """Windowed launch counters (not lifetime) decide launch capability —
        a storm that self-cleared an hour ago must not keep the service
        'unbrowsable' forever."""
        t = threading.current_thread()
        old = t.name
        t.name = "probe_0"
        try:
            probe._note_launch_ok()
            probe._note_launch_failed(RuntimeError("Resource temporarily unavailable"))
        finally:
            t.name = old
        stats = probe.launch_window_stats(window_s=900.0)
        assert stats["ok"] == 1 and stats["failed"] == 1
        assert probe._launch_health_snapshot()["launch_failed"] == 1
        # the failed launch was NOT a poison signature — no state blacklisting
        assert probe.launch_poison_snapshot()["poisoned_threads"] == []


# ── T33-2: four-state /health ───────────────────────────────────────────────


def _health_fn():
    src = _read(SERVER_PATH)
    code = _grab(src, "_compute_health_status")
    ns = {"Optional": __import__("typing").Optional}
    exec(compile(code, "<grabbed>", "exec"), ns)
    return ns["_compute_health_status"]


def _good_inputs(**over):
    d = dict(
        ready=True,
        scraper_cdp_alive=True,
        scraper_not_required=False,
        mcp_cdp_alive=True,
        mcp_process_alive=True,
        mcp_http_state="up",
        nav_state="ok",
        poison={"poisoned_threads": []},
        launch_recent={"ok": 3, "failed": 0, "window_s": 900.0},
        degraded_since=None,
        now=1000.0,
        persist_after_s=1500.0,
    )
    d.update(over)
    return d


class TestT33_2FourStateHealth:
    def test_all_green_is_ok_and_browsable(self):
        v = _health_fn()(**_good_inputs())
        assert v == {"status": "ok", "browsable": True, "degraded_since": None}

    @pytest.mark.parametrize(
        "over",
        [
            {"ready": False},
            {"mcp_cdp_alive": False},
            {"mcp_process_alive": False},
            {"mcp_http_state": "down"},
            {"scraper_cdp_alive": False, "scraper_not_required": False},
        ],
    )
    def test_dead_conditions_are_immediate_503_class(self, over):
        v = _health_fn()(**_good_inputs(**over))
        assert v["status"] == "dead" and v["browsable"] is False

    def test_lazy_idle_scraper_is_not_dead(self):
        """W6 lazy escape: a deliberately-unstarted Scraper Chrome must not
        read as dead (compose healthcheck would block dependents from boot)."""
        v = _health_fn()(
            **_good_inputs(scraper_cdp_alive=False, scraper_not_required=True)
        )
        assert v["status"] == "ok"

    def test_poison_state_degrades_and_kills_browsable(self):
        poison = {"poisoned_threads": ["navigate_0"]}
        v = _health_fn()(**_good_inputs(poison=poison))
        assert v["status"] == "degraded" and v["browsable"] is False
        assert v["degraded_since"] == 1000.0, "first observation arms the clock"

    def test_poison_held_past_threshold_becomes_degraded_persistent(self):
        poison = {"poisoned_threads": ["navigate_0"]}
        v = _health_fn()(
            **_good_inputs(poison=poison, degraded_since=1000.0, now=1000.0 + 1501.0)
        )
        assert v["status"] == "degraded_persistent"
        assert v["browsable"] is False

    def test_poison_inside_threshold_stays_degraded_200(self):
        poison = {"poisoned_threads": ["navigate_0"]}
        v = _health_fn()(
            **_good_inputs(poison=poison, degraded_since=1000.0, now=1000.0 + 1499.0)
        )
        assert v["status"] == "degraded"

    def test_nav_window_degrades_but_stays_browsable(self):
        """The nav-outcome window is a bounded transient about RECENT OUTCOMES,
        not launch capability — celery should keep using the service."""
        v = _health_fn()(**_good_inputs(nav_state="degraded"))
        assert v["status"] == "degraded" and v["browsable"] is True

    def test_nav_window_held_becomes_degraded_persistent(self):
        v = _health_fn()(
            **_good_inputs(
                nav_state="degraded", degraded_since=1000.0, now=1000.0 + 1501.0
            )
        )
        assert v["status"] == "degraded_persistent"

    def test_all_recent_launches_failed_is_not_browsable(self):
        v = _health_fn()(
            **_good_inputs(launch_recent={"ok": 0, "failed": 3, "window_s": 900.0})
        )
        assert v["status"] == "degraded" and v["browsable"] is False

    def test_quiet_launch_window_is_not_a_failure(self):
        v = _health_fn()(
            **_good_inputs(launch_recent={"ok": 0, "failed": 0, "window_s": 900.0})
        )
        assert v["status"] == "ok" and v["browsable"] is True

    def test_recovery_resets_the_persistent_clock(self):
        v = _health_fn()(**_good_inputs(degraded_since=900.0))
        assert v == {"status": "ok", "browsable": True, "degraded_since": None}

    def test_handler_wiring(self):
        """The handler must: use the pure fn, persist the arm clock, map
        ok/degraded→200 and dead/degraded_persistent→503, publish browsable,
        and read poison STATE (not the lifetime counter) for status."""
        src = _read(SERVER_PATH)
        assert "verdict = _compute_health_status(" in src
        assert "_HEALTH_DEGRADED_SINCE" in src, (
            "the arm clock must persist across calls"
        )
        assert 'status_code=200 if status in ("ok", "degraded") else 503' in src
        assert '"browsable": verdict["browsable"]' in src
        hsrc = re.search(
            r'@app\.get\("/health"\).*?(?=\n@app\.|\nclass )', src, re.S
        ).group(0)
        assert "launch_poison_snapshot()" in hsrc
        assert 'launch_health.get("poison_guard")' not in hsrc, (
            "lifetime counter must not gate status — it degrades forever and "
            "nothing recycles the container (the prod 8h latch)"
        )


# ── T33-2: celery consumers ─────────────────────────────────────────────────


def _bhttp_ns(monkeypatch, status_code, body, strict=""):
    """Exec browser_service_healthy/browser_service_strict_ok with a fake
    httpx and an explicit HEALTH_STRICT value."""
    fake_resp = types.SimpleNamespace(status_code=status_code, json=lambda: body)

    class _FakeHttpx:
        @staticmethod
        def get(url, timeout=None):
            return fake_resp

    ns = {
        "httpx": _FakeHttpx,
        "HEALTH_TIMEOUT_S": 6.0,
        "BROWSER_SERVICE_URL": "http://browser_service:8001",
        "HEALTH_STRICT": strict.strip().lower() in ("1", "true", "yes", "on"),
        "time": time,
        "logger": __import__("logging").getLogger("t33"),
    }
    src = _read(BHTTP_PATH)
    exec(compile(_grab(src, "browser_service_healthy"), "<g>", "exec"), ns)
    exec(compile(_grab(src, "browser_service_strict_ok"), "<g>", "exec"), ns)
    return ns


class TestT33_2CeleryConsumers:
    def test_degraded_browsable_is_accepted_by_default(self, monkeypatch):
        ns = _bhttp_ns(monkeypatch, 200, {"status": "degraded", "browsable": True})
        assert ns["browser_service_healthy"]() is True

    def test_degraded_unbrowsable_is_rejected(self, monkeypatch):
        """Never green-light a service that cannot launch browsers — that
        converts park-into-failure (testers die in ~1ms launch errors)."""
        ns = _bhttp_ns(monkeypatch, 200, {"status": "degraded", "browsable": False})
        assert ns["browser_service_healthy"]() is False

    def test_degraded_without_browsable_key_is_rejected(self, monkeypatch):
        """Pre-deploy old body (no browsable key) must read strict — absence
        means 'unknown', and unknown is no."""
        ns = _bhttp_ns(monkeypatch, 200, {"status": "degraded"})
        assert ns["browser_service_healthy"]() is False

    def test_ok_is_accepted_both_ways(self, monkeypatch):
        ns = _bhttp_ns(monkeypatch, 200, {"status": "ok", "browsable": True})
        assert ns["browser_service_healthy"]() is True
        assert ns["browser_service_strict_ok"]() is True

    def test_503_rejected_both_ways(self, monkeypatch):
        ns = _bhttp_ns(
            monkeypatch, 503, {"status": "degraded_persistent", "browsable": False}
        )
        assert ns["browser_service_healthy"]() is False
        assert ns["browser_service_strict_ok"]() is False

    def test_health_strict_restores_literal_ok(self, monkeypatch):
        ns = _bhttp_ns(
            monkeypatch, 200, {"status": "degraded", "browsable": True}, strict="1"
        )
        assert ns["browser_service_healthy"]() is False

    def test_resumer_keeps_the_strict_gate(self):
        """The beat park-RESUMER must require literal ok: resuming a parked job
        into a merely-browsable gateway re-runs full scrapes against a service
        that may still be sick. Conservative by design (plan §A2)."""
        src = _read(TASKS_PATH)
        resumer = re.search(
            r"def resume_browser_unavailable_jobs.*?(?=\n@|\ndef )", src, re.S
        )
        assert resumer, "resumer task not found"
        assert "browser_service_strict_ok" in resumer.group(0)
        assert "browser_service_healthy()" not in resumer.group(0)

    def test_dashboard_derives_from_body_status(self):
        """The health dashboard must classify from the BODY status: degraded
        states read degraded, dead reads down — not one 503 bucket."""
        src = _read(VIEWS_PATH)
        check = re.search(r"def _check_browser_service\(\).*?(?=\ndef )", src, re.S)
        assert check, "_check_browser_service not found"
        body = check.group(0)
        assert 'body_status = data.get("status"' in body
        assert '"degraded_persistent"' in body, "sustained degraded must read degraded"
        assert body.count('"degraded"') >= 2, "ok/degraded mapping present"
        assert 'display = "down"' in body, "dead must read down, not degraded"
