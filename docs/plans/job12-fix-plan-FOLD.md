# Job 12 fix plan — FOLD (candidate plan v1, pre-critique)

> Status: CANDIDATE. Synthesizes the six planner outputs (`job12-fix-plan-{1..6}-*.md`) into one plan.
> Evidence base: `job12-context-brief.md` (read-only facts — do not re-derive).
> This document is NOT approved for implementation. It goes to critique next (3 lenses:
> efficacy+genericity, regression safety, optimality+buildability), then to the user.

## 0. Where the planners converged (high confidence)

Six independent planners, six lenses. Full agreement on:

1. **P4 (date-bomb): delete the upper bound.** `recompute_date_reliability.py:29` — remove
   `FIXED_AT` + the `__lte` clause (net −7 lines). Provably idempotent-safe: post-fix rows are
   fixed points of the unchanged parser. Add: warn loudly on 0 rows scanned; failing-first test
   that a row created *tomorrow* is included. Un-breaks the 2 baseline test failures. **Ship first.**
2. **The root cause is evidence discarded, not evidence missing.** `traversal.verify_api` already
   extracts `sample_keys` from real responses (`traversal.py:472/495`) — `graph.py:2393-2398`
   projects the descriptor down to `{url, count, items_per_page}` and throws the keys away.
   Found independently by planners 3, 4, and 5.
3. **One weak predicate, four consumers.** "An api_endpoint with a URL is a real data API" is
   evaluated independently at `graph.py:3051` (strategy gate), `validate_coverage.py:181`
   (coverage gate), `subagents.py:2679` (writer hint), and `subagents.py:2809` (writer
   instruction block). Any fix that touches only the strategy gate does NOT fix job 12
   (planner 1: `subagents.py:2809` replaces the two-phase instruction block with an api_section
   that says "do NOT use Playwright").
4. **`_ESCALATION` has no rung after `internal_api`** (`graph.py:2912`; `_ESCALATION[4:]` is
   empty) — cycle 3 re-picked the *same* failed strategy with zero variation. That is the
   10-min/49-tool-call/zero-write cycle. One-line-class independent fix.
5. **P1's ladder was never the bug.** The classified-retry ladder is already correct-shaped
   (full jitter, honors Retry-After). The real defects: (a) `code_tester`/`cleanup`/
   `skill_learner`/`dagster_converter` call `agent.invoke` raw, bypassing
   `_invoke_agent_with_timeout` and its error boxing — so the 429 was job-fatal
   (`graph.py:3716/3934/3978/4069`); (b) `_invoke_agent_with_timeout:1699` flattens exceptions to
   `str(exc)[:200]`, destroying the type — provider outages are laundered into "agent made no
   progress"; (c) Z.AI sends no Retry-After on `code 1302`.
6. **P5 (stale re-injection) is absorbed.** With resume skip-flags set, `browser_traverse` never
   runs (`graph.py:1492-1499`); job 12's poison descriptor came off disk. A predicate that runs
   *wherever the descriptor came from* + P3's salvage handling covers it. No freshness machinery.
7. **Validity ≠ completeness is real and live.** Job 12's `product_analysis.json` was a pass-2
   prefix salvage: parses clean, carries 1 of 6 requested fields. And `validate_coverage` —
   the node that would have flagged it — **never ran** (`check_accessibility:1497` routes to
   `scraper_analyzer` when both skip flags are set).

## 1. Where they disagreed — and the reconciliation

### 1.1 P2 evidence rule (three competing specs)

| Planner | Spec | Failure mode it fears |
|---|---|---|
| 5 | R1 JSON content-type, R2 ≥3 dict records, R3 page-size sensitivity, R5 identity-join vs DOM cards; V1 (>12 keys)/V2 (>2KB HTML) absolute vetoes; verdicts verified/hint/rejected; shadow run before flip | rmwilliams (small catalog, truncated DOM titles → join lands at 1) wrongly rejected |
| 1 | Three-state verified/refuted/unknown reusing production `verify_api`; **fail-open on unknown** | Constraint-1 sites broken by a too-strict gate |
| 2 | Positive-int `count` required; repo-wide scan: only 2 artifacts ever carried `items_per_page` (aya count=26955 passes; sidley count=null taxonomy — rejection *correct*); amnhealthcare has no count/items/data_source → **never enters the branch at all** | Stripping amn's writer hint if the same rule hits `subagents.py:2679` |

**Reconciled — tier by risk:**

- **Tier A (ship immediately, no shadow):** tighten the strategy gate's count clause from
  `count != 0` to `isinstance(count, int) and count > 0`. Empirically validated by planner 2's
  repo scan: the only artifact it newly rejects is sidley's taxonomy payload (correct), job 12's
  useinsider descriptor (correct), and it preserves lw.com's explicit-zero behavior exactly
  (`count:0` still fails the branch — lw.com ships `http_navigation`, not `internal_api`).
  amnhealthcare is untouched (not in the branch). Plus the V-vetoes where evidence exists:
  response content-type HTML/JS-with-HTML-blob, or `sample_keys` present but non-product-shaped
  → `rejected`.
- **Tier B (shadow-only first):** the R5 identity-join (records' identity values vs DOM card
  labels/hrefs/ids — content-type-agnostic because it contains no vocabulary). Log
  `would_reject: true` without enforcing; flip only after shadow verdicts prove no
  rmwilliams-class false rejection. Planner 5's mitigations if/when it flips: join on ids and
  href slugs (truncation-immune), `JOIN_MIN=1` when fewer than 5 cards captured.
- **Mechanism:** one shared predicate function (single definition, imported by all four
  consumer sites) returning a grade: `verified | hint | rejected | legacy`.
  - `legacy` (no evidence fields — artifacts predating capture, rehydrated, verify_api
    unavailable) ⇒ consumers keep **today's behavior** (fail-open; planner 1's safety principle).
  - `rejected` ⇒ planner 5's relocation: endpoint moves out of `api_endpoint` into
    `rejected_endpoints[]` at write time — disarming every consumer at once without touching
    them — AND the predicate at read time so rehydrated/legacy-shaped files can't resurrect one.
  - `hint` (real records, unjoinable) ⇒ writer may be told about it, never authoritative.
- **New captures only:** stop discarding `sample_keys` at `graph.py:2393`; persist
  `sample_keys` + verification grade + `"source": "verify_api"` stamp in the descriptor
  (planner 2's insurance so consumers can distinguish measured evidence from shape-guessing).

### 1.2 P1 shape (numeric vs DEFER vs boxing-first)

**Reconciled — ordered layers, each independently shippable:**

1. **Marker fix (prereq, tiny):** `_invoke_agent_with_timeout:1699` stops flattening the
   exception — preserve class + provider code in the boxed error and the log line. Without
   this, no downstream layer can even tell a 429 from "agent made no progress".
2. **Boxing (de-fatals the proximate cause):** extend the existing `{"_error": ...}` boxing
   precedent (`graph.py:1694-1711`) to the four raw `agent.invoke` sites. A provider 429 in
   `code_tester` becomes a test-report error that flows through `route_after_testing`'s existing
   retry/approval routing instead of killing the celery task. ⚠️ OPEN QUESTION for critique:
   `route_after_testing`'s exhausted path lands in `human_approval` — does the intake
   (`skip_approvals`) path bypass that, and if so where does a boxed failure actually surface?
3. **Classed ladder:** split retry classes in `src/llm.py` — transient keeps base 1.5/max 3;
   rate-limit gets base 4.0/cap 45.0, 5 attempts, Retry-After honored but jittered (today N
   workers wake in lockstep), with a per-phase sleep budget (~180s) so a 50-call codegen phase
   can't back off forever. Mean time-to-exhaustion against job 12's burst: ~7.5s → ~73s.
4. **DEFER (the contested layer):** on in-call exhaustion, celery task sets a deferred state and
   `self.retry(countdown=120/300/900)` (ladder already proven at `tasks.py:133`) instead of
   FAILED; pre-flight gated by a cross-worker 429 ledger in the existing Redis cache (one GET,
   zero LLM cost). Planner 1's rationale: PAUSE/human_approval is exactly what `skip_approvals`
   intake jobs bypass — job 12 produced 0 approval rows. Mechanical prerequisites it verified:
   the typed handler must sit ABOVE `run_scrape_task`'s generic `except` (which sets FAILED
   without re-raising), and the decorator's `max_retries=1` needs the per-call override the
   same-site retry already uses. ⚠️ OPEN QUESTION: a new status must be walked through the
   partner 4-state projection, UI, watchdogs (celery-beat stuck-job), and `route_after_testing`
   semantics — is DEFER worth it given layer 2 already de-fatals? Critics must rule.

### 1.3 P3 posture (DEGRADE vs never-authoritative vs refuse-to-publish)

**Reconciled:** provenance sidecar + read-time consequence, no publisher refusal in v1.

- The repair ladder already computes provenance and drops it (planner 1). Persist it: sidecar
  (`<name>.provenance.json`) recording pass#, lossless-vs-lossy, original byte length, and
  field coverage vs `target_fields` for the four salvage paths
  (`filesystem_tools.py:52/291/353`, `graph.py:281/355`, `setup_workspace.py:104`).
- Read-time consequences (the only behavior changes):
  - **Resume/rehydration** (`setup_workspace`): a *lossy-salvaged* artifact is not authoritative
    → clear the corresponding skip flag (re-run the phase) instead of trusting it. (Planner 2;
    flips `test_artifact_copy_guards.py:250` — flagged, deliberate.)
  - **Authoritative decision points** (strategy gate, coverage gate): WARNING log when the
    artifact they're reading is lossy-salvaged.
- Explicitly NOT in v1: refusing to publish salvaged artifacts (planner 6's NO-SHIP verdict
  stands); job 12's 1-of-6 artifact would still proceed (planner 1's line) — the defense is
  that its *strategy* input can no longer be a poison descriptor and its resume can no longer
  trust a salvage silently.

### 1.4 P5

Absorbed (§0.6) + two cheap add-ons from planner 1: widen the skip fingerprint with
`page_type`/`site_type` (columns already exist, no migration), and the predicate runs on
rehydrated descriptors (same code path — no separate implementation).

## 2. The plan

### Ship order (risk-ascending, each step independently revertable)

| # | Step | Layer | Rough size |
|---|------|-------|-----------|
| 0 | **P4 date-bomb**: delete upper bound + 0-rows warning + tomorrow-row test | command | −7 lines prod |
| 1 | **P1 marker fix**: preserve exception type at `_invoke_agent_with_timeout:1699` | graph | ~10 lines |
| 2 | **P2 terminal rung**: add fallback rung(s) after `internal_api` in `_ESCALATION` (→ `playwright` / `http_requests`) so a failed strategy never re-picks itself | graph | ~5 lines |
| 3 | **P1 boxing**: box the 4 raw `agent.invoke` sites with the `{"_error": ...}` precedent | graph | ~40 lines |
| 4 | **P2 Tier A**: shared predicate (grade function) + count tightening + V-vetoes + wire 4 consumer sites + descriptor evidence retention (`graph.py:2393`) + relocation to `rejected_endpoints[]` | traversal/graph/coverage/subagents | ~80 lines |
| 5 | **P3**: provenance sidecar + resume skip-flag clearing + warnings at decision points | repair ladder/setup_workspace | ~60 lines |
| 6 | **P1 ladder**: classed retry (rate-limit base 4/cap 45/5 attempts, jittered Retry-After, per-phase budget) | src/llm.py | ~25 lines |
| 7 | **P1 DEFER** (critique-gated): deferred state + celery retry ladder + Redis 429 pre-flight ledger | tasks.py | ~50 lines |
| 8 | **P2 Tier B shadow**: R5 identity-join in shadow (log-only); flip after verdicts | traversal | ~70 lines |
| 9 | **P5**: skip-fingerprint widening (page_type/site_type) | tasks/check_tracker | ~10 lines |

TDD throughout (house style): each step lands failing-test-first. Estimated test mass ~280 lines
(planner 2's estimate). Suite target after step 0: **721 passed / 0 failed** (the 2 date-bomb
failures flip) + new tests; `test_artifact_copy_guards.py:250` flips deliberately at step 5.

### What makes job 12's exact sequence impossible or non-fatal — claim to verify

1. Poison descriptor → strategy gate: Tier A predicate rejects useinsider (count null, JS/HTML
   content evidence) → strategy falls to playwright per product_analysis → **no contradictory
   writer inputs** (subagents.py:2809's api_section is predicate-gated too).
2. Even if a poison slips through: terminal rung makes cycle 3 change strategy instead of
   re-picking the failed one → no 49-call zero-write thrash.
3. Even if the writer thrashes: boxed 429 → `route_after_testing` handles it → **job not killed
   by the provider**; classed ladder absorbs an 8s burst; DEFER (if approved) spans the quota
   window.
4. Stale salvage on resume: skip-flag clearing re-runs the phase instead of trusting 1-of-6.

### Constraint walk (all 7 must survive — critics re-walk independently)

1. **No new per-run LLM cost** — every check is deterministic; R5 (Tier B) joins already-fetched
   bytes. ✔ by construction.
2. **Working sites** — empirical basis: 9 of the constraint-1 sites carry `api_endpoint: {}`
   (untouchable); amnhealthcare ships `http_requests` (never in the gate); lw.com explicit-zero
   behavior identical; aya count=26955 passes Tier A; rmwilliams risk confined to Tier B shadow.
   ⚠️ Critics: walk every constraint-1 artifact through the new predicate by hand.
3. **Yesterday's fixes** — word-boundary `url_looks_like_data_api` stays; the predicate
   *supersedes* it as the authority (URL rules demoted to a fast-path hint), never deletes it.
4. **Streaming stays on** — untouched.
5. **No async, no new infra** — Redis ledger reuses the existing cache client; DEFER reuses
   proven `self.retry`.
6. **scraper_analyzer stays deterministic** — the predicate is deterministic; nothing re-LLMs.
7. **TDD + suite baseline** — per-step failing tests; baseline flips to 721/0.

## 3. Open questions the critique MUST answer

1. Does boxing (step 3) actually surface the failure for **intake jobs** (`skip_approvals`)?
   Where does a boxed code_tester failure terminate if `human_approval` is bypassed?
2. Is DEFER (step 7) worth its status-projection blast radius given step 3 already de-fatals?
   Walk STATUS_DEFERRED through partner 4-state projection, UI, beat watchdog, resume.
3. Hand-walk all constraint-1 site artifacts through the Tier A predicate — any false rejection?
4. Is the Tier A count tightening safe for **rescrapes of aya** (its stored artifact is
   legacy-shaped but carries count=26955 — confirm it still passes)?
5. Does the shared predicate get its evidence from the right place on each of the four consumer
   paths (in-memory state vs navigation_analysis.json vs rehydrated artifact)?
6. Is anything in the 10-step plan redundant (boxing AND DEFER AND ledger), and what would you
   cut to make this a 3-step plan?
