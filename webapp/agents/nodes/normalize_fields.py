"""Normalize raw product analysis data into a standard `fields` mapping.

Reads ``product_analysis.json`` (which may contain raw JSON-LD keys, Algolia
field names, or other platform-specific identifiers) and maps them to the
standard output field names via a deterministic resolver.  Always adds "direct"
fields (``url``, ``src_url``, ``status_code``, ``scraped_at``, ``remarks``)
that are set by the scraper itself.

Job content types use ``src.job_fields`` (a generic resolver). Other content
types fall back to the existing analysis ``fields`` dict + the direct fields.
"""

import json
import logging
import os
from typing import Any

from ..state import ScrapeState

logger = logging.getLogger(__name__)

CORE_FIELDS = [
    "title",
    "price",
    "availability",
    "original_price",
    "currency",
    "url",
    "src_url",
]

DIRECT_FIELDS = {
    "url": {
        "method": "direct",
        "selector": "product_url from scraper input",
        "notes": "Direct URL to the product page, passed from input.",
    },
    "src_url": {
        "method": "direct",
        "selector": "source URL from scraper input",
        "notes": "The source listing URL where the product was discovered.",
    },
    "status_code": {
        "method": "direct",
        "selector": "HTTP response status code",
        "notes": "Set by the scraper after fetching the page.",
    },
    "scraped_at": {
        "method": "direct",
        "selector": "current ISO-8601 timestamp",
        "notes": "Set by the scraper when extraction completes.",
    },
    "remarks": {
        "method": "direct",
        "selector": "set by scraper",
        "notes": "Notes or warnings from the extraction process.",
    },
}


def _get_project_root() -> str:
    try:
        from django.conf import settings
        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _load_analysis(slug: str) -> dict | None:
    root = _get_project_root()
    path = os.path.join(root, "workspace", slug, "product_analysis.json")
    if not os.path.isfile(path):
        path = os.path.join(root, "workspace", slug, "content_analysis.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("normalize_fields: cannot load product_analysis: %s", exc)
        return None


def _save_analysis(slug: str, analysis: dict) -> None:
    root = _get_project_root()
    path = os.path.join(root, "workspace", slug, "product_analysis.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(analysis, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error("normalize_fields: cannot save product_analysis: %s", exc)


def _merge_fields(existing: dict, mapped: dict, direct: dict) -> dict:
    merged = {}
    for name, info in direct.items():
        merged[name] = info

    if existing:
        for name, info in existing.items():
            if isinstance(info, dict) and (info.get("method") or info.get("selector")):
                merged[name] = info

    if mapped:
        for name, info in mapped.items():
            if isinstance(info, dict) and (info.get("method") or info.get("selector")):
                if name not in merged:
                    merged[name] = info

    return merged


def _prune_to_schema(merged: dict, state) -> dict:
    """If the user provided a custom schema (target_fields), prune the merged
    field map to schema ∪ bookkeeping (DIRECT_FIELDS). Prevents non-schema
    fields from flowing through to code_writer."""
    target_fields = state.get("target_fields") or []
    if not target_fields:
        return merged
    allowed = set(target_fields) | set(DIRECT_FIELDS.keys())
    pruned = {k: v for k, v in merged.items() if k in allowed}
    if len(pruned) < len(merged):
        logger.info(
            "normalize_fields: pruned %d → %d fields (custom schema)",
            len(merged), len(pruned),
        )
    return pruned


def _deterministic_job_mapping(analysis: dict, content_type_config: dict) -> dict:
    """Map job fields with the generic resolver (src.job_fields) — deterministic,
    no LLM.  Builds a sample list from the raw data sections in product_analysis
    (JSON-LD / API / Algolia), infers the best source path per field by coverage,
    and returns the same ``{field: {method, selector, examples}}`` shape the LLM
    mapper produces so downstream nodes are unchanged.
    """
    try:
        from src.job_fields import infer_field_map, apply_field_map
    except Exception as exc:
        logger.warning("normalize_fields: src.job_fields unavailable: %s", exc)
        return {}

    samples: list[dict] = []
    jd = (analysis.get("jsonld_extraction") or {}).get("product_data") or {}
    if isinstance(jd, dict) and jd:
        samples.append(jd)
    api = analysis.get("api_fields") or {}
    if isinstance(api, dict) and api:
        samples.append(api)
    alg = (analysis.get("algolia_fields") or {}).get("primary") or {}
    if isinstance(alg, dict) and alg:
        samples.append(alg)
    if not samples:
        return {}

    fmap = infer_field_map(samples, content_type_config)
    example = apply_field_map(samples[0], fmap, content_type_config) if samples else {}
    out: dict[str, dict] = {}
    for field, path in fmap.items():
        if path:
            out[field] = {
                "method": "resolver",
                "selector": path,
                "examples": str(example.get(field, ""))[:80],
            }
    return out


def _verify_mappings(slug: str, state: ScrapeState, analysis: dict) -> dict:
    """[T2.7] Exercise every mapped field source against ONE live render of the
    sample page (src.field_verification). Mutates analysis["fields"][*]
    (tested / resolved_value) and stores the summary at
    analysis["field_verification"] so code_writer sees VERIFIED mappings instead
    of guesses. Strictly additive: any failure returns the analysis unchanged.
    """
    try:
        from src.field_verification import verify_enabled, verify_field_mappings
    except Exception as exc:
        logger.debug("normalize_fields: field_verification unavailable: %s", exc)
        return {}
    if not verify_enabled():
        return {}
    try:
        analysis, summary = verify_field_mappings(slug, state, analysis)
        if summary:
            analysis["field_verification"] = summary
    except Exception as exc:
        logger.warning("normalize_fields: field verification errored (no-op): %s", exc)
        return {}
    return summary


def normalize_fields(state: ScrapeState) -> dict[str, Any]:
    slug = state["site_slug"]
    analysis = _load_analysis(slug)

    content_type_config = state.get("content_type_config", {})

    if analysis is None:
        logger.error("normalize_fields: analysis not found for %s", slug)
        return {}

    existing_fields = analysis.get("fields", {})
    if not isinstance(existing_fields, dict):
        existing_fields = {}

    # Job content type: use the deterministic field resolver.
    ct_name = (content_type_config or {}).get("content_type") or ""
    page_type = (state.get("page_type") or "").lower()
    if ct_name == "job_posting" or page_type.startswith("job"):
        job_mapped = _deterministic_job_mapping(analysis, content_type_config)
        if job_mapped:
            merged = _merge_fields(existing_fields, job_mapped, DIRECT_FIELDS)
            merged = _prune_to_schema(merged, state)
            analysis["fields"] = merged
            _verify_mappings(slug, state, analysis)
            _save_analysis(slug, analysis)
            logger.info(
                "normalize_fields: job fields mapped via resolver: %s",
                ", ".join(sorted(job_mapped.keys())),
            )
            return {
                "product_analysis": analysis,
                "content_analysis": analysis,
                "fields_extracted": list(merged.keys()),
            }

    # Non-job content types: keep the analyzer's existing field map + direct
    # fields (no LLM mapping). The analyzer is responsible for emitting the
    # field extraction map; we only augment with scraper-direct fields.
    merged = _merge_fields(existing_fields, {}, DIRECT_FIELDS)
    merged = _prune_to_schema(merged, state)
    analysis["fields"] = merged
    _verify_mappings(slug, state, analysis)
    _save_analysis(slug, analysis)

    logger.info(
        "normalize_fields: fields after merge: %s",
        ", ".join(sorted(merged.keys())),
    )

    return {
        "product_analysis": analysis,
        "content_analysis": analysis,
        "fields_extracted": list(merged.keys()),
    }
