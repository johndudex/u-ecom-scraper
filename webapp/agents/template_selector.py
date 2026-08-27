"""Single authority for scraper-template selection (T3.2 / I5-narrow).

Historically TWO selectors disagreed: this mapping (graph.py, choosing the
template injected into code_writer's system prompt) and a mechanism-first
re-derivation inside ``build_code_writer_message`` (subagents.py, choosing the
template-hint line) that could never return api/ssr_div_list/requests
templates. When they disagreed, the writer read one template's code while
being pointed at another — the C7/argparse-exit-2 class. Both call sites now
import this function; do NOT add a third derivation.

This module must stay import-light (no Django / no graph import) so both
graph.py and subagents.py can use it without cycles.
"""

from __future__ import annotations

from typing import Any


def select_template_file(state: dict[str, Any] | None) -> str:
    """Return the template filename for this job's strategy/data_source.

    Simplified selection covering the 5 main templates. For edge cases
    (undetected_chromedriver, navigation_scraper), the LLM can still read_file
    the template — the system prompt's template is a reference, not a
    replacement for the message's hint.
    """
    state = state or {}
    nav = state.get("navigation_analysis") or {}
    sa = state.get("scraper_analysis") or {}
    strategy = (sa.get("strategy") or "").lower()
    data_source = nav.get("data_source", "")
    api_ep = nav.get("api_endpoint") or {}

    if isinstance(api_ep, dict) and (api_ep.get("url") or api_ep.get("api_url")):
        return "api_scraper.py"
    if data_source == "ssr_div_list":
        return "ssr_div_list_scraper.py"
    if strategy in ("http_requests", "requests"):
        # Form-POST sites (locumtenens: QuickSearch POST → SSR) need the
        # navigation template (playwright form-POST replay + FORM_ACTION),
        # not the plain requests template. Verified: locumtenens' working
        # scraper imports playwright.sync_api + uses FORM_ACTION.
        _nav_fm = (nav.get("search") or {}).get("form_method") or ""
        if str(_nav_fm).upper() == "POST":
            return "navigation_scraper.py"
        return "requests_scraper.py"
    if strategy == "http_navigation":
        return "http_navigation_scraper.py"
    if strategy == "playwright":
        # Playwright strategy → playwright_scraper.py (its discover step
        # render-polls, which is what surfaces JS-rendered listings like Coveo
        # that http_navigation's /navigate 2s wait cannot). Mapping playwright
        # to http_navigation_scraper.py silently defeated the strategy.
        return "playwright_scraper.py"
    if strategy in ("internal_api", "api"):
        return "api_scraper.py"
    return "requests_scraper.py"
