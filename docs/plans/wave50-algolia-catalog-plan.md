# Wave-50 — Algolia catalog discovery + junk-proof real-item bars (job-1116 wonderbly)

Owner directive (2026-09-29): "yes plan a fix use deepagents and i expect results not marking
this as failure." The bar: wonderbly-class jobs must ship REAL products — an honest FAILED is
not an acceptable outcome. "Use deepagents" = deep multi-agent execution of this plan (the
deepagents pip package stays rejected per wave-29: patterns, not the package).

## What actually happened to job 1116 (wonderbly.com, navigation, firstn/10)

Prod on 826c1e6 (waves 48+49 live). The writer **did** diagnose client-rendered + extracted
Algolia creds from the astro-island props (`W3CI0YBYUX` / search key, index `products_us`) —
real progress vs job 1082's blind 2-FAQ-links — but the draft still shipped junk. The chain,
every link verified:

1. SSR listing HTML has **zero** product hrefs (6 astro-islands, `/tmp/wb_listing.html`).
   The draft's Algolia query **succeeded**: multi-query envelope, `nbHits=86` (taxon
   `Kids > Age > 0-3` on `taxon_path`), collected 86 hits.
2. Hit→URL conversion returned **0**: the draft's `_hit_product_url` fallback checks
   `productUrl, product_url, url, slug, handle`; the index carries slugs under
   **`solidusSlug` / `prismicProductSlug`** (live re-verified: nbHits=228 unfiltered; hit keys
   include `solidusSlug`, `prismicProductSlug`, and NO `url`/`productUrl`/`handle`).
   Zero URLs → writer fell back to SSR **nav/collection anchors** as "product URLs".
3. Phase 2 fetched those collection shells → no JSON-LD product → `_norm_price` → None
   ("unparseable price is EMPTY, never zero") → junk rows `{"title": <nav text>,
   "currency": "USD", "price": ""}`. 5 such rows on disk.
4. The tester **honestly FAILED** the draft (confidence 0.95, ready_for_execution false) —
   because `_scraper_has_real_items` counts a row on `any(p.get(f) for f in fields)` with
   `fields = ["price","availability","currency"]` and the template-defaulted `currency:"USD"`
   is truthy, the junk rows read as "real items" at every deterministic layer, and the
   ground-truth override blessed the run. (Here that layer disagreed with the LLM tester —
   the junk rows defeated the honest FAIL too.)
5. Finalize's rescue summed all 9 in-window cycle outputs (5+0+5+0+0+…) = **15** while the
   named evidence file holds **5**, and completed the job — recording 15, shipping 5 junk
   rows. `[SCOPE-SHORTFALL]` is wired only at the override arm, never the rescue arm.

The one working chain (live-verified): `solidusSlug` → `https://www.wonderbly.com/personalized-products/<slug>` → **200**, JSON-LD `"price": 29.99`. PDP shape confirmed via
sitemap.xml. The raw material for a real extraction was in the writer's hands the whole time.

## Root causes

| # | Defect | Site |
|---|--------|------|
| RC1 | **Writer brief FORBIDS Algolia discovery.** Fires on `site_analysis.algolia.detected` and says "Use Algolia as a **per-product lookup** … Do NOT implement discover mode … Products come from `input_urls.json`, not Algolia discovery". For a client-rendered listing, input_urls.json is seedable only from SSR anchors — i.e. junk. | `webapp/agents/subagents.py:3888-3901` |
| RC2 | **No catalog-discovery helper exists.** `probe_tools.py:956-984` measures `listing_client_rendered` (it never reaches the writer brief); no template or `src/` module can convert Algolia hits → PDP URLs; strategy escalation never reaches `internal_api` (`graph.py:5211-5212`). | templates/* (nothing), `src/listing_discovery.py` (HTML-only) |
| RC3 | **Junk rows pass the real-item bar.** `any(p.get(f) for f in fields)` — bare truthiness of ANY ONE filter field; template-defaulted `currency:"USD"` alone counts a row as real. Bare truthiness also still mis-reads tatcha's genuine float `0.0` at these three sites (F1 fixed `has_substantive_field` but not this predicate). | `webapp/agents/nodes/route_after_testing.py:722` (+ copies `:698`, `:769`); `graph.py:624-629` (`_patch_scraper_output_filter`); `run_execution.py:2062, 2108, 2278` |
| RC4 | **Rescue credits a sum, not the named file.** `_real_items_evidence` sums every in-window cycle file (source_total/fm_total → `_consider`); finalize records that sum as `product_count` while `output_file` points at ONE file holding fewer rows. Wave-49 pinned this as "deliberately not changing" — 1116 is the fresh prod instance that changes it. | `webapp/scraper/tasks.py:1845-1888` (`_good_rows` callers), `:2079/:2113` (sum sites) |
| RC5 | **Rescue arm never marks shortfalls.** `_scope_shortfall_row` fires only at the finalize override arm; the rescue completion arm completes asked-10-shipped-5 silently (wave-49 finding; 476 jomashop 2/5 + 484 teva 1/2 are its regression targets). | `webapp/scraper/tasks.py` rescue arm at `_rescue_fires` call site (`:1651`) |

## Fixes (no env gates — the reviewed merge is the switch; merge = activation)

### T1 — `src/algolia_discovery.py` (the catalog-discovery helper)

Single implementation, imported by templates (the `src.listing_discovery` precedent —
templates already do `from src.listing_discovery import discover_listing_urls_with_retry`;
scraper_runner re-adds PROJECT_ROOT to PYTHONPATH).

- `extract_algolia_creds(html) -> dict | None` — `{app_id, api_key, index_name}` from
  (a) astro-island `props` attributes (HTML-escaped JSON tuples `[0,"value"]` — verified
  shape: `props="{&quot;appId&quot;:[0,&quot;W3CI0YBYUX&quot;],&quot;apiKey&quot;:[0,…]}"`),
  (b) `algoliasearch('APP','KEY')` / InstantSearch inline config; index from
  `indexName`/`index_name` props, else discovered from the multi-query
  `results[0].index` echo at first call.
- `discover_catalog_urls(base_url, creds, max_items, query="", filter_str="", fetch_fn=None) -> dict`
  — paginated multi-query (`POST https://{app}-dsn.algolia.net/1/indexes/*/queries`,
  `X-Algolia-Application-Id`/`X-Algolia-API-Key` headers, `hitsPerPage=250`, page walk until
  collected ≥ max_items / page empty / page repeats / MAX_PAGES); hit→URL via wide key
  fallback **`solidusSlug, prismicProductSlug, productUrl, product_url, url, slug, handle,
  permalink, pathname`**, objectID last; absolute URLs pass through, bare slugs get a prefix:
  sitemap grep (`{base}/sitemap.xml`, cached once — first loc containing the slug wins its
  directory prefix) then HEAD-verify of a small candidate set, winner cached for the run;
  per-URL verify budget capped (never one HEAD per hit when the prefix is already proven).
  Returns `{"urls": [...], "nbHits": int, "collected": int, "stop_reason": str, "pdp_prefix": str}`.
  Never raises; degrade to `{"urls": []}` on any shape surprise.
- `build_taxon_filter(creds, taxon_query, fetch_fn=None) -> str` — facets=* probe →
  `(taxon_path, category_path, taxonomies, categories, taxon)` value match →
  `filter:taxon_path:"Kids > Age > 0-3"` (the prod draft's `_discover_taxon_facet`,
  generalized); "" when nothing matches (unfiltered query is correct for firstn scope).

TDD with the recorded live payload (`tests/fixtures/algolia/wonderbly_page0.json` — 10 real
hits captured from the live index; creds in tests are synthetic, real creds are extracted at
runtime from site HTML and are never committed). Chain the tests pin: creds extraction →
discover → 10 URLs of shape `/personalized-products/<solidusSlug>` → prefix from sitemap
stub → second page walk stops on `stop_reason`.

### T2 — Wire the helper in + flip the writer brief

- `templates/http_navigation_scraper.py`: in Phase-1 discovery, before HTML anchor
  discovery: fetch listing → `extract_algolia_creds` → `build_taxon_filter` (when a taxon
  prop is present) → `discover_catalog_urls`; ≥1 URL → those are the item URLs
  (`stop_reason="algolia_catalog"`); zero/exception → fall through to existing discovery
  untouched. `api_scraper.py`/`requests_scraper.py` get the same pre-step where they do
  discovery.
- `subagents.py:3888-3901` brief flip: when `algolia.detected` AND
  `probe_result.listing.listing_client_rendered` → prescribe catalog discovery (use the
  template's Algolia block / `src.algolia_discovery`, paginate, wide-key fallback named
  explicitly, "the listing page is client-rendered: SSR HTML has no product links — nav
  anchors are NOT product URLs"); drop the "Do NOT implement discover mode" line in THIS
  branch only (keep per-product-lookup guidance when not client-rendered).
- Inject the client-rendered fact + creds presence into the writer message so the
  prescription is grounded in measured evidence, not a guess.

### T3 — Core-value real-item bar (junk rows stop passing; tatcha zeros stay data)

One semantic: **a row is real iff `_field_has_value` on ≥1 filter field, where filter fields
exclude `_ALWAYS_PRESENT_FIELDS`-class defaults.** `currency` joins `_ALWAYS_PRESENT_FIELDS`
(`src/content_types.py:496`) — every template defaults it (`http_navigation_scraper.py:1253`,
`playwright_scraper.py:231`, `api_scraper.py:209`, `navigation_scraper.py:673`), so it is
definitionally always-present; `output_filter_fields` then yields `["price","availability"]`
for products and every call site inherits the narrowing.

- `route_after_testing.py:722 / :698 / :769`: `any(p.get(f) for f in fields)` →
  `any(_field_has_value(p.get(f)) for f in fields)` — float 0.0 counts (tatcha), "" doesn't
  (wonderbly junk).
- `graph.py:624-629` `_patch_scraper_output_filter` cond: drop `currency` from the
  disjunction; price/availability through `_field_has_value`.
- `run_execution.py:2062` (`_substantive_item_count`), `:2108` (`prune_empty_records`),
  `:2278` (`_item_has_core_field`) + the hardcoded fallback lists `:2048-2054 / :2098-2104 /
  :2271-2277`: same semantics, `currency` out of the fallback lists.
- Pinning tests to update: `tests/test_f15_ground_truth.py:94-97` (`test_availability_only_counts`
  keeps passing — availability survives; ADD a currency-only row test asserting it does NOT
  count), `test_wave49_honest_recording.py:43-66`, `test_f8_f16_output_selection.py:81-100`,
  `test_job73_freshness_and_noop.py`, `test_job71_output_gates.py:119-151`,
  `test_wave36_filter_marker.py:60-130` — each touched fixture re-checked: rows carrying
  price/availability keep counting; only currency-only rows change verdicts.

### T4 — Rescue-arm honesty (count what the named file holds; mark what ships short)

- `tasks.py` `_real_items_evidence`: the credited count becomes the **named file's**
  core-value row count (max single file, not the per-source sum) — `product_count` and
  `output_file` then always describe the same bytes. Books-473 class records 5, not 15.
- `_good_rows` (`:1845`): row predicate joins the T3 semantics
  (`_field_has_value`, always-present fields out).
- Rescue arm (the `if _rescue_fires(...)` block at `:1651`): append
  `_scope_shortfall_row(...)` when the rescue count < the job's requested count — RC5;
  regression targets 476 (5-asked/2-shipped) and 484 (2/1).
- `test_770_finalize_completes_with_19` stays green (single 19-row file → named-file count
  is 19). Wave-49 `TestFmRescueWindow` pins re-checked: F3 window semantics unchanged — only
  the aggregation across files changes.

### T5 — E2E: results, not labels

1. Local refire wonderbly (navigation/listing, firstn/10): expect **COMPLETED with 10 REAL
   products** — every row a `/personalized-products/<slug>` URL with a parsed price (29.99
   class). This is the owner's bar.
2. Tatcha regression (firstn/5): still COMPLETED 5/5 — float 0.0 rows still count.
3. Briscoes regression (firstn/5): still 5/5 real NZD (Magento PWA path untouched).
4. Full suite + `ruff check webapp/ src/` (lint gate covers webapp/ src/, never tests/).
5. Report outcomes to the owner with row-level evidence (titles + prices, not counts alone).

## Blast-radius guards

- `_always_present` narrowing is registry-derived — article/job/forum types unaffected
  (their core fields never included currency).
- T1 only activates where creds are extractable AND the helper yields URLs; every failure
  mode degrades to the pre-wave-50 discovery path (fall-through, never raise).
- T4 changes recorded counts ONLY where they were incoherent (sum > named-file rows).
  Cases where a single file holds ≥ all others are byte-identical to today.
- No env vars, no new settings; tests stub the network via `fetch_fn` injection.

## Execution findings (T5, 2026-09-29) — T5b corrections

The first e2e (local job 485: wonderbly list_page firstn/10, mirror of prod
1116) exposed that BOTH T2 brief gates were dead on the exact site the wave
targets:

1. **Dead key** — the brief read `probe_result["listing"]`; the advisory
   listing probe is written at `probe_result["listing_connectivity"]`
   (graph.py:2381). Fixed to the canonical key.
2. **The narrow gate never fires on wonderbly itself**:
   - `site_analysis.algolia` is LLM-authored and was EMPTY for job 485
     (nothing deterministic writes it);
   - `listing_client_rendered` was False because the wave-48 anchor counter
     counted **55 nav/collection anchors** on the 142 KB shell — the
     zero-anchor heuristic only proves the class when the shell carries no
     same-host links at all. Tightening the counter is a wave of its own
     (item_link_count feeds wave-48 known-yield-gap routing); NOT done here.
   - strategy was `http_requests` (fingerprint-proven PDP → wave-28 rule), so
     the http_navigation template — and the T2 pre-step — never entered play.

**T5b fixes (all deterministic, no env gates):**
- Brief: canonical `listing_connectivity` key + a third branch — any
  discovery-mode job (navigation/list_page/search_term) whose listing is
  HTTP-reachable gets the CHECK-FIRST prescription (extract creds → prefer
  catalog → fall back to anchors). url_list jobs get nothing.
- `templates/requests_scraper.py` now carries the same pre-step: imports
  `src.algolia_discovery`, `_discover_urls_via_algolia` rides
  `create_fetch_text` (the SAME proxy/fingerprint ladder — wonderbly's
  listing needs the fingerprint tier; a bare requests.get would be blocked),
  and a single `_discover_phase1` gate routes BOTH main() discovery arms
  Algolia-first with anchor discovery as the untouched fallback.
- `tests/test_wave40_real_items_evidence.py` 762-shape assertion superseded
  (wave-50 named-file honesty): 4 files × 1 row now credits 1 (the named
  file's rows), not the old sum of 4 — crediting the sum while naming one
  file is precisely the job-1116 vice.

**Pinning:** 3888 passed / 4 failed on the full suite at wave-49 HEAD + T1-T4;
all 4 verified pre-existing at clean HEAD via `git stash` (truncation cap,
admin job-visibility, read-file paging, wave-16 dead-tester probe — none
touch wave-50 surfaces).

## Critique / regression-risk review (2026-09-30, owner ask: "so that it doesn't break what already works")

Every wave-50 surface was traced through its downstream consumers. Verdicts:

**R1 — algolia-path coverage dict vs `listing_yield_failure` (RESOLVED, no bug).**
`_discover_phase1` returns `{"stop_reason": "algolia:…", "max_pages_hit": False}`
with no `discovered_urls`/`found`. The shared dead-listing predicate
(`src/listing_discovery.py:66`) treats a missing `discovered_urls` as raw zero
→ would verdict "dead listing" on a WORKING catalog. Traced the consumer chain
before patching: the template's main() does NOT propagate that dict — it
re-derives the contract §1 block from `len(product_urls)` / `len(results)`
(requests_scraper.py:639-648), so the output always carries
`discovered_urls: N, found: N` and the predicate returns False. Residual:
`"algolia:*"` stop reasons never match `_EXHAUSTION_REASONS` (exact set
membership) — with found=0 and >2 URLs (discovery worked, PDP fetches died —
the jcrew-1107 disease) the RC1 listing fallback correctly stays OFF: that is
an access problem the strategy ladder owns, not a dead listing.

**R2 — extra listing fetch on every discovery run (ACCEPTED).**
Currently-passing SSR sites pay ONE extra listing fetch (+1 ladder walk) per
discovery before anchor fallback. Same URL, same ladder, delay-delimited —
rate-limit risk negligible; latency +~2-5 s on a minutes-long Phase 1.

**R3 — false-positive creds could replace working anchor discovery
(ACCEPTED with backstop).** `extract_algolia_creds` takes the first
app/key/index it merges; a multi-index page could theoretically yield a
non-product index whose hits convert to non-PDP URLs — and the algolia gate
does NOT filter through `PRODUCT_URL_RE` (anchors do). Deliberately NOT
hardened with the regex filter: the writer sets PRODUCT_URL_RE from anchor
evidence; on a client-rendered listing that evidence is junk, so filtering
catalog hits by it would discard real PDPs and push back to the junk anchor
path — the filter CAUSES the regression it guards against. The real backstop
is T3: a wrong-index discovery produces rows with no price/availability → the
core-value bar fails the draft → retry loop regenerates. Caught, not silent.

**R4 — brief over-rotation on SSR sites (PINNED).** CHECK-FIRST step 4
preserves anchor discovery as a first-class fallback; creds-absent sites are
byte-identical in behavior (pre-step degrades to `algolia_unavailable` →
anchor path). url_list jobs never see the section (test-pinned), and a
detected-algolia url_list job keeps the lookup-only branch (test-pinned).

**R5 — T3 core-value bar vs sparse-but-legit rows (PINNED safe).** The bar is
`any(_field_has_value(row.get(f)))` over price/availability/author/… — it only
removes the defaulted-currency loophole (templates default-fill currency, so
it could not certify a row). OOS rows carry availability; article/job rows
carry content/author/company — none can newly fail. Title-only rows were
ALREADY pruned by the job-10 rule; T3 changes nothing for them.

**R6 — T4 rescue honesty (SUPERSEDING CONTRACT).** The wave-40 762 assertion
(sum of 4 files) was updated to the named-file count (n==1). Any external
consumer summing rescue files would see the same count change — searched: the
sum existed only in that test. No production path summed rescue files.

**R7 — blast radius of template edits (PINNED).** The pre-step is
failure-degraded by construction (every failure → `([], "algolia_unavailable")`
→ the exact pre-wave-50 anchor path). The only shared-path edits are the gate
wrapping two main() call sites; anchor discovery code, retry ladder, coverage
block, CLI contract untouched. Test-pinned by `TestRequestsTemplateWiring`
(gate ordering, main() routing) + the 11-test brief suite.

## Prod job-1107 RCA (jcrew.com) — 2026-09-30 (deep-agent, evidence-pinned)

**Verdict: false success, doubly dishonest.** COMPLETED/170 is 20+50+50+50 —
the SAME ~50 sweaters counted across four retry-cycle files by pre-wave-49
`_real_items_evidence` summing — attached to an `output_file` the same
finalize deleted. The job's real work was real: a 50-row http_navigation draft
(extraction verified 3×, 10:30–10:59Z), correctly judged NEEDS_FIXES by the
tester on all four cycles, then destroyed by its own recovery machinery:
a no-op fix cycle forked TWO concurrent graph executions (two writers 40 s
apart, seq 44/60); cleanup #1's `mkdir -p` invented a celery-local
`scrapers/jcrew-com/`; the FM newest-5 prune evicted every row-bearing output
in favour of five `phase:"discovery"` smoke-probe stubs;
`_resolve_rescue_file`'s local-path fallback recorded the unresolvable
`/app/...` path (the 404). Phase 2 never "failed" — post-11:47 extraction
attempts died on the tester's 300 s cap (5 cloak-browser PDP loads ×
NAVIGATE_TIMEOUT=120 cannot fit). `firstn/10` never bound because scope is
injected only in `run_execution`, which never ran.

**Fix split:**

[already-fixed at HEAD — do NOT re-fix] the 170 itself (T4 per-file max +
`test_output_file_always_describes_the_recorded_count`), stale-FM phantom rows
(wave-49 F3 payload-first), zero-item honesty on the failure path (wave-48 T6).

[new, wave-51 candidates — each with blast-radius guard from the RCA]:
- **N1** FM prune: exempt zero-row `phase:"discovery"` outputs from the
  keep-5 prune (publish stays unconditional; skip only parsed-empty
  discovery files; pin: row-bearing keys survive a 6-file prune).
- **N2** `_resolve_rescue_file`: return the local path only when
  `Path(locator).is_file()`; else the bare `scrapers/<slug>/<name>` key
  shape (dev bind-mount keeps working; prod never records dead paths).
- **N3** per-job finalize idempotency keyed on the `completed_at`
  compare-and-set, NOT task entry (resume re-entry must keep working) —
  contains the concurrent-execution fork without touching Command routing.
- **N4** cap the Phase-1 smoke probe with `--limit scope_value` (tester runs
  stay unbounded — job-88 selfridges waiver); `_accepted_cli_flags` guard
  already exists.
- **N5** runtime completion assertion at finalize: output_file resolves AND
  row count == product_count; "unreadable" → keep count + `[RECORD-UNVERIFIED]`
  note, "mismatch" → correct the record (never fail a good job on an FM 500).
- **Circuit breaker** on the fix loop: zero substantive rows for N=3
  consecutive cycles WHILE discovery keeps yielding >2 real URLs → honest
  FAIL with the access diagnosis, not another regeneration; exempt
  increasing-yield sequences (1107's 20→50→50→0 collapse is the signal).
- **Ticket**: SessionLog seq values are not unique (70 dupes in 1107;
  `?since_seq=` pagination silently drops 68 rows) — monotonic per-batch
  allocator behind a migration.

**Wave-50 guard added from the RCA critique:** the CHECK-FIRST brief must
fire only on a real HTTP-tier listing win. Already structural — probe
`http_ok = _is_http_method(winner)` admits direct_http*/fingerprint_* only
(probe_tools.py:946), so cloak-only winners (jcrew: cloak_none) are excluded
by construction — now pinned by
`test_cloak_only_probe_gets_no_check_first_section` (12/12).

## T5c — post-RCA critique fixes + suite triage (2026-09-30)

Two real items surfaced by the critique pass (the rest of the 66-failure
suite run was job-488 container contention — every sampled cluster passed in
isolation):

1. **CHECK-FIRST tier predicate pinned.** The deep-agent critique asked
   whether "HTTP-reachable" could fire on a cloak-only winner (jcrew class:
   budget burned hunting creds that don't exist). Verified structural:
   probe `http_ok = _is_http_method(winner)` admits direct_http*/fingerprint_*
   only (probe_tools.py:946) — cloak rungs are browser-flavoured and excluded.
   Pinned by `test_cloak_only_probe_gets_no_check_first_section` (12/12).
2. **`test_both_discovery_call_sites_use_the_shared_module` superseded** —
   it pinned both main() arms assigning `= discover_listing_urls_with_retry(`
   directly; they now assign `= _discover_phase1(` and the shared-module call
   lives once inside the gate. Same invariant, one level deeper. Plus the f6
   subprocess budget 120s → 300s: the env-gate arm now walks the ladder twice
   on a dead listing (pre-step creds fetch + anchor walk) — the env-gate
   assertions themselves are unchanged.

## T5d — the 488 e2e RED and the gate repair (2026-09-30)

**488 verdict: COMPLETED/5 — RED against the bar.** The 5 rows were the
job-1116 vice again: collection links (`/personalized-books/new-in`,
`/personalized-books/kids`…) recorded as products, all priced a defaulted
5.0 USD. The tester SAW it ("I notice the discovered URLs include
/personalized-books/new-in…") and passed them anyway (core-value bar counts
fields, not URL shapes).

**Root cause — the third layer of the disease (LLM compliance).** The
CHECK-FIRST brief WAS live in 488's writer context (verified at seq 74) and
the writer read `src/algolia_discovery.py` twice — then still GUTTED the
gate: the draft's `_discover_phase1` kept its name but lost the pre-step and
the import entirely, and its `PRODUCT_URL_RE` blessed the listing's own
collection tree (`^…/personalized-books/[^/?#]+/?$`). After the dead brief
key (T5b-a) and the template bypass (T5b-d), the brief alone cannot be the
enforcement layer.

**T5d fix — deterministic, same doctrine as wave-37's ladder repair:**
- `agents.draft_safety.algolia_gate_prescribed(state)` — THE shared
  CHECK-FIRST predicate (mode ∈ discovery ∧ (lcr ∨ http_ok)); the writer
  brief now delegates to it, so brief and guard cannot drift.
- `agents.draft_safety.repair_algolia_gate_violation(draft, template)` —
  splices the render-safe `_discover_urls_via_algolia` + algolia-first
  `_discover_phase1` + the parenthesized `src.algolia_discovery` import back
  from the requests template. Honesty guards: unwired-but-compliant drafts
  are None (idempotent), unparseable None, no-gate-shape None (the
  http_navigation template wires the pre-step per-branch in main() — no gate
  fn — so that shape honestly declines rather than splicing into main()).
- Wired in `_invoke_code_tester` beside the ladder repair, budget 1 per job
  (`state.gate_repairs`, B2 semantics — spent win or lose), BEFORE the entry
  fingerprint so the repaired bytes are what the pass tests.
- 25 tests in `tests/test_wave50_gate_repair.py` + the 12-test brief suite,
  all green; draft-corruption/empty-discovery/core-value/rescue/762 suites
  re-green post-change.

## T5e — the generic nav-tree junk bar (2026-09-30)

The 488 e2e shipped 5 nav/collection links as products **with core fields
present** (nav titles + the template's defaulted price) — the F9 emptiness
gate and the T3 core-value bar both pass such rows. T5e is the backstop:
`src/discovery_quality.shallow_section_links` fires when ≥80% of ≥3
same-host discovered URLs share the listing's FIRST path segment AND sit
strictly shallower in the tree (real item pages hang below their listing;
menu links hang beside/above). Wired as `_shallow_section_gate` in
run_execution, its own FAILED verdict after F9 — it fails the run, never
edits rows (honest FAILED beats junk shipped as COMPLETED).

**Scope: list_page ONLY.** First draft scoped all discovery modes; real job
data showed search_term jobs carry deep submitted URLs (15 of the last 19),
whose anchor is not necessarily the page discovery walked — a depth
comparison there could false-fire on sites whose real PDPs sit in a flat
namespace under a shared first segment (e.g. `/shop/<cat>/<sub>` listing,
`/shop/<slug>` PDPs). navigation/search_term/url_list keep their own junk
nets (T5d gate, core-value bar, F9), none of which depend on URL depth.

Known-safe shapes pinned in tests: `/collections/all` → `/products/<slug>`
(different first segment), `/c/<cat>` → `/p/<slug>`, deep PDPs under a
shared prefix, mixed sets below the 80% share, root-URL listings
(structurally no-op: nothing is shallower than depth 0). Equal-depth
siblings (`/collections/all` → `/collections/sale`) are deliberately NOT
caught — that is the T5d gate's job. False-positive cost = one honest
FAILED + a named diagnosis in the fix loop; the module contains no site
names (enforced by test).
