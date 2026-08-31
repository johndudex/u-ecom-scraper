"""T2.7: verify field mappings against ONE live page render.

The writer→tester loop's dominant mapping-failure class starts before any
code exists: ``product_analysis`` maps each field to a source (JSON-LD path,
CSS selector) guessed from one sample browse, and NOTHING exercises those
sources again until code_tester fails the draft 1-3 cycles later. This
module renders the sample page ONCE via browser_service ``/render``
(``start_method`` = the probe's ``method_that_worked`` — the access path
that actually reached the site) and applies every mapped source to the
rendered HTML, rewriting ``fields[].tested`` / ``fields[].resolved_value``
so code_writer consumes VERIFIED mappings and the tester stops being the
first thing that discovers a dead selector.

Constraints (deliberate):
- ONE ``/render`` call total: ``/render`` holds browser_service's GLOBAL
  PROBE_LOCK, so every call serializes all jobs site-wide. Never loop.
- Env-gated: ``SCRAPER_MAPPING_VERIFY`` (default on; "0"/"false" disables).
- Never during the tester window: this runs from ``normalize_fields``,
  which sits before code_writer; the graph never reaches it concurrently
  with code_tester (remap cycles re-enter at product_analyzer).
- Deterministic and total: any error degrades to "unverified" — this module
  NEVER raises and NEVER invents a mapping.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Field-map source kinds that a page render CAN exercise. "api" (dotted path
# into a JSON endpoint response) needs an API call, not a render, so it is
# reported as skipped rather than guessed at.
_RENDERABLE_METHODS = {"structured_data", "css", "resolver", "jsonld"}

_VALUE_SAMPLE_CAP = 200


def verify_enabled() -> bool:
    """SCRAPER_MAPPING_VERIFY gate (default on; "0"/"false"/"no" disables)."""
    raw = (os.environ.get("SCRAPER_MAPPING_VERIFY") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _walk_path(data: Any, path: str) -> Any:
    """Walk a dotted path (``offers.price``, ``hits.0.title``) through dicts/lists."""
    node = data
    for seg in str(path or "").split("."):
        seg = seg.strip()
        if not seg:
            continue
        if isinstance(node, dict):
            node = node.get(seg)
        elif isinstance(node, list):
            try:
                node = node[int(seg)]
            except (ValueError, IndexError, TypeError):
                return None
        else:
            return None
        if node is None:
            return None
    return node


def _product_block(
    jsonld_blocks: list, jsonld_types: tuple[str, ...], allow_fallback: bool = True
) -> Optional[dict]:
    """The JSON-LD block to walk paths against (content-type typed, else first).

    ``allow_fallback=False`` (the resolver path) requires an ACTUALLY TYPED
    match — the first-dict fallback would judge an API-derived path against
    an unrelated block and false-alarm "empty".
    """
    blocks = [b for b in (jsonld_blocks or []) if isinstance(b, dict)]
    # "@graph" wrappers (schema.org multi-entity pages) hide the typed entity
    # one level down — flatten them into the search pool.
    flat: list[dict] = []
    for block in blocks:
        graph = block.get("@graph")
        if isinstance(graph, list):
            flat.extend(b for b in graph if isinstance(b, dict))
    flat = blocks + flat
    for block in flat:
        btype = block.get("@type", "")
        if isinstance(btype, list):
            btype = btype[0] if btype else ""
        if btype and jsonld_types and str(btype) in jsonld_types:
            return block
    if allow_fallback:
        for block in flat:
            return block
    return None


def _strip_jsonld_selector(selector: str) -> str:
    """``JSON-LD Product.offers.price`` → ``offers.price``.

    The analyzer prefixes JSON-LD selectors with the block type; the actual
    path starts at the first segment AFTER the @type token. Heuristic: drop a
    leading "JSON-LD" token and the @type segment that follows it.
    """
    parts = [p for p in str(selector or "").strip().split(".") if p]
    # The analyzer writes "JSON-LD Product.offers.price": the "JSON-LD" tag is
    # space-joined to the @type inside the FIRST dotted segment, so split it
    # off before path-walking.
    if parts and parts[0].lower().startswith("json-ld"):
        head = parts[0].split(None, 1)
        parts = ([head[1]] if len(head) > 1 else []) + parts[1:]
    if len(parts) > 1 and parts[0].lower() in (
        "product", "productgroup", "individualproduct", "offer", "article",
        "newsarticle", "blogposting", "jobposting", "discussionforumposting",
        "question",
    ):
        parts = parts[1:]
    return ".".join(parts)


def _resolve_via_render(
    field_info: dict, html: str, jsonld_blocks: list, jsonld_types: tuple[str, ...]
) -> tuple[str, str]:
    """Apply one field's mapped source to rendered HTML. Returns (verdict, sample).

    verdict: ``verified`` (source yielded a value) | ``empty`` (source ran but
    produced nothing — a mapping bug signal) | ``skipped`` (source not
    reachable via a page render).
    """
    method = str(field_info.get("method") or "").strip().lower()
    selector = str(field_info.get("selector") or "").strip()
    if method not in _RENDERABLE_METHODS:
        return "skipped", ""

    # "resolver" paths (src.job_fields) may be derived from API/Algolia samples,
    # not page JSON-LD — only judge them against a CONTENT-TYPE-TYPED block
    # (no first-dict fallback), otherwise an API-derived path false-alarms
    # "empty" against an unrelated first block.
    typed_block = _product_block(
        jsonld_blocks, jsonld_types, allow_fallback=(method != "resolver")
    )
    any_block = typed_block or _product_block(jsonld_blocks, ())
    if method == "resolver":
        block = typed_block
        if block is None:
            return "skipped", ""
    else:
        block = any_block

    value: Any = None
    if method in ("structured_data", "jsonld", "resolver"):
        path = _strip_jsonld_selector(selector)
        value = _walk_path(block, path) if (block is not None and path) else None
        if value in (None, "", {}, []):
            fallback = str(field_info.get("css_fallback") or "").strip()
            if fallback:
                value = _css_value(html, fallback)
    elif method == "css":
        value = _css_value(html, selector)
    if value in (None, "", {}, []):
        return "empty", ""
    return "verified", str(value)[:_VALUE_SAMPLE_CAP]


def _css_value(html: str, selector: str) -> Any:
    """First-match text for a CSS selector (None when nothing matches)."""
    if not html or not selector:
        return None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        el = soup.select_one(selector)
        if el is None:
            return None
        text = el.get_text(strip=True)
        if text:
            return text
        # img/content/value style attributes carry data without text nodes.
        return (
            el.get("content")
            or el.get("value")
            or (el.get("src") if el.name == "img" else None)
        )
    except Exception:
        return None


def _jsonld_types_for(page_type: str) -> tuple[str, ...]:
    try:
        from src.content_types import get_config_for_page_type

        cfg = get_config_for_page_type(page_type)
        return tuple(str(t) for t in (getattr(cfg, "jsonld_types", None) or ()))
    except Exception:
        return ()


def _fetch_render(url: str, start_method: str) -> tuple[Optional[str], str]:
    """ONE /render call. Returns (html, method_used); html None on failure."""
    try:
        import httpx

        service_url = os.environ.get(
            "BROWSER_SERVICE_URL", "http://browser_service:8001"
        )
        payload: dict = {"url": url, "timeout": 120}
        if start_method:
            payload["start_method"] = start_method
        resp = httpx.post(f"{service_url}/render", json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") and data.get("html"):
            return data["html"], str(data.get("method") or "?")
        return None, str(data.get("error") or "render failed")
    except Exception as exc:
        return None, str(exc)[:200]


def verify_field_mappings(
    slug: str, state: dict, analysis: dict
) -> tuple[dict, dict]:
    """Verify ``analysis["fields"]`` against one live render of the sample page.

    Returns ``(analysis, summary)``. ``summary`` has verified/empty/skipped
    counts and is safe to log. On ANY problem (no sample URL, render failed,
    disabled) the analysis is returned unchanged with an empty summary —
    verification is strictly additive, never a gate.
    """
    if not isinstance(analysis, dict):
        return analysis, {}
    fields = analysis.get("fields")
    if not isinstance(fields, dict) or not fields:
        return analysis, {}

    url = str(
        state.get("sample_url") or state.get("product_url") or state.get("url") or ""
    ).strip()
    if not url.startswith(("http://", "https://")):
        return analysis, {}

    probe = state.get("probe_result") if isinstance(state.get("probe_result"), dict) else {}
    conn = probe.get("connectivity") if isinstance(probe.get("connectivity"), dict) else {}
    start_method = str(conn.get("method_that_worked") or "").strip()

    html, method_used = _fetch_render(url, start_method)
    if not html:
        logger.warning(
            "field_verification: render failed for %s (%s) — fields left unverified",
            url[:80], method_used,
        )
        return analysis, {"error": method_used}

    try:
        from src.page_analysis import extract_jsonld

        jsonld_blocks = extract_jsonld(html) or []
    except Exception:
        jsonld_blocks = []
    jsonld_types = _jsonld_types_for((state.get("page_type") or "product").lower())

    summary = {"verified": 0, "empty": 0, "skipped": 0, "method": method_used}
    for name, info in fields.items():
        if not isinstance(info, dict):
            continue
        try:
            verdict, sample = _resolve_via_render(info, html, jsonld_blocks, jsonld_types)
        except Exception:
            verdict, sample = "skipped", ""
        info["tested"] = verdict
        if sample:
            info["resolved_value"] = sample
        summary[verdict] = summary.get(verdict, 0) + 1

    _empty = [
        name for name, info in fields.items()
        if isinstance(info, dict) and info.get("tested") == "empty"
    ]
    if _empty:
        summary["empty_fields"] = _empty
        logger.warning(
            "field_verification: %d mapped source(s) produced NOTHING on the live "
            "render of %s: %s — code_writer must treat these as unproven and wire "
            "a verified fallback",
            len(_empty), url[:80], ", ".join(_empty[:10]),
        )
    logger.info(
        "field_verification: %s → verified=%d empty=%d skipped=%d (render via %s)",
        slug, summary["verified"], summary["empty"], summary["skipped"], method_used,
    )
    return analysis, summary
