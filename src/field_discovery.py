"""LLM-driven field discovery for the /intake "Discover Fields" button.

Given a rendered page's HTML, discovers ALL extractable data fields using a
one-shot LLM call (glm-5-turbo). Falls back to deterministic JSON-LD parsing
if the LLM fails. Pure Python except for the lazy LLM import.

Flow: extract_jsonld + extract_meta_tags + visible-text summary → LLM prompt →
parse JSON response → {fields, json_schema, source, content_type}.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── helpers (reuse from src/page_analysis.py) ────────────────────────────────
from src.page_analysis import extract_jsonld, extract_meta_tags, extract_title

_BOILERPLATE = "script|style|svg|nav|footer|header|aside|noscript|template"
_VALID_TYPES = {"text", "number", "boolean", "url", "date", "array", "object"}
_TYPE_MAP = {"text": "string", "number": "number", "boolean": "boolean",
             "url": "string", "date": "string", "array": "array", "object": "object"}


def _content_summary(html: str, limit: int = 4000) -> str:
    """Strip boilerplate tags + extract visible text, truncated."""
    if not html:
        return ""
    cleaned = re.sub(
        rf"<(?:{_BOILERPLATE})\b[^>]*>.*?</(?:{_BOILERPLATE})>",
        " ", html, flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _strip_fences(text: str) -> str:
    """Strip ``` / ```json fences GLM sometimes wraps JSON in."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if text[:4].lower() == "json":
        text = text[4:].lstrip()
    return text


def _build_flat_schema(fields: list[dict]) -> dict:
    """Build a flat JSON Schema from a field list (fallback)."""
    props = {}
    required = []
    for f in fields:
        ft = f.get("type", "text")
        prop: dict[str, Any] = {"type": _TYPE_MAP.get(ft, "string")}
        if ft == "url":
            prop["format"] = "uri"
        elif ft == "date":
            prop["format"] = "date"
        props[f["name"]] = prop
        if f.get("required"):
            required.append(f["name"])
    return {"type": "object", "properties": props, "required": required}


# ── LLM prompt ───────────────────────────────────────────────────────────────
_DISCOVERY_PROMPT = """\
You are a field-discovery engine for a web scraper. Given ONE rendered web page's
structured data (JSON-LD), meta tags, and a truncated visible-content summary, your
job is to list EVERY data field that could be reliably extracted from pages like
this one. You are NOT writing selectors — only field NAMES, TYPES, nesting, and
where the signal comes from.

## What to return
A single JSON object (NO markdown, NO ``` fences, NO prose before or after):
{
  "content_type": "product" | "job_posting" | "article" | "forum_thread" | "serp" | "page_content",
  "fields": [
    {"name": "<snake_case>", "type": "text|number|boolean|url|date|array|object",
     "source": "jsonld|meta|content", "required": true|false, "description": "<short>"}
  ],
  "json_schema": {"type":"object","properties":{...},"required":[...]},
  "model_notes": "<one line>"
}

## Rules
1. Derive fields PRIMARILY from JSON-LD. Map property names to snake_case:
   name→title, offers.price→price, offers.priceCurrency→currency,
   offers.availability→availability, sku→sku, brand.name→brand,
   aggregateRating.ratingValue→rating, datePublished→publish_date,
   author.name→author, hiringOrganization.name→company,
   jobLocation.address.addressLocality→location.
2. Add fields from META tags JSON-LD lacks (og:title, og:image, product:price:amount).
3. Add fields from CONTENT that neither expose (specs table → "specs" as array/object).
4. NESTING: when source data nests (offers.{price,availability}, address.{city,zip},
   variants:[{size,color}]), emit a parent field type "object"/"array" AND reflect
   the nesting in json_schema.properties. Do NOT flatten.
5. Always include "url" (type url, required true).
6. type must be one of: text, number, boolean, url, date, array, object.
7. Output ONLY the JSON object.

## Example (product page)
Input JSON-LD: [{"@type":"Product","name":"Air Max 90","sku":"NW123","brand":{"name":"Nike"},"offers":{"price":129.99,"priceCurrency":"USD","availability":"https://schema.org/InStock"},"aggregateRating":{"ratingValue":4.5,"reviewCount":128}}]
Output:
{"content_type":"product","fields":[{"name":"title","type":"text","source":"jsonld","required":true,"description":"Product name"},{"name":"sku","type":"text","source":"jsonld","required":false,"description":"SKU"},{"name":"brand","type":"text","source":"jsonld","required":false,"description":"Brand name"},{"name":"price","type":"number","source":"jsonld","required":true,"description":"Price"},{"name":"currency","type":"text","source":"jsonld","required":true,"description":"Currency"},{"name":"availability","type":"text","source":"jsonld","required":true,"description":"Stock status"},{"name":"rating","type":"number","source":"jsonld","required":false,"description":"Rating"},{"name":"review_count","type":"number","source":"jsonld","required":false,"description":"Review count"},{"name":"url","type":"url","source":"content","required":true,"description":"Page URL"}],"json_schema":{"type":"object","properties":{"title":{"type":"string"},"sku":{"type":"string"},"brand":{"type":"string"},"price":{"type":"number"},"currency":{"type":"string"},"availability":{"type":"string"},"rating":{"type":"number"},"review_count":{"type":"number"},"url":{"type":"string"}},"required":["title","price","currency","availability","url"]},"model_notes":"Rich Product JSON-LD."}

## Now analyze THIS page
URL: {url}
PAGE TITLE: {title}

JSON-LD BLOCKS:
{jsonld_block}

META TAGS (filtered):
{meta_block}

VISIBLE CONTENT SUMMARY (truncated, {content_len} chars):
{content_summary}

Return ONLY the JSON object."""


def _build_prompt(url: str, title: str, jsonld: list, meta: dict, content: str) -> str:
    jl = json.dumps(jsonld[:8], ensure_ascii=False, default=str)[:12000] or "(none found)"
    interesting = {k: v[:120] for k, v in meta.items()
                   if k.startswith(("og:", "twitter:", "article:", "product:"))
                   or k in ("availability", "description", "keywords")}
    mb = json.dumps(interesting, ensure_ascii=False)[:3000] or "(none found)"
    return _DISCOVERY_PROMPT.format(
        url=url, title=title or "(unknown)",
        jsonld_block=jl, meta_block=mb,
        content_len=len(content), content_summary=content,
    )


# ── response parsing ─────────────────────────────────────────────────────────
def _parse_llm_response(text: str) -> dict | None:
    """Parse the LLM's JSON response into {fields, json_schema, content_type}."""
    try:
        result = json.loads(_strip_fences(text))
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("fields")
    if not isinstance(raw, list) or not raw:
        return None
    fields = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "")).strip().lower().replace("-", "_").replace(" ", "_")
        if not name or not re.match(r"^[a-z_][a-z0-9_]*$", name):
            continue
        ft = str(f.get("type", "text")).lower()
        fields.append({
            "name": name, "type": ft if ft in _VALID_TYPES else "text",
            "source": str(f.get("source", "")).lower() or "content",
            "required": bool(f.get("required", False)),
            "description": str(f.get("description", ""))[:200],
        })
    if not fields:
        return None
    schema = result.get("json_schema")
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        schema = _build_flat_schema(fields)
    return {
        "fields": [f["name"] for f in fields],
        "json_schema": schema,
        "content_type": str(result.get("content_type", "")) or "page_content",
    }


# ── deterministic JSON-LD fallback ───────────────────────────────────────────
def _fallback_jsonld(jsonld_blocks: list[dict]) -> dict:
    """Deterministic field discovery from JSON-LD @type → content-type core fields."""
    from src.content_types import CONTENT_TYPES
    type_map: dict[str, str] = {}
    for ct_name, cfg in CONTENT_TYPES.items():
        for jt in getattr(cfg, "jsonld_types", ()):
            type_map[jt.lower()] = ct_name
    best_ct = ""
    for block in jsonld_blocks:
        t = block.get("@type", "")
        if isinstance(t, list):
            for sub in t:
                ct = type_map.get(str(sub).lower())
                if ct:
                    best_ct = ct
                    break
        else:
            ct = type_map.get(str(t).lower())
            if ct:
                best_ct = ct
    if not best_ct:
        return {"fields": [], "json_schema": None, "source": "jsonld", "content_type": ""}
    cfg = CONTENT_TYPES.get(best_ct)
    if not cfg:
        return {"fields": [], "json_schema": None, "source": "jsonld", "content_type": best_ct}
    names = list(getattr(cfg, "core_field_names", ()))
    fields = [{"name": n, "type": "text", "source": "jsonld", "required": True, "description": ""}
              for n in names]
    return {
        "fields": names,
        "json_schema": _build_flat_schema(fields),
        "source": "jsonld",
        "content_type": best_ct,
    }


# ── public API ───────────────────────────────────────────────────────────────
def discover_fields_from_html(*, url: str, html: str, title: str = "",
                              llm_timeout: int = 15) -> dict:
    """Discover extractable fields from a rendered page via LLM.

    Returns ``{fields: list[str], json_schema: dict|None, source: "llm"|"jsonld",
    content_type: str}``. Falls back to deterministic JSON-LD parsing on LLM
    failure. Never raises (returns empty fields on total failure).
    """
    jsonld = extract_jsonld(html)
    meta = extract_meta_tags(html)
    content = _content_summary(html)
    if not title:
        title = extract_title(html)

    # Try LLM first.
    try:
        from agents.llm import get_small_llm
        from langchain_core.messages import HumanMessage
        llm = get_small_llm(temperature=0.0, timeout=llm_timeout)
        prompt = _build_prompt(url, title, jsonld, meta, content)
        resp = llm.invoke([HumanMessage(content=prompt)])
        parsed = _parse_llm_response(resp.content if hasattr(resp, "content") else str(resp))
        if parsed and parsed["fields"]:
            parsed["source"] = "llm"
            return parsed
    except Exception:
        pass  # fall through to deterministic fallback

    # Fallback: deterministic JSON-LD.
    result = _fallback_jsonld(jsonld)
    if result["fields"]:
        return result

    # Total failure.
    return {"fields": [], "json_schema": None, "source": "none", "content_type": ""}
