"""SSRF validator for partner callback URLs (sync_api.yaml create + PATCH rotate).

Create-time half of critique B4: the callback URL is partner-controlled and
our servers will POST to it. Delivery-time re-validation (DNS rebinding) is
the 1b dispatcher's hard requirement; this gate is the first line.

Rules (spec sync_api.yaml SSRF block):
- https REQUIRED (http rejected — secrets ride the signature, not the body,
  but event payloads are business data)
- hostname must be a public IP after DNS resolution OR a public-domain name
  that RESOLVES to public IPs (all A/AAAA records checked)
- literal private/loopback/link-local/reserved/multicast IPs rejected,
  in BOTH v4-mapped and integer forms
- ports: 443 only (spec: HTTPS callbacks)

DNS resolution in tests is injectable (`resolver` param) — no network.
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

from scraper.api.ssrf import validate_callback_url  # noqa: E402


def resolve_none(host):
    return []


def resolve_public(host):
    return ["93.184.216.34"]


def resolve_private(host):
    return ["10.1.2.3"]


class TestSchemeAndShape:
    def test_https_public_ok(self):
        assert validate_callback_url("https://hooks.partner.example/cb", resolver=resolve_public) is None

    def test_http_rejected(self):
        err = validate_callback_url("http://hooks.partner.example/cb", resolver=resolve_public)
        assert err == "callback_url must use https"

    def test_not_a_url_rejected(self):
        assert validate_callback_url("garbage", resolver=resolve_public) is not None

    def test_explicit_port_443_ok(self):
        assert validate_callback_url("https://hooks.partner.example:443/cb", resolver=resolve_public) is None

    def test_nonstandard_port_rejected(self):
        err = validate_callback_url("https://hooks.partner.example:8443/cb", resolver=resolve_public)
        assert err is not None and "port" in err.lower()


class TestLiteralIPs:
    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/cb",
        "https://10.0.0.5/cb",
        "https://192.168.1.1/cb",
        "https://169.254.169.254/cb",   # cloud metadata
        "https://0.0.0.0/cb",
        "https://[::1]/cb",
        "https://[fe80::1]/cb",          # link-local v6
        "https://[fc00::1]/cb",          # unique-local v6
        "https://2130706433/cb",         # 127.0.0.1 as integer
        "https://0x7f000001/cb",         # hex form
    ])
    def test_private_literals_rejected(self, url):
        assert validate_callback_url(url, resolver=resolve_none) is not None

    def test_public_literal_ok(self):
        assert validate_callback_url("https://93.184.216.34/cb", resolver=resolve_none) is None


class TestDNSResolution:
    def test_domain_resolving_public_ok(self):
        assert validate_callback_url("https://hooks.partner.example/cb", resolver=resolve_public) is None

    def test_domain_resolving_private_rejected(self):
        # the rebinding case at create time: name looks fine, DNS says internal
        assert validate_callback_url("https://hooks.partner.example/cb", resolver=resolve_private) is not None

    def test_domain_mixed_records_rejected(self):
        # ANY A record private → reject (no partial trust)
        assert validate_callback_url(
            "https://hooks.partner.example/cb",
            resolver=lambda h: ["93.184.216.34", "192.168.0.1"],
        ) is not None

    def test_unresolvable_rejected(self):
        assert validate_callback_url("https://noexist.invalid/cb", resolver=resolve_none) is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
