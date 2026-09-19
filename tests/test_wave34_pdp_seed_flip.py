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
LISTING_URL = "https://www.vinted.be/catalog/5-men"


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

    def test_lone_product_foreign_ref_does_not_flip(self):
        # [wave-34 critique] one Product block whose refs point at a
        # different url = a single card of a lazy grid, not a PDP.
        assert not graph._jsonld_product_entity(
            [{"@type": "Product", "url": "https://www.vinted.fr/items/9"}],
            "https://www.vinted.be/catalog/5-men",
        )
        assert not graph._jsonld_product_entity(
            [{"@type": "Product", "offers": {"url": "https://www.vinted.fr/items/9"}}],
            "https://www.vinted.be/catalog/5-men",
        )

    def test_lone_product_canonical_ref_flips(self):
        assert graph._jsonld_product_entity(
            [{"@type": "Product", "url": PDP_URL}], PDP_URL
        )


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
    def test_seed_file_write_failure_fails_open(self, monkeypatch):
        # [wave-34 critique] if input_urls.json cannot be written the flip is
        # withheld — a flipped job without its seed file is a guaranteed fail.
        monkeypatch.setattr(
            probe_tools,
            "run_probe_with_captcha_check",
            lambda *a, **k: _probe_data([{"@type": "Product"}]),
        )
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda data: {"captcha_detected": False}
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_write_flip_input_urls", lambda *a, **k: False)

        state = {
            "job_id": 0,
            "url": PDP_URL,
            "input_mode": "list_page",
            "site_slug": "vinted-be",
        }
        cmd = graph.check_accessibility(state, None)
        assert cmd.goto == "browser_traverse"
        assert "pdp_seed_flip" not in (cmd.update or {})

    @pytest.mark.django_db
    def test_flip_persists_notes_and_seed_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            probe_tools,
            "run_probe_with_captcha_check",
            lambda *a, **k: _probe_data([{"@type": "Product"}]),
        )
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda data: {"captcha_detected": False}
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        import agents.state as state_mod

        seen = {}

        def _fake_write(st, urls):
            seen["slug"] = st.get("site_slug")
            seen["urls"] = list(urls)
            return True

        monkeypatch.setattr(graph, "_write_flip_input_urls", _fake_write)
        state = {
            "job_id": 0,
            "url": PDP_URL,
            "input_mode": "list_page",
            "site_slug": "vinted-be",
        }
        cmd = graph.check_accessibility(state, None)
        assert cmd.update["pdp_seed_flip"] is True
        assert seen == {"slug": "vinted-be", "urls": [PDP_URL]}
        # state field exists for the belt (TraverseState / resume consumers)
        assert "pdp_seed_flip" in state_mod.ScrapeState.__annotations__

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


# ── [wave-39] PDP seed + same-host listing → swap, not demote ───────────

class TestPdpListingSwap:
    """The wave-34 demote is right when the job ONLY has a PDP. When the job
    itself names a same-host listing (search_criteria) and the advisory
    listing probe reached it, the honest reading of the intake is "discover
    from the listing" — prod retry batch 719-737 all collapsed to 1 item."""

    def _state(self, criteria=LISTING_URL, mode="list_page"):
        return {
            "job_id": 0,
            "url": PDP_URL,
            "input_mode": mode,
            "site_slug": "vinted-be",
            "search_criteria": criteria,
        }

    def _seed_probe(self):
        return _probe_data([{"@type": "Product", "offers": {"price": 44.99}}])

    def _listing_probe(self, jsonld=None, **over):
        lp = _probe_data(
            jsonld
            if jsonld is not None
            else [{"@type": "ItemList", "itemListElement": [{"@type": "Product"}]}]
        )
        lp.update(over)
        return lp

    def _swap(self, state, listing_probe):
        return graph._pdp_listing_swap(
            state, PDP_URL, self._seed_probe(), listing_probe
        )

    def test_swap_fires_same_host_listing(self):
        out = self._swap(self._state(), self._listing_probe())
        assert out is not None
        updates, note = out
        assert updates["url"] == LISTING_URL
        assert updates["product_url"] == LISTING_URL
        assert "[INTAKE-PDP-SWAP]" in note

    def test_no_criteria_demotes(self):
        # No listing named → the wave-34 demote is still the honest reading.
        assert self._swap(self._state(criteria=""), self._listing_probe()) is None

    def test_cross_host_criteria_demotes(self):
        st = self._state(criteria="https://www.vinted.fr/catalog/5-men")
        assert self._swap(st, self._listing_probe()) is None

    def test_missing_listing_probe_demotes(self):
        # Advisory probe never ran (host mismatch / skip path) — no evidence,
        # no swap.
        assert self._swap(self._state(), None) is None

    def test_blocked_listing_probe_demotes(self):
        lp = self._listing_probe(blocked=True)
        assert self._swap(self._state(), lp) is None

    def test_listing_itself_pdp_demotes(self):
        # User pasted two product URLs — demote, never discover from a PDP.
        lp = self._listing_probe([{"@type": "Product", "offers": {"price": 1.0}}])
        assert self._swap(self._state(), lp) is None

    def test_non_list_page_mode_never_swaps(self):
        st = self._state(mode="navigation")
        assert self._swap(st, self._listing_probe()) is None

    def test_seed_not_pdp_no_swap(self):
        # Defensive: helper re-checks the seed discriminator itself.
        listing_seed_probe = _probe_data(
            [{"@type": "ItemList", "itemListElement": [{"@type": "Product"}]}]
        )
        out = graph._pdp_listing_swap(
            self._state(), LISTING_URL, listing_seed_probe, self._listing_probe()
        )
        assert out is None
