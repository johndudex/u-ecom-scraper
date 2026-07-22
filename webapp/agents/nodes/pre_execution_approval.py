"""Pre-execution approval node (Wave 2 Cut 2: merged into field_confirmation).

Historically this was a SECOND consecutive interrupt fired after
``field_confirmation`` resolved — "Ready to scrape ~N items? Proceed?". That was
redundant with field_confirmation's own "Approve to proceed with the full
scrape" gate: two interrupts back-to-back for the same run/don't-run decision.

Wave 2 Cut 2 merged the two:
  * The item-count estimate (read from ``input_urls.json``) now appears in
    ``field_confirmation``'s interrupt message, alongside the sample fields.
  * ``field_confirmation`` routes straight to ``run_execution`` on approve
    (and still loops back to ``product_analyzer`` on reject, preserving the
    cancel/re-analyze path).
  * This node is NO LONGER registered in the graph (``graph.build_scrape_graph``
    does not ``add_node`` it).

The function is kept as a pass-through so existing imports resolve — notably
``webapp/agents/nodes/__init__.py`` still re-exports it, and an in-flight job
whose checkpoint references this node can still load the symbol. It is not
reached by fresh runs.
"""

import logging

from langgraph.types import Command

from ..state import ScrapeState

logger = logging.getLogger(__name__)


def pre_execution_approval(state: ScrapeState) -> Command:
    """Pass-through — the execution gate is now ``field_confirmation``.

    If ever invoked directly (legacy checkpoint / playground), route straight
    to ``run_execution``. ``sample_only`` jobs already bypassed this node via
    ``field_confirmation`` and continue to do so.
    """
    logger.info(
        "pre_execution_approval: pass-through (merged into field_confirmation) — "
        "routing to run_execution"
    )
    return Command(goto="run_execution")
