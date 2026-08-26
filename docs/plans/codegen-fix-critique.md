# Codegen Fix Critique — adversarial pass on the 7-item plan

**Critic:** read-only. Grounded in code + on-disk artifacts (no test runs).
**Inputs:** `codegen-regression-analysis.md`, `codegen-contract-audit.md`, and fresh verification of every
code path the plan touches.

---

## VERDICT

**SHIP-WITH-CHANGES** — the plan's targets are all real, but two fixes (2 and 3) as written would break
true-positive sites that already exist in `scrapers/`, one fix (1) has an expensive arm that should be a
last resort, and the plan misses the actual root cause of the ketch leak: a **substring tokenization bug**
in `api_from_network`, not a domain problem.

### The new fact that reframes Fix 2 (verified)

`_API_HINT_TOKENS_NET` (`experimental/nav_traversal/traversal.py:916-917`) matches by raw substring.
The ketch URL contains `production`:

```python
"product" in "https://global.ketchcdn.com/web/v3/.../production/default/en/config.json"  # True
```

That is the *entire* reason ketch became a candidate. Every hint token there is substring-matched
(`traversal.py:1015`). Word/path-segment matching was tested against real endpoints:

| Endpoint | substring (today) | path-segment (fixed) | verdict |
|---|---|---|---|
| `global.ketchcdn.com/.../production/default/en/config.json` | `product` ✓ | — | **killed** |
| `api.ayahealthcare.com/AyaHealthcareWeb/job/search` | ✓ | `search`, `job` | kept |
| `api.priceline.com.au/occ/v2/priceline/products/search` | ✓ | `search`, `product` | kept |
| `abc-dsn.algolia.net/1/indexes/products/queries` | ✗ today | `product` | **kept (newly)** |
| `js.klevu.com/2.0/search/company/search` | ✓ | `search` | kept |
| `lw.com/coveo/rest/search` | ✓ | `search` | kept |

The fix makes the false positive impossible *and* widens true-positive coverage (Algolia's
`/indexes/products/queries` currently fails the substring test). This is strictly better than domain
rejection, which is *strictly wrong* here — see Axis A.

---

## Per-fix table

| # | Original proposal | Hardened / generic version | False positive now avoided | Speed |
|---|---|---|---|---|
| **1** | Corrupt analysis → UNREADABLE note *or* route to missing-artifact rerun; never silently `{}` | Three arms, ordered by cost. **(a)** `_fix_json_artifact` (`graph.py:122-141`) validate-*before*-write — today `f.write(fixed)` precedes `json.loads(fixed)`, so the mangled file is left on disk. **(b)** Reuse `_safe_json` (`traversal.py:505-530` — already exists for exactly this shape) as a second salvage pass for unescaped-quote corruption, the class the current backslash regex cannot touch. **(c)** Only when both fail: `_summarize_product_analysis` emits an UNREADABLE block *instead of* `""` — the anti-read_file note lives inside `pa_summary`, so an empty summary also deletes that guard (regression doc §3.3). **Rerun is NOT the default arm** — see Axis C. Gate keys on *parse failure only*, never on size or field-count. | Thin-but-valid artifacts (adameve: sparse JSON-LD; simple url_list sites where `fields` is small) never trigger a rerun. A valid artifact with zero mapped fields is `validate_analysis`/`validate_coverage`'s business, not a corruption gate's. | (a)+(b)+(c): **0 LLM calls, ~100 prompt tokens**. Rerun arm: +1 full product_analyzer phase (`PRODUCT_ANALYSIS_BUDGET` 50/70/70) — minutes to ~9 min, and it re-rolls an LLM whose previous output was corrupt. |
| **2** | Reject `count:null` + cross-registrable-domain endpoints | **Drop the domain reject.** Keep three narrow signals, all at `verify_api` (`traversal.py:472-502`) — *not* at the `graph.py:2746-2786` override gate: (i) word/path-segment hint tokens in `api_from_network`; (ii) blocklist path keywords `/config`, `/consent`, `/privacy`, `.min.json`, `/collect`; (iii) check `sample_keys` — `verify_api` already captures it and never inspects it; a consent-config key set (`{organization, property, …}`) is trivially disjoint from a record schema. Tighten the override gate to `count is None and items_per_page < 5 → no assert`, not a hard count-null reject. | **amnhealthcare**: its real jobs API is `api.amnhealthcare.io` — cross-registrable vs `amnhealthcare.com`, `count: null` on disk (`scrapers/amnhealthcare-com/analysis/navigation_analysis.json`). Domain+count rejection as proposed kills a true API. The repo already reverted a same-site filter once for exactly this class — `traversal.py:144-160`: *"cross-domain links are intentionally ALLOWED… Workday portals (myworkdaysites.com), Algolia (algolia.net)… the same-site filter dropped the only relevant link (kirkland dead-ended here)"*, and `:344-346` calls third-party APIs "a separate, rarer case". **aya's taxonomy endpoint** (`wp-json/.../joblookups`, 2217 records, `count:null`) is precisely why the count-based *scorer* exists — a hard reject would discard the ranking that already beats it. | **Free** (regex + key-set check, zero extra fetches). **Net gain**: a false `internal_api` sends the writer down an api template it abandons mid-flight — job 10's improvisation. Killing it removes a retry class. |
| **3** | Inject prior scraper + prior count into the writer message; tester FAILs at <25% of prior | **Never inject the scraper file.** Inject a 3-line stat from the DB — `Job.product_count` / `Site.product_count` (`models.py:140,410`) already exist; one query, ~40 tokens vs a 16-24 KB file (~4-6 K tokens) into a 120 K-budget agent with a documented ballooning failure. Gate shape: **compare substantive counts on both sides** via `_substantive_item_count` (`run_execution.py:1053`), **only when both runs are full-scope**, in **bands** — FAIL `< max(50, 10% of prior)` *only if* discovery telemetry independently confirms underperformance (stop_reason gave-up, or `found << expected_total`); WARN 10-40%. | **scope=firstn**: `run_execution.py:424-432` passes `--limit N` — a 5-item sample after a 3,616 run is 0.14% and hard-FAILs. **code_tester's own run** is capped at `--limit 50` (`subagents.py:3246/3276`) — it can never measure "≥25% of 3,616". **A bloated prior baseline**: job 9's 3,616 included delivery pseudo-SKUs; a *cleaner* 2,500-row run reads as regression against raw counts. **First-run sites**: gate must stay dormant (no prior). | Injection: **free**. FAIL arm costs +1 code_writer retry cycle (minutes + tokens) — which is why it needs the telemetry corroboration before firing. |
| **4** | Output filter fails loudly when `OUTPUT_KEY` missing; summarizer never returns `""` | Confirmed: only 4 of 9 templates define `OUTPUT_KEY` (`api`, `requests`, `shopify`, `undetected_chromedriver`, `dagster` do not) — the injected filter `NameError`s into `except Exception: pass` (`graph.py:262-281`) for **five** families. Fix: substitute the **literal** output key from `src/content_types.py` (`cfg.output_key` — `products`/`jobs`/…, already generic) when the draft lacks the symbol, and log on the except. The summarizer half is Fix 1(c) — same change. | Any api/requests/shopify/uc draft whose author defined its own key name — injecting a literal derived from the content-type registry works for all 11 page types with no per-site logic. | **Free.** Deterministic AST/string check. |
| **5** | api template `src_url` = discovery page; emit `discovery_coverage` | **Not a hard rule — env-first fallback**, one line: `src_url = os.environ.get("SCRAPER_LISTING_URL","").strip() or f"{API_BASE_URL}{API_PRODUCTS_ENDPOINT}"`. `run_execution.py:399-419` already sets `SCRAPER_LISTING_URL` for `navigation|list_page|search_term` (F17 domain-guarded); it is absent for `url_list`. This is exactly what job 9's LLM-written scraper did (regression doc §1.2) — the template just never adopted it. For `discovery_coverage`: emit it, but define **api-family stop reasons** (`total_reached`, `short_page`, `query_sweep_done`) — `_COVERAGE_FAIL_STOP_REASONS` (`route_after_testing.py:76`) is `{navigate_error, dedup_flat}` and stays inert otherwise. | **url_list via API** and **pure search APIs** have no listing page — a hard "src_url = discovery page" rule produces a fabricated URL or breaks. Job 10's `src_url = url` (self-citation) and aya's `src_url = None` across 26,871 rows are *both* wrong-but-different shapes; env-first handles the first and leaves the second to a normalize-only check, not a FAIL. | **Free.** Template edit + metadata block. |
| **5b** | Restore catalog-coverage guidance for api jobs | **Change the mechanism, not just the content.** `subagents.py:2762` (`navigation_section = api_section`) *replaces* the nav block. The de-bloat rationale was avoiding two OPPOSING instructions (HTTP API vs drive a browser) — correct — but the fix is to drop the *browser mechanics* (`_get_next_page_url` HARD RULE, click selectors) while **appending** the *coverage facts* the api_section never carries: `item_links.url_examples` categories (20 category URLs exist in the artifact and are discarded), `discovery.listing_url`, and `pagination.type`. Job 10's writer invented `DEFAULT_QUERIES` from nothing because it received zero enumeration input. | No false positive — this is additive prompt text. But note the dependency: it must land **with** Fix 2 or a rejected endpoint yields *neither* section (see Axis C). | **Free**, ~15 prompt lines (~300 tokens) — offset by removing the invent-and-retry loop it prevents. |
| **6** | Row-level emptiness check in `run_execution` (>X% blank → fail or prune) | **Prune, don't fail**, and split by schema origin: when the job has a requested schema, use its `target_fields` predicate; when not, use `output_filter_fields(content_type)` (core minus `_ALWAYS_PRESENT_FIELDS`, `content_types.py:468`) and **prune + WARN + record `pruned_rows` in metadata**. Escalate to FAIL only at the existing 80% collapse line. Separately fix the **zero-stub** hole: `any(item.get(f))` counts `ratings: "0.00 (0)"` as good (`run_execution.py:1053-1096`) — a non-empty-string-that-means-nothing passes both the hand-written and injected filters (audit L1-4). | **Honest sparseness**: `job_posting` lists `salary` as *optional* (`content_types.py:196`) — job boards with 60% salary-less postings; out-of-stock products with no price; forum threads with empty bodies. A blanket fail ratio punishes all three. Job 10's 30/68 (44%) sat *below even the 50% warn line* — pruning removes those rows regardless of ratio. | **Free** — one post-run pass over the output JSON. Failing instead would re-run a 10-60 min job. |
| **7** | Persist `response_metadata.model_name` to SessionLog | Ship as-is. `SessionLog` (`models.py:336-354`) stores role/agent/content only; the model string is env-reroutable (`CODE_WRITER_MODEL`, `litellm/` prefix kill-switch) and recorded nowhere, so the job-9-vs-10 model question is unanswerable. One field + migration + one line in `_persist_agent_logs` (`graph.py:648,825`). | None. Observability only. | **Free.** Zero accuracy effect by itself — it makes the *next* regression diagnosable. |

---

## Axis A — Genericity: where each fix misfires (condensed)

- **Fix 2** is the plan's genericity hazard. Real ecommerce runs its catalog on third-party SaaS
  legitimately: Algolia (`{APP}-dsn.algolia.net` — a repo skill documents it as the *primary* endpoint,
  `.opencode/skills/algolia-detection/SKILL.md:110`), Klevu, Bloomreach, FastSimon, Kibo, Workday
  (`myworkdaysites.com`), and this repo's own amn (`api.amnhealthcare.io`). Cross-domain rejection
  converts every one of those into a lost true API — the same regression the codebase already suffered
  and reverted (`traversal.py:144-160`). **The line that actually separates ketch from a catalog API:**
  (1) path/segment tokens, not substrings; (2) `/config|/consent|.min.json` path shapes; (3) `sample_keys`
  shape; (4) `count:null` **combined with** `items_per_page ≤ 5` *and* a config-ish path — never `count:null`
  alone, because amn's true API is count-null on disk and aya's taxonomy false-positive is already
  out-ranked by the existing `has_count` scorer (`traversal.py:1096-1101`).
- **Fix 3** misfires on scope (firstn `--limit`, tester `--limit 50`), on first runs, on shrinking
  catalogs, and on a degenerate-or-bloated prior baseline. The shape that cannot false-fire: substantive
  counts, scope-matched, banded, telemetry-corroborated.
- **Fix 6** misfires on every content type whose core fields are legitimately optional (salary, price on
  OOS, thread bodies). Per-content-type predicates already exist via `output_filter_fields` — use them,
  and prune.
- **Fix 5** misfires where there is no listing page. Env-first fallback covers both modes with no branch.
- **Fix 1** misfires if "corrupt" is read as "small". Parse-failure-only keys the gate to the real defect.

## Axis B — Speed & accuracy per token

Nothing here needs an LLM call. The two SLOW risks in the plan as written:

1. **Fix 3's "inject the prior scraper"** — a 16-24 KB file into the agent with a documented
   context-ballooning failure mode (memory: `code-writer-context-ballooning-rootcause`). The cheap
   deterministic version is one DB query (`Job.product_count`) + three lines.
2. **Fix 1's rerun arm** — a full product_analyzer phase, historically the timeout-prone one (myntra job
   51, calvinklein). Make the UNREADABLE note the default and the reron conditional on the note provably
   failing (first test fails).

Accuracy-per-token **gainers**: Fix 4 (kills blank rows at zero cost), Fix 2 narrowed (prevents a doomed
strategy diversion and the improvisation-retry that follows), Fix 1(c) (one paragraph that stops the
writer `read_file`-ing a 12 KB corrupt file — the cheapest single accuracy win in the plan), Fix 5b
(replaces an invention with the 20 category URLs already captured).

## Axis C — Interaction effects

- **Fix 2 + 5b (ordering matters).** If rejection happens only at the `graph.py:2746` override gate,
  `api_section` still fires — it checks nothing but `api_endpoint.url` truthiness (`subagents.py:2657`) —
  so the writer gets "use this API (PREFERRED)" while `strategy` stays `http_requests`: a *worse*
  contradiction than either alone. **Fix 2 must reject at `verify_api`**, the earliest point, so
  `api_endpoint` is never populated and the two-phase block (with the pagination HARD RULE job 10 lost)
  survives untouched. Then 5b is still needed for *true* APIs — aya-class jobs keep the swap and keep
  losing catalog guidance.
- **Fix 3 + 6 (ratchet).** If 6 prunes rows post-run and 3's floor reads raw counts, each run prunes a few
  more rows and every successor's floor drops. Compare substantive counts on both sides — the helper
  already exists.
- **Fix 1's rerun + budgets.** The missing-artifact path routes through the existing
  `budget_exhausted_* → missing_artifact_*` interrupt machinery (`graph.py:1459-1510`) and re-runs
  product_analyzer at budget 50/70/70. On heavy SPA sites that phase *is* the timeout class. Default to
  the note; escalate to rerun only on demonstrated failure.

---

## Ship first (highest accuracy-gain-per-effort)

1. **Fix 4** — `OUTPUT_KEY` literal + loud except. ~10 lines, kills the exact mechanism that shipped job
   10's 30 blank rows, zero genericity risk, zero cost.
2. **Fix 2 (narrowed)** — word-boundary/path-segment tokens + config-path blocklist + `sample_keys` check,
   applied in `verify_api`. ~15 lines. Kills the ketch class *and* newly admits Algolia-shaped endpoints.
3. **Fix 1 (cheap arm)** — validate-before-write + `_safe_json` salvage + UNREADABLE note. ~20 lines.
   Job 10's scraper was a pure function of a corrupt artifact; this is the largest single accuracy lever
   and it costs one paragraph of prompt.

## Replace entirely

- **Fix 2's "reject cross-registrable-domain endpoints"** → replaced by tokenization + path/shape
  rejection (above). On-disk counterexample: amn.
- **Fix 3's "inject the prior scraper"** → replaced by a DB-sourced prior-count/src_url-mode/strategy
  stat. The prompt bar at `.opencode/agents/code-writer.md:34` ("Do not read reference scrapers") exists
  for a reason; lifting it to inject 24 KB is the ballooning bug re-imported by another door.

## Notable miss in the original plan

`vistastaff`'s stored artifact (`scrapers/vistastaff-com/analysis/navigation_analysis.json`) carries a
`googleads.g.doubleclick.net/pagead/viewthroughconversion/...` URL as `api_endpoint` — a second
false-positive class, currently inert only because `data_source` is `None` there. The `_TELEMETRY_RE`
(`traversal.py:910-921`) matches `doubleclick` but did not prevent storage; whatever arm hardens
`verify_api` must also cover already-stored artifacts on the resume path, or a re-scrape will re-read the
poisoned `navigation_analysis.json` from disk and skip discovery entirely.
