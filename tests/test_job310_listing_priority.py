"""Job-310 (pillowtalk e2e) regressions: execution must not force a listing
URL the draft cannot discover, and finalize must not bless a 0-item output.

What happened (job 310): the navigator promoted /collections/bestsellers/ as
discovery.listing_url, and run_execution's F7 chain + SCRAPER_LISTING_URL env
gate forced it onto a draft whose discovery selector only matched search.php
product cards (collections cards carry no data-product-id anchors). Fresh
discovery found 0 URLs, execution "succeeded" with an empty output, and the
finalize catch-all blessed the job COMPLETED with 0 products.

Fixes pinned here:
- list_page: the job URL (the user-provided listing, by definition) outranks
  the navigator's promotion in BOTH the --listing-url chain and the env gate
  (run_execution.py);
- finalize: an output file that parses but holds 0 records → FAILED, not
  COMPLETED (tasks.py `_output_file_has_zero_items`);
- code-writer prompt: zero-yield discovery must retry DEFAULT_LISTING_URL.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_job310_listing_priority.py -q
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))


class TestListPageListingPriority:
    """The job URL outranks discovery.listing_url for list_page jobs."""

    def _src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")) as f:
            return f.read()

    def test_cli_chain_puts_job_listing_first(self):
        src = self._src()
        assert "_job_listing" in src, "list_page job-URL priority missing"
        # first candidate in the --listing-url chain, before discovery.listing_url
        assert src.index("_candidates = [") < src.index('_disc.get("listing_url")')
        assert "_job_listing,\n" in src.split("_candidates = [", 1)[1][:120]

    def test_env_gate_puts_job_listing_first(self):
        src = self._src()
        assert '_env_candidate = _job_listing or ""' in src, (
            "SCRAPER_LISTING_URL env gate must prefer the list_page job URL"
        )

    def test_navigation_mode_unaffected(self):
        """The priority is scoped to list_page — navigation jobs keep the
        navigator's discovery.listing_url first."""
        src = self._src()
        i = src.find('_job_listing = ""')
        j = src.find("if input_mode == \"list_page\":", i)
        cond = src[i : j + 200]
        assert 'input_mode == "list_page"' in cond


class TestZeroItemFinalizeGate:
    """`_output_file_has_zero_items` — behavior + the finalize ladder pin."""

    def _helper(self):
        import importlib

        sys.modules.pop("scraper.tasks", None)
        tasks = importlib.import_module("scraper.tasks")
        return tasks._output_file_has_zero_items

    def _write(self, tmp_path, name, payload):
        p = tmp_path / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_empty_products_list_is_zero(self, tmp_path):
        p = self._write(tmp_path, "out.json", {"site": {}, "products": [], "metadata": {}})
        assert self._helper()(p) is True

    def test_populated_products_is_not_zero(self, tmp_path):
        p = self._write(
            tmp_path, "out.json",
            {"products": [{"title": "Pillow", "price": "$9"}]},
        )
        assert self._helper()(p) is False

    def test_any_content_type_counts(self, tmp_path):
        p = self._write(tmp_path, "out.json", {"jobs": [{"title": "RN", "company": "X"}]})
        assert self._helper()(p) is False

    def test_bare_top_level_array(self, tmp_path):
        p = self._write(tmp_path, "out.json", [{"title": "x"}])
        assert self._helper()(p) is False
        p2 = self._write(tmp_path, "out2.json", [])
        assert self._helper()(p2) is True

    def test_metadata_only_dict_is_zero(self, tmp_path):
        p = self._write(tmp_path, "out.json", {"site": {}, "metadata": {"duration": 3}})
        assert self._helper()(p) is True

    def test_missing_or_unreadable_is_conservative_false(self, tmp_path):
        h = self._helper()
        assert h(str(tmp_path / "nope.json")) is False
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert h(str(bad)) is False
        assert h("") is False

    def test_finalize_ladder_pins_the_gate(self):
        with open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")) as f:
            src = f.read()
        assert "_output_file_has_zero_items(job.output_file)" in src, (
            "finalize ladder must gate COMPLETED on the zero-item check"
        )
        assert "Execution produced 0 items" in src


class TestCodeWriterSelfHealPrompt:
    """Defense-in-depth: zero-yield discovery must retry DEFAULT_LISTING_URL."""

    def test_prompt_rule_present(self):
        with open(os.path.join(ROOT, ".opencode", "agents", "code-writer.md")) as f:
            src = f.read()
        assert "Zero-yield discovery must self-heal" in src
        assert "DEFAULT_LISTING_URL" in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
