"""[wave-26 hotfix #2, jobs 349/350] Concurrent MCP-browser walks collide.

Local e2e 2026-09-08: jobs 349 (nike.in) and 350 (karenmillen) ran as the
wave-26 pair on prefork workers 2 and 1. browser_traverse drives the SHARED
Playwright MCP Chrome — the two walks interleaved (both captured the same
karenmillen verbolia XHR at 14:18:49.121) and job 349's walk observed
karenmillen pages that job 350 was browsing. The wave-19 cross-domain guard
correctly refused the poisoned result — turning an infrastructure collision
into a false "Navigator returned cross-domain traversal results twice" site
failure. The same shape explains prod cross-domain verdicts whenever two
jobs are in flight.

Contract: MCP-browser-driving walks hold a shared lock (cache-based, atomic
add, TTL'd so a dead holder can't deadlock the queue). A walker that can't
get the lock within its wait budget proceeds anyway with a loud warning —
availability over deadlock, the guard remains the honesty backstop.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.core.cache import cache  # noqa: E402

from agents.graph import _mcp_browser_lock  # noqa: E402

LOCK_KEY = "mcp-browser-lock"


@pytest.fixture(autouse=True)
def _clean_lock():
    cache.delete(LOCK_KEY)
    yield
    cache.delete(LOCK_KEY)


class TestLockBehavior:
    def test_free_lock_acquires_and_releases(self):
        with _mcp_browser_lock(job_id=1, wait_timeout=1.0) as acquired:
            assert acquired is True
            assert cache.get(LOCK_KEY) == 1
        assert cache.get(LOCK_KEY) is None, "lock must be released on exit"

    def test_releases_on_exception(self):
        with pytest.raises(RuntimeError):
            with _mcp_browser_lock(job_id=2, wait_timeout=1.0):
                raise RuntimeError("boom")
        assert cache.get(LOCK_KEY) is None

    def test_held_lock_times_out_but_body_still_runs(self):
        """A walker that can't get the lock proceeds with acquired=False —
        availability over deadlock; the cross-domain guard stays the
        honesty backstop for the rare contaminated walk."""
        cache.add(LOCK_KEY, 999, 60)
        with _mcp_browser_lock(job_id=3, wait_timeout=0.2, poll_interval=0.05) as acquired:
            assert acquired is False
        assert cache.get(LOCK_KEY) == 999, "must NOT delete a lock it never held"

    def test_waiter_acquires_once_holder_releases(self):
        cache.add(LOCK_KEY, 999, 60)
        cache.delete(LOCK_KEY)  # holder "finishes" before the waiter polls
        with _mcp_browser_lock(job_id=4, wait_timeout=2.0, poll_interval=0.05) as acquired:
            assert acquired is True

    def test_waits_while_held_then_acquires(self):
        cache.add(LOCK_KEY, 999, 60)

        # release the "holder" from another thread mid-wait
        import threading

        def release():
            import time

            time.sleep(0.3)
            cache.delete(LOCK_KEY)

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
        i_lock2 = node.index("with _mcp_browser_lock(", node.index("with _mcp_browser_lock(") + 1)
        i_explore = node.index("navigate_explore(")
        assert i_lock2 < i_explore

    def test_lock_is_ttl_scoped(self):
        """cache.add(key, value, ttl) — a crashed holder must not deadlock
        every future traversal."""
        import inspect

        src = inspect.getsource(_mcp_browser_lock)
        assert "cache.add(" in src and src.count(",") >= 2
        assert "cache.delete(" in src


class TestLogging:
    def test_timeout_logs_loud_warning(self, caplog):
        cache.add(LOCK_KEY, 999, 60)
        import logging

        with caplog.at_level(logging.WARNING, logger="webapp.agents.graph"):
            with _mcp_browser_lock(job_id=7, wait_timeout=0.2, poll_interval=0.05):
                pass
        assert any("TRAVERSAL-LOCK" in r.message for r in caplog.records), (
            "proceeding without the lock must be loud — it re-exposes the "
            "349/350 collision and the log is how we'll see it"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
