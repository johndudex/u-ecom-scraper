"""[wave-19 T0.4] The measured fingerprint identity must survive into state.

Job 324's class of site wins the probe over ``fingerprint_*`` (curl_cffi TLS
impersonation). Two facts the pipeline measured and then THREW AWAY:

- the raw ``needs_browser`` verdict (check_accessibility rewrote it into
  ``js_rendering_needed``, which only feeds prompt text — no logic reads it);
- WHICH impersonation profile won (``chrome`` vs ``safari184``) — Tier-2's
  fingerprint reroute needs this at recipe time, and the probe result is the
  only place it is ever known.

Contract pinned here:
- ``probe_tools.fingerprint_profile()`` parses ``fingerprint_{profile}_{tier}``.
- ``check_accessibility``'s connectivity dict carries the raw ``needs_browser``
  and the parsed ``fingerprint_profile``.
- ``_derive_strategy``'s ``access_recipe`` carries ``fingerprint_profile``
  (PDP method wins; the listing method is the fallback).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents import graph  # noqa: E402
from agents.tools import probe_tools  # noqa: E402


class TestFingerprintProfileParsing:
    @pytest.mark.parametrize(
        "method,expected",
        [
            ("fingerprint_chrome_none", "chrome"),
            ("fingerprint_chrome_datacenter", "chrome"),
            ("fingerprint_safari184_residential", "safari184"),
            ("direct_http", ""),
            ("direct_http_residential", ""),
            ("cloak_none", ""),
            ("playwright_datacenter", ""),
            ("", ""),
        ],
    )
    def test_parse_profile_from_rung_name(self, method, expected):
        assert probe_tools.fingerprint_profile(method) == expected


def _fingerprint_probe_data() -> dict:
    """The funko/324 shape: every legacy rung blocked, fingerprint chrome win."""
    return {
        "success": True,
        "method": "fingerprint_chrome_none",
        "http_method": "fingerprint_chrome_none",
        "browser_method": None,
        "proxy_tier": "none",
        "needs_browser": False,
        "blocked": False,
        "captcha_detected": False,
        "status_code": 200,
        "body_length": 200000,
        "methods_tried": ["direct_http", "fingerprint_chrome_none"],
        "jsonld": [],
        "meta": {},
        "selector_results": {},
    }


class TestConnectivityPreservesRawFlags:
    @pytest.mark.django_db
    def test_connectivity_carries_needs_browser_and_profile(self, monkeypatch):
        monkeypatch.setattr(
            probe_tools,
            "run_probe_with_captcha_check",
            lambda *a, **k: _fingerprint_probe_data(),
        )
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda data: {"captcha_detected": False}
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        state = {
            "job_id": 0,
            "url": "https://www.myhouse.com.au/products/arcosteel-popcorn-maker-black",
            "input_mode": "url_list",
        }
        cmd = graph.check_accessibility(state, None)
        conn = cmd.update["probe_result"]["connectivity"]
        assert conn["method_that_worked"] == "fingerprint_chrome_none"
        # the RAW measured verdict, not just the prompt-text alias
        assert conn["needs_browser"] is False
        # which impersonation profile won — Tier-2 reroute's seed
        assert conn["fingerprint_profile"] == "chrome"


class TestAccessRecipeCarriesProfile:
    def test_pdp_fingerprint_win_lands_in_recipe(self):
        state = {
            "url": "https://www.myhouse.com.au/products/arcosteel-popcorn-maker-black",
            "site_slug": "myhouse-com-au",
            "input_mode": "url_list",
            "probe_result": {
                "connectivity": {"method_that_worked": "fingerprint_chrome_none"},
                "anti_bot": {"detected": False},
            },
        }
        recipe = graph._derive_strategy(state)["access_recipe"]
        assert recipe["fingerprint_profile"] == "chrome"

    def test_listing_fingerprint_is_fallback_when_pdp_not_fingerprint(self):
        state = {
            "url": "https://www.myhouse.com.au/products/arcosteel-popcorn-maker-black",
            "site_slug": "myhouse-com-au",
            "input_mode": "list_page",
            "search_criteria": "https://www.myhouse.com.au/collections/sale-clearance",
            "probe_result": {
                "connectivity": {"method_that_worked": "cloak_none"},
                "anti_bot": {"detected": False},
                "listing_connectivity": {
                    "method_that_worked": "fingerprint_safari184_datacenter",
                    "needs_browser": False,
                    "blocked": False,
                },
            },
            "navigation_analysis": {"discovery": {"listing_reached": True}},
        }
        recipe = graph._derive_strategy(state)["access_recipe"]
        assert recipe["fingerprint_profile"] == "safari184"

    def test_no_fingerprint_evidence_gives_empty_profile(self):
        state = {
            "url": "https://www.crocs.com/p/kids-classic-clog/206991.html",
            "site_slug": "crocs-com",
            "input_mode": "url_list",
            "probe_result": {
                "connectivity": {"method_that_worked": "playwright_datacenter"},
                "anti_bot": {"detected": True},
            },
        }
        recipe = graph._derive_strategy(state)["access_recipe"]
        assert recipe["fingerprint_profile"] == ""
