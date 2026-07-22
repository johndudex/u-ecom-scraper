"""Validate that the product analysis covers enough core fields."""

import json
import logging
import os
from typing import Any

from langgraph.types import Command

from ..constants import MAX_COVERAGE_RETRIES, MAX_VALIDATE_RETRIES
from ..decisions import options_to_decisions
from ..state import ScrapeState

logger = logging.getLogger(__name__)

MIN_COVERAGE = 0.80

CORE_FIELDS = {
    "title",
    "price",
    "availability",
    "original_price",
    "currency",
    "url",
    "src_url",
}

DEFAULT_CORE_FIELDS = CORE_FIELDS


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
    if not os.path.isfile(path):
        path = os.path.join(root, "workspace", slug, "content_analysis.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("validate_coverage: cannot load analysis: %s", exc)
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
    """Read analysis and check field coverage.

    * Coverage >= 80% of core fields → continue to ``code_writer``.
    * Coverage < 80% → interrupt for human decision (HIP #5).
    """
    slug = state["site_slug"]
    skip = state.get("skip_code_generation", False)

    if skip:
        logger.info("validate_coverage: skipping (code generation already done)")
        return Command(goto="code_tester")

    content_type_config = state.get("content_type_config", {})
    # Resolve core fields from the job's page type so job/article/forum jobs are
    # evaluated against their OWN core fields, not the legacy product set.
    # Product-family page types keep DEFAULT_CORE_FIELDS exactly (no behavior
    # change for the dominant product use case).
    page_type = state.get("page_type", "product") or "product"
    core = DEFAULT_CORE_FIELDS
    if not page_type.startswith("product"):
        try:
            from src.content_types import get_content_type

            ct = get_content_type(page_type)
            if ct and getattr(ct, "core_fields", None):
                core = set(ct.core_fields)
        except Exception as exc:  # defensive — fall back to product fields
            logger.warning(
                "validate_coverage: could not resolve core fields for %s: %s",
                page_type,
                exc,
            )
    if content_type_config and "core_field_names" in content_type_config:
        core = set(content_type_config["core_field_names"])

    analysis = _load_product_analysis(slug)
    if analysis is None:
        product_retries = state.get("product_analysis_retries", 0) + 1
        logger.error(
            "validate_coverage: no analysis found (attempt %d), interrupting",
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
        options = ["Retry content analysis", "Continue without analysis", "Cancel"]
        return Command(
            update={
                "error_message": "analysis not found in workspace",
                "interrupt_reason": "low_coverage",
                "interrupt_message": "analysis not found in workspace. The content analyzer may not have completed successfully.",
                "interrupt_options": options,
                "interrupt_decisions": options_to_decisions(options),
                "product_analysis_retries": product_retries,
            },
            goto="human_approval",
        )

    state_update: dict[str, Any] = {
        "product_analysis": analysis,
        "content_analysis": analysis,
    }

    extracted_fields = _extract_covered_fields(analysis)

    covered = extracted_fields & core
    coverage_ratio = len(covered) / len(core) if core else 1.0

    logger.info(
        "validate_coverage: covered %d/%d core fields (%.0f%%) [all extracted: %s]",
        len(covered),
        len(core),
        coverage_ratio * 100,
        ", ".join(sorted(extracted_fields)) if extracted_fields else "(none)",
    )

    missing = core - covered
    state_update["fields_extracted"] = list(extracted_fields)

    # For SPA-over-API jobs, field mapping is done generically by src.job_fields
    # at SCRAPE time (the generated api_scraper calls ``map_jobs`` against the
    # real API items), not from this product_analysis (a single detail page).
    # So a low product_analysis coverage here is a false negative — don't pause;
    # let code_writer map fields via the resolver. product/article paths (no
    # api_endpoint) keep the coverage gate unchanged.
    nav_analysis = state.get("navigation_analysis") or {}
    api_ep = nav_analysis.get("api_endpoint")
    api_url = (api_ep or {}).get("url") if isinstance(api_ep, dict) else None
    if api_url:
        logger.info(
            "validate_coverage: backend JSON API discovered (%s); fields will be mapped "
            "generically by src.job_fields at scrape time — skipping product_analysis "
            "coverage gate (%.0f%%)",
            str(api_url)[:80],
            coverage_ratio * 100,
        )
        return Command(update=state_update, goto="scraper_analyzer")

    if coverage_ratio < MIN_COVERAGE:
        # Cap the retry loop (the missing-file path uses MAX_VALIDATE_RETRIES;
        # the low-coverage path previously had NO cap → infinite loop on
        # repeated "Retry"). On exhaustion, interrupt with coverage_exhausted
        # (human gate) rather than silently proceeding — the analysis EXISTS
        # but is incomplete, so silent proceed would ship a scraper that
        # drops core fields.
        coverage_retries = (state.get("coverage_retry_count", 0) or 0) + 1
        logger.info(
            "validate_coverage: low coverage (retry %d/%d), missing: %s",
            coverage_retries, MAX_COVERAGE_RETRIES, missing,
        )
        missing_str = ", ".join(sorted(missing)) if missing else "(unknown)"
        covered_str = ", ".join(sorted(covered)) if covered else "(none)"
        if coverage_retries >= MAX_COVERAGE_RETRIES:
            options = ["Continue anyway", "Abort"]
            return Command(
                update={
                    **state_update,
                    "coverage_retry_count": coverage_retries,
                    "interrupt_reason": "coverage_exhausted",
                    "interrupt_message": (
                        f"Field coverage still low after {coverage_retries} retry(s): "
                        f"{len(covered)}/{len(core)} core fields ({coverage_ratio:.0%}). "
                        f"Covered: {covered_str}. Missing: {missing_str}. "
                        f"Continue with partial coverage, or abort?"
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                },
                goto="human_approval",
            )
        options = [
            "Continue anyway",
            "Retry content analysis",
            "Cancel",
        ]
        return Command(
            update={
                **state_update,
                "coverage_retry_count": coverage_retries,
                # Set test_report.remediation so build_product_analyzer_message's
                # re-map directive fires on retry ("re-probe ONLY the failed
                # fields — do NOT re-analyze from scratch"). Without this, the
                # re-run gets a fresh OBJECTIVE, re-analyzes from scratch, and
                # destructively overwrites the detailed analysis. P0-16.
                "test_report": {"remediation": {"target": "mapping", "fields": sorted(missing)}},
                "interrupt_reason": "low_coverage",
                "interrupt_message": (
                    f"Field coverage is low (retry {coverage_retries}/{MAX_COVERAGE_RETRIES}): "
                    f"{len(covered)}/{len(core)} core fields covered "
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
