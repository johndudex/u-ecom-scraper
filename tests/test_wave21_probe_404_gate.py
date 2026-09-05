"""[wave-21 T4] A 404 page is not a successful probe.

Local e2e jobs 335/336 (michaelhill, marimekko — 2026-09-05): both seeds
were dead URLs, and both pipelines built scrapers off them because the
accessibility probe declared the sites reachable. All four fetch paths in
``browser_service/probe.py`` judge success purely on
``len(html) > 2000 and not blocked`` — and both retailers serve
fully-styled ~1 MB 404 pages, so the probe logged
``Status code: 404 … SUCCEEDED — real content``, cached the dead seed as a
WORKING method, and poisoned analysis, coverage and testing downstream
(335 died at the draft-freeze gate, 336 at testing-cascade exhaustion).

Contract: success requires an HTTP 2xx status in EVERY fetch path. A 404
body is rejected with ``success=False`` and — importantly for the T5
ladder verdict — ``blocked=False`` (a missing page is not a block; no
proxy or stealth browser fixes it). A 200 with the same body still
succeeds, and a 5xx error page is rejected too.

Run: docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.settings -e PYTHONPATH=/app:/app/webapp django sh -c "cd /app && pytest tests/test_wave21_probe_404_gate.py -q"
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")


def _load_probe():
    """Load probe.py as browser_service.probe without triggering the real
    package __init__ (which imports server.py → fastapi, absent from the
    django/celery test image — same loader as tests/test_browser_resilience)."""
    pkg_name = "browser_service"
    saved_pkg = sys.modules.get(pkg_name)
    saved_probe = sys.modules.pop("browser_service.probe", None)
    saved_cfg = sys.modules.pop("browser_service.config", None)

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules[pkg_name] = pkg
    try:
        spec = importlib.util.spec_from_file_location("browser_service.probe", PROBE_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["browser_service.probe"] = mod
        spec.loader.exec_module(mod)
    finally:
        if saved_pkg is not None:
            sys.modules[pkg_name] = saved_pkg
        else:
            sys.modules.pop(pkg_name, None)
        sys.modules.pop("browser_service.probe", None)
        if saved_probe is not None:
            sys.modules["browser_service.probe"] = saved_probe
        if saved_cfg is not None:
            sys.modules["browser_service.config"] = saved_cfg
    return mod


probe = _load_probe()

SEED_URL = "https://www.example.com/p/signet-ring-in-sterling-silver-18353730.html"

# A fully-styled "Page Not Found" body — big enough to clear the 2000-byte
# floor, styled enough that michaelhill/marimekko-class pages sail through
# every pre-T4 check.
BIG_NOT_FOUND_HTML = (
    "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
    "<title>Page Not Found | Example</title></head><body>"
    "<header><nav>Home Shop Sale About Contact " + "<span>nav </span>" * 80 + "</nav></header>"
    "<main><h1>Page Not Found</h1><p>The page you requested does not exist."
    " Browse our bestsellers below.</p></main>"
    "<footer>Terms Privacy Cookies " + "<em>foot </em>" * 80 + "</footer>"
    "</body></html>"
)


class _FakeHttpxResp:
    status_code = 404
    text = BIG_NOT_FOUND_HTML

    def __init__(self, status_code: int = 404):
        self.status_code = status_code
        self.text = BIG_NOT_FOUND_HTML


class _FakeHttpxClient:
    def __init__(self, status_code: int = 404, **_kw):
        self._status_code = status_code

    def get(self, url: str):
        return _FakeHttpxResp(self._status_code)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakePage:
    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 0):
        return SimpleNamespace(status=404)

    def wait_for_timeout(self, ms: int):
        return None

    def evaluate(self, script: str):
        return ""


class _FakeCtx:
    page = _FakePage()

    def close(self):
        return None


def _patch_browser_rungs(monkeypatch, status: int):
    """Fake the shared browser-launch seam used by _try_playwright/_try_cloak."""

    class _StatusPage(_FakePage):
        def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 0):
            return SimpleNamespace(status=status)

    ctx = SimpleNamespace(page=_StatusPage(), close=lambda: None)
    monkeypatch.setattr(probe, "_launch_page", lambda **kw: ctx)
    monkeypatch.setattr(probe, "_settle_past_challenge", lambda page: BIG_NOT_FOUND_HTML)
    monkeypatch.setattr(probe, "_safe_title", lambda page: "Page Not Found | Example")


class Test404IsNotProbeSuccess:
    def test_direct_http_404_big_page_is_not_success(self, monkeypatch):
        monkeypatch.setattr("httpx.Client", lambda **kw: _FakeHttpxClient(404))
        result = probe._try_direct_http(SEED_URL)
        assert result is not None
        assert result["success"] is False
        assert result["status_code"] == 404
        # A missing page is not a BLOCK — T5 keys the terminal ladder verdict
        # on this distinction (no proxy fixes a 404, but it isn't anti-bot).
        assert result["blocked"] is False

    def test_fingerprint_404_big_page_is_not_success(self, monkeypatch):
        @contextmanager
        def fake_session(profile, proxy_url, timeout):
            yield SimpleNamespace(get=lambda url, timeout=None: _FakeHttpxResp(404))

        monkeypatch.setattr(probe, "_fingerprint_session", fake_session)
        result = probe._try_fingerprint(SEED_URL, "none", profile="chrome")
        assert result is not None
        assert result["success"] is False
        assert result["status_code"] == 404
        assert result["blocked"] is False

    def test_playwright_404_big_page_is_not_success(self, monkeypatch):
        _patch_browser_rungs(monkeypatch, 404)
        result = probe._try_playwright(SEED_URL, "none")
        assert result is not None
        assert result["success"] is False
        assert result["status_code"] == 404
        assert result["blocked"] is False
        assert not result.get("needs_akamai_bypass")

    def test_cloak_404_big_page_is_not_success(self, monkeypatch):
        _patch_browser_rungs(monkeypatch, 404)
        result = probe._try_cloak(SEED_URL, "none")
        assert result is not None
        assert result["success"] is False
        assert result["status_code"] == 404
        assert result["blocked"] is False


class TestHealthyStatusesUnchanged:
    def test_direct_http_200_same_page_still_succeeds(self, monkeypatch):
        """Control: the rig itself must pass a live 200 page — a RED here
        means the fixtures are wrong, not that the gate is missing."""
        monkeypatch.setattr("httpx.Client", lambda **kw: _FakeHttpxClient(200))
        result = probe._try_direct_http(SEED_URL)
        assert result is not None
        assert result["success"] is True
        assert result["status_code"] == 200

    def test_playwright_500_error_page_rejected(self, monkeypatch):
        """5xx error pages poison a seed just as surely as 404s."""
        _patch_browser_rungs(monkeypatch, 500)
        result = probe._try_playwright(SEED_URL, "none")
        assert result is not None
        assert result["success"] is False
        assert result["status_code"] == 500


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
