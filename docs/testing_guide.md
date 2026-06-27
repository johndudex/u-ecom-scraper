# Agent Testing Guide

Individual agent testing via the **Agent Playground** (`/agent-playground/`). Each agent can be tested in isolation. Test agents in pipeline order since each depends on upstream artifacts.

## How to Use the Playground

1. Go to `/agent-playground/` in the Django admin UI
2. Select agent name, enter URL, optional search criteria, and prompt
3. Submit and poll for results
4. Check output artifacts in `workspace/{slug}/`

**Important:** Do NOT clean workspace between sequential agent tests. Each agent reads files written by the previous one.

## Pipeline Flow

```
check_tracker → setup_workspace → check_accessibility → site_analyzer
  → navigation_explore → navigation_synthesize → product_analyzer
  → normalize_fields → validate_coverage → scraper_analyzer → code_writer
  → code_tester → cleanup
```

## Test Sequence

Test agents 1 through 7 in order. Each produces files the next one needs.

---

### 1. `site_analyzer`

**What it does:** Probes the site to detect platform, anti-bot protection, product discovery method, and which scraping mechanism works.

| Field | Value |
|-------|-------|
| **Reads** | State: `url`, `site_slug`. Uses Playwright MCP + probe tools. |
| **Writes** | File: `workspace/{slug}/site_analysis.json`. State: `site_analysis`, `probe_result` |
| **Deterministic?** | No (LLM agent, budget=10) |

**Playground setup:**
- Agent: `site_analyzer`
- URL: `https://www.calvinklein.co.uk`
- Search criteria: *(leave empty)*
- Prompt: `Analyze the site structure of https://www.calvinklein.co.uk. Detect the platform, scraping mechanism, anti-bot protection, and product discovery method. Write your findings to workspace/calvinklein-co-uk/site_analysis.json.`

**Pass criteria:**
- `site_analysis.json` exists and is valid JSON
- `connectivity.method_that_worked` is set (e.g. `"uc_chrome_none"`, `"uc_chrome"`, `"playwright"`)
- `platform` is detected
- `anti_bot` has relevant entries
- No crash, budget not exhausted

**Common failures:**
- Budget exhausted (10 iterations used probing guessed URLs) — reduce site complexity or fix the budget
- Guards blocking all probe calls — check `probe_result` in tool context
- Stale browser connection — restart browser-service container

---

### 2. `navigation_explore`

**What it does:** Navigates to the site, finds search form, executes search, extracts product links and category links from search results pages.

| Field | Value |
|-------|-------|
| **Reads** | State: `url`, `site_slug`, `search_criteria`, `input_mode`, `probe_result`. Files: `site_analysis.json` (optional). Uses Playwright MCP. |
| **Writes** | File: `workspace/{slug}/navigation_findings.json`. State: `navigation_findings` |
| **Deterministic?** | Yes (no LLM, pure procedural code) |

**Playground setup:**
- Agent: `navigation_explore`
- URL: `https://www.calvinklein.co.uk`
- Search criteria: `watches`
- Prompt: *(any text — deterministic node ignores it)*

**Pass criteria:**
- `navigation_findings.json` exists and is valid JSON
- `listing_page.product_links` has >= 3 entries
- Each product link has `href` with a real product URL path
- `search_attempted` = true
- `method` = `"playwright"` (preferred over `"probe_html"`)
- No `errors` containing "oops" or session gating indicators

**Common failures:**
- 0 product links — search form not found or form.submit() didn't navigate
- Session gating — `method: "probe_html"` means Playwright MCP failed or was blocked
- Double locale prefix in search URLs — locale detection bug
- `http_links` undefined — session gating merge bug (crashes, falls back to empty HTTP findings)

---

### 3. `navigation_synthesize`

**What it does:** Reads raw navigation findings and site analysis, produces a structured navigation analysis with search strategy, selectors, URL patterns.

| Field | Value |
|-------|-------|
| **Reads** | Files: `workspace/{slug}/navigation_findings.json`, `site_analysis.json`. State: `url`, `site_slug`, `search_criteria` |
| **Writes** | File: `workspace/{slug}/navigation_analysis.json`. State: `navigation_analysis` |
| **Deterministic?** | Partially (fallback path is deterministic, LLM path is not) |

**Playground setup:**
- Agent: `navigation_synthesize`
- URL: `https://www.calvinklein.co.uk`
- Search criteria: `watches`
- Prompt: `Read workspace/calvinklein-co-uk/navigation_findings.json and site_analysis.json, then write the structured navigation_analysis.json. Choose the best discovery method and fill in all fields.`

**Pass criteria:**
- `navigation_analysis.json` exists and is valid JSON
- `discovery_method` = `"search"` (for search_term mode)
- `search_strategy.url_template` has the correct parameter name (e.g. `/search?searchTerm={term}`)
- `search_strategy.product_card_selector` is non-empty
- `product_url_examples` has >= 3 real product URLs
- `pagination.type` is set (`infinite_scroll`, `click_next`, or `url_based`)
- `site_info.url` matches the target site

**Common failures:**
- Empty `discovery_method` or `null` — fallback path didn't run correctly
- Wrong URL template parameter — copied from homepage hints instead of actual search behavior
- `listing_page` crash if `navigation_findings` is missing required keys
- Missing `product_url_examples` — upstream findings had 0 product links

---

### 4. `product_analyzer`

**What it does:** Fetches individual product pages, maps all extractable fields (title, price, availability, etc.) with exact CSS selectors.

| Field | Value |
|-------|-------|
| **Reads** | Files: `workspace/{slug}/navigation_analysis.json`, `site_analysis.json`. State: `url`, `site_slug`. Uses `probe_page` tool to fetch product pages. |
| **Writes** | File: `workspace/{slug}/product_analysis.json`. State: `product_analysis` |
| **Deterministic?** | No (LLM agent, budget=15) |

**Playground setup:**
- Agent: `product_analyzer`
- URL: `https://www.calvinklein.co.uk`
- Search criteria: `watches`
- Prompt: `Analyze product pages from the navigation_analysis.json at workspace/calvinklein-co-uk/. Focus on watches. Map all extractable fields with exact CSS selectors, JSON-LD paths, and meta tag fallbacks. Write to workspace/calvinklein-co-uk/product_analysis.json.`

**Pass criteria:**
- `product_analysis.json` exists and is valid JSON
- `fields.title.selector` is non-empty
- `fields.price.selector` is non-empty
- `fields.availability.selector` is non-empty
- `fields.currency.selector` or `fields.currency.default_value` is set
- `fields.original_price.selector` exists (may be empty if never on sale)
- Each field has a realistic `example_value` (e.g. price = "$250.00")
- `tested_urls` lists the product pages actually analyzed

**Common failures:**
- Guards blocking `probe_page` calls to off-target URLs (e.g. `/mens-clothing` instead of `/stainless-steel-watch-wm25200520000`)
- Probing random non-product pages — `navigation_analysis` had no `product_url_examples`
- Budget exhausted (15 iterations, each page takes multiple tool calls)
- Bad JSON output (backslash escapes) — `_fix_json_artifact` in graph.py should handle this in pipeline mode

---

### 5. `scraper_analyzer`

**What it does:** Reads all upstream analysis files and determines the scraping strategy. No live site access — purely file-based.

| Field | Value |
|-------|-------|
| **Reads** | Files: `workspace/{slug}/site_analysis.json`, `navigation_analysis.json`, `product_analysis.json` |
| **Writes** | File: `workspace/{slug}/scraper_analysis.json`. State: `scraper_analysis` |
| **Deterministic?** | No (LLM agent, budget=10) |

**Playground setup:**
- Agent: `scraper_analyzer`
- URL: `https://www.calvinklein.co.uk`
- Search criteria: `watches`
- Prompt: `Read workspace/calvinklein-co-uk/site_analysis.json, navigation_analysis.json, and product_analysis.json. Determine the scraping strategy. Write to workspace/calvinklein-co-uk/scraper_analysis.json.`

**Pass criteria:**
- `scraper_analysis.json` exists and is valid JSON
- `strategy` matches site requirements (e.g. `"seleniumbase_uc"` for CK UK, `"playwright"` for standard sites, `"requests"` for SSR sites)
- `warmup_required` = true for Akamai/session-gated sites
- `fields` dict includes all fields from `product_analysis`
- For navigation jobs: `discovery_phase` has `url_template` and `product_card_selector`
- `scraping_method` is set

**Common failures:**
- Generic output when upstream analyses are empty or malformed
- Wrong strategy selected (e.g. `"requests"` for a JS-rendered site)
- Missing `warmup_required` for session-gated sites
- `discovery_phase` missing for navigation jobs (code_writer won't build Phase 1)

---

### 6. `code_writer`

**What it does:** Reads scraper_analysis and navigation_analysis, generates a complete Python scraper script.

| Field | Value |
|-------|-------|
| **Reads** | Files: `workspace/{slug}/scraper_analysis.json`, `navigation_analysis.json`, `product_analysis.json`. Templates from `templates/`. |
| **Writes** | File: `workspace/{slug}/scraper_draft.py`. State: `scraper_code` |
| **Deterministic?** | No (LLM agent, budget=15) |

**Playground setup:**
- Agent: `code_writer`
- URL: `https://www.calvinklein.co.uk`
- Search criteria: `watches`
- Prompt: `Read workspace/calvinklein-co-uk/scraper_analysis.json and navigation_analysis.json. Write a two-phase scraper to workspace/calvinklein-co-uk/scraper_draft.py. Phase 1 searches for "watches" and discovers product URLs. Phase 2 extracts title, price, availability from each product page.`

**Pass criteria:**
- `scraper_draft.py` exists and is valid Python (no syntax errors)
- Has `--sample` CLI flag (runs 5 products)
- Has `--urls` CLI flag for explicit URL list mode
- Phase 1: uses search URL template from `navigation_analysis` to discover product URLs
- Phase 2: iterates product URLs, extracts fields using selectors from `product_analysis`
- Outputs JSON matching the expected output schema (title, price, availability, etc.)
- Uses logging, rate limiting, error handling
- Imports are correct (no missing modules)

**Common failures:**
- Writes URL-list scraper instead of two-phase (ignores `navigation_analysis`)
- Hardcodes product URLs instead of using search discovery
- Missing imports or wrong module names
- Doesn't use selectors from `product_analysis` (guesses them)
- No `--sample` flag
- Output format doesn't match expected schema

---

### 7. `code_tester`

**What it does:** Runs the scraper with `--sample` (5 products), validates extracted fields against product_analysis expectations.

| Field | Value |
|-------|-------|
| **Reads** | Files: `workspace/{slug}/scraper_draft.py`, `product_analysis.json` |
| **Writes** | File: `workspace/{slug}/test_report.json`. State: `test_report` |
| **Deterministic?** | No (LLM agent, budget=15) |

**Playground setup:**
- Agent: `code_tester`
- URL: `https://www.calvinklein.co.uk`
- Search criteria: `watches`
- Prompt: `Test the scraper at workspace/calvinklein-co-uk/scraper_draft.py with --sample. Validate each extracted field against product_analysis.json. Write report to workspace/calvinklein-co-uk/test_report.json.`

**Pass criteria:**
- `test_report.json` exists and is valid JSON
- `confidence` >= 0.8
- `tested_fields` shows non-empty values for title, price, availability
- `issues` list is specific (e.g. "price selector returns empty on 2/5 products")
- `recommendation` is `"accept"` or gives actionable fix instructions

**Common failures:**
- Scraper execution times out — execution budget too low
- Tester re-probes live pages instead of comparing against `product_analysis.json`
- Vague issues ("some fields missing") instead of specific per-field validation
- `confidence` too high for clearly wrong results

---

## Pre-Test Checklist

Before running agent tests, verify:

1. **browser-service** container is running: `docker compose ps browser-service`
2. **celery-worker** is running and has latest code: `docker compose restart celery-worker`
3. **Workspace is clean** for the target slug (only before agent #1):
   ```bash
   docker compose exec django rm -rf /app/workspace/calvinklein-co-uk/*.json
   ```
4. **Playwright MCP** is accessible from celery-worker
5. **Site tracker** entry exists (for pipeline runs, not needed for playground)

## Quick Diagnostic Commands

```bash
# Check last navigate_explore output
docker compose exec django python3 -c "
import json
with open('workspace/calvinklein-co-uk/navigation_findings.json') as f:
    d = json.load(f)
print('Method:', d.get('method'))
print('Products:', len(d.get('listing_page',{}).get('product_links',[])))
print('Categories:', len(d.get('homepage_nav',{}).get('category_links',[])))
print('Errors:', d.get('errors',[]))
"

# Check all workspace artifacts
docker compose exec django ls -la /app/workspace/calvinklein-co-uk/

# Check celery worker logs for an agent
docker compose logs celery-worker --since 10m | grep "navigate_explore"

# Check if job is still running
docker compose exec django python3 -c "
from django.conf import settings
import django; django.setup()
from scraper.models import ScrapeJob
j = ScrapeJob.objects.last()
print(f'Job {j.id}: {j.status} — {j.url}')
"
```
