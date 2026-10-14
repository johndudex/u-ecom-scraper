# Wave-63: Execution time-budget (graceful bound) + timeout salvage

**Status**: COMPLETE — e2e 3/3 PASS (2026-10-14). N1 `6538f94` + N2+N3 `e39792d` on
`wave-42e-antibot-section`; TDD `tests/test_wave63_exec_budget.py` 44/44 (35 red at start); full gate
5099 passed / 5 failed / 11 errors — baseline-consistent (4 carried known-reds outside footprint +
skills-state drift; the 11 errors = collection artifact, pass standalone; test_wave16 stale pin
repaired — wave-34 drift, pre-existing). Lint clean. Workers restarted before firing.

**e2e (≤2 cap, sequential pairs)**: 663 jomashop flagship **PASS** — COMPLETED pc=92 (662 was FAIL
pc=0), 92 unique urls, bounded with `time_budget_bounded` recorded; PRICE-GATE disclosed 6/92
zero/null-price rows. 664 royaldesign control **PASS** pc=40 (= 658 replica, budget never fired). 665
ultrabeauty replay **PASS** pc=1000 (= 657 replica, 1000 unique titles+urls, budget never fired).

**The honest caveat**: 663's bound was WRITER-borne, not system-borne — the fix loop delivered the RCA
and the writer self-implemented a graceful budget under its OWN env name `SCRAPER_TIME_BUDGET_S=540`
(our injected `SCRAPER_EXEC_TIME_BUDGET=3000` was ignored); the two-deadline reserve math, 25-item
flush, and N2 harvest hooks did not propagate into the draft. Template-borne path evidence remains the
44 behavioral tests (live-unexercised). Opened wave-64 candidate class "writer contract adherence":
(a) run_execution injects BOTH env names (1-line belt), (b) writer prompt rule to consume contract env
verbatim, (c) code_tester static check that drafts read the contract env names. Ledger rows 9-11 +
follow-ups in scratch/guardian_ledger.md. Merge PARKED per user (57-63 queued). Wave-62 name remains
reserved for the ratified transport-episode candidate (unbuilt).

**Build notes (deviations, all within plan)**: budget env carried at the bs lane by per-payload merge
(`{**stealth_env, **_exec_budget_env()}`) so the shared `stealth_env` dict is not mutated for the
category-leg lane; in-process injection lives INSIDE the Popen env composition (single chokepoint for
all in-process callers); `_exec_budget_env` returns `{}` when disabled (ambient env is never a carrier
— in-process Popen starts from celery's env, bs stages a fresh subprocess env).

## Critique history

- **v1 → critique-1 FAIL**: B1 one shared deadline lets discovery eat the whole budget on the nav template
  (no discovery deadline of its own — `DISCOVERY_DEADLINE_SECONDS` exists only in http_nav :247) → the
  flagship can land 0 rows again. B2 salvage was bs-runner-only; the in-process lane (run_execution.py
  :2022-2027 stall, :2058-2061 rc≠0 — the DEFAULT for the HTTP family per its own comment :2114-2116) reads
  no output; and the bs rc≠0 branch (:2428-2431) returns BEFORE the persist block (:2435-2444) that turns
  `output_content` into `output_file`. B3 `_run_category_sources` (:1770-1776) composes its own env + a
  hard-coded 600s wall — the wave-40 T15 lane-miss class again.
- **v2 → critique-2 FAIL (B4)**: reserve `max(600, 0.3×budget)` is not scale-aware — for the B3-scaled
  category-leg budget 540s, reserve 600 > budget ⇒ discovery_deadline already expired ⇒ every category leg
  breaks at its first walk check with 0 URLs and silently no-ops. **v3 fix**: clamped formula (below).
  Critique-2 VERIFIED RESOLVED: B1 (two-deadline math closes; ≥~77 rows at 11.6s/item; http_nav
  min-of-both strictly better; walk-break-with-0-URLs degrades to honest FAIL), B2 (both lanes + persist
  restructure + attempt_start floors + in-process rescue-shape parity), B3 env injection itself.
- Critique-1/2 verified SAFE (kept): w61 rescue interaction (harvested FAILED+rows ⇒ `skip_failed_no_rescue_evidence`
  :999-1006; starved bounded run ⇒ rescue not armed — a bounded run is not a dead listing); C4 (bs stages a
  virgin `/tmp/scrape_{uuid}` per call — tester file structurally unreachable; in-process floor = load-bearing,
  use `attempt_start` :599 not outer `start` :582); stop-reason collision clean (`exec_time_budget` in neither
  `_COVERAGE_FAIL_STOP_REASONS` :137-152 nor `_ACCESS_WALL_STOP_REASONS` :155-161; probe composes its own env
  graph.py:10084+); margin math (3000 graceful vs 3600 wall + 600 finalize; C2 caller cancel at +30 after
  return; 504 belt at +120 stays hard-zero — correct); MUST-NOT integrity clean; bounded-COMPLETED = honest
  pass per standing objective; critique-history accuracy spot-checked by critique-2 (note 4).

## RCA (job 662 jomashop, Guardian-verified, evidence-closed)

Tester certified the draft at `--limit 50` (exit 0 in 579s, 50/50 rows @100% coverage, PASS 0.93).
run_execution then fired the SAME scraper **uncapped** (`--listing-url … --fresh-discovery`, no `--limit`;
`scope=all` never passes one — only `scope=firstn` does, run_execution.py:1491-1502) on a catalogue-scale
site (62 cards/page, thousands of items, `MAX_DISCOVER_PAGES=200` hard-coded). The walk mathematically
exceeds the browser-service wall: runner `timeout: int = 3600` (scraper_runner.py:496/:526) → `SIGKILL`
(:644) → **hard-zero dict** (:734-741). Templates write output only at the end (nav :1254, http_nav :2003);
checkpoint only post-walk (:1040/:1068) — SIGKILL loses everything. Probe clean all run ⇒ NOT walled.
w61 admission correctly declined. Honest FAILED pc=0 from a certified-good draft.

## N1: execution time-budget — graceful scope bound with a protected extraction window (the pass-maker)

**Contract**: execution must be able to FINISH with rows. Discovery gets a **sub-deadline that reserves
extraction time**; extraction runs to the full budget. Sites that finish under the budget are unaffected.

1. **Env, explicit-both-directions** (the `SCRAPER_CHECKPOINT_REUSE` shape :683-701): `SCRAPER_EXEC_TIME_BUDGET`
   (seconds), injected by run_execution at **ALL THREE dispatch sites**: bs `env_overrides` (:2329-2341),
   in-process Popen env (:1944-1951), and `_run_category_sources` legs (:1770-1776) **scaled per leg**:
   `min(EXECUTION_ITEM_TIME_BUDGET, leg_timeout − 60)` ⇒ 540 on today's 600s legs. Value from setting
   `EXECUTION_ITEM_TIME_BUDGET` (default **3000**). Absent ⇒ unlimited (probe lane never sees it — it
   composes its own env, graph.py:10084+).
2. **Two-deadline template consumption** (both templates, reading beside `SCRAPER_LISTING_URL` nav :945 /
   http_nav :1668):
   - **Scale-aware reserve (B4 fix)**: `reserve = min(max(600, int(0.3 × budget)), int(0.5 × budget))`;
     `discovery_deadline = start + (budget − reserve)`. Pinned at BOTH budget values: budget 3000 →
     reserve 900 (discovery ≤ 2100s, extraction ≥ 900s ≈ 77+ rows at 11.6s/item); budget 540 (category
     legs) → reserve 270 (≈270s discovery / ≈270s extraction ≈ 23 rows/leg) — never negative, never
     already-expired. http_nav keeps its own 300s discovery deadline — the walk honors `min(both)`.
   - Deadline checks in **every walk loop**: both `_discover_urls_via_*` page loops (limit check sits at
     nav :389-391 / :306-311 — beside those) AND the `CATEGORY_URLS` dimension loop (nav :1053-1081,
     **before** the browser rotation at :1074). Break ⇒ stop_reason `exec_time_budget`, proceed to
     extraction with URLs in hand.
   - Extraction loop: stop **between items** (http_nav: between thread-pool **submissions**, GIL-safe
     snapshot — no named lock exists today; builder adds one or copies) when past `start + budget`;
     finalize through the NORMAL output path, exit 0. Output `metadata.discovery_coverage` records
     `time_budget_bounded: true` + items/pages.
3. **Honesty + note delivery (BUILT, not referenced)**: run_execution gains a run-scope note mechanism
   (none exists today — grep-verified): the injected budget and, when the bound fires, the bounded counts
   ride `discovery_coverage` → state → **job notes** (delivery pattern = `capped_by_scope`, graph.py
   :6345-6360). scope=all becomes "all **up to the budget**" — pinned, declared behavior change.
   **Declared regression, pinned loudly**: runs that previously used the in-process extension toward 9600s
   (EXECUTION_MAX_TIMEOUT, settings.py:463-464) now stop at 3000s — honest trim, recorded in notes.
   `coverage.found` is NOT repurposed (stays post-filter extracted count, nav :1200 / http :1953) so
   `_execution_shortfall_reason` (graph.py:6194-6237) never arms remediation on a bounded run.
4. Untouched: scope=firstn/filter; `{MAX_PAGES}` placeholder; probe lane; wall-clock settings.

## N2: timeout salvage — partial rows on the board, BOTH lanes + the persist restructure (the rows-saver)

1. **Template periodic flush**: extraction loop rewrites the output JSON every **25 items** to the run's
   single precomputed final output path via atomic **`os.replace`** (tmp name **`.json.tmp`** — must NOT
   match the `output_*.json` glob, `_find_output_file` scraper_runner.py:772-786; single-name strategy
   pinned against `test_template_output_collision.py`'s job-71 timestamp-collision pin: one file per run,
   final write = the same name ⇒ no two-file ambiguity, lexicographic-newest unaffected). Flush builds the
   **FULL output shape** incl. `metadata.discovery_coverage` (and http's `phase2_instant_fail`) so a
   harvested file stays gate-readable (`_read_discovery_coverage` run_execution.py:3438-3459 is
   exception-safe, but coverage-less flush degrades verdicts). Thread-pool lane (http_nav): flush from a
   **GIL-safe snapshot** (list copy of completed results — no named lock exists today; builder either adds
   one or copies, per critique-2 note 3). Flushed rows are **UNFILTERED** (`_error_item` rows included;
   core-field filter runs at nav :1191 / http :1950) — partial-harvest pc is a raw count; the note says so.
2. **Harvest trigger key (critique-2 note 1 — pinned)**: harvest fires when `timed_out` is set **OR**
   (rc≠0 **AND** fresh floored `output_content` is present). Plain-crash partials (rc≠0 traceback with
   flushed rows — a shape that does not exist before this wave) are therefore ALSO recorded: FAILED + rows
   + raw-count note. Declared pinned behavior addition; `test_job83_job88_classes.py` rc≠0
   rescue/`no_fresh_output` semantics sit inside the grep re-pin set and are re-derived, not smuggled.
   **rc==3/DISCOVERY_ZERO exemption (critique-3 note 1 — pinned)**: the trigger's rc≠0 arm applies to the
   GENERIC FAILED return only — the DISCOVERY_ZERO branch (run_execution.py:2396-2427) keeps priority and
   its tags (`no_fresh_output: True` + `zero_discovery_exit: True`, which the w61 shape-A admission, the
   T2.10 tagged recycle, and test_job83_job88_classes.py:257/:321 pin); test pin: rc==3 + flushed file →
   today's zero-discovery shape unchanged.
3. **Runner harvest (bs lane)**: timeout path (:734-741) — after reaping — reads the workdir's newest
   output (mtime-floored to **attempt start**, :599 — retries) instead of unconditional hard-zero; returns
   `"timed_out": True` + rows. Marker survives the boundary (JSONResponse :2906 → `result.data` browser_http.py:299).
4. **In-process lane harvest (B2a)**: stall return (:2022-2027) and rc≠0 return (:2058-2061) get the same
   treatment via `_find_newest_output(..., mtime_floor=attempt_start)` (F8 :3338-3392; `start` :1909) —
   this is the load-bearing floor (tester output lives in the same workspace here).
5. **Persist restructure (B2b)**: run_execution's rc≠0 branch (:2379-2431) is restructured so a harvested
   result flows through the persist block (:2435-2444: `output_content`+`output_name` → local `output_file`)
   and `_read_discovery_coverage` (:2445) BEFORE the FAILED return. Today's no-harvest shape unchanged.
6. **Downstream honesty (C4 belt)**: harvested ⇒ **status FAILED, never COMPLETED**; output_file +
   product_count recorded; notes carry `PARTIAL HARVEST (budget/hang wall; raw row count N)`. Rows only
   from the execution run's own floored file. `_post_run` is not called on the timeout path today (no
   double-count); H3 checkpoint delete stays rc==0-gated. w61 rescue interaction pinned SAFE (see history).
7. **Lane-3 merge pin**: a category leg ending `timed_out`+harvested merges per today's failed-leg behavior
   (merged output adopts primary coverage, :1830-1860) — pinned so B3's fix doesn't change merge semantics.
8. **N3 folded in**: both lanes' timeout returns carry `budget_wall_timeout` in the error suffix —
   budget-wall distinguishable from hang.

## MUST-NOT

No `--limit` ladder at execution (env approach — immune to `_filter_supported_args` :1511-1513); no LLM in
the bound; scope=firstn/filter untouched; no resume/checkpoint-reuse changes (H3 :419-444/:464 +
run_execution.py:3462-3482 untouched); no wall-clock setting edits (EXECUTION_TIMEOUT/STALL/MAX untouched —
the budget is a NEW env consumed by templates; extension mechanics untouched); probe lane untouched; partial
harvest NEVER flips status to COMPLETED; no gate loosening (bounded output still passes F9/shallow/nonitem/
scope gates — `_extraction_quality_gate` :3127 unaffected); tester-artifact crediting forbidden (C4);
templates' substituted constants untouched; no second provider; deterministic only.

## Tests (TDD — files edited BEFORE the certifying suite)

- NEW `tests/test_wave63_exec_budget.py`:
  - template pins (both): env read; **clamped reserve at both budget values (3000→900, 540→270 — never
    expired, never negative)**; deadline checks present in BOTH
    `_discover_urls_via_*` loops AND the CATEGORY_URLS dimension loop (before browser rotation); http_nav
    min-of-both discovery deadlines; extraction stop between items / between pool submissions; 25-item
    atomic single-name `os.replace` flush via `.json.tmp` (glob-safe; collision-pin compatible) with FULL
    shape incl. discovery_coverage; bounded meta flag.
  - run_execution pins: budget env at ALL THREE dispatch sites (bs, in-process, category-legs scaled
    `min(budget, leg_timeout−60)`); probe lane excludes it; note delivery coverage→job-notes
    (capped_by_scope pattern); `found` not repurposed.
  - runner pins: timeout + on-disk output → `timed_out: True` + rows, floored to attempt start; timeout +
    no output → hard-zero unchanged; **trigger key pinned BOTH ways** (`timed_out` OR rc≠0 + fresh
    output_content → FAILED+rows; rc≠0 with NO output → today's shape); **rc==3 + flushed file →
    DISCOVERY_ZERO shape unchanged** (tags intact — critique-3 note 1); filename timestamp now
    start-of-run (critique-3 note 3 — covered in the test_template_output_collision re-pin pass).
  - in-process pins: stall + flushed file → FAILED+rows; rc≠0 + flushed file → persisted output_file +
    coverage read + FAILED; extension regression noted.
  - downstream pins: harvested ⇒ FAILED + output_file + pc + PARTIAL HARVEST note; never COMPLETED;
    `budget_wall_timeout` on both lanes; w61 rescue skip arms (both found>0 and starved found=0 shapes);
    lane-3 merge with a timed_out leg.
- **Re-pin procedure (replaces v1's incomplete list)**: `grep -l "navigation_scraper" tests/ webapp/tests/`
  (~25 files — incl. test_template_output_collision, test_wave32_inconclusive_termination,
  test_wave51_item_link_filter, test_wave57_client_shell_duplicate, test_wave24_discovery_overwrite,
  test_discovery_ladder, test_empty_discovery_disambiguation, test_wave59_embed_file_diet,
  test_wave13/15/16/17/19/50*, test_w8_throttle_tolerance, webapp/tests/test_embedded_json_model) —
  verify-then-edit BEFORE the suite; **order-sensitive pins exist** (`src.index(a) < src.index(b)`,
  test_f6_f7_f17_gates.py:58-61 style) — re-derive, don't smuggle.

## Commit split

1. N1 commit: template two-deadline checks + budget env consumption + run_execution three-site env
   composition + note mechanism + tests.
2. N2+N3 commit: template periodic flush + bs runner harvest + in-process harvest + persist restructure +
   markers + partial-honesty + merge pin + tests + re-pins.
Both land BEFORE the certifying suite fires; workers restarted after landing.

## Acceptance bars

1. Gate: baseline-identical except the grep-enumerated re-pins + new tests green; lint clean (webapp/ + src/ only).
2. e2e (≤2 concurrent, pairs): **pair 1** = jomashop watches.html re-fire (flagship: must COMPLETED with
   rows ≥10 @100% core coverage — the reserve guarantees an extraction window; at ~11.6s/item expect
   tens-to-hundreds — bound recorded in job notes) + royaldesign control (must stay PASS, expect pc=40).
   **pair 2** = ultrabeauty replay (no-regression: PASS @100% coverage; 1000 expected — an honest
   budget-trim lower is acceptable ONLY if recorded in notes; still ≥ bar).
3. **What e2e certifies (binding)**: the flagship exercises N1's graceful bound with protected extraction.
   N2's salvage is a failure-path feature — its evidence is the behavioral replays, NOT the e2e (bonus if
   it fires live). Partial-harvest rows bypass prune/normalize/gates (recorded raw) — the report says so.

## Guardian checks invoked

Plan gate: this doc → re-critique → guardian APPROVE/REVISE/VETO before build. Verdict gate on every e2e
job. Ledger: scratch/guardian_ledger.md (662 FAIL row 7 + follow-up recorded).
