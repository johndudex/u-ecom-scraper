"""[wave-51 T8] A search_term workspace is never seeded with the Site's URL list.

Job 499 (briscoes, search_term "air fryer"): setup_workspace wrote the Site
model's stored ``input_urls`` — job 497's 20 electrical-category PDPs — into
the workspace as ``input_urls.json``. For a search job that file is a lie:
discovery is query-owned, and the stale seed then poisoned every honesty
check downstream —

  1. the tester's VERIFICATION run is seed-driven by contract (wave-14),
     so it extracted 20/20 real-but-query-irrelevant rows and stamped
     ``PASS_VIA_SEED_ONLY``;
  2. ``route_after_testing``'s ground-truth override read those same rows
     (>=3 real items) and overrode the tester's FAIL verdict.

``url_list``/``list_page``/``navigation`` keep the wave-14 seed contract —
their workspace seed is meaningful. A search job's workspace starts empty.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from webapp.agents.nodes.setup_workspace import _seed_urls_for_workspace  # noqa: E402

SEED = [
    "https://www.briscoes.co.nz/product/1116109/magic-bullet-blender/",
    "https://www.briscoes.co.nz/product/1131756/panasonic-shaver/",
]


class TestSearchJobsGetNoSeed:
    def test_search_term_urls_are_dropped(self):
        assert _seed_urls_for_workspace({"input_mode": "search_term"}, SEED) == []

    def test_search_term_without_urls_stays_empty(self):
        assert _seed_urls_for_workspace({"input_mode": "search_term"}, []) == []

    def test_modeless_state_is_not_emptied(self):
        """A missing input_mode must not silently drop seeds (legacy paths)."""
        assert _seed_urls_for_workspace({}, SEED) == SEED


class TestOtherModesKeepTheWave14Contract:
    def test_url_list_keeps_seed(self):
        assert _seed_urls_for_workspace({"input_mode": "url_list"}, SEED) == SEED

    def test_list_page_keeps_seed(self):
        assert _seed_urls_for_workspace({"input_mode": "list_page"}, SEED) == SEED

    def test_navigation_keeps_seed(self):
        assert _seed_urls_for_workspace({"input_mode": "navigation"}, SEED) == SEED


class TestNodeAppliesTheGuard:
    def test_seed_write_goes_through_the_guard(self):
        """The node must route its seed write through the helper — the guard
        has to sit between the Site read and the file write."""
        src = (
            ROOT / "webapp" / "agents" / "nodes" / "setup_workspace.py"
        ).read_text(encoding="utf-8")
        guard_at = src.index("input_urls = _seed_urls_for_workspace(state, input_urls)")
        write_at = src.index('os.path.join(workspace_dir, "input_urls.json")')
        assert guard_at < write_at
