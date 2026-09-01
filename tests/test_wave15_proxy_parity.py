"""[wave-15 PR-3] Proxy identity parity — one egress identity per run.

PR-3 closes the gaps where the run's identity changed mid-flight or the
proxy tier silently dropped:

- 3.1 probe bypass: the Akamai bypass used to hardcode ``cloak_none``, so a
  datacenter/residential rung that DETECTED the block sent its bypass out
  unproxied (different Bright Data peer, usually still blocked) and the
  cache recorded the WRONG identity. Same-tier bypass now, and the service
  ladder CONTINUES on bypass failure instead of stopping.
- 3.2 geo: /navigate bodies carry the site's country WHEN proxied, so the
  peer matches the probe's geo instead of re-rolling geo-sensitive scoring.
- 3.3 ssr_div_list: _get_proxy called ProxyConfig.from_file()/get_proxies()
  — neither exists → silent unproxied — and passed httpx the removed
  ``proxies=`` kwarg (TypeError on httpx >=0.28, both images ship 0.28.1).
- 3.4 shared ladder: per-item fetches in the API/HTTP templates were bare
  unproxied GETs; they now ride src.http_fetch (create_fetch_text/json).
  Bonus find: api_scraper's old scrape_product referenced an undefined
  HEADERS → NameError → every item silently None, always.
- 3.5 SCRAPER_PROXY_TIER staging: run_execution + the tester stage the
  probe's working tier so testing, discovery and extraction share ONE
  identity; templates honor the env override.

Run from repo root:
    PYTHONPATH=/app:/app/webapp python -m pytest tests/test_wave15_proxy_parity.py -v
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


def _resp(payload: dict, status: int = 200):
    """Stand-in for an httpx.Response."""
    from types import SimpleNamespace

    r = SimpleNamespace(status_code=status)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


# ═══════════════════════════════════════════════════════════════════════════
# 3.1 — same-tier Akamai bypass (django probe_tools + browser_service probe)
# ═══════════════════════════════════════════════════════════════════════════


class TestDjangoBypassTier:
    @pytest.mark.django_db
    def test_bypass_uses_the_detecting_rungs_tier(self, monkeypatch):
        """direct_http_datacenter detects Akamai → the bypass POST must be
        cloak_DATACENTER, and the cache must record that identity."""
        from agents.tools import probe_tools

        payloads = []

        def fake_post(url, json=None, timeout=None):
            payloads.append({"url": url, "json": json})
            method = (json or {}).get("method", "")
            if method == "direct_http_datacenter":
                return _resp({"success": False, "needs_akamai_bypass": True,
                              "method": "direct_http_datacenter", "status_code": 403})
            if method == "cloak_datacenter":
                return _resp({"success": True, "method": "cloak_datacenter",
                              "status_code": 200, "title": "Real page",
                              "body_length": 5000, "jsonld": [], "meta": {},
                              "selector_results": {}})
            return _resp({"success": False, "status_code": 403})

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)
        monkeypatch.setattr(probe_tools, "_verify_captcha_free",
                            lambda data: {"captcha_detected": False})

        result = probe_tools.run_probe_with_captcha_check(
            "https://example.com/p/1", job_id=0
        )

        bypass_calls = [p for p in payloads
                        if str(p["json"].get("method", "")).startswith("cloak_")]
        assert bypass_calls, "no cloak bypass attempt was made"
        assert bypass_calls[0]["json"]["method"] == "cloak_datacenter", (
            "bypass must ride the DETECTING rung's tier, not hardcoded none"
        )
        assert result["method"] == "cloak_datacenter"

        from scraper.models import ProbeCache

        row = ProbeCache.objects.filter(domain="example.com").first()
        assert row is not None and row.method == "cloak_datacenter", (
            "the cache recorded the WRONG identity if this fails"
        )

    @pytest.mark.django_db
    def test_probe_page_tool_bypass_is_same_tier(self, monkeypatch):
        """The /probe aggregate path (probe_page tool) escalates with the
        aggregate's tier too."""
        from agents.tools import probe_tools

        payloads = []

        def fake_post(url, json=None, timeout=None):
            payloads.append({"url": url, "json": json})
            if url.endswith("/probe"):
                return _resp({"success": False, "needs_akamai_bypass": True,
                              "method": "playwright_residential",
                              "proxy_tier": "residential", "status_code": 403,
                              "title": "", "body_length": 0, "jsonld": [],
                              "meta": {}, "selector_results": {}})
            return _resp({"success": True, "method": "cloak_residential",
                          "proxy_tier": "residential", "status_code": 200,
                          "title": "Real page", "body_length": 5000,
                          "jsonld": [], "meta": {}, "selector_results": {}})

        monkeypatch.setattr(probe_tools.httpx, "post", fake_post)

        probe_fn = probe_tools.get_probe_tools()[0]
        text = probe_fn.invoke({"url": "https://example.com/p/2"})

        singles = [p for p in payloads if p["url"].endswith("/probe-single")]
        assert singles and singles[0]["json"]["method"] == "cloak_residential"
        assert "cloak_residential" in text

        from scraper.models import ProbeCache

        row = ProbeCache.objects.filter(domain="example.com").first()
        assert row is not None and row.method == "cloak_residential"


def _load_probe():
    """Load probe.py as browser_service.probe without the real package
    __init__ (which imports server.py → fastapi). Mirrors
    tests/test_browser_resilience.py."""
    saved_pkg = sys.modules.get("browser_service")
    saved_probe = sys.modules.pop("browser_service.probe", None)
    pkg = types.ModuleType("browser_service")
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules["browser_service"] = pkg
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_service.probe", os.path.join(ROOT, "browser_service", "probe.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["browser_service.probe"] = mod
        spec.loader.exec_module(mod)
    finally:
        if saved_pkg is not None:
            sys.modules["browser_service"] = saved_pkg
        else:
            sys.modules.pop("browser_service", None)
        sys.modules.pop("browser_service.probe", None)
        if saved_probe is not None:
            sys.modules["browser_service.probe"] = saved_probe
    return mod


class TestServiceLadder:
    def _patched_probe(self, monkeypatch, steps, dispatch_by_name, cloak_results):
        """Wire run_probe to a scripted ladder. Returns (probe, cloak_calls)."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "active_escalation_steps", lambda: steps)
        monkeypatch.setattr(mod, "_proxy_tier_configured", lambda tier: True)
        monkeypatch.setattr(mod, "detect_country", lambda url: None)
        monkeypatch.setattr(mod, "fingerprint_step_enabled", lambda: False)
        monkeypatch.setattr(
            mod, "_dispatch_step",
            lambda name, url, timeout, country=None: dispatch_by_name[name],
        )
        cloak_calls = []

        def fake_cloak(url, tier, timeout=None, country=None):
            cloak_calls.append(tier)
            return cloak_results.get(tier)

        monkeypatch.setattr(mod, "_try_cloak", fake_cloak)
        return mod, cloak_calls

    @staticmethod
    def _akamai(method, tier):
        return {"success": False, "method": method, "proxy_tier": tier,
                "needs_akamai_bypass": True, "blocked": True, "status_code": 403}

    def test_bypass_rides_the_detecting_tier(self, monkeypatch):
        mod, cloak_calls = self._patched_probe(
            monkeypatch,
            steps=[("direct_http", "none")],
            dispatch_by_name={"direct_http": self._akamai("direct_http", "none")},
            cloak_results={"none": {"success": True, "method": "cloak_none"}},
        )
        out = mod.run_probe("https://example.com/x")
        assert cloak_calls == ["none"]
        assert out.get("method") == "cloak_none"

    def test_bypass_failure_continues_the_ladder(self, monkeypatch):
        """The old code RETURNED the blocked result — a proxied deployment
        never got its remaining tiers. Now: continue, and a later rung's
        same-tier bypass can still win."""
        mod, cloak_calls = self._patched_probe(
            monkeypatch,
            steps=[("direct_http", "none"),
                   ("playwright_datacenter", "datacenter"),
                   ("playwright_residential", "residential")],
            dispatch_by_name={
                "direct_http": self._akamai("direct_http", "none"),
                "playwright_datacenter": self._akamai("playwright_datacenter", "datacenter"),
                "playwright_residential": self._akamai("playwright_residential", "residential"),
            },
            cloak_results={"none": None, "datacenter": None,
                           "residential": {"success": True, "method": "cloak_residential"}},
        )
        out = mod.run_probe("https://example.com/x")
        assert cloak_calls == ["none", "datacenter", "residential"], (
            "each rung's bypass must ride that rung's own tier"
        )
        assert out.get("method") == "cloak_residential"

    def test_all_bypasses_fail_preserves_the_akamai_signal(self, monkeypatch):
        """Nothing bypasses → the LAST blocked result (needs_akamai_bypass)
        is returned, not a generic all_failed — the caller diagnoses (and
        prescribes) differently for Akamai."""
        mod, cloak_calls = self._patched_probe(
            monkeypatch,
            steps=[("direct_http", "none"),
                   ("playwright_datacenter", "datacenter")],
            dispatch_by_name={
                "direct_http": self._akamai("direct_http", "none"),
                "playwright_datacenter": self._akamai("playwright_datacenter", "datacenter"),
            },
            cloak_results={"none": None, "datacenter": None},
        )
        out = mod.run_probe("https://example.com/x")
        assert cloak_calls == ["none", "datacenter"]
        assert out.get("needs_akamai_bypass") is True
        assert out.get("method") == "playwright_datacenter"

    def test_direct_http_detects_akamai_on_proxied_tiers(self, monkeypatch):
        """The none-only gate is gone: a datacenter rung that gets an Akamai
        page reports needs_akamai_bypass with ITS method/tier (previously a
        generic failure — the bypass never fired)."""
        mod = _load_probe()

        class _R:
            status_code = 403
            # Matches _detect_akamai's 403 keyword set ("access denied").
            text = "<html><body>Access Denied. Reference #18.c3f1</body></html>"

        class _C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return _R()

        import httpx as _httpx

        orig_client = _httpx.Client
        monkeypatch.setattr(_httpx, "Client", _C)
        try:
            out = mod._try_direct_http("https://example.com/x", proxy_tier="datacenter")
        finally:
            monkeypatch.setattr(_httpx, "Client", orig_client)
        assert out["needs_akamai_bypass"] is True
        assert out["method"] == "direct_http_datacenter"
        assert out["proxy_tier"] == "datacenter"


# ═══════════════════════════════════════════════════════════════════════════
# 3.2 — /navigate geo pinning (http_navigation template)
# ═══════════════════════════════════════════════════════════════════════════


def _load_template(relpath: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_httpnav(name: str):
    """http_navigation's config block includes BARE {PLACEHOLDER}s (lists and
    ints, not quoted strings) that only become valid Python after code_writer
    substitutes them. Fill minimal values so the module imports for testing."""
    import tempfile

    src = open(os.path.join(ROOT, "templates", "http_navigation_scraper.py")).read()
    for old, new in (
        ("CATEGORY_URLS = {CATEGORY_URLS}", "CATEGORY_URLS = []"),
        ("ITEMS_PER_PAGE = {ITEMS_PER_PAGE}", "ITEMS_PER_PAGE = 25"),
        ("MAX_PAGES = {MAX_PAGES}", "MAX_PAGES = None"),
        (
            "DELAY_BETWEEN_REQUESTS = {DELAY_BETWEEN_REQUESTS}",
            "DELAY_BETWEEN_REQUESTS = 0",
        ),
    ):
        src = src.replace(old, new)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, dir=tempfile.gettempdir()
    ) as fh:
        fh.write(src)
        path = fh.name
    return _load_template(os.path.relpath(path, ROOT), name)


class TestHttpNavigationGeo:
    def test_placeholder_tier_resolves_to_none(self):
        mod = _load_httpnav("w15_httpnav")
        assert mod._effective_proxy_tier() == "none"
        mod.PROXY_TIER = "residential"
        assert mod._effective_proxy_tier() == "residential"

    def test_proxied_navigate_carries_country(self, monkeypatch):
        mod = _load_httpnav("w15_httpnav_geo")
        mod.PROXY_TIER = "datacenter"
        mod._detect_country = lambda url: "au"
        seen = {}
        monkeypatch.setattr(
            mod.httpx, "post",
            lambda url, json=None, timeout=None: (
                seen.update(url=url, payload=json) or _resp({"success": True, "html": "<x/>"})
            ),
        )
        mod._navigate("https://example.com/p/1")
        assert seen["payload"]["proxy_tier"] == "datacenter"
        assert seen["payload"]["country"] == "au"

    def test_unproxied_navigate_carries_no_country(self, monkeypatch):
        mod = _load_httpnav("w15_httpnav_geo2")
        mod.PROXY_TIER = "none"
        mod._detect_country = lambda url: "au"
        seen = {}
        monkeypatch.setattr(
            mod.httpx, "post",
            lambda url, json=None, timeout=None: (
                seen.update(payload=json) or _resp({"success": True, "html": "<x/>"})
            ),
        )
        mod._navigate("https://example.com/p/1")
        assert seen["payload"]["proxy_tier"] == "none"
        assert seen["payload"]["country"] is None

    def test_staged_tier_env_overrides_the_template_guess(self, monkeypatch):
        src = open(os.path.join(ROOT, "templates", "http_navigation_scraper.py")).read()
        assert 'os.environ.get("SCRAPER_PROXY_TIER")' in src, (
            "the staged probe tier must override the writer's PROXY_TIER guess"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3.3 — ssr_div_list proxy path (real API + httpx 0.28 kwarg)
# ═══════════════════════════════════════════════════════════════════════════


class TestSsrDivListProxy:
    def test_no_removed_proxies_kwarg(self):
        """httpx 0.28.1 in BOTH images: ``proxies=`` raises TypeError, so the
        template must never pass it. (The docstring may MENTION the removed
        kwarg — only the call site is pinned.)"""
        src = open(
            os.path.join(ROOT, "templates", "ssr_div_list_scraper.py")
        ).read()
        assert "proxies=proxy_url" not in src
        assert "proxy=proxy_url" in src

    def test_get_proxy_uses_the_real_api(self, monkeypatch):
        mod = _load_template(
            "templates/ssr_div_list_scraper.py", "w15_ssrdiv"
        )
        monkeypatch.setattr(
            "src.proxy.build_proxy_url",
            lambda tier, config=None, country=None: f"http://{tier}-peer:22225",
        )
        mod.PROXY_TIER = "datacenter"
        assert mod._get_proxy() == "http://datacenter-peer:22225"
        mod.PROXY_TIER = "none"
        assert mod._get_proxy() is None

    def test_tier_reads_the_staged_env(self):
        """The old bare os.environ.get("PROXY_TIER") read an env that is
        never staged — the tier was silently 'none' every run."""
        src = open(
            os.path.join(ROOT, "templates", "ssr_div_list_scraper.py")
        ).read()
        assert 'os.environ.get("SCRAPER_PROXY_TIER"' in src
        assert "build_proxy_url" in src
        # The dead ProxyConfig.from_file/get_proxies call sites are gone —
        # the only remaining mention is the docstring recording the history.
        body = src.split("def _get_proxy", 1)[1]
        assert "ProxyConfig.from_file" not in body.replace(
            "[wave-15 3.3] This used to call ProxyConfig.from_file()/", ""
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3.4 — shared ladder for per-item fetches (src.http_fetch + templates)
# ═══════════════════════════════════════════════════════════════════════════


class _FakeConfig:
    def __init__(self, escalation=("datacenter", "residential")):
        self.config = {"strategy": {"ssl_verify": False, "curl_cffi_tier": False}}
        self._escalation = list(escalation)

    def get_escalation_tier(self):
        return list(self._escalation)

    def get_proxy_dict(self, tier):
        return {"https": f"http://{tier}-proxy:22225"}

    def get_max_retries(self, tier):
        return 1

    def get_cooldown(self, tier):
        return 0

    def get_timeout(self):
        return 10

    def is_banned(self, status_code, text=""):
        return status_code in (403, 503, 429)


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http_fetch.requests.RequestException(str(self.status_code))


class _FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.calls = []
        self._responses = list(responses)
        self._last = _FakeResponse(500)

    def get(self, url, params=None, proxies=None, timeout=None, verify=None):
        self.calls.append({"url": url, "params": params, "proxies": proxies})
        if self._responses:
            return self._responses.pop(0)
        return self._last


import src.http_fetch as http_fetch  # noqa: E402
from src.http_fetch import (  # noqa: E402
    SoftBlock,
    create_fetch_json,
    create_fetch_text,
)


def _text_factory(monkeypatch, responses, config=None):
    cfg = config or _FakeConfig()
    session = _FakeSession(responses)
    monkeypatch.setattr(
        http_fetch, "ProxyConfig",
        type("P", (), {"get_instance": staticmethod(lambda *a, **k: cfg)}),
    )
    monkeypatch.setattr(http_fetch, "should_warn_residential", lambda tier, config=None: False)
    monkeypatch.setattr(http_fetch, "warn_residential_usage", lambda url, config=None: None)
    monkeypatch.setattr(http_fetch.requests, "Session", lambda: session)
    return create_fetch_text(delay_s=0, headers={"User-Agent": "t/1"}), session


class TestFetchTextAndJson:
    def test_success_returns_text_and_status(self, monkeypatch):
        fetch_text, session = _text_factory(
            monkeypatch, [_FakeResponse(200, "<html>ok</html>")]
        )
        out = fetch_text("https://example.com/p/1")
        assert out == ("<html>ok</html>", 200)

    def test_hard_block_escalates_tier(self, monkeypatch):
        fetch_text, session = _text_factory(
            monkeypatch, [_FakeResponse(403), _FakeResponse(200, "ok")]
        )
        out = fetch_text("https://example.com/p/1")
        assert out == ("ok", 200)
        assert session.calls[0]["proxies"] is None
        assert session.calls[1]["proxies"] == {"https": "http://datacenter-proxy:22225"}

    def test_min_tier_slices_the_ladder(self, monkeypatch):
        fetch_text, session = _text_factory(
            monkeypatch, [_FakeResponse(200, "ok")]
        )
        fetch_text("https://example.com/p/1", min_tier=1)
        assert session.calls[0]["proxies"] == {"https": "http://datacenter-proxy:22225"}

    def test_params_passthrough(self, monkeypatch):
        fetch_text, session = _text_factory(
            monkeypatch, [_FakeResponse(200, "ok")]
        )
        fetch_text("https://example.com/api", params={"limit": 50})
        assert session.calls[0]["params"] == {"limit": 50}

    def test_all_tiers_fail_returns_none(self, monkeypatch):
        fetch_text, _s = _text_factory(
            monkeypatch, [_FakeResponse(403), _FakeResponse(403), _FakeResponse(403)]
        )
        assert fetch_text("https://example.com/p/1") is None

    def test_soft_block_returns_the_falsy_signal(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_SOFT_BLOCK_MIN_BYTES", "10000")
        fetch_text, _s = _text_factory(
            monkeypatch, [_FakeResponse(200, "captcha _abck=1; verify you are a human")]
        )
        out = fetch_text("https://example.com/p/1")
        assert isinstance(out, SoftBlock) and not out

    def test_fetch_json_parses_and_passes_params(self, monkeypatch):
        cfg = _FakeConfig()
        session = _FakeSession([_FakeResponse(200, '{"a": 1}')])
        monkeypatch.setattr(
            http_fetch, "ProxyConfig",
            type("P", (), {"get_instance": staticmethod(lambda *a, **k: cfg)}),
        )
        monkeypatch.setattr(http_fetch, "should_warn_residential", lambda tier, config=None: False)
        monkeypatch.setattr(http_fetch, "warn_residential_usage", lambda url, config=None: None)
        monkeypatch.setattr(http_fetch.requests, "Session", lambda: session)
        fetch_json = create_fetch_json(delay_s=0, headers={"User-Agent": "t/1"})
        out = fetch_json("https://example.com/api", params={"limit": 7})
        assert out == ({"a": 1}, 200)
        assert session.calls[0]["params"] == {"limit": 7}

    def test_fetch_json_non_json_body_is_none_not_exception(self, monkeypatch):
        cfg = _FakeConfig()
        monkeypatch.setattr(
            http_fetch, "ProxyConfig",
            type("P", (), {"get_instance": staticmethod(lambda *a, **k: cfg)}),
        )
        monkeypatch.setattr(http_fetch, "should_warn_residential", lambda tier, config=None: False)
        monkeypatch.setattr(http_fetch, "warn_residential_usage", lambda url, config=None: None)
        monkeypatch.setattr(
            http_fetch.requests, "Session",
            lambda: _FakeSession([_FakeResponse(200, "<html>not json</html>")]),
        )
        fetch_json = create_fetch_json(delay_s=0)
        assert fetch_json("https://example.com/api") is None


class TestTemplateLadderAdoption:
    def _src(self, name):
        return open(os.path.join(ROOT, "templates", name)).read()

    def test_api_scraper_items_ride_the_ladder(self):
        src = self._src("api_scraper.py")
        assert "create_fetch_json" in src
        # The old inline fetch referenced an UNDEFINED HEADERS → NameError →
        # every item silently None. The bare fallback must not regress.
        assert "headers=API_HEADERS" in src

    def test_shopify_scraper_items_ride_the_ladder(self):
        src = self._src("shopify_scraper.py")
        assert "create_fetch_json" in src
        assert "headers=HEADERS" in src

    def test_http_navigation_get_rides_the_ladder(self):
        src = self._src("http_navigation_scraper.py")
        assert "create_fetch_text" in src

    def test_templates_pin_one_closure_per_process(self):
        """The Session must persist across items for cookie continuity
        (job-58) — a per-call factory would re-open a fresh identity."""
        for name in ("api_scraper.py", "shopify_scraper.py"):
            src = self._src(name)
            assert "_FETCH_JSON = None" in src
            assert "_get_fetch_json" in src

    def test_api_scraper_fallback_uses_importerror_not_bare(self):
        """The bare-GET fallback exists for images predating the module — it
        must trigger on ImportError only, never swallow ladder failures."""
        src = self._src("api_scraper.py")
        assert "except ImportError:" in src


# ═══════════════════════════════════════════════════════════════════════════
# 3.5 — SCRAPER_PROXY_TIER staging
# ═══════════════════════════════════════════════════════════════════════════


class TestScraperProxyTierStaging:
    def _re(self):
        # NOT "import agents.nodes.run_execution as run_exec": the nodes
        # package __init__ rebinds the name `run_execution` to the node
        # FUNCTION, so the `as` import binds the function, not the module.
        import importlib

        return importlib.import_module("agents.nodes.run_execution")

    def test_tier_from_probe_result_method(self):
        run_exec = self._re()
        assert run_exec._scraper_proxy_tier(
            {"probe_result": {"method": "playwright_residential"}}
        ) == "residential"

    def test_tier_from_probe_method_state(self):
        run_exec = self._re()
        assert run_exec._scraper_proxy_tier(
            {"probe_method": "cloak_datacenter"}
        ) == "datacenter"

    def test_tier_from_connectivity_method_that_worked(self):
        run_exec = self._re()
        assert run_exec._scraper_proxy_tier(
            {"probe_result": {"connectivity": {"method_that_worked":
                                               "fingerprint_chrome_residential"}}}
        ) == "residential"

    def test_unproxied_methods_stage_nothing(self):
        run_exec = self._re()
        assert run_exec._scraper_proxy_tier(
            {"probe_result": {"method": "direct_http"}}
        ) == ""
        assert run_exec._scraper_proxy_tier({}) == ""

    def test_stealth_env_carries_both_keys(self):
        run_exec = self._re()
        env = run_exec._stealth_env(
            {"anti_bot_detected": True,
             "probe_result": {"method": "cloak_residential"}}
        )
        assert env == {"STEALTH_BROWSER": "cloak", "SCRAPER_PROXY_TIER": "residential"}

    def test_tester_stages_the_same_tier(self):
        """Tester parity is a source pin: run_scraper's env block must stage
        SCRAPER_PROXY_TIER from the probe method."""
        src = open(os.path.join(ROOT, "webapp", "agents", "tools", "shell_tools.py")).read()
        assert 'env_overrides["SCRAPER_PROXY_TIER"]' in src
        assert "get_probe_method" in src

    def test_all_reader_templates_honor_the_env(self):
        for name in (
            "playwright_scraper.py",
            "http_navigation_scraper.py",
            "ssr_div_list_scraper.py",
        ):
            src = open(os.path.join(ROOT, "templates", name)).read()
            assert 'os.environ.get("SCRAPER_PROXY_TIER"' in src, name
