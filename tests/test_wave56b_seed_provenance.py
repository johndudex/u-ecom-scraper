"""[wave-56b F10] Seed provenance: the url_list payload execution measures is
the JOB's seed — never a stale ``input_urls.json`` left in the scraper's
directory.

Live proof (job 592, 2026-10-09): a resumed url_list job whose submitted
seed was ``catalogue/category/books/travel_2`` scraped three homepage books
out of a Sep-29 ``scrapers/books-toscrape-com/input_urls.json`` instead.
``_stage_extra_files`` stages the seed from ``dirname(scraper_path)``; a
resumed job's ``scraper_path`` points at the production dir, where cleanup
rewrites the file AFTER the run (23s post-run on 592) — so every re-scrape
silently measures whatever the last job left behind, and can PASS while
extracting a payload the user never submitted. Same family as audit C2
(job 1227), one branch over.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

JOB_SEED = [
    "https://books-toscrape.com/catalogue/category/books/travel_2/index.html",
]
STALE_SEED = ["https://books-toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"]


def _scraper_with_stale_seed(tmp_path):
    """A resumed job's shape: the draft lives in the production dir beside a
    seed file an earlier job wrote."""
    d = tmp_path / "books-toscrape-com"
    d.mkdir()
    scraper = d / "scraper_draft.py"
    scraper.write_text("# draft\n", encoding="utf-8")
    seed_file = d / "input_urls.json"
    seed_file.write_text(json.dumps({"urls": STALE_SEED}), encoding="utf-8")
    return str(scraper)


class TestStageSeedProvenance:
    def test_stale_seed_file_never_stages_for_url_list(self, tmp_path):
        from agents.nodes.run_execution import _stage_extra_files

        scraper = _scraper_with_stale_seed(tmp_path)
        extra = _stage_extra_files(scraper, "url_list", seed_urls=JOB_SEED)
        staged = json.loads(extra["input_urls.json"])
        assert staged["urls"] == JOB_SEED, (
            f"staged seed is not the job's payload: {staged['urls']}"
        )

    def test_seed_heal_rewrites_the_stale_file(self, tmp_path):
        """Both lanes read input_urls.json from dirname(scraper_path) — the
        heal must land in the file itself, so the in-process subprocess (no
        staging, direct file read) measures the job seed too."""
        from agents.nodes.run_execution import _heal_url_list_seed

        scraper = _scraper_with_stale_seed(tmp_path)
        _heal_url_list_seed(scraper, "url_list", JOB_SEED)
        with open(os.path.join(os.path.dirname(scraper), "input_urls.json"),
                  encoding="utf-8") as fh:
            assert json.load(fh)["urls"] == JOB_SEED

    def test_nav_modes_still_stage_the_on_disk_carrier(self, tmp_path):
        """For navigation families input_urls.json is the persisted-discovery
        carrier the tester wrote — staging stays file-based there."""
        from agents.nodes.run_execution import _stage_extra_files

        scraper = _scraper_with_stale_seed(tmp_path)
        extra = _stage_extra_files(scraper, "navigation")
        assert json.loads(extra["input_urls.json"])["urls"] == STALE_SEED

    def test_url_list_without_job_seed_falls_back_to_disk(self, tmp_path):
        """No seed on state → no guess (the C2 rule): stage what's on disk;
        the scope gate still judges rows against whatever the job declared."""
        from agents.nodes.run_execution import _stage_extra_files

        scraper = _scraper_with_stale_seed(tmp_path)
        extra = _stage_extra_files(scraper, "url_list")
        assert json.loads(extra["input_urls.json"])["urls"] == STALE_SEED


class TestSeedStamp:
    def test_seed_stamp_lands_in_output_metadata(self, tmp_path):
        from agents.nodes.run_execution import _stamp_seed_provenance

        out = str(tmp_path / "output_x.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"products": [{"title": "t", "url": JOB_SEED[0]}],
                       "metadata": {"phase": "extract"}}, fh)
        assert _stamp_seed_provenance(out, JOB_SEED, "url_list") is True
        with open(out, encoding="utf-8") as fh:
            meta = json.load(fh)["metadata"]
        assert meta["seed_urls_used"] == JOB_SEED
        assert meta["phase"] == "extract"

    def test_stamp_is_url_list_only_and_never_edits_rows(self, tmp_path):
        from agents.nodes.run_execution import _stamp_seed_provenance

        out = str(tmp_path / "output_y.json")
        rows = [{"title": "t"}]
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"products": rows}, fh)
        assert _stamp_seed_provenance(out, JOB_SEED, "navigation") is False
        with open(out, encoding="utf-8") as fh:
            assert json.load(fh)["products"] == rows
