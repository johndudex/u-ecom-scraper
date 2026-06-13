"""Validate the site analysis output and decide whether to continue.

Checks confidence score, mechanism availability, and routes accordingly.
"""

import json
import logging
import os
from typing import Any

from langgraph.types import Command

from ..decisions import options_to_decisions
from ..state import ScrapeState

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.7
MAX_VALIDATE_RETRIES = 2


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
    path = os.path.join(root, "workspace", slug, "site_analysis.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("validate_analysis: cannot load analysis: %s", exc)
        return None


def validate_analysis(state: ScrapeState) -> Command:
    """Read ``site_analysis.json`` from the workspace and validate it.

    * Confidence >= 0.7 → continue to ``product_analyzer``.
    * Multiple scraping mechanisms available → interrupt for human choice (HIP #4).
    * Low confidence (< 0.7) → interrupt for human decision (HIP #3).
    """
    slug = state["site_slug"]
    skip = state.get("skip_product_analysis", False)

    if skip:
        logger.info("validate_analysis: skipping (product analysis already done)")
        return Command(goto="scraper_analyzer")

    analysis = _load_analysis(slug)
    if analysis is None:
        site_retries = state.get("site_analysis_retries", 0) + 1
        logger.error(
            "validate_analysis: no analysis file found (attempt %d), interrupting",
            site_retries,
        )
        if site_retries >= MAX_VALIDATE_RETRIES:
            logger.warning(
                "validate_analysis: max retries reached (%d), skipping to scraper_analyzer",
                site_retries,
            )
            return Command(
                update={"site_analysis_retries": site_retries},
                goto="scraper_analyzer",
            )
        options = ["Retry site analysis", "Continue without analysis", "Cancel"]
        return Command(
            update={
                "error_message": "site_analysis.json not found in workspace",
                "interrupt_reason": "low_confidence",
                "interrupt_message": "site_analysis.json not found. The site analyzer may not have completed successfully.",
                "interrupt_options": options,
                "interrupt_decisions": options_to_decisions(options),
                "site_analysis_retries": site_retries,
            },
            goto="human_approval",
        )

    state_update: dict[str, Any] = {"site_analysis": analysis}

    confidence = float(
        analysis.get("confidence_score", analysis.get("confidence", 0.0))
    )
    mechanisms = analysis.get("scraping_mechanisms", [])
    primary_mechanism = analysis.get("scraping_mechanism", "")

    if not mechanisms and primary_mechanism:
        mechanisms = [primary_mechanism]

    if len(mechanisms) > 1:
        logger.info(
            "validate_analysis: %d mechanisms available, interrupting", len(mechanisms)
        )
        return Command(
            update={
                **state_update,
                "interrupt_reason": "choose_mechanism",
                "interrupt_message": (
                    f"Multiple scraping mechanisms detected: {', '.join(mechanisms)}. "
                    f"Choose which mechanism to use."
                ),
                "interrupt_options": mechanisms,
                "interrupt_decisions": options_to_decisions(mechanisms),
            },
            goto="human_approval",
        )

    if confidence < MIN_CONFIDENCE:
        logger.info("validate_analysis: low confidence (%.2f)", confidence)
        platform = analysis.get("platform", "unknown")
        primary = mechanisms[0] if mechanisms else "unknown"
        options = [
            "Continue anyway",
            "Retry with different approach",
            "Cancel",
        ]
        return Command(
            update={
                **state_update,
                "interrupt_reason": "low_confidence",
                "interrupt_message": (
                    f"Site analysis confidence is low: {confidence:.0%}. "
                    f"Platform: {platform}, Mechanism: {primary}. "
                    f"Consider retrying or proceeding with caution."
                ),
                "interrupt_options": options,
                "interrupt_decisions": options_to_decisions(options),
            },
            goto="human_approval",
        )

    logger.info("validate_analysis: passed (confidence=%.2f)", confidence)
    return Command(
        update=state_update,
        goto="product_analyzer",
    )
