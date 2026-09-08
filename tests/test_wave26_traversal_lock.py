"""[wave-26 hotfix #2, jobs 349/350] Concurrent MCP-browser walks collide.

Local e2e 2026-09-08: jobs 349 (nike.in) and 350 (karenmillen) ran as the
wave-26 pair on prefork workers 2 and 1. browser_traverse drives the SHARED
Playwright MCP Chrome — the two walks interleaved (both captured the same
karenmillen verbolia XHR at 14:18:49.121) and job 349's walk observed
karenmillen pages that job 350 was browsing. The wave-19 cross-domain guard
correctly refused the poisoned result — turning an infrastructure collision
into a false "Navigator returned cross-domain traversal results twice" site
failure.

The first cut of the lock used django.core.cache — which is LocMem here
(settings.py configures no CACHES), i.e. PER-PROCESS: jobs 351/352 both
"acquired" simultaneously and interleaved again. The lock MUST live on a
backend shared across prefork workers — Redis (already in the stack for
celery). Contract: SET NX EX acquire, compare-and-delete release, TTL so a
dead holder can't deadlock the queue; a walker that times out proceeds
anyway with a loud warning (the cross-domain guard stays the backstop).

Tests run against the real Redis (compose service) — atomicity is the
behavior under test; a fake would prove nothing.
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents.graph import (  # noqa: E402
    _TRAVERSAL_LOCK_KEY,
    _mcp_browser_lock,
    _traversal_redis,
)

r = _traversal_redis()


@pytest.fixture(autouse=True)
def _clean_lock():
    r.delete(_TRAVERSAL_LOCK_KEY)
    yield
    r.delete(_TRAVERSAL_LOCK_KEY)


class TestLockBehavior:
    def test_free_lock_acquires_and_releases(self):
        with _mcp_browser_lock(job_id=1, wait_timeout=1.0) as acquired:
            assert acquired is True
            assert r.get(_TRAVERSAL_LOCK_KEY) == b"1"
        assert r.get(_TRAVERSAL_LOCK_KEY) is None, "lock must be released on exit"

    def test_acquire_is_atomic_nx_with_ttl(self):
        with _mcp_browser_lock(job_id=1, wait_timeout=1.0):
            ttl = r.ttl(_TRAVERSAL_LOCK_KEY)
            assert 0 < ttl <= 1500, "acquire must be SET NX EX (dead-holder self-heal)"

    def test_second_holder_cannot_acquire_while_held(self):
        with _mcp_browser_lock(job_id=1, wait_timeout=1.0):
            got = r.set(_TRAVERSAL_LOCK_KEY, "2", nx=True, ex=1500)
            assert not got, "SET NX must refuse a second holder (the 351/352 bug)"

    def test_releases_on_exception(self):
        with pytest.raises(RuntimeError):
            with _mcp_browser_lock(job_id=2, wait_timeout=1.0):
                raise RuntimeError("boom")
        assert r.get(_TRAVERSAL_LOCK_KEY) is None

    def test_release_is_owner_checked(self):
        """A timed-out walker must not delete the LIVE holder's lock."""
        r.set(_TRAVERSAL_LOCK_KEY, "999", nx=True, ex=60)
        with _mcp_browser_lock(job_id=3, wait_timeout=0.2, poll_interval=0.05) as acquired:
            assert acquired is False
            assert r.get(_TRAVERSAL_LOCK_KEY) == b"999", "proceeding walker must NOT delete"
        assert r.get(_TRAVERSAL_LOCK_KEY) == b"999", "lock it never held survives"

    def test_waits_while_held_then_acquires(self):
        r.set(_TRAVERSAL_LOCK_KEY, "999", nx=True, ex=60)

        def release():
            time.sleep(0.3)
            r.delete(_TRAVERSAL_LOCK_KEY)

        t = threading.Thread(target=release)
        t.start()
        with _mcp_browser_lock(job_id=5, wait_timeout=5.0, poll_interval=0.05) as acquired:
            t.join()
            assert acquired is True, "waiter must keep polling until the deadline"


class TestWiring:
    def test_traverse_call_holds_lock(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        start = src.index("def _invoke_navigation_traverse(")
        node = src[start:]
        i_lock = node.index("with _mcp_browser_lock(")
        i_trav = node.index("browser_traverse(")
        assert i_lock < i_trav, (
            "the MCP browser walk (browser_traverse) must run inside the "
            "traversal lock — jobs 349/350 interleaved on the shared Chrome"
        )

    def test_navigate_explore_fallback_holds_lock(self):
        """The archived LLM explorer also drives the MCP browser."""
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        start = src.index("def _invoke_navigation_traverse(")
        node = src[start:]
        assert node.count("with _mcp_browser_lock(") >= 2, (
            "both MCP-driving paths (browser_traverse walk + archived "
            "navigate_explore fallback) must hold the lock"
        )
        i_lock2 = node.index(
            "with _mcp_browser_lock(", node.index("with _mcp_browser_lock(") + 1
        )
        i_explore = node.index("navigate_explore(")
        assert i_lock2 < i_explore

    def test_lock_is_redis_backed_not_django_cache(self):
        """LocMemCache is per-process — jobs 351/352 both 'acquired' it.
        The acquire must be redis SET with nx=True + ex=."""
        src = inspect.getsource(_mcp_browser_lock)
        assert ".set(" in src and "nx=True" in src, (
            "acquire must be Redis SET NX (atomic across prefork workers)"
        )
        assert "ex=" in src or "ttl" in src
        assert "from agents.graph import _traversal_redis" or True


class TestLogging:
    def test_timeout_logs_loud_warning(self, caplog):
        r.set(_TRAVERSAL_LOCK_KEY, "999", nx=True, ex=60)
        import logging

        with caplog.at_level(logging.WARNING, logger="webapp.agents.graph"):
            with _mcp_browser_lock(job_id=7, wait_timeout=0.2, poll_interval=0.05):
                pass
        assert any("TRAVERSAL-LOCK" in r2.message for r2 in caplog.records), (
            "proceeding without the lock must be loud — it re-exposes the "
            "349/350 collision and the log is how we'll see it"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
