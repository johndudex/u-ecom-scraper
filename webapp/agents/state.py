"""LangGraph state definition for the Universal Scraper graph."""

from __future__ import annotations

import operator
from typing import Annotated, Any

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
    sample_url: str | None
    product_url: str | None
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
    # [wave-36] Field-mapping contract (docs/plans/wave36-field-mapping-plan.md
    # §1a). resolved_fields = the canonical output-key list the pipeline
    # enforces; == target_fields verbatim when no mapping exists (byte-identical
    # legacy behavior). field_mapping = the chip→canonical resolution dict.
    # field_notes is written at tasks.py:_build_initial_state and read by the
    # writer guidance builder — declared so it survives graph passes instead of
    # being stripped (it has been behavior-dead until wave-36; gated behind
    # FIELD_MAPPING_ENABLED).
    # TWO-VOCABULARY RULE: drafts emit BOTH canonical AND chip-verbatim keys, so
    # every consumer that runs BEFORE the record-key rename admits
    # resolved ∪ raw ∪ custom; resolved-only is correct only at the finalize
    # prune (post-rename).
    resolved_fields: list
    field_mapping: dict
    field_notes: dict
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
    # [wave-34 T34-2] check_accessibility proved the list_page seed is itself
    # a product page (probe JSON-LD) and demoted the job to url_list.
    pdp_seed_flip: bool

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
    # [wave-22 B2] forced re-tests consumed this job. route_after_testing
    # grants ONE code_tester pass when the on-disk draft differs from
    # last_tested_draft_fp at a terminal verdict; _invoke_code_tester consumes
    # the allowance when it enters with that mismatch signature, so a forced
    # pass that dies cannot bounce terminal→code_tester forever.
    forced_retest_count: int
    # [wave-40 T1] Tester-side accounting channels. UNDECLARED keys are
    # silently stripped from node returns by langgraph, so every one of these
    # was behavior-dead: writers stamped them, readers always read 0/""/False.
    # Plain (LastValue) channels — each writer OVERWRITES; none may use
    # operator.add (graph.py's reset-to-0 for tester_wall_clock_timeouts is
    # load-bearing). Activation guards live at the writer sites — see
    # tests/test_wave40_state_channel_contract.py.
    tester_wall_clock_timeouts: int
    fast_fail_detail: str
    browser_unavailable_detail: str
    draft_absent_count: int
    last_tested_draft_bytes: int
    no_fresh_output: bool
    # [wave-37 W37-NEW-D] Ladder-repair budget: ONE deterministic proxy-ladder
    # re-injection per job, applied by _invoke_code_tester at entry (before
    # the window is spent) when the draft violates
    # ladder_preservation_violation (prod 628/623/600: otherwise-sound
    # drafts whose wiring the writer stripped). A second violation keeps the
    # honest-fail path.
    ladder_repairs: int
    # [wave-24 W24-1] Draft changed WHILE the tester ran (leaked writer thread
    # mutating under a live test — prod 395: zombie edit_file 2s into the
    # tester's run). _invoke_code_tester stamps it by comparing entry/exit
    # fingerprints; route_after_testing spends ONE re-test on what is actually
    # on disk before accepting a PASS verdict.
    draft_mutated_during_test: bool
    # [wave-24 W24-1] Mutated-verdict re-tests consumed. The tester node
    # increments when it enters with draft_mutated_during_test set, so the
    # router's one-re-test arm cannot loop if the mutation repeats.
    mutated_verdict_retests: int
    # [wave-24 W24-3] Access-wall accounting for the router's ×2 early
    # terminal. The tester's zero-yield arm counts discovery probes that died
    # on infra-class stop reasons (throttle / tier-block / render-wall —
    # walls no writer cycle can fix; prod 393 burned 3 writer turns on them).
    # escape_used caps the one-reset escape granted on a concrete
    # scraper-target diagnosis; all_throttled tracks whether every counted
    # stop was our own browser-service 429 (terminal routes to the park lane
    # instead of an honest-FAIL cleanup).
    access_wall_cycles: int
    access_wall_all_throttled: bool
    access_wall_escape_used: int
    # [wave-24 W24-7] True when the deterministic strategy node re-picked the
    # SAME strategy it already had (the anti-bot authority can force that —
    # 394's "strategy-switch" that wasn't). The writer message builder reads
    # it to give the cycle edit-over-write framing instead of the
    # template-rewrite hint.
    strategy_rerun: bool
    # [wave-22 B3] remediation identity (structural fingerprint of
    # target+field+issue-type set+exception class — NEVER the free-text fix)
    # of every remediation the writer has attempted, and the count of grace
    # fix cycles consumed. The router grants ONE code_writer cycle when an
    # exhausted ladder would terminate with a never-attempted remediation in
    # the report; the writer appends its fingerprint on entry and consumes
    # the allowance on the grace signature (exhausted entry + unseen fp).
    remediation_fps_seen: list
    remediation_grace_used: int
    # [S-4 cheap half] consecutive code_writer invocations that died on
    # wall-clock timeout while an existing draft was already on disk. ≥2 means
    # the loop is re-running a 900s writer against a draft it cannot improve —
    # escalate to human_approval instead of burning another cycle.
    writer_wall_clock_timeouts: int
    # [wave-30 W30-2] the draft-finisher salvage invocation (the _wc >= 2
    # skip_approvals arm) fires at most once per job — this flag routes the
    # second arrival straight to the honest cleanup instead of another
    # full-length window.
    writer_finisher_attempted: bool
    # [job-65 phase 3a] Execution-phase strategy recycles. When the run reaches
    # EXECUTION and the draft's Phase 1 discovers 0 URLs with a FAIL-class
    # coverage stop_reason, route_after_execution sends the job back through
    # scraper_analyzer → code_writer → code_tester with the failed strategy
    # recorded (the testing ladder escalates the rung). Capped at 1: the first
    # zero-item execution recycles; a second one finalizes honestly.
    execution_recycle_count: int
    # [wave-36 Fix 3] Shortfall remediation budget (bound 1), SEPARATE from
    # execution_recycle_count (transport-recycle). MUST be a declared key:
    # undeclared keys are stripped from graph state, so an undeclared counter
    # would read 0 on every pass → unbounded shortfall→writer→tester→execution
    # loop until SoftTimeLimit (round-2 B3).
    shortfall_remediation_count: int
    # [wave-36 Fix 3] No-regression floor: the pre-remediation artifact is
    # stashed here and restored if the remediation retry delivers fewer items
    # — a delivering result is never converted to a worse one by a recycle.
    prior_output_file: str
    prior_product_count: int
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
    # [job-329 wall] sha256 of scraper_draft.py as of the tester's verdict.
    # run_execution refuses to launch a draft that drifted after this was
    # stamped (abandoned writer threads edited the draft post-verdict on 329).
    tested_draft_sha256: Annotated[str, _last_write_wins]
    cleanup_report: Annotated[dict[str, Any], _last_write_wins]
    learning_report: Annotated[dict[str, Any], _last_write_wins]
    nav_learning_report: Annotated[dict[str, Any], _last_write_wins]
    navigation_analysis: Annotated[dict[str, Any], _last_write_wins]

    # ── Probe cache ────────────────────────────────────────────────────
    probe_result: Annotated[dict[str, Any] | None, _last_write_wins]
    probe_url: Annotated[str, _last_write_wins]

    # ── Execution metadata ─────────────────────────────────────────────
    execution_status: Annotated[str, _last_write_wins]
    output_file: Annotated[str, _last_write_wins]
    # Per-job scraper artifact path (attributed; set by _invoke_cleanup).
    scraper_path: Annotated[str | None, _last_write_wins]
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
    human_response: dict[str, Any] | None
    human_feedback: Annotated[str, _last_write_wins]

    # ── Routing decisions (set by routing nodes, read by conditional edges) ─
    next_node_after_testing: Annotated[str, _last_write_wins]
    next_node_after_cleanup: Annotated[str, _last_write_wins]

    # ── Dagster conversion (post-completion, non-blocking) ──────────────
    # Path to the generated {slug}_dagster.py file (set by dagster_converter
    # agent; read by the UI to show the download button). None if not generated.
    dagster_path: Annotated[str | None, _last_write_wins]

    # ── Navigation ──────────────────────────────────────────────────────
    navigation_findings: Annotated[dict[str, Any] | None, _last_write_wins]
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
