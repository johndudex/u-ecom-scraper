"""[wave-55 C5 + H13] Retention anchors on the dead end, and the trash sweep
runs where the tombstones are.

C5 (audit #23): the one effective prod purge (2026-10-05 03:17Z) destroyed
481 jobs' evidence because ``_expired_page`` keyed ``created_at__lt`` — a job
that sat parked/pending for weeks and then failed yesterday was already past
the line at birth. The 7-day window is "after the dead end", so it must key
``completed_at``, falling back to ``created_at`` only for the rows that never
got a terminal stamp (the UI-cancel path leaves ``completed_at`` NULL).

H13 (audit #24): ``_sweep_workspace_trash`` ran inside the task routed to the
celery-events pool, whose ``/app/workspace`` is a frozen 2026-08-25 snapshot —
the tombstones are written by celery-worker, so ``trash_entries_purged`` was
structurally 0 (wave-40 T11 dead code in prod). The sweep is its own beat task
now, routed to the worker's queue.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_retention_anchor_and_trash.py -q"
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from scraper import retention  # noqa: E402
from scraper.models import ScrapeJob  # noqa: E402

pytestmark = pytest.mark.django_db

TASK_NAME = "scraper.retention.sweep_workspace_trash"
WORKER_QUEUE = "celery"  # celery-worker runs without -Q → the default queue


def _compose_service(src: str, name: str) -> str:
    """The compose block for one top-level service (header to the next)."""
    chunks = re.split(r"^  (?=[\w-]+:)", src, flags=re.MULTILINE)
    for chunk in chunks:
        if chunk.startswith(f"{name}:"):
            return chunk
    return ""


def _job(status: str, created_days_ago: float, completed_days_ago: float | None) -> ScrapeJob:
    """A dead-end row stamped the way the pipeline does: ``created_at`` at
    dispatch, ``completed_at`` at the verdict. ``auto_now_add`` overrides the
    create-time value, so the backdates go through an UPDATE."""
    job = ScrapeJob.objects.create(url="https://a.example/x", status=status)
    stamp = timezone.now() - timedelta(days=created_days_ago)
    done = None if completed_days_ago is None else (
        timezone.now() - timedelta(days=completed_days_ago)
    )
    ScrapeJob.objects.filter(pk=job.pk).update(created_at=stamp, completed_at=done)
    return ScrapeJob.objects.get(pk=job.pk)


# ─── C5: the window is anchored on the dead end ──────────────────────────────


class TestAnchoredOnCompletedAt:
    def test_created_8d_ago_but_failed_1d_ago_is_not_purged(self):
        """The audit's 79-job cohort: parked/pending for a week, failed
        yesterday — fresh evidence, not 8-day-old evidence."""
        _job(ScrapeJob.STATUS_FAILED, created_days_ago=8, completed_days_ago=1)
        assert retention._expired_page(timezone.now(), 7, 90, 0, 500) == []
        report = retention._dry_run_report(timezone.now(), 7, 90)
        assert report["jobs_expired"] == 0

    def test_created_8d_ago_and_failed_8d_ago_is_purged(self):
        job = _job(ScrapeJob.STATUS_FAILED, created_days_ago=8, completed_days_ago=8)
        assert retention._expired_page(timezone.now(), 7, 90, 0, 500) == [job.id]

    def test_cancelled_uses_the_same_completed_anchor(self):
        fresh = _job(ScrapeJob.STATUS_CANCELLED, created_days_ago=30, completed_days_ago=1)
        stale = _job(ScrapeJob.STATUS_CANCELLED, created_days_ago=8, completed_days_ago=8)
        page = retention._expired_page(timezone.now(), 7, 90, 0, 500)
        assert fresh.id not in page
        assert stale.id in page

    def test_null_completed_at_falls_back_to_created_at(self):
        """H10: the UI cancel path leaves ``completed_at`` NULL. Those rows
        still age out — on their creation stamp, never younger than the
        window."""
        stale = _job(ScrapeJob.STATUS_CANCELLED, created_days_ago=8, completed_days_ago=None)
        fresh = _job(ScrapeJob.STATUS_FAILED, created_days_ago=1, completed_days_ago=None)
        page = retention._expired_page(timezone.now(), 7, 90, 0, 500)
        assert stale.id in page
        assert fresh.id not in page

    def test_dry_run_mirror_agrees_with_the_live_page(self):
        """The audit's mirror complaint: two hand-written selectors drift. One
        selector, both consumers."""
        _job(ScrapeJob.STATUS_FAILED, created_days_ago=8, completed_days_ago=1)
        stale = _job(ScrapeJob.STATUS_FAILED, created_days_ago=8, completed_days_ago=8)
        null_done = _job(ScrapeJob.STATUS_CANCELLED, created_days_ago=30, completed_days_ago=None)
        _job(ScrapeJob.STATUS_COMPLETED, created_days_ago=8, completed_days_ago=8)

        page = retention._expired_page(timezone.now(), 7, 90, 0, 500)
        report = retention._dry_run_report(timezone.now(), 7, 90)
        assert report["jobs_expired"] == len(page) == 2
        assert set(page) == {stale.id, null_done.id}

    def test_the_completed_arm_is_not_dragged_to_the_failed_anchor(self):
        """Scope guard: the brief anchors the purgeable-FAILED window only; a
        8-day-old COMPLETED row still sits far inside its 90-day window."""
        _job(ScrapeJob.STATUS_COMPLETED, created_days_ago=8, completed_days_ago=8)
        assert retention._expired_page(timezone.now(), 7, 90, 0, 500) == []

    def test_the_docstring_promises_the_dead_end_anchor(self):
        doc = (retention.__doc__ or "") + (retention._expired_page.__doc__ or "")
        assert "completed_at" in doc, "the window anchor is undocumented"
        assert "created_at" in doc, "the NULL fallback is undocumented"

    def test_the_purge_docstring_hands_the_trash_sweep_off(self):
        """The ``purge_retention`` docstring is what an operator reads for the
        03:17 beat entry. It still promised the tombstone sweep after the
        sweep grew its own beat task — the same comment-drift class the audit
        itemizes (L2 / C1 #25)."""
        from scraper import tasks as scraper_tasks

        doc = scraper_tasks.purge_retention.__doc__ or ""
        assert "_sweep_workspace_trash" not in doc, (
            "purge_retention still claims the workspace tombstone sweep — "
            "that moved to scraper.retention.sweep_workspace_trash")
        assert "sweep-workspace-trash" in doc, (
            "purge_retention must point the reader at the beat entry that "
            "actually sweeps the tombstones")


# ─── H13: the trash sweep runs in the container that writes tombstones ───────


def _plant_trash(tmp_path: Path, entries: dict[str, int]) -> dict[str, Path]:
    trash = tmp_path / "workspace" / "_trash"
    trash.mkdir(parents=True, exist_ok=True)
    now_ns = int(time.time())
    planted = {}
    for name, age_days in entries.items():
        entry = trash / f"{name}-{(now_ns - int(age_days * 86400)) * 10 ** 9}"
        entry.mkdir()
        (entry / "scraper_draft.py").write_text("# tombstoned", encoding="utf-8")
        planted[name] = entry
    return planted


class TestTrashSweepIsItsOwnWorkerTask:
    def test_purge_retention_no_longer_touches_workspace_trash(self, tmp_path, monkeypatch):
        """The DB purge ran on celery-events, whose workspace is a frozen
        2026-08-25 snapshot — the sweep it folded in could never see a
        tombstone the worker wrote. It does not pretend to any more."""
        planted = _plant_trash(tmp_path, {"a-example-999999": 30})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))

        report = retention.purge_retention(days_failed=0, days_completed=0)

        assert "trash_entries_purged" not in report
        assert planted["a-example-999999"].is_dir()

    def test_the_task_sweeps_on_the_same_windows(self, tmp_path, monkeypatch):
        """[wave-40 T11 semantics, preserved] the owner's COMPLETED tombstone
        lives days_completed, anything else days_failed, and a tombstone whose
        job is still resumable is never swept."""
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "RETENTION_DAYS_FAILED", 7)
        monkeypatch.setattr(settings, "RETENTION_DAYS_COMPLETED", 90)
        completed = ScrapeJob.objects.create(
            url="https://a.example/x", status=ScrapeJob.STATUS_COMPLETED)
        running = ScrapeJob.objects.create(
            url="https://b.example/x", status=ScrapeJob.STATUS_RUNNING)
        planted = _plant_trash(tmp_path, {
            f"a-example-{completed.id}": 100,   # past the completed window
            f"a-example-{completed.id}b": 40,   # inside it (unparseable suffix)
            f"b-example-{running.id}": 100,     # resumable sibling — hands off
            "c-example-999999": 30,             # orphan job → failed window
            "d-example-999998": 0,              # fresh
        })

        report = retention.sweep_workspace_trash()

        assert report == {"trash_entries_purged": 2}
        assert not planted[f"a-example-{completed.id}"].exists()
        assert planted[f"a-example-{completed.id}b"].exists(), "unparseable name is never ours"
        assert planted[f"b-example-{running.id}"].exists()
        assert not planted["c-example-999999"].exists()
        assert planted["d-example-999998"].exists()

    def test_the_kill_switch_covers_the_sweep_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "RETENTION_ENABLED", False)
        planted = _plant_trash(tmp_path, {"a-example-999999": 30})

        assert retention.sweep_workspace_trash() == {"disabled": True}
        assert planted["a-example-999999"].is_dir()

    def test_no_trash_dir_is_a_quiet_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        assert retention.sweep_workspace_trash() == {"trash_entries_purged": 0}


# Registered at a worker boot. The events pair is in the same probe as the
# sweep: ``config_from_object(namespace="CELERY")`` *replaces* the
# ``app.conf.imports`` tuple rather than appending to it, so a settings-side
# import add-on silently deregisters partner-event delivery (audit 2-h13).
BOOT_TASK_NAMES = (
    TASK_NAME,
    "scraper.events.dispatcher.deliver_callback",
    "scraper.events.reconciler.dispatch_pending_callbacks",
)

# The registry leg of a worker boot, isolated from this test module — the
# ``from scraper import retention`` above registers the shared_task on
# whatever app is live, which would make the in-process check vacuous.
_BOOT_PROBE = (
    "import os;"
    "os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');"
    "import django;django.setup();"
    "from config.celery import app;"
    "app.loader.import_default_modules();"
    f"missing=[n for n in {BOOT_TASK_NAMES!r} if n not in app.tasks];"
    "print(','.join(missing));"
    "raise SystemExit(1 if missing else 0)"
)


def _boot_env() -> dict:
    """Same import surface the test module gives itself (repo root + webapp)."""
    parts = [ROOT, os.path.join(ROOT, "webapp")]
    if os.environ.get("PYTHONPATH"):
        parts.append(os.environ["PYTHONPATH"])
    return {**os.environ, "PYTHONPATH": os.pathsep.join(parts)}


class TestTrashSweepRouting:
    def test_beat_schedules_the_sweep(self):
        entry = settings.CELERY_BEAT_SCHEDULE.get("sweep-workspace-trash")
        assert entry is not None, "the trash sweep is not scheduled any more"
        assert entry["task"] == TASK_NAME

    def test_the_sweep_lands_on_the_worker_queue(self):
        """celery-worker consumes the default queue (no ``-Q`` in compose);
        celery-events consumes ``-Q events``. The route must name the worker's
        queue — the sweep only ever saw the frozen events snapshot."""
        assert settings.CELERY_TASK_ROUTES.get(TASK_NAME) == WORKER_QUEUE

        compose = Path(ROOT) / "docker-compose.yml"
        if compose.exists():
            src = compose.read_text(encoding="utf-8")
            worker = _compose_service(src, "celery-worker")
            events = _compose_service(src, "celery-events")
            assert worker, "compose no longer declares celery-worker"
            assert not re.search(r"^\s*command:.*-Q\b", worker, re.MULTILINE), (
                "celery-worker now consumes a named queue — the WORKER_QUEUE "
                "pin above is stale")
            assert events and "-Q events" in events

    def test_the_db_purge_stays_on_the_events_pool(self):
        """Moving the filesystem sweep out must not drag the checkpoint DELETE
        batch onto the 3-slot scrape pool (the wave-35 routing rule)."""
        assert settings.CELERY_TASK_ROUTES.get("scraper.tasks.purge_retention") == "events"

    def test_the_worker_imports_the_task_module_at_boot(self):
        """The task lives in scraper.retention, which nothing imported at boot
        before — an unregistered task name is a beat send that no worker can
        run. The pin sits on the one tuple workers actually read
        (``config.celery.app.conf.imports``) and names the events pair too:
        that tuple is *replaced* by config_from_object, so the only safe place
        to add a module is inside it."""
        from config.celery import app

        imports = app.conf.imports
        if isinstance(imports, str):
            imports = tuple(imports.split(","))
        for module in ("scraper.events.dispatcher", "scraper.events.reconciler", "scraper.retention"):
            assert module in tuple(imports), (
                f"{module} is not imported at worker boot — the "
                f"imports tuple is {tuple(imports)!r}")
        assert not getattr(settings, "CELERY_IMPORTS", None), (
            "CELERY_IMPORTS in settings overwrites app.conf.imports instead "
            "of appending to it — the events pair would vanish from every "
            "worker boot and beat's dispatch-pending-callbacks publish into "
            "a void")

    def test_the_worker_resolves_the_task_on_its_own_queue(self):
        """Boot-level, in a clean process: ``import_default_modules`` is the
        worker's own registry step, and it must resolve all three task names
        — the sweep plus the two events tasks it would silently deregister.
        The router leg stays in-process; that is not reachable from a
        subprocess assertion and is not the vacuous one."""
        proc = subprocess.run(
            [sys.executable, "-c", _BOOT_PROBE],
            capture_output=True, text=True, timeout=300, env=_boot_env(),
        )
        assert proc.returncode == 0, (
            "clean-process worker boot did not register: "
            f"{(proc.stdout or '').strip() or (proc.stderr or '').strip()[-400:]}")

        from config.celery import app

        assert app.amqp.router.route({}, TASK_NAME)["queue"].name == WORKER_QUEUE


class TestSettingsWindowFeedsTheTask:
    def test_the_task_reads_the_configured_windows(self, tmp_path, monkeypatch):
        """The task feeds settings.RETENTION_DAYS_* into the sweep, not the
        module defaults — retuning the env cannot leave the sweep on 7/90."""
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "RETENTION_ENABLED", True)
        monkeypatch.setattr(settings, "RETENTION_DAYS_FAILED", 3)
        monkeypatch.setattr(settings, "RETENTION_DAYS_COMPLETED", 5)
        owner = ScrapeJob.objects.create(
            url="https://a.example/x", status=ScrapeJob.STATUS_COMPLETED)
        planted = _plant_trash(tmp_path, {
            f"a-example-{owner.id}": 4,   # past the 3d failed window, inside 5d completed
            f"a-example-{owner.id}2": 8,  # past the 5d completed window
            "b-example-999999": 4,        # orphan → failed window → past it
            "c-example-999998": 1,        # fresh against a 3d window
        })

        assert retention.sweep_workspace_trash() == {"trash_entries_purged": 2}
        assert planted[f"a-example-{owner.id}"].is_dir(), (
            "the owner's tombstone is judged on the completed window")
        assert not planted[f"a-example-{owner.id}2"].exists()
        assert not planted["b-example-999999"].exists()
        assert planted["c-example-999998"].is_dir()


# ─── H7 #22 clause 2: the purge report carries its own beat row ──────────────


class TestPurgeReportCarriesItsBeatRow:
    """Audit remediation #22, second clause — 'log total_run_count in the
    purge report.' The 19-day silence the audit caught stayed invisible
    partly because the 03:17 report never carried what the scheduler itself
    knew about that entry."""

    def _beat_row(self, **kw) -> None:
        from django_celery_beat.models import CrontabSchedule, PeriodicTask

        cron = CrontabSchedule.objects.create(minute="17", hour="3")
        PeriodicTask.objects.create(
            name="purge-retention",
            task="scraper.tasks.purge_retention",
            crontab=cron,
            enabled=True,
            **kw,
        )

    def test_the_report_names_the_entrys_run_count(self):
        self._beat_row(total_run_count=16)

        report = retention.purge_retention(days_failed=0, days_completed=0)

        assert report["beat"]["total_run_count"] == 16
        assert report["beat"]["date_changed"] is not None

    def test_the_dry_run_preview_carries_it_too(self):
        """The operator's preview is the report they read before arming the
        sweep — the same builder feeds it, so it cannot drift from the live
        shape."""
        self._beat_row(total_run_count=16)

        report = retention._dry_run_report(timezone.now(), 0, 0)

        assert report["beat"]["total_run_count"] == 16

    def test_a_missing_row_never_kills_the_report(self):
        """No django_celery_beat row (fresh DB, scheduler not yet synced) must
        not take the purge down with it — the report just omits the numbers."""
        report = retention.purge_retention(days_failed=0, days_completed=0)

        assert report["beat"] == {}

    def test_the_dry_run_command_prints_the_beat_row(self):
        """[round-30 GAP 3] The operator runs the management command, not the
        builder — the exact command the audit describes as the manual
        equivalent. The console preview must carry what the report dict
        does, or the third surface (the Railway console) still hides the
        entry's own run count."""
        import io

        from django.core.management import call_command

        self._beat_row(total_run_count=16)

        out = io.StringIO()
        call_command("purge_retention", stdout=out)

        assert "total_run_count" in out.getvalue(), out.getvalue()


class TestParkedStatusParityPins:
    """[round-32 GAP 1] The two operator-facing enumerations of the parked
    set — the ``purge_retention`` command docstring and ``retention``'s own
    module docstring — drifted once already: migration 0045 added
    ``provider_dead`` in wave-53 and neither docstring followed (HEAD's
    command docstring still enumerated the pre-wave-53 six; retention.py
    never named it at all until the wave-55 rewrite). Round 31 fixed the
    command by hand; these pins make the next status addition redden both
    surfaces instead of drifting them silently again."""

    def test_the_command_docstring_enumerates_every_parked_status(self):
        # The operator surface is the command MODULE's docstring — the class
        # has none of its own (`Command.__doc__` is None; the sketch in the
        # round-32 gap text reads BaseCommand's), and `Command.help` carries
        # only its first line.
        from scraper.management.commands import purge_retention as purge_retention_cmd

        purgeable = {"completed", "failed", "cancelled"}
        parked = {s for s, _ in ScrapeJob.STATUS_CHOICES} - purgeable

        doc = purge_retention_cmd.__doc__ or ""
        for status in sorted(parked):
            assert status in doc, f"purge_retention docstring omits parked status {status}"

    def test_the_retention_docstring_enumerates_every_parked_status(self):
        purgeable = {"completed", "failed", "cancelled"}
        parked = {s for s, _ in ScrapeJob.STATUS_CHOICES} - purgeable

        doc = retention.__doc__ or ""
        for status in sorted(parked):
            assert status in doc, f"retention docstring omits parked status {status}"
