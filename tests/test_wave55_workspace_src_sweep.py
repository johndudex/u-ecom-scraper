"""[wave-55 C1] setup_workspace sweeps a workspace/src/ shadow it did not write.

Prod evidence (audit 2026-10-05, C1): job 1156 minted ``workspace/src/`` on
2026-10-01 and it was still live on 2026-10-05 — Railway mounts
``/app/workspace`` on a PERSISTENT volume (celery-worker-volume AND
celery-events-volume), so nothing in the pipeline ever removed it. Nothing in
repo history creates it either: ``git log --all -S"workspace/src"`` is empty.
The stale-artifact wipe only clears ``workspace/{slug}/``, so the shadow sat
outside every cleanup path and poisoned every draft for 4.5 days.

Contract: when ``workspace/src/`` exists and no file in it has an mtime at or
after THIS job's created_at, it is not this job's work — rmtree it before the
job dir is staged. A shadow written by the live job (or an unjudgeable case —
no job timestamp) is left alone. tmp_path fixtures only: the sweep must never
be exercised against the real workspace tree.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

pytestmark = [pytest.mark.django_db]

SLUG = "backmarket-de"
URL = "https://www.backmarket.de/en-de/laptops"
SHADOW_FILES = ("__init__.py", "http_fetch.py", "listing_discovery.py", "page_analysis.py")


def _make_job(minutes_ago):
    from django.utils import timezone

    from scraper import models

    job = models.ScrapeJob.objects.create(
        url=URL, status=models.ScrapeJob.STATUS_RUNNING, input_mode="list_page"
    )
    models.ScrapeJob.objects.filter(pk=job.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes_ago)
    )
    return job.pk


def _plant_shadow(root, mtime=None):
    """The prod shape: a 4-module workspace/src/ clone, no discovery.py."""
    shadow = root / "workspace" / "src"
    shadow.mkdir(parents=True)
    for name in SHADOW_FILES:
        path = shadow / name
        path.write_text("# workspace-local build for abiinteriors-com-au\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
    return shadow


def _run_setup(tmp_path, state):
    from django.test.utils import override_settings

    from agents.nodes.setup_workspace import setup_workspace

    with override_settings(PROJECT_ROOT=str(tmp_path)):
        setup_workspace(state)


class TestShadowSweep:
    def test_stale_shadow_is_removed_on_setup(self, tmp_path):
        jid = _make_job(minutes_ago=120)
        shadow = _plant_shadow(tmp_path, mtime=time.time() - 3 * 3600)

        _run_setup(tmp_path, {"site_slug": SLUG, "job_id": jid, "input_mode": "list_page", "url": URL})

        assert not shadow.exists(), (
            "a workspace/src/ older than this job is cross-run residue on a "
            "persistent volume — it must not survive into this run"
        )

    def test_sweep_runs_before_the_job_dir_is_staged(self, tmp_path):
        """The sweep belongs to setup: a draft the job writes right after
        staging must never find the shadow on its import path."""
        jid = _make_job(minutes_ago=120)
        _plant_shadow(tmp_path, mtime=time.time() - 3 * 3600)

        _run_setup(tmp_path, {"site_slug": SLUG, "job_id": jid, "input_mode": "list_page", "url": URL})

        assert not (tmp_path / "workspace" / "src").exists()

    def test_shadow_written_by_this_job_is_kept(self, tmp_path):
        """A file at/after created_at is live work (the same 60s grace the
        draft guard uses) — the sweep must not eat a concurrent job's write."""
        jid = _make_job(minutes_ago=120)
        shadow = _plant_shadow(tmp_path, mtime=time.time())

        _run_setup(tmp_path, {"site_slug": SLUG, "job_id": jid, "input_mode": "list_page", "url": URL})

        assert shadow.exists()

    def test_mixed_shadow_with_one_fresh_file_is_kept(self, tmp_path):
        """One file this job could have written means the tree is live —
        deleting it would destroy in-flight work."""
        jid = _make_job(minutes_ago=120)
        shadow = _plant_shadow(tmp_path, mtime=time.time() - 3 * 3600)
        os.utime(shadow / "http_fetch.py", (time.time(), time.time()))

        _run_setup(tmp_path, {"site_slug": SLUG, "job_id": jid, "input_mode": "list_page", "url": URL})

        assert shadow.exists()

    def test_no_shadow_is_a_noop(self, tmp_path):
        jid = _make_job(minutes_ago=120)
        _run_setup(tmp_path, {"site_slug": SLUG, "job_id": jid, "input_mode": "list_page", "url": URL})
        assert not (tmp_path / "workspace" / "src").exists()

    def test_no_job_timestamp_falls_open(self, tmp_path, caplog):
        """Without a created_at to judge against, the sweep must not guess."""
        _plant_shadow(tmp_path, mtime=time.time() - 3 * 3600)
        _run_setup(tmp_path, {"site_slug": SLUG, "job_id": None, "input_mode": "list_page", "url": URL})
        assert (tmp_path / "workspace" / "src").exists()


class TestRedeployComment:
    def test_no_redeploy_wipes_the_filesystem_claim(self):
        """The jobs-79/80 comment claimed Railway redeploys wipe the ephemeral
        FS. Audit-proven false for workspace/: /app/workspace is a persistent
        volume on both celery-worker and celery-events, and the shadow written
        2026-10-01 was still live across the 2026-10-04 deploy. A guard
        calibrated on a wipe that never happens stops anyone from reasoning
        about what survives a deploy."""
        with open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "setup_workspace.py"),
            encoding="utf-8",
        ) as fh:
            src = fh.read()
        assert "Railway redeploys wipe the ephemeral FS" not in src
        assert "persistent volume" in src
