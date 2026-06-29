"""Generic interrupt node for human-in-the-loop decisions.

Provides a reusable interrupt mechanism that any graph edge can target.
The node reads ``interrupt_reason``, ``interrupt_options`` and
``interrupt_decisions`` from state and calls ``langgraph.types.interrupt()``.
After the user responds, stores ``human_response``.

Auto-detects ``testing_exhausted`` when ``test_retry_count >= 3`` and no
explicit interrupt_reason is set by the upstream routing node.
"""

import logging

from langgraph.types import interrupt

from ..decisions import _parse_decision, options_to_decisions
from ..state import ScrapeState

logger = logging.getLogger(__name__)

MAX_TEST_RETRIES = 3


def human_approval(state: ScrapeState) -> dict:
    reason = state.get("interrupt_reason", "")
    options = state.get("interrupt_options", [])
    decisions = state.get("interrupt_decisions", [])
    custom_msg = state.get("interrupt_message", "")

    if not reason:
        test_retries = state.get("test_retry_count", 0)
        if test_retries >= MAX_TEST_RETRIES:
            reason = "testing_exhausted"
            assessment = "UNKNOWN"
            confidence = 0.0
            report = state.get("test_report") or {}
            if report:
                assessment = report.get("overall_assessment", "UNKNOWN")
                try:
                    confidence = float(report.get("confidence_score", 0.0))
                except (ValueError, TypeError):
                    pass
            custom_msg = (
                f"The scraper failed testing after {test_retries} retries "
                f"(last assessment: {assessment}, confidence: {confidence:.0%}). "
                "The scraper may produce incomplete or incorrect data.\n\n"
                "Choose:\n"
                "- **Continue anyway**: proceed to field confirmation\n"
                "- **Provide feedback for final retry**: describe what's wrong so the "
                "code-writer can re-code and run one final test. If that also fails, "
                "the job will end.\n"
                "- **Cancel**: abort the job"
            )
            options = [
                "Continue anyway",
                "Provide feedback for final retry",
                "Cancel",
            ]
            decisions = [
                {"type": "approve", "label": "Continue anyway"},
                {
                    "type": "approve",
                    "label": "Provide feedback for final retry",
                    "allow_feedback": True,
                },
                {"type": "reject", "label": "Cancel", "allow_feedback": False},
            ]
            logger.info(
                "human_approval: auto-detected testing_exhausted "
                "(retries=%d, assessment=%s, confidence=%.2f)",
                test_retries,
                assessment,
                confidence,
            )

    if not decisions and not options:
        logger.warning("human_approval: no decisions/options in state, using fallback")
        options = ["Continue", "Cancel"]
        decisions = [
            {"type": "approve", "label": "Continue"},
            {"type": "reject", "label": "Cancel"},
        ]

    if not decisions:
        decisions = options_to_decisions(options)

    if not reason:
        reason = "review"

    if custom_msg:
        message = custom_msg
    else:
        message = f"Review needed ({reason}). Continue to proceed, or Cancel to stop."

    payload = {
        "reason": reason,
        "message": message,
        "decisions": decisions,
    }

    response = interrupt(payload)

    decision = _parse_decision(response)
    feedback = decision.get("feedback", "")

    logger.info(
        "human_approval: resolved '%s' → %s (feedback: %s)",
        reason,
        decision.get("decision", "?"),
        feedback[:200] if feedback else "(none)",
    )

    return {
        "human_response": decision,
        "human_feedback": feedback,
        "interrupt_reason": reason,
    }
