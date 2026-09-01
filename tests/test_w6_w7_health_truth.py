"""W6+W7: truthful /health + navigation observability
(docs/plans/browser-service-resilience-plan.md v2, commit 4).

Locked here: /health dispatches NOTHING (cached liveness — a per-request
probe dispatch on a saturated executor is what made it blind by
construction); the lazy-aware AND (mcp AND (scraper OR lazy_idle) — the
cross-confirmed finding; strict AND without the lazy escape hatch would 503
from boot and block compose dependents); the TIME-bounded (300s) outcome
window with no_data fall-through and throttled EXCLUDED from the fail rate
(429 is the system working); state-change-only liveness chatter; per-outcome
INFO logs with host-only URLs; and the Django consumer forwarding the new
keys so the dashboard shows WHY browser-service is degraded.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_w6_w7_health_truth.py -q
"""
from __future__ import annotations

import os
import re
import sys
import textwrap
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")
POOL_PATH = os.path.join(ROOT, "browser_service", "browser_pool.py")
VIEWS_PATH = os.path.join(ROOT, "webapp", "scraper", "views.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _server_src() -> str:
    return _read(SERVER_PATH)


def _grab(path: str, name: str, dedent: bool = False) -> str:
    """Extract one def/method by name from source."""
    src = _read(path)
    pat = (
        rf"^def {name}\(.*?(?=^def |^class |^@)"
        if not dedent
        else rf"^    def {name}\(.*?(?=^    def |^    @|^[A-Za-z@#])"
    )
    m = re.search(pat, src, re.M | re.S)
    assert m, f"{name} not found in {path}"
    return textwrap.dedent(m.group(0)) if dedent else m.group(0)


def _exec_src(code: str, ns: dict):
    exec(compile(code, "<grabbed>", "exec"), ns)
    return ns


def _health_fn_src() -> str:
    m = re.search(r'@app\.get\("/health"\).*?(?=\n@app\.|\nclass )', _server_src(), re.S)
    assert m, "/health handler not found"
    return m.group(0)


class TestHealthDispatchesNothing:
    def test_health_handler_has_no_run_in_executor(self):
        assert "run_in_executor" not in _health_fn_src(), (
            "/health must read cached state — a per-request probe dispatch on a "
            "saturated executor is the blind-by-construction failure"
        )

    def test_liveness_loop_publishes_to_cache(self):
        src = _server_src()
        assert "_CDP_LIVENESS_CACHE.update(" in src, (
            "the 15s loop owns the probe and must publish snapshots"
        )

    def test_lifespan_warms_both_caches(self):
        lifespan = re.search(r"async def lifespan\(.*?(?=\napp = )", _server_src(), re.S)
        assert lifespan, "lifespan not found"
        body = lifespan.group(0)
        assert "_CDP_LIVENESS_CACHE.update(" in body, "boot must warm liveness cache"
        assert "_CLOAK_INFO_CACHE = await" in body, "boot must warm cloak cache"

    def test_health_reads_cache_not_probe(self):
        assert "dict(_CDP_LIVENESS_CACHE)" in _health_fn_src()


class TestLazyAwareAnd:
    def test_and_semantics_in_health(self):
        src = _health_fn_src()
        assert 'bool(liveness.get("mcp_cdp_alive")) and (' in src, (
            "was OR — one alive Chrome masked a dead one (the cross-confirmed finding)"
        )
        assert "browser_pool.scraper_not_required()" in src, (
            "strict AND without the lazy escape hatch = 503 from boot = compose "
            "healthcheck blocks django/celery dependents"
        )

    def test_scraper_chrome_state_matrix(self):
        ns = {"os": os}
        _exec_src(_grab(POOL_PATH, "scraper_chrome_state", dedent=True), ns)

        class _FakeProc:
            def __init__(self, alive):
                self._alive = alive

            def poll(self):
                return None if self._alive else 1

        lazy_idle = types.SimpleNamespace(
            _scraper_chrome_proc=None, _scraper_chrome_started=False
        )
        down = types.SimpleNamespace(
            _scraper_chrome_proc=None, _scraper_chrome_started=True
        )
        up = types.SimpleNamespace(
            _scraper_chrome_proc=_FakeProc(True), _scraper_chrome_started=True
        )
        dead_proc = types.SimpleNamespace(
            _scraper_chrome_proc=_FakeProc(False), _scraper_chrome_started=True
        )
        assert ns["scraper_chrome_state"](lazy_idle) == "lazy_idle"
        assert ns["scraper_chrome_state"](down) == "down"
        assert ns["scraper_chrome_state"](up) == "up"
        assert ns["scraper_chrome_state"](dead_proc) == "down"

    def test_scraper_not_required_only_when_lazy_and_unstarted(self):
        ns = {"os": os, "SCRAPER_CHROME_LAZY": True}
        _exec_src(_grab(POOL_PATH, "scraper_chrome_state", dedent=True), ns)
        state_fn = ns["scraper_chrome_state"]

        def _stub(proc, started):
            s = types.SimpleNamespace(
                _scraper_chrome_proc=proc, _scraper_chrome_started=started
            )
            s.scraper_chrome_state = lambda: state_fn(s)
            return s

        ns2 = dict(ns)
        _exec_src(_grab(POOL_PATH, "scraper_not_required", dedent=True), ns2)
        fn = ns2["scraper_not_required"]
        lazy_idle = _stub(None, False)
        started = _stub(None, True)
        assert fn(lazy_idle) is True
        ns2["SCRAPER_CHROME_LAZY"] = False
        assert fn(lazy_idle) is False, "LAZY off → today's always-started behavior"
        ns2["SCRAPER_CHROME_LAZY"] = True
        assert fn(started) is False, "launched once → its CDP is required again"

    def test_health_reports_scraper_chrome_state(self):
        assert '"scraper_chrome_state": browser_pool.scraper_chrome_state()' in _health_fn_src()

    def test_launch_flag_set_on_any_attempt(self):
        src = _read(POOL_PATH)
        m = re.search(r"def _start_scraper_chrome\(.*?(?=\n    def )", src, re.S)
        assert m and "_scraper_chrome_started = True" in m.group(0), (
            "a FAILED boot launch must read 'down', never 'lazy_idle'"
        )


class TestOutcomeWindow:
    def _summary_fn(self):
        ns = {
            "time": __import__("time"),
            "deque": __import__("collections").deque,
            "_NAV_OUTCOMES": __import__("collections").deque(),
            "_NAV_RESOURCE_FAILURES": __import__("collections").deque(),
            "NAV_WINDOW_S": 300.0,
            "NAV_WINDOW_MIN_SAMPLES": 3,
        }
        _exec_src(_grab(SERVER_PATH, "_nav_outcome_summary"), ns)
        return ns["_nav_outcome_summary"], ns

    def test_empty_window_is_no_data_not_ok(self):
        fn, _ns = self._summary_fn()
        out = fn()
        assert out["state"] == "no_data"
        assert out["fail_rate"] is None

    def test_time_bounded_decay_not_count_bounded(self):
        fn, ns = self._summary_fn()
        now = __import__("time").monotonic()
        # 300 ok outcomes from 10 minutes ago: count-bounded would say ok
        # forever; time-bounded must have decayed them all away.
        ns["_NAV_OUTCOMES"].extend((now - 350 - i, "ok") for i in range(300))
        assert fn()["state"] == "no_data"

    def test_throttled_excluded_from_fail_rate(self):
        fn, ns = self._summary_fn()
        now = __import__("time").monotonic()
        ns["_NAV_OUTCOMES"].extend(
            [(now, "throttled"), (now, "throttled"), (now, "ok"), (now, "ok")]
        )
        out = fn()
        assert out["state"] == "ok", "429 is the system WORKING — must not degrade"
        assert out["total"] == 4 and out["fail_rate"] == 0.0

    def test_half_failures_degrade(self):
        fn, ns = self._summary_fn()
        now = __import__("time").monotonic()
        ns["_NAV_OUTCOMES"].extend(
            [(now, "ok"), (now, "fail"), (now, "fail"), (now, "ok")]
        )
        out = fn()
        assert out["state"] == "degraded"
        assert out["fail_rate"] == 0.5

    def test_resource_counts_as_fail_in_rate(self):
        fn, ns = self._summary_fn()
        now = __import__("time").monotonic()
        ns["_NAV_OUTCOMES"].extend(
            [(now, "ok"), (now, "resource"), (now, "ok"), (now, "resource")]
        )
        assert fn()["fail_rate"] == 0.5

    def test_more_than_three_launch_failures_degrade_even_at_low_rate(self):
        fn, ns = self._summary_fn()
        now = __import__("time").monotonic()
        # 4 resource failures + 12 ok → rate 0.25 (below 0.5) but the launch-
        # failure count trigger (>3) fires — the container is out of air.
        ns["_NAV_OUTCOMES"].extend([(now, "ok")] * 12)
        ns["_NAV_OUTCOMES"].extend((now, "resource") for _ in range(4))
        # the parallel launch-failure deque stores bare timestamps
        ns["_NAV_RESOURCE_FAILURES"].extend([now] * 4)
        assert fn()["state"] == "degraded"

    def test_classify_nav_failure_resource_markers(self):
        ns = {}
        _exec_src(_grab(SERVER_PATH, "_classify_nav_failure"), ns)
        fn = ns["_classify_nav_failure"]
        assert fn("OSError: [Errno 11] Resource temporarily unavailable") == "resource"
        assert fn("Cannot allocate memory while forking chrome") == "resource"
        assert fn("net::ERR_NAME_NOT_RESOLVED") == "fail"
        assert fn("") == "fail"


class TestHealthGauges:
    def test_memory_gauge_shape_never_raises(self):
        ns = {}
        _exec_src(_grab(SERVER_PATH, "_read_memory_gauge"), ns)
        out = ns["_read_memory_gauge"]()
        assert set(out) == {"current", "limit", "ratio", "source"}
        assert out["source"] in ("cgroup_v2", "cgroup_v1", "meminfo", None)
        if out["ratio"] is not None:
            assert out["ratio"] >= 0.0

    def test_gauges_degrade_to_null_under_deadline(self):
        ns = {"time": __import__("time"), "os": os}
        for name in ("_health_gauges", "_read_memory_gauge", "_count_chrome_processes"):
            _exec_src(_grab(SERVER_PATH, name), ns)
        ns.update(
            NAVIGATE_ACTIVE_PIDS={},
            SCRAPE_IN_FLIGHT={},
            MISC_EXECUTOR=types.SimpleNamespace(
                _work_queue=types.SimpleNamespace(qsize=lambda: 0)
            ),
            RESTART_EXECUTOR=types.SimpleNamespace(
                _work_queue=types.SimpleNamespace(qsize=lambda: 0)
            ),
        )
        # A deadline in the past: everything expensive must stay null, but the
        # handler still returns a full shape.
        out = ns["_health_gauges"](0.0)
        assert out["memory"] is None and out["fd_count"] is None
        assert out["chrome_processes"] is None
        assert set(out) == {
            "memory", "fd_count", "chrome_processes",
            "navigate_active_pids", "scrape_busy", "misc_queue", "restart_queue",
            # [wave-14] /tmp/scrape_* leftovers gauge (stale run-dir sweep)
            "stale_run_dirs",
        }


class TestNavigateObservability:
    def test_every_outcome_path_records_and_logs(self):
        nav = re.search(r'@app\.post\("/navigate"\).*?(?=\n@app\.|\nclass |\Z)', _server_src(), re.S)
        assert nav
        body = nav.group(0)
        # ok, 2× throttled, 2× crash, fail(timeout), resource|fail catch-all
        assert body.count("_record_nav_outcome(") >= 7
        assert body.count("_log_nav_outcome(") >= 7

    def test_outcome_log_uses_host_only(self):
        ns = {}
        _exec_src(_grab(SERVER_PATH, "_url_host"), ns)
        assert ns["_url_host"]("https://example.com/search?q=SECRET&q2=x") == "example.com"
        log_fn = _grab(SERVER_PATH, "_log_nav_outcome")
        assert "_url_host(url)" in log_fn, "logs must never carry the full querystring"
        assert "_url_host(url) or" in log_fn

    def test_liveness_chatter_fires_on_state_change_only(self):
        loop = re.search(
            r"async def _periodic_cdp_liveness\(.*?(?=\nasync def |\ndef )",
            _server_src(), re.S,
        )
        assert loop
        body = loop.group(0)
        assert '"CDP liveness: %s went DOWN' in body, "transition warning"
        assert body.count('went DOWN') == 1, (
            "the DOWN warning must exist ONCE (transition only) — not per interval"
        )
        assert "RECOVERED" in body

    def test_liveness_never_chatters_about_lazy_scraper(self):
        loop = re.search(
            r"async def _periodic_cdp_liveness\(.*?(?=\nasync def |\ndef )",
            _server_src(), re.S,
        )
        assert "scraper_not_required()" in loop.group(0), (
            "a deliberately-unstarted lazy Chrome is not an incident"
        )

    def test_resource_502_carries_error_class(self):
        nav = re.search(r'@app\.post\("/navigate"\).*?(?=\n@app\.|\nclass |\Z)', _server_src(), re.S)
        assert '"error_class": "resource"' in nav.group(0)


class TestDjangoConsumerForwarding:
    def test_health_dashboard_forwards_new_keys(self):
        src = _read(VIEWS_PATH)
        m = re.search(r"def _check_browser_service\(.*?(?=\ndef |\nclass )", src, re.S)
        assert m
        body = m.group(0)
        assert '"scraper_chrome_state": data.get("scraper_chrome_state")' in body
        assert '"navigate_recent": data.get("navigate_recent")' in body
        assert '"memory": data.get("gauges", {}).get("memory")' in body


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
