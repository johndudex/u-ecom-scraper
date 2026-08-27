"""Param-alias ladder (src/discovery.py) + template output-contract tests.

Two halves, matching the two defects behind priceline job 302 (36/97 products):

1. The draft paginated with ``?page=N``; the server IGNORED it and kept serving
   page 1, so discovery saw "no new items" on page 2 and stopped silently at one
   page of results. ``_try_page_param`` now runs a 4-candidate alias ladder on
   that verified-stuck condition (``currentPage`` 0-indexed → ``p`` → page-sized
   ``offset`` → ``skip``), each candidate verified exactly like the primary
   param (≥1 genuinely NEW url; the ``seen``-set diff is the identical-content
   detector).

2. The same family emitted ``"site": <hostname string>`` and NO
   ``metadata.discovery_coverage``, so ``_read_discovery_coverage`` /
   ``_attach_discovery_coverage`` and the Tier-1 ``dedup_flat`` gate found
   nothing (and tasks.py's ground-truth reader crashed on ``str.get``).

The template half is asserted STATICALLY (AST) — templates are not importable
(bs4/playwright) and are copied verbatim by code_writer, so the contract has to
hold on the source text.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from typing import Optional
from urllib.parse import parse_qsl, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.discovery import (  # noqa: E402
    DiscoveryConfig,
    StopReason,
    _PAGE_PARAM_ALIASES,
    _alias_param_url,
    build_page_param_url,
    discover_item_urls,
)

TEMPLATES = os.path.join(ROOT, "templates")

# discovery-coverage-gate contract §1 — the exact key set every two-phase
# template emits and `_read_discovery_coverage` consumers read.
CONTRACT_KEYS = {
    "stop_reason",
    "found",
    "discovered_urls",
    "expected_total",
    "dimensions_iterated",
    "dimensions_total",
    "max_pages_hit",
    "ran_phase1",
    "skipped_reason",
}
SITE_KEYS = ("name", "url", "platform", "scraping_method", "scraped_at")


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1 — the ladder, against a mocked fetch (job-302 shaped)
# ═══════════════════════════════════════════════════════════════════════════════

PAGE_SIZE = 36
TOTAL = 97  # priceline job 302: 36 + 36 + 25


def _items_for(url: str, honored: str | None) -> list[str]:
    """Item URLs a server-rendered listing returns for ``url``.

    Only ``honored`` paginates. If that param is absent from the URL — or the
    site has no working param at all (``honored=None``) — the server serves page
    1 again, which is exactly the job-302 shape: the configured param is
    silently dropped rather than erroring.
    """
    qs = dict(parse_qsl(urlparse(url).query))
    start = 0
    if honored and honored in qs:
        raw = int(qs[honored])
        if honored in ("offset", "skip"):
            start = raw
        elif honored == "currentPage":
            start = raw * PAGE_SIZE
        else:  # 1-indexed page param
            start = (raw - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, TOTAL)
    return [f"https://x.test/item/{i:04d}" for i in range(start, end)]


class _Resp:
    def __init__(self, status: int):
        self.status = status


class _FakePage:
    """PageLike double: records navigations, serves `_items_for(url, honored)`.

    ``honored`` is the ONE query param this fake site paginates on (None = the
    site ignores every param — nothing can be adopted). Any other param in a
    requested URL is ignored, which is what a server that drops an unknown param
    does.
    """

    def __init__(self, start_url: str, honored: str | None = None,
                 error_params: tuple[str, ...] = ()):
        self._url = start_url
        self.honored = honored
        self.error_params = set(error_params)
        self.goto_urls: list[str] = []

    @property
    def url(self) -> str:
        return self._url

    @property
    def current_items(self) -> list[str]:
        return _items_for(self._url, self.honored)

    def goto(self, url: str, timeout: int = 0) -> _Resp:
        self.goto_urls.append(url)
        qs = dict(parse_qsl(urlparse(url).query))
        if self.error_params and any(k in qs for k in self.error_params):
            return _Resp(404)  # this page is left where it was
        self._url = url
        return _Resp(200)

    def evaluate(self, js, *args):
        return []

    def query_selector(self, selector):
        return None

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state):
        pass


def _extract(page) -> list[str]:
    return page.current_items


def _cfg(**overrides) -> DiscoveryConfig:
    """page_param-only config with every sleep stripped out."""
    base = dict(
        strategies=("page_param",),
        page_param_name="page",
        items_per_page=PAGE_SIZE,
        page_settle_after_nav_s=0.0,
        max_pages=25,
        min_initial_links=1,
    )
    base.update(overrides)
    return DiscoveryConfig(**base)


def _alias_gotos(page: _FakePage, configured: str = "page") -> list[str]:
    """Navigations that used an ALIAS param (the configured param excluded — it
    may itself be one of the alias names, e.g. `p`)."""
    out = []
    for u in page.goto_urls:
        qs = dict(parse_qsl(urlparse(u).query))
        if any(a in qs for a, _m in _PAGE_PARAM_ALIASES if a != configured):
            out.append(u)
    return out


def _all_query_keys(page: _FakePage) -> set[str]:
    keys: set[str] = set()
    for u in page.goto_urls:
        keys |= {k for k, _v in parse_qsl(urlparse(u).query)}
    return keys


class TestLadderAdoptsVerifiedAlias:
    def test_currentpage_rescues_an_ignored_page_param(self):
        """`?page=N` ignored → currentPage (0-indexed) verified + adopted."""
        page = _FakePage("https://x.test/list", honored="currentPage")
        result = discover_item_urls(page, "https://x.test/list", _extract, _cfg())

        assert len(result.urls) == TOTAL, (
            f"expected all {TOTAL} items via the alias ladder, got {len(result.urls)}"
        )
        assert result.param_used == "currentPage"
        # The ladder verified, it did not guess: currentPage=1 (0-indexed) was
        # navigated and produced page 2's items.
        assert "https://x.test/list?currentPage=1" in page.goto_urls
        assert "https://x.test/list?currentPage=2" in page.goto_urls
        # Never reached alias #2 (`p`), and the dead param was not carried along.
        assert "p" not in _all_query_keys(page)

    def test_adopted_alias_drives_every_subsequent_page(self):
        page = _FakePage("https://x.test/list", honored="currentPage")
        result = discover_item_urls(page, "https://x.test/list", _extract, _cfg())
        currents = [
            int(dict(parse_qsl(urlparse(u).query))["currentPage"])
            for u in page.goto_urls if "currentPage" in urlparse(u).query
        ]
        # 1,2,3 carry the 97 items; 4,5 are the 3-strike exhaustion re-checks
        # (empty pages). Monotonic — page 1 is never re-fetched.
        assert currents == sorted(currents)
        assert currents[:3] == [1, 2, 3]
        assert len(result.urls) == TOTAL

    def test_dead_param_is_stripped_from_alias_probes(self):
        """`?page=2&currentPage=1` leaves the server free to honor either, and
        the leftover accumulates on every page. The configured param must go."""
        page = _FakePage("https://x.test/list", honored="currentPage")
        discover_item_urls(page, "https://x.test/list", _extract, _cfg())
        for u in _alias_gotos(page):
            qs = dict(parse_qsl(urlparse(u).query))
            assert "page" not in qs, f"dead param carried into an alias probe: {u}"

    def test_offset_alias_uses_the_observed_page_size(self):
        """Site honors `offset` only; page size comes from the fetched page
        (items_per_page=None), so offset must be 36/72 — not 2/4."""
        page = _FakePage("https://x.test/list", honored="offset")
        cfg = _cfg(items_per_page=None)
        result = discover_item_urls(page, "https://x.test/list", _extract, cfg)

        assert result.param_used == "offset"
        assert len(result.urls) == TOTAL
        offsets = [
            int(dict(parse_qsl(urlparse(u).query))["offset"])
            for u in page.goto_urls if "offset" in urlparse(u).query
        ]
        assert offsets[0] == PAGE_SIZE      # page 2 → offset 1 × 36
        assert offsets[1] == PAGE_SIZE * 2  # page 3 → offset 2 × 36

    def test_p_alias_adopted_when_it_is_the_real_param(self):
        page = _FakePage("https://x.test/list", honored="p")
        result = discover_item_urls(page, "https://x.test/list", _extract, _cfg())
        assert result.param_used == "p"
        assert len(result.urls) == TOTAL

    def test_stop_reason_is_genuine_exhaustion_not_a_stuck_page(self):
        """The load-bearing coverage signal: after the alias is exhausted the run
        ends no_new_items (real end of the list), never an early "stuck" from the
        dead primary param."""
        page = _FakePage("https://x.test/list", honored="currentPage")
        result = discover_item_urls(page, "https://x.test/list", _extract, _cfg())
        assert result.stop_reason == StopReason.NO_NEW_ITEMS.value
        assert len(result.urls) == TOTAL
        assert result.pages_visited >= 4  # page 1 + 3 alias pages

    def test_failed_alias_probe_does_not_poison_the_sticky_stop_reason(self):
        """`_discovery_goto` stamps NAVIGATE_ERROR (sticky, H4). A 404 on ONE
        alias must be rolled back, else a working alias is shadowed and a
        successful run is reported as gave-up."""
        page = _FakePage(
            "https://x.test/list", honored="currentPage",
            error_params=("p", "offset", "skip"),
        )
        result = discover_item_urls(page, "https://x.test/list", _extract, _cfg())
        assert result.param_used == "currentPage"
        assert len(result.urls) == TOTAL
        assert result.stop_reason == StopReason.NO_NEW_ITEMS.value
        assert "navigate_error" not in result.stop_reason


class TestLadderBoundedAndConservative:
    def test_alias_set_is_bounded_to_four_candidates(self):
        assert len(_PAGE_PARAM_ALIASES) == 4
        assert [name for name, _m in _PAGE_PARAM_ALIASES] == [
            "currentPage", "p", "offset", "skip",
        ]
        assert dict(_PAGE_PARAM_ALIASES)["currentPage"] == "page0"  # 0-indexed

    def test_all_candidates_stuck_returns_the_unchanged_stuck_result(self):
        """Site honors nothing → 4 probes, then the pre-ladder behavior: same
        urls, same stop_reason, param_used = the configured param."""
        page = _FakePage("https://x.test/list", honored=None)  # ignores everything
        result = discover_item_urls(page, "https://x.test/list", _extract, _cfg())

        assert len(result.urls) == PAGE_SIZE
        assert result.stop_reason == StopReason.NO_NEW_ITEMS.value
        assert result.param_used == "page"
        assert result.max_pages_hit is False

    def test_ladder_probed_at_most_once_per_run(self):
        """Bounded: 4 candidate navigations for the WHOLE run, not 4 per stuck
        page (the ladder is memoized exhausted in `state`)."""
        page = _FakePage("https://x.test/list", honored=None)
        discover_item_urls(page, "https://x.test/list", _extract, _cfg())
        assert len(_alias_gotos(page)) == len(_PAGE_PARAM_ALIASES)
        # ...while the primary param kept being retried by the orchestrator.
        assert sum(1 for u in page.goto_urls if "page=" in u) >= 3

    def test_ladder_never_fires_when_the_primary_param_advances(self):
        """A HEALTHY paginating site pays zero alias probes: the ladder only
        fires on a page that returned items we already have — never on the
        empty page that genuinely ends the list."""
        page = _FakePage("https://x.test/list", honored="page")
        result = discover_item_urls(page, "https://x.test/list", _extract, _cfg())
        assert len(result.urls) == TOTAL
        assert result.param_used == "page"
        assert _alias_gotos(page) == []  # no alias navigation at all

    def test_primary_param_name_is_skipped_not_reprobed(self):
        """A site configured with `p` must not waste a probe re-testing `p`."""
        assert any(name == "p" for name, _m in _PAGE_PARAM_ALIASES)
        page = _FakePage("https://x.test/list", honored=None)
        discover_item_urls(page, "https://x.test/list", _extract,
                           _cfg(page_param_name="p"))
        probed = [urlparse(u).query for u in _alias_gotos(page, configured="p")]
        assert len(probed) == len(_PAGE_PARAM_ALIASES) - 1  # `p` skipped
        assert not any(q.startswith("p=") for q in probed)

    def test_param_used_is_none_when_page_param_never_runs(self):
        from src.discovery import config_for_load_more

        page = _FakePage("https://x.test/list", honored=None)
        result = discover_item_urls(
            page, "https://x.test/list", _extract,
            config_for_load_more(max_pages=1, page_settle_after_nav_s=0.0,
                                 min_initial_links=1),
        )
        assert result.param_used is None


class TestAliasUrlMath:
    """The value each alias mode produces — the off-by-one trap is the point."""

    def test_currentpage_is_zero_indexed(self):
        url = _alias_param_url("https://x.test/list?page=2", "currentPage",
                               "page0", 2, PAGE_SIZE, dead_param="page")
        assert url == "https://x.test/list?currentPage=1"

    def test_p_is_one_indexed(self):
        url = _alias_param_url("https://x.test/list?page=2", "p",
                               "page", 2, PAGE_SIZE, dead_param="page")
        assert url == "https://x.test/list?p=2"

    def test_offset_and_skip_are_page_sized(self):
        assert _alias_param_url("https://x.test/list", "offset", "offset", 2, PAGE_SIZE).endswith("offset=36")
        assert _alias_param_url("https://x.test/list", "skip", "offset", 3, PAGE_SIZE).endswith("skip=72")

    def test_alias_replaces_the_dead_param_instead_of_appending(self):
        url = _alias_param_url("https://x.test/list?page=2", "currentPage",
                               "page0", 2, PAGE_SIZE, dead_param="page")
        assert "page=2" not in url and url.endswith("currentPage=1")

    def test_build_page_param_url_still_replaces_its_own_param(self):
        """The public builder is unchanged — it only ever replaces the param it
        is given (existing behavior `test_discovery_config.py` pins)."""
        assert build_page_param_url("https://x.test/list?page=2", "page", 3, 36) \
            == "https://x.test/list?page=3"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2 — template output contracts (static; templates are copied verbatim)
# ═══════════════════════════════════════════════════════════════════════════════

def _tpl(name: str) -> str:
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def _tree(name: str) -> ast.AST:
    return ast.parse(_tpl(name))


def _assigned_dicts(tree: ast.AST) -> dict[str, ast.Dict]:
    """`name = { ... }` assignments anywhere in the module (last one wins), so a
    `"discovery_coverage": discovery_coverage` reference can be resolved."""
    out: dict[str, ast.Dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                out[tgt.id] = node.value
    return out


def _literal_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return keys


def _resolve_keys(node: ast.AST, assigned: dict[str, ast.Dict]) -> set[str]:
    """String key set of a dict VALUE — inline literal, or one `Name` hop to the
    dict literal it was built from."""
    if isinstance(node, ast.Dict):
        return _literal_keys(node)
    if isinstance(node, ast.Name) and node.id in assigned:
        return _literal_keys(assigned[node.id])
    return set()


def _value_of(node: ast.Dict, key: str) -> Optional[ast.AST]:
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _blocks_with_key(name: str, key: str) -> list[ast.Dict]:
    """Every dict literal in the template carrying ``key`` (an output block or a
    metadata block)."""
    return [
        node for node in ast.walk(_tree(name))
        if isinstance(node, ast.Dict) and key in _literal_keys(node)
    ]


class TestSiteIsADict:
    def test_ssr_emits_site_as_dict(self):
        assigned = _assigned_dicts(_tree("ssr_div_list_scraper.py"))
        sites = _blocks_with_key("ssr_div_list_scraper.py", "site")
        assert sites, "ssr_div_list emits no `site` key"
        for node in sites:
            value = _value_of(node, "site")
            assert isinstance(value, ast.Dict), (
                "ssr_div_list `site` must be an inline dict — a bare string "
                "makes tasks.py's ground-truth reader crash on .get()"
            )
            assert set(SITE_KEYS) <= _resolve_keys(value, assigned)

    def test_playwright_emits_site_as_dict_everywhere(self):
        """Line ~413 emitted `"site": SITE_NAME` (a string) inside the
        --discover-only block; every `site` in the template must be a dict."""
        assigned = _assigned_dicts(_tree("playwright_scraper.py"))
        sites = _blocks_with_key("playwright_scraper.py", "site")
        assert len(sites) >= 2, "expected the discover-only AND the main output block"
        for node in sites:
            value = _value_of(node, "site")
            assert isinstance(value, ast.Dict), (
                f"playwright_scraper `site` must be a dict, got {ast.dump(value)}"
            )
            assert set(SITE_KEYS) <= _resolve_keys(value, assigned)

    def test_ssr_carries_the_site_constants(self):
        src = _tpl("ssr_div_list_scraper.py")
        for const in ("SITE_NAME", "SITE_URL", "PLATFORM", "SCRAPING_METHOD"):
            assert f"{const} = " in src, f"ssr_div_list missing {const}"


class TestDiscoveryCoverageEmitted:
    def test_both_templates_emit_the_contract_block(self):
        for name in ("ssr_div_list_scraper.py", "playwright_scraper.py"):
            blocks = _blocks_with_key(name, "discovery_coverage")
            assert blocks, f"{name} emits no metadata.discovery_coverage"
            assigned = _assigned_dicts(_tree(name))
            for node in blocks:
                got = _resolve_keys(_value_of(node, "discovery_coverage"), assigned)
                missing = CONTRACT_KEYS - got
                assert not missing, f"{name} discovery_coverage missing {missing}"
                extra = got - CONTRACT_KEYS
                assert extra <= {"pages_visited"}, (
                    f"{name} invented contract keys {extra - {'pages_visited'}}"
                )

    def test_block_is_nested_inside_metadata(self):
        """`_read_discovery_coverage` reads output["metadata"]["discovery_coverage"]."""
        for name in ("ssr_div_list_scraper.py", "playwright_scraper.py"):
            assigned = _assigned_dicts(_tree(name))
            metas = [
                node for node in ast.walk(_tree(name))
                if isinstance(node, ast.Dict) and "metadata" in _literal_keys(node)
            ]
            assert metas, f"{name} emits no metadata dict"
            ok = any(
                "discovery_coverage" in _resolve_keys(_value_of(node, "metadata"), assigned)
                for node in metas
            )
            assert ok, f"{name} does not nest discovery_coverage under metadata"

    def test_reference_templates_still_emit_the_same_contract(self):
        """Mirror check — these three define the contract and must not drift.
        (Read-only: they are NOT modified by this change.)"""
        for name in ("requests_scraper.py", "http_navigation_scraper.py",
                     "navigation_scraper.py"):
            blocks = _blocks_with_key(name, "discovery_coverage")
            assert blocks, f"{name} stopped emitting discovery_coverage"
            assigned = _assigned_dicts(_tree(name))
            for node in blocks:
                got = _resolve_keys(_value_of(node, "discovery_coverage"), assigned)
                assert CONTRACT_KEYS <= got, (
                    f"{name} drifted from contract §1: {CONTRACT_KEYS - got}"
                )

    def test_reader_mirror_parses_an_ssr_shaped_output(self):
        """The exact read path `_read_discovery_coverage` implements
        (webapp/agents/nodes/run_execution.py) applied to the shape the ssr
        template now writes. Mirrored here because importing the webapp module
        needs Django settings."""
        coverage = {
            "stop_reason": "no_new_items",
            "found": 36,
            "discovered_urls": 72,
            "expected_total": None,
            "dimensions_iterated": 0,
            "dimensions_total": 0,
            "max_pages_hit": False,
            "ran_phase1": True,
            "skipped_reason": None,
            "pages_visited": 2,
        }
        site = {"name": "priceline", "url": "https://www.priceline.com.au",
                "platform": "", "scraping_method": "ssr_div_list",
                "scraped_at": "2026-08-27T00:00:00"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output_x.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"site": site, "jobs": [{"title": "a"}],
                           "metadata": {"total_items": 1,
                                        "discovery_coverage": coverage}}, fh)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        metadata = data.get("metadata") or {}
        block = metadata.get("discovery_coverage") if isinstance(metadata, dict) else None
        assert isinstance(block, dict)
        assert block["stop_reason"] == "no_new_items"
        assert block["found"] == 36 and block["discovered_urls"] == 72
        assert data["site"]["scraping_method"] == "ssr_div_list"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 3 — the ssr template's pagination loop, executed with stubbed deps
# ═══════════════════════════════════════════════════════════════════════════════

def _load_scrape_listing(pages: dict[int, list[dict]]):
    """Extract + exec ONLY `scrape_listing` from the template, with every
    external dependency stubbed (bs4 is not importable on the host)."""
    src = _tpl("ssr_div_list_scraper.py")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "scrape_listing")
    namespace = {
        "MAX_PAGES": max(pages) if pages else 1,
        "_construct_page_url": lambda base, n: f"{base}?page={n}",
        "_fetch": lambda url, retry=0: (url, 200, url),  # body == url (the page marker)
        "BeautifulSoup": lambda html, parser: html,
        "_find_items": lambda soup: pages[_page_of(soup)],
        "_extract_record": lambda element, base: dict(element),
        "logger": _NullLogger(),
    }
    exec(compile(ast.get_source_segment(src, fn), "<scrape_listing>", "exec"), namespace)
    return namespace["scrape_listing"]


class _NullLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _page_of(soup) -> int:
    return int(dict(parse_qsl(urlparse(soup).query)).get("page", 1))


class TestSsrLoopContract:
    def test_identical_second_page_reports_no_new_items(self):
        """Job-302 shape: page 2 returns the same containers → the loop stops
        AND reports it (found < discovered_urls is the dedup-flat tell)."""
        dupes = [{"id": f"i{n}", "title": f"t{n}"} for n in range(3)]
        scrape_listing = _load_scrape_listing({1: dupes, 2: dupes})
        records, meta = scrape_listing("https://x.test/list")
        assert len(records) == 3
        assert meta["stop_reason"] == "no_new_items"
        assert meta["discovered_urls"] == 6     # raw containers seen (3 + 3 dupes)
        assert meta["pages_visited"] == 2
        assert meta["max_pages_hit"] is False

    def test_genuine_end_reports_no_next_link(self):
        scrape_listing = _load_scrape_listing({1: [{"id": "a", "title": "a"}], 2: []})
        records, meta = scrape_listing("https://x.test/list")
        assert len(records) == 1
        assert meta["stop_reason"] == "no_next_link"
        assert meta["discovered_urls"] == 1

    def test_fetch_failure_reports_navigate_error(self):
        """navigate_error must be distinguishable from exhaustion (contract §2)."""
        src = _tpl("ssr_div_list_scraper.py")
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "scrape_listing")
        namespace = {
            "MAX_PAGES": 3,
            "_construct_page_url": lambda base, n: f"{base}?page={n}",
            "_fetch": lambda url, retry=0: ("", 503, url),
            "_find_items": lambda soup: [],
            "_extract_record": lambda element, base: dict(element),
            "logger": _NullLogger(),
        }
        exec(compile(ast.get_source_segment(src, fn), "<scrape_listing>", "exec"), namespace)
        records, meta = namespace["scrape_listing"]("https://x.test/list")
        assert records == []
        assert meta["stop_reason"] == "navigate_error"

    def test_max_pages_cap_is_reported(self):
        page1 = [{"id": f"a{n}", "title": f"t{n}"} for n in range(3)]
        page2 = [{"id": f"b{n}", "title": f"u{n}"} for n in range(3)]  # distinct → no stall
        scrape_listing = _load_scrape_listing({1: page1, 2: page2})
        records, meta = scrape_listing("https://x.test/list")
        assert len(records) == 6
        assert meta["max_pages_hit"] is True
        assert meta["stop_reason"] == "max_pages_hit"
        assert meta["pages_visited"] == 2
        assert meta["discovered_urls"] == 6

    def test_limit_truncation_is_not_reported_as_exhaustion(self):
        page1 = [{"id": f"i{n}", "title": f"t{n}"} for n in range(4)]
        page2 = [{"id": f"j{n}", "title": f"u{n}"} for n in range(4)]
        scrape_listing = _load_scrape_listing({1: page1, 2: page2})
        records, meta = scrape_listing("https://x.test/list", limit=2)
        assert len(records) == 2
        assert meta["stop_reason"] == "limit_reached"
        assert meta["max_pages_hit"] is False


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
