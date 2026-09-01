"""[wave-15] Queue honesty — PR-1 contract tests.

Wave-15 PR-1 makes the dispatch path honest end to end:

- 1.0 keystone: ``dispatch_scrape_job`` generates the task id, stamps the row
  BEFORE publishing, and reverts the stamp if the publish fails — so
  ``celery_task_id=""`` strictly means "never dispatched".
- 1.1 claim: ``run_scrape_task`` claims its row with one atomic UPDATE
  (PENDING, or RUNNING with the SAME task id = celery redelivery). No more
  read-then-judge dedup that two racing workers could both pass.
- 1.1b honesty: the same-site requeue's ``self.retry`` is wrapped — when the
  retry budget (decorator max_retries=1) is exhausted, MaxRetriesExceededError
  used to escape unhandled and strand the claimed row forever.
- 1.2 sweep: ``redispatch_abandoned_pending`` recovers abandoned
  PENDING-no-id rows, claims on the redispatch_count COUNTER (so the
  republished task's own entry claim still wins), and honestly fails a row
  after PENDING_REDISPATCH_CAP attempts. Env-gated OFF by default.
- R1: the watchdog fails a claimed-but-never-started row on first sighting
  past a short grace (pre-graph fast-fail) instead of waiting out the full
  silence window and mislabeling it.

Run from repo root:
    PYTHONPATH=/app:/app/webapp python -m pytest tests/test_wave15_queue_honesty.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


# ─── helpers ─────────────────────────────────────────────────────────────────


def _make_job(**kw):
    from scraper.models import ScrapeJob

    return ScrapeJob.objects.create(
        url=kw.pop("url", "https://example.com/p/1"),
        **kw,
    )


def _backdate(job, minutes):
    """created_at is auto_now_add — backdate via a queryset update."""
    from django.utils import timezone

    from scraper.models import ScrapeJob

    ScrapeJob.objects.filter(pk=job.pk).update(
        created_at=timezone.now() - timezone.timedelta(minutes=minutes)
    )
    job.refresh_from_db()


def _run_task(job_id, task_id=None, retry_exc=None):
    """Call run_scrape_task's raw body with a stubbed bound self."""
    from celery.exceptions import MaxRetriesExceededError

    import scraper.tasks as wt

    fake_self = SimpleNamespace(
        request=SimpleNamespace(id=task_id),
        retry=Mock(side_effect=retry_exc) if retry_exc else Mock(),
    )
    calls = {"graph": 0}

    def _fake_graph(job, rescrape=False, force_full=False):
        calls["graph"] += 1

    orig_graph = wt._run_graph_job
    wt._run_graph_job = _fake_graph
    try:
        # __wrapped__ is the bound method; __func__ is the raw (self, job_id).
        wt.run_scrape_task.__wrapped__.__func__(fake_self, job_id)
    finally:
        wt._run_graph_job = orig_graph
    return calls


@pytest.fixture(autouse=True)
def _quiet_finalize(monkeypatch):
    """_finalize_job_failed recycles DB connections (F4) — under pytest that
    closes the TEST's transactional connection. Neutralize, like wave-14."""
    monkeypatch.setattr("django.db.close_old_connections", lambda: None)


# ─── 1.0 keystone ────────────────────────────────────────────────────────────


class TestDispatchKeystone:
    @pytest.mark.django_db
    def test_stamps_row_before_publish_and_reuses_that_id(self, monkeypatch):
        import scraper.tasks as wt

        job = _make_job()
        seen = {}

        def fake_apply(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return SimpleNamespace(id=kwargs["task_id"])

        monkeypatch.setattr(wt.run_scrape_task, "apply_async", fake_apply)
        task_id = wt.dispatch_scrape_job(job.id, rescrape=True)

        job.refresh_from_db()
        assert task_id == seen["kwargs"]["task_id"]
        # The id on the row IS the id that was published — stamp BEFORE publish.
        assert job.celery_task_id == task_id
        assert seen["kwargs"]["args"] == (job.id,)
        assert seen["kwargs"]["kwargs"] == {"rescrape": True}

    @pytest.mark.django_db
    def test_publish_failure_reverts_the_stamp(self, monkeypatch):
        import scraper.tasks as wt

        job = _make_job()

        def boom(*args, **kwargs):
            raise RuntimeError("broker down")

        monkeypatch.setattr(wt.run_scrape_task, "apply_async", boom)
        with pytest.raises(RuntimeError, match="broker down"):
            wt.dispatch_scrape_job(job.id)
        job.refresh_from_db()
        # "" again = "never dispatched" — the sweep's recoverable signature.
        assert job.celery_task_id == ""

    @pytest.mark.django_db
    def test_ids_are_unique_per_dispatch(self, monkeypatch):
        import scraper.tasks as wt

        job = _make_job()
        ids = []
        monkeypatch.setattr(
            wt.run_scrape_task,
            "apply_async",
            lambda *a, **k: ids.append(k["task_id"]),
        )
        wt.dispatch_scrape_job(job.id)
        wt.dispatch_scrape_job(job.id)
        assert len(ids) == 2 and ids[0] != ids[1]

    def test_no_dispatch_site_uses_bare_delay(self):
        """Every former publish-then-stamp site now routes through the
        keystone — a stray ``run_scrape_task.delay(`` reintroduces the
        stamp-after-publish race the sweep's signature depends on."""
        sites = [
            os.path.join(ROOT, "webapp", "scraper", "views.py"),
            os.path.join(ROOT, "webapp", "scraper", "api", "writers.py"),
            os.path.join(ROOT, "webapp", "scraper", "management", "commands", "scrape.py"),
        ]
        for path in sites:
            src = open(path).read()
            assert "run_scrape_task.delay(" not in src, path


# ─── 1.1 entry claim ─────────────────────────────────────────────────────────


class TestEntryClaim:
    @pytest.mark.django_db
    def test_pending_row_is_claimed_with_the_task_id(self):
        from scraper.models import ScrapeJob

        job = _make_job()
        calls = _run_task(job.id, task_id="task-abc")
        assert calls["graph"] == 1
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING
        assert job.celery_task_id == "task-abc"

    @pytest.mark.django_db
    def test_same_id_running_reentry_is_allowed(self):
        """Celery republishes self.retry with the SAME id, and a worker-level
        redelivery re-executes the same message — both must reclaim their own
        RUNNING row instead of being dropped as duplicates."""
        from scraper.models import ScrapeJob

        job = _make_job(status=ScrapeJob.STATUS_RUNNING, celery_task_id="same-id")
        calls = _run_task(job.id, task_id="same-id")
        assert calls["graph"] == 1  # reclaimed, not skipped

    @pytest.mark.django_db
    def test_different_id_on_running_row_is_skipped(self, caplog):
        """A second, different dispatch must not steal the live row."""
        from scraper.models import ScrapeJob

        job = _make_job(status=ScrapeJob.STATUS_RUNNING, celery_task_id="live-id")
        with caplog.at_level("WARNING"):
            calls = _run_task(job.id, task_id="duplicate-id")
        assert calls["graph"] == 0
        assert "skipping duplicate dispatch" in caplog.text
        job.refresh_from_db()
        assert job.celery_task_id == "live-id"  # stamp NOT stolen

    @pytest.mark.django_db
    def test_eager_no_id_never_wipes_a_stamp(self):
        """Eager/always_eager runs (and tests) have no request id: the claim
        must still work for PENDING, but must not blank an existing stamp."""
        from scraper.models import ScrapeJob

        job = _make_job(status=ScrapeJob.STATUS_RUNNING, celery_task_id="pre-set")
        calls = _run_task(job.id, task_id=None)
        assert calls["graph"] == 0
        job.refresh_from_db()
        assert job.celery_task_id == "pre-set"

        fresh = _make_job(url="https://example.com/p/2")  # distinct: no sibling
        calls = _run_task(fresh.id, task_id=None)
        assert calls["graph"] == 1
        fresh.refresh_from_db()
        assert fresh.status == ScrapeJob.STATUS_RUNNING
        assert fresh.celery_task_id == ""  # claim didn't fabricate one

    @pytest.mark.django_db
    def test_waiting_approval_row_is_not_claimed(self):
        """WAITING_APPROVAL belongs to resume_scrape_task + the stuck-approved
        watchdog — a stray run_scrape_task delivery must not touch it."""
        from scraper.models import ScrapeJob

        job = _make_job(status=ScrapeJob.STATUS_WAITING_APPROVAL)
        calls = _run_task(job.id, task_id="t")
        assert calls["graph"] == 0
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_WAITING_APPROVAL


# ─── 1.1b requeue exhaustion is honest ───────────────────────────────────────


class TestRequeueExhaustion:
    @pytest.mark.django_db
    def test_max_retries_exceeded_finalizes_failed(self):
        """max_retries=None means the decorator default (1): once spent,
        self.retry RAISES MaxRetriesExceededError outside the graph try — which
        used to strand the claimed row forever."""
        from celery.exceptions import MaxRetriesExceededError

        from scraper.models import ScrapeJob

        job = _make_job()
        _make_job(url=job.url, status=ScrapeJob.STATUS_RUNNING)  # the sibling
        calls = _run_task(job.id, task_id="t", retry_exc=MaxRetriesExceededError())
        assert calls["graph"] == 0
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "Same-site requeue exhausted" in (job.error_message or "")
        assert job.completed_at is not None


# ─── 1.2 redispatch sweep ────────────────────────────────────────────────────


@pytest.fixture
def sweep_enabled(monkeypatch):
    from django.conf import settings

    monkeypatch.setattr(settings, "REDISPATCH_SWEEP_ENABLED", True)


class TestRedispatchSweep:
    def test_disabled_by_default(self):
        """The sweep MUST ship OFF: prod rows never carried a stamp before
        wave-15, so "" still covers queued work until the keystone deploys."""
        import scraper.tasks as wt

        from django.conf import settings

        assert not getattr(settings, "REDISPATCH_SWEEP_ENABLED", True)
        assert wt.redispatch_abandoned_pending() == {"action": "disabled"}

    @pytest.mark.django_db
    def test_recovers_one_abandoned_pending_row(self, sweep_enabled, monkeypatch):
        import scraper.tasks as wt
        from scraper.models import ScrapeJob

        job = _make_job()
        _backdate(job, 30)
        published = []
        monkeypatch.setattr(
            wt, "dispatch_scrape_job", lambda jid, **kw: published.append((jid, kw))
        )
        out = wt.redispatch_abandoned_pending()
        assert out == {"action": "redispatched", "job_id": job.id}
        assert published == [(job.id, {"rescrape": False})]
        job.refresh_from_db()
        # Claim is on the COUNTER, not the status: the republished task's own
        # entry claim (PENDING arm) must still be able to win.
        assert job.status == ScrapeJob.STATUS_PENDING
        assert job.redispatch_count == 1

    @pytest.mark.django_db
    def test_young_rows_are_left_alone(self, sweep_enabled, monkeypatch):
        import scraper.tasks as wt

        job = _make_job()  # just created — inside the claim floor
        monkeypatch.setattr(wt, "dispatch_scrape_job", lambda jid, **kw: None)
        assert wt.redispatch_abandoned_pending() == {"action": "idle"}
        job.refresh_from_db()
        assert job.redispatch_count == 0

    @pytest.mark.django_db
    def test_queued_rows_with_a_task_id_are_not_redispatched(
        self, sweep_enabled, monkeypatch
    ):
        """PENDING WITH a stamp = queued normally (same-site serializer) —
        redispatching those would double-dispatch."""
        import scraper.tasks as wt

        job = _make_job(celery_task_id="queued-id")
        _backdate(job, 60)
        monkeypatch.setattr(wt, "dispatch_scrape_job", lambda jid, **kw: None)
        assert wt.redispatch_abandoned_pending() == {"action": "idle"}

    @pytest.mark.django_db
    def test_exhausted_row_fails_honestly(self, sweep_enabled, monkeypatch):
        import scraper.tasks as wt
        from scraper.models import ScrapeJob

        job = _make_job(redispatch_count=wt.PENDING_REDISPATCH_CAP)
        _backdate(job, 30)
        published = []
        monkeypatch.setattr(
            wt, "dispatch_scrape_job", lambda jid, **kw: published.append(jid)
        )
        out = wt.redispatch_abandoned_pending()
        assert out == {"action": "failed", "job_id": job.id}
        assert published == []  # no fourth attempt
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "never claimed a worker" in (job.error_message or "")


# ─── R1 pre-graph fast-fail ──────────────────────────────────────────────────


class TestPreGraphFastFail:
    def _run_watchdog(self, monkeypatch):
        import scraper.tasks as wt

        monkeypatch.setattr(wt, "_publish_job_status", lambda *a, **k: None)
        monkeypatch.setattr(
            "agents.tools.browser_http.cancel_scrape",
            lambda job_id: {"requested": False},
        )
        wt.cleanup_stuck_jobs()

    @pytest.mark.django_db
    def test_claimed_never_started_row_fails_fast(self, monkeypatch):
        from scraper.models import ScrapeJob

        job = _make_job(status=ScrapeJob.STATUS_RUNNING, celery_task_id="claimed")
        _backdate(job, 10)  # well past the 300s grace, well short of 30 min
        self._run_watchdog(monkeypatch)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "pre-graph fast-fail" in (job.error_message or "")
        assert "never started" in (job.error_message or "")

    @pytest.mark.django_db
    def test_young_claimed_row_is_not_killed(self, monkeypatch):
        """The grace keeps a just-claimed job safe — it looks identical to a
        corpse for its first seconds."""
        from scraper.models import ScrapeJob

        job = _make_job(status=ScrapeJob.STATUS_RUNNING)
        self._run_watchdog(monkeypatch)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING


# ─── settings wiring pins ────────────────────────────────────────────────────


class TestSettingsWiring:
    def test_sweep_and_watchdog_route_to_the_events_queue(self):
        src = open(os.path.join(ROOT, "webapp", "config", "settings.py")).read()
        assert '"scraper.tasks.redispatch_abandoned_pending": "events"' in src
        # The stuck-approved watchdog was previously UNROUTED — it only worked
        # by luck of the scrape pool's queueing.
        assert '"scraper.tasks.redispatch_stuck_approved_interrupts": "events"' in src

    def test_sweep_beat_entry_precedes_schedule_next_site(self):
        src = open(os.path.join(ROOT, "webapp", "config", "settings.py")).read()
        assert '"redispatch-abandoned-pending"' in src
        assert src.index('"redispatch-abandoned-pending"') < src.index(
            '"schedule-next-site"'
        )

    def test_sweep_master_switch_defaults_off(self):
        src = open(os.path.join(ROOT, "webapp", "config", "settings.py")).read()
        assert 'config("REDISPATCH_SWEEP_ENABLED", default=False' in src

    def test_redispatch_count_field_and_migration_exist(self):
        model_src = open(os.path.join(ROOT, "webapp", "scraper", "models.py")).read()
        assert "redispatch_count = models.IntegerField(default=0)" in model_src
        mig = os.path.join(
            ROOT, "webapp", "scraper", "migrations",
            "0036_scrapejob_redispatch_count.py",
        )
        assert os.path.exists(mig)


# ─── 1.3 /health queue component ─────────────────────────────────────────────


class _FakeRedis:
    def __init__(self, llen=(0, 0), zcard=0):
        self._llen = llen
        self._zcard = zcard

    def llen(self, key):
        return self._llen[0] if key == "celery" else self._llen[1]

    def zcard(self, key):
        return self._zcard


class TestHealthQueue:
    def _call(self, monkeypatch, fakeredis, cache_stub):
        import scraper.views as views

        monkeypatch.setattr("redis.from_url", lambda *a, **k: fakeredis)
        monkeypatch.setattr("django.core.cache.cache", cache_stub)
        return views._check_queue()

    @pytest.mark.django_db
    def test_healthy_queue_reports_up_with_gauges(self, monkeypatch):
        import scraper.views as views

        out = self._call(
            monkeypatch,
            _FakeRedis(llen=(2, 5), zcard=0),
            SimpleNamespace(get=lambda k: None, set=lambda *a, **k: None),
        )
        assert out["status"] == "up"
        assert out["gauges"]["celery_queue_depth"] == 2
        assert out["gauges"]["events_queue_depth"] == 5
        assert out["alerts"] == []

    @pytest.mark.django_db
    def test_old_undispatched_pending_raises_stranded_alert(self, monkeypatch):
        job = _make_job()
        _backdate(job, 30)  # > PENDING_CLAIM_MINUTES with no task id
        out = self._call(
            monkeypatch,
            _FakeRedis(),
            SimpleNamespace(get=lambda k: None, set=lambda *a, **k: None),
        )
        assert out["status"] == "warn"
        assert any("stranded PENDING" in a for a in out["alerts"])
        assert out["gauges"]["oldest_undispatched_job"] == job.id

    @pytest.mark.django_db
    def test_old_pending_with_id_is_not_stranded(self, monkeypatch):
        """Queued-behind-a-sibling is legitimate — must NOT alert."""
        job = _make_job(celery_task_id="queued")
        _backdate(job, 120)
        out = self._call(
            monkeypatch,
            _FakeRedis(),
            SimpleNamespace(get=lambda k: None, set=lambda *a, **k: None),
        )
        assert out["status"] == "up"
        assert out["gauges"]["oldest_pending_age_s"] >= 120 * 60

    @pytest.mark.django_db
    def test_events_backlog_and_stuck_unacked_alert(self, monkeypatch):
        calls = {"set": 0}

        class _Cache:
            def get(self, k):
                return 3  # unacked seen on the previous poll too

            def set(self, *a, **k):
                calls["set"] += 1

        out = self._call(
            monkeypatch,
            _FakeRedis(llen=(0, 25), zcard=3),
            _Cache(),
        )
        assert any("events queue backlog" in a for a in out["alerts"])
        assert any("unacked_index stuck" in a for a in out["alerts"])
        assert out["status"] == "warn"

    @pytest.mark.django_db
    def test_redis_failure_reports_down_not_500(self, monkeypatch):
        import scraper.views as views

        def boom(*a, **k):
            raise RuntimeError("no redis")

        monkeypatch.setattr("redis.from_url", boom)
        monkeypatch.setattr(
            "django.core.cache.cache",
            SimpleNamespace(get=lambda k: None, set=lambda *a, **k: None),
        )
        out = views._check_queue()
        assert out["status"] == "down"
        assert "no redis" in out["detail"]
