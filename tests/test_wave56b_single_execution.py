"""[wave-56b F8] One job, one live execution.

Live proof (job 582, 2026-10-10 ~03:31 log-tz): a redelivered copy of task
``0b3ce722`` ran CONCURRENTLY with the original on a second pool pid — both
graph instances wrote the same ``workspace/briscoes-co-nz/scraper_draft.py``.
The duplicate-dispatch guard (``webapp/scraper/tasks.py`` ~:1203-1255)
checks only the DB ``status`` column, which the stuck-job watchdog re-drive
had just flipped — so a live execution had no lock to defend itself with.
The slot was contaminated and had to be re-fired (589).

Contract: a Redis-backed (Django-cache) live-instance lock with heartbeat
renewal. The dedup guard must refuse when the lock is held even if the
status column lies; the watchdog re-drive must refuse while an execution is
live; a crashed owner's lock must expire (TTL) so the job can recover.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


class FakeCache:
    """SETNX-with-TTL surface (django cache.add semantics) + manual clock."""

    def __init__(self):
        self.store: dict = {}
        self.expires: dict = {}
        self.now = 1000.0

    def add(self, key, value, timeout=None):
        exp = self.expires.get(key)
        if key in self.store and exp is not None and exp > self.now:
            return False
        self.store[key] = value
        self.expires[key] = self.now + (timeout or 0)
        return True

    def get(self, key, default=None):
        exp = self.expires.get(key)
        if key not in self.store or (exp is not None and exp <= self.now):
            return default
        return self.store[key]

    def delete(self, key):
        self.store.pop(key, None)
        self.expires.pop(key, None)


@pytest.fixture
def lock():
    from scraper.tasks import JobExecutionLock

    return JobExecutionLock(cache=FakeCache(), ttl_s=300)


class TestMutualExclusion:
    def test_second_acquire_refused_while_held(self, lock):
        assert lock.acquire(582, owner="pid-23") is True
        assert lock.acquire(582, owner="pid-25") is False

    def test_release_allows_reacquire(self, lock):
        assert lock.acquire(582, owner="pid-23")
        lock.release(582, owner="pid-23")
        assert lock.acquire(582, owner="pid-25") is True

    def test_non_owner_cannot_release(self, lock):
        lock.acquire(582, owner="pid-23")
        lock.release(582, owner="pid-25")
        assert lock.acquire(582, owner="pid-25") is False

    def test_owner_heartbeat_renews(self, lock):
        assert lock.acquire(582, owner="pid-23")
        assert lock.renew(582, owner="pid-23") is True

    def test_non_owner_cannot_renew(self, lock):
        lock.acquire(582, owner="pid-23")
        assert lock.renew(582, owner="pid-25") is False

    def test_is_held_tracks_state(self, lock):
        assert lock.is_held(582) is False
        lock.acquire(582, owner="pid-23")
        assert lock.is_held(582) is True
        lock.release(582, owner="pid-23")
        assert lock.is_held(582) is False


class TestCrashRecovery:
    def test_stale_lock_expires_so_the_job_recovers(self, lock):
        assert lock.acquire(582, owner="pid-doomed")
        lock.cache.now += 301          # past TTL, owner crashed
        assert lock.is_held(582) is False
        assert lock.acquire(582, owner="pid-recovery") is True

    def test_renew_keeps_a_live_lock_alive(self, lock):
        lock.acquire(582, owner="pid-23")
        lock.cache.now += 200
        assert lock.is_held(582) is True, "inside TTL"
        lock.renew(582, owner="pid-23")
        lock.cache.now += 200
        assert lock.is_held(582) is True, "renewed past the original TTL"

    def test_cache_outage_fails_closed(self):
        """No cache = cannot prove exclusivity = refuse to run."""
        from scraper.tasks import JobExecutionLock

        class DeadCache:
            def add(self, *a, **k):
                raise RuntimeError("redis down")

        lck = JobExecutionLock(cache=DeadCache(), ttl_s=300)
        assert lck.acquire(582, owner="pid-1") is False


class TestWiring:
    """The guard and the watchdog must actually consult the lock."""

    def test_dedup_guard_checks_the_lock(self):
        import inspect

        import scraper.tasks as t

        src = inspect.getsource(t.run_scrape_task)
        assert "JobExecutionLock" in src or "_job_exec_lock" in src

    def test_watchdog_refuses_re_drive_while_live(self):
        import inspect

        import scraper.tasks as t

        src = inspect.getsource(t.cleanup_stuck_jobs)
        assert "is_held" in src or "JobExecutionLock" in src, (
            "stuck-job watchdog must consult the execution lock"
        )
