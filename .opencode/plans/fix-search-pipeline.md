# Plan: Fix Search Pipeline for CK UK Watches

## Problem Statement

When a user submits a job with `search_criteria="watches"` for `calvinklein.co.uk`, the pipeline fails to:
1. Search the site for "watches"
2. Discover the 93 product URLs
3. Generate working scraper code

## Root Cause

`_route_after_site_analyzer()` in `graph.py:2086` only routes to `navigation_explore` for `input_mode` values `"navigation"` and `"list_page"`. The `search_term` input mode falls through to `update_tracker_analysis`, completely bypassing the navigation phase (search, product URL discovery, pagination).

## Fixes (5 items)

### Fix 1: Route `search_term` to navigation_explore
**File:** `webapp/agents/graph.py`
- Line 2063: Add `"search_term"` to `route_from_setup_workspace` condition
- Line 2086: Add `"search_term"` to `_route_after_site_analyzer` condition

### Fix 2: Add `searchTerm` to `_build_search_urls` param detection
**File:** `webapp/agents/nodes/navigate_explore.py`
- Line 1077: Add `"searchterm"` to the parameter name list in the `search_url_hints` substitution

### Fix 3: Add `searchTerm` to search URL hint detection regex
**File:** `webapp/agents/nodes/navigate_explore.py`
- Line 232: Add `/\?searchterm=/i` to `searchLinkRegexes`

### Fix 4: Auto-detect `input_mode` from `search_criteria`
**File:** `webapp/agents/nodes/parse_command.py`
- When `search_criteria` is non-empty and `input_mode` is `url_list`, auto-set `input_mode = "search_term"`

### Fix 5: Ensure `search_url_hints` includes `searchTerm` links
**File:** `webapp/agents/nodes/navigate_explore.py`
- Line 228-242: Remove the condition `if (!result.search_form || !result.search_form.action)` — always collect `search_url_hints` so the form action can use them as backup

---

## Expected Pipeline Flow for CK UK "Watches"

### Step 1: parse_command (deterministic)
- Input: `url=https://www.calvinklein.co.uk`, `search_criteria="watches"`
- Output: `site_slug="calvinklein-co-uk"`, `input_mode="search_term"`

### Step 2: check_tracker (deterministic)
- No existing artifacts → proceed

### Step 3: setup_workspace (deterministic)
- Create `workspace/calvinklein-co-uk/`

### Step 4: check_accessibility (deterministic)
- Probe `calvinklein.co.uk` through escalation chain
- Result: `uc_chrome_datacenter` works (or `uc_chrome_none`)
- Passes pre-verified probe data to site_analyzer

### Step 5: site_analyzer (LLM, ~5 calls)
- Reads pre-verified probe data from accessibility check
- Loads `sfcc-detection` skill
- Probes ONE page (homepage) via `probe_page` to verify
- Detects: SFCC platform, Akamai protection, `uc_chrome_datacenter` method
- Identifies search URL pattern from homepage: `/search?searchTerm={query}`
- Writes `site_analysis.json`
- **Does NOT guess `.html` URLs** — uses probe data from accessibility

### Step 6: navigation_explore (deterministic, 0 LLM calls)
- Takes `_do_explore_via_http` path (UC Chrome site → uses `probe_html`)
- **STEP 1:** Fetches homepage HTML via `probe_html`
  - Parses with BeautifulSoup
  - Detects search form: `<input name="searchTerm">` with action `/search`
  - Extracts 50 category links (Watches & Jewellery, Menswear, etc.)
- **STEP 2:** Builds search URL from form: `/search?searchTerm=watches`
  - Also tries fallback patterns: `/search?q=watches`, `/search?search=watches`
- **STEP 3:** Fetches `/search?searchTerm=watches` via `probe_html`
  - Parses rendered HTML with BeautifulSoup
  - Detects SFCC product tiles using `[ref=productTile]`, `[data-tileid]`, etc.
  - Extracts product links matching pattern `/en-uk/{category}/{slug}/p/{id}`
  - Applies `_looks_like_product_url()` filter
  - **Expected result: 48-93 product links** (page 1 of 2)
- **STEP 4:** Detects pagination: "You've viewed 48 of 93 items", "01/02"
- **STEP 5:** Tries page 2 URL to get remaining product links
- Writes `navigation_findings.json`
- **Total: ~3-5 probe_html calls (homepage + search page 1 + maybe page 2)**

### Step 7: navigation_synthesize (deterministic, 0 LLM calls)
- Reads `navigation_findings.json`
- Sets `discovery_method: search`
- Provides search URL template: `/search?searchTerm={criteria}`
- Provides item link selectors: SFCC product tile selectors
- Documents pagination: 2 pages
- Writes `navigation_analysis.json`

### Step 8: product_analyzer (LLM, ~5 calls)
- Picks ONE product URL from navigation findings (a watch)
- Opens it via `probe_html` (UC Chrome site)
- Extracts JSON-LD Product data (title, price, brand, images, SKU, availability)
- Maps 18+ fields with JSON-LD as primary extraction method
- Writes expectations for each field
- Writes `product_analysis.json`
- **Does NOT probe via Playwright (UC Chrome site — domain guard blocks it)**

### Step 9: normalize_fields → validate_coverage (deterministic)
- Normalizes field names to standard schema
- Checks coverage: all required fields mapped

### Step 10: scraper_analyzer (LLM, ~3 calls)
- Reads `site_analysis.json` and `product_analysis.json`
- Reads `navigation_analysis.json` (new — for search URL and link selectors)
- Strategy: `seleniumbase_uc`
- Proxy: matches `site_analysis.connectivity.method_that_worked` (no override)
- Extraction: `jsonld_primary` with `dom_fallback`
- Search: `/search?searchTerm={criteria}`, pagination via URL pattern
- Writes `scraper_analysis.json`

### Step 11: code_writer (LLM, ~8-10 calls, 1 cycle)
- Reads 4 analysis files + `undetected_chromedriver_scraper.py` template
- Writes scraper that:
  1. **Phase 1 (Discovery):** Opens `/search?searchTerm=watches` via SB()
     - Extracts product URLs using SFCC tile selectors from `scraper_analysis`
     - Handles pagination (page 2 via "next page" or URL pattern)
  2. **Phase 2 (Extraction):** For each product URL:
     - Opens fresh `SB(xvfb=args.xvfb)` session
     - Extracts JSON-LD Product data (primary)
     - Falls back to DOM selectors (secondary)
     - Writes structured output fields
  3. **CLI:** Accepts `--xvfb` flag in argparse (runner injects it)
  4. **URL filter:** Uses `navigation_analysis` URL patterns, NOT custom `is_product_url()`
- Writes `scraper_draft.py` + `input_urls.json` (with 93 watch URLs)

### Step 12: code_tester (LLM, ~5 calls, 1 cycle)
- Reads `scraper_draft.py`
- Reads `product_analysis.json` expectations
- Runs scraper via `run_scraper` (3-5 sample URLs)
- Validates output against expectations
- Writes `test_report.json` with PASS/FAIL

### Step 13: pre_execution_approval (deterministic)
- If test_report shows PASS → approve execution
- If FAIL → route to code_writer for fix (max 1 retry)

### Step 14: run_execution (deterministic)
- Runs full scraper on all 93 watch product URLs
- Collects output JSON with product data

### Step 15: cleanup (LLM, ~3 calls)
- Moves `scraper_draft.py` → `scrapers/calvinklein-co-uk/scraper.py`
- Writes `input_urls.json` with all 93 watch URLs
- Updates `data/ecom-websites.json` tracker

### Step 16: nav_skill_review (LLM, ~5 calls, post-scrape)
- Compares nav findings against existing skills
- Proposes new patterns if any (e.g., SFCC `searchTerm` param)

### Step 17: skill_learner (LLM, ~5 calls)
- Examines completed scrape artifacts
- Proposes skill updates

---

## Expected Totals

| Agent | Ideal Calls | Notes |
|-------|------------|-------|
| parse_command | 0 | deterministic |
| check_tracker | 0 | deterministic |
| setup_workspace | 0 | deterministic |
| check_accessibility | 0 | deterministic (4 probes, no LLM) |
| site_analyzer | 5 | 1 load_skill + 1 probe + 1 write |
| navigation_explore | 0 | deterministic (3-5 probe_html calls, no LLM) |
| navigation_synthesize | 0 | deterministic |
| product_analyzer | 5 | 1 probe_html + 1 load_skill + 1 write |
| scraper_analyzer | 3 | 2 read_file + 1 write_file |
| code_writer | 8 | 5 read_file + 1 write_file + 1 write_file(input_urls) + 1 load_skill |
| code_tester | 5 | 2 read_file + 1 run_scraper + 1 read_file(output) + 1 write_file |
| cleanup | 3 | 2 read_file + 1 write_file |
| nav_skill_review | 5 | post-scrape learning |
| skill_learner | 5 | post-scrape learning |
| **TOTAL LLM calls** | **~44** | **down from 136-177** |

## Total tool calls including deterministic nodes: ~49-54
