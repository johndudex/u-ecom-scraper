"""[wave-50 T1] src/algolia_discovery.py — catalog discovery for Algolia-backed
client-rendered listings.

Prod job 1116 (wonderbly): the draft's Algolia query SUCCEEDED (nbHits=86) but
hit→URL conversion returned 0 — the index carries slugs under ``solidusSlug`` /
``prismicProductSlug`` and the draft's 5-key fallback only knew
``productUrl/product_url/url/slug/handle``. The writer then shipped SSR nav
anchors as "product URLs" and the job completed with junk rows.

These tests pin the working chain end to end with recorded payloads (the
fixture is a real 10-hit page from the live wonderbly index) and stubbed
transport — no network in tests, creds in tests are synthetic.
"""

import html as html_mod
import json
from pathlib import Path

import pytest

from src.algolia_discovery import (
    _hit_to_url,
    build_taxon_filter,
    discover_catalog_urls,
    extract_algolia_creds,
    extract_taxon_query,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "algolia" / "wonderbly_page0.json")
    .read_text(encoding="utf-8"))

CREDS = {"app_id": "TESTAPPID", "api_key": "test-search-key",
         "index_name": "test_products"}


def _result(payload):
    return {"results": [payload]}


# ── creds extraction ─────────────────────────────────────────────────────────


class TestExtractCreds:
    def test_astro_island_props_tuples(self):
        """The wonderbly shape: astro-island serializes props as HTML-escaped
        JSON tuples ``[0,"value"]``. appId+apiKey+algoliaIndex all ride the
        same island (real listing HTML, creds swapped for synthetic)."""
        props = ('<astro-island uid="F0B4v" props="{&quot;algoliaIndex&quot;:'
                 '[0,&quot;prod_us&quot;],&quot;appId&quot;:[0,&quot;TESTAPPID&quot;],'
                 '&quot;apiKey&quot;:[0,&quot;test-search-key&quot;]}" ssr client="only">')
        creds = extract_algolia_creds(props)
        assert creds == {"app_id": "TESTAPPID", "api_key": "test-search-key",
                         "index_name": "prod_us"}

    def test_inline_json_and_algoliasearch_call(self):
        """Non-Astro shape: inline config JSON (raw quotes — <script> bodies
        are not HTML-escaped) + the algoliasearch() init call."""
        page = json.dumps({"algoliaIndex": "catalog_idx", "searchActive": True})
        script = ("<script>const c=algoliasearch('TESTAPPID','test-search-key');"
                  "window.__cfg=" + page + ";</script>")
        creds = extract_algolia_creds(script)
        assert creds is not None
        assert creds["app_id"] == "TESTAPPID"
        assert creds["api_key"] == "test-search-key"
        assert creds["index_name"] == "catalog_idx"

    def test_creds_and_index_split_across_islands_merge(self):
        """The live wonderbly page: AlgoliaInsights carries appId/apiKey, the
        listing island carries algoliaIndex — the extractor merges them."""
        page = (
            '<astro-island uid="A" props="{&quot;appId&quot;:'
            '[0,&quot;TESTAPPID&quot;],&quot;apiKey&quot;:'
            '[0,&quot;test-search-key&quot;]}">')
        page += (
            '<astro-island uid="B" props="{&quot;algoliaIndex&quot;:'
            '[0,&quot;prod_us&quot;],&quot;filters&quot;:[0]}">')
        creds = extract_algolia_creds(page)
        assert creds == {"app_id": "TESTAPPID", "api_key": "test-search-key",
                         "index_name": "prod_us"}

    def test_deep_and_case_insensitive_keys(self):
        """The live page's real shapes: ``algoliaIndex`` nested inside a
        regions array, and ``appID`` (capital D) as the app-id key."""
        page = ('<astro-island props="{&quot;regions&quot;:[1,[[0,{&quot;'
                'locale&quot;:[0,&quot;en-us&quot;],&quot;algoliaIndex&quot;:'
                '[0,&quot;prod_us&quot;]}]]]}">'
                '<astro-island props="{&quot;appID&quot;:[0,&quot;TESTAPPID&quot;],'
                '&quot;apiKey&quot;:[0,&quot;test-search-key&quot;]}">')
        creds = extract_algolia_creds(page)
        assert creds == {"app_id": "TESTAPPID", "api_key": "test-search-key",
                         "index_name": "prod_us"}

    def test_taxon_query_extracted_from_any_island(self):
        page = ('<astro-island props="{&quot;taxonQuery&quot;:'
                '[0,&quot;Kids &gt; Age &gt; 0-3&quot;],'
                '&quot;hitsPerPage&quot;:[2,24]}">')
        assert extract_taxon_query(page) == "Kids > Age > 0-3"
        assert extract_taxon_query("<html></html>") == ""

    def test_plain_html_yields_none(self):
        assert extract_algolia_creds("<html><body>shop</body></html>") is None


# ── hit → URL conversion ─────────────────────────────────────────────────────


class TestHitToUrl:
    def test_solidus_slug_is_a_bare_slug(self):
        kind, val = _hit_to_url({"solidusSlug": "newspaper-birthday-book",
                                 "objectID": "newspaper-birthday"})
        assert kind == "slug" and val == "newspaper-birthday-book"

    def test_absolute_url_beats_slugs(self):
        kind, val = _hit_to_url({"url": "https://x.example/p/1",
                                 "solidusSlug": "s1"})
        assert kind == "url" and val == "https://x.example/p/1"

    def test_path_absolute_joined_later_not_here(self):
        """A path-absolute value ('/p/1') is a path, not a bare slug — the
        caller joins it to base_url."""
        kind, val = _hit_to_url({"url": "/p/1", "solidusSlug": "s1"})
        assert kind == "path" and val == "/p/1"

    def test_objectid_is_the_last_resort(self):
        kind, val = _hit_to_url({"objectID": "obj-9"})
        assert kind == "slug" and val == "obj-9"

    def test_unusable_hit_is_none(self):
        assert _hit_to_url({"_highlightResult": {}, "price": "$9"}) is None


# ── catalog discovery (recorded payload, stubbed transport) ──────────────────


class TestDiscoverCatalogUrls:
    def _page_fetch(self, pages):
        """fetch_fn stub serving numbered pages of hits."""
        calls = []

        def fetch(url, body, headers):
            calls.append(body)
            page = len(calls) - 1
            if page >= len(pages):
                return _result({"hits": [], "nbHits": 0, "page": page})
            return _result(pages[page])

        return fetch, calls

    def test_recorded_payload_yields_pdp_urls_via_sitemap(self):
        """THE chain: 10 real hits (solidusSlug only) → sitemap-derived
        /personalized-products/ prefix → 10 real PDP URLs."""
        slugs = [h["solidusSlug"] for h in FIXTURE["results"][0]["hits"]]
        sitemap_locs = "".join(
            f"<loc>https://www.wonderbly.com/personalized-products/{s}</loc>"
            for s in slugs)

        def sitemap_fn(url):
            assert url == "https://www.wonderbly.com/sitemap.xml"
            return f"<urlset>{sitemap_locs}</urlset>"

        fetch, _ = self._page_fetch([FIXTURE["results"][0]])
        out = discover_catalog_urls(
            "https://www.wonderbly.com", dict(CREDS), max_items=10,
            fetch_fn=fetch, sitemap_fn=sitemap_fn,
            head_fn=lambda u: 404)
        assert out["nbHits"] == 228
        assert len(out["urls"]) == 10
        assert all(u.startswith(
            "https://www.wonderbly.com/personalized-products/") for u in out["urls"])
        assert out["urls"][0].endswith("/" + slugs[0])
        assert out["stop_reason"] == "max_items"
        assert out["pdp_prefix"] == "/personalized-products/"

    def test_prefix_from_head_candidate_no_sitemap_call(self):
        """Solidus default route: /products/<slug> verifies by HEAD → prefix
        cached for the whole run, sitemap never fetched."""
        heads = []

        def head_fn(url):
            heads.append(url)
            return 200 if "/products/" in url else 404

        page = {"hits": [{"solidusSlug": f"s{i}"} for i in range(3)],
                "nbHits": 3, "page": 0}
        fetch, _ = self._page_fetch([page])
        out = discover_catalog_urls(
            "https://shop.example", dict(CREDS), max_items=3,
            fetch_fn=fetch, head_fn=head_fn,
            sitemap_fn=lambda u: (_ for _ in ()).throw(AssertionError("sitemap")))
        assert out["urls"] == [f"https://shop.example/products/s{i}" for i in range(3)]
        assert out["pdp_prefix"] == "/products/"
        assert len([u for u in heads if u.endswith("/products/s0")]) == 1  # cached

    def test_pagination_walks_to_nbhits(self):
        page0 = {"hits": [{"solidusSlug": f"a{i}"} for i in range(10)],
                 "nbHits": 12, "page": 0}
        page1 = {"hits": [{"solidusSlug": f"b{i}"} for i in range(2)],
                 "nbHits": 12, "page": 1}
        fetch, calls = self._page_fetch([page0, page1])
        out = discover_catalog_urls(
            "https://shop.example", dict(CREDS), max_items=25,
            hits_per_page=10,
            fetch_fn=fetch, head_fn=lambda u: 200,
            sitemap_fn=lambda u: "")
        assert len(out["urls"]) == 12
        assert out["stop_reason"] == "exhausted"
        assert len(calls) == 2

    def test_max_pages_guard_against_endless_identical_pages(self):
        page = {"hits": [{"solidusSlug": "dup"} for _ in range(10)],
                "nbHits": 100000, "page": 0}
        fetch, calls = self._page_fetch([page] * 50)
        out = discover_catalog_urls(
            "https://shop.example", dict(CREDS), max_items=500, max_pages=3,
            hits_per_page=10,
            fetch_fn=fetch, head_fn=lambda u: 200)
        assert len(calls) <= 3
        assert out["stop_reason"] == "max_pages"

    def test_absolute_hit_urls_pass_through_untouched(self):
        page = {"hits": [{"url": f"https://cdn.example/p/{i}"} for i in range(4)],
                "nbHits": 4, "page": 0}
        fetch, _ = self._page_fetch([page])
        out = discover_catalog_urls(
            "https://shop.example", dict(CREDS), max_items=4,
            fetch_fn=fetch,
            head_fn=lambda u: (_ for _ in ()).throw(AssertionError("head")),
            sitemap_fn=lambda u: (_ for _ in ()).throw(AssertionError("sitemap")))
        assert out["urls"] == [f"https://cdn.example/p/{i}" for i in range(4)]

    def test_garbage_payloads_degrade_to_empty_never_raise(self):
        for bad in (None, {}, {"results": []}, {"results": [None]},
                    {"results": [{"hits": "nope"}]}):
            out = discover_catalog_urls(
                "https://shop.example", dict(CREDS), max_items=5,
                fetch_fn=lambda u, b, h, _bad=bad: _bad, head_fn=lambda u: 200)
            assert out["urls"] == []

    def test_zero_nbhits_is_empty_not_an_error(self):
        fetch, _ = self._page_fetch([])
        out = discover_catalog_urls(
            "https://shop.example", dict(CREDS), max_items=5,
            fetch_fn=lambda u, b, h: _result({"hits": [], "nbHits": 0, "page": 0}),
            head_fn=lambda u: 200)
        assert out["urls"] == [] and out["nbHits"] == 0

    def test_unresolvable_slugs_are_skipped_not_fatal(self):
        page = {"hits": [{"solidusSlug": "ghost"}], "nbHits": 1, "page": 0}
        fetch, _ = self._page_fetch([page])
        out = discover_catalog_urls(
            "https://shop.example", dict(CREDS), max_items=5,
            fetch_fn=fetch, head_fn=lambda u: 404, sitemap_fn=lambda u: "")
        assert out["urls"] == []


# ── taxon facet filter ───────────────────────────────────────────────────────


class TestBuildTaxonFilter:
    def test_known_attribute_value_match(self):
        probe = _result({"hitsPerPage": 0, "facets": {
            "taxon_path": {"Kids > Age > 0-3": 86, "Kids > Age > 3-6": 120}}})

        def fetch(url, body, headers):
            return probe

        assert build_taxon_filter(dict(CREDS), "Kids > Age > 0-3",
                                  fetch_fn=fetch) == 'taxon_path:"Kids > Age > 0-3"'

    def test_hierarchical_categories_ladder_matches(self):
        """Live wonderbly has NO taxon_path — the taxon rides the Algolia
        hierarchical facets (categories.lvl0/lvl1/lvl2, ' > ' separated)."""
        probe = _result({"hitsPerPage": 0, "facets": {
            "categories.lvl2": {"Kids > Age > 0-3": 86,
                                "Kids > Age > 3-6": 120}}})
        assert build_taxon_filter(
            dict(CREDS), "Kids > Age > 0-3",
            fetch_fn=lambda u, b, h: probe) == 'categories.lvl2:"Kids > Age > 0-3"'

    def test_no_match_returns_empty_string(self):
        probe = _result({"hitsPerPage": 0, "facets": {
            "category_path": {"Other": 5}}})
        assert build_taxon_filter(
            dict(CREDS), "Kids > Age > 0-3",
            fetch_fn=lambda u, b, h: probe) == ""

    def test_probe_failure_returns_empty_string(self):
        assert build_taxon_filter(
            dict(CREDS), "x", fetch_fn=lambda u, b, h: None) == ""


@pytest.mark.parametrize("hit,expected", [
    ({"solidusSlug": ""}, None),
    ({"prismicProductSlug": "  "}, None),
])
def test_blank_slug_values_are_none(hit, expected):
    assert _hit_to_url(hit) is expected
