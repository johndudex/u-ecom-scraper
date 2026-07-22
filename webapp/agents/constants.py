"""Shared constants for the LangGraph scraping pipeline.

This module must NEVER import from any other module in the agents package
to avoid circular import risks.
"""

# Raised 3 → 6: complex sites need a strategy cascade (try http → playwright → api)
# plus in-strategy fixes/re-maps. Bounded by the strategy space (≤4 strategies) +
# MAX_REMAPS, so it can't loop forever. Each strategy switch records into
# state.strategies_tried so a failed strategy is never re-picked.
MAX_TEST_RETRIES: int = 2  # was 6 — cap the codegen loop so hard sites give up fast

MAX_VALIDATE_RETRIES: int = 2
# Cap on low-coverage (analysis exists but < MIN_COVERAGE) retry cycles through
# product_analyzer. The low-coverage interrupt path previously had NO cap
# (infinite loop risk). Mirrors MAX_VALIDATE_RETRIES / MAX_REANALYZE_CYCLES.
MAX_COVERAGE_RETRIES: int = 2

# How many times a failed field mapping can be re-done by product_analyzer
# before giving up (a mapping failure routes back to product_analyzer instead
# of code_writer — see route_after_testing). Re-maps share the test_retry_count
# budget (each re-map cycle goes through code_writer), so this is an additional
# cap on the mapping-specific recovery path.
MAX_REMAPS: int = 2

FINAL_RETRY_SENTINEL: int = 99

FINAL_RETRY_FAILED: str = "final_retry_failed"

DEAD_STATUS_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308, 404, 410, 451})
