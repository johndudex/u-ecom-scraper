"""Wave-48 — client-rendered listing detection (job 1082 class).

wonderbly.com/personalized-books/kids/age/0-3 is an Astro shell + Algolia
InstantSearch grid: the non-JS HTML passes the probe's 2000-byte floor with
ZERO grid anchors (only 2 escaped FAQ links inside a <script> body), so the
job shipped http_requests discovery that starved (2 of firstn/10 items,
COMPLETED). These tests pin the fix chain:

- T1 — probe rungs report ``item_link_count`` (browser_service/probe.py)
- T2 — ``listing_client_rendered`` stamp + _derive_strategy flip
- T3 — ``identical_last_page`` telemetry (src/listing_discovery.py)
- T4 — known-yield-gap checker + routing touchpoints
- T6 — [SCOPE-SHORTFALL] finalize honesty note

[wave-48b] Originally gated behind LISTING_ANCHOR_FLOOR / LISTING_YIELD_GATE
for the local e2e. Owner decision (wave-45b rule): an env var is not a
proper switch — the gates are REMOVED and the reviewed merge/deploy is the
switch; rollback = revert the commit. These tests pin the unconditional
behavior. TestEnvVarIsDead asserts the old vars are inert in BOTH
directions; those tests would have passed the OPPOSITE way under the old
flag-OFF contract. The guards added alongside the removal: a
fingerprint-proven PDP transport declines Source A (job-524), POST-form
discovery is excluded from the flip (locumtenens replay contract), and
job_restart strips inherited [SCOPE-SHORTFALL] rows.
"""

import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")


def _load_probe():
    """Load browser_service/probe.py the way test_wave21_probe_404_gate does."""
    pkg_name = "browser_service"
    saved_pkg = sys.modules.get(pkg_name)
    saved_probe = sys.modules.pop("browser_service.probe", None)
    saved_cfg = sys.modules.pop("browser_service.config", None)

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules[pkg_name] = pkg
    try:
        spec = importlib.util.spec_from_file_location("browser_service.probe", PROBE_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["browser_service.probe"] = mod
        spec.loader.exec_module(mod)
    finally:
        if saved_pkg is not None:
            sys.modules[pkg_name] = saved_pkg
        else:
            sys.modules.pop(pkg_name, None)
        sys.modules.pop("browser_service.probe", None)
        if saved_probe is not None:
            sys.modules["browser_service.probe"] = saved_probe
        if saved_cfg is not None:
            sys.modules["browser_service.config"] = saved_cfg
    return mod


probe = _load_probe()

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

# wonderbly-shaped client shell: nav junk links, an empty InstantSearch grid,
# and the ONLY PDP URLs on the page — escaped inside a <script> JSON island.
# A regex anchor scan counts those 2 and silently defeats the flag; BeautifulSoup
# treats script bodies as text and correctly counts 0.
ASTRO_SHELL_HTML = (
    "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
    "<title>Personalized Kids Books | Example</title>"
    "<script type='module' src='/_astro/InstantSearch.abc123.js'></script>"
    "</head><body><header><nav>"
    "<a href='/personalized-books/kids/age/0-3'>Kids 0-3</a>"
    "<a href='/cart'>Cart</a>"
    "<a href='/account/login'>Sign in</a>"
    "</nav></header><main>"
    "<div class='ais-InstantSearch'><div class='ais-Hits'></div></div>"
    "<script type='application/json' data-astro-props>"
    '{"faq": "See \\"<a href=\\"/personalized-products/when-you-were-born-book\\">Born</a>"'
    ' and "<a href=\\"/personalized-products/bedtime-for-name-book\\">Bedtime</a>"'
    "}</script>"
    + "<p>Editorial body copy padding the shell past the byte floor. </p>" * 60
    + "</main></body></html>"
)


def _ssr_grid_html(n_items: int = 9) -> str:
    """Server-rendered listing: real PDP anchors plus nav junk to ignore."""
    items = "".join(
        f"<a href='/products/test-product-{i}-slug'>Item {i}</a>" for i in range(n_items)
    )
    return (
        "<!DOCTYPE html><html><head><title>Shop</title></head><body><main>"
        + items
        + "<a href='https://www.wonderbly.com/products/absolute-product-widget'>Abs</a>"
        + "<a href='/cart'>Cart</a>"
        + "<a href='/account/login'>Login</a>"
        + "<a href='#top'>Top</a>"
        + "<a href='mailto:x@y.z'>Mail</a>"
        + "<a href='https://cdn.other-shop.com/products/cdn-item-thing'>CDN</a>"
        + "<p>Server-rendered category copy padding the page past the probe byte floor. </p>" * 40
        + "</main></body></html>"
    )


LISTING_URL = "https://www.wonderbly.com/personalized-books/kids/age/0-3"


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _Client:
    def __init__(self, status_code: int, text: str, **_kw):
        self._resp = _Resp(status_code, text)

    def get(self, url: str, **_kw):
        return self._resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ═════════════════════════════════════════════════════════════════════════════
# T1 — probe rungs report item_link_count
# ═════════════════════════════════════════════════════════════════════════════


class TestCountItemLinks:
    def test_astro_shell_script_embedded_anchors_count_zero(self):
        # THE load-bearing correctness case: the wonderbly signature. The only
        # PDP URLs on the page are escaped <a> strings inside a <script> body.
        assert probe._count_item_links(ASTRO_SHELL_HTML, LISTING_URL) == 0

    def test_ssr_grid_counts_pdp_anchors(self):
        # 9 relative + 1 absolute same-host PDP anchor.
        assert probe._count_item_links(_ssr_grid_html(), LISTING_URL) == 10

    def test_excludes_nav_junk_facets_and_cross_host(self):
        html = (
            "<html><body>"
            "<a href='/cart'>1</a><a href='/checkout'>2</a>"
            "<a href='/account/login'>3</a><a href='/search?q=x'>4</a>"
            "<a href='/personalized-books/kids/age/0-3?page=2'>self+facet</a>"
            "<a href='https://other.example.com/products/nope-product-x'>x</a>"
            "<a href='#grid'>5</a><a href='mailto:a@b.c'>6</a><a href='tel:+123'>7</a>"
            "</body></html>"
        )
        assert probe._count_item_links(html, LISTING_URL) == 0

    def test_returns_none_without_bs4(self, monkeypatch):
        # probe.py's lazy-import doctrine: the helper must degrade to
        # "unknown" (None), never 0, and probe.py must stay importable
        # where bs4 is not installed (module-level import would break the
        # importlib load itself — this test only passes if the import is
        # inside the helper).
        monkeypatch.setitem(sys.modules, "bs4", None)
        assert (
            probe._count_item_links("<html><a href='/products/a-b'></a></html>", LISTING_URL)
            is None
        )

    def test_empty_and_tiny_html_count_zero(self):
        assert probe._count_item_links("", LISTING_URL) == 0
        assert probe._count_item_links("<html></html>", LISTING_URL) == 0


class TestRungsReportItemLinkCount:
    def test_direct_http_success_carries_count(self, monkeypatch):
        monkeypatch.setattr(
            "httpx.Client", lambda **kw: _Client(200, ASTRO_SHELL_HTML)
        )
        out = probe._try_direct_http(LISTING_URL, timeout=10)
        assert out is not None and out["success"] is True
        assert out["item_link_count"] == 0

    def test_direct_http_ssr_grid_count_positive(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", lambda **kw: _Client(200, _ssr_grid_html()))
        out = probe._try_direct_http(LISTING_URL, timeout=10)
        assert out is not None and out["success"] is True
        assert out["item_link_count"] == 10

    def test_direct_http_404_rejection_has_no_count_key(self, monkeypatch):
        # A rejected rung's count is never consumed: the rejection dict stays
        # unchanged (missing key → unknown), the success dict carries it.
        monkeypatch.setattr(
            "httpx.Client", lambda **kw: _Client(404, ASTRO_SHELL_HTML)
        )
        out = probe._try_direct_http(LISTING_URL, timeout=10)
        assert out is not None and out["success"] is False
        assert "item_link_count" not in out

    def test_fingerprint_success_carries_count(self, monkeypatch):
        class _FakeSess:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, timeout=None):
                return _Resp(200, ASTRO_SHELL_HTML)

        monkeypatch.setattr(probe, "_fingerprint_session", lambda *a, **kw: _FakeSess())
        out = probe._try_fingerprint(LISTING_URL, profile="chrome124", timeout=10)
        assert out is not None and out["success"] is True
        assert out["item_link_count"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# T2 — listing_client_rendered stamp (probe_tools) + strategy flip (graph)
# ═════════════════════════════════════════════════════════════════════════════


def _advisory_payload(method="direct_http", count=0, body=142_745, extra=None):
    p = {
        "success": True,
        "method": method,
        "status_code": 200,
        "body_length": body,
        "item_link_count": count,
    }
    if extra:
        p.update(extra)
    return p


class TestListingClientRenderedStamp:
    """run_listing_probe_advisory stamps the yield verdict (always-on)."""

    def _run(self, monkeypatch, payload, failing_rungs=()):
        from agents.tools import probe_tools

        class _Resp:
            def __init__(self, ok):
                self._ok = ok

            def raise_for_status(self):
                return None

            def json(self):
                return payload if self._ok else {"success": False}

        def _post(url, json=None, timeout=None):
            return _Resp(json["method"] not in failing_rungs)

        monkeypatch.setattr(probe_tools.httpx, "post", _post)
        import src.geo as geo

        monkeypatch.setattr(geo, "detect_country", lambda u: "US")
        return probe_tools.run_listing_probe_advisory(
            "https://www.wonderbly.com/personalized-books/kids/age/0-3",
            {"http_method": "direct_http"},
            job_id=0,
        )

    def test_zero_anchors_rich_shell_stamps_client_rendered(self, monkeypatch):
        out = self._run(monkeypatch, _advisory_payload(count=0, body=142_745))
        assert out["listing_client_rendered"] is True
        assert out["client_rendered_reason"] == "http_rung_zero_item_links"
        assert out["item_link_count"] == 0

    def test_positive_anchor_count_not_client_rendered(self, monkeypatch):
        out = self._run(monkeypatch, _advisory_payload(count=10))
        assert out["listing_client_rendered"] is False
        assert out["client_rendered_reason"] == ""

    def test_missing_count_is_unknown_not_zero(self, monkeypatch):
        # Older browser-service payload: no item_link_count → unknown → False.
        p = _advisory_payload()
        p.pop("item_link_count")
        out = self._run(monkeypatch, p)
        assert out["listing_client_rendered"] is False
        assert out["item_link_count"] is None

    def test_small_body_zero_anchors_not_stamped(self, monkeypatch):
        # A tiny 0-anchor page is not evidence of a client-rendered grid.
        out = self._run(monkeypatch, _advisory_payload(count=0, body=8_000))
        assert out["listing_client_rendered"] is False

    def test_browser_winner_never_stamped(self, monkeypatch):
        # The stamp is the HTTP-starvation verdict; a browser rung winning is
        # the existing needs_browser path.
        out = self._run(
            monkeypatch,
            _advisory_payload(method="cloak_none", count=0),
            failing_rungs=("direct_http", "cloak_none"),
        )
        assert out["http_ok"] is False
        assert out["listing_client_rendered"] is False

    def test_jsonld_never_propagated(self, monkeypatch):
        # Landmine: _pdp_listing_swap's guard (graph.py:2124) reads this
        # dict's "jsonld" key — propagating the winner's jsonld would revive
        # a live-but-inert wave-39 veto and change swap behavior.
        out = self._run(
            monkeypatch, _advisory_payload(extra={"jsonld": [{"price": "12.99"}]})
        )
        assert "jsonld" not in out


def _flip_state(method="direct_http", listing=None):
    state = {
        "url": "https://www.wonderbly.com/p/when-you-were-born-book",
        "site_slug": "wonderbly-com",
        "input_mode": "list_page",
        "search_criteria": "https://www.wonderbly.com/personalized-books/kids/age/0-3",
        "probe_result": {
            "connectivity": {"method_that_worked": method},
            "anti_bot": {"detected": False},
        },
        "navigation_analysis": {},
    }
    if listing is not None:
        state["probe_result"]["listing_connectivity"] = listing
    return state


def _lcr_listing(method="direct_http"):
    return {
        "probed_url": "https://www.wonderbly.com/personalized-books/kids/age/0-3",
        "advisory": True,
        "method_that_worked": method,
        "http_ok": True,
        "browser_ok": False,
        "needs_browser": False,
        "blocked": False,
        "status_code": 200,
        "body_length": 142_745,
        "methods_tried": [method],
        "attempts": [],
        "item_link_count": 0,
        "listing_client_rendered": True,
        "client_rendered_reason": "http_rung_zero_item_links",
    }


class TestAnchorFloorStrategyFlip:
    """[wave-48b] The flip is unconditional — no env gate (wave-45b rule)."""

    def test_delenv_still_flips(self, monkeypatch):
        # The gate is gone: with the var DELETED the flip still fires. Under
        # the old flag-OFF contract this test asserted the opposite.
        monkeypatch.delenv("LISTING_ANCHOR_FLOOR", raising=False)
        from agents.graph import _derive_strategy

        assert _derive_strategy(_flip_state(listing=_lcr_listing()))["strategy"] == (
            "http_navigation"
        )

    def test_flips_http_requests_to_http_navigation(self):
        from agents.graph import _derive_strategy

        a = _derive_strategy(_flip_state(listing=_lcr_listing()))
        assert a["strategy"] == "http_navigation"
        assert "client_rendered" in a["access_recipe"]["evidence"]

    def test_evidence_stamp_rides_the_flip(self):
        from agents.graph import _derive_strategy

        a = _derive_strategy(_flip_state(listing=_lcr_listing()))
        assert a["strategy"] == "http_navigation"
        assert "client_rendered" in a["access_recipe"]["evidence"]

    def test_fingerprint_proven_pdp_never_flips(self):
        # job-524 revolve guard: a fingerprint win means plain HTTP was
        # REJECTED and TLS-impersonated HTTP answered — no upgrade to a
        # vanilla browser lane the probe never proved.
        from agents.graph import _derive_strategy

        a = _derive_strategy(
            _flip_state(
                method="fingerprint_chrome124",
                listing=_lcr_listing(method="fingerprint_chrome124"),
            )
        )
        assert a["strategy"] == "http_requests"

    def test_post_form_discovery_never_flips(self, monkeypatch):
        # [wave-48b H3] locumtenens replay contract: a form-POST→SSR listing
        # is HTTP-reachable via POST replay and http_navigation's phase 1
        # cannot replay the POST — the flip must not override the cascade's
        # deliberate http_requests pick. (setenv kept only so this test fails
        # for the RIGHT reason while the env gate still exists.)
        monkeypatch.setenv("LISTING_ANCHOR_FLOOR", "1")
        from agents.graph import _derive_strategy

        st = _flip_state(listing=_lcr_listing())
        st["navigation_analysis"] = {
            "rendering_verified": "ssr",
            "search": {"form_method": "POST", "action": "/search"},
        }
        assert _derive_strategy(st)["strategy"] == "http_requests"

    def test_not_stamped_no_flip(self):
        from agents.graph import _derive_strategy

        listing = _lcr_listing()
        listing["listing_client_rendered"] = False
        assert _derive_strategy(_flip_state(listing=listing))["strategy"] == (
            "http_requests"
        )

    def test_internal_api_never_flipped(self):
        # API evidence outranks — the count gate already decided with content
        # proof; the flip only ever touches http_requests.
        from agents.graph import _derive_strategy

        st = _flip_state(listing=_lcr_listing())
        st["navigation_analysis"] = {
            "data_source": "api",
            "api_endpoint": {
                "url": "https://www.wonderbly.com/api/search",
                "count": 26955,
                "items_per_page": 20,
            },
        }
        assert _derive_strategy(st)["strategy"] == "internal_api"


# ═════════════════════════════════════════════════════════════════════════════
# T3 — identical_last_page telemetry (src/listing_discovery.py)
# ═════════════════════════════════════════════════════════════════════════════


def _dup_fetch(calls_box: list, page_plans: list):
    """fetch_page serving page_plans[i] (list of hrefs) for call i.

    Past the plan: empty page (genuine end-of-catalog shape).
    """
    from bs4 import BeautifulSoup

    def fetch_page(url, min_tier=0):
        calls_box.append(url)
        i = len(calls_box) - 1
        if i >= len(page_plans):
            return BeautifulSoup("<html><body></body></html>", "html.parser"), 200
        links = "".join(f'<a href="{h}">i</a>' for h in page_plans[i])
        return BeautifulSoup(f"<html><body>{links}</body></html>", "html.parser"), 200

    fetch_page.min_tier_floor = 0
    fetch_page.tiers_total = 1
    return fetch_page


def _extract_all(soup):
    return [a["href"] for a in soup.find_all("a")]


class TestIdenticalLastPageTelemetry:
    def test_duplicate_last_page_flagged(self):
        # The job-1082 signature: page 2 serves the SAME links as page 1
        # (client shell re-serving page 1 for every ?page=N).
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _dup_fetch(calls, [
            ["https://site.test/p/a", "https://site.test/p/b"],
            ["https://site.test/p/a", "https://site.test/p/b"],
        ])
        urls, meta = discover_listing_urls(fetch, ["https://site.test/c/x"], _extract_all)
        assert meta["stop_reason"] == "no_new_items", "vocabulary unchanged"
        assert meta["identical_last_page"] is True
        assert meta["discovered_urls"] == 2

    def test_fresh_pages_not_flagged(self):
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _dup_fetch(calls, [
            ["https://site.test/p/a", "https://site.test/p/b"],
            ["https://site.test/p/c", "https://site.test/p/d"],
        ])
        _, meta = discover_listing_urls(fetch, ["https://site.test/c/x"], _extract_all)
        assert meta["identical_last_page"] is False

    def test_subset_overlap_page_not_flagged(self):
        # Page 2 all-seen but a DIFFERENT link set ([a] vs [a,b]) — genuine
        # overlap, not the identical-shell signature. Pins links-tuple
        # comparison, not "new count == 0".
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _dup_fetch(calls, [
            ["https://site.test/p/a", "https://site.test/p/b"],
            ["https://site.test/p/a"],
        ])
        _, meta = discover_listing_urls(fetch, ["https://site.test/c/x"], _extract_all)
        assert meta["stop_reason"] == "no_new_items"
        assert meta["identical_last_page"] is False

    def test_prev_links_reset_per_listing_url(self):
        # Listing 1 starves identical (True), listing 2 ends short (False):
        # the single meta value must reflect the LAST listing, and listing 2's
        # page 1 must never compare against listing 1's tail.
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _dup_fetch(calls, [
            ["https://site.test/p/a", "https://site.test/p/b"],
            ["https://site.test/p/a", "https://site.test/p/b"],  # listing 1 dup
            ["https://site.test/p/c", "https://site.test/p/d"],  # listing 2 p1
        ])
        _, meta = discover_listing_urls(
            fetch,
            ["https://site.test/c/x", "https://site.test/c/y"],
            _extract_all,
        )
        # listing 2 page 2 = empty → short_page; identical flag False.
        assert meta["stop_reason"] == "short_page"
        assert meta["identical_last_page"] is False

    def test_requests_template_copies_the_key_through(self):
        # The key dies at template assembly if the coverage-block whitelist
        # (contract §1) doesn't carry it — requests_scraper is the only
        # template that consumes discovery_meta.
        src = open(
            os.path.join(ROOT, "templates", "requests_scraper.py"), encoding="utf-8"
        ).read()
        assert '"identical_last_page": discovery_meta.get("identical_last_page"' in src

    def test_contract_allows_the_key(self):
        # Contract moved in the same commit: identical_last_page is a legal
        # extra in discovery_coverage (§1 telemetry, never a fail-set input).
        src = open(
            os.path.join(ROOT, "tests", "test_discovery_ladder.py"), encoding="utf-8"
        ).read()
        assert '"identical_last_page"' in src


# ═════════════════════════════════════════════════════════════════════════════
# T4 — known-yield gap: checker + routing touchpoints
# ═════════════════════════════════════════════════════════════════════════════


def _kyg_state(**over):
    st = {
        "job_id": 0,
        "site_slug": "",
        "input_mode": "list_page",
        "url": "https://www.wonderbly.com/p/when-you-were-born-book",
        "search_criteria": "https://www.wonderbly.com/personalized-books/kids/age/0-3",
        "scope": "firstn",
        "scope_value": "10",
        "probe_result": {
            "connectivity": {"method_that_worked": "direct_http"},
            "anti_bot": {"detected": False},
            "listing_connectivity": {
                "method_that_worked": "direct_http",
                "http_ok": True,
                "needs_browser": False,
                "body_length": 142_745,
                "item_link_count": 0,
                "listing_client_rendered": True,
                "client_rendered_reason": "http_rung_zero_item_links",
            },
        },
        "navigation_analysis": {},
        "page_type": "product",
    }
    st.update(over)
    return st


def _kyg_report(stop="no_new_items", found=2, **over):
    r = {
        "overall_assessment": "PASS",
        "confidence_score": 0.8,
        "successful_extractions": found,
        "discovery_coverage": {"stop_reason": stop, "found": found, "discovered_urls": found},
    }
    r.update(over)
    return r


class TestKnownYieldGapIssue:
    """[wave-48b] The checker runs unconditionally — no env gate."""

    def test_delenv_checker_still_runs(self, monkeypatch):
        # The gate is gone: with the var DELETED the checker still arms.
        # Under the old flag-OFF contract this test asserted the opposite.
        monkeypatch.delenv("LISTING_YIELD_GATE", raising=False)
        from agents.nodes.route_after_testing import known_yield_gap_issue

        issue = known_yield_gap_issue(_kyg_state(), _kyg_report())
        assert issue is not None
        assert issue["source"] == "probe"

    def test_source_a_probe_arms_and_fires(self):
        from agents.nodes.route_after_testing import known_yield_gap_issue

        issue = known_yield_gap_issue(_kyg_state(), _kyg_report(found=2))
        assert issue is not None
        assert issue["severity"] == "high"
        assert issue["field"] == "discovery"
        assert issue["source"] == "probe"
        assert issue["expected"] == 10
        assert issue["discovered"] == 2
        assert issue["description"].startswith("Deterministic check:")

    def test_fingerprint_proven_pdp_declines_source_a(self, monkeypatch):
        # [wave-48b H2] job-524 revolve: the listing probe's rung list rides
        # the PDP winner — a fingerprint_* PDP win makes pdp_http the
        # fingerprint rung, and _is_http_method counts it as HTTP-flavoured,
        # so a ≥20KB 0-anchor shell there stamps listing_client_rendered.
        # Steering discovery to a vanilla browser lane then repeats the
        # documented 2×1800s burn. Source A must decline; Source B (an
        # HONEST browser traverse) stays armed — the traverse itself proves
        # the browser lane reaches.
        monkeypatch.setenv("LISTING_YIELD_GATE", "1")  # inert post-removal
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st["probe_result"]["connectivity"] = {
            "method_that_worked": "fingerprint_chrome124"
        }
        assert known_yield_gap_issue(st, _kyg_report(found=2)) is None

    def test_stealth_proven_pdp_declines_source_a(self, monkeypatch):
        # Same guard for the stealth (uc_chrome/cloak) family.
        monkeypatch.setenv("LISTING_YIELD_GATE", "1")  # inert post-removal
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st["probe_result"]["connectivity"] = {"method_that_worked": "cloak_residential"}
        assert known_yield_gap_issue(st, _kyg_report(found=2)) is None

    def test_source_b_still_arms_when_pdp_fingerprint_proven(self, monkeypatch):
        # The H2 guard is Source-A-scoped: an honest browser traverse that
        # REACHED the listing is positive evidence the browser lane works.
        monkeypatch.setenv("LISTING_YIELD_GATE", "1")  # inert post-removal
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st["probe_result"] = {
            "connectivity": {"method_that_worked": "fingerprint_chrome124"},
            "listing_connectivity": {
                "method_that_worked": "direct_http",
                "http_ok": True,
                "needs_browser": False,
                "body_length": 142_745,
                "item_link_count": 0,
                "listing_client_rendered": True,
            },
        }
        st["navigation_analysis"] = {
            "traverse_reached": True,
            "item_links": {
                "url_examples": [
                    f"https://www.wonderbly.com/personalized-products/book-{i}"
                    for i in range(20)
                ]
            },
        }
        issue = known_yield_gap_issue(st, _kyg_report(found=2))
        assert issue is not None
        assert issue["source"] == "traverse"

    def test_source_a_no_gap_when_scope_met(self):
        from agents.nodes.route_after_testing import known_yield_gap_issue

        assert known_yield_gap_issue(_kyg_state(), _kyg_report(found=10)) is None

    def test_url_list_never_arms(self):
        from agents.nodes.route_after_testing import known_yield_gap_issue

        assert known_yield_gap_issue(_kyg_state(input_mode="url_list"), _kyg_report()) is None

    def test_coverage_fail_stop_reason_ladder_owns_it(self):
        from agents.nodes.route_after_testing import known_yield_gap_issue

        assert known_yield_gap_issue(
            _kyg_state(), _kyg_report(stop="navigate_error")
        ) is None

    def test_checkpoint_reused_never_fires(self):
        # Fresh zero discovered on a rescued run — the yield lives in the
        # checkpoint, not the coverage block. Not starvation evidence.
        from agents.nodes.route_after_testing import known_yield_gap_issue

        assert known_yield_gap_issue(
            _kyg_state(), _kyg_report(stop="checkpoint_reused")
        ) is None

    def test_non_firstn_degenerates_to_zero_yield_only(self):
        # scope_target()=1 without firstn — the trigger becomes discovered<1.
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st.pop("scope"), st.pop("scope_value")
        assert known_yield_gap_issue(st, _kyg_report(found=2)) is None
        assert known_yield_gap_issue(st, _kyg_report(found=0)) is not None

    def test_source_b_traverse_measured_arms(self):
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st["probe_result"] = {
            "connectivity": {"method_that_worked": "direct_http"},
            "anti_bot": {"detected": False},
        }
        st["navigation_analysis"] = {
            "traverse_reached": True,
            "item_links": {
                "url_examples": [
                    f"https://www.wonderbly.com/personalized-products/book-{i}"
                    for i in range(20)
                ]
            },
        }
        issue = known_yield_gap_issue(st, _kyg_report(found=2))
        assert issue is not None
        assert issue["source"] == "traverse"
        assert issue["expected"] == 10  # min(len(url_examples)=20, scope=10)

    def test_source_b_blocked_without_honest_reached(self):
        # discovery.listing_reached is fabricated True by the fallback — the
        # gate keys on traverse_reached, which the fallback does not set.
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st["probe_result"] = {"connectivity": {"method_that_worked": "direct_http"}}
        st["navigation_analysis"] = {
            "discovery": {"listing_reached": True},  # fabricated
            "item_links": {"url_examples": [
                f"https://www.wonderbly.com/personalized-products/b-{i}" for i in range(20)
            ]},
        }
        assert known_yield_gap_issue(st, _kyg_report()) is None

    def test_source_b_blocked_below_five_examples(self):
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st["probe_result"] = {"connectivity": {"method_that_worked": "direct_http"}}
        st["navigation_analysis"] = {
            "traverse_reached": True,
            "item_links": {"url_examples": [
                "https://www.wonderbly.com/personalized-products/born-book",
                "https://www.wonderbly.com/personalized-products/bedtime-book",
            ]},
        }
        assert known_yield_gap_issue(st, _kyg_report()) is None

    def test_source_b_wrong_site_notes_block(self):
        from agents.nodes.route_after_testing import known_yield_gap_issue

        st = _kyg_state()
        st["probe_result"] = {"connectivity": {"method_that_worked": "direct_http"}}
        st["navigation_analysis"] = {
            "traverse_reached": True,
            "traverse_notes": "aborted: wrong-site navigation detected",
            "item_links": {"url_examples": [
                f"https://www.wonderbly.com/personalized-products/b-{i}" for i in range(20)
            ]},
        }
        assert known_yield_gap_issue(st, _kyg_report()) is None

    def test_int_coverage_read_never_raises(self):
        # discovered_urls is an INT in the templates — len() on it would
        # TypeError inside the router (the _volume_gap latent bug).
        from agents.nodes.route_after_testing import known_yield_gap_issue

        r = _kyg_report()
        r["discovery_coverage"] = {"stop_reason": "no_new_items", "discovered_urls": 2}
        r.pop("successful_extractions")
        issue = known_yield_gap_issue(_kyg_state(), r)
        assert issue is not None and issue["discovered"] == 2


class TestEnvVarIsDead:
    """[wave-48b] The old vars are inert in BOTH directions — these tests
    would have passed the OPPOSITE way under the old flag-OFF contract."""

    def _rat_mod(self):
        # agents/nodes/__init__ re-exports the NODE function under the same
        # name, shadowing the module — resolve the module via sys.modules.
        import sys

        import agents.nodes.route_after_testing  # noqa: F401  (ensures import)

        return sys.modules["agents.nodes.route_after_testing"]

    def test_env_zero_checker_still_fires(self, monkeypatch):
        monkeypatch.setenv("LISTING_YIELD_GATE", "0")
        from agents.nodes.route_after_testing import known_yield_gap_issue

        assert known_yield_gap_issue(_kyg_state(), _kyg_report()) is not None

    def test_env_one_changes_nothing(self, monkeypatch):
        monkeypatch.setenv("LISTING_YIELD_GATE", "1")
        from agents.nodes.route_after_testing import known_yield_gap_issue

        issue = known_yield_gap_issue(_kyg_state(), _kyg_report())
        assert issue is not None and issue["expected"] == 10

    def test_anchor_floor_zero_still_flips(self, monkeypatch):
        monkeypatch.setenv("LISTING_ANCHOR_FLOOR", "0")
        from agents.graph import _derive_strategy

        assert _derive_strategy(_flip_state(listing=_lcr_listing()))["strategy"] == (
            "http_navigation"
        )

    def test_gate_symbols_are_gone(self):
        import agents.graph as graph_mod

        assert not hasattr(graph_mod, "_listing_anchor_floor_enabled"), (
            "the env gate helper must be DELETED, not stubbed True (wave-45b: "
            "the reviewed merge is the switch)"
        )
        assert not hasattr(self._rat_mod(), "_listing_yield_gate_enabled")

    def test_env_names_survive_only_in_comments(self):
        # The wave-45b history comments NAME the dead var (that is the
        # record); nothing else may reference it — every surviving mention
        # must be a comment line, never a read.
        for rel in (
            ("webapp", "agents", "graph.py"),
            ("webapp", "agents", "nodes", "route_after_testing.py"),
            ("webapp", "scraper", "tasks.py"),
        ):
            src = open(os.path.join(ROOT, *rel), encoding="utf-8").read()
            for ln in src.splitlines():
                if "LISTING_ANCHOR_FLOOR" in ln or "LISTING_YIELD_GATE" in ln:
                    assert ln.lstrip().startswith("#"), (rel, ln)


class TestKygBeltAndClassify:
    """The distinct report key + belt narrowing + classify placement."""

    def test_belt_uses_narrowed_helper(self):
        # Source pin: the belt must route through the narrowing helper, not
        # presence-read the dict (a kyg-shaped annotation riding
        # deterministic_gate would otherwise hijack the CLI-contract bounce).
        src = open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "route_after_testing.py"),
            encoding="utf-8",
        ).read()
        assert src.count("_gate_is_contract_bad") >= 2, (
            "belt still presence-reads deterministic_gate — a known_yield_gap "
            "stamp there would hijack the CLI-contract bounce"
        )

    def test_gate_string_stamp_is_contract(self):
        # Legacy pin (tests/test_cli_contract.py:329-333): a plain string
        # stamp from the tester stays contract-bad.
        from agents.nodes.route_after_testing import _gate_is_contract_bad

        assert _gate_is_contract_bad("ladder") is True

    def test_gate_dict_with_violation_kind_is_contract(self):
        from agents.nodes.route_after_testing import _gate_is_contract_bad

        assert _gate_is_contract_bad({"violation_kind": "call"}) is True

    def test_gate_dict_without_violation_kind_is_not_contract(self):
        # A kyg-shaped diagnostic dict is NOT a CLI-contract violation.
        from agents.nodes.route_after_testing import _gate_is_contract_bad

        assert _gate_is_contract_bad({"known_yield_gap": "x"}) is False

    def test_gate_falsy_is_not_contract(self):
        from agents.nodes.route_after_testing import _gate_is_contract_bad

        assert _gate_is_contract_bad(None) is False
        assert _gate_is_contract_bad("") is False

    def test_classify_kyg_http_requests_is_strategy(self):
        from agents.nodes.route_after_testing import classify_test_failure

        report = _kyg_report()
        report["known_yield_gap"] = "discovered 2 of >= 10 (source: probe)"
        action, reason = classify_test_failure(report, "http_requests")
        assert action == "strategy"
        assert "known yield gap" in reason

    def test_classify_kyg_two_arg_caller_is_strategy(self):
        # The load-bearing caller: _decide_strategy's classify has NO state.
        from agents.nodes.route_after_testing import classify_test_failure

        report = _kyg_report()
        report["known_yield_gap"] = "discovered 2 of >= 10 (source: probe)"
        action, _ = classify_test_failure(report, "http_requests")
        assert action == "strategy"

    def test_classify_kyg_throttled_outranks(self):
        # A throttled run is UNPROVEN coverage — never a strategy switch.
        from agents.nodes.route_after_testing import classify_test_failure

        report = _kyg_report(stop="navigate_throttled")
        report["known_yield_gap"] = "x"
        action, _ = classify_test_failure(report, "http_requests")
        assert action == "retest"

    def test_classify_kyg_gateway_down_outranks(self):
        from agents.nodes.route_after_testing import classify_test_failure

        report = _kyg_report(stop="navigate_unavailable")
        report["known_yield_gap"] = "x"
        action, _ = classify_test_failure(report, "http_requests")
        assert action == "park_unhealthy"

    def test_classify_kyg_non_http_requests_falls_through(self):
        # Only http_requests has a flip target worth burning a cycle for.
        from agents.nodes.route_after_testing import classify_test_failure

        report = _kyg_report()
        report["known_yield_gap"] = "x"
        action, _ = classify_test_failure(report, "playwright")
        assert action != "strategy"


class TestKygDecideStrategy:
    """THE test that proves the fix works: a kyg PASS records the prior
    strategy so the escalation ladder actually walks."""

    def _state(self):
        return {
            "job_id": 0,
            "site_slug": "",
            "input_mode": "list_page",
            "url": "https://www.wonderbly.com/p/x",
            "search_criteria": "https://www.wonderbly.com/personalized-books/kids/age/0-3",
            "probe_result": {
                "connectivity": {"method_that_worked": "direct_http"},
                "anti_bot": {"detected": False},
            },
            "navigation_analysis": {},
            "scraper_analysis": {"strategy": "http_requests"},
            "strategies_tried": [],
            "test_report": {
                "overall_assessment": "PASS",
                "confidence_score": 0.8,
                "successful_extractions": 2,
                "known_yield_gap": "discovered 2 of >= 10 (source: probe)",
                "discovery_coverage": {"stop_reason": "no_new_items", "found": 2},
            },
        }

    def test_kyg_pass_records_prior_strategy_and_escalates(self):
        import sys

        from agents.graph import _decide_strategy

        # _decide_strategy routes via Command(goto=..., update=...).
        result = _decide_strategy(self._state())
        upd = result.update
        tried = upd.get("strategies_tried") or []
        assert any(
            (t.get("strategy") if isinstance(t, dict) else t) == "http_requests"
            for t in tried
        ), (
            "kyg PASS must record http_requests as tried so the ladder "
            "escalates — as drafted it re-picked http_requests forever"
        )
        assert (upd.get("scraper_analysis") or {}).get("strategy") == "http_navigation"

    def test_plain_pass_still_re_picks_without_kyg(self):
        # Without the stamp the historical behavior is untouched (PASS is not
        # a failed cycle) — the gate change must not widen beyond kyg.
        from agents.graph import _decide_strategy

        st = self._state()
        st["test_report"].pop("known_yield_gap")
        result = _decide_strategy(st)
        assert not (result.update.get("strategies_tried") or [])
        assert (result.update.get("scraper_analysis") or {}).get(
            "strategy"
        ) == "http_requests"


class TestKygExhaustedRedirectVeto:
    def _state(self):
        return {
            "site_slug": "wonderbly-com",
            "input_mode": "list_page",
            "url": "https://www.wonderbly.com/p/x",
            "scope": "firstn",
            "scope_value": "10",
            "page_type": "product",
        }

    def _rat_mod(self):
        # agents/nodes/__init__ re-exports the NODE function under the same
        # name, shadowing the module — resolve the module via sys.modules.
        import sys

        import agents.nodes.route_after_testing  # noqa: F401  (ensures import)

        return sys.modules["agents.nodes.route_after_testing"]

    def test_kyg_blocks_the_rescue(self, monkeypatch, tmp_path):
        from agents.nodes.route_after_testing import _exhausted_cleanup_redirect

        # Real items on disk + all other vetoes clean: pre-wave-48 this
        # rescued to run_execution, re-applying what the override veto
        # blocked. kyg must be the seventh veto.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            self._rat_mod(), "_scraper_has_real_items", lambda s, min_count=1: True
        )
        out = _exhausted_cleanup_redirect(
            self._state(), "cleanup", known_yield_gap=True
        )
        assert out == "cleanup"

    def test_without_kyg_rescue_still_fires(self, monkeypatch, tmp_path):
        from agents.nodes.route_after_testing import _exhausted_cleanup_redirect

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            self._rat_mod(), "_scraper_has_real_items", lambda s, min_count=1: True
        )
        out = _exhausted_cleanup_redirect(self._state(), "cleanup")
        assert out == "run_execution"

    def test_signature_has_seventh_veto(self):
        import inspect

        from agents.nodes.route_after_testing import _exhausted_cleanup_redirect

        assert "known_yield_gap" in inspect.signature(_exhausted_cleanup_redirect).parameters


class TestTraverseHonestyPersistence:
    def test_analysis_carries_honest_signals(self):
        # browser_traverse's analysis dict must persist what the artifact
        # never carried: the traverse's own reached verdict (discovery.
        # listing_reached is fabricated True by the fallback), its notes,
        # and the wrong-site abort flag.
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8").read()
        assert '"traverse_reached"' in src
        assert '"traverse_notes"' in src
        assert '"wrong_site_abort"' in src
        # ...and the checker reads the honest key, not the fabricated one.
        rat_src = open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "route_after_testing.py"),
            encoding="utf-8",
        ).read()
        assert "traverse_reached" in rat_src


# ═════════════════════════════════════════════════════════════════════════════
# T6 — [SCOPE-SHORTFALL] marker on the job record + targeted retry strip
# ═════════════════════════════════════════════════════════════════════════════


class TestScopeShortfallRow:
    def test_shortfall_produces_row(self):
        from scraper.tasks import _scope_shortfall_row

        row = _scope_shortfall_row("firstn", "10", 2)
        assert row.startswith("[SCOPE-SHORTFALL]")
        assert "asked 10" in row and "shipped 2" in row

    def test_scope_met_no_row(self):
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("firstn", "10", 10) == ""
        assert _scope_shortfall_row("firstn", "10", 12) == ""

    def test_zero_shipped_no_row(self):
        # A zero-item run already fails loudly elsewhere; the marker is for
        # PARTIAL starvation (job 1082: 2 of 10 shipped as COMPLETED).
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("firstn", "10", 0) == ""

    def test_non_firstn_no_row(self):
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("all", "", 2) == ""
        assert _scope_shortfall_row("", "", 2) == ""

    def test_garbage_values_no_row(self):
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("firstn", "abc", 2) == ""
        assert _scope_shortfall_row("firstn", None, None) == ""


class TestScopeShortfallWiring:
    def test_finalize_appends_row_unconditional(self):
        # [wave-48b] Source pins: the append site sits after the ground-truth
        # product_count read, NO env gate in front of it.
        src = open(
            os.path.join(ROOT, "webapp", "scraper", "tasks.py"), encoding="utf-8"
        ).read()
        i = src.find("job.product_count = len(successful)")
        assert i != -1, "ground-truth product_count site moved"
        window = src[i: i + 2000]
        assert "_scope_shortfall_row(" in window
        assert "_scope_shortfall_row" in src[:i]  # row text lives in the helper

    def test_retry_seed_strips_only_shortfall_rows(self):
        # Targeted strip: [SCOPE-SHORTFALL] lines never reach the retry
        # agents' user_notes; [FIELD-MAP] audit rows KEEP flowing.
        src = open(
            os.path.join(ROOT, "webapp", "scraper", "tasks.py"), encoding="utf-8"
        ).read()
        assert '"[SCOPE-SHORTFALL]" not in ln' in src, (
            "user_notes seed must drop [SCOPE-SHORTFALL] lines only"
        )


class TestScopeShortfallRestartStrip:
    """[wave-48b H5] job_restart copies the old job's notes into the new row
    (views.py). A [SCOPE-SHORTFALL] row is a VERDICT about the parent run —
    inherited onto the re-run's own row it reads as a fresh lie, so restart
    strips exactly those lines ([FIELD-MAP] audit rows keep flowing)."""

    def test_strip_helper(self):
        from scraper.views import _strip_scope_shortfall_notes

        notes = (
            "[FIELD-MAP] keep me\n"
            "[SCOPE-SHORTFALL] asked 10, shipped 2\n"
            "plain line"
        )
        out = _strip_scope_shortfall_notes(notes)
        assert "[SCOPE-SHORTFALL]" not in out
        assert "[FIELD-MAP] keep me" in out
        assert "plain line" in out

    def test_strip_helper_none_and_empty(self):
        from scraper.views import _strip_scope_shortfall_notes

        assert _strip_scope_shortfall_notes(None) == ""
        assert _strip_scope_shortfall_notes("") == ""
        assert (
            _strip_scope_shortfall_notes("only\n[SCOPE-SHORTFALL] asked 3, shipped 1")
            == "only"
        )

    def test_job_restart_routes_notes_through_the_strip(self):
        src = open(
            os.path.join(ROOT, "webapp", "scraper", "views.py"), encoding="utf-8"
        ).read()
        i = src.find("def job_restart(")
        assert i != -1, "job_restart moved"
        assert "_strip_scope_shortfall_notes" in src[i: i + 9000], (
            "job_restart must strip inherited [SCOPE-SHORTFALL] rows from the "
            "copied notes"
        )
