"""[wave-55 H2 job-1222 + M7] Watchdog guards — never kill a live child.

What happened (proven RCA, job 1222 noon.com 2026-10-05): the row sat 36m07s
in the ready queue (normal for a 3-slot pool), was claimed at 08:26:52.040Z,
and the watchdog's pre-graph fast-fail — which judges the SHAPE alone
(RUNNING + started_at=None + zero SessionLog rows + age > 300s) — failed it
at 08:26:52.046Z. The child was alive: 5.3 ms later ``_run_graph_job``'s
UNCONDITIONAL ``status=RUNNING + started_at`` save resurrected the row over
the FAILED verdict, and the revoked task produced a 1821s zombie until the
second liveness pass killed it.

Three guards, plus the honesty the docstring already promised:

- the RUNNING stamp is a conditional UPDATE that refuses to overwrite a
  terminal verdict ("not resurrecting failed job");
- the pre-graph discriminator needs the PROBE, not just the shape: a live
  child (or an unsayable probe) keeps waiting under the normal ladder;
- ``_task_liveness`` gains a third leg — the answering set must cover the
  cluster membership, so a worker too saturated to answer EITHER probe can
  no longer validate a partial reply as "runs nowhere";
- the caller treats ``unknown`` as keep-waiting (no evidence is not a
  corpse) and M7's honest fast path fails a PROVABLY absent task now
  instead of waiting out the 30-min silence window.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from scraper.models import ScrapeJob  # noqa: E402


_DERIVED = object()


class _FakeInspect:
    """Both probes answered by the same worker set unless told otherwise."""

    def __init__(self, reply, ping_reply=_DERIVED):
        self._reply = reply
        self._ping_reply = ping_reply
        self.timeout = None

    def active(self):
        return self._reply

    def ping(self):
        if self._ping_reply is _DERIVED:
            return {name: {"ok": "pong"} for name in (self._reply or {})}
        return self._ping_reply


def _set_inspect(monkeypatch, reply, ping_reply=_DERIVED):
    def fake_inspect(timeout=None):
        return _FakeInspect(reply, ping_reply)

    monkeypatch.setattr(
        "celery.current_app.control.inspect", fake_inspect, raising=False
    )


@pytest.fixture(autouse=True)
def _membership_isolation(monkeypatch):
    """No test may inherit another's cluster membership (cache is process
    local, but the suite is one process)."""
    import scraper.tasks as wt
    from django.core.cache import cache

    # getattr: the guard constants are the thing under test — a missing one is
    # a red test below, not a red fixture.
    env = getattr(wt, "CELERY_KNOWN_WORKERS_ENV", "")
    key = getattr(wt, "WORKER_SIGHTING_CACHE_KEY", "")
    if env:
        monkeypatch.delenv(env, raising=False)
    cache.delete(key)
    yield
    cache.delete(key)


@pytest.fixture(autouse=True)
def _no_kill_side_effects(monkeypatch):
    """The kill tail must not touch the broker or browser_service in tests."""
    revocations = []

    def fake_revoke(task_id, terminate=False, signal=None, **kw):
        revocations.append((task_id, terminate, signal))

    monkeypatch.setattr(
        "celery.current_app.control.revoke", fake_revoke, raising=False
    )
    monkeypatch.setattr(
        "agents.tools.browser_http.cancel_scrape",
        lambda job_id: {"requested": True, "flagged": []},
        raising=False,
    )
    monkeypatch.setattr("django.db.close_old_connections", lambda: None)
    return revocations


def _make_claimed_job(minutes_ago=237, task_id="t-1222"):
    """A claimed-but-never-started row: RUNNING, started_at NULL, no logs.

    237 min is job 1222's real age at the false verdict (14 227 s of queue
    wait before the claim)."""
    from django.utils import timezone

    from scraper.models import ScrapeJob

    job = ScrapeJob.objects.create(
        url="https://example.com/collections/jackets",
        status=ScrapeJob.STATUS_RUNNING,
        celery_task_id=task_id,
    )
    ScrapeJob.objects.filter(pk=job.pk).update(
        created_at=timezone.now() - timezone.timedelta(minutes=minutes_ago)
    )
    job.refresh_from_db()
    return job


def _log(job, content, minutes_ago, agent="site-analyzer"):
    from django.utils import timezone

    from scraper.models import SessionLog

    row = SessionLog.objects.create(
        job=job,
        role=SessionLog.ROLE_SYSTEM,
        agent=agent,
        content=content,
        seq=SessionLog.objects.filter(job=job).count(),
    )
    SessionLog.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timezone.timedelta(minutes=minutes_ago)
    )
    row.refresh_from_db()
    return row


def _run_watchdog():
    import scraper.tasks as wt

    wt.cleanup_stuck_jobs.__wrapped__()


class _FakeService:
    """Stands in for LangGraphService so the RUNNING stamp is reached without
    building the real graph."""

    last = None

    def __init__(self):
        self.built = 0
        self.streamed = []
        _FakeService.last = self

    def build_graph(self):
        self.built += 1
        return SimpleNamespace(get_state=lambda config: None)

    def get_thread_id(self, job_id):
        return f"thread-{job_id}"

    def get_config(self, job_id):
        return {"configurable": {}}

    def stream_graph(self, graph, initial_state, config, job):
        self.streamed.append(job.id)


@pytest.fixture
def _graph_run(monkeypatch):
    """Patch the graph builder + the finalize so ``_run_graph_job`` can be
    called for its stamp alone."""
    import scraper.tasks as wt

    monkeypatch.setattr(wt, "LangGraphService", _FakeService)
    finalized = []
    monkeypatch.setattr(wt, "_finalize_job", lambda job: finalized.append(job.id))
    return finalized


# ─── sub-fix 1: the RUNNING stamp cannot resurrect a terminal verdict ────────


class TestRunningStampGuard:
    @pytest.mark.django_db
    def test_a_failed_row_is_not_resurrected(self, _graph_run, caplog):
        """The 1222 mechanism: the watchdog failed the row while the child was
        between the claim and its stamp. The stamp must lose to the verdict."""
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_claimed_job()
        ScrapeJob.objects.filter(pk=job.pk).update(
            status=ScrapeJob.STATUS_FAILED,
            error_message="pre-graph fast-fail",
        )
        job.refresh_from_db()

        with caplog.at_level("ERROR", logger="scraper.tasks"):
            wt._run_graph_job(job)

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "pre-graph fast-fail" in (job.error_message or "")
        assert job.started_at is None
        assert _FakeService.last.streamed == []
        assert "not resurrecting failed job" in caplog.text

    @pytest.mark.django_db
    def test_a_pending_row_is_still_stamped(self, _graph_run):
        """The guard is a guard, not a gate: a normal dispatch still stamps
        and runs."""
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = ScrapeJob.objects.create(
            url="https://example.com/collections/coats",
            status=ScrapeJob.STATUS_PENDING,
        )
        wt._run_graph_job(job)

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING
        assert job.started_at is not None
        assert job.graph_thread_id == f"thread-{job.id}"
        assert _FakeService.last.streamed == [job.id]

    @pytest.mark.django_db
    def test_a_cancelled_row_is_not_resurrected_by_a_resume(self, monkeypatch):
        """The resume stamp sits behind its own build_graph() — the same
        window, the same guard: an operator cancel that lands while the resume
        task is building must not be overwritten. The guard returns before the
        graph is touched, so no checkpoint is needed for the pin."""
        import scraper.tasks as wt

        job = ScrapeJob.objects.create(
            url="https://example.com/collections/boots",
            status=ScrapeJob.STATUS_CANCELLED,
        )
        monkeypatch.setattr(wt, "LangGraphService", _FakeService)

        # __wrapped__ is bound to the celery task instance (bind=True).
        wt.resume_scrape_task.__wrapped__(job.id, {"choice": "Yes"})

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_CANCELLED
        # Built for the race window, then abandoned — the resume never ran.
        assert _FakeService.last.built == 1

    @pytest.mark.django_db
    def test_the_1222_race_is_closed_end_to_end(self, _graph_run, monkeypatch):
        """The audit's exact sequence: false pre-graph verdict (the watchdog
        judged the shape while the child was alive) → the child's stamp must
        still refuse to overwrite it."""
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_claimed_job()
        # The child IS alive: celery reports it active on its worker.
        _set_inspect(
            monkeypatch,
            {"celery-worker@railway": [{"id": "t-1222", "name": "scraper.tasks.run_scrape_task"}]},
        )

        _run_watchdog()
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING, (
            "the live child must not be failed by the pre-graph fast-fail"
        )

        # Now the honest corpse shape: nothing anywhere owns the task.
        ScrapeJob.objects.filter(pk=job.pk).update(
            status=ScrapeJob.STATUS_RUNNING, started_at=None
        )
        _set_inspect(
            monkeypatch, {"celery-worker@railway": [{"id": "other-999"}]}
        )
        _run_watchdog()
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "pre-graph fast-fail" in (job.error_message or "")

        wt._run_graph_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.started_at is None
        assert _FakeService.last.streamed == []


# ─── sub-fix 2: the pre-graph discriminator needs the probe ──────────────────


class TestPreGraphDiscriminator:
    @pytest.mark.django_db
    def test_a_live_child_between_claim_and_stamp_is_not_failed(self, monkeypatch):
        """Shape alone is not a verdict: the child is 5.3 ms from stamping."""
        from scraper.models import ScrapeJob

        job = _make_claimed_job()
        _set_inspect(
            monkeypatch,
            {"celery-worker@railway": [{"id": "t-1222", "name": "scraper.tasks.run_scrape_task"}]},
        )

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING
        assert job.error_message == ""

    @pytest.mark.django_db
    def test_an_unsayable_probe_keeps_the_row_waiting(self, monkeypatch):
        """No worker answered (saturated pool, broker hiccup) — that is not
        evidence the child is gone."""
        from scraper.models import ScrapeJob

        job = _make_claimed_job()
        _set_inspect(monkeypatch, None)

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING

    @pytest.mark.django_db
    def test_a_pre_graph_row_with_no_task_id_is_not_immortal(self, monkeypatch):
        """No id = nothing to probe = no evidence = no fast-fail. The ladder
        still owns the row, so skipping the fast-fail cannot strand it."""
        from scraper.models import ScrapeJob

        job = _make_claimed_job(minutes_ago=95, task_id="")
        import scraper.tasks as wt

        monkeypatch.setattr(wt, "_task_liveness", lambda task_id: "unknown")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "appears hung" in (job.error_message or "")

    @pytest.mark.django_db
    def test_a_dead_child_still_fast_fails(self, monkeypatch):
        """The arm's original purpose stands: a worker that died between the
        claim and graph start owns no task, so absence is still a verdict."""
        from scraper.models import ScrapeJob

        job = _make_claimed_job()
        _set_inspect(
            monkeypatch, {"celery-worker@railway": [{"id": "other-999"}]}
        )

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "pre-graph fast-fail" in (job.error_message or "")
        assert "never started" in (job.error_message or "")


# ─── sub-fix 3: the third liveness leg — cluster membership ──────────────────


class TestClusterMembershipLeg:
    def test_a_saturated_worker_missing_both_probes_is_unknown(self, monkeypatch):
        """The audit's collapse: both probes answered by the ONE worker that
        is free, while the member holding the task answers neither. Agreement
        between two probes that never reached the pool is not "runs nowhere"."""
        import scraper.tasks as wt

        wt._record_cluster_sighting(
            ["celery-worker@railway", "celery-events@railway"]
        )
        _set_inspect(
            monkeypatch,
            {"celery-events@railway": [{"id": "other-999"}]},
        )
        assert wt._task_liveness("t-1222") == "unknown"

    def test_a_full_cluster_answer_is_still_absent(self, monkeypatch):
        """Every member answered and the task is in nobody's active set —
        the verdict stays honest in the direction that fails."""
        import scraper.tasks as wt

        wt._record_cluster_sighting(
            ["celery-worker@railway", "celery-events@railway"]
        )
        _set_inspect(
            monkeypatch,
            {
                "celery-worker@railway": [{"id": "other-999"}],
                "celery-events@railway": [],
            },
        )
        assert wt._task_liveness("t-1222") == "absent"

    def test_an_env_pinned_membership_grounded_without_history(self, monkeypatch):
        """CELERY_KNOWN_WORKERS is the deployment's own word: it grounds the
        membership before any sighting exists."""
        import scraper.tasks as wt

        monkeypatch.setenv(
            wt.CELERY_KNOWN_WORKERS_ENV,
            "celery-worker@railway, celery-events@railway",
        )
        _set_inspect(
            monkeypatch, {"celery-events@railway": [{"id": "other-999"}]}
        )
        assert wt._task_liveness("t-1222") == "unknown"

    def test_stale_sightings_leave_the_membership(self, monkeypatch):
        """A worker silent past the TTL is a scaled-down node, not a missing
        answer — it must not pin the verdict to unknown forever."""
        import time

        from django.core.cache import cache

        import scraper.tasks as wt

        cache.set(
            wt.WORKER_SIGHTING_CACHE_KEY,
            {
                "celery-worker@railway": time.time()
                - wt.WORKER_SIGHTING_TTL_SECONDS
                - 10,
                "celery-events@railway": time.time(),
            },
            wt.WORKER_SIGHTING_TTL_SECONDS,
        )
        _set_inspect(
            monkeypatch, {"celery-events@railway": [{"id": "other-999"}]}
        )
        assert wt._task_liveness("t-1222") == "absent"

    @pytest.mark.django_db
    def test_the_watchdog_feeds_membership_from_its_own_probe(self, monkeypatch):
        """The tick's ping is the membership feed: a worker that answered this
        tick is a member the next tick cannot conclude behind."""
        import scraper.tasks as wt
        from django.core.cache import cache

        _set_inspect(
            monkeypatch,
            {"celery-worker@railway": [{"id": "other-999"}]},
        )
        _run_watchdog()

        seen = cache.get(wt.WORKER_SIGHTING_CACHE_KEY) or {}
        assert "celery-worker@railway" in seen

    @pytest.mark.django_db
    def test_unknown_liveness_is_kept_waiting_by_the_caller(self, monkeypatch):
        """No evidence is not a corpse: the old caller revoked a task it could
        not see, which is how a saturated pool loses a live run."""
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_claimed_job(minutes_ago=40, task_id="t-silent")
        ScrapeJob.objects.filter(pk=job.pk).update(
            started_at=job.created_at
        )
        job.refresh_from_db()
        _log(job, "[TOOL] browser_navigate: ...", 40)
        monkeypatch.setattr(wt, "_task_liveness", lambda task_id: "unknown")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING

    @pytest.mark.django_db
    def test_unknown_liveness_still_fails_at_the_wedge_backstop(self, monkeypatch):
        """Keep-waiting is bounded by the 90-min wedge backstop — the ladder
        still ends, it just refuses to guess."""
        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        job = _make_claimed_job(minutes_ago=95, task_id="t-silent")
        ScrapeJob.objects.filter(pk=job.pk).update(started_at=job.created_at)
        job.refresh_from_db()
        _log(job, "[TOOL] browser_navigate: ...", 95)
        monkeypatch.setattr(wt, "_task_liveness", lambda task_id: "unknown")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "appears hung" in (job.error_message or "")

    @pytest.mark.django_db
    def test_a_stale_member_blunts_the_fast_path_at_the_caller(self, monkeypatch):
        """The semantic pin of the membership leg: a member the probes did
        not reach blunts an agreeing-``absent`` to ``unknown`` — no evidence
        is not a corpse.

        The stale member here is a BANKED RESIDUAL, not a reachable
        deployment state in this repo: the blindness it models needs the
        sighting cache to outlive a container replace, and settings.py
        configures no CACHES, so Django's default LocMemCache applies
        per-process (webapp/agents/graph.py states this itself) and dies with
        each prefork child — recycled every <=10 tasks
        (CELERY_WORKER_MAX_TASKS_PER_CHILD). Post-deploy, no process can hold
        a rotated-out name. The reachable cost is the OPPOSITE direction: the
        derived membership is a per-process lower bound that in practice
        equals this tick's ``_ping_answerers()`` feed, so a member that
        misses the tick feed as well as both probes is invisible to every
        verdict — the audit's 1222 collapse is reduced, not eliminated, and
        grounding it is an ops action (CELERY_KNOWN_WORKERS, or a Redis
        backed CACHES), not a code change. See the census §3/§5 rows.
        """
        import time

        from django.core.cache import cache
        from django.utils import timezone

        from scraper.models import ScrapeJob

        import scraper.tasks as wt

        # Mid-graph and silent past the fast-path floor but well short of the
        # 30-min window: the only thing that can fail it now is the probe.
        job = _make_claimed_job(minutes_ago=8, task_id="t-absent")
        ScrapeJob.objects.filter(pk=job.pk).update(
            started_at=job.created_at + timezone.timedelta(seconds=30)
        )
        job.refresh_from_db()
        _log(job, "[TOOL] browser_navigate: ...", 8)

        # Post-deploy: the rotated-out name is still inside its TTL, the
        # rotated-in name answered this tick.
        cache.set(
            wt.WORKER_SIGHTING_CACHE_KEY,
            {
                "celery-worker@old": time.time() - 60,
                "celery-events@new": time.time(),
            },
            wt.WORKER_SIGHTING_TTL_SECONDS,
        )
        # Only the new node answers, and both probes agree the task runs
        # nowhere — the stale member is what stops that being ``absent``.
        _set_inspect(monkeypatch, {"celery-events@new": [{"id": "other-999"}]})
        monkeypatch.setattr(wt, "_ping_answerers", lambda: ["celery-events@new"])

        # Consultation witness: a row that never reaches the probe pins
        # nothing (see test_a_freshly_active_row_is_not_probed).
        probed: list[str] = []
        _real_absent = wt._provably_absent
        monkeypatch.setattr(
            wt,
            "_provably_absent",
            lambda task_id: probed.append(task_id) or _real_absent(task_id),
        )

        _run_watchdog()

        assert probed == ["t-absent"], (
            "the fast path must actually consult the probe for this row"
        )
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING


# ─── sub-fix 4 (M7): provable absence skips the 30-min silence window ────────


class TestProvableAbsenceFastPath:
    def _mid_graph_job(self, minutes_ago=8):
        """A RUNNING row with started_at and a real (non-heartbeat) log row —
        mid-graph, silent for a few minutes, well short of the 30-min window."""
        job = _make_claimed_job(minutes_ago=minutes_ago, task_id="t-absent")
        from django.utils import timezone

        from scraper.models import ScrapeJob

        ScrapeJob.objects.filter(pk=job.pk).update(
            started_at=job.created_at + timezone.timedelta(seconds=30)
        )
        job.refresh_from_db()
        _log(job, "[TOOL] browser_navigate: ...", minutes_ago)
        return job

    @pytest.mark.django_db
    def test_a_provably_absent_task_fails_now(self, monkeypatch):
        """Every member of the cluster answered and the task is nowhere: the
        docstring always promised an immediate fail. Job 1222's corpse waited
        the full window anyway (08:26:52 → 08:57:13)."""
        import scraper.tasks as wt

        job = self._mid_graph_job()
        wt._record_cluster_sighting(
            ["celery-worker@railway", "celery-events@railway"]
        )
        _set_inspect(
            monkeypatch,
            {
                "celery-worker@railway": [{"id": "other-999"}],
                "celery-events@railway": [],
            },
        )

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "Worker process lost" in (job.error_message or "")
        assert job.completed_at is not None

    @pytest.mark.django_db
    def test_absence_without_grounded_membership_keeps_waiting(self, monkeypatch):
        """Two agreeing probes with nothing to check them against is the
        pre-wave-55 evidence — the silence rule still owns the verdict. (The
        tick's own feed grounds membership from whoever answers, so at caller
        level this seam is reached when the membership read itself fails or
        has been aged out; pinned at that seam.)"""
        import scraper.tasks as wt

        job = self._mid_graph_job()
        monkeypatch.setattr(wt, "_cluster_membership", lambda: frozenset())
        monkeypatch.setattr(wt, "_task_liveness", lambda task_id: "absent")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING

    @pytest.mark.django_db
    def test_provable_absence_needs_both_the_verdict_and_the_grounding(self):
        """The fast path's whole contract in one place: absent alone is not
        enough, a grounded membership alone is not enough."""
        import unittest.mock as mock

        import scraper.tasks as wt

        with mock.patch.object(wt, "_cluster_membership", lambda: frozenset()), \
                mock.patch.object(wt, "_task_liveness", lambda t: "absent"):
            assert wt._provably_absent("t") is False
        with mock.patch.object(
            wt, "_cluster_membership",
            lambda: frozenset({"celery-worker@railway"}),
        ), mock.patch.object(wt, "_task_liveness", lambda t: "unknown"):
            assert wt._provably_absent("t") is False
        with mock.patch.object(
            wt, "_cluster_membership",
            lambda: frozenset({"celery-worker@railway"}),
        ), mock.patch.object(wt, "_task_liveness", lambda t: "absent"):
            assert wt._provably_absent("t") is True

    @pytest.mark.django_db
    def test_a_freshly_active_row_is_not_probed(self, monkeypatch):
        """The fast path costs a probe only where there is silence worth
        probing: a row with recent activity is alive by definition.

        The pin sits on the symbol the idle gate actually consults. Stubbing
        the inspect with a raise proves nothing — both probe handlers swallow
        any exception into ``unknown``/``[]`` — and the autouse membership
        isolation empties ``_cluster_membership()``, so ``_provably_absent``
        short-circuits on ``bool(...)`` before reaching a probe either way.
        Recording the call is the only observable of "costs no probe".
        """
        import scraper.tasks as wt

        job = self._mid_graph_job(minutes_ago=1)

        probed: list[str] = []
        monkeypatch.setattr(
            wt, "_provably_absent", lambda task_id: probed.append(task_id) or False
        )
        # Hermeticity, not behaviour: the tick's own membership feed is a
        # different symbol and must not reach a live broker from a test.
        monkeypatch.setattr(wt, "_ping_answerers", lambda: [])

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING
        assert probed == [], "a fresh row must not cost a liveness probe"

    @pytest.mark.django_db
    def test_the_window_is_meaningfully_shorter_than_silence(self):
        import scraper.tasks as wt

        assert 0 < wt.LIVENESS_FAST_PATH_IDLE_MINUTES < (
            wt.STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES
        )
