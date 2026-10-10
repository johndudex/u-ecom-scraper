"""[wave-56b F8 remainder] The resume path runs under the same one-execution
lock as dispatch.

Guardian cp1.5(b): ``resume_scrape_task`` drives the graph INLINE
(``build_graph()``, no ``run_scrape_task`` hop, NO lock); its only duplicate
guard is ``status == RUNNING``. A redelivered resume of a WAITING_APPROVAL
row sails through — two graph instances on one checkpoint/workspace, the
582/583/584 shape one approval later. ``redispatch_stuck_approved_interrupts``
reaches the graph through the same resume path, and its re-drive would burn
``resume_attempts`` on a dispatch the lock then refuses.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


@pytest.fixture
def waiting_job(db):
    from scraper.models import ScrapeJob

    return ScrapeJob.objects.create(
        url="https://resume-lock.example/p",
        status=ScrapeJob.STATUS_WAITING_APPROVAL,
        input_mode="url_list",
        page_type="product",
    )


class TestResumeRunsUnderTheLock:
    def test_redelivered_resume_refused_while_lock_held(self, waiting_job):
        """A live resume holds the lock: a redelivered resume of the SAME
        WAITING_APPROVAL row must return before the graph is built and must
        not flip the row."""
        import scraper.tasks as t
        from scraper.models import ScrapeJob

        lck = t.JobExecutionLock(ttl_s=60)
        assert lck.acquire(waiting_job.id, owner="live-resume") is True
        try:
            with patch.object(
                t, "LangGraphService",
                side_effect=AssertionError("graph built under a held lock"),
            ):
                res = t.resume_scrape_task.apply(
                    args=[waiting_job.id, {"decision": "approve"}])
            assert not res.failed(), (
                f"resume ran under a held lock: {getattr(res, 'result', res)}"
            )
            waiting_job.refresh_from_db()
            assert waiting_job.status == ScrapeJob.STATUS_WAITING_APPROVAL
        finally:
            lck.release(waiting_job.id, owner="live-resume")

    def test_resume_releases_the_lock_on_every_exit(self, waiting_job):
        """A resume that dies mid-flight must leave the lock free — the
        finally-release is what keeps honest reclaim possible."""
        import scraper.tasks as t

        service = MagicMock()
        service.get_config.return_value = {}
        service.build_graph.return_value.get_state.side_effect = RuntimeError(
            "boom mid-resume")
        with patch.object(t, "LangGraphService", return_value=service):
            t.resume_scrape_task.apply(
                args=[waiting_job.id, {"decision": "approve"}])
        lck = t.JobExecutionLock(ttl_s=60)
        assert lck.is_held(waiting_job.id) is False, (
            "a dead resume must release the execution lock"
        )


class TestWatchdogLivenessSkip:
    def test_redispatch_consults_the_lock(self):
        """The stuck-approved watchdog must not burn resume_attempts on a
        dispatch the execution lock will refuse."""
        import inspect

        import scraper.tasks as t

        src = inspect.getsource(t.redispatch_stuck_approved_interrupts)
        assert "is_held" in src or "JobExecutionLock" in src


class TestNoVestigialLockCode:
    def test_redis_store_carries_no_lock_methods(self):
        """``_RedisSetnxStore`` grew a copy-pasted copy of JobExecutionLock's
        methods that reference ``self.cache``/``self._ttl`` — attributes the
        store does not have. Dead on arrival, one refactor away from a
        silent AttributeError."""
        from scraper.tasks import _RedisSetnxStore

        for name in ("acquire", "renew", "release", "is_held", "_key"):
            assert not hasattr(_RedisSetnxStore, name), name
