import json
import logging
import os
import signal
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

# ── curl_cffi fingerprint sub-ladder (T3.8) ───────────────────────────────
# ``fingerprint_*`` is an HTTP-CLIENT tier (browser TLS/HTTP2 + header-order
# impersonation), NOT a browser: no JS runs, so an SPA shell still needs a
# browser step afterwards. It is spliced into every proxy tier right after
# that tier's plain-HTTP step and before its browser launches — a curl
# handshake costs ~1-2s where a Chrome launch costs 15-45s, and a site that
# 403s httpx on TLS fingerprint alone is passed without launching anything.
#
# Method names follow the existing ``{mechanism}_{tier}`` convention used by
# ``playwright_*``/``cloak_*``, so the full set is:
#   fingerprint_chrome_none / fingerprint_chrome_datacenter /
#   fingerprint_chrome_residential / fingerprint_safari184_none /
#   fingerprint_safari184_datacenter / fingerprint_safari184_residential
# All start with the stable ``fingerprint_`` prefix (HTTP-flavoured — this is
# deliberately NOT in the stealth/browser method prefix sets).
#
# Profile SUB-LADDER, empirically chosen (C5 spike): "chrome" and "safari184"
# passed sephora.de (200, ~855KB) where "chrome136"/"firefox133" were
# 403-blocked, so those two are deliberately absent. "chrome" is curl_cffi's
# alias for the newest Chrome fingerprint in the PINNED library version —
# which is why requirements.txt pins ``curl_cffi==0.16.2`` (an unpinned bump
# would silently change the TLS fingerprint this rung depends on).
FINGERPRINT_PROFILES = ("chrome", "safari184")
FINGERPRINT_METHOD_PREFIX = "fingerprint_"

# "0" removes the rungs entirely and restores the pre-T3.8 ladder byte for
# byte; anything else (including unset) keeps them. Fail-escalating: the
# rungs only execute when every step above them already failed.
FINGERPRINT_STEP_ENV = "SCRAPER_PROBE_FINGERPRINT_STEP"


def fingerprint_step_enabled() -> bool:
    return os.environ.get(FINGERPRINT_STEP_ENV, "1").strip().lower() != "0"


def _fingerprint_steps_for_tier(proxy_tier: str) -> list[tuple[str, str]]:
    return [
        (f"{FINGERPRINT_METHOD_PREFIX}{profile}_{proxy_tier}", proxy_tier)
        for profile in FINGERPRINT_PROFILES
    ]


def active_escalation_steps() -> list[tuple[str, str]]:
    """The escalation ladder actually run: ``ESCALATION_STEPS`` with the
    fingerprint rungs spliced in after each tier's plain-HTTP step (or the
    unmodified base ladder when ``SCRAPER_PROBE_FINGERPRINT_STEP=0``)."""
    if not fingerprint_step_enabled():
        return list(ESCALATION_STEPS)
    steps: list[tuple[str, str]] = []
    for step_name, proxy_tier in ESCALATION_STEPS:
        steps.append((step_name, proxy_tier))
        if step_name.startswith("direct_http"):
            steps.extend(_fingerprint_steps_for_tier(proxy_tier))
    return steps


def parse_fingerprint_method(method_name: str) -> Optional[tuple[str, str]]:
    """Split a fingerprint method name into ``(profile, proxy_tier)``.

    ``fingerprint_safari184_datacenter`` → ``("safari184", "datacenter")``.
    Returns None for anything that is not a fingerprint step, so callers can
    use it as the dispatch test.
    """
    if not method_name.startswith(FINGERPRINT_METHOD_PREFIX):
        return None
    profile, sep, proxy_tier = method_name[len(FINGERPRINT_METHOD_PREFIX):].rpartition("_")
    if not sep or profile not in FINGERPRINT_PROFILES or proxy_tier not in PROXY_TIERS:
        return None
    return profile, proxy_tier


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
    fingerprint = parse_fingerprint_method(method_name)
    if fingerprint:
        profile, tier = fingerprint
        return _try_fingerprint(
            url, tier, profile=profile, timeout=min(timeout, 20), country=country
        )
    if method_name.startswith("cloak_"):
        tier = method_name.replace("cloak_", "")
        return _try_cloak(url, tier, timeout=min(timeout, 40), country=country)
    if method_name.startswith("playwright_"):
        tier = method_name.replace("playwright_", "")
        pw_timeout = 35 if tier != "none" else 25
        return _try_playwright(url, tier, timeout=min(timeout, pw_timeout), country=country)
    return None


def run_probe(
    url: str,
    render_js: bool = True,
    timeout: int = 120,
    start_method: Optional[str] = None,
    country: Optional[str] = None,
    proxy_tier: Optional[str] = None,
) -> dict[str, Any]:
    steps_log = []
    debug_path = "/tmp/probe_debug.json"

    def _log_step(msg):
        steps_log.append(msg)
        logger.info("PROBE [%s]: %s", url[:80], msg)

    _log_step(
        f"Starting probe: render_js={render_js}, timeout={timeout}, "
        f"start_method={start_method}, proxy_tier={proxy_tier}, "
        f"fingerprint_step={fingerprint_step_enabled()}"
    )

    if country is None:
        country = detect_country(url)
        if country:
            _log_step(f"Auto-detected country: {country}")

    # Optional tier restriction from the /probe payload: run only the rungs
    # that belong to this proxy tier (caller hint, default = whole ladder).
    escalation_steps = active_escalation_steps()
    if proxy_tier:
        escalation_steps = [s for s in escalation_steps if s[1] == proxy_tier]
        _log_step(f"Proxy tier filter: {len(escalation_steps)} step(s) at tier '{proxy_tier}'")

    start_method = _resolve_start_method(start_method)
    skip_index = 0
    if start_method:
        for i, (step_name, _) in enumerate(escalation_steps):
            if step_name == start_method:
                skip_index = i
                _log_step(f"Cache hint: starting at step {i} ({step_name})")
                break

    if not render_js:
        result = _try_direct_http(url, timeout=timeout)
        if result and result.get("success"):
            return result
        return result or _failure_result("all_failed", "none", "Direct HTTP failed and render_js=false")

    last_blocked: Optional[dict] = None  # last Akamai-blocked result (returned if nothing bypasses)
    for i, (step_name, step_proxy_tier) in enumerate(escalation_steps):
        if i < skip_index:
            continue

        # Skip unconfigured proxy tiers (default deployment has no proxies —
        # datacenter/residential steps would launch through NO proxy, identical
        # to the 'none' tier already tried, wasting 30-45s each).
        if not _proxy_tier_configured(step_proxy_tier):
            _log_step(f"{step_name}: skipped (proxy tier '{step_proxy_tier}' not configured)")
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
            # [wave-15 3.1] Bypass with the DETECTING rung's tier — this used
            # to hard-code "none", so a datacenter/residential detection sent
            # the bypass out unproxied (different egress, usually still
            # blocked). And when the bypass fails, CONTINUE the ladder: a
            # proxied deployment still has its remaining tiers to try.
            bypass_method = f"cloak_{step_proxy_tier}"
            _log_step(f"{step_name}: Akamai detected — trying {bypass_method} stealth bypass")
            cloak_res = _try_cloak(
                url, step_proxy_tier, timeout=min(timeout, 40), country=country
            )
            if cloak_res and cloak_res.get("success"):
                _log_step(f"{bypass_method}: SUCCEEDED (Akamai bypassed)")
                return cloak_res
            last_blocked = result
            _log_step(f"{step_name}: {bypass_method} did not bypass Akamai; continuing escalation")
            continue

        if result and result.get("success"):
            _log_step(f"{step_name}: SUCCEEDED")
            return result

    _log_step("ALL FAILED")
    try:
        with open(debug_path, "w") as f:
            json.dump({"url": url, "steps": steps_log}, f, indent=2)
    except Exception:
        pass
    # Preserve the Akamai signal: a rung that detected Akamai is a different
    # diagnosis (and a different bypass prescription) than a generic failure.
    if last_blocked is not None:
        return last_blocked
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
    proxy_tier: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch a page and return the full HTML using the correct access method.

    Uses the same escalation chain as ``run_probe`` but returns the raw HTML
    content (truncated to ``MAX_RENDER_HTML`` chars).  This is used by agents
    that need the full page DOM (e.g. navigation_explore for extracting
    category links, search forms, product cards).

    Returns a dict with: ``success``, ``html``, ``status_code``, ``method``,
    ``title``, ``proxy_tier``, ``error``.

    ``proxy_tier`` optionally restricts the escalation to one tier (same
    meaning as :func:`run_probe`'s).
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
    escalation_steps = active_escalation_steps()
    if proxy_tier:
        escalation_steps = [s for s in escalation_steps if s[1] == proxy_tier]
        logger.info(
            "RENDER [%s]: proxy tier filter '%s' -> %d step(s)",
            url[:80], proxy_tier, len(escalation_steps),
        )
    skip_index = 0
    if start_method:
        for i, (step_name, _) in enumerate(escalation_steps):
            if step_name == start_method:
                skip_index = i
                logger.info(
                    "RENDER [%s]: cache hint starting at %s", url[:80], step_name
                )
                break

    for i, (step_name, step_proxy_tier) in enumerate(escalation_steps):
        if i < skip_index:
            continue

        # Skip unconfigured proxy tiers (default deployment has no proxies —
        # datacenter/residential steps would launch through NO proxy, identical
        # to the 'none' tier already tried, wasting 30-45s each).
        if not _proxy_tier_configured(step_proxy_tier):
            logger.info(
                "RENDER [%s]: skipping %s (proxy tier '%s' not configured)",
                url[:80], step_name, step_proxy_tier,
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
                html = _refetch_html(url, step_name, step_proxy_tier, timeout, country)
            return {
                "success": True,
                "html": html[:MAX_RENDER_HTML],
                "status_code": result.get("status_code", 200),
                "method": result.get("method", step_name),
                "title": result.get("title", ""),
                "proxy_tier": step_proxy_tier,
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

        fingerprint = parse_fingerprint_method(step_name)
        if fingerprint:
            profile, tier = fingerprint
            return _render_via_fingerprint(
                url, tier, profile=profile, timeout=min(timeout, 20), country=country
            )

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


def _proc_child_pids(pid: int) -> set[int]:
    """Direct children of pid via /proc (Linux). Empty on any failure.

    Local copy of server.py's ``_proc_children`` reader — probe.py must not
    import server.py (it owns the FastAPI app); the reader is 10 lines.
    """
    kids: set[int] = set()
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            try:
                with open(f"/proc/{pid}/task/{tid}/children") as fh:
                    kids.update(int(x) for x in fh.read().split())
            except OSError:
                continue
    except OSError:
        pass
    return kids


def _hard_kill_tree(root_pid: int, _list_children=_proc_child_pids) -> int:
    """SIGKILL ``root_pid`` and every descendant (via /proc children BFS).

    Returns the number of processes signalled. Depth-bounded (Chrome trees are
    shallow); per-PID kills only — never a process group, because the
    playwright driver is spawned WITHOUT its own session and shares uvicorn's
    group (killpg there would suicide the service). Anything that escapes
    (race, vanished /proc entry) is the orphan killer's job.
    """
    signalled = 0
    seen: set[int] = set()
    frontier = [root_pid]
    for _ in range(32):
        if not frontier:
            break
        nxt: list[int] = []
        for pid in frontier:
            if pid in seen:
                continue
            seen.add(pid)
            try:
                os.kill(pid, signal.SIGKILL)
                signalled += 1
            except (ProcessLookupError, PermissionError):
                pass
            nxt.extend(_list_children(pid))
        frontier = nxt
    return signalled


def _playwright_driver_pid(obj) -> Optional[int]:
    """Best-effort PID of the playwright node driver behind a Playwright
    handle or a sync Browser. Private-API shapes are guarded — returns None
    if the layout ever changes (the leak then reverts to the orphan-killer
    backstop instead of killing the wrong process)."""
    for chain in (
        lambda o: o._transport,  # Playwright handle (sync_playwright().start())
        lambda o: o._impl_obj._connection._transport,  # sync Browser
        lambda o: o._connection._transport,  # impl Browser
    ):
        try:
            return chain(obj)._proc.pid
        except AttributeError:
            continue
    return None


def _hard_kill_partial_launch(browser=None, pw=None) -> None:
    """W1: hard-kill the half-built resources of a failed ``_launch_page``.

    Without this, a launch that raises between driver-start and page-ready
    leaks the node driver AND its whole Chrome tree (prod: failed launches
    under memory pressure deepened the pressure → next launch failed → doom
    loop). The caller's ``finally`` closes a still-None context and can't help.

    SIGKILL, never a graceful ``browser.close()``/``pw.stop()``: launch
    failures happen under exactly the memory pressure this guards against,
    where the driver may be unresponsive — a blocking close would hold a
    NAVIGATE executor slot (three such hangs wedge all three threads and
    callers see 408s instead of 429s). The tree is being discarded; kill it.
    """
    roots: list[tuple[str, int]] = []
    if pw is not None:
        pid = _playwright_driver_pid(pw)
        if pid:
            roots.append(("playwright driver", pid))
    elif browser is not None:  # cloak path: no pw handle; driver behind browser
        pid = _playwright_driver_pid(browser)
        if pid:
            roots.append(("cloak browser driver", pid))
    if not roots:
        logger.warning(
            "launch failed and no driver PID could be attributed — leaving "
            "cleanup to the orphan killer"
        )
        return
    for label, pid in roots:
        killed = _hard_kill_tree(pid)
        logger.warning(
            "launch failed: SIGKILLed %d process(es) under %s (PID %d)",
            killed,
            label,
            pid,
        )


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

    Both launch sequences are wrapped: a failure between driver-start and
    page-ready hard-kills the partial driver/Chrome tree before re-raising
    (see :func:`_hard_kill_partial_launch`).
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
        browser = None
        try:
            browser = cloak_launch(**launch_kwargs)
            page = browser.new_page()
            page.set_default_timeout(timeout * 1000)
        except Exception:
            _hard_kill_partial_launch(browser=browser)
            raise
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
    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.set_default_timeout(timeout * 1000)
    except Exception:
        _hard_kill_partial_launch(browser=browser, pw=pw)
        raise
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

        body_text = _extract_body_text(html)

        # [wave-15 3.1] Akamai detection on EVERY tier — the none-only gate
        # meant a datacenter/residential rung that got Akamai-blocked was
        # misreported as a generic failure, so the bypass never fired and the
        # caller never learned the block was Akamai.
        if _detect_akamai(html, resp.status_code):
            method_name = f"direct_http_{proxy_tier}" if proxy_tier != "none" else "direct_http"
            return {
                "success": False,
                "method": method_name,
                "proxy_tier": proxy_tier,
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


def _fingerprint_session(
    profile: str,
    proxy_url: Optional[str],
    timeout: int,
):
    """Build a curl_cffi session impersonating ``profile``.

    Split out so both the probe step and the render re-fetch share one
    construction site (and one place to learn about a curl_cffi API change).
    Import is inside the function: curl_cffi is a browser-image dependency and
    probe.py must stay importable where it is not installed.
    """
    from curl_cffi import requests as curl_requests

    # NOTE: no explicit User-Agent here. The impersonated TLS handshake is only
    # coherent with curl_cffi's own matching header set (``default_headers``);
    # stamping src.page_analysis.get_user_agent() on top would pair one
    # browser's JA3 with another's UA — the mismatch this rung exists to avoid.
    kwargs: dict[str, Any] = {"impersonate": profile, "timeout": timeout}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return curl_requests.Session(**kwargs)


def _try_fingerprint(
    url: str,
    proxy_tier: str = "none",
    profile: str = "chrome",
    timeout: int = 20,
    country: Optional[str] = None,
) -> Optional[dict]:
    """Probe via curl_cffi browser-TLS impersonation (HTTP client, NOT a browser).

    Sits after the plain-HTTP step in each proxy tier (see
    :func:`active_escalation_steps`). Deliberately a *profile sub-ladder*
    rather than a single impersonate value — a fresh Session per profile, each
    attempt individually guarded, so one raising profile (unknown alias, TLS
    negotiation failure) returns instead of killing the step.

    Success uses the same bar as the neighbouring steps: a real status plus
    meaningful content (:func:`_classify_block` + the 2000-byte floor). A 200
    whose body is a tiny challenge page is a FAIL here just as it is for
    ``direct_http``.

    Returns ``None`` only when the library itself is unavailable (the rung
    then costs nothing); a fetched-but-rejected page returns
    ``success=False`` so the escalation log records why the rung failed.
    """
    method_name = f"{FINGERPRINT_METHOD_PREFIX}{profile}_{proxy_tier}"
    try:
        config = get_proxy_config()
        proxy_url = (
            config.build_proxy_url(proxy_tier, country=country)
            if proxy_tier != "none"
            else None
        )
    except Exception as exc:
        logger.info("Fingerprint (%s) setup failed: %s", method_name, exc)
        return None

    # Fresh Session per attempt: curl_cffi keeps connection + cookie state on
    # the session, and a profile that was just challenged must not hand that
    # state to the next profile.
    try:
        with _fingerprint_session(profile, proxy_url, timeout) as session:
            resp = session.get(url, timeout=timeout)
    except Exception as exc:
        logger.info("Fingerprint (%s) failed: %s", method_name, exc)
        return None

    status_code = resp.status_code
    html = resp.text or ""
    _capture_html_for_render(html)
    blocked = is_blocked(html[:5000])
    jsonld = extract_jsonld(html)
    meta = extract_meta_tags(html)
    title = extract_title(html)
    body_text = _extract_body_text(html)

    block_class = _classify_block(html, status_code)
    has_content = len(html) > 2000 and block_class is None

    if has_content:
        has_price_in_jsonld = any(has_price(block) for block in jsonld)
        needs_browser = not has_price_in_jsonld and len(jsonld) == 0
        spa_detected, spa_framework = _detect_spa(html, body_text)
        if spa_detected:
            needs_browser = True
            selector_results = f"SPA detected ({spa_framework}) — JS rendering required"
        else:
            selector_results = "Skipped — fingerprint HTTP"

        return {
            "success": True,
            "method": method_name,
            "proxy_tier": proxy_tier,
            "status_code": status_code,
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
            "fingerprint_profile": profile,
        }

    logger.info(
        "Fingerprint (%s) rejected: status=%s bytes=%d block=%s/%s",
        method_name, status_code, len(html), block_class, blocked,
    )
    return {
        "success": False,
        "method": method_name,
        "proxy_tier": proxy_tier,
        "status_code": status_code,
        "title": title,
        "body_length": len(html),
        "body_text": body_text,
        "needs_browser": True,
        "blocked": True,
        # Deliberately NOT ``needs_akamai_bypass``: this is an early, cheap
        # rung — tripping the cloak bypass here would also stop the ladder
        # before the (untried) datacenter/residential tiers.
        "jsonld": jsonld,
        "meta": meta,
        "selector_results": "Skipped — page blocked, empty or challenge-shaped",
        "error": f"fingerprint fetch rejected (block={block_class or 'unknown'})",
        "fingerprint_profile": profile,
    }


def _extract_body_text(html: str, limit: int = 1500) -> str:
    """Visible text of ``<body>`` (first ``limit`` chars), '' if unavailable.

    Shared by the HTTP-flavoured probe steps so their ``body_text`` payload
    (which feeds the downstream captcha/LLM checks) has one implementation.
    """
    if len(html) <= 500:
        return ""
    import re as _re

    match = _re.search(r"<body[^>]*>(.*?)</body>", html, _re.DOTALL | _re.IGNORECASE)
    if not match:
        return ""
    text = _re.sub(r"<[^>]+>", " ", match.group(1))
    return _re.sub(r"\s+", " ", text).strip()[:limit]


def _render_via_fingerprint(
    url: str,
    proxy_tier: str,
    profile: str = "chrome",
    timeout: int = 20,
    country: Optional[str] = None,
) -> str:
    """Full-HTML re-fetch for a ``fingerprint_*`` step (render_page fallback).

    ``_try_fingerprint`` already captures the HTML inline, so this normally
    never runs — it exists so a caller that reaches :func:`_refetch_html`
    with a fingerprint method still gets content instead of ''.
    """
    try:
        config = get_proxy_config()
        proxy_url = (
            config.build_proxy_url(proxy_tier, country=country)
            if proxy_tier != "none"
            else None
        )
        with _fingerprint_session(profile, proxy_url, timeout) as session:
            resp = session.get(url, timeout=timeout)
        return resp.text or ""
    except Exception as exc:
        logger.warning(
            "RENDER re-fetch failed (%s_%s): %s", profile, proxy_tier, exc
        )
    return ""


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
