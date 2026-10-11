# Wave-59 Plan v3: writer working memory + deterministic compile gates

**Objective:** pass rate. R2 scored 1/3 (624 jomashop PASS pc=2; 623 jjshouse / 625 wonderbly honest FAILs, 0 rows, ~17,250s of writer window burned). Both failures share ONE measured mechanism: the writer's working memory collapses to 6 messages, so it cannot retain what it just wrote. This wave fixes the memory and adds deterministic compile/repair gates so a window death converts into a draft instead of a cleanup.

**Evidence base** (RCA subagent, turn-by-turn, HEAD d4399cb; all pins re-verified by orchestrator):

| Observation | Where measured |
|---|---|
| `Truncated messages: 280 → 7` / `295 → 7` (623 inv2), `28 → 1` (625 inv1) | celery log, truncation hook step 2 |
| 114 draft re-reads, 0 edits, 88% of turns = read_file (623 inv2) | turn→tool attribution |
| 625 inv2: GraphRecursionError 452 at 2303s (~15s/turn), ~400s window unused | writer windows |
| 623 died twice on ONE never-checked syntax error (`re.compile(...-g\d+", re.I)` lines 107 then 116); 0 syntax checks in the final 77 min | draft fingerprints + tool ledger |
| The ONE bounded embed (finisher, 47,226 chars) did the batch's only sustained repair (24 edits, 4 syntax checks) | 625 finisher window |
| `[WRITER-EMBED]` 93,112 ×2 fresh inits → post-trim budgets 62,387/66,880 vs 115,062-130,663 for small templates | SessionLog |
| 623-inv1 (http_requests) burned 2700s proving requests can't see a client-rendered listing the class-1 predicate already knew | inv1 window |

---

## C1 — Memory floor: the truncation budget walks the WHOLE history

**File:** `webapp/agents/subagents.py:1166-1168` (hook `_hook` :996-1219)

`kept_recent = other[-_MIN_KEEP_RECENT:]` pre-slices history to the last 6 messages; the budget walk at `:1180-1187` then only ever sees those 6. **Fix:** pass the full step-1-trimmed `other` to the walk — the newest-first budget loop becomes the limiter. `_MIN_KEEP_RECENT` stays as a HARD floor of 6 (guardian #3, single semantics): if the walk selects <6, take the newest 6 REGARDLESS of budget — explicitly stated: the hard floor may ship an over-budget payload in the all-oversized case (identical to today's unconditional `other[-6:]`, so C1 is bounded above by max(today, budget)). Pair-safe pass (`:1191-1206`) unchanged. Kill-switch `LLM_TRUNCATION_MODE='off'` unchanged.

**Honest magnitude (critic-1 corrected):** standalone at a 62k budget the gain is small (+1: the newest-first walk selects the same prefix either way — today 7 kept → 8). The real story is COMBINED with C2: at the doubled ~109k budget the walk keeps ~10 messages and the spiral onset moves from turn ~4 to past turn ~15. Tests assert the combined regime, not the standalone one.

**Tests** (`tests/test_wave59_writer_memory.py`): kept-count grows with budget; a synthetic 623-shaped transcript (280 messages, 24k tool results, 62k budget) keeps ≥8; seed never trimmed; orphan ToolMessage still dropped; 'off' no-op; hard min-6 floor holds even when every message is oversized AND ships over budget (assert the over-budget outcome explicitly).

## C2 — Embed diet activation for file templates + fix the stale W24-6 lie (guardian-revised)

The mechanism is complete and PROVEN (finisher's bounded embed did the best repair of the batch) and the settings default already enables it (`settings.py:349` `default="bounded"`). It is dormant through one call-site word. The critics proved the naive wiring hides the machinery: `templates/http_navigation_scraper.py` is 92,550 chars; HEAD/TAIL = 20k/20k (`draft_context.py:28-29`) elides chars ~17.5k-72.6k — **every phase-1 function the frame orders "use VERBATIM — do not rewrite discovery/pagination"** (`_navigate`@17,483; `_get_next_page_url`@29,601; `_extract_item_links`@34,633; all four `_discover_urls_via_*`@38,975-58,837).

**Guardian correction of the premise (verified in-container):** `templates/` IS present in the celery writer container (Dockerfile:23 baked + `.:/app` bind; orchestrator re-verified: 92,550 chars readable) and file reads are project-root-wide (`filesystem_tools.py:334-336`, `:385`). The W24-6 do-not-read note (`draft_context.py:44-47`, `subagents.py:1237/1253`) and the stale comment at `graph.py:8392` ("kill-switch (default \"full\")") are FALSE TODAY — a known-false statement in the writer's prompt is exactly the dishonesty this wave bans. Therefore:

- **Wiring:** `webapp/agents/graph.py:8399-8404` — `classify_embed(..., eow_active=(_eow_active or None))`: fresh writes classify on SIZE alone (the finisher's existing golden → "bounded" iff ≥40k); EOW fix cycles keep the True golden byte-identically.
- **Primary fix — make the pointer TRUE:** the bounded embed's note for file-kind points at the REAL path (`templates/{template}.py`, verified present) and instructs targeted recovery: `read_file` with the map's line spans (reads are project-root-wide; 24k-capped results). The W24-6 do-not-read note is corrected, not deleted: the true statement is "the elided bodies live at `templates/{template}.py` lines N-M (see the index below) — read those spans; do not copy the whole file into context".
- **Reference-file materialization = optional robustness, NOT required:** the node MAY still mirror the template to `workspace/{slug}/template_reference.py` for workspace-scoped tooling, but no test hard-requires the artifact.
- **Frame text for file-kind** tells the truth: machinery indexed below with line spans, readable at the real path; the do-not-define-inline rule stays.
- **Blast radius (guardian #4): TWO templates exceed 40k and are dieted — `http_navigation_scraper.py` (92,550) AND `navigation_scraper.py` (~57k), i.e. the whole navigation family fleet-wide. Both named; both pinned in tests.**
- **Named residual risk (critic-2, accepted):** first-draft quality now depends on the writer actually pulling elided spans; C3 cannot catch a missing-body draft (compiles fine, dies as runtime NameError). Mitigation: the ast map names every elided def with line spans and the note says so.

**Effect:** 93,112 → ~46k embed; fresh-writer history budget 62,387 → ~109k (+75%); elided machinery reachable by targeted `read_file`.

**Tests** (`tests/test_wave59_embed_file_diet.py`): big templates (http_navigation AND navigation) → bounded + note carries the true templates/ path + map line spans; small → full, byte-identical legacy; EOW True path unchanged; `[WRITER-EMBED]` stats still emitted (format pinned at `tests/test_wave25e_embed_redesign.py:120`). **Update BEFORE the suite:** `tests/test_wave32_eow_diet.py` (`test_first_cycle_still_full_embed`'s contract is inverted — pin the new size-only fresh-write classification instead), `tests/test_wave25e_embed_redesign.py` (render_writer_embed signature, embed_kind threading).

## C3 — Deterministic pre-write compile gate (critics 1+2: use the existing seam)

**File:** `webapp/agents/tools/filesystem_tools.py:65-124` — the `_f821_rejections` pre-write gate ("two gates, one seam"), already writer-scoped via `syntax_gate=(agent_name == "code_writer")` (`subagents.py:1663`). NOT the `_apply_guards` pipeline.

Add a THIRD check to the existing seam: `ast.parse` the incoming content before the write lands; on SyntaxError REJECT the write (file untouched — consistent with the B1/W25-d rejection contract, `filesystem_tools.py:57-62`) with a tool-result error: `SYNTAX ERROR line N col M: msg — fix this line and retry`. Critic-1 verified by execution that today BOTH existing gates open for 623's exact bug (`re.compile(r"...", re.I` unterminated literal): F821's message filter drops the invalid-syntax row (`:101-104`) and `draft_call_violation` returns "" on unparseable (`draft_safety.py:788-791`) — so the bad write succeeds, which is exactly how 623 died. 3-consecutive-rejection closure via the `_rs_budget` precedent (`subagents.py:2014`): on the 3rd consecutive syntax rejection the tool result escalates to "rewrite the file fresh with write_file" and the block lifts (never wedging the writer). **Counter discipline (critic-2 ordering trap): the strike counter counts REJECTIONS (pre-write, file unchanged) — never parse "current file state", which after a rejection is stale content.**

Cost (critic-2): ast.parse on 150KB is tens of ms; the seam already spawns a ruff subprocess per write — non-issue. Honesty counters unaffected: `writer_dup_read_count` counts read-only tool names (`graph.py:9022-9029`), a rejected write is not a read; arg-stub hook retains the latest tool-call turn's full args so the writer can edit immediately after a rejection; zombie latch watches the join thread, not tool results.

**Effect:** 623's terminal bug was ONE unterminated string literal written at 19:29 and never checked — inv2+inv3 then ran 77 more minutes and both windows died "no draft". This gate stops the bad bytes at the write.

**Tests** (`tests/test_wave59_compile_gate.py`): 623's exact regex shape → rejected with line/col; clean write → unchanged; 3-strike escalation lifts the block; counter counts rejections not file state; non-draft files untouched; gate exception degrades to no-op (never blocks a write); existing F821/call-violation gates still pass through the shared seam.

## C4 — Strike accounting + the finisher repairs unparsable drafts (critics 1+2 revised)

**Files:** `webapp/agents/graph.py:8575-8667` (draft-ok check + no-draft arm), `:7873-7880` (finisher pre-gate), `:8714`/`:8734` (draft-exists wall ledger + two-strike gate)

- **(a) Finisher repair arm:** in the no-draft arm, when a death leaves an UNPARSABLE draft on disk with ≥200 lines, run ONE `_run_draft_finisher` window with the SyntaxError (line/col) in the seed BEFORE honest cleanup (:8653-8667). Finisher pre-gate PARAMETERIZED (guardian rec): `require_parses: bool = True` — the existing draft-exists arm keeps its defensive parse invariant; the new arm calls with `require_parses=False` + a size floor (≥200 lines); `draft_parses` stays as the POST gate (:7967 — outcome contract unchanged). Critic-1 cleared the key risk: `_run_draft_finisher` never parses before the pre-gate and the bounded embed degrades by design on unparsable input (raw head/tail cuts, map returns "" — `draft_context.py:81-86`, `:116-129`). **The once-per-job latch is explicit:** the new arm CHECKS and SETS `writer_finisher_attempted` (:8813/:8827) — a job can never buy two finisher windows. **Ordering (critic-2):** the finisher runs NO provider window gate (`:7941-7952`), so the new arm sits INSIDE the non-provider-dead branch (after :8636) — a provider-dead double death parks without an uncanaried 1800s burn.
- **(b) Ledger completeness — NO second increment (guardian #2):** the no-draft arm ALREADY increments the wall/recursion ledgers (`graph.py:8604-8615`, pinned `tests/test_wave22_writer_timeout_accounting.py:73-86`) — adding another increment would double-count and could strike-2 the job after ONE death, aborting a salvageable run (a gate veto = missed pass). The real gaps, both consumer-side: (i) `writer_step_budget_deaths` (:8460) is NOT in the strike sums today — add it to the summed death ledger and to `_writer_extension_ceiling` (:2865, which today sums only wall+recursion — verify and extend); (ii) NO death-aware gate consumes the ledger on the no-draft path (:8623 reads only the error count) — C4(a)'s finisher arm IS that consumer, keyed on the death ledgers + a substantial unparsable draft. Preserve the wave-22 pinned literals exactly (`update["writer_wall_clock_timeouts"] = 0` reset, unconditional increment).
- **(c) Two READERS, two semantics — do NOT collapse them (critic-1 blocker + critic-2 trap):** the no-draft arm's strike decision (:8623) KEEPS reading `code_writer_error_count` (consecutive no-draft failures of ANY class, including live no-ops that never strike a window ledger — replacing it with a deaths-only sum would make the cheap `code_writer → scraper_analyzer` live-no-op loop unbounded to the task deadline). The draft-exists two-strike gate (:8734) keeps reading the summed DEATH ledger and **keeps its exact expression** `if _wc + _rec_deaths >= 2:` — `tests/test_wave30_draft_finisher.py:284-288` slices the source on that literal. Invariant, tested: a death increments at least one of the two readers — no death class is invisible to both.
- **(d) C4×C7 interplay (critic-2's catch), resolved:** C7's rotation on a strategy delta removes exactly the draft this arm repairs — so on a delta sequence, strike 2 arrives with NO draft and the honest outcome is cleanup-after-rotation, BY DESIGN (repairing a draft for an abandoned strategy is a wasted window; the ladder is monotonic — the archived strategy never returns). The finisher fires on SAME-strategy strike-2 sequences. The wave's bet for 623 is C1+C2+C3 preventing the deaths, not the finisher.
- **(e) Replays:** same-strategy: wall-with-draft → wall-leaving-unparsable-draft = NO third writer window, ONE finisher attempt, then honest cleanup. Delta: strike-1 death + delta → rotation, strike-2 death → cleanup with NO finisher (assert the negative).

**Effect:** a same-strategy two-strike job with a substantial unparsable draft gets its repair window; the 623-class delta sequence gets a coherent fresh-draft retry instead of a wrong-contract edit.

**Hard test pins to update BEFORE the suite (critic-2):** `tests/test_wave30_draft_finisher.py:326-333` (exactly-once `_run_draft_finisher(` in `_invoke_code_writer` — replace with a per-arm count) and `:284-288` (keep the literal slice target); `tests/test_wave22_writer_timeout_accounting.py:73-100` (literals preserved). New tests (`tests/test_wave59_strike_ledger.py`): (a) same-strategy strike-2 + substantial unparsable draft → finisher invoked, not cleanup; finisher success → salvage; finisher fail → honest cleanup; latch set → second arrival skips to cleanup; (b) delta sequence → rotation, no finisher; provider-dead double death STILL parks (:8636 arm reachable, no finisher before park); uncompilable + step-budget deaths reach the summed gate; small/absent draft → straight cleanup.

## C5 — Recursion floor must not outrun the wall it serves (critic-2: reclassification DROPPED)

**File:** `webapp/agents/graph.py:1213` (`_WRITER_ROUNDS = 150` → 200; comment at :1210-1212 states the intent "the wall clock is the backstop again")

At the measured 15s/turn fast-turn regime, 150 rounds ≈ 2250s < the 2700s wall — the step ceiling bound BELOW the wall (625-inv2 died at 2303s with ~400s unused). 200 rounds ≈ 3070s > 2700: wall is the backstop again at both observed turn rates (critic-1 caveat, accepted: an average <4.5s/super-step can still recurse below the wall — probabilistic, not guaranteed; H12 is fixed by the wave-52 zombie latch, so the wall does not silently vanish).

**Critic-2 killed the second half of the original lever:** reclassifying fast-ceiling recursion deaths as wall-class would make them T12-eligible (a writer that just proved it cannot converge in 200 rounds becomes eligible for a doubled grant — the exact burn S3 exists to prevent, `graph.py:2852-2858` rationale + `tests/test_wave58_writer_convergence.py:141-174` pin) and would land them in `writer_wall_clock_timeouts`, silently subjecting the next window to the F6 cap (`:2807`). Both are polarity flips on the flagship death shape. **DROPPED — recursion deaths stay recursion-class; the raise to 200 is the whole fix.** The wave-58 S3 rationale test is amended to document the 200-raise, not the classification.

**Tests** (`tests/test_wave59_recursion_floor.py`): limit == 602 (formula per `tests/test_wave32_writer_rounds.py:46-47`, `test_wave58_writer_convergence.py:50`); recursion deaths still NEVER T12-eligible (S3 pin preserved); ceiling sums unchanged.

## C6 — Strategy truth: listing-seed url_list jobs skip the http_requests rung

**File:** `webapp/agents/graph.py:5639-5680` (`_apply_listing_seed_strategy`)

The wave-58 1c predicate upgrades api/internal_api → http_navigation but deliberately spares http_requests (:5648-5650 "requests_scraper runs SCRAPER_LISTING_URL discovery in-process"). R2 proved that branch cannot see a client-rendered listing: 623-inv1 burned its whole 2700s window returning 0 items. **Fix:** widen the predicate LOCALLY (:5667, per critic-2's trap: `API_STRATEGIES = frozenset({"api","internal_api"})` at `constants.py:85` must NOT be widened — flipping it would move requests drafts onto the API nav-flag family for every other consumer, `constants.py:125-126` + `run_execution.py:253` + `subagents.py:4439`) → the ladder STARTS at http_navigation for listing-shaped seeds. **Alias constraint (critic-1):** the alias loop at :5672-5676 must also treat `http_requests`, else `scraping_method`/`scraping_mechanism` stay stale while `strategy` moves — the finisher seed reads `scraping_method or strategy` (:7887-7889) and the listing-seed template override reads `mechanism` (`subagents.py:4437-4439`), so a stale alias silently neuters both. Stamp reason: "listing-shaped seeds — single-phase HTTP rung cannot see client-rendered listings". Item-shaped seed sets are never touched (:5660 predicate).

Fleet safety (critic-2's scan): 84 site dirs in `scrapers/` — ZERO shipped scrapers are http_requests + listing-shaped seeds (only jjshouse-com and woolworths-com-au have listing-shaped seeds at all; neither ships a requests scraper). The browser-capacity cost lands only on the 623-inv1 class that today returns 0 rows. The requests env-gated two-phase branch (`templates/requests_scraper.py:563-572`, armed at `webapp/agents/nodes/run_execution.py:1246-1270`) becomes unreachable-IN-COMBINATION with listing seeds — recorded as DELIBERATE (623-inv1 proved it dead); the wave-58 1b run_execution arm stays (it also feeds the http_navigation template's env gate, `:1729-1734`). Ladder coherence: `_STRATEGY_ESCALATION` is monotonic (`graph.py:5856`) and the upgrade runs before escalation (:5755), never re-picking a burnt rung.

**Tests** (`tests/test_wave59_listing_strategy_truth.py`): listing seeds + probe pick http_requests → http_navigation + aliases updated; listing seeds + api pick → http_navigation (existing 1c behavior); PDP-seed url_list → unchanged ladder (http_requests survives); non-url_list → untouched; API_STRATEGIES frozenset pinned unchanged. **HARD PIN to rewrite BEFORE the suite (critic-2): `tests/test_wave58_listing_seed_runtime.py:360-370` `test_http_requests_strategy_is_kept` asserts the exact opposite of C6.**

## C7 — Draft rotation on strategy delta at death-retry

**Files:** rotation `_archive_dead_strategy_draft` (`graph.py:7424-7455`) + `_decide_strategy` (:5685, prior-vs-new comparison already computed at :5815-5817)

Critic-1 confirmed the premise: rotation fires only from `_strategy_axis_escape` (:7519/:7539), reachable only from tester-bounce arms (`_noop_strike1_ladder_bounce` :7552, `_writer_axis_pivot` :7581) — the writer window-death retry (no-draft arm :8685 `goto="scraper_analyzer"` → `_decide_strategy`) has NO rotation, so 623-inv3 inherited inv2's draft under a new brief. **Fix:** in `_decide_strategy`, rotate when the prior strategy differs from the newly picked one AND the arrival is a writer-retry — **arrival signal named (guardian #6): a non-zero `code_writer_error_count` in state (stamped by the no-draft arm at :8604 before its `goto="scraper_analyzer"`); tester-bounce arrivals carry `test_retry_count`/`noop_fix_cycles` instead, so the two paths are distinguishable without a new state key.** Call `_archive_dead_strategy_draft` (best-effort, collision-suffixed, safe no-op with no draft :7444; the wave-57 presence pin `tests/test_wave57_r3_dead_draft_rotation.py:198-200` breaks nothing). No thrash risk (critic-2): the ladder is monotonic up and the tier axis keeps the strategy — pivot-back cannot occur within a job or across a resume (`strategies_tried` restored from artifacts). Minor: rotation clears `last_writer_template` (:8895). **C4 interplay is resolved in C4(d): rotation on delta means the strike-2 finisher correctly does NOT fire on delta sequences.**

**Tests** (`tests/test_wave59_death_retry_rotation.py`): strategy-delta death-retry → draft archived + last_writer_template cleared; same-strategy death-retry → draft preserved; combined C4(d)/C7 ordering test (delta: rotation then cleanup-no-finisher; same-strategy: finisher fires).

---

## Explicit CUTS (instrumentation-only — deferred per fix-not-label directive)

- **Error-message accuracy** (623's PARTIAL message; 617's invented "repeated 900s timeouts" lie): honest-recording-only → backlog.
- **Mid-window churn detector**: root cause (amnesia) is C1+C2; a detector only kills windows earlier → contingent backlog, revisit only if R3 still shows 100+ re-read spirals.
- **Draft-size diet** (1045-1508-line drafts): wave-60 candidate.
- **Stale-seed refresh** for jjshouse (`/all|/collection|/shop` wrong shape): C6+C7 give the writer discovery machinery from turn 1; revisit only if R3 shows discovery YIELD failure (distinct from 623's memory failure).

## Commit plan (4 commits, tests written BEFORE each certifying run)

1. **C1** memory floor + tests
2. **C2** embed activation + reference-file materialization + tests
3. **C3+C4** pre-write compile gate + strike accounting + finisher repair arm + tests
4. **C5+C6+C7** recursion raise + listing-seed strategy truth + death-retry rotation + tests

**Existing test files to update BEFORE the certifying suite (critic-2's named pins):**
1. `tests/test_wave58_listing_seed_runtime.py:360-370` — rewrite `test_http_requests_strategy_is_kept` (C6 flips it).
2. `tests/test_wave30_draft_finisher.py:284-333` — replace the exactly-once pin (C4 adds a second call site); keep the `if _wc + _rec_deaths >= 2:` slice literal; add provider-dead-parks-without-finisher ordering.
3. `tests/test_wave22_writer_timeout_accounting.py:73-100` — preserve the pinned ledger increment/reset literals (C4b).
4. `tests/test_wave32_eow_diet.py` — invert `test_first_cycle_still_full_embed` to pin size-only fresh-write classification (C2).
5. `tests/test_wave25e_embed_redesign.py` — render_writer_embed signature (embed_kind threading, C2).
6. `tests/test_wave58_writer_convergence.py:50,141-174` — amend S3 rationale for the 200-raise (C5 drops the reclassification).

## Acceptance criteria

- All new wave-59 tests green; full gate baseline-identical (5 failed / 4,932 passed / 2 skipped / 1 xfailed / 11 errors); `ruff check webapp/ src/` clean.
- Worker restart protocol honored: `docker compose restart celery-worker` + `/proc/1` age verification (R1 lesson — `up -d` does not reload bind-mounted code).
- **e2e R3 bars (guardian #5 — control bar explicit):**
  - **624 jomashop (control):** remains PASS, pc ≥ 2, no junk-gate regression — this is the fleet-wide C1/C2 regression tripwire.
  - **623 jjshouse (flagship):** ≥3 off-seed rows or honest FAILED at a captcha/akamai wall ONLY.
  - **625 wonderbly:** PASS or honest FAILED; no recursion death below the wall.
  - Mechanism instrumentation on all three: fresh-writer budgets ≥100k on big templates, truncation kept-count ≥8 on long invocations, ≥1 compile-gate save if any syntax error is written, finisher clamp held (exactly ≤1 finisher window per job), no second ledger increment in the no-draft arm.

## Guardian build constraints (MUST NOT)

1. Do NOT widen `API_STRATEGIES` — the fix is local to `graph.py:5667` + the alias loop.
2. Do NOT touch `templates/*` semantics.
3. Do NOT add a second ledger increment in the no-draft arm (double-count hazard → premature abort of salvageable jobs).
4. Do NOT let the finisher arm run outside the non-provider-dead branch (no provider canary in `_run_draft_finisher`).
5. Do NOT resurrect the C5 reclassification (recursion deaths stay recursion-class; T12 stays wall-only).
6. Do NOT fire R3 before `docker compose restart celery-worker` + `/proc/1` age check.

---

## Build record (2026-10-11)

All four commits landed on `wave-42e-antibot-section`, tests written BEFORE each certifying run:

| Commit | Scope | Tests |
|--------|-------|-------|
| `8015a33` | **C1** truncation budget walks the whole history with a hard min-6 floor | 9 new (`test_wave59_writer_memory.py`) |
| `1f4a547` | **C2** embed diet activates for FILE templates (`eow_active=(_eow_active or None)`); kind-aware truth notes (W24-6 lie fixed) | 10 new + wave-32 pin inverted |
| `bf7c461` | **C3** gate 0 pre-write compile gate at the `_f821_rejections` seam (bounded 3-streak, `SCRAPER_DRAFT_SYNTAX_GATE=0`); **C4** `writer_step_budget_deaths` declared (was langgraph-stripped → always 0), joined to the F7 ceiling sum; no-draft strike-2 REPAIR arm (`require_parses=False`, ≥200-line floor, error in seed, latched, no second increment, salvage falls through to the test ladder) | 26 new + wave-30 exactly-once → exactly-two |
| `3721651` | **C5** `_WRITER_ROUNDS` 200 (limit 602); **C6** listing-seed walk widened to http_requests (LOCAL, `listing_seed_reason` stamp); **C7** death-arrival strategy delta rotates the dead draft + clears the EOW arm | 22 new + wave-58 pins amended |

Guardian MUST-NOTs: all six honored and test-pinned (API_STRATEGIES untouched; templates/ untouched; no second ledger increment; repair arm inside the non-provider-dead branch; no reclassification; R3 gated on the worker restart).

**Acceptance**: pending the full gate (baseline 5 failed / 4,932 passed / 2 skipped / 1 xfailed / 11 errors) + lint. C3+C4 neighbors 166 green; C5-C7 neighbors 94+13 green; ruff clean at every commit.
