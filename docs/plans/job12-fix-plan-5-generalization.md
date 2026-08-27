# Job 12 fix plan — Planner 5/6 — GENERALIZATION: design for the unseen site

> Lens: the fix must work on the 22-site regression set **and** on sites nobody has
> seen yet. Yesterday's ketchcdn word-boundary fix was defeated by useinsider within
> one day. That is not bad luck — it is the *defining property* of URL heuristics:
> a blocklist is a game you always lose, because the adversary (the set of
> non-data endpoints on the web) is unbounded and our test set is finite.
>
> **Thesis: stop enumerating URLs. Enumerate the adversary's *shapes*, then require
> the endpoint to produce POSITIVE evidence that it is the item source.**

Everything below is grounded in reproduced evidence, not inference. Two of the poison
payloads were re-fetched live during planning and their real shapes are recorded here.

---

## 0. New facts established during planning (extend the brief)

These are verified findings that the context brief does not contain. Planners and
critics should treat them as additional evidence.

| # | Fact | Where verified |
|---|------|----------------|
| F1 | Job 12's on-disk `api_endpoint` was **ketchcdn**, not useinsider. Two different poison endpoints hit the same job/run family. The poison class is at least 2 wide, not 1. | `/tmp/job12_forensics/artifacts/pl_navigation_analysis.json` |
| F2 | ketchcdn's `config.json` **does** contain a 5-element array of dicts (`.purposes`, keys `code, name, description, legalBasisCode, …`). That is why `verify_api` scored `items_per_page=5` and passed the gate. | live GET 2026-08-27, HTTP 200, `content-type: application/json`, 157 KB |
| F3 | ketch's purpose records carry `name`, `code`, `id`, `description`. **A naive "has a title/name-ish key" content check PASSES this poison.** Identity-key presence is necessary but nowhere near sufficient. | same |
| F4 | useinsider `https://pricelineau.api.useinsider.com/api/info/824.24` returns `content-type: application/javascript` (not JSON), body `{"campId":824,"frameless":true,"content":"<div …6447 chars of HTML>"}`. Its largest dict-array has **1** element (a currency object `[{name:'Australian Dollar', symbol:'$', …}]`). | live GET 2026-08-27, HTTP 200, 30 KB |
| F5 | useinsider's URL contains a genuine `/api/` path segment → today's `url_looks_like_data_api` returns **True**. No URL rule can reject it without also rejecting every real `/api/` endpoint. Confirms the brief. | `experimental/nav_traversal/traversal.py:937-969` + F4 |
| F6 | `webapp/agents/graph.py:2393-2398` **discards `sample_keys`** when persisting `api_endpoint` into `navigation_analysis`. The one piece of response evidence we already had is dropped at the state boundary. | graph.py |
| F7 | `_httpx_fetch` returns `{ok, status, final_url, text}` and **throws away `content-type`**, which is on the response object. Recording it is a one-line change. | `traversal.py:837-850` |
| F8 | There are **TWO independent `api_endpoint` producers**, and only one is gated. Path A: `traversal.verify_api` (fetches, checks array-of-dicts) → `{url, count, items_per_page, sample_keys}` → gated by `_derive_strategy`. Path B: `navigate_explore` XHR capture → `navigate_synthesize._best_api_endpoint` (`webapp/agents/nodes/navigate_synthesize.py:128-192`), which is **pure URL regex scoring with no fetch at all** (`job|search|listing…` +3, `PageNumber|page=` +3, `?` +2, `/api/|/v\d+/` +1, telemetry −10) → `{url, base, method, query_params, pagination_param, …}`. Path B is a second, currently-ungated poison route into `code_writer` via `subagents.py:1452`. | navigate_synthesize.py, subagents.py:1443-1479 |
| F9 | `_fetch_api_sample()` (`subagents.py:1111`) **already performs an HTTP GET** of the api_endpoint on the product_analyzer path. The network cost of content verification is already being paid — it is simply not validated. | subagents.py |
| F10 | Job 12's `product_analysis.json` — the artifact P3 calls a truncated salvage — **contains the correct diagnosis in prose**: *"The site_analyzer identified a Ketch CDN config URL as the 'backend API'. This is INCORRECT — that is a consent management boot endpoint."* plus `corrections_count: 2`. The correct answer was already in the state, deterministically readable, and the gate overrode it. | `pl_product_analysis.json` |
| F11 | Job 12's real listing carried **zero product links** — `item_links.url_examples` is 20 *category* links (`/c/vitamins-supplements`, …). The DOM ground truth that must anchor any join is the **card label set**, not `url_examples`. | `pl_navigation_analysis.json` |
| F12 | `_PAGE_STATE_JS` computes the winning sibling-group but returns **only its count** (`results_items`), not its member labels/hrefs. Capturing those is free — the loop already has them in hand. | `traversal.py:1204-1290` |
| F13 | P4 is exactly as briefed and additionally: the *semantic* reason for an upper bound ("don't touch post-fix data") is not actually required, because the re-derivation is idempotent — the fixed parser produces the same answer for post-fix rows. The upper bound can be dropped safely, not just widened. | recompute_date_reliability.py |
| F14 | Both live poison payloads are killed by **content-shape vetoes with zero URL rules**: ketch has 38 top-level keys (config envelope); useinsider has a 6,447-char top-level HTML string (widget payload). | reproduced above |
| F15 | **A third poison endpoint fired `internal_api`: zquiet.** `https://c.heatmap.com/2097/zquiet.com-collections-shop-all-products.json`, `count: null, items_per_page: 1`, `data_source: "api"` → `strategy: "internal_api"`. Its *filename* is `zquiet.com-collections-shop-all-products.json` — **the URL looks maximally like a product-collection feed.** Any URL rule wants to accept this one. Only response shape (`items_per_page: 1`) separates it. | `shared-data/scrapers/zquiet-com/analysis/navigation_analysis.json:33-37`, `scraper_analysis.json:16-20` |
| F16 | **amnhealthcare's shipped strategy is `http_requests`, not `internal_api`.** Its `api_endpoint` is an LLM-shaped dict (`{url, method, base_params, notes}`) with **no `items_per_page`**, so `isinstance(_api_items, int)` fails and the override gate never fired. amn therefore does **not** depend on the P2 gate — which substantially de-risks this plan's changes to it. Its count lives elsewhere (`pagination.total_count_field: "jobCount"`, `total_count_example: 6953`). | `scrapers/amnhealthcare-com/analysis/navigation_analysis.json`, `scraper_analysis.json` |
| F17 | **`sample_keys` is computed by `verify_api` and then never inspected by anything** — and `graph.py:2393-2398` drops it before persisting. Only one artifact in the whole repo retains a full descriptor: Sidley, whose `sample_keys` are `["text","value","count"]` — a dropdown/facet shape that the existing `_SELECT_OPTION_KEYS` guard **failed to reject** because `count` is not in that set. That is a live bug in the existing shape guard: any response that reports a count can never be a subset of a key set that omits `count`. | `workspace/sidley-com/navigation_analysis.json:10-25`, `traversal.py:480-499` |
| F18 | There are **more consumers** of `api_endpoint` than the strategy gate: `subagents.py:2657` emits `api_section` ("CRITICAL — Backend JSON search API discovered (PREFERRED…)") on **URL truthiness alone**, and `subagents.py:2762` sets `navigation_section = api_section`, **replacing the whole pagination block**. This is the mechanism behind the brief's "deleted the pagination guidance". | `subagents.py:2657, 2762` |
| F19 | `src/job_fields.py:156-220` already contains a **field-alias resolver** (`map_jobs`) with exactly the candidate-key table the spec needs: `title: [title, jobTitle, positionTitle, name, headline, job_title, $compose:employmentTypeText|expertiseText|professionText]`, `company: […, facilityName, divisionCompany.companyName]`, `salary: [$salrange:payRate.minPayRate|…]`. It must be **reused, not re-invented** — and it exposes the decisive counter-example: aya's records contain **no `title` key at all**; their identity is `expertiseText` / `professionText` / `facilityName`. | `src/job_fields.py:156-220`, `scrapers/ayahealthcare-com/analysis/product_analysis.json` |
| F20 | The useinsider poison **leaked into delivered output**: `shared-data/scrapers/priceline-com-au/output_2026-08-26_223642.json:13` carries `"src_url": "https://pricelineau.api.useinsider.com/api/info/824.24"`. Poison endpoints are not only a strategy problem, they reach customer data as the item's source URL. | output file |
| F21 | lw.com has **no artifact on disk** (`shared-data/scrapers/lw-com/` analysis/ and jobs/ are empty) and the workspace log shows `POST https://www.lw.com/coveo/rest/search returned 403`. The Coveo good-endpoint evidence is code-comment + log only. | `workspace/logs/lw-com.log:1720-1722, 2587`, `graph.py:3042-3048` |
| F22 | **`internal_api` is reachable by a second route the gate does not control.** `_ESCALATION = ["http_requests", "http_navigation", "playwright", "internal_api"]` (`graph.py:2912`) — `internal_api` is the **top** of the strategy escalation ladder and is reached when lower strategies fail, independent of the API gate. Fixing only `graph.py:3051` leaves this route open: a site whose poison endpoint was rejected can still *escalate* into `internal_api` after two failed retries. | graph.py:2912 |
| F23 | **A poison `api_endpoint` also disables the completeness gate.** `validate_coverage` (`webapp/agents/nodes/validate_coverage.py:178-189`) skips the coverage check entirely whenever `api_endpoint.url` is present — no `data_source` check. So job 12's ketchcdn endpoint both selected the wrong strategy *and* turned off the check that would have flagged the 1-of-6 artifact. P2 and P3 are the same bug at two layers. | validate_coverage.py:178-189 |
| F24 | **`api_endpoint` is trusted by six consumers, five of which never look at `data_source`:** `_derive_strategy` (:3051, the only gated one), `_select_template_file` (:3341-3379 → `api_scraper.py` on bare URL truthiness), `validate_coverage` (:178-189), `build_product_analyzer_message` (:1443-1480), `build_code_writer_message` api_section (:2672-2740), `_is_form_only_discovery` (:3119-3154). Fixing only `_derive_strategy` leaves five poison routes open. | graph.py, subagents.py, validate_coverage.py |
| F25 | **`create_agent_with_retry` (`nodes/retry_wrapper.py:56-147`) is exported but NOT used by the graph** — only `nodes/__init__.py:13` imports it. It cannot be the P1 hook. The real routing is: `_invoke_agent_with_timeout` (`graph.py:1673-1711`) **swallows every agent exception** into `{"_error": ...}`, a key **no caller reads**; `_invoke_code_tester` (`graph.py:3909-3913`) and `_invoke_cleanup` re-raise. **Job 12's 429 was code_tester** — so the fatal path is exactly `_invoke_code_tester`'s bare re-raise. | graph.py:1673-1711, 3909-3913; nodes/__init__.py:13 |
| F26 | The **"pause, don't fail" machinery already exists**: `services.stream_graph` catches `GraphRecursionError` → `create_recursion_approval` → job `WAITING_APPROVAL` ("paused, not failed"). P1 should reuse that exact pattern rather than inventing a new approval type plumbing. | `webapp/scraper/services.py:413-455` |
| F27 | The raw response body is **never persisted anywhere** — `verify_api` reads `r["text"]` and discards it; `_fetch_api_sample`'s sample is inlined into prompt text only (surviving in DB `SessionLog` at `[:20000]`). Any future poison therefore requires a live re-fetch to diagnose. | traversal.py:490, subagents.py:1111-1150, graph.py:4436 |
| F28 | `_sanitize_nav_domains` (`graph.py:2143-2199`) blanks cross-registrable-domain URLs in `search.working_url`, `discovery.listing_url`, `item_links.url_examples` — but deliberately does **not** touch `api_endpoint`. So a domain-sameness rule would also fight the existing sanitizer. Cross-domain must stay legal. | graph.py:2143-2199 |
| F29 | Reusable predicates already exist and should not be re-invented: `has_substantive_field` (`src/content_types.py:471-485`, content-type-agnostic "real item" test), `output_filter_fields` (:488-500), `_ALWAYS_PRESENT_FIELDS = {title,url,src_url,scraped_at,status_code,remarks,id}` (:468), and the `map_jobs` alias resolver (`src/job_fields.py:156-220`). | src/content_types.py, src/job_fields.py |

---

## 1. Centerpiece A — The taxonomy of non-product endpoints

The unit of enumeration is the **class**, identified by *response shape*, not by URL.
Every class lists whether a URL rule could reject it. "URL rule?" = "would a URL
pattern reject every member of this class without also rejecting a legitimate item
API?" If the answer is no, the class **must** be handled by content shape.

| # | Poison class | Real instances seen in this repo / in the wild | Typical URL shape | Typical response shape | Would ANY URL rule reject it? | Rejected by (spec §2) |
|---|---|---|---|---|---|---|
| **C1** | **Consent management platform** | ketchcdn (job 12), OneTrust `otCookieBanner`, TrustArc, Cookiebot, Sourcepoint | Cross-host CDN; `/config/{org}/{site}/{env}/{locale}/config.json`; versioned; `.json` | 100-300 KB object; 30-40 top-level keys (`purposes, vendors, stacks, theme, jurisdiction, regulations, isConfigPaused`); leaf arrays of **exactly 5-ish** `{code,name,description,…}` records | **No.** `/config` + `.json` rules catch ketch but a CMP may serve from `/api/` too, and `production` ⊃ `product` defeats token rules. URL rules here are a coin flip. | **V1** (38 top keys) + **R3** (5 records regardless of requested page size) + **R5** (purpose names never appear as listing cards) |
| **C2** | **Personalization / CDP / experience widget** | useinsider (job 12), Segment, mParticle, Braze, Dynamic Yield, Algolia-Recommend | Frequently `/api/…` — **deliberately indistinguishable by URL** | `{campId, frameless, content:"<2-6 KB HTML>"}`; largest dict-array is 1-3 records (currency, locale); `content-type: application/javascript` | **No.** `/api/` is present and real. Blocking `/api/` breaks every legit API. | **R1** (`application/javascript` ≠ JSON) + **R2** (1 record) + **V2** (6 KB HTML string) |
| **C3** | **Analytics / ads / tag delivery** | doubleclick (vistastaff artifact), GA/GTM, criteo, bat.bing, `/collect`, `/track` | Cross-host; `/collect`, `/v2/track`, `collect?v=2&tid=…` | Hit arrays / pixel responses; no item identity fields; no count; often 1×1 gif or 204 | **Mostly yes** (`_TELEMETRY_RE` already covers these hosts) but the class is unbounded (new vendors weekly). | **R4** (no identity key) + **R2** + existing `_TELEMETRY_RE` as a fast pre-filter (kept, never load-bearing) |
| **C4** | **Search-index vendor — ⚠️ LEGITIMATE item source** | lw.com Coveo `/coveo/rest/search`, Algolia `/*-dsn.algolia.net/1/indexes/*/query`, Typesense, Elasticsearch | **Cross-domain vendor host**, `/search`, `/query`, `/indexes/*/query` | `{results:[{hits:[15-100 records with name/price/url]}], totalCount|nbHits: N}` | **No — and we must not try.** Any cross-domain or `/search` rule kills lw.com and every Algolia site. | **Must PASS.** R1 ✓, R2 ✓, R3 ✓ (`nbHits`/`totalCount`), R4 ✓, R5 ✓ (hits are the rendered cards). This class is the reason the spec is positive, not negative. |
| **C5** | **Enterprise-commerce hydration** | SAP OCC `api.priceline.com.au/occ/v2/.../products/details` (job 12's *actual* source), SFCC `on/demandware.store/.../Search-Update`, hybris cx-state | Cross- or same-domain; `/occ/v2/`, `/Search-Update` | **Single object** (`{product:{…}}`) or a product array with envelope | **No.** And it is *usually not the listing source anyway*. | **R2** fails for single-object detail responses → correctly rejected as a *listing* API; the embedded_json / playwright path owns it (exactly what job 12's `product_analysis` concluded). Array-shaped OCC list responses PASS normally. |
| **C6** | **Taxonomy / lookup** | aya `wp-json/…/joblookups` — 2,217 city/state records, no count (named in traversal.py docstring) | Real API host, real `/api/` | Long array of small `{code,name,id}` records from a **closed vocabulary**; no count | **No.** Same host, same shape as a real API. | **R5** (city names are not job cards) + **R3** (no count, no page growth). Today `_score` beats it by preferring `count`; the spec preserves that outcome without preferring a URL. |
| **C7** | **i18n / translation / manifest / feature flags** | `/translations/en.json`, `*.min.json`, `manifest.json`, LaunchDarkly | Static file suffix, often same-host | Flat `key→string` maps or flag maps; **no dict arrays** | Often yes (suffix), but suffixes change and are also used by real APIs. | **R2** (no array of dicts) |
| **C8** | **Review / UGC widget** | Yotpo, Okendo, Bazaarvoice, Trustpilot | Cross-host; `/reviews/{sku}.json`, `/v1/widget/…` | `{products:[{id,name,rating}], bottomline:…}` — real arrays, real identity keys, sometimes real counts | **No** (and we should not: it *is* structured JSON). | **R5** (review bodies / product names may match cards, but the array is reviews, not items — join on the *item id* set fails) → demoted to hint. Deliberate: reviews are useful data but are not the item source. |
| **C9** | **Currency / geo / session bootstrap** | `*/geo.json`, CSRF/session init, insider's currency block | Short paths, short bodies | 1-few small objects, no identity array | No. | **R2** (array too small) |
| **C10** | **SPA state blob / embedded JSON** | cx-state (priceline), `__NEXT_DATA__`, `window.__INITIAL_STATE__`, aya `ayaSearchMenusInitialState.jobsData` | **Not a network URL at all** — inline `<script>` in the listing HTML | One enormous object | N/A — never appears in the network log, so it never reaches the selector. Belt-and-braces: bundle/page URLs fail **R1** (`text/javascript`, `text/html`). | Owned by the existing embedded_json detector; R1 backstop. |
| **C11** | **Analytics / heatmap config that URL-mimics a product feed** — the *hardest* class, because **every URL heuristic wants to accept it** | zquiet: `c.heatmap.com/2097/zquiet.com-collections-shop-all-products.json` — **fired `internal_api`** | Cross-host; filename contains `collections-shop-all-products` | A heatmap's collection-config blob; 1 record, no count | **No — inverted.** A URL rule actively *accepts* this; blocking it means blocking `-collections-all/products.json`, which is Shopify's real feed shape (zquiet's shipped scraper uses `/collections/all/products.json`). | **R2** (1 record) + **R3** (no count, no growth) + **R5** (no item identities). This row is the single strongest argument that URL text cannot be evidence: the poison URL is a *superset* match of the legitimate one. |

**Reading the table.** C4 (legit search index) and C1/C2 (the two poisons that
actually broke job 12) are *shape-neighbours* — all three are cross-host JSON-ish
arrays fetched by XHR. No URL rule separates them. Content shape separates them
cleanly, on four independent axes. That is the argument for the spec.

**The ketch trap, stated explicitly.** C1 is the class that defeats naive positive
specs. ketch's `.purposes` array contains dicts with `name`, `code`, `id`,
`description` — it passes "array of ≥3 objects with a title/name-ish key". Any spec
built on identity-key presence alone **admits the poison that broke job 12.** The
spec below therefore never uses identity-key presence as sufficient evidence; R3, R5,
V1 and V2 all exist primarily because of C1.

---

## 2. Centerpiece B — The positive-evidence spec ("endpoint evidence contract")

### 2.1 Statement

> An endpoint may be published as `data_source="api"` — and therefore may select
> `strategy=internal_api` — **only** if a captured response proves it returns the
> items the page is showing. URL text is never evidence. It is at most a hint about
> *what to fetch and verify*.

### 2.2 The requirements

All evaluation happens on a response we already fetch. No new LLM calls, no new
service, no new infrastructure.

**R1 — transport.** The verify GET returned 2xx and its `content-type` is JSON-ish:
contains `json` or `+json`.
*Rejects:* useinsider (`application/javascript`), SPA shells (`text/html`),
bundle scans (`text/javascript`).
*Cost:* zero — `_httpx_fetch` already holds `resp.headers` and discards it (F7).

**R2 — record array.** The parsed body contains a list of ≥ `MIN_RECORDS = 3` dicts.
Reuse `_extract_items_count` verbatim.
*Rejects:* useinsider (1 record), C7, C9, single-object OCC detail (C5-detail).

**R3 — page-size sensitivity.** Re-probe with a large page request
(`limit=50`, then `pageSize=50`, then `perPage=50`). PASS iff
`records(asked=50) > records(asked=5)` **or** the body reports `count/total/nbHits/totalCount ≥ 10`.
*Rejects:* ketchcdn (5 purposes whether you ask for 5 or 50 — a config blob is not
paginated), aya joblookups (no count, no growth).
*Passes:* aya (26,955), Coveo (`totalCount`), Algolia (`nbHits`), amnhealthcare
(page grows 5 → real page size).
This replaces the current `items_per_page > 0` test, which today is vacuous: the
probe asks for 5, so `items_per_page == 5` merely echoes the request (F2).

**R4 — identity (a soft score, deliberately not a gate).** Score = fraction of records
yielding a non-bookkeeping identity value when run through the **existing** resolvers:
`has_substantive_field` / `output_filter_fields` (`src/content_types.py:471-500`) and
the `map_jobs` alias table (`src/job_fields.py:156-220`). Excluded keys:
`_ALWAYS_PRESENT_FIELDS` (`content_types.py:468`) — `url`, `src_url`, `status_code`,
`scraped_at`, `remarks`, `id`, `title` are bookkeeping, not evidence of an item.

R4 is a **score, not a requirement**, for two reasons both taken from real artifacts:

1. *It would reject a good site.* aya's records contain **no `title` key at all** —
   their identity is `expertiseText` / `professionText` / `facilityName` (F19). A fixed
   identity-key list scores aya near zero. Only the existing alias resolver (which
   already carries `$compose:employmentTypeText|expertiseText|professionText`) recovers
   it, and that resolver is job-specific, not product-specific.
2. *It admits the poison.* ketch's purposes carry `name`, `code`, `id`, `description`
   (F3) — a near-perfect identity score on a consent config.

So R4 feeds the tie-break when several candidates are otherwise equal (which is how
`_score`'s `len(sample_keys)` is used today at `traversal.py:1138-1145`) and is
recorded in the evidence block for audit. The *hard* work of "is this the item source"
is R5's job, which compares values rather than key names and is therefore immune to
both failure modes.

**R5 — ground-truth join. The discriminator.**
In the same browser session, capture the listing's own entities (F12: extend
`_PAGE_STATE_JS` to also return the winning sibling group's labels + hrefs +
`data-*` ids, ≤ 25 strings — the loop already holds them). Then ≥ `JOIN_MIN = 2`
of the API records' identity values — normalized (casefold, HTML/emoji stripped,
trailing slug or numeric id extracted) — must appear in that captured set.
*Content-type agnostic by construction:* there is no vocabulary in this rule. It
works for products (title), jobs (`jobTitle` vs job cards), articles (`headline`),
SERP (`title`), forums (`title`). It works for the unseen site because it asks
"does this response contain the things the page is showing?", which is the actual
definition of "this endpoint is the item source".
*Rejects:* ketchcdn (consent purpose names never appear on `/c/gifts`),
aya joblookups (city names ≠ job cards), review widgets (C8).

**V1 — config-envelope veto.** If the chosen array sits in an object with more than
`WIDE = 12` top-level keys → reject outright, regardless of everything else.
*Rejects:* ketchcdn (**38** top-level keys), useinsider (**14**).

**V2 — widget-payload veto.** If any top-level string value exceeds 2 KB and parses
as HTML (`/<[a-z]{2,}[\s>]/`) → reject outright.
*Rejects:* useinsider (`content` = 6,447 chars of HTML).

### 2.3 Verdicts — never lose the information

Verification is a pure function
`verify_data_endpoint(url, fetch_fn, page_type, dom_ground_truth) -> Evidence`
returning one of three verdicts. The endpoint is **always written to the artifact**;
what changes is whether it is authoritative.

| Verdict | Condition | Consequence |
|---|---|---|
| `verified` | R1 ∧ R2 ∧ **R5** (R4 recorded as score) | `data_source="api"`; may select `internal_api`. This is the small-catalog escape: R3 is **not** required when R5 passes, so a 6-item API on a 6-card page still verifies. |
| `hint` | R1 ∧ R2, but R5 unavailable and R3 fails — the endpoint returns real records we cannot tie to the page | `data_source` keeps the DOM mechanism. `api_endpoint` is **retained** with `evidence.verdict:"hint"`, so `_select_template_file` / `_best_api_endpoint` consumers still see it (amnhealthcare's Path-B shape). Downstream gets the soft hint, never the "primary data source / do NOT browse" block. |
| `rejected` | any R fails, or V1/V2 fire | The descriptor is **moved out of `api_endpoint` entirely** into `rejected_endpoints[]` with a machine-readable `reason_code`. `api_endpoint` is left empty, so all six consumers (F24) stop trusting it **without any per-consumer edit** — this is the choke point. |

V1/V2 are **absolute vetoes** — they fire even when R1–R5 all pass, because a config
envelope and a widget payload are structurally incapable of being an item source.

**Why moving rejected endpoints out of `api_endpoint` is the right choke point.**
F24 enumerates six consumers; only one is gated today. Editing six call sites is six
chances to miss one and six regression surfaces. Making **the presence of
`api_endpoint` itself mean "this endpoint produced positive evidence"** fixes all six
at once, is self-documenting in the artifact, and preserves every rejected candidate
in `rejected_endpoints[]` for audit and for the shadow run. `data_source` continues to
carry the DOM-derived mechanism, which is what `_pick_mechanism` already computes.

### 2.4 Known-good cross-check (the constraint-1 site list)

The bar must be *passable* on every site we must not break.

| Site | Real endpoint (source of the row) | count | R1 | R2 | R3 | R5 | Verdict |
|---|---|---|---|---|---|---|---|
| **aya** | `api.ayahealthcare.com/AyaHealthcareWeb/job/search` — artifact on disk | **26955** (runtime 26803; output 26,742 jobs) | ✓ | ✓ | ✓ count | ✓ job cards | **verified**. Note R4 scores **low** here: aya records have no `title` key — identity is `expertiseText`/`professionText`/`facilityName`. This is why R4 is a score, not a gate (F19). |
| **amnhealthcare** | `api.amnhealthcare.io/ONEAmnJobSearch/v1/JobSearch` (cross-domain) — artifact on disk, LLM-shaped descriptor | key absent (`jobCount` 6953 / 14674 carried in `pagination`) | ✓ | ✓ | ✓ page grows with `PageSize` | ✓ | **hint or verified**. Critically, **amn's shipped strategy is `http_requests`** — the `internal_api` gate never fired because `items_per_page` is absent (F16). So amn is *not* on the critical path for this change; the `hint` verdict existing at all is what protects it. |
| **lw.com** | Coveo `/coveo/rest/search` — **no artifact on disk**; `totalCount=0` is known from code comments, and the workspace log shows the endpoint returning **403** (F21) | **0** explicit | ✓ | ✓ (~15 default samples) | ✓ `totalCount` present | ✓ | **verified** *and* the separate `count != 0` guard at the strategy gate is **retained unchanged** — two independent guards, deliberate redundancy. Flagged as the weakest-evidenced row in this table; treat the shadow run as its real test. |
| **zquiet** | shipped scraper uses `/collections/all/products.json` (the real feed); the artifact carried the C11 heatmap poison | n | ✓ | ✓ | ✓ | ✓ | **verified** for the real feed, **rejected** for `c.heatmap.com/2097/…-collections-shop-all-products.json` (F15) — which today fires `internal_api`. |
| **vistastaff** | artifact carried `googleads.g.doubleclick.net/pagead/viewthroughconversion/...` — inert only because `data_source` was `none` | — | ✗ | ✗ | ✗ | ✗ | **rejected**, no longer by luck. `pagination_param: "page"` was inferred from noise. |
| **myntra / calvklein** | `api_endpoint: {}` in every artifact — no API was captured; the scrapers hardcode their real endpoints | — | n/a | n/a | n/a | n/a | **unchanged** — empty descriptor, no candidate to verify. |
| **rmwilliams / adameve / dollartree / locumtenens / kirkland / books / quotes / gutenberg** | `api_endpoint: {}` across all artifacts | — | n/a | n/a | n/a | n/a | **unchanged** — these sites produce no candidate at all, so the spec cannot affect them. The 22-site regression exposure is narrower than the constraint list implies. |
| **sidley** | `www.sidley.com/sitecore/api/people/search` — the only artifact retaining a full descriptor | **null** | ✓ | ✓ (100) | ✗ | ? | **the deliberate ambiguous case.** `sample_keys: ["text","value","count"]` is a facet/dropdown shape that the existing `_SELECT_OPTION_KEYS` guard failed to reject because `count` ∉ that set (F17). Under this spec it lands on `hint`, not `verified` — exactly right for an attorney-directory API that may or may not be the item source for a given job. Also the test case for the key-set bug. |
| **priceline (job 12)** | `global.ketchcdn.com/.../production/default/en/config.json` | null | ✓ | ✓ (5) | ✗ | ✗ | **rejected** (V1 38-keys + R3 no-growth + R5 no-join). Job 12's chain breaks at step 1. |

Cross-check against the taxonomy: **every** poison class is rejected by at least two
independent requirements; C4 is the only class that passes and the only one that
should.

Two observations that lower the risk of this change substantially:

1. **Nine of the constraint-1 sites have `api_endpoint: {}`.** The spec has nothing to
   reject and nothing to verify for them — their behaviour is bit-identical.
2. **amn's `internal_api` gate never fired.** The site the critique history most
   worries about does not depend on the code being changed.

The residual exposure is therefore concentrated on: aya (count-backed, safe), lw.com
(no artifact, weakest evidence), zquiet (would *gain* correctness), and any unseen
site. That is a small, testable surface.

### 2.5 Cost

R1–R4 reuse the GETs `verify_api` and `_fetch_api_sample` already perform (F9).
Today `verify_api` fires up to **9** requests (3 param-shapes × up to 3 host
variants); the spec fires **2** per candidate (one probe, one scale probe) and stops
at first rejection. R5 reuses strings the browser already evaluated (F12).
**Net: fewer HTTP requests, zero LLM calls.** Constraint 1 holds with margin.

---

## 3. Mechanisms and files, per problem area

### 3.1 P2 — poison endpoint / strategy-gate trust (root cause)

**3.1.1 One choke point, both producers.** F8 is the structural hazard: fixing Path A
while leaving Path B open would leave job-12-class failures reachable. New module
`webapp/agents/endpoint_evidence.py` (plain Python, Django-free, importable from both
`experimental/nav_traversal/` and `webapp/agents/nodes/`):

```
verify_data_endpoint(url, fetch_fn, page_type, dom_ground_truth=None) -> Evidence
Evidence = {verdict, reason_code, url, content_type, records_small, records_large,
            count, identity_hit_rate, join_hits, join_sample, veto, probed_at}
```

- `traversal.verify_api` calls it and returns `None` unless verdict ≠ `rejected`
  (so `_pick_mechanism` at `traversal.py:656` can no longer say `"api"` on weak shape).
- `navigate_synthesize._best_api_endpoint` calls it for each candidate before the
  ranking; candidates that fail are dropped from the descriptor and recorded in
  `rejected_endpoints`. **This closes Path B.** Note this path is currently
  *browser-unavailable fallback only* (`graph.py:2232-2251`), so its exposure is
  lower than Path A's — but its descriptor is the shape amnhealthcare's artifact
  carries, which is exactly why the `hint` verdict exists.
- `subagents._fetch_api_sample` refuses to emit the "★ DATA SOURCE = BACKEND JSON API
  (primary) ★ / do NOT browse the page" block unless the descriptor's verdict is
  `verified`; a `hint` descriptor gets the existing soft "Backend API Hint" wording,
  and a `rejected` descriptor is not in `api_endpoint` at all. Belt and braces on the
  third of six consumers (F24).

The remaining three consumers (`_select_template_file`, `validate_coverage`,
`_is_form_only_discovery`, plus `build_code_writer_message`'s `api_section` — F18,
which additionally **replaces the whole pagination block** when an api_endpoint is
present) need **no edits at all**: under §2.3 a rejected endpoint is no longer in
`api_endpoint`, so they cannot act on it. That is the point of choosing artifact shape
rather than call sites as the choke point — six regression surfaces collapse to one.

**3.1.2 Persist the evidence (fixes F6, F27).** `webapp/agents/graph.py:2393-2398`:
write the whole evidence block into `navigation_analysis.api_endpoint`, not just
`url/count/items_per_page`. Evidence must survive into `scraper_analysis.api_endpoint`
(`graph.py:3092`) so `code_writer` and any audit can see *why*. Include a **truncated
structural fingerprint** of the deciding response (≤ 2 KB: top-level key names, chosen
array path, first record's key list, first 400 chars of the first record) — today the
raw body is discarded entirely (F27), so diagnosing the *next* unknown poison requires
a live re-fetch against a site that may be rate-limiting us. This is the forensic
lesson of job 12 applied forward.

**3.1.2b Fix the existing shape-guard bug (F17).** `traversal.py:480`:
`_SELECT_OPTION_KEYS` omits `count`, so `set(sample_keys) <= _SELECT_OPTION_KEYS` can
never match a response that reports a count — Sidley's `["text","value","count"]`
slipped through for exactly this reason. Add `count`, `total`, `total_count` to the
set. One line, and it removes a false-positive class the new spec would otherwise
have to re-derive.

**3.1.3 DOM ground truth (fixes F12).** `traversal._PAGE_STATE_JS`: in the winning
sibling-group loop, collect `label` (innerText, ≤ 80 chars) + `href` + first
`data-*-id` for up to 25 members; return as `listing_entities`. Plumbed through
`browser_traverse` → `navigation_analysis.listing_entities`. The join helper
(normalize + match) lives in `endpoint_evidence.py` and is unit-testable without a
browser.

**3.1.4 Keep the existing URL heuristics — demote them.** `url_looks_like_data_api`
and `_TELEMETRY_RE` remain, but only as a **cheap pre-filter that decides whether a
candidate is worth one GET**, never as the reason an endpoint is accepted. Acceptance
is decided by `verify_data_endpoint` only. Yesterday's word-boundary fix is thereby
*extended, not reverted* (constraint 3).

**3.1.5 The gate itself.** `webapp/agents/graph.py:3051-3058` gains one conjunct:
`api_endpoint.evidence.verdict == "verified"`. The `items_per_page > 0` and
`count != 0` conjuncts stay — they are cheap, independent and encode the real
lw.com/Coveo explicit-zero case.

**The second route (F22) must be closed in the same change.** `_ESCALATION`
(`graph.py:2912`) escalates to `internal_api` when lower strategies fail, with no
reference to the API gate. Add the same conjunct there: escalating to `internal_api`
requires a `verified` `api_endpoint`. Without this, a site whose poison endpoint was
correctly rejected simply re-achieves the wrong strategy two retries later, which is
the *shape* of job 12's thrash (three codegen cycles) rather than its first cause.

**3.1.6 Reconciliation with a correcting content_analysis (job 12's actual rescue).**
Deterministic, no LLM: if `content_analysis.site_analysis_review.corrections_count >= 1`
and `content_analysis` names a URL or endpoint, then nav API evidence is treated as
**non-authoritative** and `internal_api` may only be selected from a `verified`
(join-backed) verdict — a `hint` verdict is insufficient. Additionally the
`strategy_justification` string records the contest explicitly, so `code_writer`
receives *both* facts instead of a single overruling verdict. This is the cheap
version of "let corrected product_analysis win", and it does not depend on trusting a
salvaged artifact for anything except the presence of a correction (F10 shows the
correction survived even the truncation).

### 3.2 P1 — 429 / provider-error retry policy

Read of the current implementation: `_retry_settings()` (`llm.py:62-72`) gives
`ratelimit_max=3`, `transient_max=2`, `backoff_base=1.5`, `backoff_cap=30`;
`_backoff_delay` (`llm.py:88-91`) is **full-jitter** — `uniform(0, min(cap, base*2**attempt))`.
Full jitter means attempt 1 can sleep **0.05 s**. That is precisely how job 12
produced four HTTP 429s in eight seconds: each retry re-entered the same closed
window and burned an attempt. `_parse_retry_after` is honoured but z.ai sent no
`Retry-After`, so the floor was effectively zero.

**3.2.1 Provider-keyed retry policy.** Generalize `_retry_settings()` to resolve a
per-provider policy keyed by `_provider_for(model)`'s base_url / prefix — the same
resolution used for the breaker. Registry shape:

```
LLM_RETRY_POLICIES = {
  "zai-direct":    {ratelimit_max, transient_max, backoff_floor, backoff_base, backoff_cap, total_budget_s},
  "litellm-proxy": {…},
  "default":       {…},   # today's numbers exactly — unknown providers see no change
}
```

This is the generalization ask: z.ai direct, the LiteLLM proxy, and "whatever comes
next" each get a policy without code changes, and an unknown provider silently falls
back to the current behaviour.

**3.2.2 Bounded jitter with a floor for the rate-limit class only.**
`uniform(backoff_floor, min(cap, base * 2**attempt))` when `kind == "rate_limit"`;
full jitter retained for `transient`. Default `backoff_floor = 2.0`. A rate limit is
a *window*, not a coin flip — retrying 0.05 s later is a guaranteed second 429.

**3.2.3 Raise the rate-limit budget, add a total budget.** `ratelimit_max` 3 → 5 for
the rate-limit class, plus `total_budget_s` (default 300) enforced cumulatively
across the class's sleeps: once the budget is spent, stop and escalate. Expected
total wait ≈ 2+4+8+16+32 ≈ 62 s, worst case ≈ 118 s — inside the 900 s agent wall
(`subagents.py:89`), and a quota-style window is actually given a chance to close.

**3.2.4 Exhaustion pauses, it does not fail.** On rate-limit exhaustion, raise a typed
`LLMProviderExhausted` instead of the raw SDK exception. The hook is **not**
`nodes/retry_wrapper.py` — that wrapper is exported but unused by the graph (F25). The
real routing today is: `_invoke_agent_with_timeout` (`graph.py:1673-1711`) swallows
every agent exception into `{"_error": ...}`, a key **no caller reads** (so for
site/product/nav agents a provider error already surfaces as a misleading
"budget_exhausted" interrupt), while `_invoke_code_tester` (`graph.py:3909-3913`) and
`_invoke_cleanup` re-raise — and **job 12's 429 was code_tester**, so that bare
re-raise is precisely the fatal path.

Change, in order of leverage:

1. In `_invoke_code_tester`, `_invoke_cleanup` and `_run_budgeted_agent`, catch
   `LLMProviderExhausted` and return the existing `human_approval` routing
   (`Command(goto="human_approval", update={..., "interrupt_reason":
   "llm_provider_rate_limited"})`), mapped in
   `services.py:INTERRUPT_TO_APPROVAL_TYPE`.
2. Reuse the established pause pattern rather than inventing new plumbing: model the
   approval creation on `create_recursion_approval` (`services.py:413-455`), which
   already sets job `WAITING_APPROVAL` — "paused, not failed".
3. Make the swallowed-error channel honest while there: `_invoke_agent_with_timeout`'s
   `{"_error": ...}` is never read by anyone. Have it detect `LLMProviderExhausted`
   and route as above, instead of letting a provider outage masquerade as a budget
   problem.

The job then **pauses** with an approval row the user can act on, instead of going
FAILED with zero approval rows (job 12's exact outcome). No async, no new service, no
new infrastructure (constraint 5). LLM_ASYNC_EXECUTION stays off;
`_retry_classified_sync` is the only path that matters and is unchanged in shape.

**3.2.5 Session-log the exhaustion.** The 429 never entered SessionLog because it was
thrown after the last logged line. Emit a log line *inside* `_handle_retry`'s
exhaustion branch (before the raise) so the artifact trail shows the provider error
even when the interrupt path is what catches it.

### 3.3 P3 — artifact completeness (validity ≠ completeness)

**3.3.1 Completeness is content-type driven, not product-shaped.** New pure helper in
`src/content_types.py` (next to `schema_field_names`):

```
artifact_completeness(artifact, target_fields, page_type, output_schema)
  -> {score, present[], missing[], basis}
```

`basis` = `target_fields` (intake chips) if given, else `output_schema["fields"]`
names, else the content type's `core_field_names`. This is what makes the predicate
work for **all 11 page types / 6 content types** rather than hardcoding
`title`/`price`: `job_posting` scores against `title, company, location, description`,
`serp` against `rank, url, title, snippet`, `forum_thread` against `title, author,
posts`. Bookkeeping fields (`BOOKKEEPING_FIELDS`) are excluded from the denominator.

**3.3.2 Salvage provenance is written, not inferred.** `repair_json_text`
(`webapp/agents/graph.py:281`) and `_fix_json_artifact` (`:355`) already know when
they salvaged. Emit a sibling `*_provenance.json` (or an `evidence` sub-key where the
writer controls the shape) recording `{salvaged: true, repair_pass, reason,
truncated_at_char, completeness}`. `sanitize_json_content`
(`filesystem_tools.py:52`) and `guard_json_bytes` (`:108`) are the two write/copy
choke points — provenance is attached there so it survives the workspace → File-Master
copy that `setup_workspace.py:104` performs.

**3.3.3 Read-time behaviour, per consumer.**
- `_derive_strategy` (3.1.6): a `completeness < 0.5` content_analysis may not
  *authoritatively* confirm a strategy — it may only veto (contested) — which is
  exactly the epistemic position a 1-of-6 salvage deserves.
- `build_code_writer_message`: injects a `⚠ artifact incomplete (N/M fields,
  salvaged)` line so the writer knows the field map is partial instead of
  discovering it at test time (cycle 3's 49 tool calls and zero writes).
- `code_tester`: unchanged thresholds; the banded prior-count fix from `dbb52e0` is
  untouched.
- **`validate_coverage` (F23) is the P2/P3 junction:** it skips the coverage gate
  whenever `api_endpoint.url` exists, with no `data_source` check. Under the choke
  point (§2.3) a *rejected* endpoint is no longer in `api_endpoint`, so the gate
  re-arms itself for free on poison sites. No edit needed — but add a test locking it
  (`test_poison_endpoint_does_not_disable_coverage_gate`, §4.5), because nothing else
  in the codebase documents that dependency.

**3.3.4 Do not refuse to publish.** The brief's "refusing to publish salvaged
artifacts as authoritative" is deliberately **not** adopted in full: refusing would
break `check_tracker` resume (which needs *some* artifact to exist) and would make a
1-of-6 salvage worse than no artifact. Provenance + read-time demotion achieves the
same safety without that regression.

### 3.4 P5 — stale-artifact re-injection on resume

**3.4.1 Re-verify, don't re-trust.** `setup_workspace.py:104` re-hydrates a
`navigation_analysis.json` whose api_endpoint is a URL. Verification
(`verify_data_endpoint`) is a pure function of `(url, response)` and costs one GET.
On re-hydration of a navigation artifact carrying `api_endpoint.url`, re-run
verification; if the verdict is `rejected`, strip `data_source="api"` from the
rehydrated artifact (and log the strip) before writing it into the workspace. Jobs
9/10's ketchcdn artifact would therefore no longer poison job 12 even with
`skip_site_analysis` set.

**3.4.2 Target-field consistency.** When re-hydrating a content artifact, compare its
`fields` keys against the current job's `target_fields`. If the coverage is below
0.5, do **not** set `skip_content_analysis` — re-run the content agent. This is
deterministic, uses 3.3.1, and directly addresses "same bad inputs → identical wrong
decision".

### 3.5 P4 — date bomb

`webapp/scraper/management/commands/recompute_date_reliability.py:29`.

- Replace `FIXED_AT = datetime(2026,8,27)` with `FIXED_AT = datetime(9999,12,31,23,59,59, tzinfo=utc)` and add an optional `--until` argument (ISO datetime) defaulting to that sentinel. Postgres `timestamp` accepts 9999-12-31; no schema change.
- Fix the comment: the current text claims "end-of-day inclusive", which is false — `datetime(2026,8,27)` is **midnight**, so `__lte` excludes every row scraped that day and after.
- Rationale for dropping rather than widening (F13): the re-derivation is idempotent — for post-fix rows the fixed parser yields the same answer it already wrote — so an upper bound buys nothing and re-creates the bomb on every future run. Hand-widening 3× (Aug 25→26→27) is the signature of a wrong-shaped bound.
- Keep `BROKEN_FROM` unchanged; keep `equals_scrape_date` / `future_dated` staying unreliable-and-NULL.
- This is live in prod; ship it first (constraint 5: web-UI-only deploy, it is a management command inside the Django image).

---

## 4. Failing tests first (TDD — constraint 7)

House style: `tests/test_*.py`, pytest, Django `--keepdb` where models are touched.
Suite baseline 719 pass / 2 fail (P4) / 2 skip.

### 4.0 Fixtures

`tests/fixtures/endpoints/*.json` — real recorded response bodies, sanitized:

- `ketchcdn_config.json` — captured live 2026-08-27 (157 KB, 38 top keys, `.purposes[5]`)
- `useinsider_info.json` — captured live 2026-08-27 (30 KB, `content` = 6.4 KB HTML)
- `aya_job_search.json` (array + `count`), `coveo_search.json` (explicit `totalCount: 0` + 15 `hits`),
  `amn_job_search.json` (`count: null`, `PageSize`), `algolia_query.json` (`nbHits`),
  `yotpo_reviews.json`, `aya_joblookups.json` (2,217 closed-vocab records),
  `occ_product_detail.json` (single object), `translations_en.json`, `geo_currency.json`,
  `zquiet_heatmap_config.json`, `sidley_people_search.json` (`sample_keys: ["text","value","count"]`),
  `shopify_collections_all.json` (the legit twin of the C11 poison), and a
  `priceline_gifts_cards.json` DOM ground-truth fixture (10 card labels from the real
  `/c/gifts` listing, for the R5 join tests)

### 4.1 Poison-class tests (one minimum per taxonomy row — the hard requirement)

All in `tests/test_endpoint_evidence.py`, all asserting the named `verdict` and
`reason_code`, **none asserting any URL property as the reason** — several
deliberately assert the *opposite* (that the URL in question passes today's URL
heuristic) to lock in that URL text is not doing the work:

| Test | Taxonomy class | Fixture | Asserts |
|---|---|---|---|
| `test_c1_consent_config_rejected_by_envelope_veto` | C1 | ketchcdn | `rejected`, `veto="config_envelope"`, `top_level_keys=38` |
| `test_c1_consent_config_rejected_by_page_scale` | C1 | ketchcdn | 5 records at `limit=5` **and** at `limit=50` → `reason_code="no_page_growth"` |
| `test_c1_consent_purposes_fail_dom_join` | C1 | ketchcdn + priceline `/c/gifts` card labels | `join_hits=0` |
| `test_c2_personalization_rejected_by_content_type` | C2 | useinsider | `content_type=application/javascript` → `reason_code="not_json"` |
| `test_c2_personalization_widget_payload_veto` | C2 | useinsider | `veto="html_widget_payload"` (6,447-char HTML string) |
| `test_c2_personalization_record_array_too_small` | C2 | useinsider | 1 record → `reason_code="record_array_too_small"` |
| `test_c3_analytics_hit_array_rejected` | C3 | doubleclick-style hit array | `reason_code="no_identity_fields"` |
| `test_c3_url_heuristics_are_not_evidence` | C3 | synthetic URL with `/api/track` + empty array body | rejected even though `url_looks_like_data_api` is True — **locks in the demotion** |
| `test_c4_coveo_explicit_zero_is_verified_not_rejected` | C4 (legit) | coveo | `verdict="verified"`; `count=0` present; the **strategy gate** still refuses `internal_api` (existing `count != 0` guard intact) |
| `test_c4_algolia_cross_domain_is_verified` | C4 (legit) | algolia | `verified` — proves cross-domain is not a rejection axis |
| `test_c5_occ_single_object_is_rejected_as_listing_api` | C5 | occ detail | `reason_code="record_array_too_small"` |
| `test_c6_taxonomy_lookup_rejected_by_join` | C6 | aya joblookups + aya job cards | `join_hits=0`, `reason_code="no_dom_join"` |
| `test_c7_translations_rejected` | C7 | translations_en | `reason_code="no_record_array"` |
| `test_c8_review_widget_demoted_to_hint` | C8 | yotpo | `rejected` (or `verified` with `data_source` NOT `api`) and the **code_writer prompt** contains the soft hint, not the "primary data source / do NOT browse" block |
| `test_c9_currency_geo_rejected` | C9 | geo_currency | `reason_code="record_array_too_small"` |
| `test_c10_bundle_and_page_urls_rejected_by_content_type` | C10 | `text/javascript`, `text/html` bodies | `reason_code="not_json"` |
| `test_c11_heatmap_url_mimicking_product_feed_rejected` | C11 | zquiet heatmap config | `rejected`; and asserts the URL **would pass** `url_looks_like_data_api` — locking in that URL text was not the reason |
| `test_c11_real_shopify_collection_feed_verified` | C11 (legit twin) | `/collections/all/products.json` fixture | `verified` — proves C11's rejection is shape-driven, not filename-driven |
| `test_select_option_guard_catches_count_bearing_facet_response` | F17 bug | Sidley `["text","value","count"]` | rejected by the existing key-set guard once `count`/`total` are added to `_SELECT_OPTION_KEYS` |
| `test_sidley_people_search_lands_on_hint_not_rejected` | ambiguity control | sidley fixture | `verdict="hint"` — a same-domain directory API that may be legitimate is demoted, never destroyed |

### 4.2 Known-good tests (must pass — constraint 2)

| Test | Asserts |
|---|---|
| `test_aya_job_search_verified` | `verified`, count 26955 |
| `test_amn_cross_domain_null_count_verified` | `verified` **without** a count (R5 path) — the single most important non-regression test |
| `test_small_catalog_verified_via_join_without_page_growth` | 6 records, no count, no growth, join hits 2 → `verified` (the small-catalog escape) |
| `test_existing_gate_test_still_passes` | `tests/test_content_types.py:619-657` (`_derive_strategy` → `internal_api`) passes **unchanged in intent**, with the evidence block added to the fixture |
| `test_missing_evidence_block_blocks_internal_api` | `api_endpoint` without `evidence` (legacy/artifact shape) → strategy is **not** `internal_api`; this is the migration edge |
| `test_vistastaff_doubleclick_no_longer_api` | the poison that was inert by luck is now rejected by shape |

### 4.3 Producer- and consumer-closure tests (F8, F18, F24)

| Test | Asserts |
|---|---|
| `test_best_api_endpoint_rejects_unverified_candidate` | `_best_api_endpoint` returns `{}` for a URL-shaped candidate whose body is a config blob |
| `test_best_api_endpoint_still_returns_amn_shape` | the `{url, base, method, query_params, pagination_param, …}` descriptor is unchanged for a verified endpoint |
| `test_code_writer_prompt_softens_on_rejected_endpoint` | `build_code_writer_message` with `verdict="rejected"` does **not** contain "do NOT browse the page" |

### 4.4 P1 tests — `tests/test_llm_retry_policy.py`

| Test | Asserts |
|---|---|
| `test_rate_limit_backoff_has_floor` | with `backoff_floor=2`, 429 delays are ≥ 2.0 s (this is the bug: full jitter allowed 0.05 s) |
| `test_rate_limit_budget_raises_attempts` | `ratelimit_max` for the rate-limit class is 5 |
| `test_total_backoff_budget_stops_retries` | cumulative sleep ≥ `total_budget_s` → raises `LLMProviderExhausted`, no further attempts |
| `test_provider_policy_resolved_per_provider` | z.ai-direct and litellm-proxy resolve different policies; an unknown provider resolves to `default` == today's numbers |
| `test_exhaustion_raises_typed_error_not_raw_sdk_error` | `LLMProviderExhausted` |
| `test_rate_limit_exhaustion_interrupts_not_fails` | the node wrapper returns a `human_approval` routing with `interrupt_reason="llm_provider_rate_limited"`, and an approval row is creatable |
| `test_retry_after_still_honoured_and_capped` | unchanged behaviour when the provider does send the header |

### 4.5 P3 / P5 tests

| Test | Asserts |
|---|---|
| `tests/test_artifact_completeness.py::test_completeness_all_content_types` | parametrized over all 6 content types × representative page types: `job_posting` scores on `company/location`, `serp` on `rank/snippet`, `forum_thread` on `posts` — **not** hardcoded to product fields |
| `test_completeness_of_job12_salvage_is_1_of_6` | the real priceline artifact (`fields` = `["current_price"]`, `target_fields` 6) scores ≈ 0.17 |
| `test_salvage_provenance_written_on_repair` | a truncated write yields a provenance record with `salvaged=true`, `repair_pass=2` |
| `test_provenance_survives_fm_copy` | `guard_json_bytes` preserves provenance through the workspace → FM round-trip |
| `test_incomplete_content_analysis_cannot_authoritatively_confirm_strategy` | `completeness < 0.5` + `corrections_count>=1` → `internal_api` not selected from a `hint` verdict |
| `test_poison_endpoint_does_not_disable_coverage_gate` | a `rejected` endpoint moved to `rejected_endpoints[]` → `validate_coverage` no longer skips the coverage check |
| `test_escalation_ladder_cannot_reach_internal_api_without_evidence` | `_ESCALATION` exhausted to the top rung with no `verified` api_endpoint → strategy lands on `playwright`, not `internal_api` |
| `test_incomplete_artifact_flagged_in_code_writer_message` | message contains the partial-field warning |
| `tests/test_setup_rehydrate.py::test_rehydrated_poison_api_is_stripped` | a FM `navigation_analysis.json` carrying ketchcdn is rehydrated with `data_source != "api"` |
| `test_rehydrate_skips_content_analysis_only_when_coverage_sufficient` | 1-of-6 coverage → `skip_content_analysis` **not** set |

### 4.6 P4 tests

| Test | Asserts |
|---|---|
| `test_recompute_includes_rows_created_today` | a `JobListing` with `scraped_at = now()` and `date_posted_reliable=False` is selected (currently fails — this is one of the two pre-existing red tests) |
| `test_recompute_includes_rows_created_tomorrow` | `scraped_at = now() + 1 day` is selected — **the test that would have caught the bomb**, and the one the brief asks for |
| `test_apply_fixes_row_uses_unbounded_window` | the second pre-existing red test, now green |
| `test_until_argument_narrows_window` | `--until 2026-08-20` excludes rows after it |

---

## 5. Rollout order

Rollout is by **blast radius**, smallest first, and each step is independently
revertable. All deploys are web-UI-only Railway deploys (constraint 5).

1. **P4 date bomb** — one file, one constant + one argument, 4 tests. Live in prod now, scanning 0 rows. Zero interaction with anything else.
2. **P1 retry policy** — `llm.py` + the `_invoke_*` wrappers in `graph.py` + `services.py` approval mapping. Changes no strategy decisions; worst case it changes how long a job waits. Independent of P2/P3/P5.
3. **P2 evidence module + fixtures + poison tests**, with `verify_data_endpoint` wired into `verify_api` **only**, and the gate conjunct **off by default** behind `ENDPOINT_EVIDENCE_ENFORCE` (default `False`). Ship it, watch the logs for a full regression sweep: the verdicts are recorded either way, so we get a real-world shadow comparison of "would have rejected" against every site that still works.
4. **Flip `ENDPOINT_EVIDENCE_ENFORCE=True`** after the shadow run agrees with the known-good table. This is the single moment job 12 becomes structurally impossible.
5. **Close Path B** (`navigate_synthesize._best_api_endpoint` + `subagents` prompt softening) — only after 4 is green. Path B is the browser-unavailable fallback and is the shape amnhealthcare's artifact carries, so it moves last and only once the `hint` verdict is proven to preserve amn's descriptor.
6. **P3 completeness + provenance**, then **P5 re-verify-on-rehydrate** last (P5 consumes P3's completeness helper).

Rationale for the flag: constraint 2 is a hard no-regression list and our only
regression harness is live sites. A shadow run converts an unverifiable claim
("the bar is passable everywhere") into an observed one, at zero extra cost.

---

## 6. Rollback

| Step | Rollback | Residual risk |
|---|---|---|
| P4 | restore `FIXED_AT` constant; or run `--until 2026-08-26` to reproduce old behaviour without a deploy | none — command is manual |
| P1 | unset `LLM_RETRY_POLICIES` / restore `ratelimit_max=3`; `LLM_RETRY_FLOOR=0` restores full jitter. The `human_approval` mapping can be removed independently (job then FAILs as before) | a provider with a very long quota window could now burn 300 s per phase instead of 8 s — bounded by `total_budget_s` |
| P2 | set `ENDPOINT_EVIDENCE_ENFORCE=False` — heuristics-only behaviour returns byte-for-byte; no data migration, no schema change, evidence blocks in artifacts are additive and ignored | artifacts written during enforcement keep an `evidence` key that old readers ignore |
| Path B closure | revert `_best_api_endpoint` to the unverified ranker; amn returns to today's path | none beyond the original poison exposure |
| Consumer collapse | n/a — no consumer edits were made; the choke point is the artifact shape itself | the six consumers (F24) cannot individually regress, because none of them changed |
| P3 | provenance writers are additive; readers treat absence as "complete" | a provenance file in FM that downstream ignores — inert |
| P5 | stop calling re-verification in `setup_workspace`; behaviour identical to today | re-runs the original stale-injection exposure |

Every rollback is a settings flip or a single-file revert. None requires a data
migration or a manual artifact repair.

---

## 7. Constraint compliance

| # | Constraint | How this plan complies |
|---|---|---|
| 1 | No new per-run LLM cost | Zero LLM calls anywhere. Verification reuses GETs that `verify_api` and `_fetch_api_sample` already make (F9) — **net fewer** requests (2 vs up to 9 per candidate). R5 reuses DOM strings the browser already evaluated (F12). The P1 interrupt path reuses the existing `human_approval` node. |
| 2 | Must not break the working sites | §2.4 cross-checks every named site against the spec, with a dedicated test per site-class. amnhealthcare (`count:null`, cross-domain) passes **on R5**, which is why R5 must not require a count. lw.com's `count != 0` guard is retained unchanged as an independent second guard. toscrape/locumtenens produce no candidate, so they are untouched. Shadow-run gate (§5.3-4) before enforcement. |
| 3 | Do not undo yesterday's fixes | `url_looks_like_data_api` and `_TELEMETRY_RE` are **kept** and demoted to a fetch pre-filter — extended, not reverted. The 8 codegen fixes, banded prior-count and catalog-guidance restoration are untouched; P3 explicitly does not change `validate_coverage` thresholds. |
| 4 | Streaming stays on; lenient parse is a fact of life | No LLM call path is altered except retry timing and the typed exhaustion error. P3 assumes lenient-parse salvage happens and makes it *visible* rather than trying to prevent it. |
| 5 | No async, no new infra, web-UI-only deploys | `verify_data_endpoint` is a plain synchronous function; the P1 limiter is not needed for the fix and the total-budget guard is a plain accumulator. Everything ships inside the existing Django/worker images. |
| 6 | Deterministic scraper_analyzer stays deterministic | `_derive_strategy` gains one boolean conjunct and one deterministic reconciliation rule. No LLM, no prompt. |
| 7 | Failing tests first | §4: 21 poison/shape tests (≥1 per taxonomy row, plus C11's legitimate twin and the F17 key-set bug), 6 known-good, 3 Path-B, 7 P1, 9 P3/P5, 4 P4 — all specified test-first with fixtures from real captured payloads. |

---

## 8. What could break, and the residual risk we accept

1. **R5 depends on the DOM capture being present and honest.** If `_PAGE_STATE_JS`
   fails to find a sibling group (a listing shape the repetition detector misses),
   `dom_ground_truth` is empty and we fall to the `hint` verdict, which keeps the
   endpoint as a non-authoritative hint. That
   is strictly *more* evidence than today's gate requires, so it cannot be worse than
   the status quo — but it is weaker than the intended bar.
2. **Truncated/HTML-ized card titles can break the join** on small catalogs (see §9).
   Mitigation is normalization + the small-catalog escape; residual risk accepted and
   named below.
3. **One extra GET per candidate.** Bounded by the existing 8 s fast-fetch timeout;
   net request count still falls because rejection short-circuits the 3×3 loop.
4. **The `9999-12-31` sentinel.** Postgres `timestamp` max is 9999-12-31, so the
   sentinel is legal but leaves no headroom; if a future Django/psycopg version
   objects, `--until` is the escape hatch and the bound can simply be omitted.
5. **`human_approval` on provider exhaustion changes job semantics.** A job that used
   to FAIL now PAUSES awaiting a user. That is the intended behaviour, but operators
   must expect paused jobs in the queue; the interrupt reason is a distinct, greppable
   string.
6. **Artifact schema grows.** `api_endpoint.evidence` and `rejected_endpoints` are new
   keys. All current readers access by key and ignore extras; the archived
   `test_navigation_handoff.py` expectations are already archived.

---

## 9. The one good site this spec might wrongly reject

**rmwilliams** (and its class: small-catalog Shopify/catalog-JSON sites — adameve,
zquiet are neighbours).

Why: the `verified` path wants R5, and R5 joins the API's identity values against the
card labels captured from the listing DOM. On a small catalog the exposure is doubled
— only a handful of cards are rendered (so `JOIN_MIN = 2` has little slack), and
Shopify/API catalog titles are frequently *longer* than the DOM's truncated card
titles (`…""` ellipsis) or carry different capitalization and HTML entities. If
normalization fails to reconcile those, `join_hits` lands at 1, R3 also fails (a
6-item catalog shows no page growth and reports no count), and the endpoint is
rejected — pushing a perfectly good API site onto the slower detail-link path. It
would still *complete* (that is the mitigating fact), but it would regress the
"fast, correct strategy" property we are protecting.

Mitigation already in the spec: aggressive normalization (casefold, HTML/emoji strip,
slug and trailing-numeric-id extraction — a `/products/12345` href joined on the
record's `id` is immune to title truncation), `JOIN_MIN` dropping to 1 when the
captured card set is smaller than 5, and joining on ids/hrefs as well as titles. The
shadow run in §5.3 is the check that actually catches it before enforcement flips.
