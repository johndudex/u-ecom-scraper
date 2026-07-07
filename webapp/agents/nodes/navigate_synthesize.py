"""Navigation synthesis node — LLM agent that converts raw findings to structured JSON.

Reads ``navigation_findings.json`` (produced by ``navigate_explore``) and
``site_analysis.json``, then writes the structured ``navigation_analysis.json``
that the code-writer expects.

This agent has ONLY ``read_file`` and ``write_file`` tools — no Playwright,
no web_fetch.  It cannot explore, only synthesize.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

from django.conf import settings

from agents.graph import _log_agent_context, _persist_agent_logs

logger = logging.getLogger(__name__)

NAVIGATION_SYNTHESIZE_BUDGET = 15


def _build_filters_section(findings: dict) -> dict[str, Any]:
    """Build the ``filters`` section for navigation_analysis from raw findings.

    Reads ``listing_page.detected_filters`` (URL params) and
    ``listing_page.filter_ui`` (form elements) captured by navigate_explore and
    structures them so the code-writer can apply the right filter mechanism
    (URL params vs form interaction) per site.  This is what enables job-portal
    filtering (date range, location, category).
    """
    listing_page = findings.get("listing_page", {}) or {}
    detected = listing_page.get("detected_filters", {}) or {}
    filter_ui = listing_page.get("filter_ui", {}) or {}

    url_date = detected.get("url_date_params", []) or []
    url_loc = detected.get("url_location_params", []) or []
    url_cat = detected.get("url_category_params", []) or []

    ui_date = filter_ui.get("date_selectors", []) or []
    ui_loc = filter_ui.get("location_selectors", []) or []
    ui_cat = filter_ui.get("category_selectors", []) or []

    has_filters = bool(url_date or url_loc or url_cat or ui_date or ui_loc or ui_cat)
    if not has_filters:
        return {"has_filters": False, "date_filter": {}, "location_filter": {}, "category_filter": {}}

    has_url = bool(url_date or url_loc or url_cat)
    has_form = bool(ui_date or ui_loc or ui_cat)
    method = "mixed" if (has_url and has_form) else ("url" if has_url else "form")

    # Build a base URL (scheme+host+path, no query) for url_pattern construction
    listing_url = listing_page.get("url", "")
    base_path = ""
    if listing_url:
        parsed = urlparse(listing_url)
        if parsed.scheme and parsed.netloc:
            base_path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _filter_entry(url_param_list, ui_selector_list, placeholder):
        entry: dict[str, Any] = {}
        # URL parameter (URL-based filtering)
        if url_param_list:
            first = url_param_list[0]
            param_name = first.get("param", "")
            if param_name:
                entry["param_name"] = param_name
                entry["detected_value"] = first.get("value", "")
                if base_path:
                    entry["url_pattern"] = (
                        f"{base_path}?{param_name}={{{placeholder}}}"
                    )
        # Form element (form-based filtering)
        if ui_selector_list:
            ui = ui_selector_list[0]
            if ui.get("selector"):
                entry["selector"] = ui["selector"]
            if ui.get("name"):
                entry["name"] = ui["name"]
            if ui.get("options"):
                entry["values"] = ui["options"]
            elif entry.get("detected_value"):
                entry["values"] = [entry["detected_value"]]
            for k in ("form_id", "form_action", "submit_button", "submit_text"):
                if ui.get(k):
                    entry[k] = ui[k]
        # Tag with iteration strategy: detected_value="all" → iterate (query
        # didn't narrow this dimension → code_writer loops options for full
        # catalog); specific value → pin (query named this dimension).
        _broad = str(entry.get("detected_value", "")).lower().strip() in (
            "all", "any", "", "none", "- any -"
        )
        if _broad:
            entry["strategy"] = "iterate"
            entry["strategy_value"] = None
            entry["reason"] = "Query didn't narrow this dimension — iterate options for full catalog"
        else:
            entry["strategy"] = "pin"
            entry["strategy_value"] = entry.get("detected_value")
            entry["reason"] = f"Pinned to detected_value '{entry.get('detected_value')}'"
        return entry

    return {
        "has_filters": True,
        "method": method,
        "date_filter": _filter_entry(url_date, ui_date, "days"),
        "location_filter": _filter_entry(url_loc, ui_loc, "state"),
        "category_filter": _filter_entry(url_cat, ui_cat, "category"),
    }


def _load_findings(root: str, slug: str) -> dict:
    """Load navigation_findings.json for a slug (returns {} on failure)."""
    findings_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
    try:
        with open(findings_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _best_api_endpoint(findings: dict) -> dict:
    """Pick the best backend JSON search-API endpoint from raw findings.

    navigate_explore accumulates XHR/fetch resource URLs (captured via the
    browser Performance API) into ``findings["api_endpoints"]`` and
    ``listing_page["api_endpoints"]``.  For React/Vue SPAs (e.g. AMN
    Healthcare's /JobSearch) this is the clean, correct extraction path — far
    better than driving the DOM.  Return the highest-ranked candidate that
    looks like a real listing/search API (has a page param + query string),
    plus the discovered query params so the code-writer can reproduce calls.
    """
    candidates = []
    candidates.extend(findings.get("api_endpoints") or [])
    candidates.extend((findings.get("listing_page") or {}).get("api_endpoints") or [])
    # Dedupe by URL, keep order (navigate_explore already ranked best-first)
    seen = set()
    unique = []
    for c in candidates:
        url = (c or {}).get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(c)

    def _score(c):
        url = c.get("url", "")
        s = 0
        if re.search(r"job|search|listing|result|position|vacanc|posting", url, re.I):
            s += 3
        if "PageNumber" in url or re.search(r"[?&]page=", url, re.I):
            s += 3
        if "?" in url:
            s += 2
        if re.search(r"/api/|/v\d+/", url, re.I):
            s += 1
        # Penalise obvious telemetry
        if re.search(r"zaius|optimizely|bat\.bing|pinterest|google.*collect|facebook|/v2/track|\.gif|/events", url, re.I):
            s -= 10
        return s

    unique.sort(key=_score, reverse=True)
    for c in unique:
        url = c.get("url", "")
        if _score(c) >= 4 and re.search(r"job|search|listing|position|vacanc|posting", url, re.I):
            parsed = urlparse(url)
            params = [p.split("=")[0] for p in parsed.query.split("&") if p]
            low = [p.lower() for p in params]
            page_param = (
                "PageNumber" if "pagenumber" in low
                else ("page" if "page" in low
                      else next((p for p in params if "page" in p.lower()), "page"))
            )
            page_size_param = next(
                (p for p in params if re.search(r"pagesize|page_size|per_page|limit|size", p, re.I)),
                "PageSize",
            )
            return {
                "url": url,  # FULL url with query string + every param value
                "base": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                "method": c.get("method", "GET"),
                "query_params": params,
                "pagination_param": page_param,
                "page_size_param": page_size_param,
                "has_pagination": ("pagenumber" in low) or ("page" in low),
            }
    return {}


def _ensure_api_endpoint_in_analysis(analysis: dict, root: str, slug: str) -> dict:
    """Merge a discovered backend API endpoint into analysis.

    The LLM synthesizer often produces a cleaned-up ``api_endpoint`` (e.g. just
    the base URL + pagination_param) that LOSES the full query string (and thus
    the exact param names/values like ``LocationSearch`` / ``Filters`` the
    code-writer needs to reproduce calls).  We always overlay the raw captured
    endpoint (full URL + all query params) on top, while keeping any useful
    extras the LLM added (filter_types, pagination_param).
    """
    findings = _load_findings(root, slug)
    best = _best_api_endpoint(findings)
    if not best:
        return analysis
    existing = analysis.get("api_endpoint") if isinstance(analysis.get("api_endpoint"), dict) else {}
    merged = dict(best)  # full url + query_params + pagination_param win
    # Preserve LLM-added extras that we don't compute (e.g. filter_types).
    for k, v in existing.items():
        if k not in merged and v:
            merged[k] = v
    analysis["api_endpoint"] = merged
    return analysis


def _ensure_filters_in_analysis(analysis: dict, root: str, slug: str) -> dict:
    """Merge a ``filters`` section into analysis if the LLM omitted one.

    The LLM may or may not emit a filters block; deterministically derive it
    from the raw findings so downstream agents always see filter info.  Safe to
    call on any analysis dict — never overwrites an existing ``filters`` block
    that already has ``has_filters=True``.
    """
    existing = analysis.get("filters")
    if isinstance(existing, dict) and existing.get("has_filters"):
        return analysis
    findings = _load_findings(root, slug)
    analysis["filters"] = _build_filters_section(findings)
    return analysis


def navigate_synthesize(state: dict, config=None) -> dict[str, Any]:
    """LLM synthesis node — reads raw findings, writes structured analysis.

    If the LLM fails to produce output, a best-effort fallback synthesizes
    the JSON deterministically from the raw findings.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")

    logger.info("navigate_synthesize: starting (job %s, slug=%s)", job_id, slug)

    root = getattr(settings, "PROJECT_ROOT", os.getcwd())
    findings_path = os.path.join(root, "workspace", slug, "navigation_findings.json")

    if not os.path.isfile(findings_path):
        logger.error(
            "navigate_synthesize: navigation_findings.json not found at %s — "
            "navigate_explore may have failed",
            findings_path,
        )
        return _fallback_synthesize(state, root, slug)

    # If findings have no data, skip LLM (it would hallucinate) — use fallback
    try:
        with open(findings_path, "r", encoding="utf-8") as f:
            raw_findings = json.load(f)
    except (json.JSONDecodeError, OSError):
        raw_findings = {}

    cat_links = raw_findings.get("homepage_nav", {}).get("category_links", [])
    prod_links = raw_findings.get("listing_page", {}).get("product_links", [])
    has_fatal_locale_error = any(
        "locale mismatch" in e.lower() and "compatible" not in e.lower()
        for e in raw_findings.get("errors", [])
    )

    _non_product_kw = [
        "privacy", "cookie", "terms", "policy", "mailto:", "javascript:",
        "store-locator", "careers", "about", "contact", "faq", "help",
        "unsubscribe", "gdpr", "shipping", "returns", "track", "order",
    ]
    _promo_kw = [
        "special-collection", "pride-collection", "bestsellers", "sale-",
        "gift", "edit", "new-arrivals", "new-in",
    ]

    def _is_real_product_link(link: dict) -> bool:
        href = (link.get("href", "") or "").lower()
        text = (link.get("text", "") or "").lower()
        if any(kw in href or kw in text for kw in _non_product_kw):
            return False
        if any(kw in href for kw in _promo_kw):
            return False
        if text.count(" ") > 8 and any(w in text for w in ["shop now", "experience", "discover", "explore"]):
            return False
        return True

    real_prod_links = [p for p in prod_links if _is_real_product_link(p)]

    session_gated = (
        raw_findings.get("search_attempted", False)
        and not raw_findings.get("listing_page", {}).get("url")
        and "oops" in str(raw_findings.get("errors", []))
    )

    if (not cat_links and not real_prod_links) or has_fatal_locale_error or session_gated:
        logger.warning(
            "navigate_synthesize: findings empty or low-quality (%d cats, %d real prods, session_gated=%s), "
            "skipping LLM agent to prevent hallucination",
            len(cat_links),
            len(real_prod_links),
            session_gated,
        )
        return _fallback_synthesize(state, root, slug)

    try:
        from agents.subagents import (
            build_navigation_synthesize_message,
            create_navigation_synthesize,
        )

        messages = build_navigation_synthesize_message(state)
        _log_agent_context(state, "navigation-synthesize", messages)
        agent = create_navigation_synthesize(site_slug=slug)

        agent_cfg: dict = {}
        if config:
            agent_cfg.update(config)

        try:
            result = agent.invoke({"messages": messages}, config=agent_cfg)
        except Exception as exc:
            logger.warning(
                "navigate_synthesize: agent invocation error (job %s): %s — "
                "checking if file was written before error",
                job_id,
                str(exc)[:200],
            )
            result = {"messages": []}

        _persist_agent_logs(state, result, "navigation-synthesize", agent_cfg)

        # Check if the agent wrote the file
        analysis_path = os.path.join(
            root, "workspace", slug, "navigation_analysis.json"
        )
        if os.path.isfile(analysis_path):
            try:
                with open(analysis_path, "r", encoding="utf-8") as f:
                    analysis = json.load(f)
                # Ensure a filters section exists (LLM may omit it)
                analysis = _ensure_filters_in_analysis(analysis, root, slug)
                # Ensure a discovered backend API endpoint is surfaced (SPAs)
                analysis = _ensure_api_endpoint_in_analysis(analysis, root, slug)
                try:
                    with open(analysis_path, "w", encoding="utf-8") as f:
                        json.dump(analysis, f, indent=2, ensure_ascii=False)
                except OSError:
                    pass
                logger.info(
                    "navigate_synthesize: success — navigation_analysis.json written "
                    "(job %s)",
                    job_id,
                )
                return {
                    "navigation_analysis": analysis,
                    "messages": [],
                }
            except json.JSONDecodeError as exc:
                logger.warning(
                    "navigate_synthesize: file written but invalid JSON: %s", exc
                )

        # Agent didn't write the file — use fallback
        logger.warning(
            "navigate_synthesize: agent did not write navigation_analysis.json "
            "(job %s) — using fallback synthesizer",
            job_id,
        )
        return _fallback_synthesize(state, root, slug)

    except Exception as exc:
        logger.exception("navigate_synthesize: failed: %s", exc)
        return _fallback_synthesize(state, root, slug)


def _fallback_synthesize(state: dict, root: str, slug: str) -> dict[str, Any]:
    """Deterministic fallback — produce navigation_analysis.json from findings.

    Used when the LLM agent fails or doesn't write the file.  Produces a
    minimal but valid structure from the raw findings data.
    """
    logger.info("navigate_synthesize: using fallback synthesizer (slug=%s)", slug)

    findings_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
    try:
        with open(findings_path, "r", encoding="utf-8") as f:
            findings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        findings = {}

    homepage_nav = findings.get("homepage_nav", {})
    listing_page = findings.get("listing_page", {})
    url_patterns = findings.get("url_patterns", {})
    metadata = findings.get("metadata", {})

    search_criteria = metadata.get("search_criteria", "")

    _non_product_kw = [
        "privacy", "cookie", "terms", "policy", "mailto:", "javascript:",
        "store-locator", "careers", "about", "contact", "faq", "help",
        "unsubscribe", "gdpr", "shipping", "returns", "track", "order",
    ]
    _promo_kw = [
        "special-collection", "pride-collection", "bestsellers", "sale-",
        "gift", "edit", "new-arrivals", "new-in",
    ]

    def _is_real_product_link(link: dict) -> bool:
        href = (link.get("href", "") or "").lower()
        text = (link.get("text", "") or "").lower()
        if any(kw in href or kw in text for kw in _non_product_kw):
            return False
        if any(kw in href for kw in _promo_kw):
            return False
        if text.count(" ") > 8 and any(w in text for w in ["shop now", "experience", "discover", "explore"]):
            return False
        return True

    # Determine discovery method
    has_search = bool(findings.get("search_attempted"))
    search_criteria = metadata.get("search_criteria", "")
    category_links = homepage_nav.get("category_links", [])
    has_categories = len(category_links) >= 3
    listing_product_links = listing_page.get("product_links", [])
    real_product_links = [p for p in listing_product_links if _is_real_product_link(p)]
    has_fatal_locale_error = any(
        "locale mismatch" in e.lower() and "compatible" not in e.lower()
        for e in findings.get("errors", [])
    )
    listing_url = listing_page.get("url", "")

    if has_fatal_locale_error and not real_product_links:
        discovery_method = "failed"
    elif has_search and search_criteria and real_product_links:
        discovery_method = "search"
    elif has_categories and real_product_links:
        discovery_method = "category"
    elif url_patterns.get("detected_suffix_pattern"):
        discovery_method = "url_pattern"
    elif not has_categories and not real_product_links and not has_search:
        discovery_method = "failed"
    else:
        discovery_method = "category" if has_categories else "unknown"

    working_url = ""
    if listing_url and real_product_links:
        working_url = listing_url

    # Build search section
    search_section: dict[str, Any] = {}
    search_form = homepage_nav.get("search_form")
    if search_form and isinstance(search_form, dict):
        search_section = {
            "has_search": True,
            "input_selector": search_form.get("search_input_selector", ""),
            "submit_selector": "",
            "url_pattern": search_form.get("action", ""),
            "has_url_search": bool(search_form.get("action")),
            "search_url_pattern": search_form.get("action", ""),
            "working_url": working_url,
        }
    elif findings.get("search_attempted"):
        listing_url_for_search = listing_url if listing_url else ""
        if listing_url_for_search and search_criteria:
            from urllib.parse import urlparse as _up, parse_qs as _pqs, urlencode as _ue

            parsed = _up(listing_url_for_search)
            params = _pqs(parsed.query)
            search_param = ""
            for key in list(params.keys()):
                kl = key.lower()
                if kl in ("search", "q", "searchterm", "keyword", "query"):
                    search_param = key
                    break
            if search_param:
                search_url_pattern = (
                    f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    f"?{search_param}={{criteria}}"
                )
            else:
                search_url_pattern = listing_url_for_search
        else:
            search_url_pattern = ""

        search_section = {
            "has_search": True,
            "input_selector": "",
            "submit_selector": "",
            "url_pattern": listing_url_for_search,
            "has_url_search": True,
            "search_url_pattern": search_url_pattern,
            "listing_url_used": listing_url_for_search,
            "working_url": working_url,
            "notes": f"Search was attempted by navigate_explore. Products found at: {listing_url_for_search}" if listing_url_for_search else "Search was attempted but no results found.",
        }
    else:
        search_section = {"has_search": False}

    # Build categories section
    categories_section: dict[str, Any] = {}
    if category_links:
        # Try to find a URL pattern
        cat_paths = []
        for link in category_links[:10]:
            from urllib.parse import urlparse as _up

            path = _up(link.get("href", "")).path
            if path:
                cat_paths.append(path)

        categories_section = {
            "menu_selector": "nav, [role=navigation], .menu",
            "category_links": [c.get("href", "") for c in category_links[:20]],
            "url_patterns": list({p for p in cat_paths if p})[:5],
        }

    # Build pagination section
    pagination_section: dict[str, Any] = {}
    pagination = listing_page.get("pagination")
    if pagination and isinstance(pagination, dict):
        pagination_section = {
            "type": pagination.get("type", ""),
            "next_button_selector": pagination.get("next_selector", ""),
            "page_param_name": pagination.get("page_param", ""),
            "max_pages": pagination.get("max_pages"),
            "total_count_selector": "",
        }
        if pagination.get("sample_hrefs"):
            pagination_section["sample_hrefs"] = pagination["sample_hrefs"][:3]
        for extra_key in ("next_text", "next_href", "note", "page_indicator_text",
                          "current_page", "items_per_page"):
            if pagination.get(extra_key):
                pagination_section[extra_key] = pagination[extra_key]
    # Fallback: if no pagination detected but we have total_products, infer pagination
    total_products = listing_page.get("total_products", 0)
    if not pagination_section and total_products and total_products > len(real_product_links):
        pagination_section = {
            "type": "unknown",
            "max_pages": None,
            "total_count_selector": "",
            "inferred": True,
            "note": f"Found {len(real_product_links)} of {total_products} products — pagination likely needed",
        }

    # Build item_links section — use filtered real_product_links
    item_links_section: dict[str, Any] = {}
    if real_product_links:
        # Detect URL pattern from product links
        product_hrefs = [p.get("href", "") for p in real_product_links[:10]]
        from urllib.parse import urlparse as _up

        product_paths = [_up(h).path for h in product_hrefs if h]

        # Try to generalize a pattern
        url_pattern = ""
        if product_paths:
            sample = product_paths[0]
            pattern = re.sub(r"\d+", "{id}", sample)
            url_pattern = pattern

        link_selector = "a[href]"
        if url_pattern:
            path_segments = [s for s in url_pattern.split("/") if s and not s.startswith("{")]
            for seg in path_segments:
                if seg.startswith(("-", "_")):
                    link_selector = f"a[href*='{seg}']"
                    break

        container_selector = ".product-grid, .product-list, [class*=product], [data-pid], .product-tile, .grid-item--product"
        item_links_section = {
            "container_selector": container_selector,
            "link_selector": link_selector,
            "url_pattern": url_pattern,
            "url_examples": product_hrefs[:5],
        }

    # Also pull from JSON-LD if present in listing page findings
    json_ld_data = listing_page.get("json_ld", {})
    if json_ld_data:
        json_ld_products = json_ld_data.get("products", [])
        if json_ld_products and not listing_product_links:
            product_hrefs = [p.get("href", "") for p in json_ld_products[:10]]
            from urllib.parse import urlparse as _up

            product_paths = [_up(h).path for h in product_hrefs if h]
            url_pattern = ""
            if product_paths:
                sample = product_paths[0]
                pattern = re.sub(r"\d+", "{id}", sample)
                url_pattern = pattern
            item_links_section = {
                "container_selector": "json-ld",
                "link_selector": "json-ld ItemList",
                "url_pattern": url_pattern,
                "url_examples": product_hrefs[:5],
            }
            logger.info(
                "navigate_synthesize: fallback using JSON-LD data (%d products)",
                len(json_ld_products),
            )

    # Combine into final structure
    framework_hints = homepage_nav.get("framework_hints", {})
    analysis = {
        "discovery_method": discovery_method,
        "search": search_section,
        "categories": categories_section,
        "pagination": pagination_section,
        "item_links": item_links_section,
        "filters": _build_filters_section(findings),
        "api_endpoint": _best_api_endpoint(findings),
        "framework_hints": framework_hints if framework_hints else None,
        "list_page_detection": {
            "is_list_page": bool(listing_product_links),
            "indicators": (
                ["multiple product links", "grid layout"]
                if listing_product_links
                else []
            ),
        },
        "_fallback": True,
        "_findings_source": f"workspace/{slug}/navigation_findings.json",
    }

    # Write the fallback file
    analysis_path = os.path.join(root, "workspace", slug, "navigation_analysis.json")
    os.makedirs(os.path.dirname(analysis_path), exist_ok=True)
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)

    logger.info(
        "navigate_synthesize: fallback wrote navigation_analysis.json (slug=%s, "
        "discovery=%s, real_items=%d, total_links=%d)",
        slug,
        discovery_method,
        len(real_product_links),
        len(listing_product_links),
    )

    return {
        "navigation_analysis": analysis,
    }
