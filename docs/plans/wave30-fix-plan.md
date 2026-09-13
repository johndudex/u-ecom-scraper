# Wave-30 Fix Plan — writer budget/resume + discovery contract

**Status:** REVISED PROPOSAL (RCA + adversarial critique absorbed, 2026-09-12). RCA: 2 forensic agent reports on prod jobs 569/570. Critique: independent adversarial pass re-verified all load-bearing anchors; corrections folded in (ast.parse gate is `graph.py:5526-5537`; skip_approvals cleanup arm `:5633-5646`; the `--fresh-discovery` rationale lives at `nodes/run_execution.py:836-841` + `docs/plans/wave26-fix-plan.md:40` — the plan's earlier "H3" citation was dangling).

## RCA synthesis (evidence-backed)

### Class A — job 569 (revolveclothing.com.au, list_page): writer wall-clock ×2, mid-progress
- Writer inv-1 (05:35→06:05Z): 23 tool rows / ~12 turns / **145 s/turn**; draft written 14m39s in, check_syntax + 2 sample runs + scratch probe done; died at exactly 1800.0s with a tool 56s before death.
- Tester honestly FAILED (Phase-1 discovery 403 at `impersonate=chrome`; Phase-2 legacy seeds 404×3). Cascade switched strategy http_requests→http_navigation 12ms later.
- Inv-2 (06:09→06:39Z): 19 tool rows / ~11 turns / 161 s/turn; **9/9 edits landed** on the OLD-strategy draft (strategy switches never rotate `workspace/{slug}/scraper_draft.py`) — died 34s after the last edit, ~300–600s short of self-validation.
- Plumbing: writer passes **no timeout** → module default `_AGENT_INVOKE_TIMEOUT` (graph.py:5426 → :2326 → :2031; prod env = 1800). code_tester has a per-phase override (`:2036`, used `:6825`) — the writer has none. Killer: **consecutive wall-clock-death counter** `writer_wall_clock_timeouts ≥ 2` (state.py:145-147; graph.py:5563/5620/:5626) → skip_approvals arm → cleanup (`:5633-5646`; intake jobs force `skip_approvals`, views.py:2979). 1800s ≈ 11–12 turns ≈ one draft cycle, zero slack; "not making progress" is asserted nowhere.
- Telemetry lie: ToolCallLog is return-value-derived (`:8131-8166` iterates `result["messages"]`); wall-clock death returns `{"messages": []}` (`:2436`) → 569's ~42 real writer calls logged as **zero**.
- Zombie: abandon-and-walk-away (`:2409-2436`); W24-1 latch (`subagents.py:1083-1122`) disarms tools (held — draft parse-clean) but LLM spend continues; refusals raise **before** `on_tool_start` → unlogged.
- Biggest sink: `browser_traverse` **54m26s (42% of the job)**, zero telemetry, no deadline on the traversal path (the wave-26 Redis lock `:3124-3138` is mutual exclusion, not a budget).

### Class B — job 570 (revolve.com, url_list): TypeError at listing_discovery.py:315
- Writer **complied with the wave-28 brief** (imported `create_fetch_page`, draft:130) but wrapped it thread-locally `def fetch_page(url, **kwargs)` (draft:231) — exactly what the seed's concurrency bullet ordered. `**kwargs` cannot absorb the positional `(url, min_tier)` call (contract doc `src/listing_discovery.py:246-248`, call `:315`); wrapper also lacks `min_tier_floor`/`tiers_total` (silent `getattr` defaults `:278-279`).
- Reachability: `run_execution` appends `--fresh-discovery` unconditionally (`nodes/run_execution.py:842`) — rationale is **checkpoint-reuse on nav-family drafts** (`:836-841`, locumtenens 38-of-3771; wave26 plan :40) — and the writer promoted the flag to a Phase-1 trigger (draft:711) against its own template's no-op (`requests_scraper.py:412-416`; api family `:456-463` uses trigger semantics, where `_force_fresh` is what CAUSES the url_list catalog crawl).
- Coverage hole: tester prompt says url_list skips Phase 1 (`subagents.py:4985`) → PASS 0.85, zero retries; the crashing branch first executed at run_execution (exit-1 emitter `:1398-1403`).
- Gene history: job-58 stripped the closure → closure introduced; job-62 stripped the ladder → ladder moved into src; 570 kept both but **wrapped** them. Brief-text fixes refuted — writer obeyed every instruction; instructions were jointly unsatisfiable.

## Fix items

Batch 1 = killed-the-job classes. Batch 2 = observability/cost. All TDD (red-green-refactor), ruff-clean, full suite green before e2e.

### W30-1 — Writer-scoped invoke window + activity-aware extension  (Batch 1) — SHIP-WITH-CHANGES
- `graph.py:5426` passes `timeout=_writer_invoke_timeout()` (env `WRITER_INVOKE_TIMEOUT`, default = `AGENT_INVOKE_TIMEOUT`).
- **Sync path only** (`_invoke_agent_with_timeout`); async opt-in (`AGENT_ASYNC_PHASES` ships empty) keeps strict cancellation and is untouched.
- The single blocking `thread.join(timeout)` at `graph.py:2409` becomes a poll-join deadline loop (daemon thread; semantically equivalent) that **extends** while `(now − last_activity) < WRITER_ACTIVITY_FRESH_S` (env, default **300** — mid-LLM-turn safety at 145–161 s/turn) and total < env `WRITER_MAX_TIMEOUT` (default **2700**, so a later finisher is rarely clamped to zero). Activity = `_ToolCallLogger.on_tool_start` stamps a monotonic `last_activity` (logger already rides `agent_cfg["callbacks"]`, `:1126`); "never stamped" = strict original window.
- **Clamp interaction:** extension is bounded by remaining task budget — the `_effective_timeout` clamp (`:2352-2368`, ceiling = task soft limit 12960 minus margin, `tasks.py:488-490`/settings.py:165) is applied inside the loop each poll. On a 569-shaped job (54m already spent in traverse) the extension is partially clamp-eaten; that is the intended job-level ceiling.
- **Counter semantics:** an activity-FULL death at cap **still increments** `writer_wall_clock_timeouts` — otherwise the W30-2 finisher could never fire. Reset-on-healthy (`:5666-5668`) unchanged.
- Tests: T1 writer window ≠ module default; T2 extends on fresh activity up to cap; T3 dies with stale/no activity (INVOKE-TIMEOUT row unchanged); T4 other phases bit-identical (handle=None path); T5 activity-full death increments the counter; T6 clamp-refusal (`_effective_timeout → 0.0` → `{"_error": "job budget exhausted"}`) surfaces as a **named outcome** (`writer_budget_refused`) with a test.

### W30-2 — Draft-finisher invocation before honest fail  (Batch 1) — SHIP-WITH-CHANGES
At the `_wc ≥ 2` arm, **skip_approvals branch only** (`graph.py:5633-5646`): if the on-draft parses (guaranteed — the arm is behind the `ast.parse` gate at `:5526-5537`) → **one** bounded finish invocation, guarded by a once-per-job state flag:
- Seed = current draft + test_report + failure note + **refreshed `scraper_analysis` strategy AND `strategies_tried`**, with an explicit line: "the deterministic analyzer may have switched strategy after your draft — adapt the draft to the current strategy or justify keeping it" (569's inv-2 edited an old-strategy draft because switches never rotate the draft file).
- Framing: "you are FINISHING, not restarting — land remaining edits, then check_syntax + one sample run."
- `timeout=WRITER_FINISH_TIMEOUT` (env, default 1800).
- **Outcome contract:** healthy → normal tester ladder; wall-clock death → honest cleanup (existing arm); **clamp-refusal or alive-but-no-draft → honest cleanup explicitly** (not treated as deaths, no counter increment); tester FAIL afterward re-enters the ladder with `_wc` reset semantics unchanged — inv-4/5 are bounded by the once-per-job flag + the clamp + the `_wc` backstop (written down here as the contract).
- The non-skip arm (`:5647-5664`) is untouched — the human already gets "Retry code generation"; pre-interrupt LLM spend would duplicate their decision.
- Tests: T1 fires exactly once; T2 salvages a parse-clean mid-fix draft (569 inv-2 fixture); T3 no parseable draft → straight to cleanup; T4 finisher wall-clock death → honest fail; T5 clamp-refusal → honest cleanup (no crash, no loop); T6 alive-but-no-draft → honest cleanup; T7 non-skip path still interrupts with no finisher.

### W30-3 — Discovery-callable entry contract assert  (Batch 1) — SHIP-WITH-CHANGES
In `discover_listing_urls` / `_with_retry` (`src/listing_discovery.py:231`/:462) entry:
- **Arity bind-check (fatal)**: `inspect.signature(fetch_page).bind(url, 0)` — safe (create_fetch_page returns a plain closure, `src/http_fetch.py:248`, inner `def fetch_page(url, min_tier=0)`); bind TypeError → raise `DiscoveryContractError` (ValueError subclass) with the tester/LLM-actionable message: pass the closure DIRECTLY; wrappers must forward positionally and carry the closure's attributes.
- **Attribute check (WARNING, not fatal)**: missing `min_tier_floor`/`tiers_total` → loud warning + `discovery_meta["ladder_aware"] = false` (fatal would break 9 `tests/test_probe_page_cap.py` stubs; the stubs also get the attributes added as hygiene). Floor write-back `:396-398` setattr-guarded.
- Scope note: after W30-4, Phase 1 never runs on url_list — this assert guards nav/list_page/search_term runs (where wrappers are equally likely) and is exercised in unit tests + the nav-mode smoke leg.
- Tests: T1 `def fetch_page(url, **kw)` rejected with actionable message; T2 bare requests closure → warning + meta flag, discovery proceeds; T3 real closure passes clean; T4 write-back stamps real closure only; T5 `_with_retry` asserts too; T6 probe-cap stubs updated and green.

### W30-4 — Execution-flag hygiene: `--fresh-discovery` only where Phase 1 exists  (Batch 1) — SHIP
- `nodes/run_execution.py:842`: append only for `input_mode in {navigation, list_page, search_term}`. Verified rationale: the unconditional append exists for **checkpoint-reuse on nav-family drafts** (`:836-841`); url_list drafts seed-first with `skipped_reason="url_list_mode"` and never write a checkpoint (`requests_scraper.py:453-475`), the flag is a documented no-op there (`:412-416`), and in the api family `_force_fresh` is what causes the url_list catalog crawl (`api_scraper.py:456-463`) — same change defuses it, no template edit.
- **Success criterion (resolves the critique's contradiction):** a 570-shaped url_list draft becomes **unreachable at execution** — its wrong trigger clause never fires, the seed path runs, the job completes. The contract assert (W30-3) is what guards Phase 1 where it legitimately runs. E2e criterion updated accordingly (see E2E).
- Tests: T1 url_list CLI carries no `--fresh-discovery`; T2 nav/list_page/search_term keep it; T3 checkpoint-reuse still works for nav-family (locumtenens regression guard); T4 api-strategy url_list job runs the user's URLs, not a catalog crawl.

### W30-5 — Deterministic pre-execution smoke run  (Batch 1) — SHIP-WITH-CHANGES
After a tester PASS, the code_tester **node** (deterministic, not the LLM) calls the scraper runner directly — **no cap collision**: `RUN_SCRAPER_CAPS` (`subagents.py:200-231`) binds only `agent_name == "code_writer"` per-invocation in-memory (`:1594-1614`); code_tester is uncapped by design.
- **Flag set = exactly what run_execution will pass** (incl./excl. `--fresh-discovery` per W30-4).
- **Per-strategy smoke budget** (the ~54s figure is the HTTP best case): http/api drafts ≤300s; browser drafts ≤900s (`BROWSER_RUN_TIMEOUT_FLOOR=600`, shell_tools.py:36); a nav-family smoke carrying `--fresh-discovery` gets its Phase-1 leg capped via `SCRAPER_DISCOVERY_MAX_PAGES` (probe-cap idiom). Skip when the draft failed the compile gate.
- Non-zero exit → test FAIL with the stderr tail (contract messages reach the fix loop). Smoke output persisted for the tester LLM's report.
- Tests: T1 nav-mode draft with a broken discovery callable FAILs smoke with the contract message; T2 healthy url_list draft passes via seed path; T3 smoke respects per-strategy budget; T4 skipped on compile-fail; T5 output persisted.

### W30-6 — ToolCallLog: persist the abandoned trail  (Batch 2) — SHIP-WITH-CHANGES (narrow)
Narrow version per critique (removes dedup-key fragility): when an invocation is abandoned, **then** persist the real-time rows the callback saw (post-join flag; the callback keeps an in-memory per-invocation call list). Healthy invocations keep the existing return-value path — zero duplicate risk, no join key needed. Note recorded: `call_seq` uses `count()` and is non-monotonic under interleaved writers (harmless today).
- Tests: T1 abandoned invocation has real rows + synthetic death rows; T2 healthy invocation unchanged (no duplicates); T3 `/jobs/<id>/tool-calls/` shows writer calls for a wall-clock job.

### W30-7 — Zombie telemetry  (Batch 2) — SHIP
Latch refusal path (`subagents.py:1083-1122`, raises before `on_tool_start`) writes one SessionLog `[ZOMBIE-REFUSED]` row per refused call (first 10 per invocation) + state counter. No behavior change. Tests: T1 refused call logged; T2 cap respected; T3 healthy invocations unaffected.

### W30-8 — browser_traverse heartbeat + ceiling  (Batch 2) — SHIP
Heartbeat row every 300s into SessionLog (agent-heartbeat idiom); env `NAV_TRAVERSE_MAX_TIMEOUT` (default 3600) hard ceiling → honest stop with reason. Verified: no deadline exists on the traversal path today. Ops task (non-code): grep celery logs for the 569 window to confirm/refute Redis traversal-lock contention with concurrent 570. Tests: T1 heartbeats appear >300s; T2 ceiling stops honestly; T3 short traverses unchanged.

### W30-9 — Step finalizer: never-run ≠ done  (Batch 2) — KEEP, LABELED COSMETIC
Wave-26 already deferred this as cosmetic; nothing in either RCA shows it costing wall-clock or money. Kept last; must not consume review time ahead of W30-8. Tests: T1 unwritten phases finalize `skipped`; T2 run steps still `done`.

## Job-level budget math (new, from critique)
Task budget = 12960s soft − 360 margin = **12600s usable**. Hostile-case writer path: inv 2700 (W30-1 cap) + finisher 1800 + re-ladder inv 2700 + tester windows ≈ clamp-saturated. `WRITER_MAX_TIMEOUT=2700` (not 3600) keeps the worst case inside the task budget so the finisher is rarely clamp-refused; the `_effective_timeout` clamp remains the de-facto job ceiling, and clamp-refusals are a **named, tested outcome** (W30-1-T6, W30-2-T5) rather than silent weirdness.

## Explicitly NOT doing (this wave)
- Killing/aborting zombie writer threads (async-path work, parked).
- Writer prompt/brief rewording for the discovery convention (refuted as sufficient by 570).
- W25-e pre-seeded drafts for NEW jobs (task #62 stays deferred; W30-2 is the salvage slice).
- LLM-latency work (145–161 s/turn is the provider's).
- Navigation/template behavior changes beyond W30-4's flag gating.

## E2E validation (after Batch 1+2 green)
1. Full suite + ruff (local, both containers restarted).
2. Local replay drives: revolveclothing.com.au listing (569 shape) + revolve.com product url_list (570 shape).
   - 569 shape: pass = no INVOKE-TIMEOUT, **or** finisher salvage reaching the tester.
   - 570 shape: pass = **completes via the seed path** (flag hygiene); the contract assert is proven by W30-3 unit tests + the nav-mode smoke leg, NOT by a url_list e2e.
   - `/tool-calls/` shows writer rows on any wall-clock job.
3. EB sync per standing rules (branch + fork main + merge-tree), user PR → Railway; verify via `/api/version/` SHA, then one prod replay drive.

## Rollout/rollback
All new limits env-gated with conservative defaults (`WRITER_INVOKE_TIMEOUT` defaults to today's window; `WRITER_MAX_TIMEOUT` 2700; finisher once-per-job). Rollback = unset envs; W30-3/4/5 are revertible by single-commit revert.

## As-shipped deviations (implementation record)
- **W30-5 re-scoped (honest deviation):** the plan's "deterministic pre-execution
  smoke run" already EXISTS — `_probe_phase1_discovery` (graph.py, job-316/324/85
  lineage) smoke-tests every nav-mode draft (execution listing + identity +
  flags, 180s bound, `SCRAPER_DISCOVERY_MAX_PAGES=3`, crash → forced FAIL), and
  url_list deliberately gets no probe (the tester's `--sample` owns the seed
  path — a url_list smoke would duplicate it). The MISSING piece was flag
  PARITY: the probe appended `--fresh-discovery` unconditionally while
  run_execution (post-W30-4) gates it. Shipped: the probe's flag decision now
  shares `_wants_fresh_discovery` (one decision, two consumers) + behavioral
  pins (tests/test_wave30_smoke_probe.py). A per-strategy smoke budget beyond
  the existing 180s bound was NOT added.
- **W30-6 narrowed as planned:** the abandon site in `_invoke_agent_with_timeout`
  persists the callback's in-memory call list once (`trail_persisted` flag);
  provenance string marks rows as real-time trail. `call_seq` uses `count()`
  (non-monotonic under interleaving — accepted, as planned).
- **W30-7:** counter lives in `tools/context.py` (`note_tool_refusal` /
  `get_invocation_refusal_count`, keyed by invocation id, capped at 512 like
  the latch set); SessionLog rows written by `_log_zombie_refusal` in
  subagents.py (first 10 per invocation, telemetry failures swallowed).
- **W30-8:** env `NAV_TRAVERSE_MAX_TIMEOUT` is resolved inside
  `experimental/nav_traversal/traversal.py` (single read site; default 3600);
  the graph wrapper only injects the `[NAV-TRAVERSE]` SessionLog heartbeat
  writer closure (`_traverse_heartbeat_writer`, 300s interval). A ceiling stop
  is an honest not-reached whose notes name the ceiling; it flows into the
  existing bounded HTTP `traverse()` fallback.
- **W30-9:** new `Step.STATUS_SKIPPED` ("skipped") + choices migration 0041;
  PENDING steps finalize skipped, RUNNING keep DONE; closing block extracted
  to `_close_open_steps(job)`.
- **Test collaterals from W30-4:** the flag probe in run_execution only runs
  `if args:` — the unconditional `--fresh-discovery` append used to make args
  non-empty for every job. tests/test_draft_corruption_gates.py fixtures now
  pass `scope=firstn` (keeps args non-empty without engaging mode gates);
  tests/test_f5_heartbeat.py window widened 10→20 lines (the finisher guards a
  6-kwarg invoke).
- **Pre-existing (NOT this wave):** tests/test_truncation.py
  test_non_seed_message_IS_capped, tests/test_views.py
  TestAdminJobVisibility::test_regular_intake_jobs_lists_own_only,
  tests/test_filesystem_tools.py test_oversized_file_mentions_offset, and 3
  experimental/nav_traversal/test_traversal.py browser tests fail identically
  at HEAD (snapshot-era fakes vs `_read_page_state_with_retry`); confirmed by
  stash-baseline runs.
