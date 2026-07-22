# Job 11 Root-Cause Fix Plan (18 issues, all root-caused)

## Context

Job 11 (locumtenens.com, job_navigation) completed end-to-end but surfaced **18 systemic issues**. Eighteen parallel agents dug to the TRUE root cause of each (not symptoms), critiqued them, and proposed **generic, source-level fixes** (work for any site/content-type/strategy, not just locumtenens). This plan consolidates their findings for your review/edit.

The fixes are grouped by theme. Each entry: **Root cause** (1-2 lines + file:line) → **Generic fix** (source-level). Priorities: 🔴 P0 (correctness/data-loss/hang), 🟡 P1 (efficiency/UX), 🟢 P2 (hygiene).

> Note: 7 earlier fixes are already shipped (ContentType app_label, streaming-timeout, critical_fix injection, selector-crash classification, selector guard, review-feedback promotion, escalation banner, approval dedup guard). This plan covers the REMAINING 18.

---

## Theme A — The "1000+ extraction" chain (4 compounding bugs)

Fixing ANY ONE alone won't reach full extraction. All four compose.

### 🔴 P0-1. Scraper pagination re-fetches page 1 (caps at 25)
**Root cause:** `navigation_scraper.py` template's `_get_next_page_url` prefers click-based nav; code_writer overrode it with `NEXT_PAGE_SELECTOR="a[href*='pgNum']"` + `query_selector` (first match = page-1 link). The prompt (`code-writer.md:271`) even says "NO Pagination" — a contradiction. The navigation_analysis (`type:"page_param"`, `page_param_name:"pgNum"`) was correct but ignored.
**Generic fix:**
- `templates/navigation_scraper.py:316-372` — rewrite `_get_next_page_url`: **URL-construction-first** (use `PAGE_PARAM_NAME` from analysis, `_set_query_param` via urllib to REPLACE not append), semantic-click (`a[rel="next"]`) only as last resort. Never `a[href*='<param>']` (matches every numbered link).
- `.opencode/agents/code-writer.md:189,271` — make construction-first a HARD rule; resolve the "NO Pagination" contradiction (applies to url_list only, not navigation).
- `webapp/agents/subagents.py:2068-2078,2126-2131` — inject the detected `page_param_name`, prescribe `?{param}=N` construction.
- Add a `verify-page-changed` guard (URL AND items must change).

### 🔴 P0-2. `run_execution` forces `--query` on navigation jobs
**Root cause:** `run_execution.py:203` passes `--query <search_criteria>` for ALL of navigation/list_page/search_term. But `parse_command.py:31-36` establishes that `search_criteria` is a **filter** only for `search_term` — for `navigation` it's a **discovery hint**. The scraper then triple-locks to keyword search (template `:552`, `:577-582`, multisource `:280,316`). UI (`home.html:287`) actively encourages typing a term for nav jobs.
**Generic fix:**
- `run_execution.py:203` — `if input_mode == "search_term" and search_criteria:` (only search_term filters). Navigation/list_page get full discovery.
- `field_confirmation.py:203` — same one-line fix (sample path has the identical bug).
- `run_execution.py` — for navigation/list_page, pass `--listing-url <working_url>` from `navigation_analysis.search.working_url` (the proven results page) instead of `--query`.
- Remove the `_search_q` category filter (`navigation_scraper.py:577-582`) and the `relevant[:5]` cap (`run_execution.py:316`) for navigation jobs.

### 🔴 P0-3. `code_tester` never tests Phase 1 discovery
**Root cause:** `build_code_tester_message` self-contradicts — `nav_validation` (`:2785`) says "use `--query` so Phase 1 runs" but Workflow step 1 (`:2849-2853`) hardcodes `--input input_urls.json` (which bypasses discovery). The tester's own report admits `NOT_APPLICABLE_PHASE2` but still PASSES. `route_after_testing` has no phase-coverage gate.
**Generic fix:**
- `subagents.py:2848-2856` — for navigation/list_page/search_term, run TWO scrapes: (1a) `--query/--listing-url --limit 50` to validate Phase 1 (assert `discovered_urls > items_per_page`, dimension diversity), (1b) `--input input_urls.json` for Phase 2 fields. Delete the contradiction at `:2851-2853`.
- `route_after_testing.py:301` — add a phase-coverage gate: a navigation PASS requires `phases_tested.phase1_discovery == true`; else `PASS_WITH_CAVEATS` (never silent PASS).

### 🟡 P0-14. Dead `_discover_via_specialties` + cascading-dropdown reset
**Root cause:** (a) code_writer wrote a helper then inlined a different loop in `main()` (the helper can't relaunch the browser it doesn't own) — `born dead`, no dead-code check exists. (b) `_submit_quicksearch` sets Discipline AFTER Specialty (cascade wipes the child); the analysis schema models selects as independent siblings (no `depends_on`); `_get_specialty_options` in the SAME file gets the order right — proving the rule is unwritten.
**Generic fix:**
- `templates/navigation_scraper.py` — add a `_set_cascading_selects(page, ordered_fields)` helper (parent-first, sleep between for repopulation).
- `.opencode/agents/code-writer.md:199` — hard rule: set parent selects before children; if `navigation_analysis.cascading_selects` present, honor it; else top-to-bottom order.
- `.opencode/agents/navigation-agent.md:118` + `navigation-synthesize.md` schema — add a cascading-select dependency probe (`depends_on`, `set_order`, `cascading_selects` list).
- `check_syntax.py` — ast-based dead-function detection (advisory, fed back to code_writer); de-hardcode the path.

---

## Theme B — UI / display (2 issues)

### 🔴 P0-4. Output viewer shows "0 products" for non-shopping content types
**Root cause:** `views.py:426,1247,1355` + `output_view.html:18` hardcode `data.get("products", [])`. The producer side (`tasks.py:539`, `run_execution.py:561`, `graph.py:2613`) was migrated to be content-type-aware; the consumer side (views/templates) wasn't. Affects all 5 non-shopping types.
**Generic fix:**
- `src/content_types.py` — add `get_output_key_label(page_type) -> (output_key, singular_label)` (e.g. job_posting→("jobs","job")).
- `views.py:426,1355` — use the helper + a JSON-detection fallback (scan all 6 keys if the declared one is empty, for old jobs).
- `output_view.html:18`, `job_detail.html:228`, `site_detail.html:124` — use the label (`{{ item_count }} {{ item_label }}{{ item_count|pluralize }}`).

### 🔴 P0-18. `/jobs/N/agent-summary/` shows "(No summary available)" for many agents  *(your recurring issue — fully root-caused)*
**Root cause (5 conjoined, why prior fixes failed):**
1. `views.py:900-907` — the fallback branch **requires** `assistant_msgs` to exist, then **discards** them and prints the placeholder. (Literal producer of the string.)
2. `views.py:878-889` — stale hardcoded `agent_order` list (10 names) misses `navigation-agent`, `code-reviewer`, `dagster-converter`; lists `navigation-explore` (wrong name).
3. **Dual naming** — `_persist_agent_logs` writes hyphen (`code-writer`), `_ToolCallLogger` writes underscore (`code_writer`); view keys on raw string → every agent split into 2 buckets, underscore phantoms always hit the placeholder.
4. `_ToolCallLogger` (`graph.py:496-502`) writes `[TOOL]` traces as `ROLE_ASSISTANT` → pollutes summaries with tool noise.
5. **No generate/persist layer** — SessionLog has no `summary` column, no `AgentSummary` model; "summary" is render-time concatenation.
**Generic fix (all 3 layers — why this is complete where prior fixes weren't):**
- **Canonicalize** (`graph.py`): map underscore→hyphen via `AGENT_PROMPT_MAP` at BOTH `_agent_config` and `_persist_agent_logs`; change `_ToolCallLogger` role to `ROLE_TOOL` (not ASSISTANT).
- **Generate** (`graph.py:_persist_agent_logs`): extract the LAST non-empty assistant message; fallback to a tool-call digest (`"N tool calls: read_file (x5), …"`) for tool-only agents. No LLM call needed.
- **Persist** (`models.py`): add `AgentSummary(job, agent, summary, tool_call_count)` with `unique_together(job, agent)`, upserted each run.
- **Display** (`views.py:853-913`): delete the `agent_order` list + the placeholder branch; derive order from `AGENT_PROMPT_MAP` (single source of truth); read `AgentSummary`.
- **Backfill** old jobs (job 11): group SessionLog by canonicalized agent, exclude `[TOOL]`/`[HEARTBEAT]`, write `AgentSummary` rows.

---

## Theme C — Dagster converter (2 issues)

### 🔴 P0-5. Generated dagster file won't run (import commented out)
**Root cause:** `.opencode/agents/dagster-converter.md:69` + `subagents.py:3039` never state the canonical import (`from dagster_scraper_base import BaseTlsScraper`); the template has it inline with no note. The LLM guessed `from scrapers.base` (wrong), hedged by commenting it out. `_invoke_dagster_converter` (`graph.py:2538-2556`) only `ast.parse`s — can't see a commented import or undefined base class. Same blind spot in `_fix_scraper_syntax`.
**Generic fix:**
- `templates/dagster_template.py` + prompt — state the literal import verbatim as the single source of truth.
- `graph.py:2538-2556` + `_fix_scraper_syntax:1913` — replace `ast.parse`-only with: (1) `compile`, (2) **AST name-binding walk** (catches commented-import + undefined-base with zero side effects), (3) optional sandboxed subprocess import with stubs + timeout + no-network. Feed errors back to the agent (re-invoke loop).

### 🟡 P0-6. Dagster output has zero Dagster constructs (cargo-cult)
**Root cause:** The converter prompt + message + template only ask for a `BaseTlsScraper` subclass — never `@asset`/`Definitions`/schedule. The name "dagster" implies scheduling/partitioning/incremental-load that's never requested or delivered. `dagster` isn't even a dependency.
**Generic fix (DECIDE the intent):**
- **Recommended (b):** rename to `client_format_converter` / output `{slug}_base.py` — the deliverable (plain class for the client's BaseTlsScraper) is correct; only the name lies. Zero behavioral risk (node is terminal/non-blocking).
- **Alternative (a):** if real Dagster is wanted, add `@asset`/`Definitions`/`DailyPartitionsDefinition`/`ScheduleDefinition` to the prompt + template (generic scaffolding around the site-specific class), add `dagster` dependency, and import-check it.

---

## Theme D — Graph control flow (4 issues)

### 🟡 P0-7. `skill_learner`/`nav_skill_review` run on FAILED scrapes
**Root cause:** `route_after_cleanup.py` (the intended guard) is dead code — never wired; `graph.py:3261-3264` uses unconditional direct edges. The two LLM agents (recursion 80+60) lack the `execution_status` guard that `store_job_listings`/`dagster_converter` have. Worse: they write learnings from incomplete/wrong output → **poison the skill DB**.
**Generic fix:** Add a node-level guard at the top of `_invoke_skill_learner` and `_invoke_nav_skill_review` (mirror `store_job_listings:2588`): `if state.get("execution_status") != "SUCCESS": return {"messages": []}`. Use `!= SUCCESS` (forward-compatible with a future PARTIAL status). Delete the dead `route_after_cleanup.py` + false docstring.

### 🟡 P0-8. `budget_retry_count` cross-contaminates phases
**Root cause:** Single shared int (`state.py:78`), read by site/product/nav (`graph.py:935,1148,1372`), never reset. A site-phase exhaustion sets it → product starts in extended budget (70 not 50) AND the escalation gate `if budget_retries < 1:` is False → **the human never gets the budget-bump interrupt for product**. The sibling counters (`site_analysis_retries` etc.) are already per-phase; this is the inconsistent outlier.
**Generic fix:** `state.py` — replace `budget_retry_count`/`budget_retry_summary` with per-phase `site_budget_retries`/`product_budget_retries`/`nav_budget_retries` (+ summaries). Scope the 3 reads + 5 writes. Also remove dead `content_analysis_retries`/`reanalyze_count`. (Derived global signal = sum of per-phase, computed on read.)

### 🔴 P0-9. `validate_coverage` low-coverage retry is UNBOUNDED (infinite loop)
**Root cause:** The missing-file path (`validate_coverage.py:116-142`) increments `product_analysis_retries` + caps at 2. The low-coverage path (`:184-207`) — added in a later commit — does neither. `route_from_human_approval` (`graph.py:2967`) routes "retry" back with no guard. The ONLY unbounded loop in the product_analyzer recovery graph.
**Generic fix:** `state.py` — add `coverage_retry_count`; `constants.py` — add `MAX_COVERAGE_RETRIES=2`. `validate_coverage.py:184` — increment + cap, mirror the missing-file path. On exhaustion: interrupt `coverage_exhausted` (human gate "Continue anyway / Abort" — NOT silent proceed, since the analysis exists but is incomplete). Add the route to `graph.py`.

### 🟡 P0-15. Review-test-fix loop cycles 3× (~50% of job cost)
**Root cause (5 compounding):** (a) code_reviewer emits **prose**, not structured patches (no `edit_file` tool); code_writer re-derives the fix from prose. (b) Hard edge `code_writer→code_review` fires every pass (`graph.py:3233`) — N writes = N reviews. (c) Two of three code_writer runs were full rewrites (patch_mode didn't engage — checkpoint-resume instability). (d) `test_retry_count` increments on review cycles too (`graph.py:2124`) — review burns the test budget. (e) Medium strategy issues bundled with criticals → code_writer round-trip for a "no code fix needed" issue.
**Generic fix:**
- `subagents.py:2717` — extend code_review.json schema with `edits:[{file, old_string, new_string, reason}]`; `_invoke_code_review` applies edits **deterministically** (str.replace + ast.validate), routing straight to code_tester when they apply cleanly (skip code_writer). `old_string`-match is the correctness gate (misdiagnosis → fallback to prose path).
- `state.py` — add `review_fix_count` (separate from `test_retry_count`); only increment test budget on real test failures.
- `graph.py:2294` — strip strategy-area mediums from code_writer feedback; route them to scraper_analyzer directly.
- `constants.py` — `MAX_CRITICAL_REVIEW_RETRIES=1`, `MAX_MEDIUM_REVIEW_RETRIES=0` (mediums are tester-visible). Cap = 1 write + 1 patch.

---

## Theme E — Approvals / human-in-the-loop (2 issues)

### 🔴 P0-10. Stuck-approved silent hang (the dedup-guard gap)
**Root cause:** The dedup guard (`services.py:301-320`) stops the 505-row runaway but is a one-way valve — if a resume fails to consume an interrupt, no mechanism re-dispatches it. `cleanup_stuck_jobs` only touches RUNNING; `_auto_approve_stale_jobs` only sees PENDING; the supersede signal only fires on terminal status. Result: `WAITING_APPROVAL` + APPROVED approval + interrupt still in checkpoint → **infinite silent hang**.
**Generic fix:** Add a periodic watchdog (`tasks.py`, every 5 min): find WAITING_APPROVAL jobs with an APPROVED approval whose `interrupt_id` is still in the checkpoint → re-dispatch `resume_scrape_task`. Cap at 3 retries per interrupt_id (`Approval.resume_attempts`), then FAIL the job. Store `Approval.resume_value` (JSONField) at all 5 dispatch sites so the watchdog replays the exact decision (rejections need their reject dict, not a generic approve). Register in `CELERY_BEAT_SCHEDULE`.

### 🔴 P0-11. `admin.reject_selected` doesn't dispatch resume (job strands)
**Root cause:** `admin.py:141-155` sets status=REJECTED + saves, but never calls `resume_scrape_task.delay(...)` — the only dispatch site that forgets. The graph never receives the decision → job hangs in WAITING_APPROVAL. Secondary: `approve_selected` uses the legacy `{"choice":"Approve"}` format; both read `options` (legacy) not `decisions` (current).
**Generic fix:**
- Move `_build_resume_value` (`views.py:489-503`) to `agents/decisions.py` (shared); all 6 dispatch sites import it.
- `reject_selected`: build the reject decision dict, set `STATUS_APPROVED` (not REJECTED — the targeted-resume lookup filters on APPROVED; the reject intent lives in the dict), dispatch `resume_scrape_task`.
- `approve_selected`: use the decision-dict format.

---

## Theme F — Output data quality (2 issues)

### 🟡 P0-12. "physician" search returns PA roles (36% off-target)
**Root cause:** Site FullTextSearch substring-matches "Physician Associate" (inherent). The scraper collects all results with **no query-relevance validation** — the output filter (`scraper.py:1050`) is presence-based (title+company), not relevance-based. The `job_id` role token (`-MD-`/`-PA-`) is captured but never parsed.
**Generic fix (transparency > filtering):**
- Add a `query_match` reporter to output metadata (`src/result_relevance.py` or extend `content_types.py`): `classify_query_match(query, item)` → match/off_target/ambiguous, using conservative whole-word title matching. Report counts + sample, don't drop (titles vary → auto-filtering is fragile).
- Optional per-content-type role classifier (job_posting: `job_id` token / title keywords) as an opt-in enrichment.
- Surface in the UI before full scrape ("9 of 25 may be PA roles — refine?").
- Long-term: let users select a discipline dropdown instead of typing a keyword (the over-match is inherent to keyword search).

### 🟡 P0-13. `date_posted` = scrape date for 96% (fabricated freshness)
**Root cause:** Site sets JSON-LD `datePosted` dynamically to "today" on each load; scraper copies it verbatim (`scraper.py:720`) with no validation. `posted_date` is documented "for filtering by age" — so the dashboard's "last 7 days" filter (`views.py:1722`) is silently broken (returns months-old jobs). Affects any site with dynamic datePosted (vistastaff too).
**Generic fix:**
- `src/job_fields.py` — add `assess_date_reliability(date_str, scraped_at)`: flag `equals_scrape_date`/`future_dated`/`missing`. Wire into `_normalize_value`.
- Add `date_posted_reliable: bool` field (JOB_FIELDS + ARTICLE_FIELDS); keep the claimed date but downgrade reliability.
- **System-tracked `first_seen_at`** (the only fully-generic freshness signal): `JobListing.first_seen_at` (auto_now_add, indexed). In `_invoke_store_job_listings`, DON'T overwrite `posted_date` with an unreliable value on update. Dashboard filters on `(posted_date >= cutoff AND reliable) OR first_seen_at >= cutoff`, orders by `-first_seen_at`.
- Honest contract: `posted_date` = site's claim (informational); `first_seen_at` = system freshness (authoritative).

---

## Theme G — Skills consumption (1 issue)

### 🟡 P0-17. `code_writer` never loads skills (consumption gap)
**Root cause:** Only `navigation_agent` has a `skills_section` + workflow step (`subagents.py:1110-1121`); `build_code_writer_message` (`:1697`) has none. `site_analyzer`/`product_analyzer` are **explicitly prohibited** (`:836,1049`). The generic `_append_skill_descriptions` (`:444`) is platform-detection-oriented ("when you detect Shopify..."). So code_writer reinvents patterns the skills already document (browser rotation, checkpoint, JSON-LD guards, POST-to-session) — the learn→reuse loop is half-closed (capture works, consumption doesn't).
**Generic fix:**
- Add a `skills_section` to `build_code_writer_message` instructing it to load relevant skills before writing code (navigation-patterns, playwright-navigation, jsonld-extraction, anti-bot-handling). Soften the analyzer prohibitions to "load only if a skill matches (1 call max)".
- Fix `args_summary` bug (`graph.py:781`): `args.get('name')` → `args.get('skill_name')` (the load_skill param) — currently every load_skill logs blank.
- Commit auto-applied learnings to git after `skill_learner` runs (currently uncommitted since `991d91a` — lost on reset).
- Add skill-consumption telemetry (warn when code_writer loads 0 skills).

---

## Theme H — product_analyzer (1 issue)

### 🟡 P0-16. `product_analyzer` destructive re-run (28KB analysis overwritten with 14KB)
**Root cause:** `validate_coverage` routes low-coverage back to product_analyzer with a **fresh OBJECTIVE** (not the re-map directive at `subagents.py:950-966`). The re-map path exists but is gated to `code_tester.remediation.target=="mapping"` only. So the re-run re-analyzes from scratch, picks a different sample page, and blind-overwrites. `write_file` is destructive (no merge/backup).
**Generic fix:**
- `validate_coverage.py:184` — on low-coverage, set `test_report.remediation={target:mapping, fields:missing}` so the existing `remap_context` directive fires (reuse tested machinery). Add a distinct `is_coverage_patch` flag (keeps the normalize→validate loop, unlike `is_remap`).
- Pin the sample page: persist `analyzed_sample_url` from the first run; retries must use the same page (no re-pick).
- Defense-in-depth: in `_invoke_product_analyzer`, snapshot the pre-write analysis and deep-merge (new-wins-for-missing-keys, old-wins-otherwise) before returning.

---

## Implementation order

**Phase 1 — the 1000+ chain (do together, any one alone won't work):**
P0-1 (pagination) + P0-2 (--query) + P0-3 (tester Phase 1) + P0-14 (cascading). Then re-run locumtenens WITHOUT search_criteria → expect 1000+.

**Phase 2 — silent hangs & data-loss (independent, parallelize):**
P0-10 (stuck-approved watchdog) + P0-11 (admin reject) + P0-9 (validate_coverage cap) + P0-13 (date_posted) + P0-7 (skill on fail) + P0-8 (budget counter).

**Phase 3 — UI (your recurring pain):**
P0-18 (agent-summary, 5-layer fix) + P0-4 (output viewer) + P0-12 (PA leak reporter).

**Phase 4 — efficiency & skills:**
P0-15 (review loop, structured patches) + P0-16 (product_analyzer patch) + P0-17 (code_writer skills) + P0-5/P0-6 (dagster import + cargo-cult).

## Verification
- **1000+ chain:** re-run locumtenens (no search_criteria) → expect 1000+ jobs, multi-page discovery, pagination log shows `pgNum=2,3,...` with growing unique counts.
- **Approvals:** trigger a stuck-approved interrupt → watchdog recovers within 5 min; reject via admin → job routes correctly (not hang).
- **agent-summary:** after a job, `/jobs/N/agent-summary/` shows real summaries for ALL agents (no "(No summary available)"); underscore/hyphen buckets merged.
- **Output viewer:** `/jobs/11/output/...` shows "25 jobs" not "0 products".
- **Review loop:** code_writer runs ≤2× per job; structured patches apply deterministically.
- **date_posted:** dashboard "last 7 days" returns only genuinely-recent jobs.

## Notes for editing
- Each fix is **generic** (no locumtenens strings) — verified by the agents.
- Several have a "DECIDE" (P0-6 dagster intent; P0-12 filter-vs-report; P0-13 first_seen tradeoffs) — flag your preference.
- P0-18 (agent-summary) is your recurring issue — the 5-layer fix is why this one lands where prior single-layer attempts didn't.
- The 1000+ chain (Phase 1) is the highest-impact: it's why you got 25 instead of 1000+.
