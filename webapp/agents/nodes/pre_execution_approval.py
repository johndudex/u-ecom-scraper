"""Pre-execution approval node.

[HIP #8] Interrupts the user before running the full scraper:
\"Ready to scrape ~N products (estimated X min). Proceed?\"
"""

import json
import logging
import os

from langgraph.types import Command, interrupt

from ..decisions import _parse_decision, build_decisions, is_cancel
from ..state import ScrapeState

logger = logging.getLogger(__name__)


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def pre_execution_approval(state: ScrapeState) -> Command:
    if state.get("sample_only", False):
        logger.info("pre_execution_approval: skipping execution (sample_only mode)")
        return Command(goto="cleanup")

    slug = state["site_slug"]
    product_count = state.get("product_count", 0)

    input_path = os.path.join(_get_project_root(), "workspace", slug, "input_urls.json")
    estimated_count = product_count
    try:
        with open(input_path) as fh:
            data = json.load(fh)
            estimated_count = len(data.get("urls", []))
    except Exception:
        pass

    human_response = interrupt(
        {
            "reason": "pre_execution",
            "message": (
                f"Ready to scrape ~{estimated_count} products from '{slug}'. "
                "Proceed with the full extraction?"
            ),
            "estimated_products": estimated_count,
            "decisions": build_decisions(
                approve_label="Proceed",
                reject_label="Cancel",
                reject_with_feedback=False,
            ),
        }
    )

    decision = _parse_decision(human_response)

    if not is_cancel(decision):
        logger.info("pre_execution_approval: user approved execution")
        return Command(goto="run_execution")

    logger.info("pre_execution_approval: user cancelled execution")
    return Command(
        update={"execution_status": "FAILED"},
        goto="cleanup",
    )
