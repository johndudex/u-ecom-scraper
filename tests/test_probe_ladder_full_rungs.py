"""The accessibility-gate ladder must contain every rung the service offers.

Prod 2026-09-02 (funko.com, job 248): T3.2 added the fingerprint_*
(curl_cffi TLS-impersonation) and cloak_* (stealth browser) rungs to
browser_service's /probe-single, and the T3.2 reconcile comment classified
them correctly — but ESCALATION_STEPS, the only list that decides which
rungs the job's check_accessibility gate actually SENDS, was never
extended. funko.com returned a real 200 page in 2.1s on
fingerprint_chrome_none while the gate declared "All 9 probe methods
returned captcha pages" and killed the job: the working rung was never
sent. The ladder now walks every family, HTTP-flavoured first, and the
deprecated uc_chrome_* aliases are gone (cloak_* is the honest name).

Run from repo root:
    PYTHONPATH=/app:/app/webapp python -m pytest tests/test_probe_ladder_full_rungs.py -v
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

from agents.tools.probe_tools import (  # noqa: E402
    _HTTP_METHOD_PREFIXES,
    BROWSER_METHODS,
    ESCALATION_STEPS,
)

_NAMES = [name for name, _tier in ESCALATION_STEPS]


def _resp(payload: dict, status: int = 200):
    from types import SimpleNamespace

    r = SimpleNamespace(status_code=status)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


class TestLadderContents:
    def test_every_server_rung_family_is_present(self):
        for tier in ("none", "datacenter", "residential"):
            assert ("direct_http" if tier == "none" else f"direct_http_{tier}") in _NAMES
            assert f"fingerprint_chrome_{tier}" in _NAMES, (
                "the curl_cffi chrome rung is missing — the exact rung that "
                "beat funko in prod while the gate said every method was blocked"
            )
            assert f"fingerprint_safari184_{tier}" in _NAMES
            assert f"playwright_{tier}" in _NAMES
            assert f"cloak_{tier}" in _NAMES

    def test_deprecated_uc_chrome_aliases_are_gone(self):
        assert not any(n.startswith("uc_chrome") for n in _NAMES), (
            "uc_chrome_* are deprecated server-side aliases for cloak_* — "
            "send the honest name"
        )

    def test_no_duplicates(self):
        assert len(_NAMES) == len(set(_NAMES))

    def test_tier_field_matches_name_suffix(self):
        for name, tier in ESCALATION_STEPS:
            expected = "none" if tier == "none" else tier
            if name == "direct_http":
                assert tier == "none"
            else:
                assert name.endswith(f"_{expected}"), name

    def test_http_rungs_precede_browser_rungs(self):
        def is_http(name):
            return any(name.startswith(p) for p in _HTTP_METHOD_PREFIXES)

        seen_browser = False
        for name in _NAMES:
            if not is_http(name):
                seen_browser = True
            assert not (seen_browser and is_http(name)), (
                "a cheap HTTP rung must never be retried after browser "
                "launches have started (30-90s a launch)"
            )

    def test_every_rung_classifies(self):
        """Each rung must be classifiable — the http/browser split drives the
        phase-2 'find a second method of the OTHER type' pass."""
        for name in _NAMES:
            is_http = any(name.startswith(p) for p in _HTTP_METHOD_PREFIXES)
            assert is_http or name in BROWSER_METHODS, name


class TestLadderWiring:
    @pytest.mark.django_db
    def test_fingerprint_only_site_passes_the_gate(self, monkeypatch):
        """THE funko shape: every legacy rung blocked, fingerprint_chrome_none
        returns a real page → the gate must SUCCEED, not declare captcha."""
        from agents.tools import probe_tools

        sent = []

        def fake_post(url, json=None, timeout=None):
            sent.append((json or {}).get("method", ""))
            if (json or {}).get("method") == "fingerprint_chrome_none":
                return _resp({"success": True, "method": "fingerprint_chrome_none",
                              "status_code": 200, "title": "Pop! Shinobu Kocho",
                              "body_length": 244825, "jsonld": [], "meta": {},
                              "selector_results": {}})
            return _resp({"success": False, "status_code": 403})

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        monkeypatch.setattr(probe_tools, "_verify_captcha_free",
                            lambda data: {"captcha_detected": False})

        result = probe_tools.run_probe_with_captcha_check(
            "https://funko.com/p/1", job_id=0
        )

        assert result.get("success") is True, (
            "a fingerprint-passable site must pass the accessibility gate"
        )
        assert result.get("method") == "fingerprint_chrome_none"
        assert "fingerprint_chrome_none" in sent
        assert not result.get("captcha_detected")

    @pytest.mark.django_db
    def test_cloak_rung_reached_after_http_and_playwright_fail(self, monkeypatch):
        """The stealth rung must be SENT by the ladder (job 246's cloak win at
        15:12 proves the value; the aliases' removal must not drop it)."""
        from agents.tools import probe_tools

        def fake_post(url, json=None, timeout=None):
            method = (json or {}).get("method", "")
            if method == "cloak_none":
                return _resp({"success": True, "method": "cloak_none",
                              "status_code": 200, "title": "Real page",
                              "body_length": 5000, "jsonld": [], "meta": {},
                              "selector_results": {}})
            return _resp({"success": False, "status_code": 403})

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        monkeypatch.setattr(probe_tools, "_verify_captcha_free",
                            lambda data: {"captcha_detected": False})

        result = probe_tools.run_probe_with_captcha_check(
            "https://example.com/p/3", job_id=0
        )
        assert result.get("success") is True
        assert result.get("method") == "cloak_none"

    @pytest.mark.django_db
    def test_all_blocked_still_declared_captcha_with_full_census(self, monkeypatch):
        """When genuinely everything fails, the verdict still lists every rung
        that was sent — the census the operator diagnoses from."""
        from agents.tools import probe_tools

        monkeypatch.setattr(
            probe_tools.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(probe_tools, "_verify_captcha_free",
                            lambda data: {"captcha_detected": False})

        result = probe_tools.run_probe_with_captcha_check(
            "https://example.com/p/4", job_id=0
        )
        assert result.get("captcha_detected") is True
        assert set(result.get("methods_tried", [])) == set(_NAMES), (
            "every ladder rung must be attempted before the captcha verdict"
        )
