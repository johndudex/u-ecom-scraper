"""Full re-run button regressions.

What prompted this (prod job 24, 2026-08-29): a worker redeploy killed the job
mid-code-generation and the only recovery was "Re-run" — which routes through
check_tracker's SELECTIVE rescrape. That diff skips site/product/code analysis
whenever a prior COMPLETED job for the URL has an identical config, and
setup_workspace re-hydrates the archived artifacts any True flag names. Two
problems: (a) there was no one-click way to force a clean regeneration (e.g.
after switching the agent model, or when stale artifacts are suspected — the
job-12 poison class), and (b) for a killed run the archive holds debris from a
run that never finished.

Fix pinned here:
- state: ``force_full`` travels job_restart (POST ``force_full=1`` / GET
  ``?full=1``) → run_scrape_task → _run_graph_job → initial state.
- check_tracker: inside the rescrape gate, force_full wipes the workspace AND
  the analysis archive (output_*.json kept) and forces ALL skip flags False —
  even for a completed twin the diff would normally reuse.
- The selective path is untouched without the flag.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_full_rerun.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

pytestmark = [pytest.mark.django_db]


URL = "https://full-rerun.example/products/widget"


def _make_site_and_prior_job(tmp_path):
    """Site marked complete + a prior COMPLETED job with an IDENTICAL config
    (the case selective rescrape would skip everything for). check_tracker's
    _get_project_root reads settings.PROJECT_ROOT dynamically, so the returned
    override_settings ctx is all the isolation needed."""
    from django.test.utils import override_settings
    from scraper import models

    models.Site.objects.create(url=URL, slug="full-rerun-example", status="complete")
    models.ScrapeJob.objects.create(
        url=URL,
        status=models.ScrapeJob.STATUS_COMPLETED,
        input_mode="list_page",
        target_fields=["price", "img_url"],
    )
    return override_settings(PROJECT_ROOT=str(tmp_path))


def _plant_stale_artifacts(tmp_path):
    """Workspace debris + analysis archive from a KILLED prior run + one real
    output (outputs must survive the wipe)."""
    slug = "full-rerun-example"
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "scraper_draft.py").write_text("# stale draft", encoding="utf-8")
    arch = tmp_path / "scrapers" / slug / "analysis"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "site_analysis.json").write_text('{"stale": true}', encoding="utf-8")
    (arch / "product_analysis.json").write_text('{"stale": true}', encoding="utf-8")
    (tmp_path / "scrapers" / slug / "output_2026-08-29_000000.json").write_text(
        json.dumps({"products": []}), encoding="utf-8"
    )


def _state(**extra):
    base = {
        "job_id": 999,  # NOT the twin's id
        "url": URL,
        "site_slug": "full-rerun-example",
        "sample_only": True,
        "rescrape": True,
        "input_mode": "list_page",
        "target_fields": ["price", "img_url"],
        "search_criteria": "",
        "skip_approvals": True,
    }
    base.update(extra)
    return base


class TestForceFullInCheckTracker:
    def test_force_full_wipes_and_regenerates_despite_completed_twin(self, tmp_path):
        """The core contract: identical completed twin + force_full → NO skip
        flags, archive + workspace wiped, output history kept."""
        from webapp.agents.nodes.check_tracker import check_tracker

        ctx = _make_site_and_prior_job(tmp_path)
        _plant_stale_artifacts(tmp_path)
        with ctx:
            cmd = check_tracker(_state(force_full=True))

        assert cmd.goto == "setup_workspace"
        assert cmd.update["skip_site_analysis"] is False
        assert cmd.update["skip_product_analysis"] is False
        assert cmd.update["skip_code_generation"] is False
        # Stale artifacts gone from BOTH the workspace and the archive.
        assert not (tmp_path / "workspace" / "full-rerun-example" / "scraper_draft.py").exists()
        assert not (tmp_path / "scrapers" / "full-rerun-example" / "analysis" / "site_analysis.json").exists()
        assert not (tmp_path / "scrapers" / "full-rerun-example" / "analysis" / "product_analysis.json").exists()
        # Output history preserved.
        assert (tmp_path / "scrapers" / "full-rerun-example" / "output_2026-08-29_000000.json").exists()

    def test_no_force_full_still_skips_selectively(self, tmp_path):
        """Without the flag the selective diff is untouched: identical twin →
        everything skipped, archive preserved for re-hydration."""
        from webapp.agents.nodes.check_tracker import check_tracker

        ctx = _make_site_and_prior_job(tmp_path)
        _plant_stale_artifacts(tmp_path)
        with ctx:
            cmd = check_tracker(_state())

        assert cmd.goto == "setup_workspace"
        assert cmd.update["skip_site_analysis"] is True
        assert cmd.update["skip_product_analysis"] is True
        assert cmd.update["skip_code_generation"] is True
        # Archive NOT wiped (setup_workspace re-hydrates from it).
        assert (tmp_path / "scrapers" / "full-rerun-example" / "analysis" / "site_analysis.json").exists()


class TestForceFullThreading:
    def test_task_state_sets_both_flags(self):
        """_run_graph_job must set rescrape AND force_full on the initial
        state — check_tracker's force_full arm lives inside the rescrape gate,
        so force_full alone would fall through to the re_scrape interrupt."""
        with open(os.path.join(ROOT, "webapp", "scraper", "tasks.py"), encoding="utf-8") as fh:
            src = fh.read()
        i = src.find("if force_full:")
        assert i != -1, "force_full block missing from _run_graph_job"
        block = src[i:i + 300]
        assert 'initial_state["rescrape"] = True' in block
        assert 'initial_state["force_full"] = True' in block

    def test_view_accepts_post_and_get_flag(self):
        """job_restart reads force_full from the intake checkbox POST
        (force_full=1) AND the job_detail link (?full=1)."""
        with open(os.path.join(ROOT, "webapp", "scraper", "views.py"), encoding="utf-8") as fh:
            src = fh.read()
        assert 'request.POST.get("force_full") == "1"' in src
        assert 'request.GET.get("full") == "1"' in src
        assert src.count("rescrape=True, force_full=force_full") == 1

    def test_state_declares_force_full(self):
        with open(os.path.join(ROOT, "webapp", "agents", "state.py"), encoding="utf-8") as fh:
            src = fh.read()
        assert "force_full: bool" in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
