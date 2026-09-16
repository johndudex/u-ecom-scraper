"""[wave-34 T34-2] PDP-seed honesty — classified in check_accessibility.

Prod evidence: jobs 614/620/622/625/626 all fed a PDP seed into list_page
navigation; discovery harvested (cross-domain) recommendation carousels and
died at the contamination guard. When the probe's own JSON-LD identifies the
seed as a Product, the job demotes to url_list with the seed as its single
item instead of traversing from a PDP.
"""

import re

import pytest

from agents import graph
from agents.tools import probe_tools


def _probe_data(jsonld):
    return {
        "success": True,
        "method": "direct_http",
        "http_method": "direct_http",
        "browser_method": None,
        "proxy_tier": "none",
        "needs_browser": False,
        "blocked": False,
        "captcha_detected": False,
        "status_code": 200,
        "body_length": 200000,
        "methods_tried": ["direct_http"],
        "jsonld": jsonld,
        "meta": {},
        "selector_results": {},
    }


# ── Discriminator unit tests ────────────────────────────────────────────

PDP_URL = "https://www.vinted.be/items/10014966848-levis-501"


class TestJsonldProductEntity:
    def test_top_level_product_flips(self):
        assert graph._jsonld_product_entity(
            [{"@type": "Product", "name": "x", "offers": {"price": 44.99}}], PDP_URL
        )

    def test_type_list_containing_product_flips(self):
        assert graph._jsonld_product_entity(
            [{"@type": ["Product", "Thing"]}], PDP_URL
        )

    def test_item_list_with_embedded_products_does_not_flip(self):
        # Products nested under itemListElement are NOT top-level entities.
        blocks = [{
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "Product", "name": "a", "url": "https://www.vinted.be/items/1"},
                {"@type": "Product", "name": "b", "url": "https://www.vinted.be/items/2"},
            ],
        }]
        assert not graph._jsonld_product_entity(blocks, "https://www.vinted.be/catalog/5-men")

    def test_graph_product_matching_canonical_flips(self):
        blocks = [{
            "@graph": [
                {"@type": "WebPage", "@id": PDP_URL},
                {"@type": "Product", "url": PDP_URL, "offers": {"url": PDP_URL}},
            ]
        }]
        assert graph._jsonld_product_entity(blocks, PDP_URL)

    def test_graph_product_foreign_url_does_not_flip(self):
        blocks = [{
            "@graph": [
                {"@type": "Product", "url": "https://www.vinted.fr/items/9"},
            ]
        }]
        assert not graph._jsonld_product_entity(blocks, PDP_URL)

    def test_card_grid_top_level_products_do_not_flip(self):
        # ≥2 top-level Product blocks with distinct foreign urls = a card grid.
        blocks = [
            {"@type": "Product", "url": "https://www.vinted.be/items/1"},
            {"@type": "Product", "url": "https://www.vinted.be/items/2"},
            {"@type": "Product", "url": "https://www.vinted.be/items/3"},
        ]
        assert not graph._jsonld_product_entity(blocks, "https://www.vinted.be/catalog/5-men")

    def test_repeated_same_entity_blocks_flip(self):
        # Some PDPs repeat the same Product entity (no url) — not a grid.
        blocks = [{"@type": "Product", "name": "x"}, {"@type": "Product", "name": "x"}]
        assert graph._jsonld_product_entity(blocks, PDP_URL)

    def test_no_jsonld_no_flip(self):
        assert not graph._jsonld_product_entity([], PDP_URL)
        assert not graph._jsonld_product_entity(None, PDP_URL)
        assert not graph._jsonld_product_entity([{"@type": "BreadcrumbList"}], PDP_URL)


# ── Flip integration through check_accessibility ────────────────────────

class TestPdpSeedFlip:
    @pytest.mark.django_db
    def test_list_page_pdp_seed_flips_to_url_list(self, monkeypatch):
        monkeypatch.setattr(
            probe_tools,
            "run_probe_with_captcha_check",
            lambda *a, **k: _probe_data(
                [{"@type": "Product", "offers": {"price": 44.99}}]
            ),
        )
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda data: {"captcha_detected": False}
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        state = {
            "job_id": 0,
            "url": PDP_URL,
            "input_mode": "list_page",
            "site_slug": "vinted-be",
        }
        cmd = graph.check_accessibility(state, None)
        # The job demotes to url_list → site_analyzer, never browser_traverse.
        assert cmd.goto == "site_analyzer"
        assert cmd.update["input_mode"] == "url_list"
        assert cmd.update["input_urls"] == [PDP_URL]
        assert cmd.update["pdp_seed_flip"] is True
        assert cmd.update["probe_result"]["connectivity"]["method_that_worked"]

    @pytest.mark.django_db
    def test_list_page_listing_seed_does_not_flip(self, monkeypatch):
        monkeypatch.setattr(
            probe_tools,
            "run_probe_with_captcha_check",
            lambda *a, **k: _probe_data(
                [{"@type": "ItemList", "itemListElement": [{"@type": "Product"}]}]
            ),
        )
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda data: {"captcha_detected": False}
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        state = {
            "job_id": 0,
            "url": "https://www.vinted.be/catalog/5-men",
            "input_mode": "list_page",
        }
        cmd = graph.check_accessibility(state, None)
        assert cmd.goto == "browser_traverse"
        assert "pdp_seed_flip" not in (cmd.update or {})
        assert "input_mode" not in (cmd.update or {})

    @pytest.mark.django_db
    def test_navigation_mode_pdp_seed_keeps_mode(self, monkeypatch):
        # navigation/search_term seeds are starting hints — the user asked
        # for discovery, so a PDP seed must NOT flip.
        monkeypatch.setattr(
            probe_tools,
            "run_probe_with_captcha_check",
            lambda *a, **k: _probe_data([{"@type": "Product"}]),
        )
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda data: {"captcha_detected": False}
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        state = {
            "job_id": 0,
            "url": PDP_URL,
            "input_mode": "navigation",
            "search_criteria": "levis 501",
        }
        cmd = graph.check_accessibility(state, None)
        assert cmd.goto == "browser_traverse"
        assert "pdp_seed_flip" not in (cmd.update or {})


# ── Belt: flipped job never trusts its seed as a listing ────────────────

class TestPdpFlipBelt:
    def test_belt_downgrades_on_pdp_seed_flip(self):
        with open(graph.__file__, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(
            r"result = browser_traverse\(.*?trust_start_as_listing=\((.*?)\),",
            src, re.S,
        )
        assert m, "primary browser_traverse call with belt not found"
        assert 'not state.get("pdp_seed_flip")' in m.group(1)
