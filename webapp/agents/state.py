"""LangGraph state definition for the Universal Scraper graph."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def _last_write_wins(_old: Any, new: Any) -> Any:
    return new


class ScrapeState(TypedDict, total=False):
    """Central state flowing through every node in the scraping graph.

    All fields are optional (total=False) so nodes only touch the keys
    they need while the rest carry through untouched.
    """

    # ── Input ──────────────────────────────────────────────────────────
    job_id: int
    url: str
    sample_url: Optional[str]
    product_url: Optional[str]
    currency: str
    sample_only: bool
    rescrape: bool

    # ── Content type ──────────────────────────────────────────────────
    page_type: str
    input_mode: str
    site_type: str
    content_type_config: dict[str, Any]
    search_criteria: str
    output_schema: dict[str, Any]

    # ── Tracker ────────────────────────────────────────────────────────
    site_slug: str
    site_name: str
    site_status: str

    # ── Resume / skip flags (for resuming an in-progress job) ───────────
    skip_site_analysis: bool
    skip_content_analysis: bool
    skip_product_analysis: bool
    skip_code_generation: bool

    # ── Phase bookkeeping ───────────────────────────────────────────────
    current_phase: Annotated[str, _last_write_wins]
    phases_completed: list[str]

    # ── Retry counters ─────────────────────────────────────────────────
    site_analysis_retries: int
    content_analysis_retries: int
    product_analysis_retries: int
    test_retry_count: int
    reanalyze_count: int
    budget_retry_count: int
    budget_retry_summary: str

    # ── Phase artifacts (JSON / code produced by each phase) ────────────
    site_analysis: Annotated[dict[str, Any], _last_write_wins]
    content_analysis: Annotated[dict[str, Any], _last_write_wins]
    product_analysis: Annotated[dict[str, Any], _last_write_wins]
    scraper_analysis: Annotated[dict[str, Any], _last_write_wins]
    scraper_code: Annotated[str, _last_write_wins]
    input_urls: Annotated[list[str], _last_write_wins]
    test_report: Annotated[dict[str, Any], _last_write_wins]
    cleanup_report: Annotated[dict[str, Any], _last_write_wins]
    learning_report: Annotated[dict[str, Any], _last_write_wins]
    nav_learning_report: Annotated[dict[str, Any], _last_write_wins]
    navigation_analysis: Annotated[dict[str, Any], _last_write_wins]

    # ── Probe cache ────────────────────────────────────────────────────
    probe_result: Annotated[Optional[dict[str, Any]], _last_write_wins]
    probe_url: Annotated[str, _last_write_wins]

    # ── Execution metadata ─────────────────────────────────────────────
    execution_status: Annotated[str, _last_write_wins]
    output_file: Annotated[str, _last_write_wins]
    item_count: int
    product_count: int
    scraping_method: Annotated[str, _last_write_wins]
    platform: Annotated[str, _last_write_wins]
    fields_extracted: Annotated[list[str], _last_write_wins]

    # ── Human-in-the-loop ───────────────────────────────────────────────
    interrupt_reason: Annotated[str, _last_write_wins]
    interrupt_message: Annotated[str, _last_write_wins]
    interrupt_options: Annotated[list[str], _last_write_wins]
    interrupt_decisions: Annotated[list[dict[str, Any]], _last_write_wins]
    human_response: Optional[dict[str, Any]]
    human_feedback: Annotated[str, _last_write_wins]

    # ── Routing decisions (set by routing nodes, read by conditional edges) ─
    next_node_after_testing: Annotated[str, _last_write_wins]
    next_node_after_cleanup: Annotated[str, _last_write_wins]

    # ── Navigation ──────────────────────────────────────────────────────
    navigation_findings: Annotated[Optional[dict[str, Any]], _last_write_wins]
    playwright_unavailable: bool

    # ── Error ───────────────────────────────────────────────────────────
    error_message: Annotated[str, _last_write_wins]

    # ── LangGraph message channel ───────────────────────────────────────
    messages: Annotated[list, add_messages]

    # ── Agent log accumulator ───────────────────────────────────────────
    agent_logs: Annotated[list[str], operator.add]
