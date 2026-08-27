# Job 12 fix plan — FINAL (post-critique)

> Status: READY FOR USER APPROVAL — no implementation has been done.
> Pipeline: 6 planners → fold (`job12-fix-plan-FOLD.md`) → 3 adversarial critics
> (efficacy/genericity, regression-safety, optimality/buildability) → this document.
> Evidence base: `job12-context-brief.md` + corrections in §0. Planner docs:
> `job12-fix-plan-{1..6}-*.md`.

## 0. Corrections the critique round forced (evidence-verified)

1. **Poison attribution corrected.** Job 12 had TWO poisons: the *rehydrated* ketchcdn consent
   config (`count: null`, `items_per_page: 5` — extracted from a 5-element array inside the CMP
   blob by `_extract_items_count`, which picks the largest dict-array anywhere) AND the *fresh*
   useinsider capture (`count: null`, `items_per_page: 1`). Both fail the new count clause;
   whichever was in state, the fix catches it.
2. **The gate has ONE discriminator, not two.** `verify_api` returns `None` unless it found a
   non-empty dict-array, then sets `items_per_page = len(items) ≥ 1` — every descriptor it ever
   emitted satisfies `items_per_page > 0`. The "Two signals, both required" comment
   (`graph.py:3039-3041`) is false advertising. The count clause is the entire content of the fix.
3. **The planner repo-scan missed the File Master.** `shared-data/scrapers/` (what
   `_restore_from_archive` actually reads) holds descriptors `scrapers/`+`workspace/` don't:
   **zquiet is a constraint-1 site LIVE on `internal_api` against the heatmap poison URL today**
   (`count: null, ipp: 1`; shipped output says `scraping_method: internal_api` while the shipped
   scraper actually uses `/collections/all/products.json` — its own comment: the shop-all
   endpoint "returned only 1 product via API"). Tier A flipping zquiet is a FIX, not a risk.
   The "9 sites carry `api_endpoint: {}`" claim is actually **7**.
4. **`{"_error": ...}` has zero consumers.** Produced at `graph.py:1670/:1699`; no
   `result.get("_error")` exists anywhere. Boxing without building a consumer falls through to
   `_load_test_report` and routes on a **stale prior-cycle report**.
5. **lw.com's `count: 0` case exists in no artifact anywhere** (both stored lw-com descriptors
   have `api_endpoint: {}`). It is a live-traversal-only state — unfalsifiable from the corpus,
   which is why the fixture corpus (S8) matters.
6. **The circuit breaker is a structural no-op in prod**
   (`ZAI_MAIN == ZAI_SMALL == ZAI_FALLBACK == glm-5-turbo`; `llm_breaker.py:139` returns primary
   when primary==fallback). No code step fixes this — it's a Railway env change (§6).
7. **P4 is NOT provably idempotent** — `parse_posted_date` resolves "3 days ago"/"today" against
   `datetime.now()`, so written values drift with the run date, and the old upper bound was
   accidentally bounding the drift. The deletion needs the `scraped_at` anchor (S0) to be correct.
8. Path corrections: retry ladder lives in `webapp/agents/llm.py` (not `src/llm.py`);
   rehydration lives in `webapp/agents/nodes/setup_workspace.py`.

## 1. The plan — 8 steps + 1 deferred + 3 zero-code actions

### S0 — Date-bomb (ship first; the 2 red tests)
`webapp/scraper/management/commands/recompute_date_reliability.py`
- Delete `FIXED_AT` + the `__lte` clause (net −7 lines).
- **Anchor relative-phrase parsing to `listing.scraped_at` instead of `now()`** inside the
  recompute path (~1 line) — makes the command genuinely idempotent and *more correct*: it
  recovers the date the row actually had, not one drifted by backlog age.
- Warning target changes: post-deletion the date-bomb signature becomes `scanned: huge,
  would fix: 0` — warn on that, not on 0 rows scanned.
- Failing-first tests: a row created "tomorrow" is included; idempotency across two runs
  including a relative-phrase row.
- Admin buttons safe (same `call_command` at `admin.py:293`).

### S1 — Provider-failure honesty + classed ladder (the step that would have saved job 12)
`webapp/agents/llm.py` (`_retry_settings:48`, `_handle_retry:201`) + `graph.py:1670/1699`
- **Ladder**: dedicated rate-limit class — attempts 3→6, backoff base 1.5→2.0, cap 30.0 kept,
  floor 1.0 added. Worst case 6×30=180s (17% of the 900s wall); job 12's 8s burst is absorbed
  with margin. Transient class unchanged. Retry-After stays honored, now jittered (today N
  workers wake in lockstep).
- **SessionLog emission** (2 lines in `_handle_retry`'s exhaustion branch): the 429 never
  entered SessionLog on job 12 — it existed only in `error_message`. This closes the
  observability gap WITHOUT the DEFER machinery (cut, §3).
- **Marker fix**: `_invoke_agent_with_timeout` (sync `:1699` + async `:1670`) preserves the
  exception type/provider code instead of `str(exc)[:200]` — stops provider outages being
  laundered into "agent made no progress" → auto-extend → more calls on a dead provider.

### S2 — The gate: positive-count evidence for `internal_api`
`webapp/agents/graph.py:3062` (+ fix the false comment at `:3039-3041`)
- `count != 0` → `isinstance(count, int) and count > 0`. The gate keeps its existing
  preconditions (`data_source=="api"`, api-url, `items_per_page > 0`).
- **Grade precedence PINNED (the aya question, settled):** `count > 0` ⇒ eligible; null/0/absent
  ⇒ not eligible. Absence of `sample_keys` does NOT demote — aya's stored artifact carries
  `count: 26955, items_per_page: 5` and passes the count clause directly. There is no
  `verified/hint/legacy` vocabulary at the gate; just the predicate. Consumers are NOT rewired
  (see §3 cut list — relocation of *positively-vetoed* poisons ships later, with S7's
  content-type capture, and only for veto-grade rejections, never for count-absence).
- **Falsification test-lock** (all on real recorded payloads → S8 fixtures): aya 26955 PASSES;
  lw-class explicit 0 rejected identically to today; zquiet heatmap FLIPS off `internal_api`
  (a fix — its real endpoint is the Shopify feed); sidley taxonomy rejected (also fixes a live
  bug the plan didn't know it was fixing); ketchcdn + useinsider rejected;
  **amnhealthcare's stored artifact untouched** (no `data_source`, no count/ipp → gate never
  enters the branch; ships `http_requests`).
- **Pre-ship live verification for the one unproven risk (fresh amn capture):** amn's TRUE
  fresh-capture count is UNKNOWN — its stored descriptor came from
  `navigate_synthesize._best_api_endpoint` which never computes count, so "count:null" was
  never measured. Before shipping S2, run the existing network-gated live traversal
  (`experimental/nav_traversal/test_traversal.py` harness) against amnhealthcare once and
  record the fresh descriptor. If its API reports a total (likely for a paginated job-search
  API), fresh amn passes. If it is genuinely null, S2 ships with consumer C
  (`subagents.py:2679` writer hint) explicitly excluded from the predicate (it already is —
  url-truthiness) and the residual strategy-vs-hint contradiction documented, OR the amn URL
  is added to a small allowlist. Evidence first, then decide.

### S3 — Zero-capture precedence rule (would have fixed job 12 by itself)
`webapp/agents/graph.py` `_derive_strategy`
- When `product_analysis` carries an explicit mechanism verdict
  (`mechanism_reassessment.recommended`, as priceline's did: `"playwright"`), it **beats** a
  `count:null` descriptor at the gate. Site-agnostic, no new capture, no LLM — the evidence
  already existed and was silently overridden.
- Constraint-1 conflict check test-locked: no constraint-1 artifact carries a
  `mechanism_reassessment.recommended` that conflicts with a legitimate `count>0` descriptor
  (critic A verified across the corpus).

### S4 — Escalation honesty (fixes cycle 3 without manufacturing doomed strategies)
`webapp/agents/graph.py:2912-2927` — ships AFTER S2 (closes the window critic B identified)
- **Do NOT append downward rungs** (a failed `internal_api` → `http_requests` retry is the
  "doomed strategy" for CSR pages per the gate's own comment; planner 6 ruled it NO-SHIP and
  critic B upheld that).
- Instead: (a) the `internal_api` rung itself is gated on the S2 predicate — a failed
  playwright can no longer *escalate into* `internal_api` without evidence (`_ESCALATION[3:]`
  hole); (b) when no untried strategy remains, fall through to the existing exhausted-path
  routing (`human_approval` / final-failure) instead of silently re-picking the same strategy.
  Cycle 3's "same strategy, zero variation" becomes impossible without adding bad options.

### S5 — Salvage honesty at rehydration (planner 2's variant — the sidecar is cut)
`webapp/agents/nodes/setup_workspace.py:104-121`
- The lossy-repair note is already in hand where `_restore_from_archive` guards the bytes
  (`guarded, note = guard_json_bytes(...)`; lossy passes all say "salvag*", lossless don't —
  vocabulary verified). A **lossy-salvaged** FM copy is refused: return cleared skip flags so
  the phase re-runs instead of trusting a 1-of-6 artifact. No sidecar file, no FM publish-list
  change, no schema change. Flips exactly one test
  (`test_artifact_copy_guards.py:250` — flip verified mechanically against the real ladder:
  the fixture body lands in pass 2b/3, so the `"salvage" in note` discriminator fires).
- Sequenced AFTER S2's fixtures exist (critic B's compounding: clearing skip flags forces
  fresh traverses, which raises exposure of the fresh-capture predicate — so the predicate
  must be fixture-proven first).
- `validate_coverage.py:181` bypass tightened to the **loose** rule only
  (`data_source=="api" and items_per_page > 0` — NOT the strict count predicate): amn keeps
  its exemption (its descriptor has no `data_source`), priceline-class fresh runs get the
  coverage gate armed (desirable — that gate never ran on job 12's shape).

### S6 — Capture prereqs + one-liners (enablers; each independently shippable)
- F17: `_SELECT_OPTION_KEYS` += `"count"` (one line — why sidley's `["text","value","count"]`
  taxonomy passed a guard that exists to reject it).
- F7: `_httpx_fetch` (`traversal.py:837-850`) retains `content-type`; `verify_api` persists it
  in the descriptor (today headers are discarded — no content-evidence veto is even expressible).
- `sample_keys` retention at `graph.py:2393-2398` (stop projecting it away).
- URL rules stay **capture-time vetoes** (critic B: demoting them to "hints" would re-admit
  URL-rejected endpoints on count>0 — the exact regression shape of the original ketch bug).

### S7 — Fixture corpus + golden replay (replaces the calendar shadow — critics C+B)
`tests/fixtures/endpoints/*.json` + one replay test
- Recorded payloads: ketchcdn 38-key config, useinsider JS + 6,447-char HTML `content`, aya
  26,955, coveo `totalCount:0`, amn null-count, zquiet heatmap, sidley `["text","value","count"]`,
  and the legitimate `/collections/all/products.json` twin (zquiet-class lookalike that must
  PASS). One CI test runs the S2 predicate over all of them and asserts the verdict table.
- Converts "shadow run" from a calendar into a test file: evidence-based, replayable forever,
  no prod-traffic dependency (the calendar shadow was decorative — verdicts only accrue for
  sites that happen to be re-run, and the risk sites are the least likely to be).
- Tier B (R5 identity-join) stays ON THE ROADMAP behind this corpus: it is the only mechanism
  that catches count-reporting poisons (review widgets with `totalResults: N` — planner 5's
  C8), but it needs DOM-capture plumbing and is a project, not a step. Deferred, not dropped.

### S8 (deferred, explicitly not core) — Boxing, done right
- All three critics agree boxing is not the headline fix and as specified is *harmful*
  (stale-report routing, no consumer). Its real, verified value: the celery task stops dying
  mid-graph (cleanup's artifact promotion + finalize ladder run; `route_after_testing:620-623`
  already handles `skip_approvals` → orderly honest FAILED with the real-items rescue intact).
- Ship only as a later step WITH: an `_error` consumer in `_invoke_code_tester` that suppresses
  the stale report and synthesizes a FAIL report. Cut from the core plan.

## 2. What was CUT, and why (per critic consensus)

| Cut | Why |
|---|---|
| **DEFER status + Redis 429 ledger** | NO-SHIP. New non-terminal status through ~25 sites in 9 files: dedup guard misses it (concurrent same-URL jobs), blocking-detection misses it, `cleanup_stuck_jobs` filters RUNNING only → **new stuck-forever class**, `schedule_next_site` over-dispatches, partner 4-state projection silently renders it as `failed`+`pipeline_failed` (contract violation), emitter gates on `created_via=="api"` so intake jobs — the exact class it exists to save — emit nothing, intake spinner never terminates, admin dashboard shows a slot-holding job as idle. S1's ladder + S8's eventual boxing already de-fatal. |
| **Provenance sidecar** | Cannot cross the run boundary (`_publish_analysis_artifacts` is a hardcoded 5-file list), `setup_workspace` returns `{}` unconditionally, `PRESERVE_FILES` is empty so `_clean_stale_artifacts` deletes it next run, and "no sidecar" semantics fork into never-fires or always-clears. Planner 2's note-based refusal (S5) needs none of that. |
| **Downward terminal rungs** | Escalates failed `internal_api` into `http_requests` against CSR pages; planner 6 NO-SHIP upheld. S4 achieves the cycle-3 fix without it. |
| **Calendar shadow run** | Decorative (see S7). |
| **V1/V2 magic-number vetoes as enforcement** | Reverse-engineered from exactly 2 payloads, no false-positive budget; R1 content-type (mechanism-derived) is the enforcement veto once S7 capture lands. |
| **Boxing as step 3** | See S8. |
| **"Predicate supersedes URL rules"** | URL rules stay capture-time vetoes (S6). |

## 3. Zero-code actions for the user (no PR needed)

1. **Railway env**: set `ZAI_FALLBACK_MODEL` to a genuinely different model than
   `ZAI_SMALL_MODEL` — today the breaker is a structural no-op (`llm_breaker.py:139` returns
   primary when primary==fallback).
2. **F20 data decision**: the shipped priceline output
   (`shared-data/scrapers/priceline-com-au/output_2026-08-26_223642.json`) carries
   `src_url: https://pricelineau.api.useinsider.com/api/info/824.24` — the poison reached
   customer-visible data. Clean/regenerate or accept; user's call.
3. Secrets rotation remains open from the earlier backlog (Railway token, DJANGO_SUPERUSER_PASSWORD).

## 4. Scorecard against the four requested criteria

1. **Generic nature** — Every enforcement rule is mechanism-derived, not incident-tuned:
   positive-count evidence (the gate's only real discriminator — G1), an explicit
   already-produced verdict outranking a null-count descriptor (S3), and capture-time URL
   vetoes kept intact. The honest scope statement (critic A's G3): S2 is a *no-count* filter;
   count-reporting poisons (review/personalization widgets with `totalResults: N`) need the
   deferred R5 work — the fixture corpus is what makes that future flip evidence-based.
   Threshold-free: no magic numbers were added (V1/V2 rejected as enforcement).
2. **Does it really fix it** — Job 12's chain walked step-by-step by a dedicated critic against
   the real artifacts: both poisons fail the count clause; the S3 precedence rule rejects the
   gate's override independently; S4 removes the same-strategy re-pick; S1 absorbs the 8s 429
   burst (~10.5s → ~52-73s to exhaustion) and puts it in SessionLog. Remaining honest gaps,
   stated: cycle-3's 49-call zero-write was *code_writer* thrash — S4 prevents cycle 3 from
   existing, not code_writer's ballooning pathology (documented separately, memory:
   code-writer-context-ballooning-rootcause); provider death beyond ~180s still fails the job
   (orderly, honest, with artifacts rescued once S8 lands).
3. **Not screwing up what works** — Full constraint-1 walk on real artifacts across BOTH
   directories (including the previously-unscanned File Master): aya passes, lw-class
   explicit-zero behaves identically, amn stored artifact untouched (ships `http_requests`;
   gate never fired for it), zquiet flips (a fix), 7 sites have empty descriptors (untouched).
   The one unproven case — fresh amn capture — is settled by a mandatory pre-ship live
   traversal (S2), not assumed. Yesterday's fixes: word-boundary URL rule and repair ladder
   untouched and still veto-authoritative; the one deliberate test flip is verified
   mechanically. DEFER's 25-site blast radius avoided entirely. P4 made genuinely idempotent
   rather than just wider.
4. **Optimal** — 6 planners produced a 10-step fold; the critique round cut it to **7 core
   steps + 1 deferred** (~200 prod lines + ~600-900 test lines — the fold's "280 test lines"
   was 2-3× light). Every cut is a critic-verified harm or no-op (DEFER, sidecar, downward
   rungs, calendar shadow, boxing-as-specced). The two cheapest highest-value fixes (S3's
   precedence rule; S0's `scraped_at` anchor) surfaced only in critique and replaced more
   expensive machinery.

## 5. Build order & verification

Order: **S0 → S1 → S2 (+pre-ship amn live check) → S3 → S4 → S6 → S5 → S7**, S8 deferred.
Each step lands failing-test-first (TDD house style). Suite target: 719+2 flip to green at S0
→ 721/0/2 skipped before new tests; new tests on top. Test-mass estimate: 600-900 lines
(critic B's audit; the fold's 280 was not plausible). Every step is independently revertable;
none requires new infrastructure, async, LLM cost, or env changes.

## 6. Open items explicitly NOT resolved here

- The exact skip-flag history of job 12 (which of browser_traverse/validate_coverage ran) is
  not fully reconstructible from the preserved logs; the plan is airtight against either
  history (both poisons fail the gate; the coverage tightening arms both shapes).
- code_writer context ballooning (127 trims / 350k chars in job 12) is a separate documented
  root cause; this plan removes the amplification (cycle 3), not the pathology.
- abercrombie artifacts were not found in either repo (constraint-1 list entry unverifiable).
