# wave-56b implementation plan (drafted mid-batch, untracked)

Gated on: all 9 batch jobs (580-588) terminal + guardian final checkpoint.
RED tests: tests/test_wave56b_*.py (8 files, 54 tests; new contracts RED,
regression guards GREEN as of c331b03). Implementation edits are FORBIDDEN
until the batch scores — these notes make the turn mechanical.

## Fix list (evidence → contract → RED file)

### F3b — transport-wall directive speaks the real probe vocabulary
- Site: `webapp/agents/subagents.py` wall block (~:3476-3506, inside
  `_summarize_test_report` :3174).
- Today: `_probe_method.startswith(("browser","fingerprint","stealth"))` —
  dead for playwright/cloak/uc_chrome (real vocab: `direct_http*`,
  `fingerprint_<profile>_<tier>`, `playwright_<tier>`, `cloak_<tier>`,
  `uc_chrome*`; stealth-class = `STEALTH_METHOD_PREFIXES = ("uc_chrome",
  "cloak")` in src/constants.py:67), and WRONG for `fingerprint_*` (probe
  proved HTTP-with-browser-TLS worked → directive must say "replicate the
  proven TLS client", never "move the fetch layer to the browser-class
  method").
- Contract: render-class prefix tuple = ("playwright","cloak","uc_chrome",
  "browser","stealth") → existing browser-prescription; "fingerprint" → NEW
  TLS-replication directive naming the method; `direct_http*` → silent.
- **PROBE-TRUTH CAVEAT (guardian checkpoint 1):** 580's probe cache held
  `method=fingerprint_chrome_none` with `outcome=timeout` — a stale label
  amplified into a directive. The fingerprint arm must hedge on probe
  outcome (if the probe run itself timed out at that method, the honest
  line is "no transport proved — use the browser-class rung", i.e. the
  render-class arm), not assert "HTTP-with-browser-TLS worked".
- RED: tests/test_wave56b_transport_directive.py (13 tests; 5 already green
  = silence/legacy arms).

### F2b — strike 1 spends the proxy-tier axis at the top rung
- Site: `webapp/agents/graph.py:_noop_strike1_ladder_bounce` (:7120-7164).
- **GUARDIAN CORRECTION (checkpoint 1): F2 is RUNG-DEPENDENT, NOT DEAD** —
  it fired LIVE on job 582 at 02:58 log-time (`no-op strike 1 — …
  strategy 'http_navigation' → scraper_analyzer`, graph.py:8560). Scope
  narrows to the TOP RUNG ONLY: from playwright `_escalate_strategy` always
  returns a goto (internal_api skipped) → `if goto: return None`. Zero
  bounce rows on 580/581 is explained by their rungs (580: legacy
  fall-through from http_requests pre-bounce wiring path; 581: convergence
  guard path), not by universal deadness. Do NOT "fix" the mid-ladder arm —
  it already works.
- Contract: when `_escalate_strategy` returns goto (ladder exhausted) AND
  two-dim ladder enabled AND a higher tier is configured, return
  `{"scraper_analysis": {**analysis, "proxy_tier": <next>},
  "strategies_tried": [{"strategy": cur, "reason": "no-op fix loop:
  climbing proxy tier to <next> ..."}]}`. Pure (no caller-state mutation).
  Tier exhausted → None (strike 2 owns exit). Disabled two-dim → None.
  Use `_escalate_tier_axis` region logic (:5634-5647) / `_PROXY_TIER_LADDER`
  (:5601). Tests monkeypatch `_tier_configured` → True.
- RED: tests/test_wave56b_toprung_bounce.py (9 tests; 5 green = decline
  arms that already work).
- NOTE: live 580 log shows cycle-1 no-op went straight to re-test from
  http_requests with tried=[] — my unit test of that exact shape returns a
  payload TODAY, so the live decline came from caller context (verify the
  call site's `all_tried` source during implementation; do not assume).

### F5b — leaf exemption refuses section-shaped suffixes; digit arm dies
- **ACCEPTED FALSE-NEGATIVE RISK (guardian cp2.4, record in F5 notes):**
  `_exempt_leaf_item`'s `<slug>_<digits>` pattern also excuses SECTION
  links shaped the same way from the bar (which then needs ≥80% suspicious
  share on the remainder). Books' real PDPs are the counterexample proving
  the pattern is usually right. If a future junky pass appears, trace it
  HERE first before blaming discovery.
- Site: `src/discovery_quality.py:_exempt_leaf_item` (:130-152).
- Contract: suffix arm keeps books fix but ANY segment (last one
  suffix-stripped) in `_NAV_TERMINAL_TOKENS` (:70-74) → not exempt; delete
  the standalone digit arm (`len(segs)>=3 and digit`) entirely — books fix
  never needed it; 16% of promoted corpus (1,780/11,027) currently exempt.
- RED: tests/test_wave56b_leaf_terminal_guard.py (6 tests; 2 green =
  books/citybeach regression guards).

### F4b — zero-yield verdict keys on row count, not URL harvestability
- Site: `webapp/agents/nodes/run_execution.py:_url_list_scope_gate`
  (:2521-2577) — needs a row-count read (add tiny `_output_row_count` or
  parse rows inline; `_output_row_urls` :2360-2382 returns [] for both
  empty and url-less rows).
- Contract: rows>0 but none carry `_ROW_URL_KEYS` (:2356-2357) → return ""
  (scope unverifiable ≠ zero-yield lie). Zero rows → diagnostic unchanged.
- RED: tests/test_wave56b_zero_yield_rowguard.py (4 tests; 3 green).

### F1b — differentiation directive honest on execution-recycle reports
- Site: `webapp/agents/subagents.py` DIFFERENTIATION block (:3519-3527).
- Contract: when `feedback_for_writer` starts with "EXECUTION RECYCLE",
  suppress the "was already tested and FAILED" line (the draft PASSED
  testing; it died at execution) and state the true verdict instead.
- RED: tests/test_wave56b_diff_vs_recycle.py (5 tests; 2 green).

### F6b — activity extension reserves the tester floor
- Site: `webapp/agents/graph.py:_join_with_activity_extension`
  (:3048-3091) + main writer call site (:7999-8002) passes
  `reserve_s=TESTER_WINDOW_FLOOR_S` when `writer_wall_clock_timeouts >= 1`
  (mirrors `_writer_post_death_timeout`'s condition).
- Contract: new kwarg `reserve_s: float = 0.0`; job-deadline stop becomes
  `clock() + JOB_BUDGET_FINALIZE_MARGIN + reserve_s >= job_deadline`;
  extension cap also bounded by `job_deadline - margin - reserve_s`.
- RED: tests/test_wave56b_extension_floor.py (4 tests, all RED via missing
  kwarg; fake clock + thread harness included).

### F7 — one writer wall-death retires the extension ceiling (NEW, post-audit)
- Evidence: 580 paid the 2700s extension ceiling on BOTH hangs (second:
  00:16:27→01:07:43 ≈ 3080s) because F6 caps only the base window near the
  END of budget → "Soft time limit exceeded mid-graph" with a validated
  checkpoint unfinalized.
- Site: new `_writer_extension_ceiling(state)` next to `_writer_max_timeout`
  (:2761-2770); main writer call site swaps `max_timeout=_writer_max_timeout()`
  → `_writer_extension_ceiling(state)`.
- Contract: deaths=0 → `_writer_max_timeout()`; deaths>=1 →
  `min(_writer_max_timeout(), _writer_invoke_timeout())` (base window; no
  extension slack for a repeat hang).
- **WHICH KNOB (guardian ask): F7 halves the THREAD-ABANDON CEILING
  (2700 → base 1800). The base window (1800s, WRITER_INVOKE_TIMEOUT) is
  untouched — a healthy slow writer keeps its full window.** Guardian's
  arithmetic: frees ~1350s on a 2-hang job → converts the soft-limit death
  into an honest convergence-guard death (not a pass by itself).
- RED: tests/test_wave56b_writer_ceiling.py (7 tests, all RED).

### F8 — one job, one live execution — **SHIPPED AS HOTFIX bca5076** (2026-10-10)
- THREE live dual-runs in one batch: 582 (task 0b3ce722, pids 23+25),
  584 (task b92a113a, pids 12+3344), 583 (task dc376ae0, pids 3282+3553).
  Mechanism (guardian-verified): the claim's same-id reclaim arm
  `Q(status=RUNNING, celery_task_id=_task_id)` matches ANY redelivery of
  the same task id — alive original or not. All three slots invalidated +
  re-fired: 589 (briscoes), 590 (citychic), 591 (westelm).
- Shipped: `JobExecutionLock` (SETNX-with-TTL, Django cache/Redis, heartbeat
  90s/TTL 300s, daemon-thread renew, finally-release on every exit path,
  fail-closed on cache outage); liveness gate BEFORE the row claim;
  cleanup_stuck_jobs skips held locks. Tests 11/11 GREEN + 87 passed across
  the seven claim/watchdog-adjacent suites. Lint clean.
- Deploy: queue held (cancel_consumer), recreate workers at 586+587 drain,
  consumers reactivate → 589/590/591 start under the lock.
- SCOPE CUT (disclosed): resume_scrape_task does not take the lock in the
  hotfix — deliberate short-lived single continuation; wire it in the full
  wave-56b pass.
- Remaining wave-56b work for F8: resume-path lock + RED test replicating a
  dual-dispatch through the resume flow.

## Guardian checkpoint 1.5 conditions (attach to batch record + wave-56b)
- (a) PER-SLOT CODE VERSION SPLIT goes in the final report's harness-drift
  list: slots 580-585 ran c331b03; 589/590/591/588 run bca5076 (F8). X/9
  may not be presented as one uniform harness. (Plus: two tranches,
  worker scale-up at 21:20, queue-hold + recreate at drain.)
- (b) RESUME HOLE (sharper than my statement): resume_scrape_task
  (tasks.py:1565) drives the graph INLINE (build_graph() :1581, no
  run_scrape_task hop, NO lock); its only duplicate guard is
  status == RUNNING (:1574). A redelivered resume of a WAITING_APPROVAL
  row sails through. Safe in this batch only because e2e jobs run
  skip_approvals. `redispatch_stuck_approved_interrupts`
  (tasks.py:5250) reaches the graph through the same lock-less path.
  Wave-56b: lock both entry points + RED tests.
- (c) worker-1's hold unverified until a lane frees — VERIFY AT RECREATE
  (if worker-1 picks up 589/590/591 the moment 586/587 end, the hold is
  not armed on it → recreate before any queue release; the armed sequence
  already does this — do not reorder).
- (d) Sequence confirmed sound: drain-before-recreate, redeliveries during
  the hold queue unconsumed and fail the lock post-reactivation; terminal
  rows match neither claim arm → refused.
- Residual (wave-56b): promote the renew-failure warning
  (tasks.py:~1367) into the JOB log so a heartbeat lapse >TTL is legible
  in post-mortems. ALSO (guardian cp2): delete `_RedisSetnxStore`'s
  vestigial acquire/renew/_key methods (uncalled, reference lock-class
  attributes, failsafe only).
- Open question carried: the 14-minute redelivery cadence (vs hourly) —
  unexplained; irrelevant once F8 is fleet-wide.

### EDIT-OR-DIE — writer read-spiral guard (NEW, Deep C)
- Evidence: nordstrom 581 — 125 read-only tool calls, 0 edits after cycle
  0; context truncation to 7 msgs/turn; died "identical draft twice".
- Site: new `_writer_read_only_spiral(calls)` + `_writer_spiral_directive(
  reads, edits)` in graph.py (near `_ToolCallLogger` :1244-1271); wiring in
  `_invoke_code_writer` (:7698): classify from `_find_activity_logger(
  agent_cfg).calls`; "warn" → inject force-edit message into the NEXT turn
  (or as a post-invocation directive on retry); "abort" → skip re-invoke,
  record honest no-op strike (reuse `_noop_strike1_ladder_bounce` path /
  noop_fix_cycles state).
- Read-only set: {"read_file","search_content","search_files"}; mutators:
  {"write_file","edit_file"}. warn at >=25 read-only calls with 0 mutators;
  abort at >=50.
- RED: tests/test_wave56b_edit_or_die.py (10 tests, all RED).

### F9 — output-pointer lineage (NEW, guardian cp3 from live 589)
- Evidence (job 589 briscoes, all verified in the pointer file): (a) PRICE-GATE
  dropped 13 rows from the COUNT only — pointer kept pre-gate rows (pc=9845
  vs file 9858); (b) the gate's `0 < price < 0.5` floor missed **92 rows at
  price 0.00 + 214 null-price rows retained** — the annotation understates
  retained bad prices ~8×; (c) dup-titles annotation computed on the gated
  count (9845/9597) while the pointer holds 9858/9609.
- Contract: the output artifact the final pointer names and the published
  count must be THE SAME SET. Either gate before write (pointer gets the
  gated set + honest per-reason drop counts) or rewrite the pointer
  post-gate. The PRICE-GATE floor must key on `price is None or price == 0
  or price < floor` — zero/null are not prices. Annotations (dup-titles
  etc.) must be computed on the exact artifact the pointer names.
- Site: webapp/agents/nodes/run_execution.py PRICE-GATE + finalize path
  (locate the gate drop + output write during implementation).
- RED file: tests/test_wave56b_output_lineage.py (write at implementation
  time; cases: zero-price retained → gate fires; null-price retained →
  gate fires; pointer/count agreement; annotation-artifact agreement).

### F10 — stale-seed substitution on resumed url_list jobs (NEW, guardian final cp, live on 592)
- Evidence: 592 seeded `catalogue/category/books/travel_2/index.html`
  (url_list) but scraped the HOMEPAGE's first three books — the exact
  content of a STALE `scrapers/books-toscrape-com/input_urls.json` left
  by Sep-29 jobs. 592 resumed (skip_code_generation), so its scraper_path
  pointed at the stale `scrapers/{slug}/` dir; `_stage_extra_files`
  (run_execution.py ~617-633) stages the url_list seed from
  `dirname(scraper_path)` → stale artifacts silently replace the user's
  payload on EVERY re-scrape. Cleanup rewrote input_urls.json 23s post-run,
  erasing the evidence. Same family as audit C2 (1227's unrelated-products
  off a 1-URL seed), one branch over. Fleet-wide hazard.
- Contract: the url_list seed staged for execution comes from the JOB row
  (state/url or seed_urls), NEVER from the scraper's directory. Stamp
  `seed_urls_used` into output metadata so payload identity is auditable
  post-hoc (a mismatch between job seed and staged seed must be visible in
  the artifact).
- Site: `_stage_extra_files` + the finalize/metadata writer in
  webapp/agents/nodes/run_execution.py.
- RED file: tests/test_wave56b_seed_provenance.py (cases: resumed job with
  stale input_urls.json in scraper dir → staged seed == job's seed, not the
  stale file; output metadata carries seed_urls_used == job seed; fresh
  (non-resumed) job unaffected).
- Severity note: this can make any re-scrape PASS while measuring a payload
  the user never submitted — the exact failure the scoring rule exists to
  catch. Treat as Tier-1 within wave-56b.

## Order of implementation (batch-terminal day)
1. F7 + F6b (same region, timeout family) → 2. F3b + F1b (same function)
→ 3. F2b (graph bounce) → 4. edit-or-die (graph, touches F2's state) →
5. F5b (src/, junk bar) → 6. F4b + F9 + F10 (all run_execution.py — scope
gate, PRICE-GATE/finalize lineage, seed provenance; same file, do
together) → 7. F8 remainder (resume_scrape_task + redispatch locks,
vestigial _RedisSetnxStore method deletion).
Then: full gate vs banked baseline (4 known-reds + 11 wave-52 collection
errors), lint `ruff check webapp/ src/`, commit, EB sync
(`git push origin sync-w54:main`, verify ls-remote == HEAD), re-fire batch,
guardian final checkpoint on THIS batch first.

## IMPLEMENTATION STATUS (2026-10-10, all 7 steps code-complete)
1-4 DONE (graph.py, subagents.py). 5 DONE (discovery_quality).
6 DONE — **F9 FILE-MAP DEVIATION**: the PRICE-GATE lives in
`webapp/scraper/tasks.py:_gate_output_rows`, NOT run_execution.py (plan
said "locate during implementation"; located there). Zero/null-price
disclosure arm added (drop semantics unchanged — GAP-7 ownership kept;
589's exact pc-vs-file write lane NOT re-derived statically, disclosed).
F4b `_output_row_count` + unverifiable-scope arm; F10 `_heal_url_list_seed`
(both lanes) + `_stamp_seed_provenance` (stamped pre-gates, both lanes).
7 DONE (resume lock + extraction `_resume_scrape_locked`, watchdog
liveness skip, vestigial `_RedisSetnxStore` methods deleted).
RED accounting: F5b 3 RED-for-right-reason + 1 pre-green pin
(directory-token: the conjunct pre-existed in wave-56) — 2 listings were
CORRECTED post-hoc to the deep books shape (they had tested the depth
rule, not the exemption); corpus check: 0 false vetoes on all 47 promoted
url_list seed sets (14,593/20,881 URLs flip out of exemption but none sit
shallower than their seed-0 anchor). F4b 1 RED + 3 pins; F9 2 RED + 3
pins; F10 4 RED + 2 pins; F8r 3 RED + 1 vacuous-until-fix pin.
2 wave-55 finalize-gate pins UPDATED (they pinned the audited silence:
"no PRICE-GATE note at all" for null prices — now assert the disclosure
with drop semantics unchanged). Lint clean.

## Batch scoring rule (unchanged, guardian-enforced)
PASS = status completed AND pc>0 AND final output_file rows are real items
(row_url_is_item_shaped per row; rescue repoints output_file — always read
the final pointer). Report pass rate FIRST.

## Gate reconciliation (2026-10-10)
The banked baseline (4674 passed / 11 collection errors) was produced over
`tests/ webapp/tests/` — verified by collect count on the PRE-SESSION tree
(stash round-trip at f477700): `tests/` alone collects 4286;
`tests/ webapp/tests/` collects 4705 (≈ 4674+4+11+2+1). Yesterday's
4351-passed gate was `tests/`-only — scope mismatch, not a regression.
CORRECTION (final gate): the 11 wave-52 collection errors are NOT gone —
they live in webapp/test_wave52_writer_retry_stretch.py (tests/-only scope
never collects them; my 'gone at HEAD' claim was a scope artifact). Final
reconciled gate: 5 failed / 4758 passed / 2 skipped / 1 xfailed / 11 errors
in 817s = 4 banked known-reds + 1 environmental + 11 banked errors, +84
passes = the new wave-56b tests. Baseline-identical apart from the
proven-environmental failure. `test_wave55_template_path_anchor::test_every_fm_seed_stamp…`
REPRODUCED on the pre-session stashed tree (1 failed / 45 passed) —
confirmed environmental (shared-data/skills/django-oscar-detection/ written
by batch jobs 2026-10-10 06:24: SKILL.md only, no .seed.yaml, DOUBLED
frontmatter — skill-learner writes skills the FM-seed contract cannot
classify; artifact untouched, banked as candidate finding). Final gate =
`tests/ webapp/tests/`, expected 2 failed (wave15/16 banked DeadTester
arm + template_path_anchor environmental).

## CRITIQUE FIXES (2026-10-10, post 4-agent fleet)
Diff critic found 3 BLOCKERS + 5 SHOULD-FIX; verified and fixed same-day:
1. F8 BLOCKER — self.retry escaped the finally (only release path);
   TTL 300s > countdown 60s ⇒ the t+60 same-site retry found the lock
   held and stranded the claimed-RUNNING row; honest requeue-exhausted
   finalize unreachable. FIX: release before raising (tasks.py).
2. F1b BLOCKER — read state["feedback_for_writer"] but the recycle writes
   the TEST_REPORT key ⇒ dead code. FIX: _summarize_test_report reads
   test_report first, top-level fallback.
3. edit-or-die BLOCKER — warn arm wrote the top-level key the writer
   never reads; thresholds per-window while the evidence (581: 125 reads
   / 3 invocations) was cross-window. FIX: consumer merge (top-level
   fallback at :3322) + writer_dup_read_count accumulation in state with
   abort/warn sized on the cross count (graph.py).
4. F3b — hedge read probe_result.connectivity.outcome (no such key; the
   builder writes method_that_worked) ⇒ hedge never fired, stale
   fingerprint label still earned assertive TLS prescription. FIX: hedge
   on label/evidence mismatch (method_that_worked not fingerprint-*).
5. F5b — any-segment terminal check contradicted the token set's own
   contract; digit-arm deletion flipped real PDPs (/catalog/item/12345)
   to suspicious. CORRECTED: stripped-last-segment check WITH index-file
   promotion (/new-in/index.html judges "new-in" — my first correction
   missed that the index stem is "index") + digit arm RESTORED with a
   pagination carve-out (page-N stems stay depth-judged). Corpus: 70/70
   promoted seed sets, 0 fires.
6. F10 — heal bypassed the A4 seed filter (raw paste over the workspace's
   filtered file; widens C2 scope-gate seed set). FIX: filter_seed_urls
   applied inside _heal_url_list_seed, job_url threaded through
   _stage_extra_files/_run_in_process (was a NameError hazard — caught).
7. F9 — unpriced counter double-counted dropped rows; denominators moved
   to len(kept).
8. Watchdog — is_held skip re-ordered AFTER the pre-graph probe (a dead
   task cannot renew; held key under provably-absent task = ≤300s stale).
DEFERRED with disclosure: resume-lane lock heartbeat (TTL self-expires
≤300s, watchdog reclaim ≤1 tick, resume_attempts cap 3 → FAILED);
F7 coverage on the two post-writer fixer invocations; F2b dead-proxy
tier bounce (bounded one cycle); test-infra nits (real-Redis lock tests).
Certifying run (19 files, touched scope): 248 passed / 1 failed = the
BANKED wave16 known-red. Lint clean.
