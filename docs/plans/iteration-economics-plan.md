# Iteration Economics + Priceline E2E — Root-Cause Fix Plan (v2, post-critique)

Date: 2026-08-27. v1 + four independent critiques (genericity / efficacy / regression-safety /
optimality) reconciled. Full verdict log with line-number evidence:
`docs/plans/iteration-economics-critique.md`. Read that file alongside this one — every guard
below comes from a critic-verified code trace.

## What changed from v1 (the critique mattered)

Six v1 items as drafted would NOT have engaged the mechanism they cited:
- **E1** (volume gate): `_probe_phase1_discovery` discards stdout on both paths AND returns
  early unless the draft declares `--discover-only` — which the ssr_div_list family (job
  302's family) explicitly does not use. And live tester numbers are SAMPLE-scoped
  (`successful_extractions=5` vs discovered 97), so the gate FAILs the run that succeeded.
- **E5** (medium/url retry): the ground-truth override (`route_after_testing.py:~478`) runs
  before severity escalation and ignores severity — remapping medium→high is a no-op for
  job 302's exact shape.
- **I2**: the 900s-wall paths return `{"messages": []}` with no `_error`; and even with
  `_error`, skipping fixers saves nothing (they early-return on absent draft) — the reclaim
  requires routing past code_tester.
- **I10**: premise false — `headroom` is a LIVE dependency actively compressing >4k-char tool
  output; deleting the call sites increases churn. Re-scoped out.
- **I1**: a pre_model_hook cannot end the round (static edge; model still chooses to call
  tools) — the collapse escalation was itself a nudge. Nudge-only, scoped, round 2.
- **I11**: the maps are different UNITS; "unify" would have mandated a regression
  (playground must not inherit graph values).

## Tier 0 — Confound, walls, honesty (round 1, near-zero risk)

**T0.1 Model confound.** code_writer is the ONLY per-agent model override
(`AGENT_MODEL_SETTINGS`, subagents.py:79-81); job 302's writer ran `litellm/standardcompute`
while the 204-job corpus ran glm-5-turbo. Pin `CODE_WRITER_MODEL` to the corpus model for
comparability (or document the deliberate delta); `ZAI_FALLBACK_MODEL` stays a Railway ops
item. Mine the `[MODEL]` rows `_persist_agent_logs` already writes before judging I1.

**T0.2 Invoke walls.** `_AGENT_INVOKE_TIMEOUT=900` sits INSIDE the healthy range (its own
comment: "glm-5-turbo needs ~700-900s"). Make it env-configurable; add the missing
wall-clock wrapper to `code_tester` (graph.py:3841) and `dagster_converter` (graph.py:4150)
invokes, which today run unbounded.

**T0.3 Surface dead invocations (I2 amended).** Both wall-clock return paths
(`graph.py:1653-1657` async, `:1705-1711` sync) gain `_error`/`_error_class="wall_clock"`
(today: `{"messages": []}`, read by nobody). `_invoke_code_writer` inspects `_error` OR
empty messages → `logger.error` + `state["code_writer_error"]` (new ScrapeState channel —
langgraph rejects unknown update keys). Guard: `_run_budgeted_agent` uses `.get("messages")`
→ safe. Ripple: its auto-extend math must tolerate the new key.

**T0.4 Route past code_tester on a dead writer invocation.** With `code_writer_error` set:
skip fixers (already no-op on absent draft), skip the tester's full un-timeout'd invoke, and
return `Command(goto="scraper_analyzer")` — bounded by a new retry check (reuse
`test_retry_count` vs MAX_TEST_RETRIES → honest failure to human_approval/cleanup). Without
this, T0.3 is a relabel, not a reclaim (critique #6).

**T0.5 H1 ground-truth crash.** tasks.py:~832: string `site` → AttributeError → bare except
discards product_count/site_name/platform/scraping_method overrides. Fix the 2 string-
emitting templates (`playwright_scraper.py:413`, `ssr_div_list_scraper.py:262`) + a shared
normalizer at the reader. Fallback for platform is `site_analysis.site.platform` /
`state["platform"]` — NOT product_analysis (no such key — v1 error).

## Tier 1 — Payload + tools (round 1, additive)

**T1.1 Field-map summarizer (I6).** `subagents.py:~220`: extend sel chain with
`api_path`/`api_fallback_path`; render `notes` (capped) for api-method fields; add
`api_extraction` to the verbatim list (bounded). Guards (regression critic): pin API-method
fields INTO the rendered set — `_MAX_FIELDS=15` core-first sort would otherwise drop the
non-core `ratings`; caps are load-bearing (seed is truncation-exempt). Tests: priceline +
Shopify-shaped fixtures (no n=1 test contract).

**T1.2 Test-feedback relay (I8 narrowed).** `suggested_fix` (zero readers today) and
`feedback_for_writer` join the relay. (Expected/Actual for HIGH already works — P1.)

**T1.3 `check_syntax` tool (I3 — un contested FIXES-IT).** ast.parse + compile, line-precise
errors; scope guard reuses `_enforce_root`; wire via `AGENT_TOOL_MAP` for code_writer +
dagster_converter. Deletes the `_fix_scraper_syntax` fresh-reinvoke class.

**T1.4 `run_scraper` bucket split (I4).** Inside the code_writer branch ONLY (code_tester's
run_scraper is uncapped today — keep it that way). Buckets: probe-family (2) vs
`startswith("scraper_draft")` (2). Plus a SOFT draft-ran-before-handoff signal (state note +
log) — hard-gating deferred.

**T1.5 mechanism_reassessment (I7).** Value-match, not key-alias: any string under
`mechanism_reassessment` whose value ∈ {http_requests, http_navigation, playwright}, exact
token, count-gate subordinate. EXCLUDE `scraping_mechanism` (a key `_derive_strategy` itself
writes — self-confirmation trap). Pin the inner schema in product-analyzer.md's canonical
example. Summarizer renders the block by DEFAULT when the gate-flag is absent (resumed jobs
keep the evidence).

**T1.6 write/edit tails (I9).** APPEND a ≤200-char content tail; keep the "Successfully
wrote" prefix (5 substring test assertions pin it).

**T1.7 Cap hygiene (I11-lite).** Delete the dead `AGENT_MAX_ITERATIONS` (zero readers); add
`dagster_converter ≤120` to `AGENT_RECURSION_MAP` (it currently runs at default 150 — that's
how 34 calls happened); DOCUMENT the playground delta — do NOT unify (different units; the
playground's smaller budget is its only bound). Invariant test: each entry point's effective
recursion config is byte-identical to its PRE-change value.

## Tier 2 — Make defects loud, fix at the right layer (round 1)

**T2.1 Volume + pagination at the template/module layer (E1+E2 as ONE unit).**
- `src/discovery.py` `_try_page_param`: param-ALIAS LADDER on `_STUCK` — verify-advance
  already exists; on no-new-items try `currentPage` (0-idx) → `p` → page-sized `offset` →
  `skip`, each verified. Propagates to every draft via the verbatim-import rule.
- `templates/ssr_div_list_scraper.py`: emit `metadata.discovery_coverage` (+ dict `site`) —
  today it writes a string netloc and no metadata, so `_attach_discovery_coverage`
  (`graph.py:790`) and the Tier-1 `dedup_flat` gate find nothing.
- `_probe_phase1_discovery`: capture stdout on both paths (browser_service already returns
  it) for the families that support `--discover-only`.
- `discovered_url_count` state channel fed from the OUTPUT FILE's discovery_coverage via
  `_attach_discovery_coverage` — the source that works for the ssr_div_list family.
- **Route arbitration (the missing mechanism):** extend the ground-truth override's guard
  conjunction with the volume signal — arm ONLY on full-scope runs (sample runs are
  5-bounded; this is the v1 fatal flaw), require `discovered ≥ 2×items_per_page` and
  `extracted < 0.5×discovered`, bounded by the same retry checks. Regression pin: a
  job-302-shaped state (PASS/5-extracted/97-discovered, SAMPLE scope) must STILL route
  field_confirmation.

**T2.2 URL/price deterministic checks + bounded route (E3/E5 amended).**
- Deterministic tester-side checks (in the `_patch_scraper_output_filter` slot or a tester
  helper): double-host URL (`url` containing the host twice), price inversion
  (`previous_price < current_price` on >50% of rows), optional-field non-empty rate <20%
  despite a mapped selector — each emits a MEDIUM issue with `suggested_fix`.
- Route: url/identity-shaped WRONG_VALUE **with a non-empty `suggested_fix`** joins the
  override's exclusion conjunction (bounded bounce with its own retry check). Unanchored
  tester opinion stays medium. `src_url=listing` is by-design — never flaggable.
- code-writer.md gains the root-relative-join rule + value-oriented price-pair rule
  (lower=current, higher=previous; naming only as tiebreak — NOT the OCC-shaped
  "sale=base" convention). Rule: no prose without the deterministic check beside it.

**T2.3 Substantive-emptiness scoring (E6).** New deterministic helper (field_coverage is
LLM-only today) reading the OUTPUT JSON items; key source = the analysis field map / job
output_schema (NOT optional_field_names — `ratings` isn't in the registry); hard-coded
severity=medium; requires a non-empty selector/api_path (T1.1 preserves it); values kept
string/numeric-compatible for `_core_field_zero_coverage`. Feeds the T2.1 arbitration.

**T2.4 product_analyzer ≥3-sample rule (E4).** Prompt-only, shipped WITH the I1 scoping
guard (it multiplies probe calls) and budget headroom confirmed.

## Tier 3 — Structural (round 1, parallelizable)

**T3.1 dagster_converter → deterministic renderer (replaces I12 entirely).** Verified: the
transform is byte-mechanical (`_make_absolute`, `_clean_html`, `_extract_detail_field`,
selectors, soft-404 tuples identical source→output; 3 quotes-toscrape outputs differ by
hundreds of cosmetic lines extracting the same 2 fields; output only ast.parse'd, never
executed). AST the draft → emit fixed header + BaseTlsScraper subclass with Phase-1/Phase-2
bodies copied; acceptance gate already exists (`graph.py:4198-4232`); LLM path retained as
fallback; fallback rate measurable from the 28 `jobs/dagster-*.py` artifacts. Kills 69% of
the run's LLM input and 7m05s wall.

**T3.2 I5-narrow: one flag source.** `required_cli_flags` derives from `template_file`
(the {template: flags} dict populated by the AST walk `test_cli_contract_prompt.py` already
computes) — and ALL FOUR consumer classes move together (prompt, cli_contract_violation,
route_after_testing, tester L2 gate) or the two deterministic contract checks disagree →
routing thrash. The second disagreeing selector (`subagents.py:2661-2671`) is retired.
`strategy_contract` state schema DEFERRED (the 297/302 mechanism was
`navigation_section = api_section` + poisoned URL, not template selection).

## Round 2 (recorded, not this round)

- **I1 nudge-only** convergence nudge scoped to code_writer+dagster_converter via
  `set_tool_context`, probe/read tools exempt, new ScrapeState channel; a real terminator
  (forced no-tool final turn) needs its own langgraph-validation spike. Measure via T0.1's
  `[MODEL]` mining first.
- **I2-full strategy contract** (state schema + persistence into scraper_analysis.json for
  the resume hole), **H2** adopt-and-rename output naming + mtime sort, **H3** transport-scan
  preference (rewrite value, don't delete the write), **H4** ordered Site merge (backfill →
  merge+union input_urls → constraint → slug-or-url lookup, ONE commit), **H5**
  per-checkpointer-identity graph cache + interrupt-aware candidate age cap, **H6** hygiene,
  **I10** re-scoped to the async migration (it is live compression, not a zombie).

## Verification

1. Root suite green; the 9 known webapp failures re-baselined BY NAME (I1/I11/I12-class
   edits hide inside counts).
2. New regression pins: job-302-shaped routing (T2.1), f15 override intact (T2.2),
   `inspect.getsource` string assertions preserved (`_narrowed`/`firstn`), code-writer.md
   token tripwires respected, "Successfully wrote" prefix kept.
3. Priceline list_page e2e re-run (T0.1 model pinned). Pass criteria: ≥90/97 extracted or an
   honest volume-gate FAIL+retry; 0 double-path URLs; ratings non-empty where reviews exist;
   previous_price = base on discounted rows; dagster path deterministic (0 LLM calls on the
   happy path); no silent wall-clock deaths in the log.
4. Measurement: SessionLog `[MODEL]` mining for model↔iteration↔outcome before judging I1.
