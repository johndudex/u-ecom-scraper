"""[wave-50 T6] Search-mode Algolia pre-step — job 490 root cause.

Phase 1's search branch went straight to browser navigation; on a TLS-gated
client-rendered site every ``_navigate`` burned its retry budget on
chrome-error pages (600s probe hang, zero output) while the site's own
Algolia index answers the free-text query over plain HTTPS. The
category/listing branches got the algolia pre-step in wave-50 T2 — search
mode must get the same deterministic capability. A free-text search rides
the index query and must NEVER be narrowed by the creds page's taxon filter.
"""

import json
import re
import sys
from pathlib import Path

import pytest

from src.algolia_discovery import discover_catalog_urls

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEMPLATE = ROOT / "templates" / "http_navigation_scraper.py"

CREDS = {"app_id": "APPID12345", "api_key": "k" * 20, "index_name": "products"}


def _template_body(first_def: str, next_def: str) -> str:
    src = TEMPLATE.read_text(encoding="utf-8")
    start = src.index(f"def {first_def}(")
    end = src.index(f"def {next_def}(")
    return src[start:end]


def _search_body() -> str:
    return _template_body("_discover_urls_via_search", "_discover_urls_via_form_search")


def _algolia_body() -> str:
    return _template_body("_discover_urls_via_algolia", "_discover_urls_via_category")


def _hit(slug: str) -> dict:
    return {"objectID": slug, "solidusSlug": slug, "title": slug}


# ── index query behavior (src.algolia_discovery) ─────────────────────────────


class TestIndexQuery:
    def test_query_rides_params_without_taxon_filter(self):
        bodies = []

        def fetch(url, body, headers):
            bodies.append(body)
            return {"results": [{"nbHits": 0, "hits": [], "page": 0}]}

        discover_catalog_urls(
            "https://books.example", CREDS, max_items=5,
            query="personalized book", fetch_fn=fetch,
        )
        params = bodies[0]["requests"][0]["params"]
        assert "query=personalized+book" in params or (
            "query=personalized%20book" in params
        )
        assert "filters=" not in params

    def test_empty_query_stays_unfiltered_too(self):
        bodies = []

        def fetch(url, body, headers):
            bodies.append(body)
            return {"results": [{"nbHits": 0, "hits": [], "page": 0}]}

        discover_catalog_urls(
            "https://books.example", CREDS, max_items=5, fetch_fn=fetch,
        )
        assert bodies[0]["requests"][0]["params"].endswith("query=")
        assert "filters=" not in bodies[0]["requests"][0]["params"]

    def test_hits_return_in_index_order_capped(self):
        """Relevance order is the index's — preserve it, cap at max_items."""

        def fetch(url, body, headers):
            hits = [_hit(f"slug-{i}") for i in range(10)]
            return {"results": [{"nbHits": 10, "hits": hits, "page": 0}]}

        def sitemap(url):
            return "<loc>https://books.example/personalized-products/slug-0</loc>"

        res = discover_catalog_urls(
            "https://books.example", CREDS, max_items=5,
            query="book", fetch_fn=fetch, sitemap_fn=sitemap,
            head_fn=lambda u: 0,
        )
        assert res["urls"] == [
            f"https://books.example/personalized-products/slug-{i}" for i in range(5)
        ]
        assert res["stop_reason"] == "max_items"


# ── template wiring ──────────────────────────────────────────────────────────


class TestSearchBranchWiring:
    def test_algolia_pre_step_runs_before_browser_navigate(self):
        body = _search_body()
        assert body.index("_discover_urls_via_algolia(") < body.index(
            "_navigate(search_url"
        )

    def test_query_is_forwarded_to_the_index(self):
        body = _search_body()
        call = body[body.index("_discover_urls_via_algolia("):]
        assert "query=query" in call[:400]

    def test_pre_step_result_only_short_circuits_on_urls(self):
        """A non-Algolia site (empty pre-step result) must keep the HTML
        search-navigation path — the pre-step degrades, never blocks."""
        body = _search_body()
        at = body.index("_discover_urls_via_algolia(")
        after = body[at:body.index("_navigate(search_url")]
        assert "if algolia_urls:" in after
        assert "return algolia_urls" in after

    def test_free_text_search_never_gets_taxon_filter(self):
        """The wrapper computes the taxon filter only for listing/category
        mode; query mode skips taxon extraction entirely."""
        wrap = _algolia_body()
        assert "if query else extract_taxon_query" in wrap

    def test_query_param_reaches_discover_catalog_urls(self):
        wrap = _algolia_body()
        assert "query=query" in wrap

    def test_creds_fallback_to_site_url(self):
        """Search pages sometimes ship no config island; the homepage of the
        same app shell does. One fallback attempt before giving up."""
        wrap = _algolia_body()
        first = wrap.index("extract_algolia_creds(")
        assert "SITE_URL" in wrap
        assert wrap.index("extract_algolia_creds(", first + 1) > wrap.index("SITE_URL")

    def test_existing_call_sites_stay_queryless(self):
        """The category/listing branches must be byte-identical in behavior —
        no query passed, taxon filter still armed."""
        src = TEMPLATE.read_text(encoding="utf-8")
        assert "_discover_urls_via_algolia(args.category_url, limit)" in src
        assert "_discover_urls_via_algolia(args.listing_url, limit)" in src
        assert "args.category_url, limit, query" not in src
        assert "args.listing_url, limit, query" not in src

    def test_wrapper_signature_defaults_keep_callers_identical(self):
        wrap = _algolia_body()
        assert "query: str = \"\"" in wrap

    def test_wrapper_degrades_to_unavailable_without_creds(self):
        wrap = _algolia_body()
        assert 'return [], "algolia_unavailable"' in wrap

    def test_creds_without_index_name_trigger_fallback(self):
        """Algolia rejects indexName=\"\" — partial creds from the first page
        must not short-circuit the homepage fallback."""
        wrap = _algolia_body()
        assert 'creds.get("index_name")' in wrap

    def test_page_html_feeds_the_engine(self):
        """The creds page the wrapper already fetched must reach the engine
        so the anchor-prefix vote can run without a second fetch."""
        wrap = _algolia_body()
        assert "page_html=html" in wrap


# ── wall-clock deadline (job 490: three consecutive 600s probe timeouts) ─────


class TestSearchDeadline:
    def _nav_body(self) -> str:
        src = TEMPLATE.read_text(encoding="utf-8")
        start = src.index("def _navigate(")
        end = src.index("def _nav_fail_reason(")
        return src[start:end]

    def test_navigate_takes_optional_deadline(self):
        sig = self._nav_body().split("\n\n")[0]
        assert "deadline=None" in sig

    def test_navigate_stops_retrying_past_deadline(self):
        """No retry attempt may START past the caller's wall-clock bound —
        one stuck URL must not eat the whole retry ladder."""
        body = self._nav_body()
        loop_at = body.index("for attempt in range(MAX_RETRIES):")
        guard = body.index("deadline is not None and time.monotonic() >= deadline")
        assert loop_at < guard < body.index("try:", guard)
        assert "break" in body[guard:guard + 300]

    def test_search_bounds_the_walk(self):
        body = _search_body()
        assert "DISCOVERY_DEADLINE_SECONDS" in body
        assert "time.monotonic() + DISCOVERY_DEADLINE_SECONDS" in body

    def test_both_search_navigates_carry_the_deadline(self):
        body = _search_body()
        first = body.index("_navigate(search_url")
        assert "deadline=_deadline" in body[first:first + 120]
        assert "_navigate(next_url, deadline=_deadline)" in body

    def test_pagination_checks_deadline_before_next_page(self):
        body = _search_body()
        check = body.index("if time.monotonic() > _deadline:")
        nxt = body.index("_navigate(next_url, deadline=_deadline)")
        assert check < nxt

    def test_deadline_stop_is_honest_navigate_error(self):
        """A wall-clock cut is INCONCLUSIVE/FAIL coverage, never site
        exhaustion — same semantics 1b already uses."""
        body = _search_body()
        check = body.index("if time.monotonic() > _deadline:")
        assert 'stop_reason = "navigate_error"' in body[check:check + 300]


# ── requests_scraper.py carries the same search treatment ────────────────────
# The strategy picker sends search jobs at EITHER template (490 →
# http_navigation, 491 → http_requests), so the query must ride discovery in
# both — job 491's draft logged the query and never passed it.


REQ = ROOT / "templates" / "requests_scraper.py"


def _requests_body(first_def: str, next_def: str) -> str:
    src = REQ.read_text(encoding="utf-8")
    start = src.index(f"def {first_def}(")
    end = src.index(f"def {next_def}(")
    return src[start:end]


class TestRequestsTemplateSearch:
    def test_gate_takes_query_and_skips_taxon_for_search(self):
        wrap = _requests_body("_discover_urls_via_algolia", "_discover_phase1")
        assert "query: str = \"\"" in wrap
        assert "if query else extract_taxon_query" in wrap

    def test_gate_feeds_query_and_page_html_to_the_engine(self):
        wrap = _requests_body("_discover_urls_via_algolia", "_discover_phase1")
        assert "query=query" in wrap
        assert "page_html=html" in wrap

    def test_gate_homepage_creds_fallback(self):
        wrap = _requests_body("_discover_urls_via_algolia", "_discover_phase1")
        assert "SITE_URL" in wrap
        assert 'creds.get("index_name")' in wrap

    def test_phase1_gate_threads_the_query(self):
        body = _requests_body("_discover_phase1", "load_urls_from_file")
        assert "query: str = \"\"" in body
        assert "query=query" in body

    def test_both_main_call_sites_pass_the_query(self):
        src = REQ.read_text(encoding="utf-8")
        calls = re.findall(r"_discover_phase1\(_discovery_cfg[^)]*\)", src)
        assert len(calls) == 2, calls
        for call in calls:
            assert "query=" in call, call


# ── anchor-prefix vote (src.algolia_discovery) ───────────────────────────────


class TestAnchorPrefixVote:
    def test_prefix_from_featured_anchors_when_sitemap_has_none(self):
        """The queried slug is not on the page, but the page links OTHER
        products under the same directory — HEAD-verify that directory."""
        page = (
            '<a href="/pages/about">about</a>'
            '<a href="/personalized-products/featured-a">a</a>'
            '<a href="/personalized-products/featured-b">b</a>'
            '<a href="/personalized-books/kids/themes">listings</a>'
        )
        heads = []

        def head(url):
            heads.append(url)
            return 200 if "/personalized-products/" in url else 0

        def fetch(url, body, headers):
            hits = [_hit("queried-slug-not-on-page")]
            return {"results": [{"nbHits": 1, "hits": hits, "page": 0}]}

        sitemaps = []

        def sitemap(url):
            sitemaps.append(url)
            return ""

        res = discover_catalog_urls(
            "https://books.example", CREDS, max_items=5,
            query="book", fetch_fn=fetch, head_fn=head,
            sitemap_fn=sitemap, page_html=page,
        )
        assert res["urls"] == ["https://books.example/personalized-products/queried-slug-not-on-page"]
        assert res["pdp_prefix"] == "/personalized-products/"
        assert not sitemaps  # anchors resolved first — no sitemap fetches
        # built-in candidates were tried first and missed
        assert heads[0].startswith("https://books.example/products/")

    def test_anchor_candidates_ranked_by_frequency_capped(self):
        from src.algolia_discovery import _anchor_prefix_candidates

        page = (
            '<a href="/b/x">1</a><a href="/b/y">2</a><a href="/b/z">3</a>'
            '<a href="/a/x">1</a><a href="/a/y">2</a>'
            '<a href="/c/x">1</a>'
            '<a href="/single">no-dir</a>'
            '<a href="https://other.example/d/x">cross-host-ignored</a>'
        )
        cands = _anchor_prefix_candidates(page)
        assert cands[0] == "/b/"
        assert set(cands) == {"/b/", "/a/", "/c/"}
        link_farm = "".join('<a href="/p/%d">' % i for i in range(30))
        assert len(_anchor_prefix_candidates(link_farm)) <= 12

    def test_no_page_html_keeps_sitemap_resolution(self):
        """Listing-mode runs that pass no page_html resolve exactly as
        before — sitemap grep remains the last resort."""
        def fetch(url, body, headers):
            hits = [_hit("slug-x")]
            return {"results": [{"nbHits": 1, "hits": hits, "page": 0}]}

        def head(url):
            return 0

        def sitemap(url):
            return ("<loc>https://books.example/namespace/slug-x</loc>"
                    "<loc>https://books.example/other/page</loc>")

        res = discover_catalog_urls(
            "https://books.example", CREDS, max_items=5,
            fetch_fn=fetch, head_fn=head, sitemap_fn=sitemap,
        )
        assert res["urls"] == ["https://books.example/namespace/slug-x"]
