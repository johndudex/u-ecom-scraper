# Wave-26 Fix Plan — RCA of prod re-drive failures (2026-09-08)

**Evidence base:** 6 deep-agent RCAs on campaign jobs that failed under the wave-24+25 deploy
(cd128c9, live 2026-09-07 ~23:00Z): 418 next.co.uk, 419 nike.in, 420 ralphlauren.co.uk
(cascade-exhausted class), 410 karenmillen (tested-PASS-no-exec), 406 forevernew (quality gate),
393 au.tommy (writer wall-clock ×2, ran pre-deploy on wave-23). Evidence dumps: /tmp/rca_evidence/.

**Status: COMPLETE — all 6 RCAs integrated 2026-09-08.**

## The headline

Zero of six failures was a site wall. All six are harness defects. Two jobs (410, 419) had a
**working scraper that PASSED testing with real products** and the pipeline destroyed it by
routing/evidence bugs. The most convergent single defect is the **leaked writer thread**
(4 of 6 jobs: 418, 420, 393, 410) whose post-invocation draft writes caused a SyntaxError
(418, 420), a self-inflicted browser-service 429 (393), and ground-truth evidence poisoning
(410).

## Defect matrix (job × defect; P = primary cause, C = contributing)

| # | Defect | 418 | 419 | 420 | 406 | 393 | 410 |
|---|--------|-----|-----|-----|-----|-----|-----|
| A | read_file offset (chars) vs search_content hits (lines) unit mismatch; W25-b nudge instructs the broken recipe | P | C | C | — | C | — |
| B | Step-budget death in a fix cycle wastes a test cycle: `_noop_should_escalate` reads stale pre-node `test_retry_count`; no-op latch doesn't escalate | C | C | C | — | C | — |
| C | `_enforce_discovery_import` paren-blind `rfind` splices import inside a parenthesized block → SyntaxError → extra writer invocation | C | — | C | — | — | — |
| D | Leaked writer threads mutate draft after timeout (418 SyntaxError), after cleanup copies to scrapers/ (393), hold browser capacity → self-429 (393), and poison freshness-floor evidence (410) | C | — | C | — | P(cost) | C |
| E | Seed-file lifecycle: harness rewrites `input_urls.json` from nav junk on EVERY writer entry, count-only guard destroys a good 1-URL seed (419); writer shrinks seed to its 1 hand-tuned URL, defeating ≥3-item threshold (406) | — | P | — | S | — | — |
| F | Tester prompt contract bugs: prescribes `--query "{search_criteria}"` (a listing URL) for `list_page` (420); drives Phase-2 validation off the writer's seed file instead of discovered URLs (406) | — | — | P | S | — | — |
| G | LLM-authored report fields are load-bearing for deterministic guards: `discovery_coverage.stop_reason` placement decides throttle-vs-strategy (393); `MIN_CONFIDENCE_PASS=0.85` rejects an honest 0.82 PASS and the router ignores the tester's own `ready_for_execution: true` (419, 410) | — | C | — | — | P | P |
| H | Cascade routing dishonesty: run-yield heuristic says "anti-bot site" when `remediation.target="scraper"` (420, 419); infra throttle escalated to strategy switch (393); the W19-9 exhausted arm returns cleanup **silently** — no `_log_cascade` row (410) | — | C | C | — | P | C |
| I | `no-sku-shape` PDP classifier rejects non-numeric slugs → 308/308 anchors rejected (au.tommy) | — | — | — | — | C | — |
| J | Strategy switch = forced full regeneration of an 86KB template at ~91 chars/s decode ≈ exactly one 1800s window, no slack; edit-over-write disarmed by template swap | — | — | — | — | C | — |
| K | Finalizer cosmetics: F9 says "lacked core fields" when 0 rows emitted (406); never-run Step rows backfilled to "done" incl. a fake `execution: done` (410); `_diagnose_no_execution` claims "interrupt/resume gap" when the router chose cleanup (410) | — | — | — | C | — | C |
| L | `_freshness_floor` keys ground-truth evidence to the LIVE draft's mtime: any post-test write to the draft (e.g. leaked thread) retroactively excludes the current attempt's own outputs → `_scraper_has_real_items` returns False with a real product on disk → tested-PASS job sent to cleanup (410) | — | — | — | — | — | P |

## Wave-26 items (all generic — zero site-specific logic)

### W26-1 (F+A core): tester prompt obeys the CLI contract per input_mode
- `subagents.py` tester prompt builder: emit `--query` line **only** for `search_term`;
  for `navigation|list_page` prescribe `--listing-url <search_criteria> --fresh-discovery`
  (what `run_execution` actually issues). Defensive: if `search_criteria` is URL-shaped and
  input_mode != search_term, never place it in the `--query` slot.
- Nav-mode Phase-2 validation must exercise discovery→extraction (not seed-file-only).
- *Would have saved 420 outright; hardens every nav-mode job.*

### W26-2 (E): seed-file lifecycle
- `_invoke_code_writer` writes `input_urls.json` only on first invocation and only if absent;
  never during refine-fix/code-fix cycles (seed is part of the tested contract).
- `src/seed_urls.seed_report` gains item-plausibility (drop same-host nav junk: `/help*`,
  bare `/ ?ptype=*` homepages, pathless links) — content-type-aware.
- Tester-time assert: nav-mode seed holds >1 URL.
- *Would have saved 419; removes 406's writer-shrink exploit.*

### W26-3 (G): deterministic guards stop trusting LLM field placement
- Harness stamps `discovery_coverage.stop_reason` from the scraper's own run output
  (stdout/JSON), overriding the tester's prose-only mention.
- **Honor the tester's `ready_for_execution: true`:** a final verdict of PASS + ready=true +
  0 high-severity issues + ≥1 real item routes to `field_confirmation` → execution, even at
  the exhausted boundary. (NOT a blind lowering of `MIN_CONFIDENCE_PASS` — that degrades the
  bar globally; the tester's readiness signal is the calibrated evidence.)
- *Would have saved 410 outright (PASS + 1 real product thrown away) and 419 (twice over),
  plus 393's cycle-2 escalation.*

### W26-10 (L): freshness floor must never exclude the current attempt
- `_freshness_floor` anchors to the previous attempt's bound (`last_tested_draft_fp` +
  `last_tested_at`), never the live draft's mtime. Anything the current test run produced is
  current-attempt evidence by definition.
- Unit test: draft mtime advances past the sample output → `_scraper_has_real_items` still
  sees the current attempt's items.
- *410's ground-truth rescue would then have returned True → execution.*

### W26-4 (A): read-unit consistency for the writer
- `read_file` gains line-based targeting (`line=` + `num_lines=`) alongside char `offset`;
  `search_content` hits remain line-numbered and the nudge text names the same unit.
- Out-of-range offset error steers: "offset is a CHARACTER position; nearest line boundary is
  L; your target (from search_content) is line N".
- *Unblocks the fix-cycle writer on every large draft (418/419/420/393).*

### W26-5 (B): step-death fast path
- `_noop_should_escalate` receives the bumped `test_retry_count` (from the node's own
  `update`), not stale pre-node state.
- Step-budget death + unchanged draft fingerprint in a fix cycle → skip the guaranteed-identical
  re-test; escalate immediately.
- *Saves one full tester cycle (~5-15 min) per step-death (418/419/420).*

### W26-6 (C): paren-aware discovery-import splice
- `_enforce_discovery_import` inserts after the last top-level import (ast-aware), verifies
  with `compile()`, rolls back on failure.
- *Removes a self-inflicted SyntaxError + extra writer invocation (418/420).*

### W26-7 (D): leaked-thread containment
- Cooperative-stop check between tool calls for wall-clock-abandoned threads; rows tagged
  post-mortem so they can't mutate workspace after cleanup's copy; browser slot released on
  abandon.
- *393's self-429 and post-cleanup edits; 418's SyntaxError.*

### W26-8 (H): cascade honesty
- Cascade `reason` honors `remediation.target`; `navigate_throttled` (harness-stamped) routes
  to the retest arm, never a strategy switch.
- The W19-9 silent cleanup arms (skip_approvals exhausted, CLI-contract-exhausted) MUST emit
  `_log_cascade` rows naming the arm and the rescue's numeric result (items seen vs min_count)
  — every routing decision leaves a forensic trace (wave-22 B5 rule).
- *393/419/420 routing honesty; prevents strategy ladders on infra walls; 410-class jobs
  become explainable from logs alone.*

### W26-11 (K, cosmetic): finalizer truthfulness
- Never-run Step rows are marked "skipped", not backfilled to "done" (410's fake
  `execution: done`).
- F9 gate message: "0 items emitted / N extraction failures" instead of "lacked core fields"
  when the products array is empty.
- `_diagnose_no_execution` distinguishes "router sent a tested-PASS to cleanup" from an
  interrupt/resume gap.

### W26-9 (I): PDP slug-shape acceptance
- `no-sku-shape` classifier accepts slug-style PDP candidates (`...-ww0ww51097c1g`,
  alpha-suffixed IDs), not just numeric IDs.
- *au.tommy-class Magento sites; shared module, generic.*

### Explicitly NOT in wave-26
- Site-specific scraper fixes (none needed — every site proved reachable/extractable live).
- W25-e pre-seeded drafts (falsified; deferred as task #62).
- F9 gate message rewording (K) unless trivial — it did its job in 406.

## Verification plan
1. TDD each item (failing test first); full suite + `ruff check webapp/ src/`.
2. Local e2e re-drive on RCA'd sites (≥4): nike.in, ralphlauren.co.uk, forevernew,
   next.co.uk, karenmillen, au.tommy — pc>0 with numeric prices required for credit.
3. Everything-up check: local stack healthy, campaign driver alive, prod in-flight healthy.

## Caveats carried from RCAs
- 393 ran pre-deploy (wave-23): its D3 (draft-nudge on fix cycles) is already fixed by W24-5
  in prod; wave-26 items must be re-verified against the CURRENT tree (dabf7a3) — some defects
  may already be partially addressed by W24-2/W24-7.
- 419's ground-truth-override miss (why a 1-real-item list_page run didn't end at
  field_confirmation) is unexplained from SessionLog — needs a celery-log check during impl.
- 406's exact zeroing line inside the shipped draft is unrecoverable (write payload truncated);
  the fix targets the validation gap, not the draft.

## Critic verdicts (verified against tree, 2026-09-08)

| Item | Verdict | Key correction |
|---|---|---|
| W26-10 floor | SHIP (first) | When draft fp matches last_tested_fp, floor on last_tested_at and IGNORE live mtime (mtime leg still needed cross-draft when fp differs) |
| W26-2 seed | RESCOPE | Only-if-absent freeze; generalize plausibility (query-only home + utility prefixes — ptype literal is site-shaped); DROP >1-URL assert (contradicts 419's own good 1-URL seed) |
| W26-8 cascade | RESCOPE | _log_cascade on 3 silent arms (:1824/:1851/:1994) + anti-bot reason honors remediation.target (:1901-07) + fold _diagnose PASS-wording fix in |
| W26-6 splice | SHIP | TWO sites: _enforce_discovery_import AND _patch_scraper_output_filter (same blind-marker class) |
| W26-4 read units | SHIP | As planned; nudge text at subagents.py:1381-1383 must match |
| W26-5 step-death | SHIP | Pass bumped update["test_retry_count"] (graph.py:5466 vs :4904); gate skip on unchanged fingerprint (:5445-51) |
| W26-1 prompt | SHIP rescoped | Wave-24 already added mode-correct _tester_phase1_instruction (:4586-4631) — :4666 now CONTRADICTS it; delete/gate :4666 + Phase-2 must prescribe discovery-driven extraction, not seed-file-only |
| W26-3 guards | NARROW | stop_reason stamping ALREADY SHIPPED (_attach_discovery_coverage graph.py:855); ready_for_execution appears nowhere in router — wire it into the 3 exhausted arms as part of the terminal-cleanup invariant |
| W26-7 thread | DEFER | W24-1 latch already shipped (subagents.py:1083-89); add audit test only |
| W26-11 finalizer | DEFER | _diagnose fix folded into W26-8 |
| W26-9 slug | REJECT | no-sku-shape does not exist; _pdp_score accepts alpha slugs (+2 hyphenated leaf, src/discovery.py:486-88); pdp_candidates returns all when min_score unmet (:524-31) — 393's "308/308 rejected" is not producible by this code |

**Terminal-cleanup invariant (missing fix #1, supersedes scattered patches):** one guard —
no exhausted arm may return `cleanup` while `_scraper_has_real_items(min_count=1)` is True
and the six PASS-arm vetoes (`route_after_testing.py:1612-21`) are clean — called from all
three arms (:1824, :1851, :1994).

**Implementation order:** W26-10 → W26-2 → W26-8 → W26-6 → W26-4 → W26-5 → W26-1 →
terminal invariant (W26-3 narrowed).
