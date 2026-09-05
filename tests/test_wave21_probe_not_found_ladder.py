"""[wave-21 T5] A 404/410 terminates the probe ladder — proxies can't fix a
missing page.

Local e2e jobs 335/336 (michaelhill, marimekko): the seed PDP 404'd, and
the accessibility ladder (a) treated the styled 404 body as a SUCCESS
(fixed by T4 — tests/test_wave21_probe_404_gate.py) and, once T4 makes the
rung fail honestly, (b) would burn all remaining rungs — fingerprint
sub-ladder, playwright, cloak, datacenter, residential — against a page
that NO transport can ever return, then (c) cache the domain as
``captcha_detected=True`` (the all-failed tail) — poisoning every other
page on that domain.

Contract for ``run_probe_with_captcha_check``:
- first failed rung reporting status 404/410 → ladder STOPS (one rung),
  result carries ``not_found=True``, ``captcha_detected=False`` and the
  failing status;
- the all-failed ProbeCache captcha write is SKIPPED for not_found;
- a 403/anti-bot failure still escalates exactly as before.
"""
from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

# same-module-resolution dodge as the swap test: a plain from-import can be
# shadowed by package __init__ re-exports and trips isort's block rules here
probe_tools = importlib.import_module("agents.tools.probe_tools")  # noqa: E402

URL = "https://www.example.com/p/signet-ring-in-sterling-silver-18353730.html"


def _resp(payload: dict):
    r = SimpleNamespace(status_code=200)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


class _CacheRecorder:
    def __init__(self):
        self.calls = []

    def update_or_create(self, **kw):
        self.calls.append(kw)
        return None


@pytest.fixture()
def ladder_env(monkeypatch):
    """Unit-pure ladder: no cache reads, no LLM captcha check, no cache
    writes; httpx.post is faked per-test."""
    monkeypatch.setattr(probe_tools, "_get_cached_method", lambda domain: None)
    monkeypatch.setattr(probe_tools, "_save_probe_cache", lambda *a, **kw: None)
    monkeypatch.setattr(
        probe_tools,
        "_verify_captcha_free",
        lambda data: {"captcha_detected": False, "confidence": 1.0},
    )
    recorder = _CacheRecorder()
    import scraper.models as models

    monkeypatch.setattr(
        models.ProbeCache.objects, "update_or_create", recorder.update_or_create
    )
    return recorder


def _fake_post_sequence(monkeypatch, responses: dict):
    """responses: method-name → payload; any rung NOT listed escalates as a
    generic anti-bot 403 fail (the fingerprint sub-ladder sits between the
    base rungs, and the rung set evolves — assert on membership, not count)."""
    calls: list[str] = []

    def fake_post(url, json=None, timeout=None):
        method = (json or {}).get("method", "?")
        calls.append(method)
        payload = responses.get(method) or {
            "success": False,
            "status_code": 403,
            "blocked": True,
        }
        return _resp(payload)

    monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
    return calls


class TestNotFoundTerminatesLadder:
    def test_404_on_first_rung_stops_ladder(self, ladder_env, monkeypatch):
        calls = _fake_post_sequence(
            monkeypatch,
            {"direct_http": {"success": False, "status_code": 404, "blocked": False}},
        )
        result = probe_tools.run_probe_with_captcha_check(URL, job_id=0)
        assert calls == ["direct_http"], (
            f"ladder kept walking after a terminal 404: {calls}"
        )
        assert result["success"] is False
        assert result["not_found"] is True
        assert result["captcha_detected"] is False
        assert result["status_code"] == 404

    def test_410_also_terminates(self, ladder_env, monkeypatch):
        _fake_post_sequence(
            monkeypatch,
            {"direct_http": {"success": False, "status_code": 410, "blocked": False}},
        )
        result = probe_tools.run_probe_with_captcha_check(URL, job_id=0)
        assert result["not_found"] is True
        assert result["status_code"] == 410

    def test_not_found_skips_domain_captcha_cache(self, ladder_env, monkeypatch):
        """The all-failed tail must not record the DOMAIN as captcha'd just
        because one page doesn't exist."""
        _fake_post_sequence(
            monkeypatch,
            {"direct_http": {"success": False, "status_code": 404, "blocked": False}},
        )
        probe_tools.run_probe_with_captcha_check(URL, job_id=0)
        assert ladder_env.calls == [], (
            "not_found path wrote a captcha probe-cache entry for the domain"
        )

    def test_404_after_403_still_terminates(self, ladder_env, monkeypatch):
        """Anti-bot rungs escalate (403 → next rung) but a 404 further down
        still ends the walk."""
        calls = _fake_post_sequence(
            monkeypatch,
            {
                "direct_http": {"success": False, "status_code": 403, "blocked": True},
                "playwright_none": {
                    "success": False,
                    "status_code": 404,
                    "blocked": False,
                },
            },
        )
        result = probe_tools.run_probe_with_captcha_check(URL, job_id=0)
        assert calls[0] == "direct_http"
        assert "playwright_none" in calls  # 403 escalated as usual
        assert result["not_found"] is True
        assert result["status_code"] == 404


class TestAntiBotEscalationUnchanged:
    def test_403_still_escalates_to_next_rung(self, ladder_env, monkeypatch):
        calls = _fake_post_sequence(
            monkeypatch,
            {
                "direct_http": {"success": False, "status_code": 403, "blocked": True},
                "playwright_none": {
                    "success": True,
                    "status_code": 200,
                    "blocked": False,
                    "body_length": 9000,
                },
            },
        )
        result = probe_tools.run_probe_with_captcha_check(URL, job_id=0)
        # escalation walked the fingerprint sub-ladder rungs too, then the
        # browser rung won
        assert calls[0] == "direct_http"
        assert "playwright_none" in calls
        assert result["success"] is True
        assert result.get("not_found", False) is False

    def test_captcha_all_failed_path_unchanged(self, ladder_env, monkeypatch):
        """Non-404 all-failed keeps the legacy captcha-shaped verdict AND the
        domain cache write."""
        calls = _fake_post_sequence(
            monkeypatch,
            {
                "direct_http": {"success": False, "status_code": 403, "blocked": True},
                "playwright_none": {
                    "success": False,
                    "status_code": 403,
                    "blocked": True,
                },
                "cloak_none": {"success": False, "status_code": 403, "blocked": True},
                "direct_http_datacenter": {
                    "success": False,
                    "status_code": 403,
                    "blocked": True,
                },
                "playwright_datacenter": {
                    "success": False,
                    "status_code": 403,
                    "blocked": True,
                },
                "cloak_datacenter": {
                    "success": False,
                    "status_code": 403,
                    "blocked": True,
                },
                "direct_http_residential": {
                    "success": False,
                    "status_code": 403,
                    "blocked": True,
                },
                "playwright_residential": {
                    "success": False,
                    "status_code": 403,
                    "blocked": True,
                },
                "cloak_residential": {
                    "success": False,
                    "status_code": 403,
                    "blocked": True,
                },
            },
        )
        result = probe_tools.run_probe_with_captcha_check(URL, job_id=0)
        # full ladder walked — every base rung was reached, nothing terminal
        assert set(name for name, _ in probe_tools.ESCALATION_STEPS) <= set(calls)
        assert result["success"] is False
        assert result["captcha_detected"] is True
        assert result.get("not_found") is False
        assert len(ladder_env.calls) == 1  # legacy domain-cache write happens


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
