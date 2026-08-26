# Codegen Contract Audit — why job 10's priceline scraper regressed vs job 9

**Scope:** the writer's instructions (L1), the tester/gates (L2), determinism/model (L3).
**Method:** static read of prompt builders, templates, gates, and the on-disk priceline artifacts. No test runs.

**Provenance caveat (read first):** Railway job 10's scraper/output are not on this machine. What IS on disk:
`workspace/priceline-com-au/` — the local repro generation (scraper_draft.py, navigation_analysis.json,
product_analysis.json, test_report.json, outputs) from the job-9-era repro runs (commits `194e696`, `349eb3f`),
and `scrapers/priceline-com-au/` — job-9-family outputs (5/50/8-item sample/limit runs; the 3,616-item prod
output lives on the Railway File Master). Findings below are labeled **[verified-on-disk]** where the artifact
is in front of us, **[inferred-for-job-10]** where the same code path explains the prod symptom.

The audited draft is a genuine api-family generation for this exact site, so every contract hole it exposes
applies to job 10's generation too.

---

## LAYER 1 — What the writer is TOLD vs what it needs

### L1-1 GAP — src_url semantics: one unenforced line, and the template contradicts it

- `.opencode/agents/code-writer.md` — the entire system prompt contains **zero** occurrences of `src_url`
  (grep rc=1). Not in the Output Contract (line 126-128), not in Field Formatting, nowhere.
- The only guidance in the whole writer input is one sentence in the task message,
  `webapp/agents/subagents.py:3098-3100`:
  > `- **src_url**: Set to the URL where the item was discovered. If input comes from input_urls.json, src_url equals the item URL. For navigation scrapers, src_url is the listing/search page URL.`
- **The api template hardcodes the opposite.** `templates/api_scraper.py:280`:
  ```python
  src_url = f"{API_BASE_URL}{API_PRODUCTS_ENDPOINT}"
  ```
  i.e. src_url = the **backend API endpoint**, not the listing page. The draft copied this verbatim
  (`workspace/priceline-com-au/scraper_draft.py:386`), producing
  `src_url = "https://api.priceline.com.au/occ/v2/priceline/products/search"` on every row
  (verified in every on-disk output — 1 distinct src_url across all rows).
- The template block in the system prompt is framed as **"use VERBATIM — do not rewrite"**
  (`subagents.py:655-663`, injected by `_build_agent(template_code=...)`), so when the one-line message rule
  and the verbatim template conflict, the template wins. **The LLM did not deviate here — the template
  bakes the wrong semantics.**
- Consequence: no downstream consumer can tell which listing/category an item came from, and "src_url=self"
  (job 10's symptom) is the same class — an unbound free variable the writer fills with whatever is in scope.

### L1-2 GAP — discovery completeness for the api family: the contract exists for browsers, is silently dropped for API jobs

- For browser/nav jobs the message is emphatic and correct: `subagents.py:2536-2553`
  ("**DISCOVERY — paginate EVERY page (CRITICAL for full extraction)** … keep paginating until a page returns
  NO new product URLs") and `subagents.py:3041-3055` ("**Full Extraction (MANDATORY — no item caps)** …
  Paginate until exhaustion OR until the API's reported total").
- **But for api jobs the entire nav block is replaced.** `subagents.py:2762`:
  ```python
  navigation_section = api_section
  ```
  (de-bloat, `subagents.py:2756-2761`: "emit ONLY the API data-model section"). The api_section
  (`subagents.py:2657-2750`) covers only *per-endpoint* pagination: "increment `{page_param}` starting at 1 …
  Stop when `len(items) >= response total`" (`:2674-2677`). It says nothing about **catalog enumeration** —
  which queries/facets/categories to iterate so the union covers the catalog.
- **The category taxonomy was available and then thrown away.** `navigation_analysis.json` for this site holds
  20 category URLs in `item_links.url_examples` (`/c/vitamins-supplements`, `/c/bone-joint-health`, …) and the
  listing URL `/c/gifts`. That content is rendered by the two-phase block (`subagents.py:2590-2612`) — the very
  block replaced at `:2762`. The api_section never mentions `item_links` at all.
- Result [verified-on-disk]: the writer **invented** an enumeration from nothing —
  `scraper_draft.py:63`:
  ```python
  DEFAULT_QUERIES = ["perfume", "skincare", "makeup", "hair care", "vitamins", "health", "baby", "fragrance"]
  ```
  Eight free-text guesses, with an inline comment admitting the discovered endpoint needed them
  ("OCC search only returns products for free-text queries (`:relevance` etc. return 0)").
  Whether a given generation guesses well (job 9: 3,616) or badly (job 10: 68) is pure sampling luck.
- **The injected API endpoint was garbage.** `navigation_analysis.api_endpoint.url` =
  `https://global.ketchcdn.com/web/v3/config/wesfarmers_health/priceline/production/default/en/config.json` —
  a consent-management config, `count: null`, `items_per_page: 5`. The api_section tells the writer to
  "READ it" and use it (`subagents.py:2665-2667`). The writer correctly ignored it and recovered the real OCC
  API from `product_analysis` prose — an ungoverned, unrepeatable save.

### L1-3 GAP — the field-map summarizer drops every API path/endpoint it needs to convey

`_summarize_product_analysis` renders each field as `[method] sel=… fallback=… js=…`
(`subagents.py:222-229`), reading only `selector|path`, `jsonld_fallback|css_fallback|fallback`, and
`js_extraction`. Priceline's `product_analysis.json` fields carry the extraction truth in keys that are
**not in that chain**:

| field in product_analysis.json | keys present | what the writer saw |
|---|---|---|
| `current_price` | `method: api_interception`, `api_path: price.formattedValue`, `api_endpoint: occ/v2/priceline/products/{code}?fields=FULL` | `- **current_price** [api_interception]` (bare) |
| `previous_price` | `api_path: price.wasPrice.formattedValue`, `api_endpoint: …` | bare |
| `description` | `css sel` + `api_fallback: "summary in OCC API response"` | css selector only — fallback dropped |
| `ratings` | `css sel` + `api_fallback: "averageRating and numberOfReviews…"` | css selector only — fallback dropped |

The structured `occ_api` block (`base_url`, `product_endpoint`, `price_data_in_api.current_price_path`,
`previous_price_path`, `interception_pattern`) is **not** in the verbatim-passthrough list either
(`subagents.py:244-251` passes only `page_structure`, `extraction_methods`, `jsonld_extraction`,
`mechanism_reassessment`, `site_analysis_review`, `variants`). The OCC base URL reached the writer only
inside a free-text note (`extraction_methods.notes`).

### L1-4 GAP — "what to do with an item that has no data" is stale for custom schemas

- `code-writer.md:127-128` (Output Contract): "drop items missing `title` + at least one core field."
  Priceline's requested schema is `{previous_price, current_price, description, ratings}` — there is no
  `title` in the field map at all, so the rule is uninterpretable as written.
- The deterministic backstop `_patch_scraper_output_filter` (`graph.py:213-298`, target-fields branch
  `:240-247`) replaces it with keep-if-any-of-target-fields — correct, but the LLM also writes its **own**
  filter first. The on-disk draft's hand-written filter (`scraper_draft.py:454-463`) is:
  ```python
  def _has_content(item):
      return bool(item.get("current_price") or item.get("previous_price")
                  or item.get("description") or item.get("ratings") or item.get("remarks"))
  ```
  `or item.get("remarks")` lets a remarks-only row (e.g. `"Soft 404: product not found"`,
  `scraper_draft.py:326-333`) through. The patch then drops it locally — but any row carrying a truthy
  stub of a requested field (e.g. `ratings: "0.00 (0)"` from `build_ratings`, `:181-192`, emitted whenever
  `averageRating` exists even with 0 reviews) survives **both** filters while being blank to a human.
  [inferred-for-job-10] This is the leading mechanism for job 10's 30 surviving blank rows: price/description
  absent (keys omitted entirely, `:228-235`), `ratings` a zero-stub.

### L1-5 GAP — no prior-good-scraper reference; the system prompt forbids it

- `build_code_writer_message` (`subagents.py:2145-3202`) contains **no** reference to `scrapers/{slug}/`,
  the promoted `scraper.py`, or any prior generation (grep over the function body: zero hits).
- `code-writer.md:34` says outright: **"Do not read reference scrapers."**
- Meanwhile the infrastructure to do better exists and is unused as a floor: `_archive_existing_scraper`
  (`graph.py:963-985`) archives the previous production scraper to the File Master before promotion, and
  `check_tracker._compute_rescrape_skip_flags` (`check_tracker.py:82-107`) can skip regeneration entirely
  when fields/nav are unchanged. Once regeneration *does* fire (fields or nav differ — precisely job 9 → 10),
  the writer starts from zero with no knowledge that a 3,616-item scraper for this exact site exists.
  Job 9's scraper was on disk/File Master at job-10 generation time; the writer was never told and was
  actively barred from looking.

---

## LAYER 2 — Why job 10's scraper PASSED testing

### L2-1 GATE-MISS — the api template emits no discovery telemetry, so every coverage gate is structurally inert

- `grep -c discovery_coverage`: `templates/api_scraper.py` = **0**, vs `http_navigation_scraper.py` = 5 and
  `navigation_scraper.py` = 4. The api template's metadata block (`templates/api_scraper.py:344-350`) carries
  only `scraping_duration_seconds`, `failed_products`, `rate_limit_delay` — no `discovery_coverage`, no
  `discovered_urls` count, no stop reason. Verified in every on-disk priceline output.
- Chain of consequences, each a gate turning off:
  - `_attach_discovery_coverage` (`graph.py:519-583`, called at `:3457`) reads
    `metadata.discovery_coverage` → absent → returns the report untouched → the classifier never sees it.
  - `_discovery_coverage_failure` (`route_after_testing.py:79-98`) is Tier-1-only
    (`_COVERAGE_FAIL_STOP_REASONS = {"navigate_error", "dedup_flat"}`, `:76`) and reads only that block →
    always `None` for api jobs → the field-PASS downgrade at `route_after_testing.py:517-528` can never fire.
  - The tester prompt's own instruction "Assert `metadata.discovered_urls` > 1 page worth"
    (`subagents.py:3427`) points at a key the api family never writes — the assertion is unmakeable.

### L2-2 GATE-MISS — the deterministic Phase-1 probe skips the entire api family

`_probe_phase1_discovery` (`graph.py:3301-3327`) requires the draft to declare `--discover-only`
(`:3319-3321`: `if accepted is not None and "discover-only" not in accepted: return False, None`). The api
family's contract flag set is `API_NAV_FLAGS = URL_LIST_FLAGS + ("--fresh-discovery",)`
(`agents/constants.py:74`) — no `--discover-only`. The on-disk priceline draft declares none
(grep count 0). **So the only deterministic discovery validator in the pipeline is a no-op for every
internal_api/api job.** The LLM tester's Phase-1 run is the sole check, and it is capped
(`--limit 50`, `subagents.py:3246/3276-3277`) and judged by narrative, not numbers.

### L2-3 GATE-MISS — Tier 2/3 coverage is dead code; the count that would have caught this was captured as prose

- `navigate_synthesize.py:584`: "(coverage_target ensure was removed with Phase 2.)" — nothing stamps
  `coverage_target` anymore. Priceline's `navigation_analysis.json` has no `coverage_target` key (verified).
  The tester therefore always renders the "Tier 1 only" arm (`subagents.py:3355-3363`): "A clean short page
  on a small catalog is a PASS."
- The site **told us the total** and we threw it away: `navigation_analysis.item_links.signals.reason` =
  *"Page shows **97 results** for Gifts with 10 result cards"*. That number is free-text; it is never
  structured into `total_items`. Had it been, the Tier-3 arm (`subagents.py:3326-3341`) instructs the tester
  to flag "tens vs thousands" as HIGH severity with `target: "strategy"`.
- **Direct answer to the audit question:** would ANY gate have caught "68 items from a listing with 314 on
  page 1"? **No.** Walking every gate with that state: tester PASS + confidence ≥ 0.85 →
  phase1_discovery = true (LLM self-reported) → `_cov_reason` = None (no block) → PASS →
  `field_confirmation`. Ground-truth override needs only **3** real items (`route_after_testing.py:196-208`,
  `min_count=3`; the override at `:542-552`). The probe is skipped (L2-2). Tier 3 is unwired (this item).
  F9 at execution passes (L2-4). Zero gates fire.

### L2-4 GATE-MISS — the F9 quality-gate math confirmed lets 44% blank rows through

`run_execution.py:1101-1176` (`_extraction_quality_gate`), thresholds at `:1154` and `:1164`:

```python
if processed >= 5 and bad / processed >= 0.8:   # FAIL
if processed >= 5 and bad / processed >= 0.5:   # WARN only
```

With job 10's shape — 68 rows, 30 blank: `good = 38` (rows carrying ≥1 of
`{current_price, previous_price, description, ratings}`, the threaded `target_fields` from `194e696`),
`core-less = 30`, `failed_products = 0` → `bad/processed = 30/68 = 44%`. **Below even the 50% warning.**
Not blocked, not logged. The gate was designed for *collapse* (prod 330: 95% fail; 335: 87%; 337: 100%
core-less) and is blind to *dilution*. Confirmed exactly as the audit brief hypothesized.

Two structural notes that make it worse:
- An empty record has no requested fields → it does count core-less (good), but a row whose only populated
  field is a zero-stub `ratings` string counts as **good** — same predicate, opposite verdict
  (`_substantive_item_count`, `run_execution.py:1053-1096`, `any(item.get(f) for f in use_fields)`).
- F9 has no dead-status exclusion (unlike `_is_dead_product`, `route_after_testing.py:170-177`), but that
  cuts the other way here — 404 rows would count bad and still not reach 80%.

### L2-5 GATE-MISS — nothing downstream inspects per-record emptiness

- `store_job_listings` (`graph.py:3846-4031`) — the only per-record ingest — hard-guards to job content
  types at `:3864` (`if "job" not in page_type.lower(): return`). Priceline is `product` → never runs.
- `code_tester`'s own decision logic (`code-tester.md:150-163`) fails only on
  `critical field WRONG_VALUE/MISSING AND field_coverage < 80%`. On a 5-item `--sample` where all 5 are
  healthy, coverage is 100% and the blanks (which live in the *undiscovered* tail, not the sample) are
  invisible by construction. The on-disk `test_report.json` is exactly this: 5/5 successful, every required
  field 100%, PASS @ 0.92, `phases_tested: {phase1_discovery: true, phase2_extraction: true}`.
- The tester's Phase-1 verdict is narrative, not measured: `feedback_for_writer` = *"Phase 1 discovery found
  5000+ products across multiple search queries with full pagination"* — an LLM summary of scraper logs with
  no number checked against anything (and nothing to check against; see L2-1).

---

## LAYER 3 — Determinism / model

### L3-1 DRIFT — code_writer runs at temperature 0.4 with no seed and no determinism mode

- `subagents.py:48`: `"code-writer": 0.4` (mirrors `code-writer.md:4` frontmatter).
- `subagents.py:36-42` admits the state of affairs in-line: *"z.ai does not reliably honor seed, so this
  narrows but does not guarantee determinism"*; `LLM_CODEGEN_DETERMINISTIC` defaults **False**
  (`settings.py:237`) and, even when true, only drops code-writer/product-analyzer to 0.0 (`:62-64`).
  `AGENT_TEMP_CODE_WRITER` env override exists (`:58-61`) but is unset by default.
- With L1-2 unfixed, temperature is amplified into output semantics: `DEFAULT_QUERIES` is a free parameter
  the model invents each run. temp=0.4 over "enumerate a catalog with no stated method" is precisely the
  3,616-vs-68 spread. Same inputs, different enumeration invention.

### L3-2 DRIFT — model identity is configurable, reroutable, and unrecorded — job 9 vs job 10 drift is possible and unverifiable

- code_writer is the **only** agent with a model override: `AGENT_MODEL_SETTINGS = {"code-writer":
  "CODE_WRITER_MODEL"}` (`subagents.py:79-81`), resolved lazily in `_build_agent` (`:695-700`).
- `CODE_WRITER_MODEL` defaults `glm-5-turbo` (`settings.py:188`); a `litellm/`-prefixed value reroutes to
  `LITELLM_BASE_URL = https://llm.johnjf.xyz/v1` (`settings.py:194-199`), `LITELLM_ENABLED` default True,
  client-side prefix strip + forced streaming (`llm.py:96-118, 298-345`). Per project memory, the litellm
  path was shipped specifically for code_writer and **streaming is required** (proxy 504s non-stream >60s).
- So the two Aug-26 runs could plausibly have used different models via any of: a Railway env change to
  `CODE_WRITER_MODEL`, a kill-switch flip (`litellm/` prefix present/absent), or proxy-side routing changes
  behind `llm.johnjf.xyz`.
- **It cannot be settled after the fact, because model identity is persisted nowhere.** `SessionLog`
  (`webapp/scraper/models.py:336-354`) stores `role`/`agent`/`content` only — no model, no
  `response_metadata`. The breaker keys on the configured string (`llm.py:221`, `:237`) but writes no queryable
  log. Nothing on the job row records which model wrote the draft.
- **What WOULD confirm or kill the hypothesis:**
  1. the litellm proxy's own access logs at `llm.johnjf.xyz` for both jobs' code_writer windows (model
     string + token counts + timestamps) — decisive if the proxy served both;
  2. Railway's env/config history for the `django` and `celery-worker` services around each job's start
     (variable-history or a redeploy between them);
  3. if neither exists going forward: capture `response_metadata.model_name` / `self.model_name` in
     `_persist_agent_logs` (`graph.py:4052+`) so this class of question is one query instead of a hypothesis.
- Independent of model identity, **temperature alone (L3-1) is sufficient to explain the spread** given the
  unconstrained enumeration contract — treat model drift as a contributing, unconfirmed factor.

---

## Highest-leverage fix per layer

**L1 — Give the api family a catalog-enumeration + src_url contract, in the message and the template.**
One structured block the writer cannot improvise around: `catalog_enumeration` (the category facet values or
the `item_links.url_examples` categories — currently discarded at `subagents.py:2762` — plus a
site-reported total when captured), and a real src_url rule for api jobs ("src_url = the site
listing/category URL the item was discovered under — never the backend API endpoint"), backed by fixing
`templates/api_scraper.py:280` to take the listing URL instead of `API_BASE_URL+ENDPOINT`. Also widen the
field-map renderer (`subagents.py:222-229`) to include `api_path`/`api_endpoint`/`api_fallback` and pass
`occ_api`-style blocks through verbatim — today the API extraction truth is dropped and the writer recovers
it from prose by luck. Optionally: on regeneration for a site with an archived scraper, inject it as a
reference floor (the archive already exists at `graph.py:963`; the prompt bar is at `code-writer.md:34`).

**L2 — Make the api family emit and be judged by discovery telemetry.**
Add `metadata.discovery_coverage` (`stop_reason`, `found`, `expected_total`) + `discovered_urls` count to
`templates/api_scraper.py`, extend the Phase-1 probe to accept `--fresh-discovery` as the api-family trigger
(it currently no-ops on them, `graph.py:3319-3321`), re-wire `coverage_target` stamping from the
already-captured listing count ("97 results" prose → `total_items`), and add a dilution arm to F9 — an
absolute core-less floor (e.g. fail when `coreless >= 10 and bad/processed >= 0.3`) so 30/68 blank fails
instead of passing below the warn line.

**L3 — Pin the run and record the model.**
Set `AGENT_TEMP_CODE_WRITER=0` (or flip `LLM_CODEGEN_DETERMINISTIC`) for codegen on api/nav jobs, and
persist the resolved model per agent invocation into `SessionLog` from `response_metadata`. Determinism
can't be diagnosed — or claimed — while the model string is env-dependent and unpersisted.

---

## Verdict summary

| Layer | Label | One line |
|---|---|---|
| L1 | GAP | The writer is told how to paginate one endpoint but never how to cover a catalog, never what src_url means for api jobs, and is barred from the prior scraper that already solved both. |
| L2 | GATE-MISS | The api family emits no discovery telemetry and no gate reads row-level emptiness below an 80% collapse ratio — a 44%-blank, 68-of-314 run passes every check silently. |
| L3 | DRIFT | temp=0.4 with no seed over an unconstrained design decision is sufficient for the spread; model identity is env-reroutable and recorded nowhere, so the job-9-vs-10 model question is currently unanswerable. |
