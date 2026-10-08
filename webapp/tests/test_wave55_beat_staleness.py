"""[wave-55 H7] The beat health check judges every enabled entry, not the newest.

Audit (2026-10-05, H7 #22): ``_check_celery_beat`` read only the newest
``last_run_at`` across enabled tasks, so one dead entry hid behind fresh
30s/120s/300s peers — the purge-retention entry had been silent for 19 days
while /health/ reported the beat green every day. Each ENABLED entry is now
judged against its own schedule period ×3 (grace); any stale enabled entry
degrades the check and names the task.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_beat_staleness.py -q"
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402
from django_celery_beat.models import (  # noqa: E402
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
)
from scraper.views import _check_celery_beat  # noqa: E402

pytestmark = pytest.mark.django_db


def _interval(seconds: float) -> IntervalSchedule:
    return IntervalSchedule.objects.get_or_create(
        every=seconds,
        period=IntervalSchedule.SECONDS,
    )[0]


def _task(name: str, period_s: float, last_ago_s: float | None,
          enabled: bool = True) -> PeriodicTask:
    last = dj_tz.now() - timedelta(seconds=last_ago_s) if last_ago_s is not None else None
    return PeriodicTask.objects.create(
        name=name,
        task=f"scraper.tasks.{name}",
        interval=_interval(period_s),
        enabled=enabled,
        last_run_at=last,
    )


class TestPerEntryStaleness:
    def test_a_stale_enabled_entry_degrades_and_names_itself(self):
        """The audit shape: fresh 30s/120s/300s peers plus one dead daily
        entry. The check must read degraded, not up, and the detail must name
        the dead entry — "something is stale" without a name is a restart hunt
        with no suspect."""
        _task("dispatch-pending-callbacks", 30, 20)
        _task("resume-browser-unavailable", 120, 60)
        _task("cleanup-stuck-jobs", 300, 100)
        dead = _task("purge-retention", 86400, 19 * 86400)

        result = _check_celery_beat()

        assert result["status"] == "degraded", result
        assert dead.name in result["detail"], result
        for fresh in ("dispatch-pending-callbacks", "cleanup-stuck-jobs"):
            assert fresh not in result["detail"], result

    def test_the_fresh_fleet_still_reads_up(self):
        _task("dispatch-pending-callbacks", 30, 20)
        _task("resume-provider-dead", 120, 60)
        _task("schedule-next-site", 300, 100)

        result = _check_celery_beat()

        assert result["status"] == "up", result
        assert "3" in result["detail"], result  # the enabled-entry count

    def test_grace_is_three_periods_not_a_fixed_300s(self):
        """A healthy daily entry is hours 'stale' against the old fixed
        threshold — the grace must scale with the entry's own period, in both
        directions."""
        _task("purge-retention", 86400, 2 * 3600)  # 2h into a 24h period
        assert _check_celery_beat()["status"] == "up"

    def test_just_past_three_periods_flags(self):
        period, grace = 600, 3 * 600
        _task("cleanup-stuck-jobs", period, grace + 5)
        assert _check_celery_beat()["status"] == "degraded"

    def test_just_under_three_periods_stays_up(self):
        _task("cleanup-stuck-jobs", 600, 3 * 600 - 30)
        assert _check_celery_beat()["status"] == "up"

    def test_a_disabled_entry_is_never_judged(self):
        """The audit's entry was enabled; a paused one (enabled=False) is not
        beat's promise and must not degrade the check."""
        _task("dispatch-pending-callbacks", 30, 20)
        _task("purge-retention", 86400, 19 * 86400, enabled=False)

        result = _check_celery_beat()

        assert result["status"] == "up", result
        assert "purge-retention" not in result["detail"], result

    def test_an_entry_that_never_ran_is_not_callled_dead(self):
        """A just-installed entry has last_run_at=NULL — there is no clock to
        judge, and beat may legitimately not have reached it yet."""
        _task("dispatch-pending-callbacks", 30, 20)
        PeriodicTask.objects.create(
            name="sweep-workspace-trash",
            task="scraper.retention.sweep_workspace_trash",
            interval=_interval(86400),
            enabled=True,
            last_run_at=None,
        )

        assert _check_celery_beat()["status"] == "up"

    def test_no_enabled_entries_still_reads_unknown(self):
        _task("dispatch-pending-callbacks", 30, 20, enabled=False)

        result = _check_celery_beat()

        assert result["status"] == "unknown", result
        assert "No scheduled tasks" in result["detail"], result

    def test_every_enabled_entry_stale_degrades_with_all_names(self):
        """Beat down entirely (not one dead entry): every entry is past its
        grace, so the detail carries the whole suspect list."""
        _task("dispatch-pending-callbacks", 30, 5 * 3600)
        _task("cleanup-stuck-jobs", 300, 5 * 3600)

        result = _check_celery_beat()

        assert result["status"] == "degraded", result
        assert "dispatch-pending-callbacks" in result["detail"], result
        assert "cleanup-stuck-jobs" in result["detail"], result

    def test_detail_stays_bounded_when_many_entries_are_stale(self):
        for i in range(12):
            _task(f"stale-entry-{i}", 30, 5 * 3600)

        result = _check_celery_beat()

        assert result["status"] == "degraded", result
        assert len(result["detail"]) <= 200, result


def _frozen_now(monkeypatch):
    """Freeze the clock the health check reads, and return the instant.

    The patch is the point: the pins below plant ``last_run_at`` relative to
    FROZEN, so the judged AGE is exact whatever time of day the suite runs at
    (the round-29 pin planted ``dj_tz.now() - 2h`` and failed whenever it
    executed in the last 40 minutes of a daily crontab's window)."""
    from datetime import datetime
    from datetime import timezone as _tz

    from scraper import views as scraper_views

    frozen = datetime(2026, 1, 1, 0, 14, tzinfo=_tz.utc)
    monkeypatch.setattr(scraper_views.timezone, "now", lambda: frozen)
    return frozen


def _frozen_period(sched, monkeypatch) -> float:
    """``_beat_period_seconds`` under the frozen clock. The frozen ``now`` is
    what the shipped code fed back in as ``last_run_at``; the fixed code
    derives its due points from the schedule's own clock, so the pin bites on
    the slice-vs-period distinction, not on the time of day."""
    from datetime import datetime
    from datetime import timezone as _tz

    from scraper import views as scraper_views
    from scraper.views import _beat_period_seconds

    frozen = datetime(2026, 1, 1, 0, 14, tzinfo=_tz.utc)
    monkeypatch.setattr(scraper_views.timezone, "now", lambda: frozen)
    return _beat_period_seconds(sched, frozen)


class TestCrontabArm:
    """Every audit entry that ever died here was a CrontabSchedule
    (``crontab(hour=3, minute=17)`` — the purge, silent 19 days), not an
    interval. The crontab arm derives its period from two consecutive due
    points on the schedule (round-30 GAP 1 — the remaining slice it used
    before read a healthy daily entry as overdue for most of its day), which
    is the subtlest math in the check and the arm a refactor breaks while the
    interval pins above stay green."""

    def _cron_task(self, name: str, last_ago_s: float | None) -> PeriodicTask:
        cron = CrontabSchedule.objects.create(minute="17", hour="3")
        last = (
            dj_tz.now() - timedelta(seconds=last_ago_s)
            if last_ago_s is not None else None
        )
        return PeriodicTask.objects.create(
            name=name,
            task=f"scraper.tasks.{name}",
            crontab=cron,
            enabled=True,
            last_run_at=last,
        )

    def test_a_dead_daily_crontab_degrades_among_fresh_interval_peers(self):
        """The audit shape verbatim: a daily crontab silent 19 days hiding
        behind 30s/120s/300s sweeps that kept firing."""
        _task("dispatch-pending-callbacks", 30, 20)
        _task("resume-browser-unavailable", 120, 60)
        _task("cleanup-stuck-jobs", 300, 100)
        dead = self._cron_task("purge-retention-cron", 19 * 86400)

        result = _check_celery_beat()

        assert result["status"] == "degraded", result
        assert dead.name in result["detail"], result

    def test_a_daily_crontab_most_of_the_way_through_its_period_is_not_stale(
        self, monkeypatch
    ):
        """[round-30 GAP 1] The false-flag direction, on a frozen clock. The
        critic measured it live: at 02:22 UTC a HEALTHY ``crontab(3:17)``
        entry with ``last_run_at`` = the most recent 03:17 read
        ``period 0.91h`` (the remaining slice), grace 2.72h, age 23.09h —
        DEGRADED against a true 24h period, i.e. every crontab entry read
        degraded through the last quarter of its own period. Here the same
        entry sits ~21h into its day and must read up."""
        frozen = _frozen_now(monkeypatch)
        _task("dispatch-pending-callbacks", 30, 20)
        cron = CrontabSchedule.objects.create(minute="17", hour="3")
        PeriodicTask.objects.create(
            name="purge-retention-cron",
            task="scraper.tasks.purge-retention-cron",
            crontab=cron,
            enabled=True,
            last_run_at=frozen - timedelta(hours=21),
        )

        assert _check_celery_beat()["status"] == "up"


class TestCrontabPeriodIsThePeriod:
    """[round-30 GAP 1] ``_beat_period_seconds`` must return one PERIOD of
    the schedule, not the remaining slice to the next due point.

    The slice is what the shipped code measured, and it reads a healthy
    daily entry as 'overdue' for most of its day: grace collapses to
    3 × slice, so the /health/ tile false-alarms nightly — the light lies in
    the opposite direction from the audit's H7 and trains the operator to
    ignore exactly the dead-entry alarm this check exists to raise."""

    def test_a_quarter_hour_crontab_reads_its_true_period(self, monkeypatch):
        """900s for ``*/15`` — not the seconds left before the next :15."""
        from django_celery_beat.models import CrontabSchedule as Cron

        period = _frozen_period(Cron(minute="*/15").schedule, monkeypatch)

        # The upper bound is the point: the overdue fallback collapsing to a
        # constant (or a negative estimate leaking through) must not sneak
        # back as a period — 3600 is the widest a */15 lane can read.
        assert 895.0 <= period <= 3610.0, period

    def test_a_daily_crontab_reads_24_hours(self, monkeypatch):
        from django_celery_beat.models import CrontabSchedule as Cron

        period = _frozen_period(Cron(minute="17", hour="3").schedule, monkeypatch)

        assert 86390.0 <= period <= 86410.0, period

    def test_a_quarter_hour_entry_93_percent_through_its_period_is_not_stale(
        self, monkeypatch
    ):
        """The false-alarm the critic measured end-to-end: 14 of 15 minutes
        gone is 93% of the period — the shipped slice read period 60s, grace
        180s, and degraded a healthy fleet."""
        frozen = _frozen_now(monkeypatch)
        cron = CrontabSchedule.objects.create(minute="*/15")
        PeriodicTask.objects.create(
            name="frequent-sweep",
            task="scraper.tasks.frequent-sweep",
            crontab=cron,
            enabled=True,
            last_run_at=frozen - timedelta(minutes=14),
        )

        assert _check_celery_beat()["status"] == "up"

    def test_the_same_quarter_hour_entry_dead_19_days_is_stale(self, monkeypatch):
        """Same frozen clock, dead entry: the alarm the fix exists to raise
        must survive the corrected period — 19 days against a 45-minute
        grace is still stale, and the detail still names it."""
        frozen = _frozen_now(monkeypatch)
        cron = CrontabSchedule.objects.create(minute="*/15")
        dead = PeriodicTask.objects.create(
            name="frequent-sweep",
            task="scraper.tasks.frequent-sweep",
            crontab=cron,
            enabled=True,
            last_run_at=frozen - timedelta(days=19),
        )

        result = _check_celery_beat()

        assert result["status"] == "degraded", result
        assert dead.name in result["detail"], result
