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

# ── Scraper CLI contract (discovery modes) ───────────────────────────────────
#
# Single source of truth for the discovery surface run_execution passes to a
# generated scraper and the vocabulary the deterministic guard + the
# prompt/message edits share (so they can never drift apart). Consumers:
#   * subagents.build_code_writer_message / build_code_tester_message (soft)
#   * run_execution._accepted_cli_flags / _filter_supported_args (hard stripper)
#   * run_execution.cli_contract_violation (FIX 1 deterministic check)
#   * route_after_testing._contract_bad routing
# Anti-drift: tests/test_cli_contract_prompt.py parses each template with the
# SAME ast walk _accepted_cli_flags uses and asserts per-family subsets.

CONTRACT_VIOLATION_MARKER: str = "CLI CONTRACT VIOLATION"
SCRAPER_ENV_LISTING: str = "SCRAPER_LISTING_URL"

NAV_INPUT_MODES: frozenset[str] = frozenset({"navigation", "list_page", "search_term"})
# Matches _select_template_file's api family (graph.py) — "api" / "internal_api".
API_STRATEGIES: frozenset[str] = frozenset({"api", "internal_api"})

# Base flags every strategy supports (testing + execution basics).
URL_LIST_FLAGS: tuple[str, ...] = ("--input", "--urls", "--sample", "--limit")

# Template-family declaration sets (verified by the anti-drift test):
#   playwright/http_navigation/navigation: full discovery surface
#   requests: --fresh-discovery + --discover-only, no --listing-url/--query
#   api family: --fresh-discovery only (no listing page — discovery IS the API)
#   ssr_div_list: --listing-url only (declares neither --fresh-discovery nor
#     --discover-only — do NOT advertise them)
#   UC/shopify: base only (no discovery flags)
NAV_FAMILY_FLAGS: tuple[str, ...] = URL_LIST_FLAGS + (
    "--fresh-discovery", "--discover-only", "--listing-url",
)
# --query is declared ONLY by http_navigation/navigation (the SFCC/search-form
# family). playwright search_term is env-driven (SCRAPER_LISTING_URL) —
# advertising --query for a playwright draft would demand a flag its own
# template lacks (anti-drift test enforces this).
SEARCH_QUERY_FAMILY_FLAGS: tuple[str, ...] = URL_LIST_FLAGS + (
    "--fresh-discovery", "--discover-only", "--query",
)
SEARCH_ENV_FAMILY_FLAGS: tuple[str, ...] = URL_LIST_FLAGS + (
    "--fresh-discovery", "--discover-only", "--listing-url",
)
API_NAV_FLAGS: tuple[str, ...] = URL_LIST_FLAGS + ("--fresh-discovery",)
SSR_NAV_FLAGS: tuple[str, ...] = URL_LIST_FLAGS + ("--listing-url",)


def required_cli_flags(input_mode: str, strategy: str = "") -> tuple[str, ...]:
    """Flags the generated scraper's argparse MUST declare for this job.

    Prompt-side enumeration (FIX 2) and the guard's bounce message use this.
    Strategy-aware: the set must stay ⊆ what the selected template family
    actually declares or the anti-drift test fails in CI.
    """
    im = (input_mode or "").strip().lower()
    st = (strategy or "").strip().lower()
    if im not in NAV_INPUT_MODES:
        return URL_LIST_FLAGS  # url_list and unknown modes: no discovery flags
    if st in API_STRATEGIES:
        return API_NAV_FLAGS
    if st == "ssr_div_list":
        return SSR_NAV_FLAGS
    if im == "search_term":
        if st in ("http_navigation", "http_requests", ""):
            # Known --query-declaring family (or unknown → the SFCC default).
            return SEARCH_QUERY_FAMILY_FLAGS
        # playwright family: env-driven search, --listing-url not --query
        return SEARCH_ENV_FAMILY_FLAGS
    return NAV_FAMILY_FLAGS
