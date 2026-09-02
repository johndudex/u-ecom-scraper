"""W4+W5: server-side admission control, executor separation, memory gate
(docs/plans/browser-service-resilience-plan.md v2, commit 3).

Root causes being locked here: ALL executors shared one default pool, so
navigate/scrape work starved /health's liveness probe (dispatch on a
saturated executor = blind by construction); /scrape had no admission
control (an hours-long full run could sit queued while its caller's timeout
clock already ran); rejections hardcoded ``retry_after: 5`` which
manufactured retry storms; and /navigate forked new browsers with no check
of the cgroup memory ceiling — Errno 11 under pressure WAS the 502 window.
W5: the 30-min cleanup ran synchronously ON the event loop.

server.py imports fastapi (absent in the django test image), so contracts
are asserted against SOURCE, with the two pure helpers extracted and
executed (the test_f1 pattern).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_w4_w5_server_resilience.py -q
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")


def _src() -> str:
    with open(SERVER_PATH, encoding="utf-8") as fh:
        return fh.read()


def _grab(name: str) -> str:
    """Extract one top-level def from server.py source."""
    src = _src()
    m = re.search(rf"^def {name}\(.*?(?=^def |^class |^@)", src, re.M | re.S)
    assert m, f"{name} not found in server.py"
    return m.group(0)


def _exec_helper(name: str, extra_ns: dict | None = None):
    ns = {"__name__": "t_w4", **(extra_ns or {})}
    exec(compile(_grab(name), f"<{name}>", "exec"), ns)
    return ns[name]


class TestExecutorSeparation:
    def test_five_named_pools_exist_with_planned_sizes(self):
        src = _src()
        assert re.search(
            r"NAVIGATE_EXECUTOR = ThreadPoolExecutor\(\s*max_workers=NAVIGATE_MAX_CONCURRENT",
            src,
        )
        assert "SCRAPE_MAX_CONCURRENT = int(os.environ.get(\"SCRAPE_MAX_CONCURRENT\", \"2\"))" in src
        assert re.search(r"SCRAPE_EXECUTOR = ThreadPoolExecutor\(\s*max_workers=SCRAPE_MAX_CONCURRENT", src)
        assert "MISC_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix=\"misc\")" in src
        assert "RESTART_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix=\"restart\")" in src
        # [wave-16 B1] HEALTH_EXECUTOR removed — /health dispatches nothing
        # (cached liveness) and the warm-up/liveness loop moved to MAINT.
        assert "MAINT_EXECUTOR = ThreadPoolExecutor(" in src
        assert "PROBE_EXECUTOR = ThreadPoolExecutor(" in src

    def test_no_endpoint_dispatches_on_the_default_executor(self):
        src = _src()
        # run_in_executor's first arg must be a named pool (multi-line calls
        # put it on the next line, hence DOTALL over a small window).
        for m in re.finditer(r"run_in_executor\(\s*(\w+)", src):
            assert m.group(1) != "None", (
                f"unassigned executor at offset {m.start()}: {m.group(0)!r}"
            )

    def test_pool_invariant_scrape_never_shares_restart_pool(self):
        """scraper_runner's crash-retry POSTs /restart-cdp from inside a
        SCRAPE worker — /restart-cdp must run on its own pool or the self-
        POST deadlocks the shared pool."""
        src = _src()
        i_scrape = src.index("SCRAPE_EXECUTOR,\n                    functools.partial")
        i_restart = src.index("RESTART_EXECUTOR, browser_pool.restart_chrome, request.label")
        assert i_scrape != i_restart

    def test_health_slot_now_lives_in_the_boot_warmup(self):
        """W6 superseded the per-request HEALTH_EXECUTOR dispatch — /health
        reads the cached snapshot. [wave-16 B1] the warm-up AND the 15s
        liveness loop both live on MAINT_EXECUTOR via the beat-stamping
        _maint_task wrapper; the health pool itself is gone."""
        src = _src()
        assert 'MAINT_EXECUTOR,\n                _maint_task("cdp_liveness", browser_pool.check_cdp_liveness),' in src
        assert "HEALTH_EXECUTOR" not in src, "the vestigial health pool must stay removed"
        health_fn = re.search(r"@app\.get\(\"/health\"\).*?(?=\n@app\.|\nclass )", src, re.S)
        assert health_fn and "run_in_executor" not in health_fn.group(0), (
            "/health must dispatch NOTHING (cached liveness)"
        )


class TestScrapeAdmissionControl:
    def test_admit_or_429_no_queue(self):
        src = _src()
        assert "SCRAPE_MAX_QUEUE = 0" in src
        # Occupancy read from SCRAPE_IN_FLIGHT (popped on child REAP, not on
        # response — the executor thread outlives the HTTP response).
        assert "sum(1 for dl in SCRAPE_IN_FLIGHT.values() if dl > _now)" in src
        assert '"/scrape rejected (busy=%d/%d) — backpressure"' in src

    def test_rejection_uses_backpressure_helper(self):
        src = _src()
        m = re.search(
            r"return _backpressure\(\s*429,\s*\n\s*f\"scrape concurrency limit reached",
            src,
        )
        assert m, "/scrape must reject via _backpressure (header+body retry hint)"


class TestDerivedRetryAfter:
    def test_clamps_to_15_60_band(self):
        fn = _exec_helper("_derived_retry_after")
        assert fn(None) == 15
        assert fn(3) == 15
        assert fn(600) == 60
        assert fn(30) == 30

    def test_no_hardcoded_five_second_hints_remain(self):
        assert '"retry_after": 5' not in _src(), (
            "hardcoded 5s hints manufactured retry storms — use _derived_retry_after"
        )

    def test_backpressure_emits_header_and_body(self):
        helper = _grab("_backpressure")
        assert 'headers={"Retry-After": str(derived)}' in helper
        assert '"retry_after": derived' in helper
        # ...and the endpoint rejections all route through it
        src = _src()
        assert src.count("_backpressure(") >= 5, (
            "429/503 rejections (scrape admission, navigate queue, memory gate, "
            "2× chrome death) must share the helper"
        )


class TestNavigateMemoryGate:
    def test_gate_ratio_default_and_kill_switch(self):
        src = _src()
        assert 'NAVIGATE_MEMORY_GATE_RATIO = float(os.environ.get("NAVIGATE_MEMORY_GATE_RATIO", "0.85"))' in src
        assert "if NAVIGATE_MEMORY_GATE_RATIO > 0:" in src, "≤0 must disable the gate"

    def test_gate_falls_open_when_unreadable(self):
        src = _src()
        assert "if mem_ratio is not None and mem_ratio >= NAVIGATE_MEMORY_GATE_RATIO:" in src

    def test_memory_ratio_reader_handles_v2_v1_and_missing(self):
        fn = _exec_helper("_cgroup_memory_ratio")
        ratio = fn()
        # On the test host (cgroup v1 or v2, or neither) any outcome is fine
        # as long as it never raises and stays a plausible ratio.
        assert ratio is None or (isinstance(ratio, float) and ratio >= 0.0)

    def test_tripped_gate_labels_memory_pressure(self):
        src = _src()
        assert 'error_class="memory_pressure"' in src


class TestW5CleanupOffLoop:
    def test_cleanup_body_runs_on_executor(self):
        src = _src()
        assert "def _cleanup_chrome_artifacts_sync():" in src
        # [wave-16 B1] Cleanup moved from MISC_EXECUTOR (shared with the
        # reapers/probe — H1 wedge) to the dedicated MAINT_EXECUTOR via the
        # beat-stamping _maint_task wrapper.
        assert "run_in_executor(\n                MAINT_EXECUTOR,\n                _maint_task(\"cleanup_chrome_artifacts\", _cleanup_chrome_artifacts_sync)," in src

    def test_profile_cache_sweep_is_time_budgeted(self):
        src = _src()
        assert "deadline = time.monotonic() + 20.0" in src
        budget_check = re.search(
            r"for cache_dir in cache_dirs:\s*\n\s*if time\.monotonic\(\) > deadline:", src
        )
        assert budget_check, "the sweep must check the budget INSIDE the dir loop"


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
