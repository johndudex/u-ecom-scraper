"""Normalize raw product analysis data into a standard `fields` mapping.

Reads ``product_analysis.json`` (which may contain raw JSON-LD keys, Algolia
field names, or other platform-specific identifiers) and uses a focused LLM
call to map them to the standard output field names.  Always adds "direct"
fields (``url``, ``src_url``, ``status_code``, ``scraped_at``, ``remarks``)
that are set by the scraper itself.

If the ``fields`` dict already covers all core fields, the LLM call is skipped.
"""

import json
import logging
import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

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

CORE_FIELDS = [
    "title",
    "price",
    "availability",
    "original_price",
    "currency",
    "url",
    "src_url",
]

DEFAULT_MAPPING_PROMPT = f"""You are a field name mapper for a scraper pipeline.
Your ONLY job is to map raw field names found in data to standard output field names.

## Standard Output Fields
{chr(10).join(f"- {f}" for f in CORE_FIELDS)}
- description: Product description text
- brand: Brand name
- images: Product image URL(s)
- sku: Stock keeping unit
- category: Product category

## Rules
1. Output ONLY a JSON object. No explanation, no markdown, no code fences.
2. Each key is a standard output field name.
3. Each value is an object with: "method" (extraction method used), "selector" (path/key in the raw data to find this field), "examples" (one example value).
4. Only include fields that have corresponding data in the raw input.
5. If a raw field doesn't map to any standard field, include it with its original name and note it as "extra".
6. Be precise with "selector" — use the exact key path (e.g. "jsonld_extraction.product_data.name" or "algolia_fields.primary.Name").
7. "original_price" should only be mapped if the raw data explicitly has a separate original/was/compare-at price. Do NOT map the regular price to original_price."""

MAPPING_USER_PROMPT_TEMPLATE = """## Raw Product Analysis Data

{raw_data_sections}

## Existing Fields (if any)
{existing_fields}

Map the raw data to standard output fields. Output ONLY a JSON object."""


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


def _build_mapping_prompt(content_type_config: dict) -> str:
    if content_type_config and "fields" in content_type_config:
        fields_list = content_type_config["fields"]
        field_lines = []
        for f in fields_list:
            line = f"- {f['name']}: {f['label']}"
            if f.get("required"):
                line += " (required)"
            field_lines.append(line)
        field_section = "\n".join(field_lines)
        return (
            "You are a field name mapper for a scraper pipeline.\n"
            "Your ONLY job is to map raw field names found in data to standard output field names.\n\n"
            f"## Standard Output Fields\n{field_section}\n\n"
            "## Rules\n"
            "1. Output ONLY a JSON object. No explanation, no markdown, no code fences.\n"
            "2. Each key is a standard output field name.\n"
            '3. Each value is an object with: "method" (extraction method used), "selector" (path/key in the raw data to find this field), "examples" (one example value).\n'
            "4. Only include fields that have corresponding data in the raw input.\n"
            '5. If a raw field doesn\'t map to any standard field, include it with its original name and note it as "extra".\n'
            '6. Be precise with "selector" — use the exact key path.\n'
        )
    return DEFAULT_MAPPING_PROMPT


def _core_fields_present(fields: dict, core: list[str]) -> bool:
    if not fields:
        return False
    present = set(fields.keys())
    return len(core) <= len(present & set(core))


def _build_raw_data_summary(analysis: dict) -> str:
    sections = []

    jsonld = analysis.get("jsonld_extraction", {}).get("product_data", {})
    if jsonld:
        sections.append(f"### JSON-LD Product Data\n```json\n{json.dumps(jsonld, indent=2, ensure_ascii=False)[:2000]}\n```")

    extraction_methods = analysis.get("extraction_methods", {})
    if extraction_methods:
        sections.append(f"### Extraction Methods\n```json\n{json.dumps(extraction_methods, indent=2, ensure_ascii=False)[:1000]}\n```")

    algolia_primary = analysis.get("algolia_fields", {}).get("primary", {})
    if algolia_primary:
        sections.append(f"### Algolia Primary Fields\n```json\n{json.dumps(algolia_primary, indent=2, ensure_ascii=False)[:1500]}\n```")

    algolia_descriptive = analysis.get("algolia_fields", {}).get("descriptive", {})
    if algolia_descriptive:
        sections.append(f"### Algolia Descriptive Fields\n```json\n{json.dumps(algolia_descriptive, indent=2, ensure_ascii=False)[:1500]}\n```")

    api_fields = analysis.get("api_fields", {})
    if api_fields:
        sections.append(f"### API Fields\n```json\n{json.dumps(api_fields, indent=2, ensure_ascii=False)[:1500]}\n```")

    return "\n".join(sections) if sections else "(no raw data sections found)"


def _call_llm_for_mapping(raw_data: str, existing_fields: dict, mapping_prompt: str) -> dict | None:
    from ..llm import get_small_llm

    llm = get_small_llm(temperature=0.0)
    messages = [
        SystemMessage(content=mapping_prompt),
        HumanMessage(content=MAPPING_USER_PROMPT_TEMPLATE.format(
            raw_data_sections=raw_data,
            existing_fields=json.dumps(existing_fields, indent=2, ensure_ascii=False) if existing_fields else "{}",
        )),
    ]

    try:
        response = llm.invoke(messages)
        content = response.content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:])
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
        logger.warning("normalize_fields: LLM returned non-dict JSON: %s", type(parsed))
    except json.JSONDecodeError as exc:
        logger.warning("normalize_fields: LLM returned invalid JSON: %s", exc)
    except Exception as exc:
        logger.error("normalize_fields: LLM mapping call failed: %s", exc)

    return None


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


def normalize_fields(state: ScrapeState) -> dict[str, Any]:
    slug = state["site_slug"]
    analysis = _load_analysis(slug)

    content_type_config = state.get("content_type_config", {})
    core = CORE_FIELDS
    if content_type_config and "core_field_names" in content_type_config:
        core = list(content_type_config["core_field_names"])
    mapping_prompt = _build_mapping_prompt(content_type_config)

    if analysis is None:
        logger.error("normalize_fields: analysis not found for %s", slug)
        return {
            "current_phase": "normalize_fields",
            "phases_completed": state.get("phases_completed", []) + ["normalize_fields"],
        }

    existing_fields = analysis.get("fields", {})
    if not isinstance(existing_fields, dict):
        existing_fields = {}

    if _core_fields_present(existing_fields, core):
        logger.info("normalize_fields: core fields already present, adding direct fields only")
        merged = _merge_fields(existing_fields, {}, DIRECT_FIELDS)
        analysis["fields"] = merged
        _save_analysis(slug, analysis)
        return {
            "product_analysis": analysis,
            "content_analysis": analysis,
            "fields_extracted": list(merged.keys()),
            "current_phase": "normalize_fields",
            "phases_completed": state.get("phases_completed", []) + ["normalize_fields"],
        }

    raw_summary = _build_raw_data_summary(analysis)
    logger.info("normalize_fields: calling LLM to map raw fields for %s", slug)

    mapped = _call_llm_for_mapping(raw_summary, existing_fields, mapping_prompt)

    if mapped is None:
        logger.warning("normalize_fields: LLM mapping failed, using existing + direct fields only")
        mapped = {}

    merged = _merge_fields(existing_fields, mapped, DIRECT_FIELDS)
    analysis["fields"] = merged
    _save_analysis(slug, analysis)

    logger.info(
        "normalize_fields: fields after mapping: %s",
        ", ".join(sorted(merged.keys())),
    )

    return {
        "product_analysis": analysis,
        "content_analysis": analysis,
        "fields_extracted": list(merged.keys()),
        "current_phase": "normalize_fields",
        "phases_completed": state.get("phases_completed", []) + ["normalize_fields"],
    }
