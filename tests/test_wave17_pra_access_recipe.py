"""[wave-17 PR-A] Access-recipe chain: probe rungs → strategy → runtime env.

Locks the PR-A fixes that the local e2e (jobs 322/325) proved live, plus the
TWO GENERIC PROBE DEFECTS the e2e itself uncovered on the crocs listing:

1. probe rung content race — ``page.content()`` raised mid-navigation
   (redirect chains keep committing after domcontentloaded); the rungs
   swallowed the raise → ``None`` → "Method returned no result" → a page a
   patient fetch opens fine got recorded as BLOCKED. (_try_cloak returned
   None in 9.5s; manual /navigate with settle succeeded every time.)
2. challenge settle — once the raise was survived, the flat 2s snapshot read
   the Cloudflare managed-challenge interstitial (auto-clears ~5-10s later)
   and honestly-wrongly reported blocked. Bounded poll past the interstitial.

And the propagation contracts those measurements feed:
3. ``_derive_strategy`` — listing evidence flips http_requests→
   http_navigation, raises the proxy tier, and records ``access_recipe``
   with per-URL-class evidence.
4. ``run_listing_probe_advisory`` — ≤3 rungs, first-win stops, browser win
   ⇒ ``needs_browser``, all-fail ⇒ blocked with the tried methods.
5. execution stealth — ``_needs_cloak``/``_scraper_proxy_tier`` honor the
   listing measurement and the recipe (recipe outranks the PDP-only signal).
6. render gate (server.py /navigate) — satisfied-predicate semantics, base
   settle clamp, backoff escalation, budget cap; the content-over-status
   override is locked by source contract (the block lives inside
   ``_run_navigate_sync``, too heavy to exec; the e2e is its proof).

Probe.py loads via the spec_from_file_location trick (real package __init__
imports server.py → fastapi, absent in the django test image); server.py
blocks are exec-extracted per name. Both mirror tests/test_wave14_browser_service.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")
PROBE_PATH = os.path.join(ROOT, "browser_service", "probe.py")


# ─── loaders (mirror tests/test_wave14_browser_service.py) ──────────────────


def _load_probe_mod():
    """Load probe.py without the real package __init__ (which imports
    server.py → fastapi)."""
    saved_pkg = sys.modules.get("browser_service")
    saved_probe = sys.modules.pop("browser_service.probe", None)
    pkg = types.ModuleType("browser_service")
    pkg.__path__ = [os.path.join(ROOT, "browser_service")]
    sys.modules["browser_service"] = pkg
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_service.probe", PROBE_PATH
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


def _src() -> str:
    with open(SERVER_PATH, encoding="utf-8") as fh:
        return fh.read()


def _grab(name: str) -> str:
    src = _src()
    m = re.search(
        rf"^def {name}\(.*?(?=^(?:async )?def |^class |^@)", src, re.M | re.S
    )
    assert m, f"{name} not found in server.py"
    return m.group(0)


class _FakeLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _exec_ns(names: list[str], extra: dict | None = None) -> dict:
    ns: dict = {"__name__": "t_wave17_pra", "logger": _FakeLogger(),
                **(extra or {})}
    for name in names:
        if name.startswith("_RENDER"):
            m = re.search(
                rf"^{name} = .*?(?=^[A-Za-z_@])", _src(), re.M | re.S
            )
            assert m, f"{name} not found in server.py"
            code = m.group(0)
        else:
            code = _grab(name)
        exec(compile(code, f"<{name}>", "exec"), ns)
    return ns


# ─── fake pages ──────────────────────────────────────────────────────────────


class _RacePage:
    """content() raises while a navigation is committing, then recovers."""

    def __init__(self, html: str, races: int = 2):
        self._html = html
        self._races = races
        self.waits: list[int] = []

    def wait_for_load_state(self, state, timeout):
        raise RuntimeError("simulated load-event miss")

    def content(self):
        if self._races > 0:
            self._races -= 1
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page is "
                "navigating and changing the content."
            )
        return self._html

    def title(self):
        return "T"

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class _ChallengePage:
    """evaluate() returns a probe snapshot; content() reveals the interstitial
    first and real content after enough settle time."""

    def __init__(self, interstitial: str, real: str, clears_after: int):
        self._interstitial = interstitial
        self._real = real
        self._clears_after = clears_after
        self._reads = 0
        self.waits: list[int] = []

    def wait_for_load_state(self, state, timeout):
        pass

    def content(self):
        self._reads += 1
        return self._real if self._reads > self._clears_after else self._interstitial

    def title(self):
        return "Kids' Shoes"

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self._reads += 0  # settle time passes between reads


def _probe_of(anchors: int, body_len: int, jsonld: int = 0, price: bool = False):
    return {"jsonld_items": jsonld, "anchors": anchors, "body_len": body_len,
            "has_price": price}


# ─── 1. probe rungs: content race + challenge settle ────────────────────────


class TestProbeRungSettle:
    def test_safe_page_content_survives_navigation_race(self):
        mod = _load_probe_mod()
        page = _RacePage("<html><body>real listing</body></html>", races=2)
        html = mod._safe_page_content(page)
        assert "real listing" in html

    def test_settle_returns_immediately_on_healthy_content(self):
        mod = _load_probe_mod()
        page = _ChallengePage("<html>challenge</html>",
                              "<html><body>" + "x" * 3000 + "</body></html>",
                              clears_after=0)
        html = mod._settle_past_challenge(page)
        assert len(html) > 2000
        assert page.waits == [], "healthy page must not pay the settle budget"

    def test_settle_polls_past_challenge_interstitial(self):
        mod = _load_probe_mod()
        page = _ChallengePage("<html><body>Just a moment...</body></html>",
                              "<html><body>" + "y" * 5000 + "</body></html>",
                              clears_after=2)
        html = mod._settle_past_challenge(page)
        assert "yyyy" in html
        assert len(page.waits) >= 2, "must wait between challenge reads"

    def test_persistent_challenge_reports_blocked_honestly(self):
        mod = _load_probe_mod()
        page = _ChallengePage("<html><body>Access Denied</body></html>",
                              "<html><body>Access Denied</body></html>",
                              clears_after=99)
        html = mod._settle_past_challenge(page)
        assert len(html) < 2000, "keeps the LAST read — classified blocked"

    def test_try_cloak_returns_result_dict_through_content_race(self):
        """The e2e defect verbatim: a racing content() turned into None →
        'Method returned no result' → listing 'blocked'. The rung must
        return a RESULT (success or honest blocked), never None, when only
        the read raced."""
        mod = _load_probe_mod()

        class _Resp:
            status = 200

        class _CtxPage(_RacePage):
            def goto(self, url, wait_until=None, timeout=None):
                return _Resp()

            def evaluate(self, js):
                return ""

        class _Ctx:
            def __init__(self):
                self.page = _CtxPage(
                    "<html><body><title>Kids' Shoes</title>" + "z" * 3000
                    + "</body></html>", races=1)
                self.close = lambda: None

        def _fake_launch(**kwargs):
            return _Ctx()

        import src.page_analysis as _pa

        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(mod, "_launch_page", _fake_launch)
            monkey.setattr(_pa, "run_selector_tests", lambda page: {})
            result = mod._try_cloak(
                "https://example.com/c/kids/footwear", "residential",
                timeout=30,
            )
        finally:
            monkey.undo()
        assert result is not None, "content race must not void the rung"
        assert isinstance(result, dict)
        assert result.get("success") is True, result

    def test_challenge_settle_present_in_both_browser_rungs(self):
        """Both playwright and cloak rungs read through the settle (the flat
        2s snapshot was shared)."""
        src = open(PROBE_PATH, encoding="utf-8").read()
        for fn in ("_try_playwright", "_try_cloak"):
            m = re.search(rf"^def {fn}\(.*?(?=^def )", src, re.M | re.S)
            assert m, f"{fn} not found"
            assert "_settle_past_challenge(page)" in m.group(0), (
                f"{fn} must read content via _settle_past_challenge"
            )


# ─── 2. render gate (server.py /navigate) ───────────────────────────────────


class TestRenderGate:
    NS = _exec_ns(
        [
            "_RENDER_PROBE_JS",
            "_RENDER_GATE_MIN_ANCHORS",
            "_RENDER_GATE_MIN_BODY",
            "_RENDER_GATE_BACKOFF_MS",
            "_RENDER_GATE_MAX_BUDGET_MS",
            "_render_gate_satisfied",
            "_render_probe",
            "_render_settle",
        ],
        extra={"os": os, "time": __import__("time")},
    )

    def test_satisfied_predicate_each_signal(self):
        sat = self.NS["_render_gate_satisfied"]
        assert sat(_probe_of(anchors=0, body_len=0, jsonld=2), False)
        assert sat(_probe_of(anchors=30, body_len=100), False)
        assert sat(_probe_of(anchors=0, body_len=25_000), False)
        assert sat(_probe_of(anchors=0, body_len=1, price=True), False)
        assert sat({}, False) is False
        assert sat(_probe_of(anchors=0, body_len=0), True), "wait_for hit wins"

    def test_probe_eval_failure_yields_empty_probe(self):
        class _P:
            def evaluate(self, js):
                raise RuntimeError("execution context destroyed")

        assert self.NS["_render_probe"](_P()) == {}

    def test_settle_satisfied_after_base_settle(self):
        class _Page:
            waits: list[int] = []

            def wait_for_timeout(self, ms):
                self.waits.append(ms)

            def evaluate(self, js):
                return _probe_of(anchors=40, body_len=90_000)

        out = self.NS["_render_settle"](_Page(), None, 4000, 120)
        assert out["satisfied"] is True
        assert out["steps"] == ["base_settle:4000"]
        assert out["waited_ms"] == 4000

    def test_settle_escalates_through_backoff_until_satisfied(self):
        class _Page:
            def __init__(self):
                self.calls = 0
                self.waits: list[int] = []

            def wait_for_timeout(self, ms):
                self.waits.append(ms)

            def evaluate(self, js):
                self.calls += 1
                return (_probe_of(anchors=0, body_len=500) if self.calls <= 2
                        else _probe_of(anchors=40, body_len=90_000))

        page = _Page()
        out = self.NS["_render_settle"](page, None, 1500, 120)
        assert out["satisfied"] is True
        assert out["steps"][0] == "base_settle:1500"
        assert "gate_backoff:2500" in out["steps"], out["steps"]

    def test_settle_base_settle_clamped_to_budget(self):
        class _Page:
            def __init__(self):
                self.waits: list[int] = []

            def wait_for_timeout(self, ms):
                self.waits.append(ms)

            def evaluate(self, js):
                return _probe_of(anchors=0, body_len=500)

        page = _Page()
        out = self.NS["_render_settle"](page, None, 40_000, 120)
        assert out["steps"][0] == "base_settle:25000", out["steps"]
        assert out["waited_ms"] <= 25_000

    def test_content_over_status_locked_in_navigate_source(self):
        """Render-gate-satisfied overrides blocked_type — locked as a source
        contract (the block lives in _run_navigate_sync; live e2e proved the
        behavior on the 429-with-content listing)."""
        src = _src()
        assert 'render_gate.get("satisfied")' in src
        m = re.search(
            r'if blocked_type and render_gate\.get\("satisfied"\):.*?'
            r'blocked_type = None',
            src,
            re.S,
        )
        assert m, "content-over-status override missing from _run_navigate_sync"


# ─── 3. _derive_strategy: listing evidence → recipe + flip ──────────────────


def _nav_listing_state(listing_probe: dict | None) -> dict:
    state = {
        "url": "https://www.crocs.com/p/kids-classic-clog/206991.html",
        "site_slug": "crocs-com",
        "input_mode": "list_page",
        "search_criteria": "https://www.crocs.com/c/kids/footwear",
        "probe_result": {
            "connectivity": {"method_that_worked": "direct_http_residential"},
            "anti_bot": {"detected": False},
        },
        "navigation_analysis": {"discovery": {"listing_reached": True}},
    }
    if listing_probe is not None:
        state["probe_result"]["listing_connectivity"] = listing_probe
    return state


def _advisory(method: str) -> dict:
    return {
        "probed_url": "https://www.crocs.com/c/kids/footwear",
        "advisory": True,
        "method_that_worked": method,
        "http_ok": method.startswith("direct_http"),
        "browser_ok": bool(method) and not method.startswith("direct_http"),
        "needs_browser": bool(method) and not method.startswith("direct_http"),
        "blocked": not bool(method),
        "status_code": 200,
        "body_length": 100_000,
        "methods_tried": ["direct_http_residential", "cloak_residential"],
        "attempts": [],
    }


class TestDeriveStrategyPropagation:
    def test_listing_browser_only_flips_strategy_and_raises_tier(self):
        from agents.graph import _derive_strategy

        analysis = _derive_strategy(_nav_listing_state(_advisory("cloak_residential")))
        assert analysis["strategy"] == "http_navigation", (
            "listing browser-only must flip http_requests → http_navigation"
        )
        recipe = analysis["access_recipe"]
        assert recipe["stealth"] == "cloak"
        assert recipe["proxy_tier"] == "residential"
        assert recipe["needs_browser"] is True
        assert recipe["listing_probe"] is True
        assert "listing_method=cloak_residential" in recipe["evidence"]
        assert analysis.get("method_that_worked"), "pdp method recorded"
        assert analysis["listing_connectivity"]["method_that_worked"] == (
            "cloak_residential"
        )

    def test_no_listing_probe_keeps_plain_shape(self):
        from agents.graph import _derive_strategy

        analysis = _derive_strategy(_nav_listing_state(None))
        recipe = analysis["access_recipe"]
        assert recipe["listing_probe"] is False
        # No stealth evidence (PDP answered plain HTTP, no listing probe) —
        # the recipe must NOT invent cloak even though the list_page cascade
        # itself is browser-shaped (discovery rides /navigate).
        assert recipe["stealth"] == "none"

    def test_listing_http_win_does_not_flip(self):
        from agents.graph import _derive_strategy

        analysis = _derive_strategy(
            _nav_listing_state(_advisory("direct_http_residential"))
        )
        recipe = analysis["access_recipe"]
        assert recipe["proxy_tier"] == "residential", (
            "listing HTTP win still records the measured tier"
        )
        assert recipe["stealth"] == "none", (
            "an HTTP listing win is not stealth evidence"
        )


# ─── 4. run_listing_probe_advisory: rung order + verdict shape ──────────────


class TestListingProbeAdvisory:
    def _run(self, monkeypatch, responses: dict[str, dict]):
        from agents.tools import probe_tools

        calls: list[str] = []

        class _Resp:
            def __init__(self, payload):
                self._p = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._p

        def _post(url, json=None, timeout=None):
            method = json["method"]
            calls.append(method)
            return _Resp(responses.get(method, {"success": False}))

        monkeypatch.setattr(probe_tools.httpx, "post", _post)
        import src.geo as geo

        monkeypatch.setattr(geo, "detect_country", lambda u: "US")
        out = probe_tools.run_listing_probe_advisory(
            "https://www.crocs.com/c/kids/footwear",
            {"http_method": "direct_http_residential"},
            job_id=0,
        )
        return out, calls

    def test_rungs_ordered_and_stop_at_first_win(self, monkeypatch):
        out, calls = self._run(
            monkeypatch,
            {
                "direct_http_residential": {"success": True, "status_code": 200,
                                            "body_length": 90_000},
                "cloak_none": {"success": True},
                "cloak_datacenter": {"success": True},
                "cloak_residential": {"success": True},
            },
        )
        assert calls == ["direct_http_residential"], "stops at the first win"
        assert out["http_ok"] is True and out["needs_browser"] is False

    def test_browser_only_listing_reports_needs_browser(self, monkeypatch):
        out, calls = self._run(
            monkeypatch,
            {
                "direct_http_residential": {"success": False},
                "cloak_none": {"success": False},
                "cloak_datacenter": {"success": True, "status_code": 429,
                                     "body_length": 158_455},
                "cloak_residential": {"success": True},
            },
        )
        assert calls == ["direct_http_residential", "cloak_none",
                         "cloak_datacenter"], (
            "the datacenter rung sits between none and residential and stops "
            "at the first win — a tier that can serve BOTH URL classes"
        )
        assert out["needs_browser"] is True
        assert out["method_that_worked"] == "cloak_datacenter"
        assert out["blocked"] is False

    def test_residential_win_when_datacenter_blocked(self, monkeypatch):
        out, calls = self._run(
            monkeypatch,
            {
                "direct_http_residential": {"success": False},
                "cloak_none": {"success": False},
                "cloak_datacenter": {"success": False},
                "cloak_residential": {"success": True, "status_code": 429,
                                      "body_length": 134_208},
            },
        )
        assert calls == ["direct_http_residential", "cloak_none",
                         "cloak_datacenter", "cloak_residential"]
        assert out["method_that_worked"] == "cloak_residential"

    def test_all_fail_reports_blocked_with_methods_tried(self, monkeypatch):
        out, calls = self._run(monkeypatch, {})
        assert calls == ["direct_http_residential", "cloak_none",
                         "cloak_datacenter", "cloak_residential"]
        assert out["blocked"] is True
        assert out["needs_browser"] is False
        assert out["method_that_worked"] == ""


# ─── 5. execution stealth: listing evidence + recipe precedence ─────────────


class TestExecutionStealthFromListing:
    def test_listing_browser_method_needs_cloak(self):
        from agents.nodes.run_execution import _needs_cloak, _scraper_proxy_tier

        state = _nav_listing_state(_advisory("cloak_residential"))
        assert _needs_cloak(state) is True
        assert _scraper_proxy_tier(state) == "residential"

    def test_recipe_outranks_pdp_only_signal(self):
        from agents.nodes.run_execution import _needs_cloak, _scraper_proxy_tier

        state = _nav_listing_state(None)
        state["scraper_analysis"] = {
            "access_recipe": {"stealth": "cloak", "proxy_tier": "datacenter"},
        }
        assert _scraper_proxy_tier(state) == "datacenter", (
            "access_recipe is the synthesis — it outranks the PDP method"
        )
        assert _needs_cloak(state) is True

    def test_plain_http_state_stages_nothing(self):
        from agents.nodes.run_execution import _needs_cloak, _scraper_proxy_tier

        state = {
            "probe_result": {"connectivity": {"method_that_worked": "direct_http"}},
        }
        assert _needs_cloak(state) is False
        assert _scraper_proxy_tier(state) == ""


# ─── 6. shell_tools env staging: recipe → env_overrides ─────────────────────


class TestShellToolsRecipeStaging:
    def test_recipe_cloak_and_tier_reach_env_overrides(self):
        """Source contract: run_scraper stages STEALTH_BROWSER/SCRAPER_PROXY_TIER
        from state's access_recipe (the tester-parity half of the recipe
        chain). Behavior is e2e-proven (balenciaga ran with stealth=cloak);
        this locks the wiring."""
        src = open(
            os.path.join(ROOT, "webapp", "agents", "tools", "shell_tools.py"),
            encoding="utf-8",
        ).read()
        assert 'access_recipe' in src
        m = re.search(
            r'_recipe = \(.*?STEALTH_BROWSER.*?SCRAPER_PROXY_TIER.*?\n            except',
            src, re.S,
        )
        assert m, "recipe-based env staging block missing from run_scraper"
        assert '"STEALTH_BROWSER": "cloak"' in src
        assert 'env_overrides["SCRAPER_PROXY_TIER"] = _rtier' in src
