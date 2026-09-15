"""Wave-33 T33-11 (E1): telemetry — windowed rejections, per-pool launch
capacity, boot line, webapp-side tester-skip counter.

docs/plans/wave33-browser-oom-plan.md §E1:

1. /health gains ``rejects_1h`` (memory_gate / scrape_reject / probe_slots,
   1h windows — a lifetime counter can't say whether the storm self-cleared)
   and ``launch_capacity`` (configured vs effective per pool — blacklisted
   threads are capacity the pool nominally has but cannot use).
2. ``launch_window_stats`` breaks outcomes out per pool (launch-failure rate
   per pool); _LAUNCH_EVENTS entries now carry the pool.
3. Boot line in lifespan: commit + deployment + resolved HEALTH_STRICT +
   boot time + pid — the cheapest deploy-vs-crash discriminator (the 09-15
   OOM RCA could not tell recycles from deploys without archaeology).
4. The tester-skip counter lives WEBAPP-side (graph.py's pre-flight park —
   browser-service never sees those skips, its own counter would read zero),
   in the shared cache, surfaced by /api/health/.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave33_tier_e.py -q
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import defaultdict, deque

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")
PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")


def _grab(name: str, path: str) -> str:
    src = open(path, encoding="utf-8").read()
    m = re.search(
        rf"^((?:async )?def {name}\(.*?)"
        rf"(?=^(?:async )?def |^@|^class "
        rf"|^[A-Za-z_][A-Za-z0-9_]*\s*[:=]\s|\Z)",
        src,
        re.M | re.S,
    )
    assert m, f"{name} not found in {path}"
    return m.group(1)


def _server_src() -> str:
    return open(SERVER_PATH, encoding="utf-8").read()


def _scrape_body() -> str:
    src = _server_src()
    i = src.index('@app.post("/scrape")')
    j = src.index('@app.post("/cancel")')
    return src[i:j]


# ── 1. windowed rejection counters ──────────────────────────────────────────


class TestRejectCounters:
    @staticmethod
    def _ns() -> dict:
        ns: dict = {
            "defaultdict": defaultdict,
            "deque": deque,
            "threading": __import__("threading"),
            "time": time,
            "_REJECT_EVENTS": defaultdict(deque),
            "_REJECT_LOCK": __import__("threading").Lock(),
            "_REJECT_WINDOW_S": 3600.0,
        }
        exec(compile(_grab("_record_reject", SERVER_PATH), "<r>", "exec"), ns)
        exec(compile(_grab("_reject_counts", SERVER_PATH), "<c>", "exec"), ns)
        return ns

    def test_records_and_counts_by_kind(self):
        ns = self._ns()
        ns["_record_reject"]("scrape_reject")
        ns["_record_reject"]("scrape_reject")
        ns["_record_reject"]("memory_gate")
        counts = ns["_reject_counts"]()
        assert counts == {"scrape_reject": 2, "memory_gate": 1}

    def test_window_drops_stale_entries(self):
        ns = self._ns()
        # a rejection 2h ago must not count against the 1h window
        ns["_REJECT_EVENTS"]["memory_gate"].append(time.monotonic() - 7200)
        ns["_record_reject"]("memory_gate")
        assert ns["_reject_counts"]()["memory_gate"] == 1

    def test_empty_kinds_read_zero_not_missing(self):
        ns = self._ns()
        ns["_record_reject"]("probe_slots")
        assert ns["_reject_counts"]().get("scrape_reject", 0) == 0


class TestRejectWiring:
    def test_admit_refusal_records_memory_gate(self):
        admit = _grab("_admit", SERVER_PATH)
        assert '_record_reject("memory_gate")' in admit, (
            "every _admit refusal is a gate trip — E1's gate trips/h"
        )

    def test_scrape_rejections_recorded(self):
        body = _scrape_body()
        assert body.count('_record_reject("scrape_reject")') == 2, (
            "busy-cap arm AND memory-gate arm both count as scrape rejects"
        )

    def test_probe_slot_exhaustion_recorded(self):
        src = _server_src()
        i = src.index('@app.post("/probe-single")')
        assert '_record_reject("probe_slots")' in src[i:]

    def test_health_payload_carries_both(self):
        src = _server_src()
        assert '"rejects_1h": _reject_counts(),' in src
        assert '"launch_capacity": _effective_capacity(),' in src


# ── 2. effective capacity per pool ──────────────────────────────────────────


class TestEffectiveCapacity:
    def _ns(self, poisoned, spec):
        ns: dict = {
            "launch_poison_snapshot": lambda: {"poisoned_threads": poisoned},
            "_thread_pool_name": lambda n: n.rpartition("_")[0],
            "_POOL_SPEC": spec,
        }
        exec(
            compile(_grab("_effective_capacity", SERVER_PATH), "<e>", "exec"), ns
        )
        return ns

    def test_poisoned_threads_reduce_effective_capacity(self):
        ns = self._ns(
            poisoned=["navigate_1", "navigate_2", "probe_0"],
            spec={"navigate": 3, "probe": 2},
        )
        cap = ns["_effective_capacity"]()
        assert cap["navigate"] == {"configured": 3, "poisoned": 2, "effective": 1}
        assert cap["probe"] == {"configured": 2, "poisoned": 1, "effective": 1}

    def test_healthy_pool_is_full_capacity(self):
        ns = self._ns(poisoned=[], spec={"probe": 2})
        assert ns["_effective_capacity"]()["probe"]["effective"] == 2


# ── 3. per-pool launch windows (probe.py) ───────────────────────────────────


class TestLaunchWindowByPool:
    @staticmethod
    def _ns(events):
        ns: dict = {
            "time": time,
            "_LAUNCH_HEALTH_LOCK": __import__("threading").Lock(),
            "_LAUNCH_EVENTS": deque(events),
            "_LAUNCH_WINDOW_DEFAULT_S": 900.0,
        }
        exec(
            compile(_grab("launch_window_stats", PROBE_PATH), "<l>", "exec"), ns
        )
        return ns

    def test_by_pool_breakout(self):
        now = time.time()
        ns = self._ns(
            [
                (now - 10, "ok", "navigate"),
                (now - 9, "failed", "navigate"),
                (now - 8, "failed", "navigate"),
                (now - 7, "ok", "probe"),
                (now - 9999, "failed", "probe"),  # outside window
            ]
        )
        out = ns["launch_window_stats"](900.0)
        assert out["ok"] == 2 and out["failed"] == 2
        assert out["by_pool"] == {
            "navigate": {"ok": 1, "failed": 2},
            "probe": {"ok": 1, "failed": 0},
        }

    def test_launch_events_carry_pool(self):
        for fn in ("_note_launch_failed", "_note_launch_ok"):
            src = _grab(fn, PROBE_PATH)
            assert "_thread_pool_name(threading.current_thread().name)" in src, (
                f"{fn} must record WHICH pool launched (E1 per-pool rate)"
            )


# ── 4. the boot line ────────────────────────────────────────────────────────


class TestBootLine:
    def test_lifespan_logs_identity(self):
        src = _server_src()
        i = src.index("async def lifespan")
        head = src[i: i + 2500]
        assert "[BROWSER-BOOT]" in head
        for token in (
            "RAILWAY_GIT_COMMIT_SHA",
            "RAILWAY_DEPLOYMENT_ID",
            "HEALTH_STRICT",
        ):
            assert token in head, f"boot line must stamp {token}"


# ── 5. webapp-side tester-skip counter ──────────────────────────────────────


class TestTesterSkipCounter:
    def test_record_increments_in_shared_cache(self):
        from agents.tools.browser_http import (
            TESTER_SKIP_CACHE_KEY,
            browser_tester_skips_total,
            record_browser_tester_skip,
        )
        from django.core.cache import cache

        cache.delete(TESTER_SKIP_CACHE_KEY)
        assert record_browser_tester_skip("preflight_unhealthy") == 1
        assert record_browser_tester_skip("preflight_unhealthy") == 2
        assert browser_tester_skips_total() == 2
        cache.delete(TESTER_SKIP_CACHE_KEY)

    def test_graph_preflight_skip_records(self):
        src = open(
            os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8"
        ).read()
        i = src.index("browser_service unhealthy before testing")
        tail = src[i: i + 900]
        assert "record_browser_tester_skip" in tail, (
            "the skip counter must be emitted at the park, webapp-side"
        )

    def test_health_api_surfaces_the_counter(self):
        from agents.tools.browser_http import (
            TESTER_SKIP_CACHE_KEY,
            record_browser_tester_skip,
        )
        from django.core.cache import cache
        from scraper.views import _browser_tester_skips_total

        cache.delete(TESTER_SKIP_CACHE_KEY)
        record_browser_tester_skip("health_api_test")
        assert _browser_tester_skips_total() == 1
        cache.delete(TESTER_SKIP_CACHE_KEY)

    def test_views_check_embeds_counter(self):
        src = open(
            os.path.join(ROOT, "webapp", "scraper", "views.py"), encoding="utf-8"
        ).read()
        assert '"tester_skips_total": _browser_tester_skips_total(),' in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
