"""[wave-19 T0.3] HTTP-vs-browser classification must be prefix-derived.

``HTTP_METHODS`` was a hard-coded set of the three ``direct_http*`` rungs, so a
``fingerprint_*`` win (curl_cffi TLS impersonation — HTTP-flavoured, NOT a
browser step) was mislabeled everywhere that set was consulted:

- the listing-probe classification called a fingerprint winner
  ``browser_ok=True / needs_browser=True``, which force-upgrades discovery to
  the S4 browser rung even though the site accepts plain HTTP with a browser
  TLS fingerprint;
- the PDP-won seeding refused to seed a ``fingerprint_*`` rung into the
  listing probe, so that cheapest shared transport was never tried.

``_HTTP_METHOD_PREFIXES`` already exists (probe_tools.py:70, used at :298) —
the three consult sites above it just never used it.
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

from agents.tools import probe_tools  # noqa: E402


class TestIsHttpMethod:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("direct_http", True),
            ("direct_http_datacenter", True),
            ("direct_http_residential", True),
            ("fingerprint_chrome_none", True),
            ("fingerprint_safari184_residential", True),
            ("cloak_none", False),
            ("cloak_residential", False),
            ("playwright_none", False),
            ("uc_chrome_none", False),
            ("", False),
        ],
    )
    def test_prefix_classification(self, name, expected):
        assert probe_tools._is_http_method(name) is expected


class TestListingProbeFingerprint:
    """PDP won over fingerprint HTTP → listing probe must try that rung first
    and classify its win as HTTP, not browser."""

    @pytest.fixture
    def probe_calls(self, monkeypatch):
        """Stub /probe-single: success ONLY on the fingerprint rung."""
        import httpx

        from agents.tools import probe_tools as pt

        calls: list[str] = []

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {}

        def _post(url, json=None, timeout=None):
            method = (json or {}).get("method", "")
            calls.append(method)
            payload = {
                "fingerprint_chrome_none": {
                    "success": True,
                    "status_code": 200,
                    "body_length": 40000,
                    "blocked": False,
                }
            }.get(method, {"success": False, "status_code": 403, "body_length": 300, "blocked": True})

            class _RespWith(_Resp):
                def json(self):
                    return payload

            return _RespWith()

        monkeypatch.setattr(pt, "_get_browser_service_url", lambda: "http://browser-service")
        import src.geo as geo

        monkeypatch.setattr(geo, "detect_country", lambda url: "au")
        monkeypatch.setattr(httpx, "post", _post)
        return calls

    def test_fingerprint_pdp_win_is_seeded_and_classified_http(self, probe_calls):
        pdp_data = {"http_method": "fingerprint_chrome_none"}
        result = probe_tools.run_listing_probe_advisory(
            "https://myhouse.com.au/collections/sale-clearance", pdp_data, job_id=0
        )
        # the PDP's fingerprint rung was tried FIRST (before any browser rung)
        assert probe_calls[0] == "fingerprint_chrome_none"
        assert result["method_that_worked"] == "fingerprint_chrome_none"
        # HTTP-flavoured win — NOT needs_browser
        assert result["http_ok"] is True
        assert result["browser_ok"] is False
        assert result["needs_browser"] is False

    def test_direct_http_seeding_still_works(self, probe_calls):
        """Regression pin: the original direct_http seeding is untouched."""
        pdp_data = {"http_method": "direct_http_datacenter"}
        result = probe_tools.run_listing_probe_advisory(
            "https://myhouse.com.au/collections/sale-clearance", pdp_data, job_id=0
        )
        # fingerprint-only success stub means the seeded direct_http rung fails,
        # but it MUST still appear first in the tried list
        assert probe_calls[0] == "direct_http_datacenter"
