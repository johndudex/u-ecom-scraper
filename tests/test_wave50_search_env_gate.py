"""[wave-50 T6c] A search job's env gate may only pin the query's OWN search URL.

Job 497 (briscoes, search_term "air fryer"): run_execution injected
SCRAPER_LISTING_URL from the navigator's ``discovery.listing_url`` — a
CATEGORY page (/electrical/) the navigator browsed en route. The draft's env
gate OVERRIDES query-mode discovery ("Env gate: SCRAPER_LISTING_URL set —
forcing Phase 1 discovery on /electrical/" in the draft's own log), the
client-rendered category yielded 0 HTTP anchors, and the rescue arm shipped
5 real-but-query-irrelevant small-appliance rows (blender, shaver, espresso
machine, robot vacuum). 493/494 passed only because their navigators happened
to promote query-shaped URLs. Search discovery is owned by ``--query``; the
env gate may pin nothing unless the candidate IS the query's search URL.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.agents.nodes.run_execution import _query_shaped  # noqa: E402

RE = ROOT / "webapp" / "agents" / "nodes" / "run_execution.py"


class TestQueryShaped:
    def test_plus_encoded_search_url_matches(self):
        assert _query_shaped(
            "https://www.briscoes.co.nz/catalogsearch/result/?q=air+fryer",
            "air fryer",
        )

    def test_percent_encoded_search_url_matches(self):
        assert _query_shaped(
            "https://shop.example/search?q=air%20fryer", "air fryer"
        )

    def test_raw_query_in_url_matches(self):
        assert _query_shaped("https://shop.example/search?air fryer", "air fryer")

    def test_category_page_is_not_query_shaped(self):
        assert not _query_shaped(
            "https://www.briscoes.co.nz/electrical/", "air fryer"
        )

    def test_homepage_is_not_query_shaped(self):
        assert not _query_shaped("https://www.jomashop.com/", "omega watch")

    def test_other_words_page_is_not_query_shaped(self):
        assert not _query_shaped(
            "https://shop.example/search?q=espresso+machine", "air fryer"
        )

    def test_empty_query_or_url_is_never_shaped(self):
        assert not _query_shaped("https://shop.example/search?q=x", "")
        assert not _query_shaped("", "air fryer")


class TestSearchArmWiring:
    """The env-gate block must give search_term its own arm — the navigator's
    promoted listing may pin the gate ONLY when it is the query's search URL,
    with the F17 domain guard still applied."""

    def _gate_body(self) -> str:
        src = RE.read_text(encoding="utf-8")
        start = src.index("_listing_url_env = \"\"")
        return src[start : src.index("Enforce scope=firstn")]

    def test_search_term_has_its_own_arm(self):
        body = self._gate_body()
        assert 'if input_mode in ("navigation", "list_page"):' in body
        assert 'elif input_mode == "search_term":' in body

    def test_search_arm_uses_the_query_shaped_guard(self):
        body = self._gate_body()
        arm = body[body.index('elif input_mode == "search_term":') :]
        assert "_query_shaped(" in arm
        assert "search_criteria" in arm

    def test_search_arm_logs_when_it_declines_to_gate(self):
        body = self._gate_body()
        arm = body[body.index('elif input_mode == "search_term":') :]
        assert "NOT setting SCRAPER_LISTING_URL" in arm

    def test_search_arm_keeps_the_f17_domain_guard(self):
        body = self._gate_body()
        arm = body[body.index('elif input_mode == "search_term":') :]
        assert "F17 dropped cross-domain SCRAPER_LISTING_URL" in arm

    def test_nav_arm_still_prefers_discovery_listing(self):
        """The navigation/list_page arm keeps its F7+M6 priority unchanged."""
        body = self._gate_body()
        nav_arm = body[: body.index('elif input_mode == "search_term":')]
        assert "_url_shaped_criteria(state) or _job_listing or \"\"" in nav_arm
        assert "_disc_env.get(\"listing_url\")" in nav_arm

    def test_injection_log_still_fires_for_both_arms(self):
        body = self._gate_body()
        assert body.count("(env-var, deterministic discovery)") >= 2
