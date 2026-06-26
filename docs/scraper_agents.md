# Scraper Agent Audit: CK UK "Watches" Pipeline

######## debug each agents and fail early if something is unexpeccted 

## What the User Expected

1. Go to calvinklein.co.uk
2. Find the search icon, type "watches"
3. Execute search → site returns ~93 results
4. Open at least one product from the results
5. Map what information is available (title, price, images, etc.)
6. Write code that: searches for "watches", extracts product data from each result
7. Test the code against what was observed in step 5

## What Each Agent Should Do vs What It Did

---

### 1. site-analyzer (12 calls) — Should be 5

**Should do:** Detect SFCC + Akamai, confirm `uc_chrome_none` works, identify product URL pattern `/en-uk/{cat}/{slug}/p/{id}`, note search URL pattern `/search?q=`.

**Actually did:** ✅ Detected SFCC, Akamai, seleniumbase_uc. ❌ Set `method_that_worked: uc_chrome_datacenter` (wrong tier). ❌ `product_discovery.method: product_url_pattern` — no mention of search. Wasted 7 extra calls re-probing connectivity already done by `check_accessibility`.

---

### 2. navigation-explore (deterministic, 0 LLM calls) — Should be 3

**Should do:** Visit homepage, navigate to `/search?q=watches`, extract 93 product URLs like `/en-uk/menswear/watches/calvin-klein-classic-watch-12345/p/KJ12345`, capture pagination.

**Actually did:** ✅ Navigated to `/search?q=watches`. ❌ Found **0 real product links**. Instead extracted 3 promo links (bestsellers, pride collection, special collection). `search_form: None`. Root cause: card selectors don't match CK UK's SFCC product tiles. Phase 2 fixes deployed but not yet tested (job ran old code).

---

### 3. navigation-synthesize (deterministic, 0 LLM calls) — Should be 1

**Should do:** Set `discovery_method: search`, provide search URL pattern `/search?q={criteria}`, provide link selectors for search results, document pagination.

**Actually did:** ❌ Set `discovery_method: category`. ❌ `item_links.url_pattern: /mens-special-collection-{id}` — a promo page pattern, not a product URL. Should be `/en-uk/{cat}/{slug}/p/{id}`. No pagination.

---

### 4. product-analyzer (15 calls) — Should be 5

**Should do:** Open ONE watch product page, map 18+ fields with expectations, identify JSON-LD as primary extraction method, note `uc_chrome_none` connectivity.

**Actually did:** ✅ Mapped 18 fields. ❌ Set `method: js_dom` for title/price instead of `jsonld` — CK UK has rich JSON-LD Product data, this cascades to fragile code. ❌ 0 expectations despite mandatory instructions. 10 extra calls.

---

### 5. scraper-analyzer (21 calls) — Should be 3

**Should do:** Read site_analysis.json + product_analysis.json, determine strategy `seleniumbase_uc` + proxy `none`, write scraper_analysis.json. 3 calls total.

**Actually did:** ❌ Cycle 1: set `proxy: datacenter` (overrode site_analysis). ✅ Cycle 2: corrected to `proxy: none`. ✅ Identified 7 real code-writer bugs. But 21 calls for what should be 3 — the retry loop forces re-analysis when the problem is in code-writer, not analysis.

---

### 6. code-writer (45 calls across 3 cycles) — Should be 8-10 in 1 cycle

**Should do:** Read 4 analysis files + template, write a scraper that (1) navigates to search/category, (2) extracts product URLs, (3) opens each with fresh SB() session, (4) extracts from JSON-LD + DOM, (5) accepts `--xvfb` in argparse. Write input_urls.json.

**Actually did:**
- **Cycle 1 (28 calls):** Generated 1065-line scraper with: `xvfb=True` hardcoded (no argparse), `is_product_url()` rejects ALL 48 links (checks for `.html` suffix), wrong proxy format, domain typo in seed URLs
- **Cycle 2 (9 calls):** Fixed xvfb partially, some proxy issues
- **Cycle 3 (8 calls):** Fixed more issues but is_product_url still broken

**Core bug:** `is_product_url()` checks for `.html` suffix — CK product URLs are clean slugs like `/en-uk/watches/calvin-klein-classic-watch/p/KJ12345`. LLM regenerates this broken filter every cycle instead of following the template.

---

### 7. code-tester (26 calls across 2 cycles) — Should be 5 in 1 cycle

**Should do:** Run scraper, validate output against product_analysis expectations, write test_report.json.

**Actually did:** ✅ Both cycles produced correct, actionable reports. Found xvfb argparse crash (cycle 1) and is_product_url filter bug (cycle 2). The agent itself works well — the issues are all code-writer bugs.

---

### 8. nav-skill-review (17 calls) — Should be 0 in critical path

**Should do:** Post-scrape learning only. Run after cleanup.

**Actually did:** Ran before product-analyzer, consuming 17 calls. Pure waste. Phase 3 fix moves it post-cleanup.

---

## Summary

| Agent | Calls (ideal) | Calls (actual) | Verdict |
|-------|--------------|---------------|---------|
| site-analyzer | 5 | 12 | ⚠️ wrong proxy tier |
| nav-explore | 3 | 0 | ❌ 0 real product links |
| nav-synthesize | 1 | 0 | ❌ wrong discovery method |
| product-analyzer | 5 | 15 | ⚠️ no JSON-LD, no expectations |
| scraper-analyzer | 3 | 21 | ⚠️→✅ correct after cycle 2 |
| code-writer | 8 | 45 | ❌ fundamental URL filter bug |
| code-tester | 5 | 26 | ✅ working correctly |
| nav-skill-review | 0 | 17 | ❌ in critical path (now fixed) |

**Total ideal: ~30 calls. Actual: 136 calls. 78% wasted.**

## 5 Root Causes

1. **nav-explore selectors don't match CK UK** → 0 product links found → code-writer has nothing to work with
2. **code-writer `is_product_url()` too strict** → rejects all valid links even when nav-explore does find them
3. **code-writer doesn't follow the template** → regenerates patterns from scratch with new bugs each cycle
4. **scraper-analyzer in retry loop does re-analysis work that doesn't fix code bugs** → 21 calls for a correct-after-cycle-2 answer
5. **product-analyzer ignores JSON-LD** → cascades fragile `js_dom` extraction method to code-writer
