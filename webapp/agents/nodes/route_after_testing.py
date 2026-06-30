"""Routing function after the code-tester phase.

[ADP #6/#7 / HIP #6 / G2]
"""

import logging

from ..state import ScrapeState

logger = logging.getLogger(__name__)

MIN_CONFIDENCE_PASS = 0.85
MIN_CONFIDENCE_PARTIAL = 0.5
MAX_TEST_AUTO_RETRIES = 2

DEAD_STATUS_CODES = {301, 302, 303, 307, 308, 404, 410, 451}

SOFT_404_MARKERS = (
    "soft 404",
    "product not found",
    "no longer available",
    "discontinued",
    "not a product page",
)

FINAL_RETRY_SENTINEL = 99


def _is_dead_product(p: dict) -> bool:
    status = p.get("status_code", 200)
    if status in DEAD_STATUS_CODES:
        return True
    remarks = (p.get("remarks") or "").lower()
    if any(marker in remarks for marker in SOFT_404_MARKERS):
        return True
    return False


def _scraper_produced_valid_output(state: ScrapeState) -> bool:
    report = state.get("test_report") or {}
    sample_products = report.get("sample_products") or []
    if not sample_products:
        return False
    live_products = [p for p in sample_products if not _is_dead_product(p)]
    valid = [p for p in live_products if p.get("title") and p.get("price")]
    return len(valid) > 0


def route_after_testing(state: ScrapeState) -> str:
    report = state.get("test_report")
    retry_count = state.get("test_retry_count", 0)
    is_final_attempt = retry_count == FINAL_RETRY_SENTINEL

    if not report:
        if is_final_attempt:
            logger.error(
                "route_after_testing: FINAL attempt produced no test_report → cleanup"
            )
            return "cleanup"
        if retry_count < MAX_TEST_AUTO_RETRIES:
            logger.warning(
                "route_after_testing: no test_report, retry %d/%d via scraper_analyzer",
                retry_count + 1,
                MAX_TEST_AUTO_RETRIES + 1,
            )
            return "scraper_analyzer"
        logger.error(
            "route_after_testing: no test_report after %d retries → cleanup",
            retry_count,
        )
        return "cleanup"

    assessment = report.get("overall_assessment", "FAIL")
    try:
        confidence = float(report.get("confidence_score", 0.0))
    except (ValueError, TypeError):
        confidence = 0.0
    issues = report.get("issues", [])
    high_severity = any(i.get("severity") == "high" for i in issues)

    if assessment == "PASS" and confidence >= MIN_CONFIDENCE_PASS and not high_severity:
        logger.info("route_after_testing: PASS (confidence=%.2f)", confidence)
        return "field_confirmation"

    if is_final_attempt:
        logger.error(
            "route_after_testing: FINAL attempt FAILED (assessment=%s, confidence=%.2f) "
            "→ cleanup",
            assessment,
            confidence,
        )
        return "cleanup"

    if retry_count < MAX_TEST_AUTO_RETRIES:
        logger.info(
            "route_after_testing: %s (confidence=%.2f, high_severity=%s), "
            "retry %d/%d via scraper_analyzer",
            assessment,
            confidence,
            high_severity,
            retry_count + 1,
            MAX_TEST_AUTO_RETRIES + 1,
        )
        return "scraper_analyzer"

    if confidence >= MIN_CONFIDENCE_PARTIAL and _scraper_produced_valid_output(state):
        logger.warning(
            "route_after_testing: retries exhausted (count=%d, assessment=%s, "
            "confidence=%.2f) → field_confirmation (partial output with valid products)",
            retry_count,
            assessment,
            confidence,
        )
        return "field_confirmation"

    logger.warning(
        "route_after_testing: retries exhausted (count=%d, assessment=%s) "
        "→ human_approval",
        retry_count,
        assessment,
    )

    return "human_approval"
