"""Shared helpers for structured human-in-the-loop decisions.

Interrupt payloads use ``decisions`` instead of plain ``options``::

    {
        "reason": "field_confirmation",
        "message": "Review the sample extraction …",
        "decisions": [
            {"type": "approve", "label": "Approve"},
            {"type": "reject", "label": "Reject", "allow_feedback": True},
        ],
    }

The resume value passed to ``Command(resume=…)`` is::

    {"decision": "reject", "feedback": "prices look wrong"}

Backwards compatible: if ``decisions`` is missing the payload is treated as
legacy ``options`` and ``_parse_decision`` returns ``{"decision": choice}``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_RESPOND = "respond"

CANCEL_LABELS = frozenset({"Cancel", "Abort", "No", "stop", "Stop"})


def build_decisions(
    approve_label: str = "Approve",
    reject_label: str | None = "Reject",
    reject_with_feedback: bool = True,
    respond_label: str | None = None,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = [{"type": DECISION_APPROVE, "label": approve_label}]
    if reject_label:
        decisions.append({
            "type": DECISION_REJECT,
            "label": reject_label,
            "allow_feedback": reject_with_feedback,
        })
    if respond_label:
        decisions.append({
            "type": DECISION_RESPOND,
            "label": respond_label,
        })
    return decisions


def options_to_decisions(options: list[str]) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    for opt in options:
        if opt in CANCEL_LABELS:
            choices.append({"type": DECISION_REJECT, "label": opt, "allow_feedback": False})
        else:
            choices.append({"type": DECISION_APPROVE, "label": opt})
    return choices


def _parse_decision(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        if "decision" in response:
            return response
        choice = response.get("choice", response.get("response", ""))
        return {"decision": choice}
    if isinstance(response, str):
        return {"decision": response}
    return {"decision": "Cancel"}


def is_cancel(decision: dict[str, Any]) -> bool:
    d = decision.get("decision", "")
    return d in CANCEL_LABELS or d == DECISION_REJECT


def is_approve(decision: dict[str, Any]) -> bool:
    return decision.get("decision") == DECISION_APPROVE or decision.get("decision") not in CANCEL_LABELS
