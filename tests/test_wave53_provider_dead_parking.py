"""[wave-53 candidate 10] Provider-dead parking — deaths become delays.

The measured shape (7 writer deaths on 2026-10-02: 521/524/525/526/527/529/
536 429-class, 537/541/543 Connection-class): the fire-time gate was green,
the provider opened a REQUEST-RATE window mid-job, the writer's invocation
rode the retry stretch and died twice with no draft, and the job finalized
honest-FAILED — throwing away a fully-paid probe + traverse + derivation
(the 92,439-char carrier embed proved the strategy was RIGHT). The provider
episodes are TIME-SLICES (~10-30 min); the same job re-driven 25 minutes
later starts from the preserved artifacts and a green window.

Fix (mirrors the browser_unavailable park/resume pair):
- the writer's skip_approvals double-death arm classifies the error note —
  provider-class deaths route to a ``park_provider_dead`` graph node that
  parks the row (STATUS_PROVIDER_DEAD, last_parked_at stamped) instead of
  finalizing FAILED;
- a beat resumer on the events queue flips parked rows back to PENDING after
  ``PROVIDER_REDRIVE_DELAY_S`` (25 min), bumps ``redispatch_count``, and
  re-dispatches through the same entry as a manual re-drive — check_tracker's
  skip flags resume from the preserved analysis;
- after ``PROVIDER_REDRIVE_MAX`` (2) re-drives the resumer finalizes FAILED
  with an honest "park exhausted" message. Walls and tester-fails NEVER park:
  the classifier only opens for provider-class notes at the writer-death site.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


# ─── the classifier ──────────────────────────────────────────────────────────


class TestProviderDeadClassifier:
    def test_connection_error_note_parks(self):
        from agents.graph import _is_provider_dead_note

        assert _is_provider_dead_note("Connection error.")

    def test_429_rate_limit_note_parks(self):
        from agents.graph import _is_provider_dead_note

        note = (
            "Error code: 429 - {'error': {'code': '1302', "
            "'message': 'Rate limit exceeded'}}"
        )
        assert _is_provider_dead_note(note)

    def test_usage_limit_note_parks(self):
        from agents.graph import _is_provider_dead_note

        assert _is_provider_dead_note(
            "Error code: 429 - {'message': 'Usage limit reached'}"
        )

    def test_provider_timeout_note_parks(self):
        from agents.graph import _is_provider_dead_note

        assert _is_provider_dead_note("Request timed out. APITimeoutError")

    def test_wall_clock_death_never_parks(self):
        """The wall-clock arm has its own escalation ladder (W30/T12) — a
        timed-out invocation is not a provider verdict."""
        from agents.graph import _is_provider_dead_note

        assert not _is_provider_dead_note(
            "wall-clock timeout after 900s base (thread stopped at 2693.4s)"
        )

    def test_unknown_note_does_not_park(self):
        from agents.graph import _is_provider_dead_note

        assert not _is_provider_dead_note("invocation returned no messages")


# ─── the park site routing (source contracts) ───────────────────────────────


class TestParkSiteRouting:
    def test_double_death_arm_routes_provider_class_to_the_park(self):
        import inspect

        import agents.graph as g

        src = inspect.getsource(g._invoke_code_writer)
        assert '"park_provider_dead"' in src

    def test_non_provider_double_death_still_goes_to_cleanup(self):
        import inspect

        import agents.graph as g

        src = inspect.getsource(g._invoke_code_writer)
        assert 'goto="cleanup"' in src

    def test_park_node_registered_in_the_graph(self):
        import inspect

        import agents.graph as g

        src = inspect.getsource(g.build_scrape_graph)
        assert 'add_node("park_provider_dead"' in src


# ─── the park node (DB) ──────────────────────────────────────────────────────


def _make_running_job():
    from django.utils import timezone

    from scraper.models import ScrapeJob

    job = ScrapeJob.objects.create(
        url="https://example.com/collections/jackets",
        status=ScrapeJob.STATUS_RUNNING,
        celery_task_id="park-task-1",
    )
    ScrapeJob.objects.filter(pk=job.pk).update(
        started_at=timezone.now() - timezone.timedelta(minutes=30),
        created_at=timezone.now() - timezone.timedelta(minutes=30),
    )
    job.refresh_from_db()
    return job


@pytest.fixture(autouse=True)
def _quiet_finalize(monkeypatch):
    monkeypatch.setattr("django.db.close_old_connections", lambda: None)


class TestParkNode:
    @pytest.mark.django_db
    def test_park_flips_row_and_stamps_the_episode(self):
        from scraper.models import ScrapeJob

        import agents.graph as g

        job = _make_running_job()
        state = {
            "job_id": job.id,
            "provider_dead_detail": (
                "code_writer failed twice without producing a draft "
                "(Connection error.)"
            ),
        }
        before = time.time()
        cmd = g._park_provider_dead(state)

        job.refresh_from_db()
        assert cmd.goto is not None
        assert job.status == ScrapeJob.STATUS_PROVIDER_DEAD
        assert job.completed_at is None, "a park is resumable, not terminal"
        assert "Connection error" in (job.error_message or "")
        assert job.last_parked_at is not None
        assert job.last_parked_at >= before - 5

    @pytest.mark.django_db
    def test_second_park_refreshes_the_delay_clock(self):
        """Job-545 live finding: Coalesce kept park #1's timestamp, so park
        #2 inherited the elapsed 25-min delay and re-drove immediately
        instead of waiting a fresh window. Every park is an episode start —
        the clock must restart from it."""
        from scraper.models import ScrapeJob

        import agents.graph as g

        job = _make_running_job()
        ScrapeJob.objects.filter(pk=job.pk).update(
            last_parked_at=time.time() - 40 * 60
        )
        state = {
            "job_id": job.id,
            "provider_dead_detail": "code_writer failed twice (Connection error.)",
        }
        before = time.time()
        g._park_provider_dead(state)

        job.refresh_from_db()
        assert job.last_parked_at >= before - 5, (
            "a second park must stamp a FRESH delay clock, not inherit the "
            "first episode's elapsed time"
        )

    @pytest.mark.django_db
    def test_park_at_cap_finalizes_failed_honestly(self):
        from scraper.models import ScrapeJob

        import agents.graph as g

        job = _make_running_job()
        ScrapeJob.objects.filter(pk=job.pk).update(redispatch_count=2)
        state = {
            "job_id": job.id,
            "provider_dead_detail": "code_writer failed twice (Connection error.)",
        }
        g._park_provider_dead(state)

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "exhausted" in (job.error_message or "").lower()

    @pytest.mark.django_db
    def test_finalizer_skips_parked_rows(self):
        import inspect

        import scraper.tasks as wt

        src = inspect.getsource(wt._finalize_job)
        assert "STATUS_PROVIDER_DEAD" in src


# ─── the resumer (DB) ────────────────────────────────────────────────────────


def _park_row(job, minutes_ago=26, count=0):
    from django.utils import timezone

    from scraper.models import ScrapeJob

    ScrapeJob.objects.filter(pk=job.pk).update(
        status=ScrapeJob.STATUS_PROVIDER_DEAD,
        last_parked_at=time.time() - minutes_ago * 60,
        redispatch_count=count,
        completed_at=None,
    )
    job.refresh_from_db()


class TestResumer:
    @pytest.mark.django_db
    def test_fresh_park_is_not_resumed(self, monkeypatch):
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_running_job()
        _park_row(job, minutes_ago=2)
        dispatched = []
        monkeypatch.setattr(
            wt, "dispatch_scrape_job", lambda jid, **kw: dispatched.append(jid)
        )

        wt.resume_provider_dead_jobs.__wrapped__()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_PROVIDER_DEAD
        assert dispatched == []

    @pytest.mark.django_db
    def test_aged_park_is_redispatched_with_count_bump(self, monkeypatch):
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_running_job()
        _park_row(job, minutes_ago=26, count=0)
        dispatched = []
        monkeypatch.setattr(
            wt, "dispatch_scrape_job", lambda jid, **kw: dispatched.append(jid)
        )

        wt.resume_provider_dead_jobs.__wrapped__()

        job.refresh_from_db()
        assert dispatched == [job.id]
        assert job.status == ScrapeJob.STATUS_PENDING
        assert job.redispatch_count == 1

    @pytest.mark.django_db
    def test_dispatch_failure_reparks_the_row(self, monkeypatch):
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_running_job()
        _park_row(job, minutes_ago=26, count=0)

        def boom(jid, **kw):
            raise RuntimeError("broker down")

        monkeypatch.setattr(wt, "dispatch_scrape_job", boom)

        wt.resume_provider_dead_jobs.__wrapped__()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_PROVIDER_DEAD

    @pytest.mark.django_db
    def test_row_at_cap_finalizes_failed(self, monkeypatch):
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_running_job()
        _park_row(job, minutes_ago=26, count=2)
        dispatched = []
        monkeypatch.setattr(
            wt, "dispatch_scrape_job", lambda jid, **kw: dispatched.append(jid)
        )

        wt.resume_provider_dead_jobs.__wrapped__()

        job.refresh_from_db()
        assert dispatched == []
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "exhausted" in (job.error_message or "").lower()
        assert job.completed_at is not None


# ─── wiring contracts ────────────────────────────────────────────────────────


class TestParkingWiring:
    def test_status_constant_and_label_exist(self):
        from scraper.models import ScrapeJob

        assert ScrapeJob.STATUS_PROVIDER_DEAD == "provider_dead"
        assert any(
            v == "provider_dead" for v, _ in ScrapeJob.STATUS_CHOICES
        )

    def test_beat_schedule_entry_exists(self):
        src = open(os.path.join(
            ROOT, "webapp", "config", "settings.py"
        )).read()
        assert '"scraper.tasks.resume_provider_dead_jobs"' in src
        assert '": "events"' in src

    def test_parked_rows_hold_the_auto_scheduler(self):
        import inspect

        import scraper.tasks as wt

        # The logic lives in the _do_ impl; schedule_next_site is the beat
        # wrapper.
        src = inspect.getsource(wt._do_schedule_next_site)
        assert "STATUS_PROVIDER_DEAD" in src

    def test_redrive_constants_match_the_owner_spec(self):
        """Owner spec: re-drive 20-30 min later, max 2 attempts."""
        import scraper.tasks as wt

        assert 20 * 60 <= wt.PROVIDER_REDRIVE_DELAY_S <= 30 * 60
        assert wt.PROVIDER_REDRIVE_MAX == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
