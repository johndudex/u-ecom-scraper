"""Generic Phase-1 item-URL discovery — pagination as an opaque import.

The single function :func:`discover_item_urls` drives a listing/search/category
page through every pagination style we see in the wild and returns the complete
set of item URLs:

  * ``load_more``       — click a "Show more" / "Load more" / pager-next button
                          and re-extract on the SAME page (Coveo, Algolia,
                          React/Vue result lists). +20 items/click verified live
                          on lw.com's Coveo directory (~3000 profiles reached).
  * ``infinite_scroll`` — ``window.scrollBy(viewport)`` + re-extract. Used when
                          no load-more button is present (lazy-rendered grids).
  * ``page_param``      — construct ``?{page_param}=N`` directly (deterministic,
                          DOM-independent) and navigate. Offset-style params
                          (``offset``/``start``/``skip``/…) compute
                          ``(page-1) * items_per_page`` automatically.
  * ``next_button``     — a declared selector OR the semantic fallbacks
                          (``a[rel="next"]``, ``li.next a``, ``Next``-text).
                          Reads the href when present (no JS nav) else clicks.

Per iteration the strategies are tried in the configured order; the FIRST one
that makes progress wins and the rest are skipped. This reproduces the
``navigation_scraper.py`` "load-more-then-fall-through-to-next-page" behavior
AND the ``playwright_scraper.py`` "load-more-then-scroll" behavior from one
loop.

WHY THIS MODULE EXISTS
  ``code_writer`` (an LLM) generates scrapers by adapting templates. It does
  NOT faithfully copy the template pagination loop — it drops selectors, cuts
  ``MAX_DISCOVER_PAGES``, or replaces the loop with a hand-rolled reimplementation,
  producing 0-20 items instead of 600+. Moving discovery behind a one-line import
  removes the loop from the LLM's edit surface: the LLM emits ``import`` + one
  call, and the verified pagination logic is untouchable.

CONTRACT — separation of concerns
  * THIS module owns pagination strategy (the four primitives, dedup, stop
    conditions, coverage signals, transient-error retry).
  * THE SCRAPER owns link extraction (per-site selector from product_analysis,
    robust same-domain fallback, JSON-LD item lookup). It passes that as the
    ``extract_urls`` callback. Extraction is site-specific; pagination is generic.

This module is PURE-PYTHON: no Django, no Playwright import. The ``page``
argument is duck-typed via :class:`PageLike` (anything with ``evaluate``,
``query_selector``, ``goto``, ``wait_for_timeout``, ``.url``), so the same
helper serves Playwright templates and the browser_service runner. Mirrors the
``src/job_fields.py`` / ``src/page_analysis.py`` convention.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

__all__ = [
    "DiscoveryConfig",
    "DiscoveryResult",
    "StopReason",
    "PageLike",
    "discover_item_urls",
    "click_load_more",
    "build_page_param_url",
    "find_next_button_url",
    "DEFAULT_LOAD_MORE_SELECTORS",
    "DEFAULT_NEXT_BUTTON_SELECTORS",
]

# ─── selector sets (ported verbatim from templates/playwright_scraper.py) ────

DEFAULT_LOAD_MORE_SELECTORS: tuple[str, ...] = (
    "[class*='load-more' i]", "[class*='loadMore' i]",
    "[class*='show-more' i]", "[class*='showMore' i]",
    "[class*='pager-next' i]", "[class*='pagerNext' i]",
    "button[aria-label*='more' i]", "a[aria-label*='next' i]",
    ".coveo-magicbox-load-more",
)

DEFAULT_NEXT_BUTTON_SELECTORS: tuple[str, ...] = (
    'a[rel="next"]', "a.next", "li.next a",
    '[aria-label*="next" i]',
    'a:has-text("Next")', 'button:has-text("Next")',
)

# Offset-style query params: value = (page-1)*items_per_page, not the page no.
_OFFSET_PARAMS = frozenset({"offset", "start", "skip", "begin", "from"})


class PageLike(Protocol):
    """Structural type for the Playwright-like page object this module drives.

    Defined as a Protocol (not a concrete class) so the module has no Playwright
    import — keeping ``src/`` pure-Python per the established convention.
    """

    def evaluate(self, js: str, *args: Any) -> Any: ...
    def query_selector(self, selector: str) -> Optional[Any]: ...
    def goto(self, url: str, **kwargs: Any) -> Any: ...
    def wait_for_timeout(self, ms: int) -> None: ...
    def wait_for_load_state(self, state: str) -> None: ...
    # ``url`` is a property on Playwright Page; fall back to getattr in helpers.


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC TYPES
# ═══════════════════════════════════════════════════════════════════════════════

class StopReason(str, Enum):
    """Why the discovery loop terminated.

    Mirrors the ``stop_reason`` enum threaded through ``navigation_scraper.py``
    so the discovery-coverage gate (contract §1/§2, hypothesis H4) can tell a
    GENUINELY exhausted run from one that GAVE UP.

    ``NAVIGATE_ERROR`` is sticky: once any page failed to load (HTTP 4xx/5xx,
    timeout, exception), it is preserved even if a later phase ended cleanly —
    a single gave-up signal means the whole discovery is suspect.
    """

    NO_NEXT_LINK = "no_next_link"     # no button/param/next-link available
    NO_NEW_ITEMS = "no_new_items"     # primitive acted but yielded 0 new URLs
    MAX_PAGES_HIT = "max_pages_hit"   # hit the configured ``max_pages`` cap
    NAVIGATE_ERROR = "navigate_error" # HTTP error / timeout / exception mid-loop
    EMPTY_RENDER = "empty_render"     # initial page never rendered any items


# Per-iteration primitive outcome. Internal — the orchestrator translates these
# into ``StopReason`` / no-progress accounting.
_NOT_APPLICABLE = "not_applicable"  # no button/URL for this primitive → try next
_PROGRESSED = "progressed"          # acted AND added new URLs → iteration done
_STUCK = "stuck"                    # acted but added 0 → iteration done, count it
_NAV_ERROR = "navigate_error"       # goto failed mid-primitive → stop the loop


@dataclass
class DiscoveryConfig:
    """All knobs for ``discover_item_urls``. Sub-set & override per site.

    Defaults reproduce the verified ``playwright_scraper.py`` loop (load-more →
    scroll, 3-strikes, MAX 200). The navigation-style "load-more then page-param
    then next-button" chain is a one-liner: ``strategies=(...)`` lists the order.
    """

    # ── bounds ──
    max_pages: Optional[int] = 200       # hard cap on iterations; None = unlimited
    max_no_progress: int = 3             # consecutive 0-new rounds before stopping
    min_initial_links: int = 5           # render-wait target before pagination starts
    initial_render_polls: int = 20       # max render-wait polls (~500ms each)

    # ── which primitives to try, and in what order ──
    # Full set: "load_more", "infinite_scroll", "page_param", "next_button".
    # Per iteration, the FIRST primitive that applies wins (see module docstring).
    strategies: tuple[str, ...] = ("load_more", "infinite_scroll")

    # ── load_more + infinite_scroll ──
    load_more_selectors: tuple[str, ...] = DEFAULT_LOAD_MORE_SELECTORS
    click_wait_ms: int = 2500
    scroll_wait_ms: int = 1200

    # ── page_param ──
    page_param_name: Optional[str] = None   # e.g. "page", "offset"
    items_per_page: Optional[int] = None    # page size (offset-style params only)

    # ── next_button ──
    next_button_selector: Optional[str] = None  # declared selector; semantic
                                                # fallbacks always also tried
    # ── navigation + render ──
    site_url: Optional[str] = None         # base for resolving relative hrefs
    navigate_timeout_ms: int = 30000
    page_settle_after_nav_s: float = 8.0   # mirrors nav template's post-goto sleep
    safe_eval_retries: int = 2             # Coveo "Execution context destroyed" retries


@dataclass
class DiscoveryResult:
    """Return value of :func:`discover_item_urls`.

    ``urls`` is de-duplicated, order-preserving. The coverage fields feed the
    discovery-coverage gate (``stop_reason`` is the load-bearing signal).
    """

    urls: list[str]
    stop_reason: str = StopReason.NO_NEXT_LINK.value
    max_pages_hit: bool = False
    pages_visited: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# SMALL REUSABLE PRIMITIVES — also exported for scrapers that compose their
# own loop but want the verified clicker/URL-builder.
# ═══════════════════════════════════════════════════════════════════════════════

def click_load_more(page: Any, selectors: tuple[str, ...] = DEFAULT_LOAD_MORE_SELECTORS) -> bool:
    """Click a visible+enabled load-more/show-more/pager-next button.

    Returns True if a button was found and clicked. Uses the 9-selector set
    verified on Coveo/Algolia/React/Vue listings. Implemented via a single
    ``page.evaluate`` (Playwright) so it works regardless of which template
    owns the page.
    """
    js = """
    (sels) => {
        for (const s of sels) {
            const els = document.querySelectorAll(s);
            for (const el of els) {
                if (el.offsetParent !== null && !el.disabled
                        && !el.getAttribute('aria-disabled')) {
                    el.click();
                    return true;
                }
            }
        }
        return false;
    }
    """
    try:
        return bool(page.evaluate(js, list(selectors)))
    except Exception as exc:
        logger.warning("click_load_more: evaluate failed: %s", exc)
        return False


def build_page_param_url(current_url: str, param: str, page_num: int,
                         items_per_page: Optional[int] = None) -> str:
    """Return ``current_url`` with ``param`` REPLACED (not appended).

    Offset-style params (``offset``/``start``/``skip``/``begin``/``from``) take
    ``value = (page_num - 1) * items_per_page``; all others take ``page_num``.
    Uses urllib.parse so we never emit duplicate params (``?pg=2&pg=3``) —
    servers resolve those inconsistently and historically caused re-fetching
    page 1 forever.
    """
    p = urlparse(current_url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    if param in _OFFSET_PARAMS:
        value = (page_num - 1) * (items_per_page or 25)
    else:
        value = page_num
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


def find_next_button_url(page: Any, site_url: Optional[str] = None,
                         declared_selector: Optional[str] = None) -> Optional[str]:
    """Return the href of the first visible next-button, or None.

    Tries a declared selector first, then the semantic fallbacks
    (``a[rel="next"]``, ``li.next a``, ``Next``-text, …). Resolves relative
    hrefs against ``site_url``. Returns None when no next link is present so the
    caller can fall through to another primitive or stop.

    Note: this DOES NOT click — it only reads the href. Click-and-read-page-url
    is a destructive fallback handled inside :func:`_try_next_button`, because a
    click navigates away from the current page and cannot be undone.
    """
    sels = ((declared_selector,) if declared_selector else ()) + DEFAULT_NEXT_BUTTON_SELECTORS
    for sel in sels:
        try:
            btn = page.query_selector(sel)
            if not btn:
                continue
            href = (btn.get_attribute("href") or "").strip()
            if not href:
                continue
            if href.startswith("http"):
                return href
            if not site_url:
                return href
            return site_url.rstrip("/") + (href if href.startswith("/") else "/" + href)
        except Exception:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL — render-wait, safe-eval, goto
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_eval(page: Any, js: str, retries: int) -> Any:
    """``page.evaluate`` wrapped against the Coveo/React "Execution context was
    destroyed" transient. Client-side routing can swap the JS context mid-eval;
    we back off and retry rather than killing the whole discovery loop.
    """
    for att in range(retries + 1):
        try:
            return page.evaluate(js) or []
        except Exception as exc:
            if "destroyed" in str(exc).lower() and att < retries:
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                continue
            return []
    return []


def _safe_extract(page: Any, extract_urls: Callable[[Any], list[str]],
                  retries: int) -> list[str]:
    """Run the scraper's extraction callback, defending against Playwright
    eval crashes. The callback itself may call ``page.evaluate``; we catch all
    exceptions so one bad render never aborts pagination.
    """
    try:
        out = extract_urls(page)
        return list(out or [])
    except Exception as exc:
        if "destroyed" in str(exc).lower() and retries > 0:
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            try:
                return list(extract_urls(page) or [])
            except Exception:
                return []
        logger.debug("extract_urls raised: %s", exc)
        return []


def _merge_new(accumulated: list[str], seen: set[str], new_urls: list[str]) -> int:
    """Append ``new_urls`` not already in ``seen`` to ``accumulated`` (preserving
    order). Returns the number appended. Mutates both args.
    """
    added = 0
    for u in new_urls:
        if u and u not in seen:
            seen.add(u)
            accumulated.append(u)
            added += 1
    return added


def _discovery_goto(page: Any, url: str, cfg: DiscoveryConfig,
                    state: dict) -> bool:
    """Phase-1 page fetch used by the page_param / next_button primitives.

    Returns True on success; False on navigate failure (HTTP 4xx/5xx, 429,
    timeout, exception). On failure stamps ``NAVIGATE_ERROR`` (sticky — H4) so
    the coverage gate FAILs rather than treating a gave-up run as exhaustive.
    """
    try:
        response = page.goto(url, timeout=cfg.navigate_timeout_ms)
    except Exception as exc:
        logger.warning("discovery: navigate FAILED %s: %s", url[:80], exc)
        _set_stop(state, StopReason.NAVIGATE_ERROR)
        return False
    status = getattr(response, "status", 0) or 0
    if status in (429, 502, 503) or status >= 500:
        logger.warning("discovery: HTTP %d on %s (rate-limit/block?)", status, url[:80])
        _set_stop(state, StopReason.NAVIGATE_ERROR)
        return False
    try:
        page.wait_for_load_state("domcontentloaded")
        # Mirror the nav template's settle sleep — JS listings mount links after
        # DOMContentLoaded; without it the first extraction returns the prior page.
        import time as _time
        _time.sleep(cfg.page_settle_after_nav_s)
    except Exception as exc:
        logger.warning("discovery: load-wait failed %s: %s", url[:80], exc)
        _set_stop(state, StopReason.NAVIGATE_ERROR)
        return False
    return True


def _set_stop(state: dict, reason: StopReason) -> None:
    """Record the stop reason. ``NAVIGATE_ERROR`` is sticky (H4)."""
    if state["stop_reason"] == StopReason.NAVIGATE_ERROR.value and reason != StopReason.NAVIGATE_ERROR:
        return
    state["stop_reason"] = reason.value


def _wait_for_render(page: Any, extract_urls: Callable[[Any], list[str]],
                     cfg: DiscoveryConfig) -> list[str]:
    """Poll until the first page of results mounts.

    JS-rendered listings (Coveo, React, Vue) mount result links AFTER
    ``networkidle`` fires — the first extraction can be empty or partial. Poll
    until ``len >= min_initial_links`` OR the count is stable for 2 consecutive
    checks (render done).
    """
    urls: list[str] = []
    prev = 0
    stable = 0
    for _ in range(cfg.initial_render_polls):
        urls = _safe_extract(page, extract_urls, cfg.safe_eval_retries)
        if len(urls) >= cfg.min_initial_links:
            break
        if urls and len(urls) == prev:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        prev = len(urls)
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
    return urls


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL — primitive handlers (one per pagination style)
# ═══════════════════════════════════════════════════════════════════════════════

def _try_load_more(page: Any, cfg: DiscoveryConfig,
                   extract_urls: Callable[[Any], list[str]],
                   seen: set[str], accumulated: list[str]) -> str:
    if not click_load_more(page, cfg.load_more_selectors):
        return _NOT_APPLICABLE
    try:
        page.wait_for_timeout(cfg.click_wait_ms)
    except Exception:
        pass
    added = _merge_new(accumulated, seen, _safe_extract(page, extract_urls, cfg.safe_eval_retries))
    return _PROGRESSED if added else _STUCK


_SCROLL_CONTAINER_JS = """
() => {
    // Find the largest scrollable sub-element — the results pane. Virtualized
    // lists (Coveo, React, Algolia) bind lazy-load to THIS container's scroll
    // event, NOT the window's. Scrolling the window there is a silent no-op, so
    // discovery captures the first page and then plateaus ("no_new_items").
    // Scrolling the container fires its lazy-load callback and yields more items.
    //
    // PERF: a listing page can hold 10k-50k DOM nodes once items load.
    // getComputedStyle() forces style recalc, so calling it on every element
    // costs ~150ms+ per scroll and grows with the page. We pre-filter with the
    // CHEAP layout properties (clientHeight/scrollHeight — no recalc) and only
    // pay for getComputedStyle on the handful of survivors. The survivor set is
    // identical to a getComputedStyle-first scan (all conditions must pass).
    let best = null, bestScore = 0;
    for (const el of document.querySelectorAll('*')) {
        const ch = el.clientHeight;
        if (ch < 200) continue;                                 // too small to be the results pane
        if (el.scrollHeight - ch < 80) continue;                // nothing to scroll
        const st = getComputedStyle(el);                        // cheap: few survivors reach here
        if (st.overflowY !== 'auto' && st.overflowY !== 'scroll') continue;
        if (st.visibility === 'hidden' || st.display === 'none') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 200 || r.height < 200) continue;          // not visible / not laid out
        if (ch > bestScore) { bestScore = ch; best = el; }
    }
    const isWindow = !best;
    const target = best || document.scrollingElement || document.documentElement;
    const viewH = isWindow ? window.innerHeight : best.clientHeight;
    target.scrollBy(0, Math.max(viewH * 0.9, 1));
    // If within one viewport of the bottom, jump to absolute bottom so any
    // sentinel/intersection observer (Coveo MoreResults, React "load more"
    // marker) reliably fires.
    if (target.scrollHeight - target.scrollTop - target.clientHeight < viewH) {
        target.scrollTop = target.scrollHeight;
    }
    return isWindow ? 'window' : 'container';
}
"""


def _try_infinite_scroll(page: Any, cfg: DiscoveryConfig,
                         extract_urls: Callable[[Any], list[str]],
                         seen: set[str], accumulated: list[str]) -> str:
    # Scroll-by-viewport. Always "applicable" (a no-op scroll still counts as
    # acting) — but the orchestrator only reaches here when load_more was not
    # applicable, matching the playwright template's click-then-scroll fallback.
    #
    # Targets the RESULTS CONTAINER, not the window (see _SCROLL_CONTAINER_JS):
    # virtualized listings (Coveo/React) lazy-load on container scroll, so the
    # old window-only scroll silently stalled after the first page.
    try:
        target = page.evaluate(_SCROLL_CONTAINER_JS)
        page.wait_for_timeout(cfg.scroll_wait_ms)
    except Exception as exc:
        logger.debug("scroll failed: %s", exc)
        return _STUCK
    added = _merge_new(accumulated, seen, _safe_extract(page, extract_urls, cfg.safe_eval_retries))
    if added:
        logger.debug("infinite_scroll progressed via %s: +%d urls", target, added)
        return _PROGRESSED
    return _STUCK


def _try_page_param(page: Any, cfg: DiscoveryConfig,
                    extract_urls: Callable[[Any], list[str]],
                    seen: set[str], accumulated: list[str],
                    next_page_num: int, state: dict) -> str:
    if not cfg.page_param_name:
        return _NOT_APPLICABLE
    current_url = getattr(page, "url", "") or cfg.site_url or ""
    if not current_url:
        return _NOT_APPLICABLE
    next_url = build_page_param_url(
        current_url, cfg.page_param_name, next_page_num, cfg.items_per_page,
    )
    if not _discovery_goto(page, next_url, cfg, state):
        return _NAV_ERROR
    added = _merge_new(accumulated, seen, _safe_extract(page, extract_urls, cfg.safe_eval_retries))
    return _PROGRESSED if added else _STUCK


def _try_next_button(page: Any, cfg: DiscoveryConfig,
                     extract_urls: Callable[[Any], list[str]],
                     seen: set[str], accumulated: list[str],
                     state: dict) -> str:
    href = find_next_button_url(page, cfg.site_url, cfg.next_button_selector)
    if not href:
        # Destructive fallback: click the declared/semantic button and read the
        # resulting URL. Only when no href was exposed.
        sels = ((cfg.next_button_selector,) if cfg.next_button_selector else ()) + DEFAULT_NEXT_BUTTON_SELECTORS
        clicked = False
        for sel in sels:
            try:
                btn = page.query_selector(sel)
                if btn:
                    btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return _NOT_APPLICABLE
        try:
            page.wait_for_load_state("domcontentloaded")
            import time as _time
            _time.sleep(cfg.page_settle_after_nav_s)
        except Exception:
            pass
        href = getattr(page, "url", "") or ""
        if not href:
            return _STUCK
    if not _discovery_goto(page, href, cfg, state):
        return _NAV_ERROR
    added = _merge_new(accumulated, seen, _safe_extract(page, extract_urls, cfg.safe_eval_retries))
    return _PROGRESSED if added else _STUCK


_PRIMITIVES = {
    "load_more": _try_load_more,
    "infinite_scroll": _try_infinite_scroll,
    "page_param": _try_page_param,
    "next_button": _try_next_button,
}


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def discover_item_urls(
    page: Any,
    start_url: str,
    extract_urls: Callable[[Any], list[str]],
    cfg: Optional[DiscoveryConfig] = None,
    on_progress: Optional[Callable[[int, int, list[str]], None]] = None,
) -> DiscoveryResult:
    """Drive a listing page through pagination and return all discovered item URLs.

    Parameters
    ----------
    page
        A Playwright-like page object (see :class:`PageLike`). Must already be
        created on a browser context owned by the caller.
    start_url
        The first listing/search/category URL to load. The caller builds this
        (e.g. ``SEARCH_URL_PATTERN.replace("{query}", q)``); discovery does the
        initial ``goto`` + render-wait itself.
    extract_urls
        Site-specific extraction callback ``page -> list[str]``. Owns the
        listing selector, same-domain filter, JSON-LD fallback. Discovery calls
        it after every primitive action and dedups the results.
    cfg
        :class:`DiscoveryConfig` (defaults = verified ``playwright_scraper.py``
        loop). Override ``strategies`` + ``page_param_name`` /
        ``next_button_selector`` for navigation-style sites.
    on_progress
        Optional ``callback(pages_visited, total_urls, urls)`` invoked at the end
        of each successful iteration with the current accumulated URL list (a
        reference, not a copy — cheap). Use it to checkpoint discovered URLs to
        disk (crash-retry resume on big directories) — keeps checkpointing, a
        scraper concern, out of this module.

    Returns
    -------
    DiscoveryResult
        ``urls`` (deduped, order-preserving) + ``stop_reason`` +
        ``max_pages_hit`` + ``pages_visited``. Feed ``stop_reason`` to the
        discovery-coverage gate.
    """
    cfg = cfg or DiscoveryConfig()
    state = {"stop_reason": StopReason.NO_NEXT_LINK.value}
    seen: set[str] = set()
    accumulated: list[str] = []

    # Initial navigate + render-wait.
    if not _discovery_goto(page, start_url, cfg, state):
        return DiscoveryResult([], stop_reason=state["stop_reason"])
    accumulated = _wait_for_render(page, extract_urls, cfg)
    seen.update(accumulated)
    if not accumulated:
        _set_stop(state, StopReason.EMPTY_RENDER)
        return DiscoveryResult([], stop_reason=state["stop_reason"])

    pages_visited = 1
    no_progress = 0
    while True:
        if cfg.max_pages is not None and pages_visited >= cfg.max_pages:
            _set_stop(state, StopReason.MAX_PAGES_HIT)
            return DiscoveryResult(
                accumulated, stop_reason=state["stop_reason"],
                max_pages_hit=True, pages_visited=pages_visited,
            )

        next_page_num = pages_visited + 1
        iteration_outcome: str = _NOT_APPLICABLE
        saw_stuck = False  # a same-page primitive acted but added nothing
        for name in cfg.strategies:
            handler = _PRIMITIVES.get(name)
            if handler is None:
                logger.warning("discovery: unknown strategy %r — skipped", name)
                continue
            is_same_page = name in ("load_more", "infinite_scroll")
            # A same-page stall (scroll/load-more yielded nothing) must NOT trigger
            # a NAV primitive — page_param/next_button navigate AWAY from the
            # current listing, which would derail a true load-more site that merely
            # stalled (e.g. a stray footer "Next" link). Same-page stalls may only
            # fall through to ANOTHER same-page primitive; reaching a nav primitive
            # after a same-page stall ends the iteration as stuck.
            if saw_stuck and not is_same_page:
                iteration_outcome = _STUCK
                break
            if is_same_page:
                outcome = handler(page, cfg, extract_urls, seen, accumulated)
            elif name == "page_param":
                outcome = handler(page, cfg, extract_urls, seen, accumulated, next_page_num, state)
            else:  # next_button
                outcome = handler(page, cfg, extract_urls, seen, accumulated, state)

            if outcome == _NAV_ERROR:
                return DiscoveryResult(
                    accumulated, stop_reason=state["stop_reason"],
                    pages_visited=pages_visited,
                )
            if outcome == _PROGRESSED:
                iteration_outcome = _PROGRESSED
                break  # FIRST progress wins — iteration done
            if outcome == _STUCK:
                if is_same_page:
                    # Same-page primitives (scroll, load-more) don't navigate, so
                    # it's safe and cheap to fall through to the NEXT strategy —
                    # e.g. a container scroll that stalled still lets load_more
                    # click Coveo's ".coveo-magicbox-load-more" button.
                    saw_stuck = True
                    continue
                # A nav primitive already navigated (acted) — don't also fire
                # another nav primitive this iteration (would double-navigate).
                iteration_outcome = _STUCK
                break
            # _NOT_APPLICABLE → try the next strategy

        if iteration_outcome == _NOT_APPLICABLE and saw_stuck:
            iteration_outcome = _STUCK

        if iteration_outcome == _PROGRESSED:
            no_progress = 0
            pages_visited += 1
            if on_progress:
                try:
                    on_progress(pages_visited, len(accumulated), accumulated)
                except Exception:
                    pass
            continue

        if iteration_outcome == _STUCK:
            no_progress += 1
            pages_visited += 1
            if no_progress >= cfg.max_no_progress:
                _set_stop(state, StopReason.NO_NEW_ITEMS)
                break
            continue

        # No primitive was applicable → no pagination mechanism present.
        _set_stop(state, StopReason.NO_NEXT_LINK)
        break

    return DiscoveryResult(
        accumulated, stop_reason=state["stop_reason"],
        max_pages_hit=False, pages_visited=pages_visited,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PRESETS — convenience builders mapping the old PAGINATION_TYPE → config.
# Use these from templates / code_writer instead of hand-rolling DiscoveryConfig.
# ═══════════════════════════════════════════════════════════════════════════════

def config_for_load_more(**overrides) -> DiscoveryConfig:
    """lw.com-style Coveo/React load-more + scroll. (playwright_scraper default.)"""
    cfg = DiscoveryConfig(strategies=("load_more", "infinite_scroll"))
    return _apply(cfg, overrides)


def config_for_page_param(page_param_name: str,
                          items_per_page: Optional[int] = None,
                          **overrides) -> DiscoveryConfig:
    """``?page=N`` (or ``?offset=N``) URL construction — deterministic, no DOM."""
    cfg = DiscoveryConfig(
        strategies=("page_param",),
        page_param_name=page_param_name,
        items_per_page=items_per_page,
    )
    return _apply(cfg, overrides)


def config_for_next_button(selector: Optional[str] = None,
                           **overrides) -> DiscoveryConfig:
    """Click/declared ``a.next``-style pager. Semantic fallbacks always on."""
    cfg = DiscoveryConfig(
        strategies=("next_button",),
        next_button_selector=selector,
    )
    return _apply(cfg, overrides)


def config_for_navigation_job(**overrides) -> DiscoveryConfig:
    """The navigation_scraper.py chain: load-more → page-param → next-button.

    Use for nav/list_page/search_term jobs when the site's pagination style is
    unknown or mixed (some sections load-more, others paginate by URL).
    """
    cfg = DiscoveryConfig(
        strategies=("load_more", "page_param", "next_button", "infinite_scroll"),
    )
    return _apply(cfg, overrides)


def _apply(cfg: DiscoveryConfig, overrides: dict) -> DiscoveryConfig:
    for k, v in overrides.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
        else:
            raise TypeError(f"Unknown DiscoveryConfig field: {k!r}")
    return cfg


def config_from_dict(d: dict) -> DiscoveryConfig:
    """Build a DiscoveryConfig from a discovery_config dict (from scraper_analyzer).

    The dict shape matches navigation_analysis.pagination:
    {type: "load_more"|"page_param"|"next_button"|"infinite_scroll",
     page_param_name: str, items_per_page: int, next_button_selector: str,
     max_pages: int}.

    Maps type → the right config_for_* preset, passing through extra overrides.
    Falls back to config_for_navigation_job (tries all strategies) when type is
    unknown/missing — so the loop self-discovers the right mechanism.
    """
    if not isinstance(d, dict):
        return config_for_navigation_job()
    ptype = str(d.get("type") or "").lower().replace("-", "_")
    # Pass through known overrides (max_pages, click_wait_ms, etc.)
    overrides = {k: v for k, v in d.items() if k != "type"}
    if ptype in ("load_more", "loadmore"):
        return config_for_load_more(**overrides)
    if ptype in ("page_param", "pageparam", "url_param"):
        return config_for_page_param(
            d.get("page_param_name") or "page",
            d.get("items_per_page"),
            **{k: v for k, v in overrides.items() if k not in ("page_param_name", "items_per_page")},
        )
    if ptype in ("next_button", "nextbutton"):
        return config_for_next_button(
            d.get("next_button_selector"),
            **{k: v for k, v in overrides.items() if k != "next_button_selector"},
        )
    if ptype in ("infinite_scroll", "infinite_scroll_tall", "infinitescroll"):
        from src.discovery import DiscoveryConfig as _DC
        cfg = _DC(strategies=("infinite_scroll", "load_more"))
        return _apply(cfg, overrides)
    # Unknown/missing → try all strategies (self-discovery)
    return config_for_navigation_job(**overrides)
