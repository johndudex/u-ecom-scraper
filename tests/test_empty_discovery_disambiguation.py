"""[job-58 birkenstock] Blocked-listing misread as empty catalog + no retry.

The failure: the tester's run discovered 15 URLs and extracted 5/5 (PASS
0.95); the execution run 90 seconds later fetched the SAME listing, got a
200-but-blocked response (Akamai challenge / consent wall served with HTTP
200), selected zero product links, and the discovery loop classified that as
``short_page`` — "genuine end of results". Phase 1 "succeeded" with 0 URLs in
1.65s → 0 items → the zero-item finalize gate failed the job. Nothing retried.

Three layers fixed here:

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

The template halves are tested behaviorally (the discovery functions are
exec-extracted into a namespace with a stubbed ``fetch_page``/``time`` —
templates themselves stay unimportable) and statically (presence/shape),
mirroring tests/test_discovery_ladder.py's split.
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

TEMPLATES = os.path.join(ROOT, "templates")
REQ = os.path.join(TEMPLATES, "requests_scraper.py")
NAV = os.path.join(TEMPLATES, "http_navigation_scraper.py")


# ─── namespace harness: exec-extract the template discovery functions ───────


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


class _FakeSoup:
    def __init__(self, hrefs: list[str]):
        self._hrefs = hrefs

    def select(self, _sel: str) -> list:
        return [_FakeLink(h) for h in self._hrefs]


class _FakeLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg) % a if a else str(msg))


def _make_ns(fetch_results: list, retry_delay: int = 45, listings: list | None = None):
    """Namespace with the two extracted functions + controllable stubs.

    ``fetch_results``: queue of ``list[str]`` (product hrefs on the page) or
    None (fetch failure → navigate_error path). Each listing-page fetch pops
    one entry; an exhausted queue yields empty pages.
    ``listings``: override PRODUCT_LISTING_URLS (escalation tests use a single
    listing so tier-call sequences are exact — a second listing re-escalates
    after the first short-pages, by design).
    """
    with open(REQ) as fh:
        src = fh.read()
    # The template placeholder is a literal CSS selector; make it selectable.
    src = src.replace('"{PRODUCT_LINK_SELECTOR}"', '".prod"')
    ns = {
        "time": _FakeTime(),
        "logger": _FakeLogger(),
        "PRODUCT_LISTING_URLS": listings
        or ["https://site.test/de/a/", "https://site.test/de/b/"],
        "PAGE_PARAM_NAME": "page",
        "DISCOVERY_DEADLINE_SECONDS": 300,
        "MAX_PAGES": None,
        "EMPTY_DISCOVERY_RETRY_DELAY_S": retry_delay,
        "make_absolute_url": lambda u, base="": (
            u if u.startswith("http") else "https://site.test" + u
        ),
        "fetch_page": None,  # set below (needs the queue)
    }

    queue = list(fetch_results)
    tier_calls: list[int] = []

    def fetch_page(_url, min_tier=0):
        # Mirror the real fetch_page contract: None on total failure
        # (all proxy tiers exhausted), (soup, status) on any HTTP response.
        # ``min_tier`` is the soft-block escalation index (job-62).
        tier_calls.append(min_tier)
        r = queue.pop(0) if queue else []
        return None if r is None else (_FakeSoup(r), 200)

    # Closure attributes the discovery loop reads (mirrors src/http_fetch.py):
    # the escalation bound and the floor the loop raises once a tier works.
    fetch_page.min_tier_floor = 0
    fetch_page.tiers_total = 3  # none → datacenter → residential
    fetch_page.tier_calls = tier_calls

    ns["fetch_page"] = fetch_page

    fn_src = _grab(src, "discover_product_urls")
    fn_src += _grab(src, "discover_product_urls_with_retry")
    exec(compile(fn_src, REQ, "exec"), ns)  # noqa: S102
    return ns


# ─── behavioral: requests-template discovery ─────────────────────────────────


class TestZeroUrlReclassification:
    def test_blocked_zero_url_run_is_empty_first_page_and_retried(self):
        """The job-58 signature: every listing fetch 'succeeds' but yields zero
        product links → empty_first_page, plus exactly one 45s retry."""
        ns = _make_ns([[], [], [], []])  # both listings × 2 attempts, all empty
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert urls == []
        assert meta["stop_reason"] == "empty_first_page"
        assert meta["retried_empty_discovery"] is True
        assert ns["time"].sleeps == [45]

    def test_zero_urls_without_retry_delay_disabled(self):
        """Retry disabled (delay 0) → single pass, still reclassified."""
        ns = _make_ns([[], []], retry_delay=0)
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert urls == []
        assert meta["stop_reason"] == "empty_first_page"
        assert "retried_empty_discovery" not in meta
        assert ns["time"].sleeps == []

    def test_escalation_recovers_without_paying_the_retry(self):
        """[job-62 birkenstock] Page 1 blocked at tier 0 (200, zero links) but
        the datacenter tier serves the listing: escalation recovers discovery
        in the SAME pass — no 45s retry, no empty_first_page. This is the path
        the stripped draft lacked entirely."""
        page1 = ["/de/p/first_1.html", "/de/p/second_2.html"]
        ns = _make_ns([[], page1, []], listings=["https://site.test/de/a/"])
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert len(urls) == 2
        assert meta["stop_reason"] != "empty_first_page"
        assert meta["soft_block_escalations"] == 1
        assert ns["time"].sleeps == []  # retry path never entered
        assert ns["fetch_page"].tier_calls == [0, 1, 1]  # ladder climbed, then floor held

    def test_genuine_end_after_items_keeps_short_page_no_retry(self):
        """A real catalog-end requires having seen items: listing A yields
        products on page 1 then thins out on page 2 → short_page, PASS, and
        the retry path is never entered."""
        ns = _make_ns(
            [
                ["/de/p/a1_1.html", "/de/p/a2_2.html"],  # page 1
                [],                                      # page 2 (thin)
            ],
            listings=["https://site.test/de/a/"],
        )
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert len(urls) == 2
        assert meta["stop_reason"] == "short_page"
        assert ns["time"].sleeps == []  # healthy run pays nothing

    def test_one_blocked_listing_does_not_poison_a_working_one(self):
        """Listing A blocked-empty, listing B finds URLs → partial coverage
        succeeds (urls non-empty → no retry); the T2.1 lesson: never fail the
        run that found items. The per-listing emptiness stays visible in the
        last-wins stop_reason but NOT as empty_first_page."""
        ns = _make_ns([
            [],                                       # listing A tier 0: blocked
            [],                                       # listing A tier 1: blocked
            [],                                       # listing A tier 2: blocked
            ["/de/p/b1_1.html", "/de/p/b2_2.html"],   # listing B page 1
            [],                                       # listing B page 2
        ])
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert len(urls) == 2
        assert meta["stop_reason"] != "empty_first_page"
        assert ns["time"].sleeps == []

    def test_hard_navigate_error_neither_retried_nor_reclassified(self):
        """A real fetch failure (proxy escalation exhausted → None) is already
        the FAIL signal — no retry, no reclassification."""
        ns = _make_ns([None])
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert urls == []
        assert meta["stop_reason"] == "navigate_error"
        assert ns["time"].sleeps == []


# ─── behavioral: soft-block proxy escalation [job-62] ─────────────────────────


class TestSoftBlockEscalation:
    def test_ladder_exhaustion_is_bounded_and_still_classifies_empty(self):
        """Every tier serves 200-with-zero-links: exactly tiers_total-1
        escalations fire (bounded by ladder length), the listing ends
        short_page, and the run still classifies honestly as
        empty_first_page. Friction is visible in discovery_meta."""
        ns = _make_ns([[]] * 3, retry_delay=0, listings=["https://site.test/de/a/"])
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert urls == []
        assert meta["stop_reason"] == "empty_first_page"
        assert meta["soft_block_escalations"] == 2
        assert ns["fetch_page"].tier_calls == [0, 1, 2]
        assert ns["time"].sleeps == []

    def test_empty_page_beyond_page_1_never_escalates(self):
        """An empty page >= 2 is a genuine catalog end — only page 1 of a
        listing escalates. Without that guard every thin category tail would
        burn two proxy escalations per listing."""
        ns = _make_ns(
            [["/de/p/only_1.html"], []],
            retry_delay=0,
            listings=["https://site.test/de/a/"],
        )
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert len(urls) == 1
        assert meta["stop_reason"] == "short_page"
        assert meta["soft_block_escalations"] == 0
        assert ns["fetch_page"].tier_calls == [0, 0]

    def test_escalated_tier_locks_in_as_floor(self):
        """Once a tier unblocks a listing, the closure's min_tier_floor is
        raised so later fetches — subsequent pages AND Phase-2 item pages,
        which call fetch_page(url) with no explicit min_tier — reuse it
        instead of restarting unproxied and re-entering the block."""
        ns = _make_ns(
            [[], ["/de/p/x_1.html"]],
            retry_delay=0,
            listings=["https://site.test/de/a/"],
        )
        ns["discover_product_urls_with_retry"]()
        assert ns["fetch_page"].min_tier_floor == 1


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
    def test_both_discovery_call_sites_use_the_retry_wrapper(self):
        with open(REQ) as fh:
            src = fh.read()
        assert len(re.findall(r"= discover_product_urls_with_retry\(\)", src)) == 2
        # The only bare calls are the wrapper's own two attempts (initial + retry).
        assert len(re.findall(r"= discover_product_urls\(\)", src)) == 2

    def test_wrapper_and_constant_exist(self):
        with open(REQ) as fh:
            src = fh.read()
        assert "EMPTY_DISCOVERY_RETRY_DELAY_S = 45" in src
        assert "def discover_product_urls_with_retry()" in src

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
        assert "min_tier" in src  # discovery loop drives the soft-block ladder

        with open(os.path.join(ROOT, "src", "http_fetch.py")) as fh:
            mod = fh.read()
        assert "requests.Session()" in mod  # persistent-session fix retained
        assert "min_tier_floor" in mod and "tiers_total" in mod

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


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
