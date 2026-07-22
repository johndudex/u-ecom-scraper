import json
import logging
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.geo import detect_country
from src.page_analysis import (
    extract_jsonld,
    extract_meta_tags,
    extract_title,
    has_price,
    is_blocked,
)

from .config import get_proxy_config

logger = logging.getLogger(__name__)

DISPLAY = os.environ.get("DISPLAY", ":98")
DEFAULT_TIMEOUT = 60

PROXY_TIERS = ["none", "datacenter", "residential"]


# NOTE: ``uc_chrome_*`` steps were removed — CloakBrowser (``cloak_*``) is the
# documented successor that "defeats Akamai where vanilla Playwright AND UC mode
# fail". The default deployment also has no proxies configured
# (PROXY_*_USER empty), so the datacenter/residential tiers are skipped at
# runtime via the guard in ``run_probe``/``render_page`` — leaving a 3-step
# escalation (direct_http → playwright → cloak) in the common case. Legacy
# ProbeCache ``start_method`` values naming ``uc_chrome_*`` are aliased to
# ``cloak_*`` via ``_resolve_start_method`` so cached hints keep working.
# See docs/browser-service-rework-plan.md.
ESCALATION_STEPS = [
    ("direct_http", "none"),
    ("playwright_none", "none"),
    ("cloak_none", "none"),
    ("direct_http_datacenter", "datacenter"),
    ("playwright_datacenter", "datacenter"),
    ("cloak_datacenter", "datacenter"),
    ("direct_http_residential", "residential"),
    ("playwright_residential", "residential"),
    ("cloak_residential", "residential"),
]

# Legacy probe-method aliases. ``uc_chrome`` is gone (consolidated into cloak);
# cached start_method hints naming these are routed to the cloak successor so
# existing ProbeCache rows keep their skip-ahead behaviour instead of falling
# back to a full-chain re-probe.
_LEGACY_METHOD_ALIASES = {
    "uc_chrome_none": "cloak_none",
    "uc_chrome_datacenter": "cloak_datacenter",
    "uc_chrome_residential": "cloak_residential",
}


def _resolve_start_method(start_method: Optional[str]) -> Optional[str]:
    """Map legacy probe method names to their current equivalents."""
    if not start_method:
        return start_method
    return _LEGACY_METHOD_ALIASES.get(start_method, start_method)


def _proxy_tier_configured(tier: str) -> bool:
    """True if the given proxy tier has credentials configured.

    ``build_proxy_url`` returns None when the tier lacks host/username, which is
    the default deployment state (no PROXY_*_USER). We skip those tiers in the
    escalation so we don't re-run 'none'-equivalent browser launches that burn
    30-45s each.
    """
    if tier == "none":
        return True
    return bool(get_proxy_config().build_proxy_url(tier))


def _dispatch_step(method_name: str, url: str, timeout: int, country: Optional[str] = None):
    if method_name == "direct_http":
        return _try_direct_http(url, timeout=timeout, proxy_tier="none")
    if method_name.startswith("direct_http_"):
        tier = method_name.replace("direct_http_", "")
        return _try_direct_http(url, timeout=timeout, proxy_tier=tier, country=country)
    if method_name.startswith("cloak_"):
        tier = method_name.replace("cloak_", "")
        return _try_cloak(url, tier, timeout=min(timeout, 40), country=country)
    if method_name.startswith("playwright_"):
        tier = method_name.replace("playwright_", "")
        pw_timeout = 35 if tier != "none" else 25
        return _try_playwright(url, tier, timeout=min(timeout, pw_timeout), country=country)
    return None


def run_probe(url: str, render_js: bool = True, timeout: int = 120, start_method: Optional[str] = None, country: Optional[str] = None) -> dict[str, Any]:
    steps_log = []
    debug_path = "/tmp/probe_debug.json"

    def _log_step(msg):
        steps_log.append(msg)
        logger.info("PROBE [%s]: %s", url[:80], msg)

    _log_step(f"Starting probe: render_js={render_js}, timeout={timeout}, start_method={start_method}")

    if country is None:
        country = detect_country(url)
        if country:
            _log_step(f"Auto-detected country: {country}")

    start_method = _resolve_start_method(start_method)
    skip_index = 0
    if start_method:
        for i, (step_name, _) in enumerate(ESCALATION_STEPS):
            if step_name == start_method:
                skip_index = i
                _log_step(f"Cache hint: starting at step {i} ({step_name})")
                break

    if not render_js:
        result = _try_direct_http(url, timeout=timeout)
        if result and result.get("success"):
            return result
        return result or _failure_result("all_failed", "none", "Direct HTTP failed and render_js=false")

    for i, (step_name, proxy_tier) in enumerate(ESCALATION_STEPS):
        if i < skip_index:
            continue

        # Skip unconfigured proxy tiers (default deployment has no proxies —
        # datacenter/residential steps would launch through NO proxy, identical
        # to the 'none' tier already tried, wasting 30-45s each).
        if not _proxy_tier_configured(proxy_tier):
            _log_step(f"{step_name}: skipped (proxy tier '{proxy_tier}' not configured)")
            continue

        _log_step(f"{step_name}: trying...")
        result = _dispatch_step(step_name, url, timeout, country=country)
        if result:
            _log_step(
                f"{step_name}: method={result.get('method')}, success={result.get('success')}, "
                f"body={result.get('body_length', 0)}, blocked={result.get('blocked')}, "
                f"err={result.get('error', '')[:120]}"
            )

        if result and result.get("needs_akamai_bypass"):
            _log_step(f"{step_name}: Akamai detected — trying CloakBrowser stealth bypass")
            cloak_res = _try_cloak(url, "none", timeout=min(timeout, 40), country=country)
            if cloak_res and cloak_res.get("success"):
                _log_step("cloak_none: SUCCEEDED (Akamai bypassed)")
                return cloak_res
            _log_step(f"{step_name}: cloak did not bypass Akamai; stopping escalation")
            return result

        if result and result.get("success"):
            _log_step(f"{step_name}: SUCCEEDED")
            return result

    _log_step("ALL FAILED")
    try:
        with open(debug_path, "w") as f:
            json.dump({"url": url, "steps": steps_log}, f, indent=2)
    except Exception:
        pass
    return _failure_result("all_failed", "none", "All probe methods failed")


MAX_RENDER_HTML = 500_000

_render_captured_html: str = ""


def _capture_html_for_render(html: str) -> None:
    """Side-channel to capture HTML from probe functions during render_page."""
    global _render_captured_html
    if len(html) > MAX_RENDER_HTML:
        html = html[:MAX_RENDER_HTML]
    _render_captured_html = html


def render_page(
    url: str,
    timeout: int = 120,
    start_method: Optional[str] = None,
    country: Optional[str] = None,
    accept_language: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch a page and return the full HTML using the correct access method.

    Uses the same escalation chain as ``run_probe`` but returns the raw HTML
    content (truncated to ``MAX_RENDER_HTML`` chars).  This is used by agents
    that need the full page DOM (e.g. navigation_explore for extracting
    category links, search forms, product cards).

    Returns a dict with: ``success``, ``html``, ``status_code``, ``method``,
    ``title``, ``proxy_tier``, ``error``.
    """
    if country is None:
        country = detect_country(url)

    _AKAMAI_METHOD_MAP = {
        "akamai_playwright_stealth": "cloak_none",
        "akamai_bypass": "cloak_none",
    }
    if start_method and start_method in _AKAMAI_METHOD_MAP:
        mapped = _AKAMAI_METHOD_MAP[start_method]
        logger.info(
            "RENDER [%s]: mapped akamai method %s -> %s",
            url[:80],
            start_method,
            mapped,
        )
        start_method = mapped

    start_method = _resolve_start_method(start_method)
    skip_index = 0
    if start_method:
        for i, (step_name, _) in enumerate(ESCALATION_STEPS):
            if step_name == start_method:
                skip_index = i
                logger.info(
                    "RENDER [%s]: cache hint starting at %s", url[:80], step_name
                )
                break

    for i, (step_name, proxy_tier) in enumerate(ESCALATION_STEPS):
        if i < skip_index:
            continue

        # Skip unconfigured proxy tiers (default deployment has no proxies —
        # datacenter/residential steps would launch through NO proxy, identical
        # to the 'none' tier already tried, wasting 30-45s each).
        if not _proxy_tier_configured(proxy_tier):
            logger.info(
                "RENDER [%s]: skipping %s (proxy tier '%s' not configured)",
                url[:80], step_name, proxy_tier,
            )
            continue

        logger.info("RENDER [%s]: trying %s", url[:80], step_name)
        global _render_captured_html
        _render_captured_html = ""
        result = _dispatch_step(step_name, url, timeout, country=country)

        if result and result.get("needs_akamai_bypass"):
            logger.info("RENDER [%s]: Akamai detected, escalating", url[:80])
            continue

        if result and result.get("success"):
            html = _render_captured_html
            if not html:
                html = _refetch_html(url, step_name, proxy_tier, timeout, country)
            return {
                "success": True,
                "html": html[:MAX_RENDER_HTML],
                "status_code": result.get("status_code", 200),
                "method": result.get("method", step_name),
                "title": result.get("title", ""),
                "proxy_tier": proxy_tier,
                "error": "",
            }

    return {
        "success": False,
        "html": "",
        "status_code": 0,
        "method": "all_failed",
        "title": "",
        "proxy_tier": "none",
        "error": "All render methods failed",
    }


def _refetch_html(
    url: str,
    step_name: str,
    proxy_tier: str,
    timeout: int,
    country: Optional[str],
) -> str:
    """Best-effort re-fetch of HTML when the probe result lacks it.

    The ``_try_*`` functions include ``body_text`` (1500 chars) but not the
    raw HTML.  For methods that already have a browser session, we re-run
    a lightweight fetch to get the full page source.
    """
    try:
        if step_name == "direct_http" or step_name.startswith("direct_http_"):
            import httpx
            from src.page_analysis import get_user_agent

            config = get_proxy_config()
            proxy_url = (
                config.build_proxy_url(proxy_tier, country=country)
                if proxy_tier != "none"
                else None
            )
            with httpx.Client(
                timeout=min(timeout, 15),
                follow_redirects=True,
                proxy=proxy_url,
                headers={"User-Agent": get_user_agent()},
            ) as client:
                resp = client.get(url)
                return resp.text

        if step_name.startswith("playwright_"):
            return _render_via_browser(url, step_name, proxy_tier, timeout, country)

    except Exception as exc:
        logger.warning("RENDER re-fetch failed (%s): %s", step_name, exc)
    return ""


def _render_via_browser(
    url: str,
    step_name: str,
    proxy_tier: str,
    timeout: int,
    country: Optional[str],
) -> str:
    """Fetch full HTML via Playwright.

    (The former UC Chrome / seleniumbase branch was removed when the probe
    escalation was consolidated onto CloakBrowser — ``cloak_*`` captures HTML
    inline via ``_capture_html_for_render``, so it does not need this fallback.)
    """
    config = get_proxy_config()

    if step_name.startswith("playwright_"):
        from playwright.sync_api import sync_playwright

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs: dict[str, Any] = {"headless": True, "args": launch_args}
        proxy = (
            config.build_playwright_proxy(proxy_tier, country=country)
            if proxy_tier != "none"
            else None
        )
        if proxy:
            launch_kwargs["proxy"] = proxy

        pw = None
        browser = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(**launch_kwargs)
            page = browser.new_page()
            page.set_default_timeout(timeout * 1000)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(2000)
            return page.content()
        finally:
            if browser:
                browser.close()
            if pw:
                pw.stop()

    return ""


_SPA_MARKERS = [
    ('#__next', 'nextjs'),
    ('__NEXT_DATA__', 'nextjs'),
    ('#__react-root', 'react'),
    ('data-reactroot', 'react'),
    ('data-reactid', 'react'),
    ('__NUXT__', 'nuxt'),
    ('#__nuxt', 'nuxt'),
    ('ng-app', 'angular'),
    ('ng-controller', 'angular'),
    ('ng-view', 'angular'),
    ('[data-v-]', 'vue'),
    ('__VUE_APP__', 'vue'),
    ('v-app', 'vue'),
]


def _detect_spa(html: str, body_text: str) -> tuple[bool, str]:
    """Detect if HTML is an SPA shell that needs JS rendering.

    Returns (is_spa, framework_name).
    """
    snippet = html[:50000]
    for marker, framework in _SPA_MARKERS:
        if marker in snippet:
            return True, framework

    if len(body_text) < 200 and len(html) > 10000:
        return True, "unknown-large-shell"

    return False, ""


# ── Reusable browser helpers (shared by /probe and /navigate) ────────────


class _PageContext:
    """Ephemeral browser + page launched by :func:`_launch_page`.

    Holds everything the caller needs to drive the page and tear it down.
    """

    __slots__ = ("page", "browser", "pw", "stealth_used", "method")

    def __init__(self, page, browser, pw, stealth_used: bool, method: str):
        self.page = page
        self.browser = browser
        self.pw = pw  # sync_playwright() handle (Playwright only); None for cloak
        self.stealth_used = stealth_used
        self.method = method  # "playwright" | "cloak"

    def close(self) -> None:
        """Close the browser and stop the playwright driver. Idempotent."""
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.pw is not None:
            try:
                self.pw.stop()
            except Exception:
                pass
            self.pw = None


def _launch_page(
    method: str = "auto",
    proxy_tier: str = "none",
    country: Optional[str] = None,
    stealth: str = "auto",
    timeout: int = 60,
) -> _PageContext:
    """Launch an ephemeral browser + page for a single operation.

    ``method`` / ``stealth`` resolve as:
      - stealth="cloak" or method="cloak" → CloakBrowser direct launch
      - otherwise → vanilla Playwright Chromium

    Returns a :class:`_PageContext`. Caller MUST ``.close()`` it (usually in a
    ``finally``). The cloak path calls ``cloakbrowser.launch()`` directly — it
    does NOT rely on the ``.pth`` global monkeypatch.
    """
    config = get_proxy_config()

    if method == "auto":
        method = "cloak" if stealth == "cloak" else "playwright"

    if method not in ("playwright", "cloak"):
        raise ValueError(
            f"_launch_page unsupported method: {method!r} (use 'playwright' or 'cloak')"
        )

    if method == "cloak":
        from cloakbrowser import launch as cloak_launch

        launch_kwargs: dict[str, Any] = {"headless": True}
        proxy = (
            config.build_proxy_url(proxy_tier, country=country)
            if proxy_tier != "none"
            else None
        )
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = cloak_launch(**launch_kwargs)
        page = browser.new_page()
        page.set_default_timeout(timeout * 1000)
        return _PageContext(page, browser, pw=None, stealth_used=True, method="cloak")

    # default: vanilla Playwright
    from playwright.sync_api import sync_playwright

    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    launch_kwargs = {"headless": True, "args": launch_args}
    proxy = (
        config.build_playwright_proxy(proxy_tier, country=country)
        if proxy_tier != "none"
        else None
    )
    if proxy:
        launch_kwargs["proxy"] = proxy
    pw = sync_playwright().start()
    browser = pw.chromium.launch(**launch_kwargs)
    page = browser.new_page()
    page.set_default_timeout(timeout * 1000)
    return _PageContext(page, browser, pw=pw, stealth_used=False, method="playwright")


def _extract_page_data(
    page,
    extract_selectors: Optional[dict[str, str]] = None,
    return_what: str = "all",
    max_html: int = 2_000_000,
) -> dict[str, Any]:
    """Extract HTML and/or selector data from a Playwright ``page``.

    Always captures ``page.content()`` (needed for block classification); the
    full HTML is only included in the returned dict when ``return_what`` asks
    for it — the caller decides whether to ship it in the HTTP response.

    ``return_what``: ``"all"`` | ``"html"`` | ``"data"`` | ``"none"``.
    ``extract_selectors``: ``{name: css_selector}`` → collected as
    ``{name: [{"text": str, "href": str}, ...]}``.

    Returns ``{"html", "html_truncated", "data"}``.

    ``html`` is ALWAYS populated (needed for block classification downstream).
    The caller decides whether to include it in its HTTP response based on
    ``return_what`` (filter when it is not in ``("all", "html")``).
    """
    try:
        html = page.content()
    except Exception as exc:
        logger.info("extract page.content() failed: %s", exc)
        html = ""

    truncated = len(html) > max_html
    html_out = html[:max_html] if truncated else html

    result: dict[str, Any] = {
        "html": html_out,
        "html_truncated": truncated,
        "data": {},
    }

    if return_what in ("all", "data") and extract_selectors:
        data: dict[str, list[dict[str, str]]] = {}
        for name, selector in extract_selectors.items():
            items: list[dict[str, str]] = []
            try:
                elements = page.query_selector_all(selector)
                for el in elements:
                    text = ""
                    try:
                        text = (el.inner_text() or "").strip()
                    except Exception:
                        try:
                            text = (el.text_content() or "").strip()
                        except Exception:
                            pass
                    href = ""
                    try:
                        href = el.get_attribute("href") or ""
                    except Exception:
                        pass
                    items.append({"text": text, "href": href})
            except Exception as exc:
                logger.debug("selector %r (%s) failed: %s", name, selector, exc)
            data[name] = items
        result["data"] = data

    return result


def _classify_block(html: str, status_code: int = 0) -> Optional[str]:
    """Classify a page's block state.

    Returns one of ``"antibot"``, ``"captcha"``, ``"empty"``, or ``None`` (page
    looks OK). Reuses :func:`src.page_analysis.is_blocked` for keyword matching.
    """
    if not html or len(html) < 200:
        return "empty"
    snippet = html[:5000]
    if is_blocked(snippet):
        lower = snippet.lower()
        captcha_markers = (
            "cf-browser-verification",
            "please complete the security check",
            "are you a robot",
            "please verify you are a human",
            "checking your browser",
            "just a moment",
            "captcha",
        )
        if any(m in lower for m in captcha_markers):
            return "captcha"
        return "antibot"
    if status_code in (401, 403, 429, 503):
        return "antibot"
    return None


def _try_direct_http(url: str, timeout: int = 15, proxy_tier: str = "none", country: Optional[str] = None) -> Optional[dict]:
    try:
        import httpx

        from src.page_analysis import get_user_agent

        config = get_proxy_config()
        proxy_url = config.build_proxy_url(proxy_tier, country=country) if proxy_tier != "none" else None

        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy_url,
            headers={"User-Agent": get_user_agent()},
        ) as client:
            resp = client.get(url)
        html = resp.text
        _capture_html_for_render(html)
        blocked = is_blocked(html[:5000])
        jsonld = extract_jsonld(html)
        meta = extract_meta_tags(html)
        title = extract_title(html)

        body_text = ""
        if len(html) > 500:
            import re as _re
            match = _re.search(r"<body[^>]*>(.*?)</body>", html, _re.DOTALL | _re.IGNORECASE)
            if match:
                raw = match.group(1)
                text = _re.sub(r"<[^>]+>", " ", raw)
                body_text = _re.sub(r"\s+", " ", text).strip()[:1500]

        if proxy_tier == "none" and _detect_akamai(html, resp.status_code):
            return {
                "success": False,
                "method": "direct_http",
                "proxy_tier": "none",
                "status_code": resp.status_code,
                "title": title,
                "body_length": len(html),
                "needs_browser": True,
                "blocked": True,
                "needs_akamai_bypass": True,
                "jsonld": jsonld,
                "meta": meta,
                "selector_results": "Skipped — Akamai detected",
                "error": "Akamai Bot Manager detected",
            }

        has_meaningful_content = len(html) > 2000 and not blocked
        has_price_in_jsonld = any(has_price(block) for block in jsonld)

        spa_detected, spa_framework = _detect_spa(html, body_text)

        method_name = f"direct_http_{proxy_tier}" if proxy_tier != "none" else "direct_http"

        if has_meaningful_content:
            selector_results = "Skipped — direct HTTP"
            needs_browser = not has_price_in_jsonld and len(jsonld) == 0

            if spa_detected:
                needs_browser = True
                selector_results = f"SPA detected ({spa_framework}) — JS rendering required"

            return {
                "success": True,
                "method": method_name,
                "proxy_tier": proxy_tier,
                "status_code": resp.status_code,
                "title": title,
                "body_length": len(html),
                "body_text": body_text,
                "needs_browser": needs_browser,
                "blocked": False,
                "jsonld": jsonld,
                "meta": meta,
                "selector_results": selector_results,
                "error": "",
                "spa_detected": spa_detected,
                "spa_framework": spa_framework,
            }

        return None

    except Exception as exc:
        logger.info("Direct HTTP (%s) failed: %s", proxy_tier, exc)
        return None


def _try_playwright(url: str, proxy_tier: str, timeout: int = 25, country: Optional[str] = None) -> Optional[dict]:
    ctx = None
    try:
        ctx = _launch_page(method="playwright", proxy_tier=proxy_tier, country=country, stealth="none", timeout=timeout)
        page = ctx.page

        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2000)

        html = page.content()
        _capture_html_for_render(html)
        title = page.title() or ""
        blocked = is_blocked(html[:5000])
        jsonld = extract_jsonld(html)
        meta = extract_meta_tags(html)
        status_code = resp.status if resp else 0

        body_text = ""
        try:
            body_text = page.evaluate("() => document.body?.innerText?.substring(0, 1500) || ''")[:1500]
        except Exception:
            pass

        has_content = len(html) > 2000 and not blocked
        if has_content:
            from src.page_analysis import run_selector_tests

            selector_results = run_selector_tests(page)
            return {
                "success": True,
                "method": f"playwright_{proxy_tier}",
                "proxy_tier": proxy_tier,
                "status_code": status_code,
                "title": title[:200],
                "body_length": len(html),
                "body_text": body_text,
                "needs_browser": True,
                "blocked": False,
                "jsonld": jsonld,
                "meta": meta,
                "selector_results": selector_results,
                "error": "",
            }

        return {
            "success": False,
            "method": f"playwright_{proxy_tier}",
            "proxy_tier": proxy_tier,
            "status_code": status_code,
            "title": title[:200],
            "body_length": len(html),
            "body_text": body_text,
            "needs_browser": True,
            "blocked": True,
            "needs_akamai_bypass": _detect_akamai(html, status_code),
            "jsonld": jsonld,
            "meta": meta,
            "selector_results": "Skipped — page blocked or empty",
            "error": "Page blocked or empty content",
        }

    except Exception as exc:
        logger.info("Playwright (%s) failed: %s", proxy_tier, exc)
        return None
    finally:
        if ctx:
            ctx.close()


def _try_cloak(url: str, proxy_tier: str, timeout: int = 40, country: Optional[str] = None) -> Optional[dict]:
    """Probe with CloakBrowser — a stealth Chromium with C++-level fingerprint
    patches that defeats Akamai/anti-bot where vanilla Playwright and UC mode fail.
    Mirror of ``_try_playwright`` but drives cloak's stealth binary directly
    via :func:`_launch_page` (Option 2 — NOT the ``.pth`` monkeypatch)."""
    ctx = None
    try:
        ctx = _launch_page(method="cloak", proxy_tier=proxy_tier, country=country, stealth="cloak", timeout=timeout)
        page = ctx.page

        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2000)

        html = page.content()
        _capture_html_for_render(html)
        title = page.title() or ""
        blocked = is_blocked(html[:5000])
        jsonld = extract_jsonld(html)
        meta = extract_meta_tags(html)
        status_code = resp.status if resp else 0
        body_text = ""
        try:
            body_text = page.evaluate("() => document.body?.innerText?.substring(0, 1500) || ''")[:1500]
        except Exception:
            pass

        has_content = len(html) > 2000 and not blocked
        if has_content:
            from src.page_analysis import run_selector_tests

            return {
                "success": True,
                "method": f"cloak_{proxy_tier}",
                "proxy_tier": proxy_tier,
                "status_code": status_code,
                "title": title[:200],
                "body_length": len(html),
                "body_text": body_text,
                "needs_browser": True,
                "blocked": False,
                "needs_stealth": True,
                "jsonld": jsonld,
                "meta": meta,
                "selector_results": run_selector_tests(page),
                "error": "",
            }
        return {
            "success": False,
            "method": f"cloak_{proxy_tier}",
            "proxy_tier": proxy_tier,
            "status_code": status_code,
            "title": title[:200],
            "body_length": len(html),
            "body_text": body_text,
            "needs_browser": True,
            "blocked": True,
            "needs_stealth": True,
            "needs_akamai_bypass": _detect_akamai(html, status_code),
            "jsonld": jsonld,
            "meta": meta,
            "selector_results": "Skipped — page blocked or empty",
            "error": "Page blocked or empty content",
        }

    except Exception as exc:
        logger.info("Cloak (%s) failed: %s", proxy_tier, exc)
        return None
    finally:
        if ctx:
            ctx.close()


def _detect_akamai(html: str, status_code: int = 0) -> bool:
    lower = html[:5000].lower()
    signals = [
        "sec-if-cpt-container",
        "sec-cpt-if",
        "akamai_beacon",
        "sensor_data",
        "/akam/",
    ]
    if any(s in lower for s in signals):
        return True
    if status_code == 403 and len(html) < 5000:
        text = lower[:2000]
        if any(kw in text for kw in ["access denied", "blocked", "forbidden", "reference #"]):
            return True
    return False


def _failure_result(method: str, proxy_tier: str, error: str) -> dict[str, Any]:
    return {
        "success": False,
        "method": method,
        "proxy_tier": proxy_tier,
        "status_code": 0,
        "title": "",
        "body_length": 0,
        "needs_browser": True,
        "blocked": True,
        "jsonld": [],
        "meta": {},
        "selector_results": {},
        "error": error,
    }
