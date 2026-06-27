# Scraper Agent Audit: CK UK "Watches" Pipeline

## What the User Expected

1. Go to calvinklein.co.uk
2. Find the search icon, type "watches"
3. Execute search → site returns ~93 results
4. Open at least one product from the results
5. Map what information is available (title, price, images, etc.)
6. Write code that: searches for "watches", extracts product data from each result
7. Test the code against what was observed in step 5

---

## Critical Bug Found: `search_term` Input Mode Skips Navigation Entirely

**File:** `webapp/agents/graph.py:2086`

```python
def _route_after_site_analyzer(state: ScrapeState) -> str:
    input_mode = state.get("input_mode", "url_list")
    if input_mode in ("navigation", "list_page"):
        return "navigation_explore"
    return "update_tracker_analysis"
```

The `ScrapeJob.input_mode` field has 4 choices: `url_list`, `navigation`, `list_page`, `search_term`.

When a user submits a job with `search_criteria="watches"`, the input mode should be `search_term`. But `_route_after_site_analyzer` only checks for `navigation` and `list_page` — `search_term` falls through to `update_tracker_analysis`, **completely bypassing `navigation_explore`** and `navigation_synthesize`**.

This means:
- **navigation_explore never runs** → no search is performed, no product URLs discovered
- **navigation_synthesize never runs** → no search URL pattern, no item link selectors
- The pipeline goes straight to `update_tracker_analysis → validate_analysis → product_analyzer`
- Product-analyzer has no discovered product URLs, no search context, nothing to work with

**Fix needed:** Add `"search_term"` to the routing condition in `_route_after_site_analyzer`:
```python
if input_mode in ("navigation", "list_page", "search_term"):
    return "navigation_explore"
```

---

## Pipeline Architecture (Current)

```
START → parse_command → check_tracker → setup_workspace
  → check_accessibility (probe connectivity)
  → site_analyzer (LLM agent)
  → _route_after_site_analyzer
      ├─ navigation/list_page → navigation_explore → navigation_synthesize → product_analyzer
      └─ url_list/search_term → update_tracker_analysis → validate_analysis → product_analyzer
  → normalize_fields → validate_coverage
  → scraper_analyzer (LLM agent)
  → code_writer (LLM agent)
  → code_tester (LLM agent)
  → [retry loop: code_tester → scraper_analyzer → code_writer → code_tester]
  → pre_execution_approval
  → run_execution
  → cleanup
  → nav_skill_review (post-scrape learning)
  → skill_learner
  → END
```

---

## What Each Agent Should Do for "CK UK, search for watches"

### 1. parse_command (deterministic)
**Should do:** Parse `url=https://www.calvinklein.co.uk`, `search_criteria="watches"`, set `input_mode="search_term"`, `site_slug="calvinklein-co-uk"`.

**What it does:** ✅ Works correctly. Sets `site_slug` and passes through `search_criteria`.

---

### 2. check_tracker (deterministic)
**Should do:** Check if `calvinklein-co-uk` already has artifacts. First run → no artifacts → proceed.

**What it does:** ✅ Works correctly on first run. But on re-runs, it finds old artifacts and sets `skip_site=True, skip_product=True, skip_code=True`, jumping straight to `code_tester` with stale code.

---

### 3. setup_workspace (deterministic)
**Should do:** Create `workspace/calvinklein-co-uk/` directory, clean old artifacts on fresh run.

**What it does:** ✅ Works correctly. Cannot clean `downloaded_files/` (permission issue with SeleniumBase lock files).

---

### 4. check_accessibility (deterministic)
**Should do:** Probe `calvinklein.co.uk` through the escalation chain: direct HTTP → Playwright → datacenter proxy → residential proxy. Cache the working method. For CK UK: `uc_chrome_datacenter` or `uc_chrome_none` should succeed.

**What it does:** ✅ Works correctly. Probes all 4 tiers, determines `uc_chrome_datacenter` works. Takes ~2 minutes (4 sequential probes with timeouts). Passes pre-verified probe data to site_analyzer.

**Issue:** Also tries `uc_chrome_residential` and `playwright_residential` unnecessarily. Could stop after finding `uc_chrome_datacenter` works.

---

### 5. site_analyzer (LLM agent, budget 20)
**Should do for CK UK watches:**
- Use pre-verified probe data (already in HumanMessage)
- Detect SFCC platform from HTML structure
- Detect Akamai anti-bot protection
- Confirm `uc_chrome_datacenter` as working method
- Identify product URL pattern from SFCC: `/en-uk/{category}/{slug}/p/{id}`
- Note that search is available at `/search?searchTerm={query}`
- Write `site_analysis.json`
- 5 tool calls: 1 load_skill (sfcc-detection) + 1 probe_page (verify product page) + 1 write_file = ~3-5 calls

**What it actually does:**
- Ignores pre-verified probe data, calls `probe_page` anyway (wasting calls)
- Constructs guessed `.html` URLs that don't exist on SFCC (`/en-gb/mens-clothing/underwear/classic-cotton-stretch-boxer-briefs/K040846.html`) — CK uses `/p/{id}` format, not `.html`
- Each failed probe takes 60-180 seconds (timeout + escalation chain)
- Gets stuck in a loop: probe fails → captcha_detected=True cached → ignores cache → probes another guessed URL → fails again
- Never discovers the actual URL pattern because it can't access a real product page
- When given the search URL as `product_url`, analyzes a listing page and concludes "no JSON-LD, no platform detected, custom SPA"
- 7-12 calls, ~10-20 minutes of wasted probe calls

**Root causes:**
1. No real product URL available — site-analyzer needs a product page but the pipeline didn't provide one
2. LLM guesses `.html` URLs instead of using SFCC `/p/` patterns
3. `captcha_detected=True` cache prevents re-probing the same URLs but doesn't stop the LLM from trying new ones
4. Budget of 20 calls is too high — should be 8-10 with "use pre-verified data" enforcement

---

### 6. _route_after_site_analyzer (deterministic routing)
**Should do for search_term mode:** Route to `navigation_explore` so the pipeline can search the site for watches.

**What it does:** ❌ Routes to `update_tracker_analysis` because `input_mode="search_term"` is not in the routing condition. Navigation is completely bypassed.

**This is the single biggest pipeline bug.**

---

### 7. navigation_explore (deterministic, no LLM calls)
**Should do for CK UK watches:**
- Visit homepage
- Find search form/URL pattern (detects `/search?searchTerm=` input)
- Navigate to `https://www.calvinklein.co.uk/search?searchTerm=watches`
- Parse search results page HTML
- Extract 93 product URLs matching SFCC pattern `/en-uk/{category}/{slug}/p/{id}`
- Detect pagination (page 1/2, "You've viewed 48 of 93 items")
- Write `navigation_findings.json` with:
  - `search_attempted: true`
  - `search_url: /search?searchTerm=watches`
  - `product_links: [93 URLs]`
  - `pagination: {type: "numbered", total_pages: 2}`

**What it actually does:** ❌ Never runs for `search_term` input mode.

**Previous run observations (Job 131, when it did run via `navigation` mode):**
- Found 0 real product links, 3 promo links
- Card selectors didn't match CK UK's SFCC product tiles
- `_looks_like_product_url()` filter applied to promo links (which correctly rejected them)
- The SFCC card selector fixes (Phase 2) were deployed but not tested in a real job

---

### 8. navigation_synthesize (deterministic, no LLM calls)
**Should do:**
- Read `navigation_findings.json`
- Set `discovery_method: search`
- Provide search URL pattern: `/search?searchTerm={criteria}`
- Provide item link selectors for search results page
- Document pagination

**What it actually does:** ❌ Never runs for `search_term` input mode.

**Previous run observations (Job 131):**
- Set `discovery_method: category` instead of `search`
- Used promo page URL pattern instead of product URL pattern

---

### 9. product_analyzer (LLM agent, budget 15)
**Should do for CK UK watches:**
- Open ONE watch product page (e.g. `calvin-klein-classic-watch/p/KJ12345`)
- Detect rich JSON-LD Product data (title, price, brand, images, SKU, availability)
- Map 18+ fields with JSON-LD as primary extraction method
- Include `expectations` blocks for every field
- Write `product_analysis.json`

**What it actually does:**
- Maps 18 fields ✅
- Sets `method: js_dom` instead of `jsonld` ❌ — CK UK has rich JSON-LD but the LLM doesn't use it as primary
- 0 expectations despite mandatory instructions ❌
- 15 calls (budget max) — wastes calls probing pages that are oops/captcha pages

---

### 10. scraper_analyzer (LLM agent, budget 10)
**Should do:** Read site_analysis.json + product_analysis.json, determine strategy (`seleniumbase_uc`, proxy based on connectivity), write scraper_analysis.json. 3 calls.

**What it does:** 12-21 calls across retry cycles. Cycle 1 often sets wrong proxy tier (overrides site_analysis). Correct after cycle 2.

---

### 11. code_writer (LLM agent, budget varies)
**Should do:**
- Read 4 analysis files + template
- Write scraper that:
  1. Phase 1: Navigate to `/search?searchTerm=watches`, extract product URLs from results
  2. Phase 2: Open each product URL with fresh SB() session, extract fields from JSON-LD + DOM
  3. Accept `--xvfb` in argparse
- Write `input_urls.json` with sample watch URLs
- 8-10 calls in 1 cycle

**What it does:**
- `is_product_url()` filter rejects ALL product URLs (checks for `.html` suffix) ❌
- `xvfb=True` hardcoded instead of `args.xvfb` ❌
- Proxy format wrong for SeleniumBase ❌
- Seed URLs have domain typos (hyphens instead of dots) ❌
- 45 calls across 3 retry cycles ❌

---

### 12. code_tester (LLM agent, budget 15)
**Should do:** Run scraper, validate output against product_analysis expectations, write test_report.json. 5 calls.

**What it does:** ✅ Now working well after Phase 1 fix. Both cycles produced correct, actionable reports. 12-26 calls across retry cycles (not its fault — code-writer bugs force retries).

---

### 13. run_execution (deterministic)
**Should do:** Run the generated scraper via browser-service, collect output JSON with product data.

**What it does:** ❌ Never reaches this stage — stuck in retry loop.

---

### 14. cleanup (LLM agent, budget 10)
**Should do:** Move scraper to `scrapers/calvinklein-co-uk/`, write `input_urls.json`, update site tracker.

**What it does:** ❌ Never reached.

---

### 15. nav_skill_review (LLM agent)
**Should do:** Post-scrape learning — compare nav findings against existing skills.

**What it does:** ❌ Never reached (for failed jobs). Was running pre-code-generation before Phase 3 fix. Now correctly placed post-cleanup.

---

## Summary Table

| Agent | Calls (ideal) | Calls (Job 131) | Calls (Job 132) | Calls (Job 134) | Calls (Job 137) | Status |
|-------|--------------|----------------|----------------|----------------|----------------|--------|
| parse_command | 0 | 0 | 0 | 0 | 0 | ✅ |
| check_tracker | 0 | 0 | 0 | 0 | 0 | ✅ |
| setup_workspace | 0 | 0 | 0 | 0 | 0 | ✅ |
| check_accessibility | 0 | 0 | 0 | 0 | 0 | ✅ |
| site_analyzer | 5 | 12 | 12 | 10 | 7 (stuck) | ⚠️ |
| **navigation_explore** | **3** | **0** | **0** | **NEVER RAN** | **NEVER RAN** | **❌** |
| **navigation_synthesize** | **1** | **0** | **0** | **NEVER RAN** | **NEVER RAN** | **❌** |
| product_analyzer | 5 | 15 | 15 | never reached | never reached | ⚠️ |
| scraper_analyzer | 3 | 21 | 62 | never reached | never reached | ⚠️ |
| code_writer | 8 | 45 | 45 | never reached | never reached | ❌ |
| code_tester | 5 | 26 | 26 | never reached | never reached | ✅ |
| run_execution | 0 | never | never | never | never | — |
| cleanup | 3 | never | never | never | never | — |
| nav_skill_review | 0 | 17 | 0 (post-cleanup) | never | never | ✅ |

---

## 7 Root Causes (Ordered by Impact)

### 1. `search_term` input mode skips navigation (CRITICAL)
**File:** `webapp/agents/graph.py:2086`
**Impact:** The entire navigation phase (search, product URL discovery) is bypassed. The pipeline cannot discover products via search.
**Fix:** Add `"search_term"` to the routing condition.

### 2. Site-analyzer invents `.html` URLs for SFCC sites
**Impact:** Burns 7-12 calls probing non-existent URLs. Each probe takes 60-180 seconds. Budget exhausted before analysis is written.
**Fix:** Site-analyzer should first search the site homepage for real product links (or use navigation_explore's findings). The LLM shouldn't guess URL formats — it should discover them.

### 3. No real product URL available when `product_url` is a listing page
**Impact:** Site-analyzer analyzes a search/listing page, concludes "no JSON-LD, no platform detected". Product-analyzer has no watch product to analyze.
**Fix:** Navigation_explore should run first (fix #1), discover product URLs, then pick one for product_analyzer.

### 4. Code-writer `is_product_url()` rejects all valid SFCC URLs
**Impact:** Scraper discovers product URLs but the filter rejects them all. 0 products extracted.
**Fix:** Code-writer should use navigation_analysis URL patterns instead of inventing its own filter.

### 5. Code-writer doesn't follow the template
**Impact:** Regenerates patterns from scratch with new bugs each cycle. `xvfb=True` hardcoded, proxy format wrong.
**Fix:** Stronger template adherence in code-writer prompt.

### 6. Scraper-analyzer retry loop wastes calls on code bugs
**Impact:** Re-analysis doesn't fix code-writer bugs. 21 calls for a correct-after-cycle-2 answer.
**Fix:** After 1 scraper-analyzer cycle, if the issue is a code bug (not an analysis bug), route directly back to code_writer.

### 7. Product-analyzer ignores JSON-LD
**Impact:** Sets fragile `js_dom` extraction method instead of robust `jsonld`. Cascades to code-writer.
**Fix:** Stronger JSON-LD detection instruction in product-analyzer.md.

---

## Recommended Fix Priority

1. **Fix `_route_after_site_analyzer`** (graph.py:2086) — add `"search_term"` → unblocks entire search flow
2. **Site-analyzer URL discovery** — use nav_explore results or homepage product links instead of guessing
3. **Code-writer template adherence** — enforce template patterns, remove `is_product_url()` invention
4. **Retry loop optimization** — skip scraper-analyzer when issue is code-only
5. **Product-analyzer JSON-LD priority** — stronger instructions
