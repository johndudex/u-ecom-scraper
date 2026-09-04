"""[wave-17 S17-S20] Per-class access identity — the fixes the crocs drive-3
e2e failure surfaced (job 325: discovery empty_render ×3 + phase-2 titles
collapsing to the "www.crocs.com" domain placeholder with empty price).

Measured live 2026-09-03 (probe-single, real crocs pages):

    ┌──────────────────────┬────────────┬──────────────────────────────────┐
    │ identity             │ listing    │ PDP                              │
    ├──────────────────────┼────────────┼──────────────────────────────────┤
    │ cloak_residential    │ 429 + 134K │ 403 challenge                    │
    │ cloak_datacenter     │ 429 + 158K │ 200 + 454K                       │
    │ playwright_datacenter│ —          │ 403 challenge                    │
    │ cloak_none           │ 403        │ —                                │
    └──────────────────────┴────────────┴──────────────────────────────────┘

S17  One recipe tier for two URL classes is wrong when the classes measure
     different working tiers. access_recipe now carries proxy_tier (the
     PDP's) AND discovery_proxy_tier (the listing's); the old S4 rule that
     let the listing tier raise the extraction tier is gone.
S18  playwright_scraper.py launched ALL THREE discovery browsers with NO
     proxy (get_browser(p)) and a TRUNCATED bot-like UA — the recipe was
     resolved at module level and thrown away at launch. Discovery now
     launches with the discovery tier + the full coherent Chrome UA.
S20  src/discovery.py::_discovery_goto hard-failed on HTTP 429/5xx — but
     crocs' listing serves the full catalogue BEHIND a 429. Content over
     status, at the discovery layer.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()


# ─── state builders ──────────────────────────────────────────────────────────


def _crocs_state() -> dict:
    """The measured crocs shape: PDP datacenter-only, listing residential."""
    return {
        "url": "https://www.crocs.com/p/kids-classic-clog/206991.html",
        "site_slug": "crocs-com",
        "input_mode": "list_page",
        "search_criteria": "https://www.crocs.com/c/kids/footwear",
        "probe_result": {
            "connectivity": {"method_that_worked": "playwright_datacenter"},
            "anti_bot": {"detected": True},
            "listing_connectivity": {
                "probed_url": "https://www.crocs.com/c/kids/footwear",
                "advisory": True,
                "method_that_worked": "cloak_residential",
                "http_ok": False,
                "browser_ok": True,
                "needs_browser": True,
                "blocked": False,
                "methods_tried": [
                    "cloak_none", "cloak_datacenter", "cloak_residential",
                ],
            },
        },
        "navigation_analysis": {"discovery": {"listing_reached": True}},
    }


# ─── S17: _derive_strategy carries per-class tiers ───────────────────────────


class TestDeriveStrategyPerClassTiers:
    def test_recipe_splits_tiers_when_classes_differ(self):
        from agents.graph import _derive_strategy

        analysis = _derive_strategy(_crocs_state())
        recipe = analysis["access_recipe"]
        assert recipe["proxy_tier"] == "datacenter", (
            "the extraction tier is the PDP's measured tier — the listing's "
            "residential tier must NOT raise it (the S4 raise is what sent "
            "every phase-2 crocs fetch into a perpetual challenge)"
        )
        assert recipe["discovery_proxy_tier"] == "residential"
        assert recipe["stealth"] == "cloak"
        assert "pdp_method=playwright_datacenter" in recipe["evidence"]
        assert "listing_method=cloak_residential" in recipe["evidence"]

    def test_single_identity_site_gets_equal_tiers(self):
        from agents.graph import _derive_strategy

        state = {
            "url": "https://example.com/p/1",
            "input_mode": "list_page",
            "probe_result": {
                "connectivity": {"method_that_worked": "cloak_none"},
                "anti_bot": {"detected": True},
                "listing_connectivity": {
                    "method_that_worked": "cloak_none",
                    "needs_browser": True,
                    "blocked": False,
                },
            },
        }
        recipe = _derive_strategy(state)["access_recipe"]
        assert recipe["proxy_tier"] == "none"
        assert recipe["discovery_proxy_tier"] == "none"

    def test_no_listing_evidence_discovery_tier_defaults_to_pdp_tier(self):
        from agents.graph import _derive_strategy

        state = {
            "url": "https://example.com/p/1",
            "input_mode": "url_list",
            "probe_result": {
                "connectivity": {"method_that_worked": "cloak_residential"},
                "anti_bot": {"detected": True},
            },
        }
        recipe = _derive_strategy(state)["access_recipe"]
        assert recipe["proxy_tier"] == "residential"
        assert recipe["discovery_proxy_tier"] == "residential"


# ─── S22: blocked-listing evidence outranks one clean PDP snapshot ───────────


def _blocked_listing_state(statuses: tuple[int, ...] = (403, 403, 403),
                          blocked_flags: tuple[bool, ...] | None = None):
    """The measured 327 shape: PDP answered clean once (playwright_datacenter,
    anti_bot.detected=False) while EVERY listing rung was refused."""
    flags = blocked_flags or (True,) * len(statuses)
    rungs = ["cloak_none", "cloak_datacenter", "cloak_residential"]
    return {
        "url": "https://www.crocs.com/p/kids-classic-clog/206991.html",
        "site_slug": "crocs-com",
        "input_mode": "list_page",
        "search_criteria": "https://www.crocs.com/c/kids/footwear",
        "probe_result": {
            "connectivity": {"method_that_worked": "playwright_datacenter"},
            "anti_bot": {"detected": False},
            "listing_connectivity": {
                "probed_url": "https://www.crocs.com/c/kids/footwear",
                "advisory": True,
                "method_that_worked": "",
                "http_ok": False,
                "browser_ok": False,
                "needs_browser": False,
                "blocked": True,
                "status_code": 0,
                "body_length": 0,
                "methods_tried": rungs,
                "attempts": [
                    {
                        "method": rung, "success": False,
                        "status_code": status, "body_length": 28_711,
                        "blocked": flag,
                    }
                    for rung, status, flag in zip(rungs, statuses, flags)
                ],
            },
        },
        "navigation_analysis": {"discovery": {"listing_reached": False}},
    }


class TestDeriveStrategyBlockEvidence:
    def test_all_rungs_blocked_forces_stealth_despite_clean_pdp(self):
        """[S22, the 327 defect] anti_bot.detected=False from ONE clean PDP
        snapshot must not out-vote a listing probe refused on every rung —
        stealth="none" launched vanilla Chromium against a Cloudflare
        listing and the drive burned before it started."""
        from agents.graph import _derive_strategy

        recipe = _derive_strategy(_blocked_listing_state())["access_recipe"]
        assert recipe["stealth"] == "cloak"
        assert recipe["proxy_tier"] == "datacenter"
        assert recipe["evidence"].endswith("listing_method=blocked")

    def test_plain_404_rungs_never_count_as_block_evidence(self):
        """A listing that 404s on every identity is a wrong URL, not an
        anti-bot verdict — stealth must stay unforced."""
        from agents.graph import _derive_strategy

        recipe = _derive_strategy(
            _blocked_listing_state(statuses=(404, 404, 404),
                                   blocked_flags=(False, False, False))
        )["access_recipe"]
        assert recipe["stealth"] == "none"

    def test_needs_cloak_honors_the_folded_recipe(self):
        """The runtime stagers read recipe.stealth — the S22 fold must reach
        the draft subprocess env, not stop at the analysis artifact."""
        from agents.nodes.run_execution import _needs_cloak

        state = _blocked_listing_state()
        state["scraper_analysis"] = {
            "access_recipe": {
                "stealth": "cloak",
                "proxy_tier": "datacenter",
                "discovery_proxy_tier": "datacenter",
            },
        }
        assert _needs_cloak(state) is True


# ─── S17: env staging split (run_execution) ─────────────────────────────────


class TestScraperProxyTierSplit:
    def test_extraction_tier_is_pdp_only_listing_no_longer_raises(self):
        from agents.nodes.run_execution import _scraper_proxy_tier

        assert _scraper_proxy_tier(_crocs_state()) == "datacenter"

    def test_discovery_tier_prefers_recipe_then_listing(self):
        from agents.nodes.run_execution import (
            _discovery_proxy_tier,
            _scraper_proxy_tier,
        )

        assert _discovery_proxy_tier(_crocs_state()) == "residential"

        listing_only = {
            "probe_result": {
                "listing_connectivity": {"method_that_worked": "cloak_none"},
            },
        }
        assert _discovery_proxy_tier(listing_only) == ""

    def test_stealth_env_stages_both_vars_only_when_tiers_differ(self):
        from agents.nodes.run_execution import _stealth_env

        env = _stealth_env(_crocs_state())
        assert env["STEALTH_BROWSER"] == "cloak"
        assert env["SCRAPER_PROXY_TIER"] == "datacenter"
        assert env["SCRAPER_DISCOVERY_PROXY_TIER"] == "residential"

        same = _crocs_state()
        same["probe_result"]["listing_connectivity"][
            "method_that_worked"
        ] = "cloak_datacenter"
        same["scraper_analysis"] = {
            "access_recipe": {
                "stealth": "cloak",
                "proxy_tier": "datacenter",
                "discovery_proxy_tier": "datacenter",
            },
        }
        env2 = _stealth_env(same)
        assert env2["SCRAPER_PROXY_TIER"] == "datacenter"
        assert "SCRAPER_DISCOVERY_PROXY_TIER" not in env2, (
            "same-tier is the common case — staging the discovery var then "
            "would only invite drift"
        )


# ─── S17: shell_tools stages the discovery tier for the tester ┬─────────────


class TestShellToolsDiscoveryTierStaging:
    def test_recipe_discovery_tier_reaches_run_scraper(self):
        src = open(
            os.path.join(ROOT, "webapp", "agents", "tools", "shell_tools.py"),
            encoding="utf-8",
        ).read()
        assert 'discovery_proxy_tier' in src
        assert 'SCRAPER_DISCOVERY_PROXY_TIER' in src
        assert "_dtier != _rtier" in src


# ─── S18: template discovery launches carry the recipe identity ──────────────


class TestTemplateDiscoveryIdentity:
    SRC = open(
        os.path.join(ROOT, "templates", "playwright_scraper.py"),
        encoding="utf-8",
    ).read()

    def test_all_three_discovery_launches_pass_the_proxy(self):
        assert self.SRC.count("get_browser(p, proxy=_DISCOVERY_PROXY)") == 3, (
            "discovery gate + fallback discovery + --discover-only probe: "
            "every Phase-1 launch site must ride the discovery tier"
        )

    def test_discovery_tier_env_override_present(self):
        assert "SCRAPER_DISCOVERY_PROXY_TIER" in self.SRC
        assert "DISCOVERY_PROXY_TIER" in self.SRC

    def test_truncated_bot_ua_is_gone(self):
        assert (
            'user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36"' not in self.SRC
        ), (
            "the truncated UA (no Chrome version, no Safari tail) is a bot "
            "tell that out-ranks stealth binaries and proxies on managed-"
            "challenge sites — the full coherent UA (USER_AGENT) everywhere"
        )
        assert "USER_AGENT" in self.SRC

    def test_s21_stealth_launches_keep_the_binary_native_ua(self):
        """[S21] Under STEALTH_BROWSER=cloak every context launches with the
        stealth binary's NATIVE UA (``_LAUNCH_UA`` is None → Playwright
        default). The measured crocs failure: probe's cloak rung (no UA
        stamp) read the listing as 429 + real catalogue; the draft's launches
        (same binary, same tier, Chrome/120 stamped on top) sat in a held
        challenge — the fingerprint/UA mismatch, not the identity, was the
        block."""
        assert self.SRC.count("user_agent=_LAUNCH_UA") == 4, (
            "3 discovery launches + the phase-2 extraction launch"
        )
        assert "user_agent=USER_AGENT" not in self.SRC, (
            "no launch site may force the hardcoded UA past the stealth gate"
        )
        assert 'os.environ.get("STEALTH_BROWSER"' in self.SRC
        assert "_STEALTH_NATIVE" in self.SRC and "_LAUNCH_UA" in self.SRC

    def test_generation_placeholders_intact(self):
        for ph in ("{SITE_NAME}", "{PROXY_TIER}", "{PRODUCT_LISTING_URL}"):
            assert ph in self.SRC


# ─── S20: discovery judges content, not status codes ─────────────────────────


class _Resp:
    def __init__(self, status: int):
        self.status = status


class _GotoPage:
    """PageLike double whose DOM materializes after `ready` reads."""

    def __init__(self, status: int, ready_after_reads: int = 0):
        self._status = status
        self._reads = 0
        self._ready_after = ready_after_reads
        self.waits: list[int] = []

    def goto(self, url: str, timeout: int = 0) -> _Resp:
        return _Resp(self._status)

    def evaluate(self, js, *args):
        self._reads += 1
        if self._reads <= self._ready_after:
            return 0
        return 25 if "a[href]" in js else 800

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def wait_for_load_state(self, state):
        pass


def _cfg(settle_s: float = 2.0):
    from src.discovery import DiscoveryConfig

    return DiscoveryConfig(page_settle_after_nav_s=settle_s)


class TestDiscoveryContentOverStatus:
    def test_429_with_usable_content_proceeds(self):
        from src.discovery import _discovery_goto

        page = _GotoPage(status=429, ready_after_reads=1)
        state: dict = {"stop_reason": "no_next_link"}
        assert _discovery_goto(page, "https://x.com/c/shoes", _cfg(), state)
        assert state["stop_reason"] != "navigate_error"
        assert page.waits, "content check settles before judging"

    def test_429_with_held_block_fails_honestly(self):
        from src.discovery import _discovery_goto

        page = _GotoPage(status=429, ready_after_reads=10**9)
        state: dict = {"stop_reason": "no_next_link"}
        assert not _discovery_goto(page, "https://x.com/c/shoes", _cfg(), state)
        assert state["stop_reason"] == "navigate_error"

    def test_503_with_real_dom_proceeds(self):
        from src.discovery import _discovery_goto

        page = _GotoPage(status=503, ready_after_reads=2)
        state: dict = {"stop_reason": "no_next_link"}
        assert _discovery_goto(page, "https://x.com/c/shoes", _cfg(), state)

    def test_200_fast_path_no_extra_polling(self):
        from src.discovery import _discovery_goto

        page = _GotoPage(status=200, ready_after_reads=0)
        state: dict = {"stop_reason": "no_next_link"}
        assert _discovery_goto(page, "https://x.com/c/shoes", _cfg(), state)
        assert page.waits == [], "healthy status must not pay the settle"

    def test_status_page_usable_thresholds(self):
        from src.discovery import _status_page_usable

        assert _status_page_usable(_GotoPage(200, ready_after_reads=0), 2.0)
        assert not _status_page_usable(_GotoPage(403, ready_after_reads=10**9), 1.0)

    def test_usable_poll_is_iteration_bounded(self):
        """The never-materializing path must exit on the poll cap, not spin
        hot on wall clock for the whole 20s window (fakes/wait-free runners
        would burn the full deadline at CPU speed)."""
        from src.discovery import _status_page_usable

        page = _GotoPage(429, ready_after_reads=10**9)
        assert _status_page_usable(page, 1.0) is False
        assert len(page.waits) <= 30

    def test_goto_logs_status_for_every_unhealthy_response(self, caplog):
        """[observability] The first crocs goto's status was unlogged — the
        RCA could not tell challenge from rate-limit-with-content from slow
        hydration. Every >=400 navigation now leaves its status in the log."""
        import logging as _logging

        from src.discovery import _discovery_goto

        page = _GotoPage(status=403, ready_after_reads=10**9)
        state: dict = {"stop_reason": "no_next_link"}
        with caplog.at_level(_logging.INFO, logger="src.discovery"):
            _discovery_goto(page, "https://x.com/c/shoes", _cfg(0.0), state)
        assert any(
            "HTTP 403" in rec.message for rec in caplog.records
        ), [rec.message for rec in caplog.records]


# ─── S23: a challenge page is a failure, not a product ───────────────────────


def _load_s23_helpers():
    """Pull the S23 constants + _looks_challenged out of the template with ast
    and exec them alone — importing the whole template module would drag in
    playwright and run its module-level launch config."""
    import ast

    tree = ast.parse(
        open(os.path.join(ROOT, "templates", "playwright_scraper.py"),
             encoding="utf-8").read()
    )
    wanted = {"_CHALLENGE_TITLE_MARKERS", "_SUBSTANTIVE_FIELDS",
              "_CHALLENGE_RETRY_SETTLE_MS", "_looks_challenged"}
    nodes = []
    for node in tree.body:
        names = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.FunctionDef):
            names = [node.name]
        if any(n in wanted for n in names):
            nodes.append(node)
    ns: dict = {}
    for node in nodes:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<s23>", "exec"), ns)
    assert "_looks_challenged" in ns, nodes
    return ns


class TestS23ChallengeTripwire:
    SRC = open(
        os.path.join(ROOT, "templates", "playwright_scraper.py"),
        encoding="utf-8",
    ).read()

    def test_real_product_is_never_flagged(self):
        fn = _load_s23_helpers()["_looks_challenged"]
        assert not fn({"title": "Kids' Classic Clog",
                       "price": "$39.99", "sku": "206991"})
        # Title-only (an OOS PDP with no price) and price-only (selector
        # drift on title) both carry substance — neither may be flagged.
        assert not fn({"title": "Kids' Classic Clog"})
        assert not fn({"price": "$39.99"})

    def test_challenge_interstitial_is_flagged(self):
        """The measured 328 shape: the Cloudflare interstitial WAS the
        extraction, reported as a product with status_code=200."""
        fn = _load_s23_helpers()["_looks_challenged"]
        assert fn({"title": "Just a moment...", "price": "", "sku": "10001"})
        assert fn({"title": "Attention Required! | Cloudflare"})
        assert fn({"title": "Access Denied"})

    def test_blank_extraction_is_flagged(self):
        """A page yielding no title AND no substantive field is
        indistinguishable from an interstitial at extraction level."""
        fn = _load_s23_helpers()["_looks_challenged"]
        assert fn({"title": "", "price": "", "availability": ""})

    def test_tripwire_sits_between_extract_and_emit(self):
        """scrape_product must check, retry once, and return an honest
        failure item — the main loop's _BK validity check then counts it
        as failed (every substantive field emptied)."""
        assert self.SRC.count("_looks_challenged(data)") == 2, (
            "checked once after extraction, once after the bounded retry"
        )
        assert "_CHALLENGE_RETRY_SETTLE_MS" in self.SRC
        assert self.SRC.count('wait_until="domcontentloaded"') >= 1, (
            "the challenge retry rides domcontentloaded — the interstitial's "
            "beacon traffic never reaches networkidle"
        )

    def test_honest_failure_item_shape(self):
        """No challenge title is ever emitted; the block class is a 403 with
        remarks naming the challenge - failed_products stays honest."""
        assert '"status_code": 403' in self.SRC
        assert "anti-bot challenge persisted after retry" in self.SRC
        anchor = self.SRC.index('"status_code": 403')
        block = self.SRC[anchor - 400:anchor + 400]
        assert '"title": "",' in block
        head, remarks = block.split('"remarks"', 1)
        assert "challenged_title" in remarks, (
            "the challenge wording belongs in remarks for the RCA trail"
        )
        assert "challenged_title" not in head, (
            "the challenge wording must never land in a data field"
        )
