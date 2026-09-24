"""Partner API wave-41: POST /api/v1/discover-fields.

Contract under test (sync_api.yaml /api/v1/discover-fields):
- X-API-Key pipeline (401 missing key) via the real api_view decorator
- 400 non-JSON body; 422 invalid_url (missing / non-absolute)
- 422 homepage_url — discovery needs a sample item page, not a homepage
- 422 blocked_host — private literal IPs, private DNS records, unresolvable
  hosts (the render executes inside the platform network)
- 200 shape: {url, fields, json_schema, source, content_type} — honest
  `fields: []` when nothing extractable (probe wiring tests at bottom)

Run: docker compose exec -T -w /app django sh -c \
     "PYTHONPATH=/app:/app/webapp pytest tests/test_api_discover_fields.py -v"
"""
from __future__ import annotations

import httpx
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from scraper import models  # noqa: E402
from scraper.api import ssrf  # noqa: E402
from scraper.api.discovery import discover_fields  # noqa: E402

rf = RequestFactory()
PATH = "/api/v1/discover-fields"


# ── fixtures (same shape as test_partner_api.py) ────────────────────────────

@pytest.fixture
def partner_user(db):
    return User.objects.create_user(username="_t_df_user", password="x")


@pytest.fixture
def partner_key(partner_user):
    raw = "pk_df_" + os.urandom(16).hex()
    return models.ApiKey.objects.create(
        user=partner_user, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
    ), raw


@pytest.fixture
def api_request(partner_key):
    key_obj, raw = partner_key

    def make(body=None, **kw):
        headers = kw.pop("headers", {})
        return rf.post(
            PATH, data=body, content_type="application/json",
            HTTP_X_API_KEY=raw,
            **{k: v for k, v in kw.items() if k.startswith("HTTP")},
            **headers,
        )

    return make


def _body(resp):
    return json.loads(resp.content)


# ── auth + validation skeleton (T2) ─────────────────────────────────────────

class TestAuthAndValidation:
    def test_missing_key_401(self, db):
        resp = discover_fields(
            rf.post(PATH, data=json.dumps({"url": "https://x.example/p/1"}),
                    content_type="application/json")
        )
        assert resp.status_code == 401
        assert _body(resp)["code"] == "unauthorized"

    def test_non_json_body_400(self, db, api_request):
        resp = discover_fields(api_request(body="not json{"))
        assert resp.status_code == 400
        assert _body(resp)["code"] == "validation_failed"

    def test_missing_url_422(self, db, api_request):
        resp = discover_fields(api_request(body=json.dumps({})))
        assert resp.status_code == 422
        assert _body(resp)["code"] == "invalid_url"

    def test_non_absolute_url_422(self, db, api_request):
        resp = discover_fields(api_request(body=json.dumps({"url": "ftp://x.example/p/1"})))
        assert resp.status_code == 422
        assert _body(resp)["code"] == "invalid_url"

    def test_homepage_url_422(self, db, api_request):
        resp = discover_fields(api_request(body=json.dumps({"url": "https://www.example.com/"})))
        assert resp.status_code == 422
        assert _body(resp)["code"] == "homepage_url"

    def test_private_literal_ip_422(self, db, api_request):
        resp = discover_fields(api_request(body=json.dumps({"url": "http://127.0.0.1/p/1"})))
        assert resp.status_code == 422
        assert _body(resp)["code"] == "blocked_host"

    def test_private_dns_record_422(self, db, api_request, monkeypatch):
        monkeypatch.setattr(ssrf, "_resolve", lambda host: ["10.1.2.3"])
        resp = discover_fields(api_request(body=json.dumps({"url": "https://shop.example/p/1"})))
        assert resp.status_code == 422
        assert _body(resp)["code"] == "blocked_host"

    def test_unresolvable_host_422(self, db, api_request, monkeypatch):
        monkeypatch.setattr(ssrf, "_resolve", lambda host: [])
        resp = discover_fields(api_request(body=json.dumps({"url": "https://nope.example/p/1"})))
        assert resp.status_code == 422
        assert _body(resp)["code"] == "blocked_host"


# ── probe wiring (T3) — browser render + discovery, mocked at the seams ─────

GOOD_URL = "https://shop.example/p/merino-123"


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(ssrf, "_resolve", lambda host: ["93.184.216.34"])


def _nav(payload, status=200):
    return httpx.Response(status_code=status, json=payload)


class TestProbeWiring:
    @pytest.fixture(autouse=True)
    def _setup(self, db, api_request, public_dns):
        self.api_request = api_request

    def _mock_nav(self, monkeypatch, response=None, exc=None):
        calls = {}

        def fake_post(url, json=None, timeout=None):
            calls["url"] = url
            calls["payload"] = json
            calls["timeout"] = timeout
            if exc is not None:
                raise exc
            return response

        monkeypatch.setattr(httpx, "post", fake_post)
        return calls

    def _mock_discover(self, monkeypatch, result=None, exc=None):
        calls = {}

        def fake(url, html, title="", llm_timeout=20):
            calls["url"] = url
            calls["html_len"] = len(html)
            calls["llm_timeout"] = llm_timeout
            if exc is not None:
                raise exc
            return result or {"fields": [], "json_schema": None, "source": "none",
                              "content_type": ""}

        monkeypatch.setattr("src.field_discovery.discover_fields_from_html", fake)
        return calls

    def test_llm_success_shape(self, monkeypatch):
        self._mock_nav(monkeypatch, _nav({"html": "x" * 600, "title": "Merino Polo"}))
        self._mock_discover(monkeypatch, {
            "fields": ["title", "price"],
            "json_schema": {"type": "object", "properties": {}},
            "source": "llm", "content_type": "product",
        })
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 200
        body = _body(resp)
        assert body == {
            "url": GOOD_URL,
            "fields": ["title", "price"],
            "json_schema": {"type": "object", "properties": {}},
            "source": "llm",
            "content_type": "product",
        }

    def test_navigate_payload_is_cloak_direct(self, monkeypatch):
        calls = self._mock_nav(monkeypatch, _nav({"html": "x" * 600}))
        self._mock_discover(monkeypatch, {})
        discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert calls["payload"]["url"] == GOOD_URL
        assert calls["payload"]["stealth"] == "cloak"
        assert calls["payload"]["return_what"] == "all"
        assert calls["payload"]["wait_until"] == "domcontentloaded"
        assert calls["payload"]["timeout"] == 25
        assert calls["timeout"] == 30  # navigate budget + 5s headroom

    def test_jsonld_fallback_passthrough(self, monkeypatch):
        self._mock_nav(monkeypatch, _nav({"html": "x" * 600}))
        self._mock_discover(monkeypatch, {"fields": ["title"], "json_schema": None,
                                          "source": "jsonld", "content_type": "product"})
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 200
        assert _body(resp)["source"] == "jsonld"

    def test_honest_zero_is_200(self, monkeypatch):
        self._mock_nav(monkeypatch, _nav({"html": "x" * 600}))
        self._mock_discover(monkeypatch, {"fields": [], "json_schema": None,
                                          "source": "none", "content_type": ""})
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 200
        body = _body(resp)
        assert body["fields"] == [] and body["source"] == "none"

    def test_short_html_never_calls_discovery(self, monkeypatch):
        self._mock_nav(monkeypatch, _nav({"html": "tiny"}))
        calls = self._mock_discover(monkeypatch)  # would fail loudly if called
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 200
        assert _body(resp)["source"] == "none"
        assert calls == {}

    def test_site_blocked_502(self, monkeypatch):
        self._mock_nav(monkeypatch, _nav({"blocked": True, "blocked_type": "cloudflare"}))
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 502
        assert _body(resp)["code"] == "site_blocked"
        assert "cloudflare" in _body(resp)["message"]

    def test_browser_unreachable_503(self, monkeypatch):
        self._mock_nav(monkeypatch, exc=httpx.ConnectError("no route"))
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 503
        assert _body(resp)["code"] == "discovery_unavailable"

    def test_navigate_read_timeout_504(self, monkeypatch):
        self._mock_nav(monkeypatch, exc=httpx.ReadTimeout("slow page"))
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 504
        assert _body(resp)["code"] == "discovery_timeout"

    def test_browser_http_error_503(self, monkeypatch):
        self._mock_nav(monkeypatch, _nav({}, status=503))
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 503
        assert _body(resp)["code"] == "discovery_unavailable"

    def test_discovery_crash_is_honest_zero_not_500(self, monkeypatch):
        self._mock_nav(monkeypatch, _nav({"html": "x" * 600}))
        self._mock_discover(monkeypatch, exc=RuntimeError("llm exploded"))
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 200
        body = _body(resp)
        assert body["fields"] == [] and body["source"] == "none"


# ── dedicated budget (T4) — 6 req/min per key, 1 concurrent render per key ──

class TestDiscoveryBudget(TestProbeWiring):
    @pytest.fixture(autouse=True)
    def _setup(self, db, api_request, partner_key, public_dns, monkeypatch):
        self.api_request = api_request
        self.key16 = partner_key[0].key_hash[:16]
        self.monkeypatch = monkeypatch
        from scraper.services import _get_redis
        conn = _get_redis()
        for k in conn.scan_iter(f"rl:discover:{self.key16}:*"):
            conn.delete(k)
        yield
        for k in conn.scan_iter(f"rl:discover:{self.key16}:*"):
            conn.delete(k)

    def _seed_window(self, count):
        from scraper.services import _get_redis
        import time as _t
        conn = _get_redis()
        minute_key = f"rl:discover:{self.key16}:m:{int(_t.time() // 60)}"
        conn.set(minute_key, count, ex=70)

    def _seed_lock(self):
        from scraper.services import _get_redis
        _get_redis().set(f"rl:discover:{self.key16}:lock", "1", ex=45)

    def _lock_exists(self):
        from scraper.services import _get_redis
        return _get_redis().exists(f"rl:discover:{self.key16}:lock") == 1

    def _happy_nav(self):
        self._mock_nav(self.monkeypatch, _nav({"html": "x" * 600}))
        self._mock_discover(self.monkeypatch, {"fields": ["title"],
                                               "json_schema": None, "source": "llm",
                                               "content_type": "product"})

    def test_over_window_429_with_honest_retry_after(self):
        self._seed_window(6)
        self._happy_nav()
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 429
        body = _body(resp)
        assert body["code"] == "rate_limited"
        assert "6 req/min" in body["details"]["limit"]
        # header must match details.retry_after — partners that trust the
        # header must not hammer (the old hard-coded "1" lied)
        assert resp["Retry-After"] == str(body["details"]["retry_after"])

    def test_held_lock_429_even_under_window(self):
        self._seed_lock()
        self._happy_nav()
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 429
        assert _body(resp)["code"] == "rate_limited"

    def test_success_releases_lock(self):
        self._happy_nav()
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 200
        assert not self._lock_exists()

    def test_failure_releases_lock(self):
        self._mock_nav(self.monkeypatch, exc=httpx.ConnectError("no route"))
        resp = discover_fields(self.api_request(body=json.dumps({"url": GOOD_URL})))
        assert resp.status_code == 503
        assert not self._lock_exists()

    def test_validation_failures_never_take_the_lock(self):
        resp = discover_fields(self.api_request(body=json.dumps({"url": "https://www.example.com/"})))
        assert resp.status_code == 422
        assert not self._lock_exists()
