"""Validate that the product analysis covers enough core fields."""

import json
import logging
import os
from typing import Any

from langgraph.types import Command

from ..decisions import options_to_decisions
from ..state import ScrapeState

logger = logging.getLogger(__name__)

MIN_COVERAGE = 0.80
MAX_VALIDATE_RETRIES = 2

CORE_FIELDS = {
    "title",
    "price",
    "availability",
    "original_price",
    "currency",
    "url",
    "src_url",
}


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _load_product_analysis(slug: str) -> dict | None:
    root = _get_project_root()
    path = os.path.join(root, "workspace", slug, "product_analysis.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("validate_coverage: cannot load product_analysis: %s", exc)
        return None


def _extract_covered_fields(analysis: dict) -> set[str]:
    covered: set[str] = set()

    fields_info = analysis.get("fields", {})
    if isinstance(fields_info, dict):
        for k, v in fields_info.items():
            if isinstance(v, dict) and (v.get("method") or v.get("selector")):
                covered.add(k)

    if not covered:
        has_raw = bool(
            analysis.get("jsonld_extraction") or analysis.get("algolia_fields")
        )
        if has_raw:
            logger.warning(
                "validate_coverage: 'fields' dict is empty but raw data exists. "
                "normalize_fields may have failed or been skipped.",
            )

    return covered


def validate_coverage(state: ScrapeState) -> Command:
    """Read ``product_analysis.json`` and check field coverage.

    * Coverage >= 80% of core fields → continue to ``code_writer``.
    * Coverage < 80% → interrupt for human decision (HIP #5).
    """
    slug = state["site_slug"]
    skip = state.get("skip_code_generation", False)

    if skip:
        logger.info("validate_coverage: skipping (code generation already done)")
        return Command(goto="code_tester")

    analysis = _load_product_analysis(slug)
    if analysis is None:
        product_retries = state.get("product_analysis_retries", 0) + 1
        logger.error(
            "validate_coverage: no product_analysis.json found (attempt %d), interrupting",
            product_retries,
        )
        if product_retries >= MAX_VALIDATE_RETRIES:
            logger.warning(
                "validate_coverage: max retries reached (%d), skipping to scraper_analyzer",
                product_retries,
            )
            return Command(
                update={"product_analysis_retries": product_retries},
                goto="scraper_analyzer",
            )
        options = ["Retry product analysis", "Continue without analysis", "Cancel"]
        return Command(
            update={
                "error_message": "product_analysis.json not found in workspace",
                "interrupt_reason": "low_coverage",
                "interrupt_message": "product_analysis.json not found in workspace. The product analyzer may not have completed successfully.",
                "interrupt_options": options,
                "interrupt_decisions": options_to_decisions(options),
                "product_analysis_retries": product_retries,
            },
            goto="human_approval",
        )

    state_update: dict[str, Any] = {"product_analysis": analysis}

    extracted_fields = _extract_covered_fields(analysis)

    covered = extracted_fields & CORE_FIELDS
    coverage_ratio = len(covered) / len(CORE_FIELDS) if CORE_FIELDS else 1.0

    logger.info(
        "validate_coverage: covered %d/%d core fields (%.0f%%) [all extracted: %s]",
        len(covered),
        len(CORE_FIELDS),
        coverage_ratio * 100,
        ", ".join(sorted(extracted_fields)) if extracted_fields else "(none)",
    )

    missing = CORE_FIELDS - covered
    state_update["fields_extracted"] = list(extracted_fields)

    if coverage_ratio < MIN_COVERAGE:
        logger.info("validate_coverage: low coverage, missing fields: %s", missing)
        missing_str = ", ".join(sorted(missing)) if missing else "(unknown)"
        covered_str = ", ".join(sorted(covered)) if covered else "(none)"
        options = [
            "Continue anyway",
            "Retry product analysis",
            "Cancel",
        ]
        return Command(
            update={
                **state_update,
                "interrupt_reason": "low_coverage",
                "interrupt_message": (
                    f"Field coverage is low: {len(covered)}/{len(CORE_FIELDS)} core fields covered "
                    f"({coverage_ratio:.0%}). "
                    f"Covered: {covered_str}. "
                    f"Missing: {missing_str}."
                ),
                "interrupt_options": options,
                "interrupt_decisions": options_to_decisions(options),
            },
            goto="human_approval",
        )

    return Command(
        update=state_update,
        goto="scraper_analyzer",
    )
