"""[job-58 birkenstock] Blocked-listing misread as empty catalog + no retry.

The failure: the tester's run discovered 15 URLs and extracted 5/5 (PASS
0.95); the execution run 90 seconds later fetched the SAME listing, got a
200-but-blocked response (Akamai challenge / consent wall served with HTTP
200), selected zero product links, and the discovery loop classified that as
``short_page`` — "genuine end of results". Phase 1 "succeeded" with 0 URLs in
1.65s → 0 items → the zero-item finalize gate failed the job. Nothing retried.

Layers fixed here:

1. ``templates/requests_scraper.py`` — a zero-URL discovery with no hard
   ``navigate_error`` is reclassified to ``empty_first_page`` (a real
   catalog-end requires having seen items), and a one-shot wrapper retries
   the whole enumeration after a backoff (block windows are often shorter
   than the gap between test-phase runs).
2. ``templates/http_navigation_scraper.py`` — same reclassification at each
   discovery path's return + an ``empty_first_page`` entry (FAIL class) in
   ``_STOP_REASON_PRIORITY`` so aggregation carries it.
3. ``webapp/agents/nodes/route_after_testing.py`` — ``empty_first_page``
   joins the Tier-1 coverage-FAIL stop-reason set.
4. ``src/http_fetch.py`` + template wiring [job-62 birkenstock re-run] — the
   prod re-run showed the 45s retry re-hitting the same burned IP because
   code_writer had stripped the draft's proxy ladder ("analysis says direct
   works"). The fetch machinery now lives in a shared module the writer
   cannot strip, and the discovery loop escalates the proxy tier on SOFT
   blocks (200-but-zero-links — invisible to ``is_banned``) with a floor so
   Phase 2 reuses the tier that unblocked Phase 1.
5. ``src/listing_discovery.py`` + template rewiring [job-65 citybeach] — the
   prod autopsy showed code_writer deleted the entire zero-links escalation
   branch while keeping the counter: the draft still EMITTED
   ``soft_block_escalations`` but no code path could increment it. The whole
   loop now lives in a shared module (same defense as layer 4); the writer
   adapts only ``_extract_listing_links`` + data constants. The module also
   adds the JSON-LD ``ItemList`` fallback (hidden-SSR listings embed their
   item URLs there while the visible grid hydrates client-side) and logs
   anchors-served / usable-links / new separately so "0 anchors served" and
   "300 anchors, none matching" stop being indistinguishable.

The requests-template halves are tested against ``src.listing_discovery``
directly (stubbed ``fetch_page`` + patched module ``time``) and the
templates statically (presence/shape), mirroring tests/test_discovery_ladder.py's
split.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

TEMPLATES = os.path.join(ROOT, "templates")
REQ = os.path.join(TEMPLATES, "requests_scraper.py")
NAV = os.path.join(TEMPLATES, "http_navigation_scraper.py")


# ─── namespace harness: exec-extract the nav-template discovery functions ────


def _grab(src: str, name: str) -> str:
    m = re.search(
        rf"^def {name}\(.*?(?=^def |\Z)", src, re.MULTILINE | re.DOTALL
    )
    assert m, f"function {name} not found"
    return m.group(0)


class _FakeTime:
    def __init__(self):
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return 0.0  # deadline never fires

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)


class _FakeLink:
    def __init__(self, href: str):
        self._href = href

    def get(self, _k, default=None):
        return self._href


class _FakeScript:
    def __init__(self, payload: dict):
        self.string = json.dumps(payload)


class _FakeSoup:
    """Listing page: ``select()`` returns the anchors the draft's selector
    matches; ``find_all('a')`` returns EVERY anchor served (the diagnostic
    split — a page can serve 300 anchors, none product-shaped);
    ``find_all('script', type='application/ld+json')`` returns JSON-LD."""

    def __init__(self, product_hrefs, anchors=None, jsonld_payloads=None):
        self._product = product_hrefs
        self._anchors = product_hrefs if anchors is None else anchors
        self._jsonld = [_FakeScript(p) for p in (jsonld_payloads or [])]

    def select(self, _sel: str) -> list:
        return [_FakeLink(h) for h in self._product]

    def find_all(self, name, **_kwargs) -> list:
        if name == "a":
            return [_FakeLink(h) for h in self._anchors]
        if name == "script":
            return self._jsonld
        return []


def _default_extract(soup) -> list:
    """Mirror the template's _extract_listing_links: select, absolutize."""
    out = []
    for link in soup.select(".prod"):
        href = link.get("href", "")
        out.append(href if href.startswith("http") else "https://site.test" + href)
    return out


def _discover(
    fetch_results: list,
    retry_delay: int = 45,
    listings: list | None = None,
    url_filter=None,
    extract_fn=None,
    **cfg,
) -> SimpleNamespace:
    """Run ``discover_listing_urls_with_retry`` against a stubbed fetch_page.

    ``fetch_results``: queue of entries — ``list[str]`` (product hrefs on the
    page), ``dict`` ({product, anchors, jsonld}), or None (fetch failure →
    navigate_error). An exhausted queue yields empty pages.
    ``listings``: override the listing URL list (escalation tests use a
    single listing so tier-call sequences are exact — a second listing
    re-escalates after the first short-pages, by design).
    """
    ld = importlib.import_module("src.listing_discovery")
    fake_time = _FakeTime()
    old_time = ld.time
    ld.time = fake_time

    queue = list(fetch_results)
    tier_calls: list[int] = []

    def fetch_page(_url, min_tier=0):
        tier_calls.append(min_tier)
        r = queue.pop(0) if queue else {}
        if r is None:
            return None
        if isinstance(r, dict):
            soup = _FakeSoup(
                r.get("product", []),
                anchors=r.get("anchors"),
                jsonld_payloads=r.get("jsonld"),
            )
        else:
            soup = _FakeSoup(r)
        return soup, 200

    # Closure attributes the loop reads (mirrors src/http_fetch.py): the
    # escalation bound and the floor the loop raises once a tier works.
    fetch_page.min_tier_floor = 0
    fetch_page.tiers_total = 3  # none → datacenter → residential
    fetch_page.tier_calls = tier_calls

    try:
        urls, meta = ld.discover_listing_urls_with_retry(
            fetch_page,
            listings or ["https://site.test/de/a/", "https://site.test/de/b/"],
            extract_fn or _default_extract,
            page_param="page",
            retry_delay_s=retry_delay,
            deadline_s=cfg.pop("deadline_s", 300),
            max_pages=cfg.pop("max_pages", None),
            url_filter=url_filter,
            **cfg,
        )
    finally:
        ld.time = old_time

    return SimpleNamespace(
        urls=urls, meta=meta, fetch_page=fetch_page, time=fake_time,
        tier_calls=tier_calls,
    )


# ─── behavioral: listing discovery (job-58/62 signatures) ────────────────────


class TestZeroUrlReclassification:
    def test_blocked_zero_url_run_is_empty_first_page_and_retried(self):
        """The job-58 signature: every listing fetch 'succeeds' but yields zero
        product links → empty_first_page, plus exactly one 45s retry."""
        h = _discover([[], [], [], []])  # both listings × 2 attempts, all empty
        assert h.urls == []
        assert h.meta["stop_reason"] == "empty_first_page"
        assert h.meta["retried_empty_discovery"] is True
        assert h.time.sleeps == [45]

    def test_zero_urls_without_retry_delay_disabled(self):
        """Retry disabled (delay 0) → single pass, still reclassified."""
        h = _discover([[], []], retry_delay=0)
        assert h.urls == []
        assert h.meta["stop_reason"] == "empty_first_page"
        assert "retried_empty_discovery" not in h.meta
        assert h.time.sleeps == []

    def test_escalation_recovers_without_paying_the_retry(self):
        """[job-62 birkenstock] Page 1 blocked at tier 0 (200, zero links) but
        the datacenter tier serves the listing: escalation recovers discovery
        in the SAME pass — no 45s retry, no empty_first_page. This is the path
        the stripped draft lacked entirely."""
        page1 = ["/de/p/first_1.html", "/de/p/second_2.html"]
        h = _discover([[], page1, []], listings=["https://site.test/de/a/"])
        assert len(h.urls) == 2
        assert h.meta["stop_reason"] != "empty_first_page"
        assert h.meta["soft_block_escalations"] == 1
        assert h.time.sleeps == []  # retry path never entered
        assert h.tier_calls == [0, 1, 1]  # ladder climbed, then floor held

    def test_genuine_end_after_items_keeps_short_page_no_retry(self):
        """A real catalog-end requires having seen items: listing A yields
        products on page 1 then thins out on page 2 → short_page, PASS, and
        the retry path is never entered."""
        h = _discover(
            [
                ["/de/p/a1_1.html", "/de/p/a2_2.html"],  # page 1
                [],                                      # page 2 (thin)
            ],
            listings=["https://site.test/de/a/"],
        )
        assert len(h.urls) == 2
        assert h.meta["stop_reason"] == "short_page"
        assert h.time.sleeps == []  # healthy run pays nothing

    def test_one_blocked_listing_does_not_poison_a_working_one(self):
        """Listing A blocked-empty, listing B finds URLs → partial coverage
        succeeds (urls non-empty → no retry); the T2.1 lesson: never fail the
        run that found items. The per-listing emptiness stays visible in the
        last-wins stop_reason but NOT as empty_first_page."""
        h = _discover([
            [],                                       # listing A tier 0: blocked
            [],                                       # listing A tier 1: blocked
            [],                                       # listing A tier 2: blocked
            ["/de/p/b1_1.html", "/de/p/b2_2.html"],   # listing B page 1
            [],                                       # listing B page 2
        ])
        assert len(h.urls) == 2
        assert h.meta["stop_reason"] != "empty_first_page"
        assert h.time.sleeps == []

    def test_hard_navigate_error_neither_retried_nor_reclassified(self):
        """A real fetch failure (proxy escalation exhausted → None) is already
        the FAIL signal — no retry, no reclassification."""
        h = _discover([None])
        assert h.urls == []
        assert h.meta["stop_reason"] == "navigate_error"
        assert h.time.sleeps == []


# ─── behavioral: soft-block proxy escalation [job-62] ─────────────────────────


class TestSoftBlockEscalation:
    def test_ladder_exhaustion_is_bounded_and_still_classifies_empty(self):
        """Every tier serves 200-with-zero-links: exactly tiers_total-1
        escalations fire (bounded by ladder length), the listing ends
        short_page, and the run still classifies honestly as
        empty_first_page. Friction is visible in discovery_meta."""
        h = _discover([[]] * 3, retry_delay=0, listings=["https://site.test/de/a/"])
        assert h.urls == []
        assert h.meta["stop_reason"] == "empty_first_page"
        assert h.meta["soft_block_escalations"] == 2
        assert h.tier_calls == [0, 1, 2]
        assert h.time.sleeps == []

    def test_empty_page_beyond_page_1_never_escalates(self):
        """An empty page >= 2 is a genuine catalog end — only page 1 of a
        listing escalates. Without that guard every thin category tail would
        burn two proxy escalations per listing."""
        h = _discover(
            [["/de/p/only_1.html"], []],
            retry_delay=0,
            listings=["https://site.test/de/a/"],
        )
        assert len(h.urls) == 1
        assert h.meta["stop_reason"] == "short_page"
        assert h.meta["soft_block_escalations"] == 0
        assert h.tier_calls == [0, 0]

    def test_escalated_tier_locks_in_as_floor(self):
        """Once a tier unblocks a listing, the closure's min_tier_floor is
        raised so later fetches — subsequent pages AND Phase-2 item pages,
        which call fetch_page(url) with no explicit min_tier — reuse it
        instead of restarting unproxied and re-entering the block."""
        h = _discover(
            [[], ["/de/p/x_1.html"]],
            retry_delay=0,
            listings=["https://site.test/de/a/"],
        )
        assert h.fetch_page.min_tier_floor == 1


# ─── behavioral: JSON-LD ItemList fallback [job-65 citybeach] ─────────────────


class TestJsonLdItemListFallback:
    def test_hidden_ssr_listing_discovered_via_itemlist(self):
        """The job-65 signature: a 200 listing whose grid hydrates client-side
        serves ZERO product anchors but embeds its item set as a JSON-LD
        ItemList. Discovery must read it at tier 0 — no escalation, no
        retry, no empty_first_page — and surface the fallback in meta."""
        itemlist = {
            "@type": "ItemList",
            "itemListElement": [
                {"item": {"url": "/de/p/a_1.html"}},
                {"@type": "ListItem", "url": "/de/p/b_2.html"},
            ],
        }
        h = _discover(
            [
                {"product": [], "anchors": ["/nav/a", "/nav/b"], "jsonld": [itemlist]},
                {"product": [], "anchors": ["/nav/a"], "jsonld": []},
            ],
            retry_delay=0,
            listings=["https://site.test/de/a/"],
        )
        assert len(h.urls) == 2
        assert h.meta["stop_reason"] == "short_page"  # genuine thin page-2 tail
        assert h.meta["soft_block_escalations"] == 0
        assert h.meta["jsonld_fallback_pages"] == 1
        assert h.tier_calls == [0, 0]

    def test_breadcrumb_list_is_not_accepted_as_item_source(self):
        """BreadcrumbList also carries itemListElement — accepting it would
        discover navigation crumbs as product URLs. Only ItemList counts;
        a breadcrumb-only page is still 'zero product links' and escalates."""
        crumb = {
            "@type": "BreadcrumbList",
            "itemListElement": [{"item": {"@id": "/de/c/a"}, "name": "A"}],
        }
        h = _discover(
            [{"product": [], "anchors": ["/nav/a"], "jsonld": [crumb]}] * 3,
            retry_delay=0,
            listings=["https://site.test/de/a/"],
        )
        assert h.urls == []
        assert h.meta["stop_reason"] == "empty_first_page"
        assert h.meta["jsonld_fallback_pages"] == 0
        assert h.tier_calls == [0, 1, 2]

    def test_url_filter_gates_itemlist_candidates(self):
        """url_filter (the template's PRODUCT_URL_RE) applies to ItemList
        candidates too — a filtered-to-zero ItemList does not masquerade as
        discovery, the soft-block path runs instead."""
        itemlist = {
            "@type": "ItemList",
            "itemListElement": [{"item": {"url": "/de/p/a_1.html"}}],
        }
        h = _discover(
            [{"product": [], "anchors": [], "jsonld": [itemlist]}] * 3,
            retry_delay=0,
            listings=["https://site.test/de/a/"],
            url_filter=re.compile(r"/us/"),
        )
        assert h.urls == []
        assert h.meta["jsonld_fallback_pages"] == 0
        assert h.meta["soft_block_escalations"] == 2

    def test_anchors_served_but_none_matching_still_escalates(self):
        """THE job-65 bypass, killed: a page serving 300 anchors whose hrefs
        never match the product shape used to collapse to
        `new_on_page == 0 → no_new_items` in the rewritten draft, bypassing
        the escalation branch entirely. The zero-check now runs on the
        callback's OUTPUT inside the module: 0 usable → escalation fires."""
        h = _discover(
            [
                {"product": [], "anchors": ["/nav/a", "/nav/b", "/nav/c"]},
                ["/de/p/recovered_1.html"],
                [],
            ],
            retry_delay=0,
            listings=["https://site.test/de/a/"],
        )
        assert len(h.urls) == 1
        assert h.meta["soft_block_escalations"] == 1
        assert h.tier_calls == [0, 1, 1]


# ─── behavioral: pagination URL building + diagnostics ────────────────────────


class TestPaginationAndDiagnostics:
    def test_numbered_and_offset_page_urls(self):
        ld = importlib.import_module("src.listing_discovery")
        assert ld.build_page_url("https://x/c/", 1) == "https://x/c/?page=1"
        assert ld.build_page_url("https://x/c/?a=1", 3, "p") == "https://x/c/?a=1&p=3"
        # SFCC-style offset: ?start=0&sz=48, ?start=48&sz=48 ...
        assert ld.build_page_url(
            "https://x/c/", 2, "start", page_size=48, offset_mode=True,
            extra_page_params={"sz": 48},
        ) == "https://x/c/?start=48&sz=48"
        try:
            ld.build_page_url("https://x/c/", 2, "start", offset_mode=True)
        except ValueError:
            pass
        else:
            raise AssertionError("offset_mode without page_size must raise")

    def test_per_page_log_distinguishes_anchors_from_matches(self, caplog):
        """The diagnostic regression that blinded the job-65 autopsy: the
        draft logged only the post-filter count. The module logs anchors
        served, usable product links, and new — all three."""
        import logging

        ld = importlib.import_module("src.listing_discovery")
        with caplog.at_level(logging.INFO, logger=ld.__name__):
            _discover(
                [{"product": [], "anchors": ["/nav/a", "/nav/b", "/nav/c"]}, []],
                retry_delay=0,
                listings=["https://site.test/de/a/"],
            )
        assert "3 anchors" in caplog.text
        assert "0 usable" in caplog.text
        assert "0 new" in caplog.text


# ─── behavioral: pipeline coverage gate ──────────────────────────────────────


class TestCoverageGateEmptyFirstPage:
    def test_gate_fails_empty_first_page(self):
        rat = importlib.import_module("agents.nodes.route_after_testing")
        reason = rat._discovery_coverage_failure(
            {"discovery_coverage": {"ran_phase1": True, "stop_reason": "empty_first_page"}}
        )
        assert reason and "empty_first_page" in reason

    def test_gate_still_noop_on_missing_signals(self):
        rat = importlib.import_module("agents.nodes.route_after_testing")
        assert rat._discovery_coverage_failure(None) is None
        assert rat._discovery_coverage_failure({"discovery_coverage": {"ran_phase1": False}}) is None
        assert rat._discovery_coverage_failure(
            {"discovery_coverage": {"ran_phase1": True, "stop_reason": "short_page"}}
        ) is None

    def test_classify_zero_items_with_empty_first_page_is_strategy(self):
        """End-to-end shape of the job-58 test-report: a blocked execution run
        classified by the deterministic classifier must land on 'strategy'
        (access problem), never 'refine' (field tweaking on zero items)."""
        rat = importlib.import_module("agents.nodes.route_after_testing")
        report = {
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "discovery_coverage": {
                "ran_phase1": True,
                "stop_reason": "empty_first_page",
                "found": 0,
            },
        }
        action, why = rat.classify_test_failure(report, "http_requests")
        assert action == "strategy"
        assert "empty_first_page" in why or "no items" in why


# ─── static: http_navigation template contract ───────────────────────────────


class TestHttpNavigationTemplate:
    def test_priority_table_has_empty_first_page_in_fail_class(self):
        with open(NAV) as fh:
            src = fh.read()
        tree = ast.parse(src)
        table = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_STOP_REASON_PRIORITY" for t in node.targets
            ):
                table = ast.literal_eval(node.value)
        assert table is not None
        assert table.get("empty_first_page") == 5  # same FAIL class as navigate_error
        assert table["navigate_error"] == 5

    def test_reclass_is_aggregate_level_not_per_path(self):
        """The zero-URL reclass must live ONCE in main() on the AGGREGATE url
        list — never inside the per-path discoverers. Per-path reclass +
        _merge_stop_reason would let one blocked secondary category
        (200-but-empty) fail a run whose primary path found items (T2.1)."""
        with open(NAV) as fh:
            src = fh.read()
        for fn in (
            "_discover_urls_via_search",
            "_discover_urls_via_form_search",
            "_discover_urls_via_category",
        ):
            assert "empty_first_page" not in _grab(src, fn), fn
        main_src = _grab(src, "main")
        assert 'aggregate_stop_reason = "empty_first_page"' in main_src
        # The guard keys on zero AGGREGATE urls with PASS-flavored reasons only.
        assert "not discovered_urls" in main_src
        assert '"no_new_items"' in main_src

    def test_aggregation_merge_carries_the_new_reason(self):
        with open(NAV) as fh:
            src = fh.read()
        assert src.count("_merge_stop_reason") >= 5  # used across main()'s phases


# ─── static: requests-template call-site contract ────────────────────────────


class TestRequestsTemplateCallSites:
    def test_both_discovery_call_sites_use_the_shared_module(self):
        with open(REQ) as fh:
            src = fh.read()
        # --discover-only/env-gate branch + no-input branch.
        assert len(re.findall(r"= discover_listing_urls_with_retry\(", src)) == 2
        # The inline loop is gone — nothing left for code_writer to rewrite.
        assert "def discover_product_urls" not in src
        assert "def discover_listing" not in src
        assert "def discover_" not in src.replace("_extract_listing_links", "")
        for gone in ("len(links)", "no_new_items", "min_tier", "min_tier_floor"):
            assert gone not in src, gone
        # The counter may be READ for the coverage emitter (main) — but never
        # initialized or incremented outside the module (the job-65 hollow
        # counter: emitted but no code path could ever raise it).
        assert "soft_block_escalations +=" not in src
        assert "soft_block_escalations = " not in src

    def test_import_and_callback_present(self):
        with open(REQ) as fh:
            src = fh.read()
        assert "from src.listing_discovery import discover_listing_urls_with_retry" in src
        assert "def _extract_listing_links(soup" in src
        # The callback filters via the data constants, INSIDE its own body.
        assert "PRODUCT_URL_RE" in _grab(src, "_extract_listing_links")
        # The retry delay + ItemList gate are wired through the config dict.
        assert '"retry_delay_s": EMPTY_DISCOVERY_RETRY_DELAY_S' in src
        assert '"url_filter": PRODUCT_URL_RE' in src
        assert "EMPTY_DISCOVERY_RETRY_DELAY_S = 45" in src

    def test_fetch_machinery_is_the_shared_module_not_inline(self):
        """[job-62 birkenstock root cause] code_writer rewrote fetch_page
        inline and STRIPPED the proxy ladder because analysis said "direct
        works" — the tester passed, execution got 200-wrapped challenge pages,
        and the draft had nothing to escalate to → 0 items. The machinery now
        lives in src/http_fetch.py (the same structural defense as
        src/discovery.py): the template imports it, defines no fetch loop of
        its own, and keeps no proxy objects for the writer to reason about."""
        with open(REQ) as fh:
            src = fh.read()
        assert "from src.http_fetch import create_fetch_page" in src
        assert re.search(r"^fetch_page = create_fetch_page\(", src, re.MULTILINE)
        assert "def fetch_page" not in src  # nothing inline to strip
        assert "requests.Session()" not in src  # session lives in the module
        assert "proxy_config" not in src  # module owns the ProxyConfig usage

        with open(os.path.join(ROOT, "src", "http_fetch.py")) as fh:
            mod = fh.read()
        assert "requests.Session()" in mod  # persistent-session fix retained
        assert "min_tier_floor" in mod and "tiers_total" in mod

    def test_discovery_machinery_is_the_shared_module_not_inline(self):
        """[job-65 citybeach root cause] code_writer deleted the zero-links
        escalation branch while keeping the counter — the draft emitted
        soft_block_escalations but nothing could increment it. The loop now
        lives in src/listing_discovery.py; the template defines only the
        callback + data constants."""
        with open(REQ) as fh:
            src = fh.read()
        with open(os.path.join(ROOT, "src", "listing_discovery.py")) as fh:
            mod = fh.read()
        # The module owns every recovery mechanism...
        for machinery in ("soft_block_escalations", "min_tier", "min_tier_floor",
                          "empty_first_page", "jsonld_item_urls", "no_new_items"):
            assert machinery in mod, machinery
        # ...and the template owns none of it (nothing left to strip). The one
        # allowed reference is main()'s coverage emitter reading the module's
        # meta — no init, no increment, no loop logic.
        for machinery in ("min_tier", "min_tier_floor",
                          "empty_first_page", "no_new_items", "len(links)"):
            assert machinery not in src, machinery
        assert "soft_block_escalations +=" not in src
        assert "soft_block_escalations = " not in src

    def test_friction_counters_surface_in_discovery_coverage(self):
        """Job-62's prod output carried retried_empty_discovery in
        discovery_meta only — discovery_coverage (the artifact the gate and
        any auditor read) had neither friction counter, so the execution
        output alone could not say WHY the run was blocked. Both surface."""
        with open(REQ) as fh:
            src = fh.read()
        main_src = _grab(src, "main")
        assert '"soft_block_escalations"' in main_src
        assert '"retried_empty_discovery"' in main_src
        assert '"jsonld_fallback_pages"' in main_src


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
