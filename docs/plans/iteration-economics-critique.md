# Critique log — iteration-economics-plan.md

Four critics, one per criterion. Verdicts folded into plan v2.

## Critic 1 — GENERICITY (2026-08-27)

Corrections that change the plan (all code-verified by the critic):

1. **I2 is incomplete — the dominant death path returns no `_error`.** The 900s wall
   (`graph.py:1671` async TimeoutError) and the dead-thread path (`:1707`) return
   `{"messages": []}`; `{"_error": ...}` only covers real exceptions. Since the 25%-of-wall
   claim IS the 900s wall, I2 must surface empty-messages as a distinct failure too.
2. **I1 must be scope-gated.** `pre_model_hook` is the single hook for EVERY factory agent
   (`subagents.py:725`); navigation/site_analyzer legitimately repeat tool results
   (re-scroll, re-orientation). Gate to code_writer + dagster_converter; never count stall
   while the draft hash is changing (clause (b) alone is safe; (a) alone is dangerous);
   never collapse-to-seed on a false positive.
3. **E1 as written is site-specific AND fires on every normal run.** (a) `{"codes","total"}`
   is not a template contract — no template emits it. The generic contract already exists:
   all two-phase templates unconditionally emit `metadata.discovery_coverage.discovered_urls`
   (`requests_scraper.py:477-492` etc.), `_read_discovery_coverage` (`run_execution.py:1401`)
   already parses it. Fix: `_probe_phase1_discovery` reads the discover-only OUTPUT FILE's
   discovery_coverage, not stdout. (b) `successful < 0.5 × discovered` FAILs every
   `--sample` run (≤5 URLs) on any site with >10 discovered — the repo already learned this
   (`_count_regression` requires `_tested_n > 5` + scope check, `route_after_testing.py:455-490`).
   Gate evaluates full-scope execution only.
4. **E2 contradicts the prompt's own law + cites a nonexistent instruction.** code-writer.md:65-90
   FORBIDS inline pagination loops (shared `src/discovery` import is the law; NEVER define
   `_click_load_more`/`_get_next_page_url` inline). E2's param-probe chain belongs in the
   shared module (written once, tested once, every site) — not prompt prose. The "tester
   asserts discovered_urls > 1 page" instruction does not exist (zero hits) — retracted.
   `discovery_coverage` emission is already unconditional in templates; the real gap is a
   GENERATED draft silently dropping it → deterministic static check (cli_contract_violation
   shape), not a reminder.
5. **E3(2) generalized:** sale/base-by-name is OCC-shaped. Generic rule: orient price-pairs by
   VALUE (lower=current, higher=previous) + rendered evidence note; naming only as tiebreak.
   Drop "never inverted".
6. **E5 → deterministic severity floor, not a routing special-case keyed to one tester label:**
   url/identity-shaped field + WRONG_VALUE + (non-empty `suggested_fix` OR anchored
   `known_bad_values`) ⇒ high (today's arm). Unanchored tester opinion stays medium.
   Note `src_url=listing` is BY DESIGN in two-phase scrapers — must not be flaggable.
7. **I7: match on VALUE, not a key alias set.** Any string under `mechanism_reassessment`
   whose value ∈ {http_requests, http_navigation, playwright} — closes the synonym class
   permanently. DROP `scraping_mechanism` as an alias (an LLM restating the OLD verdict under
   that key would flip strategy to the thing it argued against).
8. **I8 premise wrong (P1 already fixed part of it):** `_issue_text` reads
   description/message/problem (`subagents.py:1978-1985`) and Expected/Actual IS relayed for
   HIGH. Narrow item to: `suggested_fix` (zero readers in .py) + `feedback_for_writer`
   (code-writer-v1.md:443, also unread).
9. **I11: THREE maps, and the missing entry is in the other two.** `AGENT_MAX_ITERATIONS`
   (dead, zero importers) HAS dagster_converter:15; the entries missing are in
   `AGENT_RECURSION_MAP` and `AGENT_MAX_ITERATIONS_LOOKUP`. Unit conflation: recursion_limit
   counts graph steps, MAX_ITERATIONS counted LLM turns (~2.5× apart) — a unified map must
   state its unit.
10. **H1's root cause is template divergence, not consumer robustness:** 2 of 9 templates emit
    a string `site` (`playwright_scraper.py:413`, `ssr_div_list_scraper.py:262`), 7 emit a
    dict; four consumers read `output["site"]`. Fix the two templates + one shared normalizer.
11. **H3: prefer the SHIPPED draft's transport** (I5's `uses_browser` / template imports) over
    the mechanism verdict — the verdict is only honored when the count gate is disarmed, so it
    is not always the truth about what ran.
12. **H5 cache must be per-PID (Celery prefork + Postgres checkpointer):** module-level cache
    pins a DB connection created at import time → broken-connection-after-fork. Lazy per-PID
    cache, or cache the uncompiled workflow and compile per job.
13. **E6 key source wrong:** `ratings` is in NEITHER core nor optional field names
    (`content_types.py:162`); registry-keyed helper never scores the motivating field. Score
    fields from the analysis field map / job output_schema (more generic). 20% threshold will
    flag legitimately sparse fields (salary/availability/publish_date) — bounded, medium only.
14. **I4:** probe bucket 2 is TIGHTER than today's flat 3; guard binds code_tester too
    (legitimately re-runs the draft across fix cycles) — exempt it or size buckets from a
    corpus query of run_scraper target distribution.
15. **I5: no silent fallback** — "fallback to current derivation when absent" keeps the three
    disagreeing authorities alive as live code; ship the contract + deprecation assert.
16. **I6: add a second API-shape fixture** (e.g. Shopify) or the test encodes priceline's
    artifact shape as the contract.
17. **I12 n=1 caveat:** 69%-of-tokens is job 302 only; and stuffing full draft+template into
    the seed of the worst-context node conflicts with the "<60k peak" pass criterion — bound
    what's injected.

N=1 overreach noted: Wave 2's gate/routing/scoring items (E1/E5/E6) change pipeline-wide
control flow on one job's evidence; prompt items (E2/E3/E4) are cheap to be wrong about.
Corpus evidence that would load-bearing-justify E5: severity×field distribution across stored
test reports. E1: discovered vs extracted distribution at sample/full scope. E6: per-field
coverage histograms.

## Critic 2 — OPTIMALITY (2026-08-27)

**GAPS the plan missed (ranked):**

1. **The model variable is a CONFOUND (Tier 0, ~zero code).** `subagents.py:79-81` —
   code_writer is the ONLY per-agent model override (`CODE_WRITER_MODEL`); `LITELLM_FALLBACK_MODEL=""`.
   Job 302's writer ran `litellm/standardcompute`; the whole 204-job corpus ran glm-5-turbo.
   The plan attributes 302's defects to prompt/contract gaps WITHOUT controlling for model.
   Actions: pin/unset CODE_WRITER_MODEL, set ZAI_FALLBACK_MODEL (already a pending ops item),
   mine the `[MODEL]` rows `_persist_agent_logs` already writes (`graph.py:4506-4527`) for a
   model↔iteration↔outcome table BEFORE building I1/I5/E*.
2. **The 900s wall bisects the healthy range.** `_AGENT_INVOKE_TIMEOUT=900` with its own
   comment "glm-5-turbo needs ~700-900s for code_writer" — the wall is inside the typical
   healthy duration; and `code_tester`/`dagster_converter` invokes have NO wall at all.
   Fix: env-configurable + measured p95 + add the missing walls (~5 LOC).
3. **No prose without a deterministic check beside it.** Wave 2's E2/E3/E4 are prompt prose;
   code-writer.md's Discovery section is browser-only and says "do NOT reimplement pagination
   inline" while supplying nothing for API pagination. The CLI-contract win shipped with a
   3-layer deterministic guard behind it. Converges with genericity critic: deterministic
   half → `src/discovery.py` shared helper (`discover_api_pages`), enforced via the existing
   `_enforce_discovery_import` machinery.
4. **Two cheapest deterministic tester checks for the two most expensive defects (NEW):**
   price-orientation check (current < previous on >50% of rows → inversion flag) and
   URL double-host shape check — ~20 lines each, cover ALL sites permanently, don't depend
   on the LLM tester noticing.
5. **`agent_logs` is a dead accumulator** (`state.py:160`, never appended) and the writer's
   channel is wiped (`graph.py:3678`). Caveat: full transcript relay premise is partly wrong —
   the tester is forbidden from reading the draft and assesses by output; the real duplication
   is `_probe_phase1_discovery` AND the LLM tester both running Phase 1.
6. **MAX_PAGES prompt contradiction (free):** `subagents.py:2567-2574` "Set MAX_PAGES high
   (e.g. 20) or null" vs `:3088-3102` "Do NOT set an arbitrary MAX_PAGES/MAX_ITEMS limit";
   and `http_navigation_scraper.py:132`/`navigation_scraper.py:54` carry literal `{MAX_PAGES}`
   placeholders with NO code-side substitution. One line to delete.
7. **No measurement harness** — the single priceline re-run is n=1 on an uncontrolled model.
   Ship the SessionLog dashboard first so the e2e is one point on a measured curve.

**CUTS / REPLACEMENTS:**
- **I12 → DELETE THE LLM.** AST-based deterministic dagster renderer: verified byte-identical
  transforms (`_make_absolute`, `_clean_html`, `_extract_detail_field`, selectors, soft-404
  tuples — amergedis); 3 quotes-toscrape dagster outputs differ by hundreds of lines of
  cosmetics while extracting the same 2 fields; output is only ast.parse'd, never executed —
  the LLM buys no validated correctness. Acceptance gate already exists
  (`graph.py:4198-4232`). Fallback rate measurable in an afternoon from the 28
  `jobs/dagster-*.py` artifacts. LLM path retained as fallback. Replaces BOTH the minimal
  and the deferred-full versions.
- **I5 → narrow version only:** `required_cli_flags(input_mode, template_file)` — a
  {template: flags} dict populated from the same AST walk `test_cli_contract_prompt.py`
  already computes. The actual 297/302 mechanism was `navigation_section = api_section`
  (`subagents.py:2809`) + poisoned `api_endpoint.url` — I5-full doesn't touch either.
  Defer the `strategy_contract` state schema.
- **I1 → nudge-only core** scoped via the existing `set_tool_context` contextvar
  (`tools/context.py:38`). The collapse half is the risky part (echoes the reverted 0683cc
  attempt) — measure first.
- **E1 without E2 is a detector with no fix** — ship as ONE unit (else 36/97 → FAIL →
  regenerate → same `?page=` bug = pure added latency).
- Defer to round 2: I7, I9, I10, I11-lite, E3/E4 prose, H2 (mtime-sort one-liner version
  noted), H3, H4, H5, H6.

**TOP-5 BY LEVERAGE:** I6 (S) · I2 (S) · dagster renderer (M) · I3+I4 (S+S) · E6+E5 (S+S).
**ROUND-1 SCOPE:** Tier 0 (model confound + walls) → Tier 1 (I2, H1, I6, I8, I4, I3) →
Tier 2 (E6, E5, E1+E2 unit, new deterministic checks) → Tier 3 (dagster renderer,
I5-narrow). ~14 items, ~500 LOC incl. tests; 11 of 14 are S and mostly pure functions.

Note: optimality's H1 phrasing "every template today" conflicts with genericity's verified
"2 of 9 templates emit a string site" — genericity cited exact lines
(`playwright_scraper.py:413`, `ssr_div_list_scraper.py:262`); take genericity's count,
optimality's severity (the bare-except swallows ALL ground-truth overrides for affected jobs).

## Critic 3 — REGRESSION SAFETY (2026-08-27)

**Decisive findings:**

1. **E1 is the worst item in the plan — it fails the job that SUCCEEDED.** Live inputs are
   SAMPLE numbers: tester Phase 2 runs `--sample --input input_urls.json`
   (`subagents.py:3485`); job 302's `successful_extractions=5`, `sample_size=5`. The gate
   `5 < 0.5×97` → FAIL on the run that shipped 36 products. Every healthy nav job
   discovering >2×items_per_page fails. MANDATORY guard: arm only when the test run was
   full-scope (or normalize to `min(discovered_url_count, len(input_urls))`). Also confirmed:
   `_probe_phase1_discovery` discards stdout in BOTH paths (`graph.py:3776-3779`) but
   browser_service DOES return stdout (capped 50k, `scraper_runner.py:180`) — capture is
   additive, no contract change. Regression test: job-302-shaped state asserting
   `route_after_testing == "field_confirmation"` (today's shipped behavior).
2. **E5 as specified is a NO-OP for the exact case it cites.** The ground-truth override
   (`route_after_testing.py:577-596`) runs BEFORE severity escalation, ignores
   `high_severity` entirely, and fires on ≥3 real items — job 302 is PASS/0.9/5-real-items,
   so even forcing high_severity only rejects the PASS branch and the override still ships.
   The remap must amend the OVERRIDE's exclusion list (weakens F15 protection — trade to
   state explicitly). Net win is CONDITIONAL on `suggested_fix` being relayed (I8) so the
   retry applies a mechanical fix instead of a coin-flip rewrite. Tests:
   `test_f15_ground_truth.py` must still pass; add medium/url+5-real-items routing test.
3. **I11 as worded would MANDATE a regression.** The maps are different UNITS
   (recursion steps vs LLM turns). Playground must NOT inherit graph values (raising its
   code_writer 30→120 with no wall clock = unbounded; capping graph at 30 = GraphRecursionError
   at scale). dagster_converter is in NEITHER live map → runs at default 150 — which is why
   34 calls were possible. Fix: keep two maps in one module, add dagster to
   AGENT_RECURSION_MAP at ≤120, document playground delta. My proposed invariant test
   ("both entry points identical") was WRONG — the correct invariant is each entry point's
   effective recursion config is byte-identical to its PRE-change value.
4. **I1 global blast radius confirmed** — `pre_model_hook` at `subagents.py:725` inside
   `_build_agent` covers every LLM agent AND the playground. False positives include E4's
   own ≥3 probes, read_file's 50k truncation convergence, constant empty-search strings,
   identical static-page run_scraper results. Guard: scope to code_writer+dagster_converter,
   exempt probe/read/list tools or require ≥2 distinct tool names, inject inside the
   returned llm_input_messages list. `stall_count` is lost on the timeout branch. Round 2.
5. **I5 is the most invasive item; resume hole confirmed.** FOUR consumer classes for
   required_cli_flags (prompt, cli_contract_violation, route_after_testing, tester L2 gate)
   — partial re-pointing makes the two deterministic contract checks disagree → routing
   thrash. A SECOND disagreeing selector confirmed at `subagents.py:2661-2671`
   (mechanism-first, never returns api/ssr_div_list/requests templates). Resume hole:
   `check_tracker` skip flags route to code_writer/code_tester WITHOUT scraper_analyzer
   (`graph.py:1492-1497`) — a state-only strategy_contract is absent exactly where the
   fallback claims to cover. → persist into `scraper_analysis.json` (rehydrated at
   setup_workspace.py:216). Mirror `uses_browser` into `scraping_method` or dispatch
   fast-paths silently disappear. This all supports optimality's I5-narrow verdict.
6. **I7 self-confirmation trap:** `scraping_mechanism` is a key `_derive_strategy` ITSELF
   writes (`graph.py:3203`) — a value-match loop must stay exact-token + enum-restricted or
   a contaminated artifact becomes self-confirming. Default to RENDER when the gate-flag is
   absent (resumed jobs) or the writer loses the evidence.
7. **I6 guard:** `_MAX_FIELDS=15` core-first sort can drop the non-core API field you're
   surfacing (ratings is non-core) — pin API-method fields into the rendered set; seed is
   never trimmed so the 300/600-char caps are load-bearing.
8. **I4 facts:** cap currently binds ONLY code_writer (`subagents.py:925-938`); code_tester's
   run_scraper is UNCAPPED today — keep it that way (don't move the guard into shell_tools).
   Bucket key must be `startswith("scraper_draft")` (draft v2 names exist).
9. **I9 mechanics:** APPEND the tail, keep the "Successfully wrote" prefix (5 substring
   assertions pin it); no edit_file return assertions exist.
10. **I10 tripwire:** graph.py:1615's COMMENT references headroom — grep-zero lint must
    exclude comments.
11. **I12 guards:** seed is truncation-exempt → injecting full files moves chars into the
    untrimmable seed (cap each); keep read_file (its instructions require reading analysis
    JSONs); AGENT_TOOL_MAP is global + shared with playground (factory param needed).
12. **H2 root:** the +5:30 originates in the GENERATED DRAFT's own clock (7 templates name
    their own output files) — worker can't pass in a name for a file the draft names; fix is
    adopt-and-rename from the runner's `output_name` (`scraper_runner.py:174`) + sort by
    mtime; future-name rejection as tiebreaker only (else "wrong old file" → "shows nothing").
13. **H4 sequence (dangerous if ordered wrong):** backfill empty slugs → merge same-slug
    (keep newest, UNION input_urls; `_sync_input_urls_file` fires on every save and rewrites
    a file the pipeline reads) → add constraint → convert check_tracker's URL lookup to
    slug-or-url IN THE SAME COMMIT (else IntegrityError at node 2).
14. **H5:** only the checkpointer is lru_cached today; `build_graph()` is per-run. Module
    cache is safe keyed on checkpointer identity. Age-cap ONLY jobs with no pending interrupt
    in the checkpoint (else fails jobs a human is mid-approval on).
15. **E6 mechanics:** no deterministic coverage helper exists (field_coverage is LLM-only,
    code-tester.md:112) — E6 ADDS one reading the OUTPUT JSON items; hard-code severity=medium;
    require non-empty selector/api_path in the analysis map (I6 preserves it); keep values
    string/numeric-compatible (`_core_field_zero_coverage` parses both).
16. **I2:** timeout branch must also write `_error` (confirmed `_error` currently read by
    NOBODY — grep-zero; `.get("messages")` callers are safe).
17. **Process:** re-baseline the 9 known webapp failures BY NAME, not count — I1/I11/I12
    touch playground/agent-construction code where a new break hides inside the same number.

**Test files pinning changed behaviors:** test_job12_strategy_gate.py (regex source harness,
layout-sensitive), test_cli_contract_prompt.py (code-writer.md TOKEN TRIPWIRES — E2/E3 edits
must not trip `--listing-url`/`--fresh-discovery`/`protected\s+by...` checks),
test_codegen_fixes.py (asserts routing + inspect.getsource string assertions — refactors must
preserve `_narrowed`/`firstn` verbatim), test_f15_ground_truth.py (regex-extract bounds —
new top-level defs can break grab()), test_artifact_write_guard.py + test_skills_guards.py
("Successfully wrote" substrings), test_f2_cdp_retry.py, test_admin_recompute.py +
test_recompute_date_reliability.py + test_partner_api.py (Site.objects → H4),
test_content_types.py:470.

## Critic 4 — EFFICACY (2026-08-27)

Verdicts: I3/I6/I7/I8/I9/I11/H2/H4/H5 FIXES-IT · I1/I2/I4/I5/I12/E2/E3/E4/E6/H1/H3 PARTIAL ·
**I10 REGRESSION-RISK-OUTWEIGHS (premise false)** · **E1/E5 NO-OP for the cited job**.

**Decisive corrections:**

1. **I10's premise is FALSE — headroom is live, not a zombie.** `headroom-ai[langchain]>=0.30`
   is a live requirement with onnxruntime pinned; all four sites are live
   `from headroom import compress` actively compressing >4000-char run_scraper output.
   Deleting them injects uncompressed output → INCREASES the churn the plan targets. Real
   removal reason is async-safety (the reliability doc's P0) — re-scope out of this plan.
2. **E1 triple-broken for priceline itself:** (a) stdout discarded on both paths;
   (b) `_probe_phase1_discovery` returns early unless the draft declares `--discover-only`
   (`graph.py:3715-3717`) — and the ssr_div_list family "does NOT use --fresh-discovery/
   --discover-only" (`templates/ssr_div_list_scraper.py:218`); (c) `{"codes","total"}` exists
   nowhere. The gate's only reachable priceline branch is N=0-skip. Correct source:
   `discovered_url_count` from the OUTPUT FILE's `metadata.discovery_coverage` via the
   existing `_attach_discovery_coverage` hook — not `--discover-only` stdout.
3. **E2's right layer is the template + `src/discovery.py`, not prose:** the draft ALREADY
   detects the no-op param and stops silently (`page_records == 0` → break, ssr_div_list:201-207;
   `_try_page_param` returns `_STUCK`) — missing is the param-ALIAS LADDER on `_STUCK` +
   emitting `metadata.discovery_coverage` + dict `site` in ssr_div_list (writes string netloc,
   :262, no metadata block — which is why `_attach_discovery_coverage` and the Tier-1
   `dedup_flat` gate find nothing). ~20-line changes propagating to every draft via the
   verbatim-import rule.
4. **The route-arbitration fix is MISSING MECHANISM #1 — E1/E5/E6 all die on the
   ground-truth override** (`route_after_testing.py:~478`): severity is only consulted to
   reject the PASS branch; the override fires on ≥3 real items before any of their arms.
   Fix = add the new signals to the override's guard CONJUNCTION + a bounded bounce arm with
   its own retry check — not "remap severity".
5. **I1 cannot terminate:** a pre_model_hook has a static edge to agent, cannot route to END,
   and the model still decides whether to call a tool — the ×4 collapse IS the nudge-not-
   terminator the plan rejects. Real options: forced no-tool final turn (`bind_tools([])`/
   `tool_choice="none"` on a final invocation), a validated `Command(goto=…)` from the hook
   (must be tested against langgraph 1.x), or an explicit let-recursion-fire policy. Plus
   `stall_count` needs a NEW ScrapeState channel (langgraph rejects unknown update keys).
   Byte-identical-hash also misses the largest churn class (non-identical but uninformative).
6. **I2 is honesty, not economics, unless it routes:** skipping fixers saves nothing (both
   early-return when the draft is absent, `graph.py:3296`) and the flow still pays
   code_tester's full UN-timeout'd invoke (`graph.py:3841`) + a retry relabel — same wall,
   better label. Actual reclaim = `Command(goto=…)` past code_tester on a dead invocation,
   bounded by a retry check.
7. **I5's consumer census was incomplete:** the hard enforcer `run_execution.cli_contract_violation`
   (run_execution.py:157, called :477) picks the required discovery trigger from `strategy`;
   the dispatcher passes `--fresh-discovery` from it; `route_after_testing._HTTP_LIKE_STRATEGIES`
   (:26), `graph.py:3680` (writes state["scraping_method"]), update_tracker_analysis.py:22,
   tasks.py:1049, `_needs_browser` (run_execution.py:239) all read it. Contract must be
   consumed by the enforcer + dispatcher or C7/argparse-exit-2 survive.
8. **H1's named fallback doesn't exist:** product_analysis.json has no `platform` key. The
   working fallback is `site_analysis.site.platform` / `state["platform"]` (written by
   update_tracker_analysis.py:54, read tasks.py:807). The real loss is product_count/site_name
   ground truth, not "platform permanently empty".
9. **H3's honest source is the draft's transport scan** (`_scraper_needs_browser`) — the
   mechanism verdict is not always the truth about what ran (matches genericity critic).
10. **I12 needs a factory param** (per-invocation tool override doesn't exist; current dagster
    toolset lacks edit_file) and **no item adds a wall clock to code_tester/dagster invokes**.
11. **I4 still lacks a draft-ran-before-handoff gate** — bucketing engages starvation but a
    writer can burn draft budget on error loops or skip the draft.
12. **Deterministic field-level output assertions (new):** double-host URL, previous_price <
    current_price inversion, optional-field non-empty-rate vs mapped selector — in the
    `_patch_scraper_output_filter` slot or a tester helper. Prompt-only versions have no
    enforcement AND (per #4) no route.

**Verdict on the plan's own thesis:** confirmed — right problems, but 6 items as drafted
would not have engaged (E1, E5, I10, I2-economics, I1-terminator, parts of I5), and the
single highest-leverage missing piece is the route-arbitration change at
`route_after_testing.py:~478` that all three "make it loud" items depend on.
