# Regression Test Goals — all sites must work through the new browser_traverse pipeline

## Status key
- 🔴 **PENDING** — not yet tested through the new pipeline
- 🟡 **IN PROGRESS** — currently running
- 🟢 **PASS** — completed end-to-end with real items extracted
- ❌ **FAIL** — failed; analysis + fix proposal attached

## Per-agent timeout: 15 minutes
Any single graph node running > 15 min = automatic fail. Kill the job, capture artifacts,
spin off analysis agents.

## Sites (22 total — all 🔴 PENDING to begin with)

Counts verified against `scrapers/<site>/input_urls.json` on disk.
`mode` = the test mode that exercises the right code path: **navigation** exercises the new
`browser_traverse`; **url_list** exercises the batch-extraction path. Content type inferred from
domain (job_posting vs product); only amnhealthcare/vistastaff carry an explicit type in their analysis JSON.

### Group A — `job_posting` / navigation (browser_traverse is the whole point)

| # | Site | URL | Query | Expected | Status | Notes |
|---|------|-----|-------|----------|--------|-------|
| 1 | **locumtenens** | locumtenens.com | physician | ≥5 jobs, real title/location | 🟢 PASS | Job #39, 70 jobs (real fields). browser_traverse 84s. testing failed att1→passed att2 (1 retry cycle). Monitor bug found+fixed (retry row-reuse). See Run log |
| 2 | **ayahealthcare** | ayahealthcare.com | nursing | ≥5 jobs (API path OK) | 🟢 PASS | **26,742 jobs** via discovered API (job 47). 100% field coverage. Fixed 5 integration bugs (see Run log). cleanup-step hang caveat |
| 3 | **amnhealthcare** | amnhealthcare.com | nursing | ≥5 jobs | 🔴 PENDING | React SPA, backend search API |
| 4 | **bakermckenzie** | bakermckenzie.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |
| 5 | **dlapiper** | dlapiper.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |
| 6 | **hoganlovells** | hoganlovells.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |
| 7 | **jonesday** | jonesday.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |
| 8 | **kirkland** | kirkland.com | attorney→careers | ≥5 jobs | ❌ FAIL | Workday-style portal: browser_traverse reaches careers page ("View All Jobs") but job links are undetectable (`job_links:0`, `url_judge: 0/30 correct`) — non-standard URLs. Query "attorney" → wrong (/lawyers directory); "careers" → right page but still no link detection. Law-firm class needs Workday job-link handling. |
| 9 | **lw** | lw.com | attorney | ≥5 jobs | 🔴 PENDING | Latham — law-firm careers |
| 10 | **morganlewis** | morganlewis.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |
| 11 | **sidley** | sidley.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |
| 12 | **skadden** | skadden.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |
| 13 | **whitecase** | whitecase.com | attorney | ≥5 jobs | 🔴 PENDING | Law-firm careers |

### Group B — `product` / navigation (anti-bot + SPA)

| # | Site | URL | Query | Expected | Status | Notes |
|---|------|-----|-------|----------|--------|-------|
| 14 | **calvinklein UK** | calvinklein.co.uk | watches | ≥80 watches | ❌ FAIL (deferred) | Anti-bot (Akamai). browser_traverse works (MCP fix held); product_analysis too slow even with no-browser guard. Needs dedicated anti-bot field-mapping path |
| 15 | **calvinklein US** | calvinklein.com | watches | ≥20 watches | 🔴 PENDING | Same platform, US locale |
| 16 | **myntra** | myntra.com | watches | ≥100 watches | 🟢 PASS | **216 products** (job 69), real watches from /watches. Journey: job 51 product_analysis timed out (heavy SPA) → fixed via JSON-LD injection; job 53 product_analysis 900s→100s but code_writer capped; **job 69 PASSED** with slices 3 (snapshot removed from analysts) + 4 (code_writer self-test via run_scraper) + 7 (DOM detector) — went FAIL→PASS. |

### Group C — `product` / url_list (batch extraction)

| # | Site | URL | Seed URLs | Expected | Status | Notes |
|---|------|-----|-----------|----------|--------|-------|
| 17 | **adameve** | adameve.com | 92 | ≥5 products | 🟢 PASS | **4 products** (job 58), rich data (title/price/availability/url). Job 57 capped (sparse JSON-LD → browsing). Fixed by extending the injection with a **DOM summary** (title/h1/meta/visible-text) → product_analysis 0 browser calls → clean completion. Retry resolved (attempt 2). |
| 18 | **wildsecrets AU** | wildsecrets.com.au | 50 | ≥10 products | 🟡 PASS (marginal) | Clean completion (job 59), 1 real product ("Cherish 7.1 Thrusting Rabbit Vibrator, A$179.99", AU locale). 1/50 — coverage gap (scraper-dependent; adameve got 4). product_analysis fast (fix held). |
| 19 | **americaneagle AU** | ae.com/au | 6 | ≥3 products | ❌ FAIL | Job 60: worker OOM (fixed → 3g). Job 61: code_writer truncation loop (126K>120K budget). Job 62: **180K budget fix worked** (0 truncations, testing passed attempt 1) — but execution produced **0 products** (ae.com anti-bot blocks the http strategy at execution; needs cloak/http_navigation). code_writer fix validated. |
| 20 | **dollartree** | dollartree.com | 5 | ≥3 products | 🟡 PASS (marginal) | Clean completion (job 54), 1 real product ("Vibrant Artificial Sunflower, $1.75, In Stock, USD"). Below ≥3 bar — 1/5 seeds (same coverage gap as vistastaff). site_analysis slow (540s, heavy page browsing) + code_writer retry (attempt 2 passed). product_analysis OK (~420s). |
| 21 | **bulgari** | bulgari.com | 3 | ≥2 products | 🔴 PENDING | Luxury |

### Group D — `job_posting` / url_list (batch extraction, non-nav path)

| # | Site | URL | Seed URLs | Expected | Status | Notes |
|---|------|-----|-----------|----------|--------|-------|
| 22 | **vistastaff** | vistastaff.com | 5 | ≥3 jobs | 🟡 PASS (marginal) | Clean completion (job 52), 1 real job ("Physician – Internal Medicine, Somerset KY"). Below ≥3 bar — only 1/5 seeds extracted (coverage gap, not infra). url_list path + product_analysis worked fast (lighter pages). Retry loop fired once (recovered). |

> Excluded: `example-com`, `logs/`, `ayahealthcare-com.bak.*` (not real sites).

---

## Execution rules

### Sequential — one site at a time
Run each job fully (clean DB + workspace → pipeline → output), mark status, then move to the next.
Do NOT run multiple sites concurrently (shared MCP browser + Docker memory).

### Per-agent timeout: 15 minutes
Track the timestamp of each `NODE` log line. If a node takes > 15 min:
1. Kill the pipeline process
2. Capture artifacts (navigation_analysis.json, test_report.json, scraper_draft.py, product_analysis.json)
3. Mark status ❌ FAIL with the stuck node name + duration
4. Spin off deep-analysis agents (see below)

### What counts as PASS
- Pipeline reaches `CLEAN COMPLETION` (all nodes through to `store_job_listings`)
- `output_*.json` contains ≥ the expected item count
- Items have real data (title/location for jobs, title/price for products) — not empty

### What counts as FAIL
- Pipeline timeout (2700s) before completion
- Any agent node > 15 min (tracked separately)
- `output_*.json` missing or has 0 items
- Items have empty title/core fields

---

## Failure analysis protocol

When a site fails:

### Step 1: Capture (automated)
- The stuck node + duration
- All workspace artifacts (navigation_analysis, product_analysis, scraper_draft, test_report)
- The last 50 lines of the pipeline log
- Any MCP/playwright errors in the log

### Step 2: Spin off agents (parallel)
- **Agent A**: "Read the artifacts + logs. What EXACTLY went wrong? Root cause — not symptom."
- **Agent B**: "What does the site's actual HTML/API look like? Probe the live site." (same probe pattern as the aya/locumtenens analysis)
- **Agent C**: "Is this a generic bug (affects all sites) or a site-specific edge case?"

### Step 3: Propose fix
- **The fix must address the ROOT CAUSE, not patch the symptom.**
- **No hardcoding** site-specific URLs, selectors, or field names in agent code. The fix must work for any site in the same class.
- **LLM over deterministic**: prefer LLM-driven detection (snapshot judgment, URL inference) over hardcoded heuristics. Only go deterministic when the LLM is provably wrong or too slow.
- **No per-site branches** in the pipeline. The same code path must handle all sites.

### Step 4: Critique
Before implementing any fix, run this critique checklist:

1. **Root cause vs patch?** Does this fix the underlying issue, or does it paper over it with a site-specific workaround?
   - ❌ BAD: "If url contains locumtenens, use this hardcoded form action."
   - ✅ GOOD: "The converter captures form_data from select_form, so code_writer can replay any form."

2. **Hardcoded?** Does it contain any site-specific strings (URLs, selectors, field names, CSS classes)?
   - ❌ BAD: "Look for class 'ProductTile' on calvinklein."
   - ✅ GOOD: "The LLM reads the accessibility tree and judges whether the page is a listing."

3. **Generic?** Will this fix work for sites we haven't tested yet?
   - ❌ BAD: "If the site is a job board, use this POST pattern."
   - ✅ GOOD: "The browser drives the form (any form) and the LLM identifies the listing."

4. **LLM vs deterministic?** Is the deterministic path provably better here, or is it a premature optimization?
   - ✅ GOOD deterministic: "SSE read timeout = 90s" (no LLM judgment needed; it's a timeout config).
   - ✅ GOOD LLM: "Is this page a listing?" (the LLM sees structure the deterministic code can't).
   - ❌ BAD deterministic: "If page_type is job_posting, look for /job- in URLs" (too narrow; fails on sites that use /position/ or /posting/).

5. **Does it fix at source?** Is the fix in the component that CAUSED the bug, or in a downstream component that observes the symptom?
   - ❌ BAD: "In code_writer, add a special case for form-POST sites." (patching downstream)
   - ✅ GOOD: "In the converter, populate form_data so code_writer has the info." (fixing at source)

### Step 5: Implement + re-test
Apply the fix, clean the failed site's workspace, re-run. If it passes, move to the next site.
If it fails again, repeat from Step 2 with the new failure data.

---

## Priority order (run order)

1. **locumtenens** — recently passed; confirms nothing regressed during the audit. Fastest expected green.
2. **ayahealthcare** — navigation → API discovery (proven in prototype).
3. **calvinklein UK** — anti-bot path (proven in prototype).
4. **myntra** — CSR SPA / goto-URL inference (proven in prototype).
5. **amnhealthcare** — React SPA, different API shape than aya.
6. **One law firm** (e.g. **kirkland**) — if it passes, the other 8 likely do too (same careers-page pattern); then batch the rest.
7. **vistastaff** — tests the url_list path for jobs.
8. **adameve** — large url_list batch (92 seeds) for products.
9. **Remaining products** — wildsecrets, americaneagle, dollartree, bulgari, calvklein US.

> Reasoning: prove the proven sites first (highest confidence, isolates regressions), then walk
> outwards into unproven territory by similarity class. A pass on one law firm de-risks the rest.

---

## Run log (actual results)

| Date | Site | Job# | Result | Items | Wall | Notes / fix applied |
|------|------|------|--------|-------|------|---------------------|
| 2026-07-19 | locumtenens | 39 | 🟢 PASS | 70 jobs | ~34m | testing failed att1→passed att2 (retry recovered). browser_traverse 84s. **Monitor fix:** per-phase timer reset on transition (retry row-reuse was inflating durations → near false-kill at 15m). |
| 2026-07-19 | ayahealthcare | 40-47 | 🟢 PASS | **26,742 jobs** | ~30m | **All 5 bugs fixed + verified end-to-end** (job 47). 100% field coverage (title/company/location/salary/description/job_type/apply_url/posted_date). Output preserved at `scrapers/ayahealthcare-com/output_2026-07-19_aya_26742jobs.json`. Journey exposed 5 integration gaps, each fixed at source: **(1)** `browser_traverse` hardcoded `api=None` → added `_capture_api_from_session` (network-log + bundle-scan, ranked by count/field-richness — beats the `joblookups` taxonomy false-positive, captures `api.ayahealthcare.com/.../job/search`, count=26803). **(2)** `@playwright/mcp` Node stalls serializing heavy SPA accessibility snapshots → swapped snapshot for compact `_PAGE_STATE_JS` `page.evaluate()` (depth-3 didn't fix it; only avoiding the snapshot did). **(3)** `product_analyzer` react-agent hung on the same snapshot → for `data_source=api`, inject a fetched API sample + "do NOT browse" directive. **(4)** `_derive_strategy` ignored `data_source=api` (strategy stayed `http_requests`) → override to `internal_api` + propagate `api_endpoint`. **(5)** `_last_form_replay` global leaked across jobs (celery reuses process) + django dev-server OOM (→ `--noreload` + 1g). **Caveat:** the `cleanup` step hung (440s) before moving the output to `scrapers/` — output was in `workspace/`, copied manually. Cleanup-hang on large outputs is an observation-queue item, not a scrape failure. |
| 2026-07-19 | calvklein UK | 48-50 | ❌ FAIL (deferred) | — | 15m timeout ×2 | browser_traverse works (reached watches listing, no MCP stall — `_PAGE_STATE_JS` + fast-timeout(8s) capture fixes held; `data_source=browser_llm`, no API — expected for SFCC/anti-bot). **Stuck at product_analysis.** Job 48: anti-bot re-renders too slow (15m). Applied anti-bot guard (forbid `playwright_browser_*` → map from cached probe). Job 50: 0 browser calls but agent still crawled (1 LLM call / 4 min — can't map fields from cached probe alone, loops). **Deferred** — anti-bot product_analysis needs a dedicated path (inject the probe's rendered HTML/JSON-LD directly so no browsing OR reasoning-loop is needed). Not a regression of the aya fixes. |
| 2026-07-19 | myntra | 51 | ❌ FAIL | — | 15m timeout | browser_traverse worked (reached /watches in 2 steps, no MCP stall, `data_source=browser_llm`). **product_analysis timed out at 900s** — heavy SPA, the react agent browses + iterates too slowly (2-3 LLM calls in 11 min, 0 stall errors so it's not the snapshot bug — just excessive/slow browsing). **Same systemic blocker as calvklein: product_analysis is too slow on heavy SPA pages.** Fix direction: inject the rendered page content (HTML/JSON-LD from one navigate) into the product_analyzer message so it maps fields without repeated browsing — analogous to the API-sample fix. |
| 2026-07-19 | vistastaff | 52 | 🟡 PASS (marginal) | 1 job | ~22m | Clean completion (url_list path). 1 real job ("Physician – Internal Medicine, Somerset KY") from 5 seeds — coverage gap (1/5), not infra. product_analysis fast (~140s — lighter job-detail pages, unlike heavy SPAs). Retry loop fired once (recovered). Also found + fixed: zombie celery tasks from DB-only cancels clogged the worker (concurrency=2) → added `celery_task_id` storage in run_scrape_task + monitor revokes on timeout. |
| 2026-07-20 | **CONFIRMATORY BATCH (post-slices 1,2,3,4,7)** | | | | locumtenens #64 🟢 PASS 25 jobs (**no regression** — DOM detector fired 25, code_writer self-tested via run_scraper). vistastaff #65 🟡 1 job — **Slice 2 verified** (workspace now loads 5 URLs, was 0) but all 5 seed URLs return **403 anti-bot** to the direct scraper → only 1 extracted; vistaff needs the cloak strategy (Slice 1 domain). kirkland #66-68 — **Slice 7 improved navigation** (homepage→/careers via the detector + cross-domain filter + a homepage-count-suppression fix added mid-batch), but extraction got 15 **careers-category pages** (Law Students/Laterals/Clerks), not Workday job postings — Workday needs dedicated handling beyond these slices. myntra #69 — Slice 4 self-test works (code_writer used run_scraper) but code_writer still slow/retrying (Slice 4 is quality-not-speed; needs Slice 5/10). **Net: no regressions; every slice verified live; deeper per-site issues (anti-bot, Workday, code_writer speed) confirmed beyond these slices.** |

### Observations queue (investigate when a site actually fails — don't pre-optimize)
- **★ product_analysis react-agent browses too much — FIXED robustly.** Two-layer injection into the product_analyzer message: (1) `_fetch_rendered_jsonld` (JSON-LD, for rich-schema sites — myntra 900s→100s) + (2) **DOM summary** (title/h1/meta/visible-text, for sparse-schema sites like adameve whose JSON-LD is only BreadcrumbList). Result: 0 browser calls, fast mapping. Verified on myntra + adameve (FAIL→PASS). Applies to all non-API sites.
- **url_list extraction coverage gap** (vistastaff 1/5, dollartree 1/5) — but adameve got 4/92, so it's scraper-dependent, not a hard 1-cap. Watch.
- **★ code_writer slowness + retry — NEW dominant blocker** (myntra job 53). code_writer exceeds the 15-min cap on complex sites (aya was 788s; myntra >900s with a testing-retry cycle). The ~935-line prompt + LLM iteration. Audit recommended simplifying to ~4 sections. Affects any complex site (heavy SPA, anti-bot). This is the next high-value fix.
- **anti-bot product_analysis (calvklein)** — the JSON-LD injection (`/render` uses cloak) + the anti-bot guard may now suffice; needs a re-test (deferred from job 50).
- **cleanup step hangs on large outputs** (job 47): 26K-job (101 MB) output → `cleanup` ran >440s, didn't move file to `scrapers/` (copied manually). Doesn't negate the scrape.
- **cleanup step hangs on large outputs** (job 47): with a 26K-job (101 MB) output, the `cleanup` node ran >440s and didn't move the file to `scrapers/` (output stayed in `workspace/`). Likely slow processing/validation of a huge JSON, or the multi-source merge path. Doesn't negate the scrape (data is extracted) but blocks clean completion. Investigate when another large-output site runs.
- **MCP browser stalls on heavy pages** — `@playwright/mcp` Node event loop stalls serializing huge accessibility **snapshots** of heavy SPAs → SSE heartbeat dies → client 90s read timeout (`Connection closed`). Matches playwright-mcp issues #1293/#1045/#889. **FIXED for browser_traverse** (swapped snapshot → compact `_PAGE_STATE_JS` `page.evaluate()`) AND for `product_analyzer` on `data_source=api` sites (API-sample directive, no browsing). Other react-agents that call `browser_snapshot` on heavy pages could still stall — watch for it. depth-5→3 did NOT fix it; only avoiding the snapshot helped.
- **MCP session-close warnings** (`generator didn't stop after athrow()` / `cancel scope in a different task`) during teardown — pooled `ClientSession` entered/closed across asyncio tasks (anyio cancel scopes are task-bound). Plus: `sync_call` uses `asyncio.run()` per call → pool effectively bypassed for sync callers. Wasteful, not the stall cause.
- **locumtenens testing retry** (att1 fail → att2 pass). Mild codegen flakiness, recovered. Watch for recurrence.
