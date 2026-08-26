# Codegen Regression Analysis — priceline.com.au (Job 9 vs Job 10)

**Analyst:** codegen-quality pass, read-only (code + artifacts, no runs)
**Date:** 2026-08-26
**Site:** priceline.com.au — SAP Commerce Cloud (Spartacus), OCC API at `api.priceline.com.au/occ/v2/priceline`

| | Job 9 (good) | Job 10 (degraded) |
|---|---|---|
| Items | 3,616 | 68 (listing had 314+ on page 1) |
| `src_url` | `/c/gifts` listing (correct) | every record's own URL (wrong) |
| Empty records | 0 | 30 (blank delivery pseudo-SKUs, `remarks=''`) |
| Data path | OCC REST API, HTTP | HTML listing `cx-state` parse, HTTP |

---

## 1. The three code divergences

### 1.1 Discovery

**Job 9 — `/tmp/p9scraper.py` (24,266 bytes), OCC `/products/search` pagination**
- `API_BASE_URL = "https://api.priceline.com.au/occ/v2/priceline"` (L71), `API_PRODUCTS_ENDPOINT = "/products/search"` (L72)
- `PAGE_SIZE = 100`, `MAX_DISCOVER_PAGES = 40` (L83-84), broad-query sweep `API_SEARCH_QUERIES = ["a","e","i","o","u","the","gift","care","makeup","fragrance"]` (L97)
- `_discover_all_products_via_api()` (L260-327): params `query/pageSize/currentPage/fields=FULL` (L277-282), dedupe by product `code` (L292-296), stop on short page / `totalResults` reached (L314-322), `--limit` short-circuit at 3× limit (L309-312)
- Extraction reuses the Phase-1 payload (`fields=FULL`, no refetch) — L465-472; seed runs use `ThreadPoolExecutor` + thread-local Sessions (L359-386)

**Job 10 — `/tmp/scr10.py` (16,428 bytes), HTML `cx-state` parse with hand-rolled `?page=N`**
- `LISTING_URL = "https://www.priceline.com.au/c/gifts"` (L43), `PAGE_SIZE = 24` — a **hardcoded guess** (L44)
- `discover_product_urls()` (L162-195): fetch listing page, `page_url = f"{LISTING_URL}?page={page}"` (L169), parse `<script id="ng-state">` JSON via `extract_cx_state()` (L117-126), collect *any* dict with string `code`+`name` via recursive `_find_products()` (L129-139)
- **Broke early at L192:** `if page_new == 0 or len(products) < PAGE_SIZE: break` — the site serves ~10 cards per scroll batch (the nav artifact itself said `items_per_page: 10`), so `len(products) < 24` trips on the first page; and `infinite_scroll_tall` means `?page=` doesn't exist server-side (Spartacus paginates client-side), so `page_new == 0` would trip next anyway. 314+ available → 68 kept.
- The enforced import `from src.discovery import discover_item_urls, config_for_load_more` (L28) is **present but never called** — `_enforce_discovery_import` only checks the import line, and `discovery_config.json` on disk (`type: infinite_scroll_tall, page_param_name: null`) was ignored. The hand-rolled loop is exactly what the "HARD RULE: use `_get_next_page_url` UNMODIFIED" prompt text forbids — but that text never reached this writer (see §3).

### 1.2 `src_url`

**Job 9** — threaded properly through the record builder:
- `src_url = os.environ.get("SCRAPER_LISTING_URL", "").strip() or f"{API_BASE_URL}{API_PRODUCTS_ENDPOINT}"` (L425) — execution sets `SCRAPER_LISTING_URL=/c/gifts` for nav jobs (`run_execution.py:419`), so every record gets the listing page; verified in preserved outputs (`scrapers/priceline-com-au/output_*.json`: 100% of `src_url` = one value).
- Passed as a parameter: `transform_api_product(api_product, index, src_url)` (L197, `"src_url": src_url` L215), `scrape_products_concurrent(urls, src_url)` (L359), `_soft404_item(url, src_url, msg)` (L246).

**Job 10** — set to the record's own URL:
- `"src_url": url` in **both** record shapes: the empty-variant record (L241) and the populated record (L266). `extract_product(html, url)` (L211) is a 2-arg signature with no `src_url` parameter at all — the plumbing was never built. Job 9's record builder inherited the parameter from `templates/api_scraper.py:133` (`transform_api_product(api_product, index, src_url)`); job 10's writer wrote a bespoke builder and dropped it.

### 1.3 Empty / no-data products

**Job 9** — two layers:
- Soft-404 marker with `status_code: 404` + explanatory `remarks` (`_soft404_item`, L246-253); used when no product code in URL (L338) and for failed extraction (L382) so output count matches input.
- Output filter: hand-written drop of records with none of `previous_price/current_price/description/ratings` (L495-508) **plus** the pipeline-injected copy (L511-520, marker `_OUTPUT_FILTER_APPLIED`). Both operate on the defined `OUTPUT_KEY = "products"` (L69). Result: 0 empty records.

**Job 10** — blank rows shipped:
- `variant is None` → emits a record with every field `""`, `status_code: 200`, `remarks: "Soft 404: product not found"` (L233-245). The 30 delivery pseudo-SKUs hit this path (they exist in `cx-state` as `code`+`name` dicts — `_find_products` swept them in — but have no `variants.value`), and a separate code path emits `remarks: ""` blanks.
- The injected filter (L371-381) references `OUTPUT_KEY` — **which scr10.py never defines** (grep: no `OUTPUT_KEY =` anywhere in the file). `output[OUTPUT_KEY]` raises `NameError`, swallowed by the surrounding `except Exception: pass` (L380-381) → **the empty-record gate is a silent no-op.** This is why 30 blanks reached the output.

---

## 2. What the writer was actually told (`build_code_writer_message`, `webapp/agents/subagents.py:2145-3202`)

What it gets: site URL/slug, `scraper_analysis` strategy + proxy, content-type context + requested schema, navigation section, template hint, retry context (`_summarize_test_report`, L1952, only when `state["test_report"]` exists — first generation has none), and `_summarize_product_analysis` (L166-255) of `product_analysis`. It also gets `run_scraper` in its tool set (`webapp/agents/tools/__init__.py:31-41`) so it *can* empirically verify endpoints.

What it never gets:
- **The prior scraper.** Nothing in the builder reads `scrapers/{slug}/scraper.py`. Job 10's writer had no idea job 9 had already extracted 3,616 items from this exact site. `check_tracker`'s skip path needs `workspace/{slug}/scraper_draft.py` to exist, but `cleanup` moves it to `scrapers/` — so a re-submission is always a from-scratch generation.
- **`navigation_findings.json` content beyond URL merging** (L2453-2478 merges `product_links` into `item_links.urls` only — for job 10 those merged links were *category* URLs, see below).
- **Any prior output stats** (item counts, src_url samples) that would make a 68-vs-3,616 regression visible.

### The api_endpoint poisoning (job 10's nav artifact, `/tmp/an_navigation_analysis.json`)

```json
"data_source": "api",
"api_endpoint": {"url": "https://global.ketchcdn.com/web/v3/config/wesfarmers_health/priceline/production/default/en/config.json",
                 "count": null, "items_per_page": 5},
"pagination": {"type": "infinite_scroll_tall", "items_per_page": 10}
```

- The ketch URL is a **consent-management boot config**, not the OCC products API. `verify_api` (`experimental/nav_traversal/traversal.py:472-502`) accepts it because the config JSON contains a list of ≥5 dicts (`items_per_page: 5`) — the select-option-key rejection (`_SELECT_OPTION_KEYS`, L479-498) doesn't match its keys. `count: null` survives because the scorer only *prefers* a count (`has_count`, L1100) when there are multiple candidates; with one candidate it wins by default.
- `graph.py:2762-2786` then gates the `internal_api` override on `items_per_page > 0 and count != 0` — `5 > 0` and `null != 0` both pass → `scraper_analysis.strategy = "internal_api"` pointing at ketch (see `workspace/priceline-com-au/scraper_analysis.json` on disk).
- Consequence in the prompt: `api_endpoint.url` truthy → the builder emits `api_section` ("CRITICAL — Backend JSON search API discovered (PREFERRED…)") and then **`navigation_section = api_section` (L2762) discards the entire two-phase block** — including the pagination `nav_lines` (L2506-2553, where `infinite_scroll_tall` and the `_get_next_page_url` HARD RULE live) and the Phase-1 "start from the listing URL" contract (L2567-2613). The template hint is swapped to `templates/api_scraper.py` (L2752-2755).
- **Net effect: job 10's writer received (a) an "use this API" mandate for a garbage endpoint, (b) an api_scraper template that doesn't fit the cx-state reality, and (c) zero pagination guidance.** It rejected the garbage API (correctly), ignored the template, and improvised `?page=N` — the exact improvisation the deleted pagination block existed to prevent. Job 9's writer faced the same poisoned artifact (the workspace `navigation_analysis.json` has the identical ketch `api_endpoint`) and *also* rejected it — but went and found the real OCC endpoint itself.

### The `src_url` rule is real but unreachable in this configuration

The builder does say it — `subagents.py:3098-3100`: *"src_url: Set to the URL where the item was discovered. If input comes from input_urls.json, src_url equals the item URL. For navigation scrapers, src_url is the listing/search page URL."* But it is generic prose ~200 lines deep in the message, and `api_section` (job 10's dominant section) never mentions `src_url` at all. The current `.opencode/agents/code-writer.md` prompt never mentions `src_url` either (only the retired `code-writer-v1.md:110,136` did). Neither the code_tester prompt nor `validate_coverage` checks `src_url` uniformity/semantics — `normalize_fields.py` merely passes the key through. So nothing downstream catches "every record cites itself".

---

## 3. The malformed-JSON finding (job 10's `product_analysis`)

`/tmp/an_product_analysis.json` (12,115 bytes) **fails to parse**: `Expecting ',' delimiter: line 36 column 202 (char 2858)` — unescaped single quotes inside a `js_extraction` JS snippet (`t.match(/\{\"cx-state\"[\s\S]*$/` embedded in prose). This is the artifact the writer consumed.

Failure chain (all silent):
1. `_fix_json_artifact` (`graph.py:122-141`) only repairs *bad backslash escapes* (`re.sub(r'(?<=[^\\])\\(?!["\\/bfnrtu])'...)`); unescaped quotes are untouched. Worse, it **writes the mangled content first, then validates** — `f.write(fixed)` at L137 precedes `json.loads(fixed)` at L138, so on failure the file is left mangled on disk and the routine just logs a warning and continues.
2. `_read_json_artifact` (`graph.py`, `except Exception: return {}`) → `state["product_analysis"] = {}`.
3. `_summarize_product_analysis` (L166) returns `""` for an empty field map → **the writer got no Field Extraction Map at all**, and also lost the "DO NOT read_file the analysis JSONs" note (it's appended inside `pa_summary`, L3172-3178) — leaving the writer free (and likely compelled) to `read_file` the corrupt file as raw text.
4. `normalize_fields.py:73-88` and `validate_coverage.py:44` read the same file and equally no-op. Every quality gate downstream of product_analyzer silently degraded; the job proceeded because the *user's* requested schema (`previous_price, current_price, description, ratings`) is carried on the job row, not in the artifact.

What the corrupt file *says* (as text): `extraction_methods.primary = "embedded_json"`, state path `cx-state.product.details.entities.{code}.variants.value`, and — decisive — an `api_assessment` reading *"The site_analyzer identified a Ketch CDN config URL as the 'backend API'. This is INCORRECT… There is no separate product API to call."*

`/tmp/scr10.py` implements that text **verbatim**: `extract_cx_state()` (L117-126) reads `<script id="ng-state">`, `_get_variant_value()` (L202-208) walks `state["product"]["details"]["entities"][code]["variants"]["value"]`. The corrupt artifact didn't just lose information — it carried an active falsehood ("no separate product API") that the writer obeyed, because job 10's writer never ran the empirical probe (`run_scraper`) that would have disproven it in one call.

For contrast, the *valid* `workspace/priceline-com-au/product_analysis.json` (13,240 bytes — a different, later generation) says the opposite: `fields.current_price.method = "api_interception"`, `api_endpoint = "occ/v2/priceline/products/{code}?fields=FULL"`, `api_path = "price.formattedValue"` — and the scrapers generated against artifact generations like that one (job 9's, and the current workspace draft) all hit the OCC API. The scraper is a pure function of which `product_analysis` text reached it.

One more artifact poison in job 10's nav analysis: `item_links.url_examples` lists 20 **category** URLs (`/c/vitamins-supplements`, `/c/bone-joint-health`, …) — not one product URL. So the "CRITICAL: the navigation_analysis has the exact URL and selectors that found N products" claim (L2610-2612) was false, and `_find_products`'s anything-with-code-and-name sweep is the writer's workaround for having no real product-link pattern. The "97 results" count in the workspace variant of the artifact vs "10 result cards" in `/tmp`'s also shows the explorer under-observed the listing.

---

## 4. Verdict: input-driven, not non-deterministic

**Both jobs were the same nominal job** (same site, same `/c/gifts` listing, `search_term`-style input, same deterministic `scraper_analyzer` output — `strategy: internal_api`, ketch endpoint, `infinite_scroll_tall`). The divergence is fully explained by **different upstream artifacts**, principally `product_analysis`:

| Generation | `product_analysis` content | Scraper produced |
|---|---|---|
| Job 9 | OCC API interception (`price.formattedValue`, `occ/v2/.../products/{code}`) | OCC API scraper, 3,616 items, `src_url` threaded, 0 empties |
| Job 10 | corrupt JSON; `embedded_json` / cx-state; *"no separate product API to call"* | cx-state HTML scraper, 68 items, `src_url=url`, 30 blanks |
| Current workspace (post-10) | valid JSON; `dom_css` + `occ_api` interception section | OCC API scraper again (all outputs `src_url` = OCC search) |

Three *different* `product_analysis` generations for one URL = the upstream `product_analyzer` (LLM, temp 0.2) is the non-deterministic stage; `code_writer` then **deterministically implements whatever it is handed**. There is a secondary LLM-judgment amplifier — job 9's writer *disobeyed* its artifact's "CORS blocks direct fetch, use Playwright interception" note (realized CORS doesn't apply server-side) while job 10's writer *obeyed* its artifact's falsehood — but with a corrupt field map, an api-section built on a consent URL, no pagination guidance, and no prior art, job 10's writer was improvising in the dark. **Classification: forced-by-artifact, with the artifact variance itself produced by upstream non-determinism plus a broken-artifact gate.** Not "same inputs, worse luck."

---

## 5. Top 3 highest-leverage fixes (ranked)

### Fix 1 — Regression guard: prior scraper + output floor as generation context and a test gate
The single most detectable signal (3,616 → 68) was invisible because the writer never sees prior art and no gate compares counts.
- **Where (input):** `check_tracker.py` / `build_code_writer_message` (subagents.py) — when `scrapers/{slug}/scraper.py` + an output file exist, inject a `### PRIOR ART` block: prior item count, `src_url` sample, prior data path (OCC API endpoint), with the contract "match or beat the prior item count; reuse the prior endpoint unless the artifact disproves it."
- **Where (gate):** `build_code_tester_message` (subagents.py:3205+) / `code-tester.md` decision logic — add a **count-regression check**: if Phase-1 discovery yields < 25% of the prior run's discovered URL count, FAIL with severity high ("discovery incomplete vs prior run"). The Tier-3 coverage machinery already exists (`nav_analysis.coverage_target`, `cov_total`, ratio gate) — seed `coverage_target.total_items` from the prior run's output when no site-reported total exists.
- Also cheap and structural: an `src_url` uniformity check in code_tester — for nav-mode jobs, N distinct `src_url` values across N records where each equals its own `url` is a WRONG_VALUE.

### Fix 2 — Close the two silent no-op gates (`OUTPUT_KEY` filter + ketch-style false-positive APIs)
Both are small deterministic patches with no LLM involvement.
- **Where:** `webapp/agents/graph.py:213-298` (`_patch_scraper_output_filter`) — the injected `filter_code` references `OUTPUT_KEY` (L262-264) without checking the draft defines it. `api_scraper.py` and `requests_scraper.py` don't define it either, so any api-family draft gets a filter that `NameError`s into `except Exception: pass` — the exact mechanism that shipped job 10's 30 blank rows. Fix: detect a missing `OUTPUT_KEY` definition in the draft and inject the literal output key (from content-type config) instead of the symbol; or assert-and-log loudly when the marker line raises.
- **Where:** `experimental/nav_traversal/traversal.py:472-502` (`verify_api`) and/or `graph.py:2762-2786` (override gate) — reject candidates with `count: null` **and** no pagination-looking query params, and/or down-rank third-party registrable domains (ketchcdn ≠ priceline.com.au; `_sanitize_nav_domains` F17 already has the `_registrable` helper to reuse). At minimum, require `count` to be a non-null int for the `internal_api` override — the lw.com Coveo fix already established `count != 0`; tighten to `count is not None and count > 0`, and never let a cross-domain API endpoint assert `data_source: "api"` for the site. This kills the "PREFERRED — use this API" mandate that both jobs' writers had to overrule.

### Fix 3 — Hard gate on artifact JSON validity (stop generating from `{}`)
- **Where:** `graph.py` `_fix_json_artifact` (L122-141) — validate *before* writing (don't leave a mangled file on disk); when the artifact is still invalid after the escape repair, treat it as `missing_artifact` (re-run product_analyzer / surface the interrupt), not as "continue with `{}`".
- **Where:** `_run_budgeted_agent` `on_success` for product_analyzer (graph.py:1787-1838) — `if analysis == {}` after `output_exists`, don't route to `normalize_fields`; take the missing-artifact path.
- **Where:** `_summarize_product_analysis` (subagents.py:166) — when `fields` is empty but the file on disk is non-empty and unparseable, emit an explicit "PRODUCT ANALYSIS UNREADABLE — do not guess extraction paths; verify with run_scraper" note instead of `""` (an empty summary reads as "no fields mapped" rather than "input corrupt"). Bonus: this also prevents the writer from `read_file`-ing the corrupt file (the anti-read_file note lives inside `pa_summary`).

### Structural gaps catalogued (context for the fixes above)

1. No prior-scraper / prior-output context ever reaches the writer (nothing reads `scrapers/{slug}/`).
2. `navigation_section = api_section` (subagents.py:2762) **replaces** the two-phase + pagination guidance wholesale whenever any `api_endpoint.url` exists — so a false-positive API endpoint deletes the pagination HARD RULE and the listing-URL contract instead of being appended to them.
3. `src_url` semantics are one prose bullet (L3098-3100); `api_section` and the current `code-writer.md` prompt never state it; no gate validates it.
4. `_enforce_discovery_import` enforces the import but not the call — scr10 imports `discover_item_urls` and ignores it; `discovery_config.json` (`infinite_scroll_tall`) went unused.
5. The pagination the artifact *did* capture (`infinite_scroll_tall`, `items_per_page: 10`) never reached job 10's writer (deleted per gap 2), and nothing cross-checks a draft's `PAGE_SIZE` constant against it (scr10 hardcoded 24 vs the artifact's 10 → first-page break).
6. Corrupt artifacts cascade silently (`{}` through normalize/validate/summary) — no gate anywhere distinguishes "no fields" from "unreadable file".
7. `item_links.url_examples` were category URLs; nothing validates that "N products found" examples are actually item pages before the writer is told to trust them.
