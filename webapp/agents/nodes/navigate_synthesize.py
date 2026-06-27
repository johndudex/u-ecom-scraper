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

from django.conf import settings

from agents.graph import _log_agent_context, _persist_agent_logs

logger = logging.getLogger(__name__)

NAVIGATION_SYNTHESIZE_BUDGET = 15


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

        agent_cfg: dict = {"recursion_limit": NAVIGATION_SYNTHESIZE_BUDGET}
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
            "max_pages": None,
            "total_count_selector": "",
        }
        if pagination.get("sample_hrefs"):
            pagination_section["sample_hrefs"] = pagination["sample_hrefs"][:3]

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
