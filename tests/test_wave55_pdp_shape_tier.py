"""[wave-55 C3] The tester's verdict is not a gate — PDP-shape tier + logged override.

Audit C3 (2026-10-05): 88/616 COMPLETED jobs executed past a NOT-ready tester
verdict through the UNLOGGED ground-truth override, and junk shipped on
tester-PASS jobs too (19-26%). Two named examples drive the shape work:

- job 1200 (amxsuperstores): 5/5 rows were ``/motorcycle-gear/...`` category
  pages past "Assessment: FAIL / Ready for execution: false" — zero
  ``/products/<slug>`` rows;
- job 1068 (cartier): one title across ``/en-us/watches/collections/tank/``
  collection URLs, tester PASS.

Four sub-fixes, pinned here:

1. ``row_url_is_item_shaped`` gains a PDP-shape tier: a row URL that is a
   nav/listing position (``/collections/...``, ``/new-in``, ``/search?q=``,
   the bare ``/products`` index) fails the shape; the job-499 ``/#main``
   floor is byte-identical; PDP-ish paths pass.
2. BOTH ground-truth consumers apply the tiered predicate:
   ``route_after_testing._scraper_has_real_items`` (via ``_row_url_has_path``)
   and ``tasks._good_rows`` (which had NO row-URL floor at all).
3. The override arm logs ``[CASCADE] action=ground-truth-override`` like the
   run_execution return at route_after_testing ``:1509`` does.
4. A confident tester FAIL (FAIL + ``ready_for_execution is False`` + retries
   left) vetoes the override and falls through to the existing fix ladder;
   retries exhausted keeps the documented escape hatch.

The tier is calibrated against the repo's own promoted corpus
(``scrapers/*/input_urls.json``, 11,116 real discovered item URLs): every nav
token in the set fails ZERO of them, so the floor cannot unseat a working
scraper.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import importlib  # noqa: E402

import pytest  # noqa: E402

from src.discovery_quality import row_url_is_item_shaped  # noqa: E402

# The nodes package re-exports a NODE FUNCTION named route_after_testing, so a
# plain `from ... import route_after_testing` binds the function, not the
# module (test_wave51_nonitem_rows.py hits the same seam).
_rat = importlib.import_module("webapp.agents.nodes.route_after_testing")

scraper_tasks = pytest.importorskip("scraper.tasks")


# ── 1. the PDP-shape tier ────────────────────────────────────────────────────


class TestPdpShapeTier:
    @pytest.mark.parametrize(
        "url",
        [
            # job 1068 (cartier) — collection pages shipped as items.
            "https://www.cartier.com/en-us/watches/collections/tank/",
            "https://www.cartier.com/en-us/watches/collections/tank",
            # wave-50's junk set shape — section links under a listing.
            "https://shop.example/new-in/gifts",
            "https://shop.example/new-in/?sort=NEW_ARRIVAL_DESC",
            # bare section / search / category positions.
            "https://shop.example/new-in",
            "https://shop.example/search?q=air+fryer",
            "https://shop.example/c/sale",
            "https://shop.example/categories",
            "https://shop.example/browse/dept",
            # the bare index a shallow crawl ships when discovery gave up.
            "https://shop.example/products",
            "https://shop.example/collections",
            "https://shop.example/bestsellers",
        ],
    )
    def test_listing_or_nav_position_fails(self, url):
        assert row_url_is_item_shaped(url) is False, url

    @pytest.mark.parametrize(
        "url",
        [
            "https://shop.example/products/russell-hobbs-air-fryer",
            "https://shop.example/collections/kitchen/products/air-fryer-xl",
            "https://shop.example/product/1136060/russell-hobbs-air-fryer/",
            "https://shop.example/p/12345?variant=9",
            "https://shop.example/dp/B08N5WRWNW",
            "https://shop.example/item/98765",
            # digits / sku and a file suffix are PDP signals on their own.
            "https://shop.example/alyshia-black-2",
            "https://shop.example/adult-sex-toys/clit-vibrators/foo-112697.aspx",
            "https://shop.example/catalog/product/view/id/8811",
        ],
    )
    def test_pdp_shaped_passes(self, url):
        assert row_url_is_item_shaped(url) is True, url

    @pytest.mark.parametrize(
        "url",
        [
            # job 499 — the floor this tier must not disturb (byte-identical).
            "https://www.briscoes.co.nz/#main",
            "https://www.briscoes.co.nz/",
            "https://www.briscoes.co.nz",
            "#main",
            "",
            None,
        ],
    )
    def test_no_path_floor_is_unchanged(self, url):
        assert row_url_is_item_shaped(url) is False, url

    def test_root_level_plain_slug_still_passes(self):
        """adoreme ships real PDPs at a single plain slug (256 of them in the
        promoted corpus) — a single unknown slug is NOT provably a nav
        position, so the floor keeps passing it."""
        assert row_url_is_item_shaped("https://www.adoreme.com/sissel-dark-purple")

    def test_deep_plain_slug_paths_still_pass(self):
        """arkswimwear/jayjays ship real PDPs as 4 plain-slug segments (935 in
        the promoted corpus). That is the SAME shape as job 1200's junk
        category paths, so no URL-shape rule can separate them — the floor
        deliberately stays out of that fight (content-level bars own it)."""
        assert row_url_is_item_shaped(
            "https://www.arkswimwear.com/intl/shop/womens-bikini-tops/black-triangle-bikini-top"
        )


# ── 2. BOTH ground-truth consumers apply the tier ────────────────────────────


_NAV_ROW = {
    "title": "Watch Collections",
    "price": 7000.0,
    "currency": "USD",
    # job 1068's shape: a collection path with a defaulted price on it.
    "url": "https://www.cartier.com/en-us/watches/collections/tank/",
}


def _pdp_row(i: int) -> dict:
    return {
        "title": f"Helmet {i}",
        "price": 100.0 + i,
        "currency": "AUD",
        "url": f"https://www.amxsuperstores.com.au/products/helmet-{i}",
    }


class TestBothConsumersApplyTheTier:
    def test_router_ground_truth_ignores_nav_rows(self):
        state = {
            "site_slug": "",
            "input_mode": "url_list",
            "content_type_config": {"content_type": "product",
                                    "output_key": "products"},
            "test_report": {"sample_products": [_NAV_ROW, dict(_NAV_ROW),
                                                dict(_NAV_ROW)]},
        }
        assert _rat._scraper_has_real_items(state, min_count=1) is False

    def test_router_ground_truth_still_counts_pdp_rows(self):
        state = {
            "site_slug": "",
            "input_mode": "url_list",
            "content_type_config": {"content_type": "product",
                                    "output_key": "products"},
            "test_report": {"sample_products": [_pdp_row(1), _pdp_row(2)]},
        }
        assert _rat._scraper_has_real_items(state, min_count=1) is True

    def test_tasks_good_rows_ignores_nav_rows(self):
        data = {"products": [_NAV_ROW, dict(_NAV_ROW), dict(_NAV_ROW)]}
        assert scraper_tasks._good_rows(data, ["price"], "products") == []

    def test_tasks_good_rows_keeps_pdp_rows(self):
        data = {"products": [_pdp_row(1), _pdp_row(2)]}
        rows = scraper_tasks._good_rows(data, ["price"], "products")
        assert len(rows) == 2

    def test_tasks_good_rows_applies_the_tier_on_the_substantive_branch_too(self):
        """The no-filter-fields branch (``has_substantive_field``) must carry
        the same floor — a nav row with a title is not evidence either."""
        data = {"products": [dict(_NAV_ROW, price=None)]}
        assert scraper_tasks._good_rows(data, [], "products") == []

    def test_tasks_good_rows_fails_open_when_a_row_has_no_url(self):
        """Rows with no URL key are other bars' verdict (the router's
        ``_row_url_has_path`` contract) — the rescue must not go blind."""
        data = {"products": [{"title": "Widget", "price": 5.0},
                             {"title": "Gadget", "price": 7.0}]}
        assert len(scraper_tasks._good_rows(data, ["price"], "products")) == 2


# ── 3. + 4. the override arm: logged, and vetoed on a confident FAIL ─────────


def _state(rows, *, assessment="FAIL", ready=None, retry=0, issues=None):
    report = {
        "overall_assessment": assessment,
        "confidence_score": 0.8,
        "issues": issues if issues is not None else [],
        "sample_products": rows,
    }
    if ready is not None:
        report["ready_for_execution"] = ready
    return {
        "input_mode": "url_list",
        "content_type_config": {"content_type": "product",
                                "output_key": "products"},
        "test_report": report,
        "test_retry_count": retry,
    }


class TestOverrideArm:
    @pytest.fixture
    def cascades(self, monkeypatch):
        seen = []

        def _spy(state, action, reason):
            seen.append((action, reason))

        monkeypatch.setattr(_rat, "_log_cascade", _spy)
        return seen

    def test_override_fires_and_logs_a_cascade_row(self, cascades):
        """The 88-job class is invisible today: the arm only logger.info'd.
        It must leave a [CASCADE] row like every sibling arm."""
        state = _state([_pdp_row(i) for i in range(3)], assessment="FAIL",
                       ready=True)
        assert _rat.route_after_testing(state) == "field_confirmation"
        actions = [a for a, _ in cascades]
        assert "ground-truth-override" in actions

    def test_confident_fail_with_retries_left_vetoes_the_override(
        self, cascades
    ):
        """Job 1200's exact shape: FAIL + ready_for_execution=false + retries
        left. The override must not ship it — route back into the fix loop."""
        state = _state([_pdp_row(i) for i in range(3)], assessment="FAIL",
                       ready=False, retry=0)
        state["scraper_analysis"] = {"strategy": "http_requests"}
        dest = _rat.route_after_testing(state)
        assert dest != "field_confirmation"
        assert dest in ("code_writer", "product_analyzer", "scraper_analyzer",
                        "code_tester")
        assert "ground-truth-override" not in [a for a, _ in cascades]

    def test_confident_fail_veto_is_logged(self, cascades):
        state = _state([_pdp_row(i) for i in range(3)], assessment="FAIL",
                       ready=False, retry=0)
        state["scraper_analysis"] = {"strategy": "http_requests"}
        _rat.route_after_testing(state)
        assert any(a == "confident-fail-veto" for a, _ in cascades), cascades

    def test_retries_exhausted_override_still_allowed_and_logged(self, cascades):
        """The documented escape hatch: with the fix budget gone the override
        may stand — but it is now on the record."""
        state = _state([_pdp_row(i) for i in range(3)], assessment="FAIL",
                       ready=False, retry=_rat.MAX_TEST_RETRIES)
        assert _rat.route_after_testing(state) == "field_confirmation"
        assert "ground-truth-override" in [a for a, _ in cascades]

    def test_a_pass_verdict_is_not_vetoed(self, cascades):
        """The veto is a FAIL verdict's veto only — a PASS with real items
        keeps its normal routing."""
        state = _state([_pdp_row(i) for i in range(3)], assessment="PASS",
                       ready=False, retry=0)
        assert _rat.route_after_testing(state) == "field_confirmation"

    def test_ready_for_execution_absent_is_not_a_veto(self, cascades):
        """Legacy reports carry no key — only an EXPLICIT false vetoes."""
        state = _state([_pdp_row(i) for i in range(3)], assessment="FAIL",
                       ready=None, retry=0)
        assert _rat.route_after_testing(state) == "field_confirmation"


# ── corpus guard: the tier must not unseat a promoted scraper ───────────────

_DQ = importlib.import_module("src.discovery_quality")


def _pdp_signalled(url: str) -> bool:
    """The pass-half of the tier, restated for the corpus guard: a PDP token
    in a multi-segment path, or an ID/suffix-shaped last segment. (A bare
    `/p` is a bare index, not a detail page — the tier may reject it.)"""
    segs = [s.lower() for s in urlsplit(str(url)).path.split("/") if s]
    if len(segs) <= 1:
        return False
    if any(s in _DQ._PDP_SEGMENT_TOKENS for s in segs):
        return True
    last = segs[-1]
    return bool(_DQ._DIGIT_RE.search(last) or _DQ._SUFFIX_RE.search(last))


_PROMOTED: list = []


def _promoted_urls():
    """Every item URL the promoted scrapers on this disk judged real
    (dev bind-mount only; the guards skip when there is none)."""
    import glob
    import json

    if _PROMOTED:
        return _PROMOTED
    found = []
    for path in sorted(glob.glob(os.path.join(ROOT, "scrapers", "*",
                                             "input_urls.json"))):
        try:
            payload = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        urls = payload.get("urls") if isinstance(payload, dict) else payload
        if isinstance(urls, list):
            found.extend(
                u for u in urls if isinstance(u, str) and u.strip()
                and urlsplit(u).path.strip("/")
            )
    for path in sorted(glob.glob(os.path.join(ROOT, "scrapers", "*",
                                              "output_*.json")))[:200]:
        try:
            payload = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for key in ("products", "items", "jobs", "articles"):
            rows = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            for r in rows:
                if not isinstance(r, dict):
                    continue
                u = r.get("url") or r.get("product_url") or ""
                if u and urlsplit(str(u)).path.strip("/"):
                    found.append(u)
    _PROMOTED.extend(found)
    return _PROMOTED


def test_promoted_pdp_signalled_urls_survive_the_tier():
    """The guarantee that matters: a promoted scraper's PDP-SIGNALLED item
    URLs (a products/product/p/dp token, a digit run, a file suffix) all
    survive the tier, so no working discovery can be unseated by it."""
    urls = _promoted_urls()
    if not urls:
        pytest.skip("no promoted scrapers on disk")
    lost = [u for u in set(urls) if _pdp_signalled(u)
            and not row_url_is_item_shaped(u)]
    assert not lost, f"{len(lost)} PDP-signalled item URLs now fail: {lost[:5]}"


def test_promoted_rejections_are_nav_positions():
    """Census over real promoted data: every URL the tier rejects must be a
    nav/listing position BY SHAPE (no PDP signal at all). The rejections this
    disk actually sees are the audit's junk class — `/collections/<slug>`,
    `/new-in`, `/shop/browse/...` rows shipped as products — never a plain
    PDP path. The <1% bound keeps a future runaway token from hiding here."""
    urls = _promoted_urls()
    if not urls:
        pytest.skip("no promoted scrapers on disk")
    rejected = sorted({u for u in urls if not row_url_is_item_shaped(u)})
    assert len(rejected) <= len(set(urls)) // 100, (
        f"{len(rejected)}/{len(set(urls))} promoted item URLs rejected — "
        f"first 5: {rejected[:5]}"
    )
    wrongly = [u for u in rejected if _pdp_signalled(u)]
    assert not wrongly, (
        f"{len(wrongly)} PDP-signalled URLs rejected: {wrongly[:5]}"
    )
