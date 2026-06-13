"""Routing function after the cleanup phase.

[B3] Skips skill_learner if the scrape failed or this is a re-scrape loop.
"""

import logging

from ..state import ScrapeState

logger = logging.getLogger(__name__)


def route_after_cleanup(state: ScrapeState) -> str:
    exec_status = state.get("execution_status", "FAILED")
    reanalyze = state.get("reanalyze_count", 0)

    if exec_status == "SUCCESS" and reanalyze == 0:
        logger.info("route_after_cleanup: success, routing to skill_learner")
        return "skill_learner"

    logger.info(
        "route_after_cleanup: skipping skill_learner (status=%s, reanalyze=%d)",
        exec_status,
        reanalyze,
    )
    return "__end__"
