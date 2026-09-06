"""[wave-22 C2] A field-verdict "empty" is only evidence when the render that
produced it was FULL-FIDELITY — the rung requested is the rung answered, and
the HTML was not truncated.

Prod shape: ``verify_field_mappings`` asks /render for the probe's winning
rung, but never checks that /render ANSWERED with that rung — a render that
escalated (requested rung failed, ladder moved on) returns a page from a
DIFFERENT access path, and every mapping reads "empty" off a blocked page.
And ``probe.py render_page`` sliced oversized HTML silently, so the caller
could not tell a real empty from a cut-off one.

Contract (fail-closed per the plan):
- ``render_page`` emits ``html_truncated`` (both the capture path and the
  re-fetch path);
- the T3.13e downgrade (presence credit stripped on ``tested=="empty"``)
  fires ONLY with positive full-fidelity proof: ``html_truncated is False``
  AND the returned rung matches the requested one;
- truncated or escalated ⇒ the empty verdict is recorded as "skipped"
  (presence credit kept) and the field is stamped ``render_provenance:
  "degraded"``;
- metadata ABSENT (an older browser_service that predates ``html_truncated``,
  the deploy-order window) ⇒ keep credit, stamp ``render_provenance:
  "unknown"`` — absence must not manufacture proof.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from src import field_verification as fv  # noqa: E402


PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")


def _load_probe():
    """Load probe.py as browser_service.probe without the real package
    __init__ (which imports server.py → fastapi). Mirrors
    test_browser_resilience._load_probe."""
    pkg_name = "browser_service"
    saved_pkg = sys.modules.get(pkg_name)
    saved_probe = sys.modules.pop("browser_service.probe", None)
    saved_cfg = sys.modules.pop("browser_service.config", None)

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules[pkg_name] = pkg
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_service.probe", PROBE_PATH
        )
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


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TestRenderPageEmitsTruncation:
    def test_oversized_capture_is_flagged_truncated(self):
        probe = _load_probe()
        probe._proxy_tier_configured = lambda tier: True

        def _step(name, url, timeout, country=None):
            # the real capture path: probe helpers hand HTML to the side-channel
            probe._capture_html_for_render("<html>" + "x" * 999_999)
            return {"success": True, "method": name, "status_code": 200, "title": "t"}

        probe._dispatch_step = _step
        out = probe.render_page("https://example.com/p/1", timeout=5)
        assert out["success"] is True
        assert out["html_truncated"] is True
        assert len(out["html"]) <= probe.MAX_RENDER_HTML

    def test_oversized_refetch_is_flagged_truncated(self):
        probe = _load_probe()
        probe._proxy_tier_configured = lambda tier: True
        probe._dispatch_step = lambda name, url, timeout, country=None: {
            "success": True,
            "method": name,
            "status_code": 200,
            "title": "t",
        }
        probe._refetch_html = (
            lambda url, step, tier, timeout, country: "<html>" + "y" * 999_999
        )
        out = probe.render_page("https://example.com/p/1", timeout=5)
        assert out["html_truncated"] is True
        assert len(out["html"]) <= probe.MAX_RENDER_HTML

    def test_small_page_is_not_truncated(self):
        probe = _load_probe()
        probe._proxy_tier_configured = lambda tier: True

        def _step(name, url, timeout, country=None):
            probe._capture_html_for_render("<html>tiny</html>")
            return {"success": True, "method": name, "status_code": 200, "title": "t"}

        probe._dispatch_step = _step
        out = probe.render_page("https://example.com/p/1", timeout=5)
        assert out["html_truncated"] is False


def _analysis():
    return {
        "fields": {
            "price": {"method": "css", "selector": ".price"},
            "title": {"method": "css", "selector": "h1"},
        }
    }


def _state(start="direct_http"):
    return {
        "sample_url": "https://example.com/p/1",
        "probe_result": {"connectivity": {"method_that_worked": start}},
    }


def _patch_fetch(monkeypatch, html, method_used, truncated):
    calls = {}

    def _fake(url, start_method):
        calls["start_method"] = start_method
        return html, method_used, truncated

    monkeypatch.setattr(fv, "_fetch_render", _fake)
    return calls


class TestVerifyProvenanceGate:
    def test_full_fidelity_render_keeps_empty_verdict(self, monkeypatch):
        _patch_fetch(monkeypatch, "<html/>", "direct_http", False)
        analysis, summary = fv.verify_field_mappings(
            "acme", _state(), _analysis()
        )
        # .price matches nothing on the stub HTML → honest empty stands
        assert analysis["fields"]["price"]["tested"] == "empty"
        assert analysis["fields"]["price"]["render_provenance"] == "full"

    def test_truncated_render_blocks_the_downgrade(self, monkeypatch):
        _patch_fetch(monkeypatch, "<html/>", "direct_http", True)
        analysis, summary = fv.verify_field_mappings(
            "acme", _state(), _analysis()
        )
        assert analysis["fields"]["price"]["tested"] == "skipped", (
            "a cut-off page cannot prove a mapping dead — presence credit "
            "must survive"
        )
        assert analysis["fields"]["price"]["render_provenance"] == "degraded"

    def test_escalated_render_blocks_the_downgrade(self, monkeypatch):
        # requested direct_http, render answered cloak_none → different
        # access path, the empty verdicts describe the WRONG page
        _patch_fetch(monkeypatch, "<html/>", "cloak_none", False)
        analysis, _ = fv.verify_field_mappings("acme", _state(), _analysis())
        assert analysis["fields"]["price"]["tested"] == "skipped"
        assert analysis["fields"]["price"]["render_provenance"] == "degraded"

    def test_missing_metadata_keeps_credit_records_unknown(self, monkeypatch):
        # older browser_service (deploy-order window): no html_truncated key
        _patch_fetch(monkeypatch, "<html/>", "direct_http", None)
        analysis, summary = fv.verify_field_mappings(
            "acme", _state(), _analysis()
        )
        assert analysis["fields"]["price"]["tested"] == "skipped"
        assert analysis["fields"]["price"]["render_provenance"] == "unknown"
        assert summary.get("provenance") == "unknown"

    def test_no_requested_rung_and_clean_render_is_full(self, monkeypatch):
        _patch_fetch(monkeypatch, "<html/>", "direct_http", False)
        analysis, summary = fv.verify_field_mappings(
            "acme", _state(start=""), _analysis()
        )
        assert summary["provenance"] == "full"

    def test_verified_fields_keep_their_verdict_when_degraded(self, monkeypatch):
        # a mapping that RESOLVES on the degraded render still earns credit —
        # proven dead is gated, proven alive is kept
        _patch_fetch(
            monkeypatch, '<html><h1>Widget</h1></html>', "direct_http", True
        )
        analysis, _ = fv.verify_field_mappings("acme", _state(), _analysis())
        assert analysis["fields"]["title"]["tested"] == "verified"


class TestFetchRenderPassthrough:
    def test_fetch_render_returns_truncation_flag(self, monkeypatch):
        seen = {}

        def _fake_post(url, json=None, timeout=None):
            seen["payload"] = json
            return _FakeResp(
                {
                    "success": True,
                    "html": "<html/>",
                    "method": "cloak_none",
                    "html_truncated": True,
                }
            )

        import httpx

        monkeypatch.setattr(httpx, "post", _fake_post)
        html, method, truncated = fv._fetch_render(
            "https://example.com/p/1", "direct_http"
        )
        assert html == "<html/>"
        assert method == "cloak_none"
        assert truncated is True
        assert seen["payload"]["start_method"] == "direct_http"

    def test_fetch_render_without_flag_is_unknown(self, monkeypatch):
        def _fake_post(url, json=None, timeout=None):
            return _FakeResp({"success": True, "html": "<html/>", "method": "m"})

        import httpx

        monkeypatch.setattr(httpx, "post", _fake_post)
        _, _, truncated = fv._fetch_render("https://example.com/p/1", "")
        assert truncated is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
