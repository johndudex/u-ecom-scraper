"""[job-62 birkenstock] ``src/http_fetch.py`` — the shared fetch module.

The prod re-run of birkenstock failed because code_writer stripped the
draft's inline proxy ladder ("analysis says direct works") and the execution
run's 200-wrapped challenge pages had nothing to escalate to. The machinery
now lives here where the writer can't touch it, so its own contracts get
their own unit tests: tier resolution, hard-block escalation, the
``min_tier`` slice the discovery loop drives, the floor lock-in, and
headers/session wiring.

ProxyConfig, the residential-warning helpers, and ``requests.Session`` are
faked — no network, no config file, no singleton reads (the real
``should_warn_residential(config=None)`` path instantiates the real
ProxyConfig, which reads env + config/proxy.json).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.http_fetch as http_fetch
from src.http_fetch import create_fetch_page, resolve_tiers


class _FakeConfig:
    """Stands in for ProxyConfig (duck-typed to the real accessor methods).

    ``curl_cffi_tier`` defaults to False here so every LEGACY test pins the
    three-tier contract with the fingerprint tier explicitly off (the real
    default is on-if-importable — the tier's own tests opt in below). Without
    this, a container that ships curl_cffi would silently give every legacy
    test a live fourth network tier.
    """

    def __init__(self, escalation=("datacenter", "residential"),
                 ban_codes=(403, 503, 429)):
        self.config = {"strategy": {"ssl_verify": False, "curl_cffi_tier": False}}
        self._escalation = list(escalation)
        self._ban = set(ban_codes)

    def get_escalation_tier(self) -> list:
        return list(self._escalation)

    def get_proxy_dict(self, tier: str):
        return {"https": f"http://{tier}-proxy:22225"}

    def get_max_retries(self, tier: str) -> int:
        return 1  # one attempt per tier keeps call accounting exact

    def get_cooldown(self, tier: str) -> int:
        return 0

    def get_timeout(self) -> int:
        return 10

    def is_banned(self, status_code: int, text: str = "") -> bool:
        return status_code in self._ban


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http_fetch.requests.RequestException(str(self.status_code))


class _FakeSession:
    """Pops one scripted response per get(); repeats the last when exhausted."""

    def __init__(self, responses):
        self.headers: dict = {}
        self.calls: list[dict] = []
        self._responses = list(responses)
        self._last_response = _FakeResponse(500)

    def get(self, url, proxies=None, timeout=None, verify=None):
        self.calls.append({"url": url, "proxies": proxies})
        if self._responses:
            return self._responses.pop(0)
        return self._last_response


def _factory(monkeypatch, responses, config=None):
    """Wire http_fetch to fakes and return (fetch_page, session, config)."""
    cfg = config or _FakeConfig()
    session = _FakeSession(responses)
    monkeypatch.setattr(http_fetch, "ProxyConfig", type("P", (), {"get_instance": staticmethod(lambda *a, **k: cfg)}))
    monkeypatch.setattr(http_fetch, "should_warn_residential", lambda tier, config=None: False)
    monkeypatch.setattr(http_fetch, "warn_residential_usage", lambda url, config=None: None)
    monkeypatch.setattr(http_fetch.requests, "Session", lambda: session)
    fetch_page = create_fetch_page(delay_s=0, headers={"User-Agent": "test-agent/1.0"})
    return fetch_page, session, cfg


class TestResolveTiers:
    def test_full_ladder_from_zero(self):
        assert resolve_tiers(0, ["datacenter", "residential"]) == [
            "none", "datacenter", "residential",
        ]

    def test_min_tier_slices_off_the_unproxied_head(self):
        assert resolve_tiers(1, ["datacenter", "residential"]) == ["datacenter", "residential"]
        assert resolve_tiers(2, ["datacenter", "residential"]) == ["residential"]

    def test_beyond_the_ladder_is_empty(self):
        assert resolve_tiers(3, ["datacenter", "residential"]) == []

    def test_no_proxy_configured_ladder_is_none_only(self):
        assert resolve_tiers(0, []) == ["none"]
        assert resolve_tiers(1, []) == []

    def test_fingerprint_tier_appends_after_the_proxies(self):
        assert resolve_tiers(0, ["datacenter", "residential"], fingerprint_tier=True) == [
            "none", "datacenter", "residential", "fingerprint",
        ]
        # after an EMPTY proxy list the fingerprint rung is index 1
        assert resolve_tiers(1, [], fingerprint_tier=True) == ["fingerprint"]
        assert resolve_tiers(2, [], fingerprint_tier=True) == []
        assert resolve_tiers(3, ["datacenter", "residential"], fingerprint_tier=True) == [
            "fingerprint",
        ]
        assert resolve_tiers(99, ["datacenter"], fingerprint_tier=True) == []


class TestFetchPage:
    def test_closure_exposes_ladder_metadata(self, monkeypatch):
        fetch_page, _, _ = _factory(monkeypatch, [_FakeResponse(200)])
        assert fetch_page.min_tier_floor == 0
        assert fetch_page.tiers_total == 3  # none + datacenter + residential

    def test_headers_are_applied_to_the_session(self, monkeypatch):
        fetch_page, session, _ = _factory(monkeypatch, [_FakeResponse(200)])
        fetch_page("https://site.test/p/1")
        assert session.headers.get("User-Agent") == "test-agent/1.0"

    def test_hard_block_escalates_to_next_tier(self, monkeypatch):
        """403 (is_banned) at the none tier → datacenter serves the page."""
        fetch_page, session, _ = _factory(
            monkeypatch, [_FakeResponse(403), _FakeResponse(200, "<html/>")]
        )
        result = fetch_page("https://site.test/p/1")
        assert result is not None
        assert session.calls[0]["proxies"] is None          # none tier
        assert "datacenter" in session.calls[1]["proxies"]["https"]

    def test_min_tier_skips_the_unproxied_tier(self, monkeypatch):
        """The discovery loop's escalation entry point: min_tier=1 starts the
        ladder at datacenter — the burned direct IP is never touched again."""
        fetch_page, session, _ = _factory(monkeypatch, [_FakeResponse(200, "<html/>")])
        result = fetch_page("https://site.test/p/1", min_tier=1)
        assert result is not None
        assert "datacenter" in session.calls[0]["proxies"]["https"]
        assert len(session.calls) == 1

    def test_floor_overrides_an_explicit_lower_tier(self, monkeypatch):
        """Phase-2 calls fetch_page(url) with no min_tier — the floor raised
        during discovery must still keep it on the working tier."""
        fetch_page, session, _ = _factory(monkeypatch, [_FakeResponse(200, "<html/>")])
        fetch_page.min_tier_floor = 1
        fetch_page("https://site.test/p/1")
        assert "datacenter" in session.calls[0]["proxies"]["https"]

    def test_all_tiers_blocked_returns_none(self, monkeypatch):
        """The navigate_error contract upstream: None = every tier exhausted."""
        fetch_page, session, _ = _factory(
            monkeypatch, [_FakeResponse(403)] * 3
        )
        assert fetch_page("https://site.test/p/1") is None
        assert len(session.calls) == 3  # one attempt per tier (fake retries=1)

    def test_beyond_ladder_min_tier_returns_none(self, monkeypatch):
        fetch_page, session, _ = _factory(monkeypatch, [_FakeResponse(200)])
        assert fetch_page("https://site.test/p/1", min_tier=7) is None
        assert session.calls == []  # empty tier list → no request at all


class TestFingerprintTier:
    """[job-66 birkenstock] The curl_cffi browser-TLS tier above residential.

    Akamai scores the TLS handshake, not just the IP: every requests/urllib3
    connection presents the same client fingerprint through every proxy, which
    is why all three IP tiers saw 200-but-zero on birkenstock while a real
    browser rendered fine. The fingerprint tier re-issues the request through
    curl_cffi with an impersonated browser TLS stack, still on the residential
    exit. Import-guarded (absence → legacy 3-tier ladder) and config-gated
    ({"strategy": {"curl_cffi_tier": false}} kill switch).
    """

    def _factory_fp(self, monkeypatch, responses, curl_responses,
                    kill_switch=False, available=True):
        cfg = _FakeConfig()
        cfg.config["strategy"]["curl_cffi_tier"] = not kill_switch
        session = _FakeSession(responses)
        curl_session = _FakeSession(curl_responses)
        impersonate_kw: dict = {}

        def _curl_session_factory(**kwargs):
            impersonate_kw.update(kwargs)
            return curl_session

        monkeypatch.setattr(http_fetch, "ProxyConfig", type(
            "P", (), {"get_instance": staticmethod(lambda *a, **k: cfg)}
        ))
        monkeypatch.setattr(
            http_fetch, "should_warn_residential", lambda tier, config=None: False
        )
        monkeypatch.setattr(
            http_fetch, "warn_residential_usage", lambda url, config=None: None
        )
        monkeypatch.setattr(http_fetch.requests, "Session", lambda: session)
        monkeypatch.setattr(http_fetch, "_CURL_CFFI_AVAILABLE", available)
        monkeypatch.setattr(http_fetch, "curl_requests", type(
            "C", (), {"Session": staticmethod(_curl_session_factory)}
        ))
        fetch_page = create_fetch_page(delay_s=0, headers={"User-Agent": "ua/1"})
        return fetch_page, session, curl_session, cfg, impersonate_kw

    def test_tiers_total_extends_when_enabled(self, monkeypatch):
        fetch_page, *_ = self._factory_fp(monkeypatch, [], [])
        assert fetch_page.tiers_total == 4  # none + 2 proxies + fingerprint

    def test_impersonate_target_is_passed_to_the_session(self, monkeypatch):
        from src.http_fetch import CURL_IMPERSONATE

        _fetch_page, _, _, _, impersonate_kw = self._factory_fp(monkeypatch, [], [])
        assert impersonate_kw.get("impersonate") == CURL_IMPERSONATE

    def test_tls_blocked_ip_tiers_escalate_to_fingerprint(self, monkeypatch):
        """THE job-66 signature: every IP tier serves 200-but-zero (a soft
        block is invisible here, so the test uses hard 403s — same property
        under test: the fingerprint tier is the rung AFTER residential)."""
        fetch_page, session, curl_session, _, _ = self._factory_fp(
            monkeypatch, [_FakeResponse(403)] * 3, [_FakeResponse(200, "<html/>")],
        )
        result = fetch_page("https://site.test/c/shoes")
        assert result is not None
        assert len(session.calls) == 3  # none + datacenter + residential
        assert len(curl_session.calls) == 1  # then the fingerprint rung
        # Still riding the residential exit — the tier changes WHAT the client
        # looks like, the proxy changes WHERE it comes from.
        assert "residential" in curl_session.calls[0]["proxies"]["https"]

    def test_floor_locks_the_fingerprint_tier_for_phase2(self, monkeypatch):
        """The discovery loop's floor lock-in must land Phase-2 item fetches
        directly on the fingerprint rung — no re-walking the burned tiers."""
        fetch_page, session, curl_session, _, _ = self._factory_fp(
            monkeypatch, [], [_FakeResponse(200, "<html/>")],
        )
        fetch_page.min_tier_floor = 3
        result = fetch_page("https://site.test/p/1")
        assert result is not None
        assert session.calls == []
        assert len(curl_session.calls) == 1

    def test_kill_switch_disables_the_tier(self, monkeypatch):
        fetch_page, session, curl_session, _, _ = self._factory_fp(
            monkeypatch, [_FakeResponse(403)] * 3, [_FakeResponse(200, "<html/>")],
            kill_switch=True,
        )
        assert fetch_page.tiers_total == 3
        assert fetch_page("https://site.test/p/1") is None
        assert len(session.calls) == 3
        assert curl_session.calls == []  # never even constructed a request

    def test_missing_dependency_degrades_to_legacy_ladder(self, monkeypatch):
        fetch_page, session, curl_session, _, _ = self._factory_fp(
            monkeypatch, [_FakeResponse(403)] * 3, [_FakeResponse(200, "<html/>")],
            available=False,
        )
        assert fetch_page.tiers_total == 3
        assert fetch_page("https://site.test/p/1") is None
        assert curl_session.calls == []


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
