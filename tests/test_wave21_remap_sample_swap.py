"""[wave-21 T7] A mapping-remap must not re-analyze the same dead seed.

Local e2e job 335 (michaelhill): the tester correctly prescribed
``remediation.target = "mapping"`` twice, route_after_testing sent the job
to product_analyzer in re-map mode, and the analyzer dutifully re-ran —
against THE SAME dead sample URL (state.sample_url never changed). Same
404, same guesswork mappings, same draft: the freeze gate fired.

Contract:
- ``_live_sample_url_from_input_urls`` picks a replacement sample URL from
  the job's own discovered ``input_urls.json`` (strings or ``{"url": …}``
  entries), preferring the dead URL's own domain, skipping the dead URL
  itself; ``""`` when nothing usable exists (missing file, only the dead
  URL);
- in ``_invoke_product_analyzer``'s re-map branch, when the PREVIOUS
  analysis carries a dead-seed verdict (T6's ``_dead_seed_verdict``), the
  swap runs and the chosen URL is BOTH analyzed and persisted into graph
  state (``sample_url`` in the remap Command update).
"""
from __future__ import annotations

import importlib
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

graph = importlib.import_module("agents.graph")  # noqa: E402

DEAD = "https://www.example.com/p/dead-signet-ring-18353730.html"
LIVE_A = "https://www.example.com/p/stud-earrings-12336265.html"
LIVE_B = "https://www.example.com/p/huggie-earrings-10984697.html"
FOREIGN = "https://www.other-shop.com/p/some-ring-1.html"


def _write_input_urls(tmp_path, entries):
    ws = tmp_path / "workspace" / "example-com"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "input_urls.json").write_text(json.dumps(entries))


class TestLiveSampleUrlPicker:
    def test_prefers_same_domain_string_entries(self, tmp_path):
        _write_input_urls(tmp_path, [DEAD, LIVE_A, LIVE_B])
        picked = graph._live_sample_url_from_input_urls(
            "example-com", DEAD, project_root=str(tmp_path)
        )
        assert picked == LIVE_A

    def test_dict_entries_with_url_key(self, tmp_path):
        _write_input_urls(tmp_path, [{"url": DEAD}, {"url": LIVE_A}])
        picked = graph._live_sample_url_from_input_urls(
            "example-com", DEAD, project_root=str(tmp_path)
        )
        assert picked == LIVE_A

    def test_all_dead_returns_empty(self, tmp_path):
        _write_input_urls(tmp_path, [DEAD, DEAD])
        picked = graph._live_sample_url_from_input_urls(
            "example-com", DEAD, project_root=str(tmp_path)
        )
        assert picked == ""

    def test_missing_file_returns_empty(self, tmp_path):
        picked = graph._live_sample_url_from_input_urls(
            "example-com", DEAD, project_root=str(tmp_path)
        )
        assert picked == ""

    def test_foreign_domain_used_only_as_fallback(self, tmp_path):
        _write_input_urls(tmp_path, [FOREIGN, LIVE_A])
        picked = graph._live_sample_url_from_input_urls(
            "example-com", DEAD, project_root=str(tmp_path)
        )
        assert picked == LIVE_A

    def test_no_dead_url_picks_first_entry(self, tmp_path):
        """No dead URL → no host context to prefer — first entry wins."""
        _write_input_urls(tmp_path, [FOREIGN, LIVE_A, LIVE_B])
        picked = graph._live_sample_url_from_input_urls(
            "example-com", "", project_root=str(tmp_path)
        )
        assert picked == FOREIGN


class TestRemapWiring:
    """The swap must be wired into the re-map branch: verdict check on the
    PREVIOUS analysis, swap before the agent runs, persist sample_url."""

    def test_remap_branch_swaps_dead_sample(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        i_remap = src.index("is_remap = isinstance(_pa_remediation, dict)")
        i_success = src.index("def _on_success", i_remap)
        branch = src[i_remap:i_success]
        assert "_live_sample_url_from_input_urls" in branch, (
            "the re-map branch never swaps a dead sample URL — 335's loop"
        )
        assert "_dead_seed_verdict" in branch

    def test_remap_success_persists_swapped_sample_url(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        i_remap_cmd = src.index('"remap_count": remap_count')
        block = src[i_remap_cmd : i_remap_cmd + 400]
        assert "sample_url" in block, (
            "the remap Command must persist the (possibly swapped) sample_url"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
