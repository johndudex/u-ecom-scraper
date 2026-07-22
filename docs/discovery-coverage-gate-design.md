# Discovery Coverage Gate — Design (revised after 4-agent critique pass)

> Status: **Design, post-critique, pre-implementation.** This revision incorporates
> findings from 4 critique agents (genericness, integration-correctness,
> data-capture feasibility, adversarial completeness). The original "two-layer"
> framing was restructured into a **tiered gate** because the critiques proved the
> coverage data is not reliably present across sites.

## Problem

Two-phase scrapers (Phase 1 discovers item URLs via search/categories/pagination;
Phase 2 extracts fields) can **silently under-cover** a site. Locumtenens: a site
with **3771 jobs across 207 specialties** produced **38 jobs**, and the pipeline
reported success because Phase 2 extraction "worked" (good field coverage on the 38
it found). The existing strategy cascade retries on **0-item failures**; it has no
equivalent for **partial-coverage** failures.

**Important:** the locumtenens under-coverage likely has *multiple* causes, not just
strategy misclassification. The adversarial critic found a **latent checkpoint bug**
(see H3 below) where `code_tester` writes a checkpoint that `run_execution` then
loads, skipping Phase 1. The 38 may be partly that leak.

## Why a naive "found vs site-reported total" gate is NOT generic

A ratio gate fails on three axes (per genericness + data-capture audits):

1. **Articles / `page_content` have no meaningful total.**
2. **Jobs/forums need *dimension*-completeness** — a ratio *hides* the locumtenens
   bug (one popular specialty returns 2000 of 3771, passes any ratio, skips 29
   specialties).
3. **code_tester runs Phase 1 capped** (`--sample` forces `limit=5`; the `--limit 50`
   in the prompt is dead — `navigation_scraper.py:567`). At test time the pipeline
   cannot see full-site coverage; the 38-of-3771 manifested at *runtime*.

## The core reframe

> **Stop asking "did we find *enough*?" Ask "did the scraper stop because it
> *genuinely exhausted* the source, or because it *gave up*?"**

This reframe is forced by the **adversarial critic's H4 finding** (the most
important in the whole review): a boolean `pagination_exhausted` is a catastrophic
false-pass machine. When `_navigate` returns `None` after rate-limit retries
(429/502/503), the loop breaks "page N navigate failed, stopping" — a boolean gate
sets `exhausted=True` and **declares success on a blocked scraper**. Same for a
broken item-link selector (every page yields `[]` new → "no new items" → looks
exhausted). This is precisely the locumtenens shape.

**The gate's primary signal is therefore a `stop_reason` enum, not a boolean:**

| `stop_reason` | Meaning | Gate verdict |
|---------------|---------|--------------|
| `short_page` | Last page had `< items_per_page` items | **exhausted** (PASS) |
| `no_next_link` | No next-page element/URL found | **exhausted** (PASS) |
| `max_pages_hit` | Hit `MAX_PAGES` cap | **inconclusive** — see below |
| `navigate_error` | Stopped due to HTTP errors / rate-limiting / blocks | **NOT exhausted** (FAIL → retry) |
| `no_new_items` | Consecutive pages returned only duplicates | **inconclusive** — could be broken selector |
| `dedup_flat` | unique/seen ratio never flattened (feed injection) | **NOT exhausted** (FAIL → retry) |

Rules:
- `max_pages_hit` counts as exhausted **only if `MAX_PAGES` was `None`** (H5). A
  capped termination on a large site is inconclusive — only PASS if a trusted-total
  ratio also holds; else soft warning.
- `no_new_items` requires a `dedup_flat` cross-check: if `unique/seen` flattened,
  it's genuine exhaustion; if not, the selector is likely broken.

## Architecture: a TIERED gate (opportunistic by data availability)

The data-capture critic proved the dimension/ratio data exists on **~1 of 5 sites**
in the current corpus. So the gate cannot *depend* on that data. It is tiered: each
tier activates only when its input is trustworthy, and the gate FAILS if any active
tier signals insufficient.

### Tier 1 — `stop_reason` validation (ALWAYS ON, fully generic)
Reads only signals every scraper can emit. Catches the "gave up due to
errors / rate-limiting / broken selector / broken strategy" class for **every site
and every content type with a discovery phase**. This alone prevents the H4
false-passes. Cost: a `stop_reason` field in template metadata.

### Tier 2 — dimension-completeness (ON when dimensions are deterministic)
Active only when `dimensions_total` is captured deterministically (classic
`<select>`-form sites today; extensible). Catches the locumtenens-class
(1/207 specialties iterated). When `dimensions_total` is unknown/0 → tier is a
no-op (PASS), falling back to Tier 1.

### Tier 3 — ratio-against-trusted-total (ON when total is `site_reported`)
Active only when `coverage_target.source == "site_reported"` **and** the total was
captured by the deterministic extractor (not LLM-asserted). Catches locumtenens via
the 38/3771 ratio. When no trusted total → tier is a no-op.

**Honest boundary:** a site with neither deterministic dimensions nor a trusted
total (e.g. ayahealthcare, amnhealthcare, modern AJAX job boards) is governed by
**Tier 1 only**. Tier 1 catches "gave up" failures but *cannot* catch "cleanly
returned few." That residual gap is the real limitation — see Residual risk.

### Honest genericness scope (corrected from original)

| Content type | Discovery phase? | Tier 1 | Tier 2 | Tier 3 |
|--------------|------------------|--------|--------|--------|
| `product` | yes (nav/list/search) | ✅ | when collection count deterministic | when total trusted |
| `article` | yes | ✅ | n/a (scope is date/query, not partition) | rarely |
| `job_posting` | yes | ✅ | **classic-form sites** (locumtenens) | when total trusted |
| `forum_thread` | **no** (`url_list` only) | no-op | — | — |
| `serp` | yes (`search_term`) | ✅ | n/a | Google estimate is NOT trusted |
| `page_content` | **no** (`url_list` only) | no-op | — | — |

**Corrected claim:** the gate works for the **4 content types with a discovery
phase**; it is a no-op for `forum_thread`/`page_content` (url_list-only). For SERP,
rank contiguity (originally claimed) is **unimplemented** — Tier 1 + scope-match is
the realistic coverage. Scope-match is addressed separately below.

## The complementary gate: scope-match (scoped jobs)

The genericness critic's strongest finding (Scenario 1): the locumtenens job was
*not* "scrape all 3771" — the user's scope was Alabama + last 7 days. The system
**deliberately** treats `search_criteria` as a discovery *hint*, not a filter, for
navigation jobs (`run_execution.py:208-213`). So a dimension-completeness gate
rewards *full-catalog coverage* while the user asked for a *filtered subset* — it
would PASS a scraper returning thousands of multi-state jobs violating the user's
scope.

**Scope-match is a *different axis*** from coverage ("did we honor the user's
filter?" vs "did we cover the source?"). `assess_query_match`
(`content_types.py:325-376`) already does this generically but is **advisory-only**.

**Decision:** scope-match promotion is a **separate, complementary gate**, not folded
into this one. It is content-type-agnostic and load-bearing for scoped jobs, but it
is a distinct concern with its own thresholds and its own design. This gate focuses
on coverage; scope-match is flagged as the next design.

## Integration contract (CORRECTED — the original was false)

The integration-correctness critic disproved my original contract. The real
machinery requires these changes:

**Original (false) claim:** *"classify_test_failure → ('strategy', reason) is the
single chokepoint; no new state field needed."*

**Reality:**
1. **`classify_test_failure` is coverage-blind** (`route_after_testing.py:62-103`).
   It reads only `crash_error`, item count, and timeout/blocked/selector regexes.
   For items > 0 (partial coverage) it returns `("refine", ...)` — **never**
   `"strategy"`. → **Requires a new deterministic coverage-aware branch** that reads
   `stop_reason` / `dimensions_iterated` / `dimensions_total` from `test_report` and
   returns `("strategy", "discovery incomplete: ...")`.
2. **`strategies_tried` recording bypasses the remediation override.**
   `_invoke_scraper_analyzer` (`graph.py:1810-1811`) calls `classify_test_failure`
   *directly*, ignoring route_after_testing's `remediation.target` override. Even
   when routing succeeds, the failed strategy may not be recorded → the cascade
   re-picks it. → **Fix the recording path** to honor coverage classification (or
   route the remediation check through it).
3. **"Run to exhaustion" is unbounded and misroutes.** Templates have no wall-clock
   discovery budget; `run_scraper`'s **300s timeout** (`shell_tools.py:164`) SIGKILLs
   the probe on large sites → the timeout-shaped crash string makes
   `classify_test_failure` return `("strategy","playwright timed out")` → the cascade
   switches strategy on a *correct* scraper that needed more time → burns all 6
   retries (H1). → **Requires a dedicated long-timeout probe path** (new `run_scraper`
   arg, e.g. `timeout=1800`, gated to the coverage probe only).
4. **Anti-bot downgrade swallows coverage switches** (`route_after_testing.py:378`):
   on anti-bot sites a coverage-triggered `("strategy",...)` is forced to
   `("scraper",...)`. → **Reconcile**: either exempt coverage failures from the
   downgrade or document anti-bot sites as out of scope.
5. **No `--discover-only` template flag.** Removing the `--limit` cap makes Phase 2
   extract *thousands* of URLs. → **Add `--discover-only`** (or `--max-discover N`)
   so Phase 1 exhausts while Phase 2 stays bounded.
6. **Interrupt-reason collision.** `validate_coverage.py:204` already emits
   `interrupt_reason: "coverage_exhausted"` for *field* coverage. → **Use a distinct
   reason** `discovery_coverage_insufficient` with a dedicated
   `route_from_human_approval` branch (M2).
7. **Retry budget sharing.** Coverage failures cannibalize `MAX_TEST_RETRIES=6`
   alongside real bug-fixes, and a hard site that under-covers on all strategies may
   never reach `human_approval` with partial output. → **Separate coverage-retry
   budget**, or fall through to the partial-output path after N coverage-only
   failures (M4).

## Data-capture pillar (REALISTIC version — replaces original fixes)

The original 3 fixes were directionally right but the data-capture critic proved two
are not shippable as described. Realistic versions:

### Fix #1 — Structured `coverage_target` (feasible-but-fragile)
Add to `navigation_analysis`:
```python
"coverage_target": {
  "total_items": int | None,
  "dimensions": [{"name": str, "count": int}],   # only when deterministic
  "source": "site_reported" | "estimated" | "unknown",  # ORCHESTRATOR-stamped, not LLM
  "raw_count_string": str | None                 # the text the total was parsed from
}
```
- **Parser:** a defensive 3-regex cascade — `(start)\s*[-–]\s*(end)\s+of\s+(total)`,
  `(total)\s+(items|jobs|results|products)`, bare `\d{1,3}(,\d{3})+` — with a
  **currency/decimal negative guard** (reject `"$128,502.00"`, which otherwise
  collides with bare `"12,515"`). Do NOT attempt locale handling — no localized
  artifacts exist to test against.
- **`source` must NOT be LLM-self-certified.** A hallucinated `site_reported` is the
  most damaging failure (it *inverts* the locumtenens bug — blocks a correct 38-item
  output demanding 3771). Derive deterministically: `site_reported` only if the
  explorer captured a raw count string AND the parser succeeded; `estimated` only if
  derived from `len(category_links)`/`option_count`; else `unknown`. The LLM fills
  the number; the orchestrator stamps the source.

### Fix #2 — Count reading (NOT "LLM selector at runtime")
The original "read via LLM-identified selector at runtime" is **not feasible**: the
LLM emits a valid selector **20% of the time** (1/5 sites); `http_requests` cannot
evaluate CSS selectors for AJAX-rendered counts; the constant is already dead code
(`http_navigation_scraper.py:93`). Realistic version:
- **Extend the deterministic extractor's allowlist** (`navigate_explore.py:767-797`)
  with the locumtenens pattern (the real signal is "element whose text matches
  `\d+ - \d+ of \d+`", not a broad class glob).
- **Treat the LLM's `total_count_selector` as a fallback hint**, retried only when
  the deterministic extractor returns `None`.
- **Scope runtime count-reading to browser strategies only** (`playwright`,
  `undetected_chromedriver`, `http_navigation` where a DOM/HTML exists). Never
  promise it for AJAX-filtered catalogs under `http_requests`.

### Fix #3 — Thread `coverage_target` into `build_code_tester_message` (feasible, cheap)
Confirmed absent today (`subagents.py:2829-2948`). 5-line wiring change. **But the
tester must treat `total_items=None` and `dimensions=[]` as "Tier 1 only,
exhaustion-only"** — threading an empty object teaches nothing on 4/5 sites.

### Anti-improvisation (required, not optional)
Adding `coverage_target` to the schema is insufficient — the LLM will keep emitting
`example_total`, `total_jobs`, `category_tabs[].count` outside it (it does today).
Required:
- Add explicit anti-pattern guidance: *"count fields appear ONLY under
  `coverage_target`; do not emit `example_total`/`total_jobs`/`total_count`."*
- Apply the same constraint to `product_analyzer` (which improvises a *different*
  count vocabulary) or forbid count fields there.
- Post-validate `navigation_analysis.json` and reject unknown count keys.

## Latent bug to fix alongside: checkpoint cross-contamination (H3)

Independent of the gate, **this is likely part of the locumtenens 38-job cause.**
Both templates load `discovered_urls_checkpoint.json` and skip Phase 1 on resume
(`navigation_scraper.py:578-581`, `http_navigation_scraper.py:786-790`). The
checkpoint is written to `workspace/{slug}/` (cwd under browser_service);
`setup_workspace` cleans stale artifacts **once at job start**, not between test and
execution phases. So `code_tester`'s checkpoint (≤5 URLs) persists into
`run_execution`, which loads it and skips Phase 1 → extracts only those URLs →
reports SUCCESS. The "full extraction" comment (`run_execution.py:199-202`) is a lie
under this path.

**Fix:** delete or namespace `discovered_urls_checkpoint.json` after each run (or
pass `--fresh-discovery` from `run_execution`). The checkpoint's only legitimate
consumer is `browser_service`'s own Chrome-crash retry (`scraper_runner.py:120`);
clean it in `_post_run`.

## Multisource aggregation (H2)

For `input_mode in (navigation, list_page, search_term)` with `search_criteria`,
`run_execution` calls `_run_category_sources` (`run_execution.py:271-413`) which
re-runs the scraper against up to 5 extra category URLs and writes a merged output
with **fresh metadata and no `discovery_coverage` block**. Meanwhile `code_tester`
tests the primary source only — "the verdict and the output describe different
things."

**Fix:** `_run_category_sources` must aggregate `discovery_coverage` (sum `found`,
max `dimensions_total`, min `dimensions_iterated`, AND `stop_reason` precedence where
any `navigate_error`/`dedup_flat` wins). Decide and document whether the test-time
gate runs pre- or post-merge conceptually.

## Where the gate reads "found"

**Read `extracted_items` (post-filter), NOT `discovered_urls` (raw).** Raw
discovered counts include nav/redirect/blocked/soft-404 noise; a broken broad-link
discovery reporting 5000 URLs would defeat the gate. `route_after_testing` has
`_is_dead_product` for extracted items but nothing for discovered URLs (M5).

## Edge cases (expanded from adversarial critic)

| ID | Case | Handling |
|----|------|----------|
| H1 | 300s timeout kills probe + misroutes | dedicated long-timeout probe path |
| H2 | multisource merge drops coverage | aggregate `discovery_coverage` in merge |
| H3 | checkpoint cross-contamination test→execution | clean/namespace checkpoint; `--fresh-discovery` |
| H4 | "exhausted" vs "gave up" | `stop_reason` enum; `navigate_error`/`dedup_flat` force retry |
| H5 | `max_pages_hit` with LLM-set cap | inconclusive unless `MAX_PAGES=None`; require ratio cross-check |
| M1 | test-runner vs execution-runner divergence | run probe through same runner; document Layer-1 catches gross failures only |
| M2 | interrupt-reason collision with field-coverage | distinct reason `discovery_coverage_insufficient` |
| M3 | stale `coverage_target` on resumed list_page/search_term | freshness timestamp; invalidate on resume |
| M4 | coverage retries starve `MAX_TEST_RETRIES` | separate budget OR fall-through to partial-output after N coverage-only fails |
| M5 | discovery inflation by dupes/soft-404s | gate reads `extracted_items`, not `discovered_urls` |
| — | genuinely small site (12 products) | `short_page` on page 1 = exhausted = PASS |
| — | infinite scroll/feed injection | `dedup_flat` signal (unique/seen flattening) |
| — | no discovery phase (`page_content`/url_list) | no-op, gated on `input_mode` |

## What this retires from earlier (non-generic) proposals

- ❌ "Coverage vs site-reported total" → Tier 3 only, opportunistic, source-gated.
- ❌ "Validate category_links against URL pattern" → check dimension *iteration*.
- ❌ "Invert `_form_only` gate" → validate *output* via `stop_reason`.
- ❌ "Zero the navigation-agent temperature" → never the issue.
- ✅ Kept: cascade reuse, runtime metadata emission, `stop_reason` over boolean.

## Residual risk (honest)

1. **The "cleanly returned few" blind spot.** A broken strategy that returns a clean
   short page on the few items it can see — with no trusted total and no
   deterministic dimensions to contradict it — passes Tier 1. This is the irreducible
   boundary of a gate without site-reported ground truth. Mitigated only by broader
   deterministic data-capture (making more sites look like locumtenens).
2. **Coverage only as honest as the template's error path.** A hard SIGKILL (H1)
   produces no metadata at all; a `browser_service` Chrome crash returns partials
   with a normal exit code (L4). `stop_reason` is only meaningful when the template's
   error handling surfaces it.
3. **Test-runner vs execution-runner divergence** (M1) means Tier 1 at test time
   catches gross failures only; the real coverage verdict is at execution.
4. **Scope-match is unaddressed** here — a separate complementary gate is needed for
   scoped jobs (see above).

## Critique pass — what changed

Four critique agents reviewed the original design. Outcomes:
- **Genericness:** partially surviving. Downgraded from "all 6 content types" to "the
  4 with a discovery phase"; SERP rank-contiguity claim retracted (unimplemented);
  dimension-completeness shown to be deterministic for classic-form sites only.
  Scope-match surfaced as a load-bearing *separate* concern.
- **Integration correctness:** the original integration contract was **false**.
  `classify_test_failure` is coverage-blind; `strategies_tried` recording bypasses
  the remediation override; "run to exhaustion" is unbounded and misroutes via the
  300s timeout. All three require concrete code changes (now listed above).
- **Data-capture feasibility:** the data exists on **1 of 5** sites. Fix #2 (LLM
  selector at runtime) is not feasible as described; replaced with deterministic
  extractor extension + browser-only count reading. `source` must be
  orchestrator-stamped, not LLM-self-certified.
- **Adversarial completeness:** `stop_reason` enum (not boolean) is the single most
  important fix — without it the gate rubber-stamps the locumtenens failure. Checkpoint
  cross-contamination (H3) is a latent bug contributing to the 38-job cause. Multisource
  merge (H2) discards coverage.

**Net effect on the design:** the flat "two-layer" model became a **tiered gate**
(Tier 1 `stop_reason` always-on and generic; Tiers 2/3 opportunistic on trustworthy
data). This is more honest about what the gate can and cannot catch.

## References (file:line)

- `webapp/agents/nodes/route_after_testing.py:62-103` — `classify_test_failure` (coverage-blind; needs new branch)
- `webapp/agents/nodes/route_after_testing.py:304-330` — existing phase1 boolean gate
- `webapp/agents/nodes/route_after_testing.py:378-379` — anti-bot downgrade (swallows coverage)
- `webapp/agents/graph.py:1792-1923` — `_invoke_scraper_analyzer`, `strategies_tried` recording (bypasses override)
- `webapp/agents/subagents.py:2942-2957` — code_tester Phase 1 + dead `--limit 50`
- `webapp/agents/subagents.py:2966-2971` — "PASS on field quality" (contradicts coverage)
- `webapp/agents/subagents.py:1229-1259` — synthesize template (no `coverage_target`)
- `webapp/agents/nodes/navigate_explore.py:767-797` — hardcoded total-count selectors
- `webapp/agents/nodes/navigate_explore.py:1815-1844` — `_CLASSIC_FORM_DETECT_JS` (only deterministic dimension source)
- `webapp/agents/nodes/navigate_synthesize.py:540-548` — unparsed "Found N of M" note
- `webapp/agents/nodes/run_execution.py:199-213, 271-413` — scope-as-hint + multisource merge
- `webapp/agents/nodes/validate_coverage.py:204` — existing `coverage_exhausted` reason (collision risk)
- `webapp/agents/state.py:136` — `navigation_analysis` in state
- `webapp/agents/constants.py:11` — `MAX_TEST_RETRIES=6`
- `webapp/agents/tools/shell_tools.py:164` — `run_scraper` 300s timeout (H1)
- `templates/navigation_scraper.py:567, 578-581, 712-726` — `--sample` limit=5, checkpoint load, metadata block
- `templates/http_navigation_scraper.py:93, 142, 514-517, 786-790, 895-911` — dead TOTAL_COUNT_SELECTOR, checkpoint, navigate_error stop, metadata
- `browser_service/scraper_runner.py:120, 163` — checkpoint legitimate consumer, cwd
- `src/content_types.py:221,255,325-376` — url_list-only types, `assess_query_match` (advisory)
- `scrapers/locumtenens-com/analysis/navigation_analysis.json:157,160-162` — the `"1 - 100 of 3771"` + valid selector
- `scrapers/locumtenens-com/analysis/test_report.json` — "5 URLs across 207 specialties" smoking gun
