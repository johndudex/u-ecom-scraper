"""[jobs-79/80] A watchdog re-drive must not destroy the draft it is re-driving.

Prod evidence (2026-08-31): jobs 79 (pt-pandora-net) and 80 (renttherunway-com)
each produced a draft that RAN and passed cycle-2 testing (real run_scraper
output, output_*.json on disk). The writer then hung on an un-cancellable LLM
socket read; the watchdog reaped the task ~90 min later and re-queued the SAME
job. setup_workspace — seeing a first-time site, no skip_* flags — wiped the
workspace including the draft. The re-entered code_writer no-op'd (text-only
reply, zero tool calls — "healthy" as far as the graph could tell), the tester
burned all 3 cycles CRASHing on the missing file, and both jobs died
"cascade exhausted" holding a validated draft an hour earlier.

Fixes pinned here:
- A  setup_workspace preserves ``scraper_draft.py`` from the stale-artifact
     wipe when its mtime >= THIS job's created_at (live work, not cross-run
     residue). Older drafts (prior jobs, user full re-runs) still wipe, and
     discovered_urls_checkpoint.json is still never protected (H3).
- B1 code_writer snapshots every completed draft to the per-job FM key
     ``scrapers/{slug}/jobs/scraper-draft-{job_id}.py``, and setup_workspace
     restores from it when the local draft is gone anyway (worker volume
     recycled). The key is per-job, so no cross-job inheritance is possible.
- B2 an ALIVE writer invocation that produced no draft takes the same
     bounce-to-scraper_analyzer / escalate path as a dead one — and under
     skip_approvals the second failure goes to CLEANUP, not the
     auto-approving human_approval loop.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_job79_watchdog_redrive_draft.py -q
"""
from __future__ import annotations

import os
import re
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

URL = "https://www.renttherunway.com/shop/designers/amur/belle_midi_dress"
SLUG = "renttherunway-com"
DRAFT = b"#!/usr/bin/env python3\n# draft\n"


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


def _state(job_id, **extra):
    state = {"site_slug": SLUG, "job_id": job_id, "input_mode": "list_page", "url": URL}
    state.update(extra)
    return state


def _workspace(tmp_path):
    ws = tmp_path / "workspace" / SLUG
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _run_setup(tmp_path, state):
    from django.test.utils import override_settings

    from agents.nodes.setup_workspace import setup_workspace

    with override_settings(PROJECT_ROOT=str(tmp_path)):
        setup_workspace(state)


# ─── A: the wipe spares this job's live draft, and only that ─────────────────


class TestRedrivePreservesDraft:
    def test_draft_written_by_this_job_survives_wipe(self, tmp_path):
        jid = _make_job(minutes_ago=120)
        ws = _workspace(tmp_path)
        (ws / "scraper_draft.py").write_bytes(DRAFT)  # mtime = now > created_at
        (ws / "stale_debris.txt").write_text("from a prior run")

        _run_setup(tmp_path, _state(jid))

        assert (ws / "scraper_draft.py").read_bytes() == DRAFT
        assert not (ws / "stale_debris.txt").exists(), (
            "the stale-artifact wipe must keep working for genuinely old files"
        )

    def test_draft_from_before_this_job_is_still_wiped(self, tmp_path):
        jid = _make_job(minutes_ago=1)
        ws = _workspace(tmp_path)
        draft = ws / "scraper_draft.py"
        draft.write_bytes(DRAFT)
        old = time.time() - 3 * 3600  # written 3h ago: a PRIOR job's residue
        os.utime(draft, (old, old))

        _run_setup(tmp_path, _state(jid))

        assert not draft.exists(), (
            "a draft older than this job must wipe — a user full re-run gets a "
            "fresh regeneration, not the previous job's code"
        )

    def test_discovery_checkpoint_never_survives_even_when_fresh(self, tmp_path):
        jid = _make_job(minutes_ago=120)
        ws = _workspace(tmp_path)
        (ws / "scraper_draft.py").write_bytes(DRAFT)
        (ws / "discovered_urls_checkpoint.json").write_text('{"urls": ["x"]}')

        _run_setup(tmp_path, _state(jid))

        assert not (ws / "discovered_urls_checkpoint.json").exists(), (
            "H3: a surviving checkpoint makes extraction skip discovery and "
            "ship the tester's sample — it must stay wiped on every setup"
        )


# ─── B1: per-job FM draft archive restores a recycled volume ─────────────────


class TestPerJobDraftRestore:
    def test_restores_this_jobs_archived_draft(self, tmp_path, monkeypatch):
        import src.artifacts as artifacts

        jid = _make_job(minutes_ago=120)
        ws = _workspace(tmp_path)  # no draft on disk — volume was recycled
        seen = {}

        def fake_exists(key):
            seen["key"] = key
            return str(jid) in str(key) and "scraper-draft-" in str(key)

        monkeypatch.setattr(artifacts, "exists", fake_exists)
        monkeypatch.setattr(artifacts, "read", lambda key: DRAFT)

        _run_setup(tmp_path, _state(jid))

        assert "scraper-draft-" in seen.get("key", ""), (
            "the restore must consult THIS job's per-job draft key"
        )
        assert (ws / "scraper_draft.py").read_bytes() == DRAFT

    def test_no_archive_no_crash_no_cross_job_draft(self, tmp_path, monkeypatch):
        import src.artifacts as artifacts

        jid = _make_job(minutes_ago=120)
        ws = _workspace(tmp_path)
        monkeypatch.setattr(artifacts, "exists", lambda key: False)

        _run_setup(tmp_path, _state(jid))

        assert not (ws / "scraper_draft.py").exists()


# ─── B1/B2: writer-side pins (snapshot + no-draft-as-failure) ────────────────


class TestWriterNodePins:
    def _writer_tail(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        start = src.index("_persist_agent_logs(state, result, \"code-writer\", config)")
        end = src.index("if _cw_dead:", start)
        return src[start:end]

    def test_completed_draft_snapshots_to_per_job_fm_key(self):
        tail = self._writer_tail()
        assert "scraper-draft-{job_id}.py" in tail, (
            "code_writer must snapshot every completed draft to "
            "scrapers/{slug}/jobs/scraper-draft-{job_id}.py — cleanup (the only "
            "previous writer-artifact persistence) never runs for a wedged job"
        )

    def test_alive_noop_invocation_takes_the_failure_path(self):
        # [job-83 wave-11 refined the belt] The branch condition is now
        # `if not _draft_ok:` where `_draft_ok` STARTS as the file check —
        # the job-79 contract (no-draft branch keys off the FILE, not the
        # invocation's liveness) is preserved — and a dead invocation's draft
        # must additionally ast.parse (the partial-draft floor).
        tail = self._writer_tail()
        m = re.search(r"_draft_ok = os\.path\.isfile\(_draft_path\)", tail)
        assert m, (
            "the no-draft gate must still START from the FILE check — job "
            "79/80's writer returned text-only (no tool calls), the agent "
            "loop ended 'healthy', and the tester burned 3 cycles on a "
            "missing file"
        )
        assert re.search(r"if not _draft_ok:", tail), (
            "the no-draft branch must consume the _draft_ok gate"
        )
        assert "invocation returned no draft" in tail

    def test_second_failure_under_skip_approvals_goes_to_cleanup(self):
        tail = self._writer_tail()
        cleanup_arm = re.search(
            r"_err_count \+ 1 >= 2:.*?goto=\"cleanup\"", tail, re.S
        )
        assert cleanup_arm, (
            "skip_approvals jobs must cleanup on the second no-draft failure — "
            "human_approval auto-approves 'Retry code generation' and the "
            "writer no-ops again, an unbounded auto-approve loop"
        )
        assert "skip_approvals" in cleanup_arm.group(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
