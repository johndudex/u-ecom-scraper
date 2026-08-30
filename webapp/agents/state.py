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
    # Full re-run (intake/job_detail button): bypass the selective rescrape diff
    # entirely — wipe the stale workspace + analysis archive and regenerate EVERY
    # phase, even when a prior completed job would normally let stages be skipped.
    force_full: bool
    # Intake-UI jobs skip all human-approval gates (run unattended).
    skip_approvals: bool
    # Dagster conversion is opt-in (intake checkbox / partner-API flag). False
    # (default) → dagster_converter short-circuits and no step is seeded.
    dagster_enabled: bool
    # Intake-UI schema knobs. target_fields ENFORCES the output schema (the
    # agents + validate_coverage + normalize_fields read these). Declared here
    # so LangGraph persists them in the graph state (otherwise they'd be stripped).
    target_fields: list
    scope: str
    scope_value: str
    user_notes: str

    # ── Content type ──────────────────────────────────────────────────
    page_type: str
    input_mode: str
    site_type: str
    content_type_config: dict[str, Any]
    search_criteria: str
    output_schema: dict[str, Any]
    # Nested schema tree {field: {type, children}} from job.schema_text (only when
    # the user supplied a nested JSON Schema). None/empty for manual field-chip /
    # flat-schema / legacy jobs → flat pipeline path. Advisory to product_analyzer
    # / code_writer; drives recursive output pruning. See src.schema_validation.
    nested_schema: dict[str, Any]

    # ── Tracker ────────────────────────────────────────────────────────
    site_slug: str
    site_name: str
    site_status: str

    # ── Resume / skip flags (for resuming an in-progress job) ───────────
    skip_site_analysis: bool
    skip_product_analysis: bool
    skip_code_generation: bool

    # ── Retry counters ─────────────────────────────────────────────────
    site_analysis_retries: int
    content_analysis_retries: int
    product_analysis_retries: int
    coverage_retry_count: int
    test_retry_count: int
    reanalyze_count: int
    # T0.3/T0.4: last dead code_writer invocation (wall-clock timeout /
    # provider exception that produced no draft) and how many consecutive
    # deaths this run has seen. Deliberately NOT test_retry_count — that
    # carries FINAL_RETRY_SENTINEL semantics for the testing loop.
    code_writer_error: str
    code_writer_error_count: int
    # How many times product_analyzer has re-mapped failed fields in this run
    # (capped by MAX_REMAPS). Set when route_after_testing sends a mapping
    # failure back to product_analyzer.
    remap_count: int
    # [A1/QW-3] code_tester → code_tester re-tests of the SAME draft (429 /
    # throttling / transient render). Tracked separately from test_retry_count
    # (which carries FINAL_RETRY_SENTINEL semantics and drives escalation) so a
    # same-draft retry neither burns nor advances the strategy-fix budget.
    test_retest_count: int
    # [A2/A6] fingerprint (sha1 of draft source) + wall-clock ts of the draft
    # the tester LAST tested. route_after_testing compares against the current
    # draft to detect a no-op fix cycle (A2: escalate instead of looping
    # scraper_analyzer → code_writer → code_tester on an unchanged draft) and
    # to keep the stale-output freshness floor honest when the draft is
    # unchanged (A6: an old output predates this ATTEMPT, not just this job).
    last_tested_draft_fp: str
    last_tested_at: float
    # [S-4 cheap half] consecutive code_writer invocations that died on
    # wall-clock timeout while an existing draft was already on disk. ≥2 means
    # the loop is re-running a 900s writer against a draft it cannot improve —
    # escalate to human_approval instead of burning another cycle.
    writer_wall_clock_timeouts: int
    # [A2] consecutive code_writer invocations producing a draft byte-identical
    # to the last-tested draft (fixed syntax/CLI issues only, no semantic
    # change). ≥2 means the fix loop cannot improve the draft — escalate
    # instead of looping scraper_analyzer → code_writer → code_tester again.
    # Reset to 0 whenever the draft actually changes.
    noop_fix_cycles: int
    # Append-only log of scraping strategies that failed testing + why, so the
    # strategy cascade never re-picks a strategy that already failed. Each entry
    # is {"strategy": <name>, "reason": <why it failed>}. route_after_testing
    # appends on an access/strategy-class failure; scraper_analyzer reads it to
    # pick the next strategy.
    strategies_tried: Annotated[list, operator.add]
    # Per-phase budget-retry counters (a single shared budget_retry_count
    # cross-contaminated phases: a site_analyzer exhaustion silently gave
    # product_analyzer the extended budget + skipped its escalation interrupt).
    # Kept (legacy alias) for back-compat; new code reads the per-phase fields.
    budget_retry_count: int
    budget_retry_summary: str
    site_budget_retries: int
    site_budget_retry_summary: str
    product_budget_retries: int
    product_budget_retry_summary: str
    nav_budget_retries: int
    nav_budget_retry_summary: str

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
    # Per-job scraper artifact path (attributed; set by _invoke_cleanup).
    scraper_path: Annotated[Optional[str], _last_write_wins]
    item_count: int
    product_count: int
    # discovery_coverage block read from the scraper output metadata during
    # execution (docs/discovery-coverage-gate-contract.md §1). Absent/None for
    # url_list scrapers with no discovery phase. Read by the coverage gate.
    discovery_coverage: dict[str, Any]
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

    # ── Dagster conversion (post-completion, non-blocking) ──────────────
    # Path to the generated {slug}_dagster.py file (set by dagster_converter
    # agent; read by the UI to show the download button). None if not generated.
    dagster_path: Annotated[Optional[str], _last_write_wins]

    # ── Navigation ──────────────────────────────────────────────────────
    navigation_findings: Annotated[Optional[dict[str, Any]], _last_write_wins]
    playwright_unavailable: bool
    # set when navigate_explore hands off to the LLM navigation_agent (form-driven
    # site the deterministic explorer couldn't drive); read by navigation_synthesize
    # to skip re-synthesizing the agent's already-written navigation_analysis.json.
    # Commented out: navigation_explore/agent/synthesize phases replaced by the
    # single browser_traverse phase (other agents handle graph.py / subagents.py).
    # handoff_reason: Annotated[str, _last_write_wins]

    # ── Error ───────────────────────────────────────────────────────────
    error_message: Annotated[str, _last_write_wins]

    # ── LangGraph message channel ───────────────────────────────────────
    messages: Annotated[list, add_messages]

    # ── Agent log accumulator ───────────────────────────────────────────
    agent_logs: Annotated[list[str], operator.add]
