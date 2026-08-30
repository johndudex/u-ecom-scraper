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


def _make_ns(fetch_results: list, retry_delay: int = 45):
    """Namespace with the two extracted functions + controllable stubs.

    ``fetch_results``: queue of ``list[str]`` (product hrefs on the page) or
    None (fetch failure → navigate_error path). Each listing-page fetch pops
    one entry; an exhausted queue yields empty pages.
    """
    with open(REQ) as fh:
        src = fh.read()
    # The template placeholder is a literal CSS selector; make it selectable.
    src = src.replace('"{PRODUCT_LINK_SELECTOR}"', '".prod"')
    ns = {
        "time": _FakeTime(),
        "logger": _FakeLogger(),
        "PRODUCT_LISTING_URLS": [
            "https://site.test/de/a/",
            "https://site.test/de/b/",
        ],
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

    def fetch_page(_url):
        # Mirror the real fetch_page contract: None on total failure
        # (all proxy tiers exhausted), (soup, status) on any HTTP response.
        r = queue.pop(0) if queue else []
        return None if r is None else (_FakeSoup(r), 200)

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

    def test_retry_recovers_when_block_window_passes(self):
        """First pass blocked-empty; the 45s backoff rides out the window and
        the second pass finds URLs (the tester had found 15 ninety seconds
        earlier). Discovery succeeds; the retry is recorded."""
        page1 = ["/de/p/first_1.html", "/de/p/second_2.html"]
        ns = _make_ns([[], [], page1, []])
        urls, meta = ns["discover_product_urls_with_retry"]()
        assert len(urls) == 2
        assert meta["retried_empty_discovery"] is True
        assert meta["stop_reason"] != "empty_first_page"
        assert ns["time"].sleeps == [45]

    def test_genuine_end_after_items_keeps_short_page_no_retry(self):
        """A real catalog-end requires having seen items: listing A yields
        products on page 1 then thins out on page 2 → short_page, PASS, and
        the retry path is never entered."""
        ns = _make_ns([
            ["/de/p/a1_1.html", "/de/p/a2_2.html"],  # listing A page 1
            [],                                      # listing A page 2 (thin)
        ])
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
            [],                                       # listing A: blocked
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

    def test_fetch_page_uses_persistent_session(self):
        """[job-58 root-cause trigger] Bare requests.get() is a cookieless
        one-shot per call — Akamai never sees the consent/_abck cookie a real
        browser round-trips, so the test phase's rapid draft runs burn the
        worker IP's reputation before execution. fetch_page must go through
        a module-level Session (cookies persist, connections are reused)."""
        with open(REQ) as fh:
            src = fh.read()
        assert "requests.Session()" in src
        fetch_body = _grab(src, "fetch_page")
        assert "SESSION.get(" in fetch_body
        assert "requests.get(" not in fetch_body


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
