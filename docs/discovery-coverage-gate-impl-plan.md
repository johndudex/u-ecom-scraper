# Discovery Coverage Gate — Implementation Plan (Option A)

> Companion to `docs/discovery-coverage-gate-design.md`. Sequenced, dependency-ordered,
> with file:line targets and per-phase acceptance criteria. Scope = the tiered gate
> (Tier 1 `stop_reason` always-on + Tier 2 dimension-completeness + Tier 3 ratio,
> each opportunistic), driven through the existing `route_after_testing` cascade.

## Guiding principle

**Template foundation before classifier before routing.** The gate cannot read
signals the scraper doesn't emit. Every later phase depends on the metadata shape
defined in Phase 1. Do not start Phase 3/4 until Phase 1 lands and a template run
shows the new metadata block.

## MVP slice (catches the locumtenens class fastest)

If scope must be cut, ship **Phase 1 + Phase 3 + the H3 checkpoint fix** first.
That delivers: `stop_reason` enum (catches "gave up due to errors/blocks"), the
checkpoint-leak fix (likely part of the 38-job cause), and a coverage-aware
classifier wired to the cascade. Tiers 2/3 (dimensions/ratio) are Phase 2 and can
follow — they tighten the gate but Tier 1 + checkpoint fix already prevents the
silent rubber-stamp.

---

## Phase 1 — Template foundation (BLOCKING)

All later phases depend on this. Three template files + one tool change.

### 1a. `stop_reason` enum in metadata
- `templates/navigation_scraper.py` and `templates/http_navigation_scraper.py`:
  - Track the actual termination cause through the discovery loop (currently logged
    and discarded — `navigation_scraper.py:163,176-178`, `http_navigation_scraper.py:508,528-530,514-517`).
  - Emit `metadata.discovery_coverage.stop_reason` from the enum
    `{short_page | no_next_link | max_pages_hit | navigate_error | no_new_items | dedup_flat}`.
  - **`navigate_error`** when `_navigate` returned `None` after retries
    (`http_navigation_scraper.py:514-517`) — currently indistinguishable from exhaustion.
  - **`dedup_flat`** when consecutive pages returned only duplicates but
    `unique/seen` never flattened (feed-injection guard).
- Also emit `dimensions_iterated`, `dimensions_total`, `found` (= `extracted_items`,
  post-filter — **not** `discovered_urls`), `expected_total` (from baked-in
  `coverage_target`, nullable).
- `requests_scraper.py`: add the same `discovery_coverage` block (it currently emits
  only `failed_products`).

### 1b. `--discover-only` flag (so exhaustion probe stays bounded)
- Add to all three templates' argparse (`navigation_scraper.py:557-565` et al.):
  `--discover-only` (run Phase 1 to exhaustion, emit discovery_coverage, skip Phase 2
  extraction) and/or `--max-discover N` (cap discovered URLs without bounding pages).
- Without this, removing the `--limit` cap makes Phase 2 extract thousands of URLs.

### 1c. Checkpoint cross-contamination fix (H3 — latent bug, likely part of 38-cause)
- Clean `discovered_urls_checkpoint.json` after each completed run, OR namespace it
  per-invocation. `run_execution` passes `--fresh-discovery` (new flag).
- Standardize checkpoint path on `SCRIPT_DIR` (both templates; `navigation_scraper.py:72`
  uses `getcwd()`, risks cross-site leakage — L1).
- The checkpoint's only legitimate consumer is `browser_service` Chrome-crash retry
  (`scraper_runner.py:120`); clean it in `_post_run`.

### 1d. Long-timeout probe path (H1 — prevents misroute)
- `webapp/agents/tools/shell_tools.py:run_scraper`: add `coverage_probe: bool = False`
  arg; when true, use `timeout=1800` (not the 300s default at `:164`). Gated so only
  the Phase-1-exhaustion probe uses it. Without this the probe is SIGKILLed on large
  sites and the timeout-shaped crash misroutes to a false strategy switch.

**Acceptance (Phase 1):** a two-phase scraper run writes
`metadata.discovery_coverage.{stop_reason, dimensions_iterated, dimensions_total,
found, expected_total}`; `--discover-only` returns immediately after Phase 1; a
rate-limited run emits `stop_reason=navigate_error`; checkpoint is absent at the
start of `run_execution`.

---

## Phase 2 — Data capture (Tier 2/3 inputs)

Provides the dimension count and trusted total. **1 of 5 sites has this today**;
the goal is to raise that without over-promising.

### 2a. Defensive count parser (utility)
- New helper (e.g. `src/discovery_coverage.py`): `parse_count_string(s) -> int|None`.
  3-regex cascade with a currency/decimal negative guard (reject `"$128,502.00"`).
  No locale handling (untestable).

### 2b. Deterministic extractor extension (Fix #2, realistic version)
- `webapp/agents/nodes/navigate_explore.py:767-797`: extend the hardcoded allowlist
  with "element whose text matches `\d+ - \d+ of \d+`" (locumtenens pattern). Keep
  the LLM `total_count_selector` as a fallback hint only.
- Capture the raw count string into `navigation_findings.list_page.item_count_text`
  (currently `None` on locumtenens).

### 2c. Structured `coverage_target` + orchestrator-stamped source
- `webapp/agents/nodes/navigate_synthesize.py`: build `coverage_target` from
  `navigation_findings` (deterministic) — `total_items` via the parser,
  `dimensions[]` from `option_count`/`category_links`, **`source` stamped by the
  orchestrator** (`site_reported` iff raw string captured + parser succeeded;
  `estimated` iff derived from counts; else `unknown`). **Never LLM-self-certified.**
- `webapp/agents/subagents.py:1229-1259`: add `coverage_target` to the synthesize
  schema + anti-improvisation guidance ("count fields ONLY under coverage_target;
  do not emit example_total/total_jobs"). Post-validate to reject unknown count keys.

### 2d. Scope to browser strategies
- Runtime count-reading (TOTAL_COUNT_SELECTOR) is browser-only
  (`playwright`/`undetected_chromedriver`/`http_navigation`). Never promised for
  AJAX catalogs under `http_requests`.

**Acceptance (Phase 2):** locumtenens `navigation_analysis.coverage_target` =
`{total_items: 3771, dimensions: [{name:"specialty", count:207}, ...], source:
"site_reported", raw_count_string: "1 - 100 of 3771"}`. A site with no count
(GET-only, no form) yields `source: "unknown"`, `total_items: null`, `dimensions: []`
— no crash, no hallucination.

---

## Phase 3 — Coverage-aware classifier + recording fix

### 3a. Coverage-aware `classify_test_failure` branch
- `webapp/agents/nodes/route_after_testing.py:62-103`: add a deterministic branch
  reading `test_report.discovery_coverage`. Return `("strategy", "discovery
  incomplete: stop_reason=navigate_error" | "dimensions 1/207" | "ratio 38/3771")`
  per the tiered rules:
  - Tier 1: `stop_reason in (navigate_error, dedup_flat)` → FAIL.
  - Tier 1: `max_pages_hit` with `MAX_PAGES != None` → inconclusive (soft warning).
  - Tier 2: `dimensions_total > 0 and dimensions_iterated < dimensions_total` → FAIL.
  - Tier 3: `source=="site_reported" and found/expected_total < threshold` → FAIL.
  - Otherwise PASS (Tier 1 exhaustion signals are genuine).

### 3b. Fix `strategies_tried` recording (bypass bug)
- `webapp/agents/graph.py:1808-1816`: the analyzer calls `classify_test_failure`
  directly, bypassing route_after_testing's remediation override. With 3a in place,
  the coverage-aware classify returns `"strategy"` deterministically, so recording
  works. Verify; if the remediation override still needs honoring, route it through.

### 3c. Reconcile anti-bot downgrade
- `route_after_testing.py:378-379`: decide whether coverage failures are exempt from
  the `strategy→scraper` downgrade on anti-bot sites. Recommendation: **exempt
  coverage-triggered switches** (a coverage gap is a discovery-mechanics problem, not
  a cloak problem) — add a guard so coverage `"strategy"` actions aren't downgraded.

**Acceptance (Phase 3):** a synthetic `test_report` with
`discovery_coverage.stop_reason=navigate_error` routes to `scraper_analyzer` and
records the prior strategy in `strategies_tried`; the cascade does not re-pick it.

---

## Phase 4 — code_tester wiring (drives Tier 1 at test time)

### 4a. Thread `coverage_target` into the tester
- `webapp/agents/subagents.py:2829-2948`: inject `coverage_target` + dimension list.
  Tester treats `total_items=None`/`dimensions=[]` as "Tier 1 only."

### 4b. Coverage probe invocation
- Replace the dead `--sample --limit 50` with a `--discover-only` probe via the
  long-timeout `run_scraper` path (1d). Probe runs to exhaustion (or a page/time
  safety cap), reads `discovery_coverage`, and the LLM (or deterministic check)
  emits the structured coverage into `test_report`.

### 4c. Soften "PASS on field quality"
- `subagents.py:2966-2971`: for navigation/list_page/search_term jobs, a coverage
  failure must downgrade an otherwise-field-PASS. Today the prompt tells the LLM to
  PASS purely on field quality — directly contradicts the gate.

### 4d. Remediation decision rule
- `subagents.py:2999-3010`: list coverage/dimension under-yield as a
  `target: "strategy"` trigger (currently only access failures qualify).

**Acceptance (Phase 4):** on locumtenens, code_tester's coverage probe emits
`test_report.discovery_coverage` reflecting real discovery (e.g. `navigate_error` or
`dimensions_iterated=1/207`), `overall_assessment` is non-PASS, and the cascade
switches strategy.

---

## Phase 5 — Multisource aggregation (H2)

- `webapp/agents/nodes/run_execution.py:_run_category_sources` (`:271-413`):
  aggregate `discovery_coverage` across the up-to-6 source runs into the merged
  metadata (sum `found`, max `dimensions_total`, min `dimensions_iterated`, AND
  `stop_reason` precedence where any `navigate_error`/`dedup_flat` wins).
- Document whether the test-time gate runs pre- or post-merge conceptually
  (recommendation: test-time validates primary-source Tier 1; runtime validates the
  merged coverage).

**Acceptance (Phase 5):** a merged `output_merged_*.json` carries an honest
`discovery_coverage` reflecting all sources.

---

## Phase 6 — Routing + budgets

- **Distinct interrupt reason:** use `discovery_coverage_insufficient` (not
  `coverage_exhausted`/`low_coverage`, which collide with the field-coverage gate —
  `validate_coverage.py:204`, `:135`). Add a dedicated `route_from_human_approval`
  branch.
- **Coverage retry budget:** separate from `MAX_TEST_RETRIES=6`, OR add a fall-through:
  after N coverage-only failures (no other high-severity issue), route to
  `field_confirmation`/partial-output instead of looping (M4) — so a hard site still
  surfaces "38 jobs, at least something" rather than burning all retries.
- **State field:** add `discovery_coverage` to `ScrapeState` (TypedDict) so
  run_execution can feed runtime coverage back if Option B is ever needed.

**Acceptance (Phase 6):** a coverage-exhausted job reaches human approval with a
coverage-specific message; a site that under-covers on all strategies still surfaces
partial output.

---

## Phase 7 — Verification (end-to-end)

Run these live; each exercises a different tier/data-availability combination:

1. **locumtenens (jobs, classic form, trusted total):** all 3 tiers active. Expect
   the gate to FAIL the first strategy (http_requests) on
   `dimensions_iterated=1/207` or ratio `38/3771`, switch to http_navigation, and
   reach full coverage. **This is the regression target.**
2. **ayahealthcare or amnhealthcare (jobs, NO classic form, LLM count):** Tier 1 +
   at most Tier 3. Expect Tier 1 to catch any `navigate_error`; Tier 2 no-op
   (no deterministic dimensions); document the "cleanly returned few" residual.
3. **A small Shopify/SFCC product site (≤50 items):** Tier 1 only. Expect
   `short_page` on page 1 = exhausted = PASS (no false block on a genuinely small
   catalog).
4. **An article site:** Tier 1 only, exhaustion-based. Expect no ratio demand.
5. **A `page_content`/url_list job:** gate is a no-op (no discovery phase).
6. **Checkpoint regression:** confirm `--fresh-discovery` prevents the test→execution
   checkpoint leak (H3).

## Out of scope (separate design)

- **Scope-match gate** (`assess_query_match` promoted to gating) — load-bearing for
  scoped jobs but a distinct concern; separate design.
- **Option B (runtime re-entry to cascade)** — backstop if Phase 4's test-time probe
  proves insufficient in practice; needs a new graph edge + PARTIAL execution status.
- **Rank contiguity for SERP** — unimplemented; out of scope until SERP jobs are a
  real workload.

## Risk register

- **Tier 1 alone can't catch "cleanly returned few"** — the irreducible residual.
  Phases 2 (data capture) and broader deterministic dimension-counting are the only
  mitigations; accept the residual for non-classic-form sites.
- **The coverage probe's cost** (long-timeout, Phase 1d) adds minutes to testing on
  large sites × retries × strategies. Monitor ZAI usage (429s hit before). Consider a
  page-cap safety valve inside `--discover-only`.
- **Test-runner vs execution-runner divergence (M1)** means Phase 4's test-time
  verdict is calibrated on a different surface than the real run. Document; accept
  that Tier 1 at test time catches gross failures only.
- **Anti-improvisation enforcement** (Phase 2c) is prompt-level; post-validation is
  the only hard backstop. A determined LLM can still emit count-like keys elsewhere.
