"""Wave-33 Tier C: caller 429-tolerance (C1a, T33-6) — tolerance BEFORE gate.

Ordering constraint from docs/plans/wave33-browser-oom-plan.md: a memory/
capacity gate that answers 429 must never cause MORE launches. Today every
probe caller treats a 429 as a failed rung — the ladder escalates straight
into the saturated service and multiplies browser launches. Locked here: a
429 from browser-service yields a distinct **throttled** verdict that parks
(no rung escalation), says nothing about the site, writes no domain cache,
and carries the Retry-After; /render-adjacent paths return the busy token.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave33_tier_c.py -q
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

import httpx  # noqa: E402
import pytest  # noqa: E402

PROBE_URL = "http://browser_service:8001/probe-single"


def _resp(status: int, payload: dict | None = None, retry_after: str | None = None):
    """A REAL httpx.Response so raise_for_status() raises the real error."""
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(
        status,
        headers=headers,
        json=payload if payload is not None else {},
        request=httpx.Request("POST", PROBE_URL),
    )


# ── the shared classifier ───────────────────────────────────────────────────


class TestThrottleClassifier:
    def test_429_with_header(self):
        from agents.tools.browser_http import throttle_retry_after

        try:
            _resp(429, retry_after="12").raise_for_status()
        except httpx.HTTPStatusError as e:
            exc = e
        assert throttle_retry_after(exc) == 12.0

    def test_429_default_when_no_header(self):
        from agents.tools.browser_http import throttle_retry_after

        try:
            _resp(429, {"error": "busy"}).raise_for_status()
        except httpx.HTTPStatusError as e:
            exc = e
        assert throttle_retry_after(exc) == 5.0  # DEFAULT_RETRY_AFTER_S

    def test_non_429_is_none(self):
        from agents.tools.browser_http import throttle_retry_after

        try:
            _resp(503, {"error": "chrome_crash"}).raise_for_status()
        except httpx.HTTPStatusError as e:
            exc = e
        assert throttle_retry_after(exc) is None

    def test_transport_error_is_none(self):
        from agents.tools.browser_http import throttle_retry_after

        assert throttle_retry_after(httpx.ConnectError("refused")) is None


# ── the accessibility ladder parks ──────────────────────────────────────────


@pytest.mark.django_db
class TestAccessibilityLadderParks:
    def test_429_parks_the_ladder(self, monkeypatch):
        """The C1 ordering constraint, behavioural: one 429 → exactly ONE
        rung sent, throttled verdict, no escalation into the busy service."""
        from agents.tools import probe_tools

        sent = []

        def fake_post(url, json=None, timeout=None):
            sent.append((json or {}).get("method", ""))
            return _resp(429, {"error": "memory gate"}, retry_after="12")

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda d: {"captcha_detected": False}
        )

        result = probe_tools.run_probe_with_captcha_check(
            "https://throttle.test/p/1", job_id=0
        )

        assert len(sent) == 1, f"ladder must park after a 429, sent {sent}"
        assert result.get("throttled") is True
        assert result.get("retry_after_s") == 12.0
        assert "busy" in (result.get("error") or "").lower()
        assert result.get("success") is False
        assert result.get("captcha_detected") is False, (
            "backpressure must never read as a captcha verdict"
        )
        assert result.get("akamai_detected") is False

    def test_phase1_win_survives_a_later_429(self, monkeypatch):
        """A method that already worked is returned — the park must not bury
        a real result that arrived before the service got busy."""
        from agents.tools import probe_tools

        def fake_post(url, json=None, timeout=None):
            if (json or {}).get("method") == "direct_http":
                return _resp(
                    200,
                    {
                        "success": True,
                        "method": "direct_http",
                        "status_code": 200,
                        "title": "t",
                        "body_length": 5000,
                        "jsonld": [],
                        "meta": {},
                        "selector_results": {},
                    },
                )
            return _resp(429, {}, retry_after="5")

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda d: {"captcha_detected": False}
        )

        result = probe_tools.run_probe_with_captcha_check(
            "https://throttle.test/p/2", job_id=0
        )
        assert result.get("success") is True
        assert result.get("method") == "direct_http"

    def test_throttled_verdict_writes_no_domain_cache(self, monkeypatch):
        """Busy says nothing about the domain — the negative cache writes in
        the all-failed tail (captcha'd / needs_akamai_bypass) must not run."""
        from agents.tools import probe_tools
        from scraper.models import ProbeCache

        ProbeCache.objects.filter(domain="nocache.test").delete()

        def fake_post(url, json=None, timeout=None):
            return _resp(429, {}, retry_after="5")

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        monkeypatch.setattr(
            probe_tools, "_verify_captcha_free", lambda d: {"captcha_detected": False}
        )
        probe_tools.run_probe_with_captcha_check(
            "https://nocache.test/p/3", job_id=0
        )
        row = ProbeCache.objects.filter(domain="nocache.test").first()
        assert row is None, "a 429 park must not write any domain cache"


# ── the listing advisory parks ──────────────────────────────────────────────


@pytest.mark.django_db
class TestListingAdvisoryParks:
    def test_429_parks_rungs_and_flags_throttled(self, monkeypatch):
        from agents.tools import probe_tools
        import src.geo as geo

        sent = []

        def fake_post(url, json=None, timeout=None):
            sent.append((json or {}).get("method", ""))
            return _resp(429, {}, retry_after="9")

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        monkeypatch.setattr(geo, "detect_country", lambda u: "US")

        out = probe_tools.run_listing_probe_advisory(
            "https://throttle.test/c/shoes",
            {"http_method": "direct_http"},
            job_id=0,
        )
        assert len(sent) == 1, "advisory must not walk the remaining rungs"
        assert out.get("throttled") is True
        assert out.get("retry_after_s") == 9.0
        assert out.get("method_that_worked") == ""


# ── the probe_page tool + /render-adjacent busy tokens ─────────────────────


@pytest.mark.django_db
class TestBusyTokens:
    def test_probe_page_tool_reports_throttled(self, monkeypatch):
        from agents.tools import probe_tools

        tool = probe_tools.get_probe_tools()[0]

        def fake_post(url, json=None, timeout=None):
            return _resp(429, {}, retry_after="7")

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        out = tool.func("https://throttle.test/p/4")
        assert "throttled" in out.lower()
        assert "429" in out
        assert "retry after" in out.lower()

    def test_probe_html_tool_busy_token(self, monkeypatch):
        from agents.tools import probe_tools

        tool = probe_tools.get_probe_html_tool()[0]

        def fake_post(url, json=None, timeout=None):
            return _resp(429, {}, retry_after="7")

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        out = tool.func("https://throttle.test/p/5")
        assert "RENDER THROTTLED" in out
        assert "busy" in out.lower()

    def test_navigate_explore_render_busy_token(self, monkeypatch):
        import httpx as _httpx

        from agents.nodes import navigate_explore

        def fake_post(url, json=None, timeout=None):
            return _resp(429, {}, retry_after="8")

        # _fetch_via_probe_html does `import httpx` inside the fn — patch the
        # shared module object.
        monkeypatch.setattr(_httpx, "post", fake_post)
        out = navigate_explore._fetch_via_probe_html("https://throttle.test/")
        assert "RENDER THROTTLED" in out, out[:120]
        assert "busy" in out.lower()

    def test_probe_tester_view_answers_429(self, monkeypatch):
        from django.test import RequestFactory

        from scraper import views

        def fake_post(url, json=None, timeout=None):
            return _resp(429, {}, retry_after="7")

        monkeypatch.setattr(views.httpx, "post", fake_post)
        rf = RequestFactory()
        req = rf.post(
            "/probe-tester/",
            {"url": "https://x.test/", "method": "direct_http"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",  # the view's AJAX gate
        )
        resp = views.probe_tester.__wrapped__(req)  # bypass login_required
        assert resp.status_code == 429
        import json as _json

        body = _json.loads(resp.content)
        assert body["throttled"] is True
        assert "busy" in body["error"].lower()


# ── C1b: the shared capacity admission gate ────────────────────────────────

SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")


def _grab_fn(name: str) -> str:
    import pathlib
    import re

    src = pathlib.Path(SERVER_PATH).read_text()
    m = re.search(
        rf"^((?:async )?def {name}\(.*?)(?=^(?:async )?def |^@|^class |\Z)",
        src,
        re.M | re.S,
    )
    assert m, f"{name} not found"
    return m.group(1)


def _admit_ns(mem_values, heal_result=True, gate=0.90):
    """Exec `_admit` with a scripted memory reading + self-heal outcome.

    ``mem_values``: scalar or per-call sequence (the second read happens only
    after a successful heal).
    """
    import asyncio
    import logging
    import types

    reads = {"n": 0}
    if not isinstance(mem_values, (list, tuple)):
        mem_values = [mem_values]
    healed = {"used": False}

    def _mem():
        v = mem_values[min(reads["n"], len(mem_values) - 1)]
        reads["n"] += 1
        return v

    async def _heal(reason):
        healed["used"] = True
        healed["reason"] = reason
        return heal_result

    ns = {
        "GLOBAL_MEMORY_GATE_RATIO": gate,
        "_cgroup_memory_ratio": _mem,
        "_navigate_self_heal_if_zombie": _heal,
        "logger": logging.getLogger("t33c"),
        "asyncio": asyncio,
    }
    for name in ("_admit", "_is_browser_launch_method"):
        exec(compile(_grab_fn(name), f"<{name}>", "exec"), ns)
    ns["_reads"] = reads
    ns["_healed"] = healed
    return ns


class TestAdmitGate:
    def test_admits_below_gate(self):
        import asyncio

        ns = _admit_ns(mem_values=0.60)
        out = asyncio.run(ns["_admit"]("scrape"))
        assert out == {"admitted": True, "mem_ratio": 0.60}
        assert ns["_healed"]["used"] is False

    def test_falls_open_when_ratio_unreadable(self):
        import asyncio

        ns = _admit_ns(mem_values=None)
        out = asyncio.run(ns["_admit"]("scrape"))
        assert out["admitted"] is True, "None ratio = can't read = fall open"

    def test_refuses_at_gate_after_failed_heal(self):
        import asyncio

        ns = _admit_ns(mem_values=0.95, heal_result=False)
        out = asyncio.run(ns["_admit"]("probe-single:cloak_none"))
        assert out["admitted"] is False
        assert out["mem_ratio"] == 0.95
        assert ns["_healed"]["used"] is True, "the zombie self-heal must be tried first"
        assert "probe-single" in ns["_healed"]["reason"]

    def test_successful_heal_re_reads_and_admits(self):
        import asyncio

        # first read trips the gate; heal frees memory; re-read admits
        ns = _admit_ns(mem_values=[0.95, 0.60], heal_result=True)
        out = asyncio.run(ns["_admit"]("scrape"))
        assert out["admitted"] is True
        assert out["mem_ratio"] == 0.60
        assert ns["_reads"]["n"] == 2

    def test_zero_gate_disables_the_gate(self):
        import asyncio

        ns = _admit_ns(mem_values=0.99, gate=0.0)
        out = asyncio.run(ns["_admit"]("scrape"))
        assert out["admitted"] is True
        assert ns["_reads"]["n"] == 0, "disabled gate must not even read /proc"

    def test_http_rungs_exempt_from_browser_launch_check(self):
        import asyncio

        ns = _admit_ns(mem_values=0.99)
        for m in (
            "direct_http",
            "direct_http_datacenter",
            "fingerprint_chrome_none",
            "fingerprint_safari184_residential",
            None,
        ):
            assert ns["_is_browser_launch_method"](m) is False, m
        for m in ("playwright_none", "cloak_datacenter", "uc_chrome_none"):
            assert ns["_is_browser_launch_method"](m) is True, m


class TestGateWiring:
    def test_probe_single_gates_browser_rungs_only(self):
        import pathlib

        src = pathlib.Path(SERVER_PATH).read_text()
        assert "_is_browser_launch_method(method)" in src
        assert '_admit(f"probe-single:{method}")' in src

    def test_scrape_gates_before_staging(self):
        import pathlib

        src = pathlib.Path(SERVER_PATH).read_text()
        head = src.split('@app.post("/scrape")')[1].split("run_dir = os.path.join")[0]
        assert '_admit("scrape")' in head, "the gate must fire before any staging"

    def test_navigate_delegates_to_admit(self):
        import pathlib

        src = pathlib.Path(SERVER_PATH).read_text()
        assert 'await _admit("navigate", NAVIGATE_MEMORY_GATE_RATIO)' in src

    def test_global_ratio_default_090(self):
        import pathlib

        src = pathlib.Path(SERVER_PATH).read_text()
        assert 'GLOBAL_MEMORY_GATE_RATIO", "0.90"' in src
