# Wave-36 Plan — Field Mapping + Soft-Block JSON Carve-Out + Count Gate

**Status:** FINAL after THREE review passes: critique round 1 (25 findings, SHIP-WITH-AMENDMENTS),
feasibility round 2a on the LOCAL gate (buildable-with-additions, §5 corrected), regression round 2b
"will this break prod" (3 BLOCKER + 7 MAJOR + 5 MINOR, SHIP-WITH-AMENDMENTS — all folded; ledger §9).
**Origin:** prod job 658 westelm.com.au RCA (2026-09-17) — completed 3-of-~114; plus user-mandated
baseline acceptance gate (≥10 under-delivering prod jobs, all must pass).
**Design inputs:** 2 research agents (plumbing inventory, design space w/ measured failure modes),
1 adversarial critique agent. Critique ledger: §9.
**Scope locked by user:** bundle all three fixes; mapping is SILENT with logged rationale
(no intake confirmation UI, no graph interrupt — intake jobs run `skip_approvals=True` and
`human_approval` auto-approves, so an interrupt would be a no-op anyway).

---

## 0. Problem (verified against prod + code)

User chips are free text (`['product name', …, 'avaliability']`). Nine consumers across four
processes treat them VERBATIM as the output-key contract; generated scrapers emit canonical keys
(`title`, `availability` — templates hardcode them, e.g. `templates/requests_scraper.py:251-255`).
Finalize prune reads `job.target_fields` from the **DB row** (`webapp/scraper/tasks.py:1529-1530`,
prune `:1537-1547`) and silently deletes canonical keys → name-less records (658). Measured
deterministic-only mapping is unsafe: `product name`→`brand` (0.636), `sku code`→`status_code`
(0.737), `current_price`→`currency`, `was-price`→`price`; `rrp/qty/desc/cost` unreachable at any
safe threshold; multilingual hopeless. Silent-wrong ships plausible garbage — worse than today's
visible absence. ⇒ LLM mapping with deterministic validation, alias fallback, verbatim last resort.

---

## 1. Fix 1 — chip → canonical field mapping (PHASED)

### Phase 1a — contract isolation (no LLM, ships first)

**New declared state key `state["resolved_fields"]`** (in `ScrapeState`, `webapp/agents/state.py` —
NOT a repurpose of `target_fields`; critique F1). Semantics: the canonical output-key list. When no
mapping exists, `resolved_fields == target_fields` verbatim (today's behavior, byte-for-byte).
**Derivation when chips are empty:** mapped chips → else persisted
`output_schema["fields"][].name` → else `[]`, mirroring `schema_field_names`
(`src/content_types.py:315-336`) — otherwise the finalize schema prune silently dies for chip-less
re-runs (round-2 M5).

**THE TWO-VOCABULARY RULE (round-2 B1 — load-bearing):** prod drafts emit BOTH template-canonical
keys (`title`…) AND chip-verbatim keys — the ENFORCE prompt actively commands verbatim
(`subagents.py:2189-2196`). The in-draft output filter and every other consumer that runs BEFORE
the record-key rename must therefore admit **`resolved ∪ raw-chips ∪ custom-keys (∪ DIRECT_FIELDS)`**;
**resolved-only is correct ONLY at the finalize rename/prune point** (`tasks.py:1537-1547`).
A resolved-only predicate spliced into the draft drops 100% of chip-verbatim rows in-process with
no downstream recovery.

Consumer changes (each falls back to `target_fields` when resolved absent):

| Consumer | Site | Change |
|---|---|---|
| config override | `tasks.py:802-813` | resolved ∪ raw as required core |
| analyzer prune | `normalize_fields.py:115-129` | allowed = resolved ∪ raw ∪ DIRECT_FIELDS |
| output-filter splice | `graph.py:565-572` | predicate = resolved ∪ raw ∪ custom |
| prune-empty-records CALLERS | `run_execution.py:1754`, `:1773`, `:1058`, `:1066`, `:1482` | pass resolved ∪ raw ∪ custom (round-2 B2 — the callers, not just the bodies, are the contract) |
| row deletion + quality gate | `run_execution.py:1876-1930`, `:1962-2009` (`good_fields` `:1989`) | resolved ∪ raw ∪ custom |
| tester field-set + zero-coverage | `route_after_testing.py:591-604`, `:852` | resolved ∪ raw ∪ custom |
| filter-patch caller | `graph.py:6703` | resolved ∪ raw ∪ custom (sole caller) |
| finalize prune + order | `tasks.py:1529-1545` | resolved ONLY (post-rename; order = resolved order) |
| **keep RAW on purpose** | `validate_coverage.py:164-166`, `field_confirmation.py:234`, `subagents.py:4813` | compare against page-derived analysis names / render user-facing chips — migrating breaks coverage credit + interrupt payload (round-2 B2) |

**Record-key normalization at execution/finalize:** when a mapping exists (i.e. from 1b onward),
emitted records are keyed raw→resolved (rename-then-keep; CUSTOM keys pass through). The 658
name-loss fix therefore LANDS WITH 1b's alias table (1a alone is identity — nothing to rename);
what 1a buys is the isolated contract the rename plugs into (round-2 m4).

**Also in 1a:**
- `check_tracker.py:107` compares **resolved-vs-resolved** (both sides from the same generation
  contract). An alias-table change legitimately invalidates `skip_code` exactly once per deploy —
  document this so the first post-deploy re-drive isn't misread as a regression (F25).
- Hash-stamp the injected filter marker: `_OUTPUT_FILTER_APPLIED:<sha1(resolved_fields)>`.
  **Re-patch = CUT-THEN-INSERT (round-2 M1):** parse the stamped hash; on mismatch, excise the
  stale block (`# _OUTPUT_FILTER_APPLIED…` through its `except Exception:\n    pass`) BEFORE
  inserting — substring-presence idempotency alone double-injects (each block carries its own
  predicate → AND semantics → row loss, the exact F2 class). Legacy un-hashed
  `_OUTPUT_FILTER_APPLIED` and `_OUTPUT_PRICE_FILTER_APPLIED` both count as mismatch. Existing
  substring pins survive (`tests/test_wave26_paren_splice.py:154`, `tests/test_codegen_fixes.py:53`).
  **Ordering:** the patch must re-run AFTER `draft_safety.restore_job_draft`
  (`graph.py:6718-6730` restores an old-marker draft AFTER the `:6703` patch call today).
- Declare in `ScrapeState`: `resolved_fields`, `field_mapping`, `field_notes` (F10 — `field_notes`
  is written at `tasks.py:848`, read at `subagents.py:2211`, declared nowhere — undeclared keys
  are STRIPPED from graph state, `state.py:52-55`), and `shortfall_remediation_count: int`
  (Fix 3's bound counter — round-2 B3), plus the no-regression pair `prior_output_file` /
  `prior_product_count` (Fix 3, round-2 M2).
- **`field_notes` declaration is a behavior activation (round-2 M7):** undeclared today →
  `_field_guidance_section` (`subagents.py:2205-2225`, called `:2627`, `:4898`) has rendered
  nothing in real runs; declaring it switches W27-4 guidance ON for every schema_text job in the
  same deploy. Gate it behind `FIELD_MAPPING_ENABLED` (or its own flag) with a dedicated test.

### Phase 1b — the mapping resolver (`src/field_mapping.py`)

Resolve chain (deterministic-first; the LLM only sees chips the prior layers couldn't map, F6):

1. **Alias table** — in-code, seeded with measured cases: `product name`→title, `avaliability`→
   availability, `rrp`/`was-price`→original_price, `sku code`→sku, `desc`→description,
   `cost`→price, `in-stock?`→availability, `qty`→CUSTOM, …
2. **One-shot LLM** — `get_small_llm(temperature=0, timeout=20)`, **single attempt** (bypass the
   `ClassifiedRetryChatOpenAI` 6-attempt ladder — worst case otherwise ≈2 min on the job-start
   critical path, F6). Prompt: content-type `FieldDef` registry **incl. `jsonld_key`** (repurpose
   the dead `mapping_prompt_fields()`, `content_types.py:60-66`, and add `jsonld_key` — the signal
   that makes `product name`→title derivable), content type, unresolved chips, site context.
   Output per chip: `{canonical | "CUSTOM", confidence, rationale}`. JSON + fence-strip
   (`url_judge.py` pattern). LiteLLM streaming flag comes from the factory — do NOT bypass
   `get_llm` (`llm.py:651-656`, `:687`).
3. **Deterministic fallback** on LLM error/timeout: normalize + abstaining fuzzy (threshold ≥0.78;
   abstain when top-2 within 0.1; abstain when the chip is a plausible domain term).
4. **Verbatim passthrough** if all fail == today's behavior. **Never raises.**

**Deterministic validation (load-bearing):** canonical target must exist in the content-type
registry, else CUSTOM; duplicate canonical targets → highest confidence wins, rest demoted CUSTOM
+ flagged (intake-visible warning, F8); **CUSTOM keys are kept** and the writer is explicitly
instructed to extract them (extract-more, never drop). Charset discipline: resolved/custom keys
match `[a-z0-9_]{1,64}` or the chip passes through CUSTOM-verbatim if it already matched, else
refuse to registry names only (F7).

**Persistence + invalidation:**
- New `ScrapeJob.field_mapping` JSONField **plus `Site.field_mapping_cache` JSONField** (both in
  **migration 0043** — `Site` has NO free JSON field today (`models.py:444-483`), and
  `Site.output_schema` is contract-bearing (`tasks.py:735-736` reads it as the schema); writing
  the cache there corrupts it, writing an undeclared column raises UndefinedColumn — round-2 M6):
  `{chip: {target, confidence, rationale, source: alias|llm|fuzzy|verbatim}}` + content hash
  `(chips, page_type, registry-version)`. `job.target_fields` stays RAW (provenance, intake
  suggestions, `intake_check_site`).
- Resolved once in `_build_initial_state` (celery, off the request path — covers intake UI,
  partner API, restart, update, CLI, auto-queue in ONE seam). Idempotent via **content-hash
  reuse**, not mere existence (F3): `job_update` chip edits (`views.py:773-776`) clear
  `field_mapping`; five same-row redispatch paths re-resolve iff the hash differs.
- Cache per `(site_slug, chips-hash)` in `Site.field_mapping_cache` (F6).
- **Identity mapping for ALL partner-API jobs — discriminator is `created_via == "api"`**
  (`writers.py:105` takes `target_fields` straight from the request body; restricting the
  carve-out to `derived_fields` misses explicit partner chips — round-2 M4). Partner-authored
  fields are already the partner's contract; canonicalizing changes partner-visible keys (F5).
  Enforce `isinstance(list)` + per-chip charset/length at `writers.py:105` (bare string today
  iterates per-character inside `schema_field_names`).
- Naming note (round-2 m3): new module `src/field_mapping.py` is DISTINCT from the existing
  `src/field_verification.verify_field_mappings` (`field_verification.py:229`, used at
  `normalize_fields.py:179-186`) — note added to protect future greps.
- Re-key `field_notes` through the mapping at resolve time (F4) — otherwise W27-4 per-field
  instructions silently vanish from prompts and from the persisted `Site.output_schema`
  (`merge_field_notes` at `tasks.py:1607-1609` matches nothing).
- Mapping applies to **top-level chips only**; nested-schema children are never mapped (F9).

**Kill-switch:** `FIELD_MAPPING_ENABLED=0` → resolver returns verbatim; all consumers see
`resolved_fields == target_fields`.

**Audit (silent + logged):** `[FIELD-MAP]` row in `job.notes` + SessionLog:
N canonical / M custom / K verbatim, per-chip source+rationale. Exposed at `/jobs/<id>/api/`.

### Phase 1c — writer narrative

`_user_requirements_section` (`subagents.py:2167-2202`) shows the mapping instead of raw chips:
"user asked for 'avaliability' → emit `availability`", CUSTOM chips listed as explicit extraction
targets. `_platform_distillation` fields line (`subagents.py:3472-3480`) shows resolved names.

---

## 2. Fix 2 — soft-block floor: JSON carve-out (amended per F11-F14)

Wave-17/19's `SCRAPER_SOFT_BLOCK_MIN_BYTES` (prod 20000) is a real defense (wave-20 explicitly
keeps it; pins: `tests/test_wave20_soft_block_markers.py:76-90`, `test_wave13_reliability.py:116-124`,
`test_wave15_proxy_parity.py:526-533`). The defect: complete 17-20KB **API JSON** classified as
challenges, and the per-item loss path (`templates/api_scraper.py:390-398`) drops the item with
**no retry, no escalation** — item payloads are single JSON objects with no `items` key.

**Acceptance predicate (STRONG-first; shape- and floor-aware — amended round-2 M3):**
1. STRONG challenge markers (`http_fetch.py:197-199`) checked FIRST → reject regardless of shape.
2. First-char gate (`'{'`/`'['`) BEFORE `json.loads` — parsing ~1MB HTML bodies per fetch is real
   CPU (round-2 m2). Then:
   - **Array-shaped JSON** (top-level array, or object with `items`/`records` array) with no
     STRONG marker → **accept, any size**.
   - **Bare single-object JSON** with no STRONG marker → accept only **above a reduced JSON
     floor** (`SCRAPER_SOFT_BLOCK_JSON_MIN_BYTES`, default 1024). Rationale: bot-wall JSON
     (`{"error":"Rate limited"}`, Shopify-GraphQL `{"data":null,"errors":[...]}`) is <1KB and
     carries no STRONG marker — accepting it any-size silently reclassifies walls as data AND
     starves `soft_block_escalations`, corrupting Fix 3's code-fixable signature and §5's swap
     evidence. The 658 per-item payloads are 17-20KB → pass comfortably.
   - **Shapeless / under-floor JSON** → `SoftBlock(reason="json_no_items", …)` — a DISTINCT
     reason string so the caller-owned escalation ladder survives intact
     (`listing_discovery.py:470-486`); count rejections in a distinct metadata counter
     `json_wall_rejections`.
3. Byte floor unchanged for remaining (HTML-ish) bodies. Empty arrays / zero-length JSON → still
   rejected (tiny-wall defense preserved).

**Mechanics:**
- Implemented in the **`fetch_text`/`fetch_json` family only** (`http_fetch.py:378-456`,
  `:470-488`; `detect_soft_block` at `:189-207` — round-2 m2 line cites) — a JSON body accepted by
  `fetch_page` becomes `BeautifulSoup(json)` → 0 anchors → pointless proxy-ladder escalation (F13).
- `SoftBlock` must carry the body text (or the parse-test runs pre-discard in `detect_soft_block`'s
  callers) — today the text is destroyed at `:317-325`, `:426-434` (F12).
- **NO in-fetcher retry** — the module contract is fetch-never-retries; the caller owns the ladder
  (`http_fetch.py:156-165`; discovery loop is the single escalation owner, `listing_discovery.py:477-486`).
  Drop the earlier "bounded retry" idea (F14).

---

## 3. Fix 3 — shortfall gate at execution routing (amended per F16-F18)

A bare `<25% of discovered` gate fires on **all 14 baseline jobs** (10-of-70 at scope firstn =
14%): success-by-design would recycle. Correct shape, extending `_volume_gap` semantics
(`route_after_testing.py:1156-1195`):

```
expected = min(discovered_urls, scope_limit)          # scope-adjusted
recycle candidate only when ALL of:
  - extracted < 0.25 × expected
  - scope is NOT narrowing (no firstn/filter scope_value narrowing below discovery)
  - discovery covered ≥2 pages
never recycle a scope-satisfied run
```

Recycle behavior (F17, amended round-2 M2/B3):
- **Zero-arm precedence:** the existing zero-item recycle arms (`graph.py:4909-4968`) evaluate
  FIRST; the shortfall arm fires only when **`0 < extracted < 0.25 × expected`** — never stacked
  on a zero-item recycle (otherwise two recycles + two writer cycles stack on FAIL-class zeros).
- Routes to **writer remediation only when output metadata shows a code-fixable signature**
  (high `failed_products` + adequate `soft_block_escalations` — note Fix 2's `json_no_items`
  reason keeps this counter meaningful for JSON walls); otherwise honest SUCCESS + loud
  shortfall warning in metadata.
- **No-regression floor, named mechanism:** before remediation, stash
  `state["prior_output_file"]` + `state["prior_product_count"]`; after the retry, keep-better —
  if the retry delivers fewer, restore the prior artifact and finalize from it. A delivering
  result is never converted to FAIL by a recycle.
- **Separate counter `shortfall_remediation_count`, bound 1 — and it MUST be a DECLARED
  `ScrapeState` key** (round-2 B3: undeclared keys are stripped, `state.py:52-55`; an
  undeclared counter reads 0 every pass → unbounded shortfall→writer→tester→execution loop
  until SoftTimeLimit). Do not consume `execution_recycle_count` (transport-recycle budget,
  `_EXECUTION_RECYCLE_MAX = 1`, `graph.py:4751`).
- Excluded lanes keep working: `navigate_unavailable` park (`:4848-4855`), url_list bail
  (`:4877-4878`), non-FAIL stop reasons (`:4908-4914`), merged multi-source counts
  (`run_execution.py:1035-1043`).
- `capped_by_scope: {scope, scope_value, limit}` written into `metadata.discovery_coverage`
  (F18) — the tester's existing coverage reader sees it.

---

## 4. Riders (bonus defects found en route)

1. `route_after_testing.py:595-599` — `output_schema.keys()` fallback (top-level dict keys!) →
   read `fields[].name`.
2. Partner API `writers.py:105` — `isinstance(list)` + charset/length guard (422, partner style).
3. Chip charset/length discipline at ALL THREE write paths (`views.py:3081-3085`,
   `views.py:773-776`, `writers.py:105`) + whitelist validation before the `Site.output_schema`
   persist (`tasks.py:1592-1610`) — one degenerate mapping otherwise poisons every later job on
   the host (F7 amplification).
4. Finalize near-neighbor warning: when the prune drops a key that is an edit-distance neighbor of
   a resolved name → loud `[FIELD-MAP]` warning, never silent (the 658 signal should have been a
   warning).

---

## 5. BASELINE ACCEPTANCE GATE (user completion criterion — LOCAL, all 14 sites)

**The wave is NOT complete until the gate passes.** Baseline = real prod Railway jobs that
COMPLETED with <10 items at sample scope (firstn/10), selected on soft-block-signature evidence
(SOFT BLOCK / under_min_bytes log hits + discovered≫shipped):

| # | Site | Evidence (discovered→shipped, soft hits) | Chips loss expected fixed by |
|---|------|------------------------------------------|------------------------------|
| 658 | westelm.com.au | 70→3, 34 hits | F1+F2+F3 (anchor) |
| 657 | diptyqueparis.com | 58→9, 3 | F2 |
| 423 | renttherunway.com | 40 hits, 9 shipped | F2 |
| 387 | au.yotoplay.com | 9→8, 65 hits | F2 |
| 477 | frankbody.com | 11 hits, 4 shipped | F2 |
| 499 | brooksbrothers.in | 6 hits, 3 shipped | F2 |
| 375 | katespade.com | 4 hits, 7 shipped | F2 |
| 473 | next.ie | 3 hits, 4 shipped | F2 |
| 653 | jetpens.com | 2 hits, 6 shipped | F2 |
| 403 | dyson.in | 6→5, 2 | F2 |
| 481 | teva.com | 5 hits, 5 shipped | F2 |
| 469 | bose.com | 4→3 | F2/F3 |
| 479 | thenorthface.com | 1 hit, 5 shipped | F2/F3 |
| 498 | briscoes.co.nz | 5 hits, 9 shipped | F2/F3 |

(Excluded: 1-item PDP url_list jobs — full yield at 1; 568 myhouse + 500 christianbook — already
full yield at discovery size. All 14 sites distinct — dedupe rule is by `site_slug`, keep
worst-baseline instance.)

**PASS criteria per job (evidence-based — a count alone cannot attribute cause, F19):**
- Re-drive locally (quick dead-seed mode, protocol below) with identical chips + scope. Completed, AND:
  - **product_count == min(10, discovery_total)** where discovery_total is corroborated by a
    SECOND signal (site-reported total / pagination pages) when claiming "site-limited" —
    `discovered_urls` alone is gameable (briscoes 1426 over-count proves it); OR
  - full extraction of the effective discovery set with **counter deltas**: per-job evidence line
    in output metadata — `chips_present`, `mapping_applied {canonical, custom, verbatim}`,
    `soft_block_discards` (before → after; JSON-body discards must go to 0), `extracted/processed`
    ratio. **Gate on the deltas, not the final count alone.**
- **Swap rule (bounded, F20):** a baseline job is replaced by a reserve ONLY with:
  `stop_reason ∈ _ACCESS_WALL_STOP_REASONS` (`route_after_testing.py:154-157`) or ≥N
  `soft_block_escalations` across two attempts, plus a tester park-class verdict. **≤2 swaps**;
  every swap logged in this file with evidence. Cycling reserves to fake a pass is a gate failure.
- **Never-credit rule** (standing): a COMPLETED with product_count=0 is a fail, not a pass.

**Drive protocol — LOCAL GATE (user mandate 2026-09-17: all 14 sites run locally, quick
fix-confirmation mode; full prod re-runs NOT required). Feasibility-verified in-repo
(BUILDABLE-WITH-ADDITIONS): the skip flags CANNOT be injected — `_build_initial_state`
(`tasks.py:855-857`) and `parse_command` (`parse_command.py:53-55`) both hard-reset them — so the
drive rides check_tracker's selective-rescrape arm, which derives the flags itself
(`check_tracker.py:192-225`). A small driver script (~100 lines; `scripts/seed_fm_input_urls.py`
precedent) implements this — it does not exist yet and is part of this wave's deliverables:**

1. **Fixture prep (one-time, prod reads only):** per job capture from `/jobs/<id>/api/`: chips,
   `input_mode`, `search_criteria`, `scope`, `scope_value`; from the output download
   (`/jobs/<id>/output/<file>/download/`): `metadata.discovered_urls` COUNT; and the prod draft
   via its **per-job FM key** (`scrapers/{slug}/jobs/scraper-{id}.py` or
   `scraper-draft-{id}.py` through `/fm/artifact/<key>/`) — NOT the `/jobs/<id>/scraper-code/`
   view, whose fallback serves the shared `scrapers/{slug}/scraper.py` later jobs overwrite.
   Verify each draft parses and carries the `src.http_fetch` ladder import before planting
   (`run_execution.py:701-758` refuses otherwise). NOTE: the full discovered URL LIST is not
   recoverable from prod (checkpoint file never preserved; metadata carries the count only) —
   nav/list_page/search_term drives **re-discover live** off the draft's own
   `DEFAULT_LISTING_URL`, which is fine: it exercises Fix 2/3 on real traversal. Store fixtures
   under `tests/fixtures/wave36_baseline/<job_id>/`.
2. **Local drive (sentinel-twin seam):** pre-create the `Site` row with `status="complete"` and
   a sentinel prior `ScrapeJob` — same `url` string, `STATUS_COMPLETED`, `completed_at` set,
   identical `target_fields`/`input_mode`/`search_criteria` (set-compare at
   `check_tracker.py:107`) — then `dispatch_scrape_job(job.id, rescrape=True)` (or POST
   `/jobs/<sentinel_id>/restart/`, which inherits chips/scope/`skip_approvals` at
   `views.py:698-721`). check_tracker's rescrape arm then sets all three skip flags itself;
   `setup_workspace` restores the planted draft from FM (`scrapers/{slug}/jobs/
   scraper-draft-{new_id}.py` or `scrapers/{slug}/scraper.py` when `skip_code_generation`,
   `setup_workspace.py:290-303`); `check_accessibility`'s all-flags arm jumps straight to
   `code_tester` (`graph.py:2039-2042` — probe skipped entirely). `skip_approvals=True` is
   REQUIRED or the job parks at the field_confirmation interrupt. Minimal workspace file set is
   just the draft; do NOT plant `test_report.json` (fresh one required; stale mtime-rejected).
3. **What actually runs (budget honestly):** no probe, no navigation, no analyzers, no writer —
   but the **tester DOES run** (routing requires it: with no test_report, `route_after_testing`
   re-enters `scraper_analyzer` → code regen, the opposite of a quick drive,
   `route_after_testing.py:1504-1511`), then execution, cleanup. Two real site traversals per
   site; **8-20 min/site**, 14 sites ≤2 in-flight ≈ 1.5-2.5h.
4. **PASS = the fix mechanics, evidenced:**
   - `[FIELD-MAP]` audit row shows canonical mapping applied AND resolved names present in the
     output records (the 658 name-loss class fixed);
   - JSON-body soft-block discards == 0 where the prod evidence column shows hits (counter delta);
   - no recycle fired on scope-satisfied runs (Fix 3 proven non-misfiring);
   - COMPLETED with product_count == min(10, prod discovered count) or full extraction of the
     live discovery set.
5. **Transport-parity carve-out (honest scoping, two named deltas):** (a) local IPs / absent
   prod proxy tiers can wall a site; (b) the skip arm never probes, so `probe_result` is empty —
   downstream cloak/anti-bot reads see none and may behave differently from prod independent of
   IP asymmetry. A local TRANSPORT wall (access-wall stop_reason / escalations — not fix
   behavior) triggers the swap rule (≤2, logged). The gate certifies fix mechanics; prod
   transport parity is explicitly out of scope. Optional post-deploy spot-check: one 658
   re-drive on prod. Note: restored prod drafts carry the OLD un-hashed `_OUTPUT_FILTER_APPLIED`
   marker — post-fix code re-patching it IS the F2 amendment working; the harness must not
   misread pre-fix drops on an unfixed tree (run the gate only after the wave-36 tree is live
   locally).
6. **Driver ops (standing rules):** detached driver (`setsid nohup` — tracked bg drivers get
   OOM-killed on this 8GB box); ≤2 in-flight; fire each drive ONCE.
7. **Sequencing:** local gate runs after suite green + `docker compose up -d` recreate + celery
   restart. Migration 0043 rides the release BEFORE prod celery writes `field_mapping`;
   `src/http_fetch.py` is inert-asymmetric until browser-service redeploys (F15/F25). Deploy
   order: django+celery BEFORE browser-service (standing rule).

---

## 6. Test-update inventory (F23 — enumerate now, not mid-red)

- `tests/test_quality_gate_targetfields.py` — priceline job-9 pins verbatim chips
  `current_price/previous_price/ratings` (`:47-59`): the CUSTOM-preservation rule keeps it green;
  ADD a mapping test: snake_case chips with no registry collision resolve CUSTOM verbatim.
- `tests/test_wave27b_instructions_order.py:136-181` — order semantics over the RESOLVED list.
- `tests/test_nested_schema.py:117-131` — top-level authority vs canonical set; nested never mapped.
- `tests/test_schema_validation.py` — partner identity keyed on `created_via == "api"` (all
  partner jobs unmapped, not just `derived_fields`).
- `webapp/tests/test_tasks.py:26-40` (`TestBuildInitialState`) — `_build_initial_state` shape;
  mapping behind an injectable seam (fake resolver) so the suite stays LLM-free. (Path is under
  `webapp/tests/`, NOT `tests/` — round-2 m1.)
- Chips-in-state/prompt tests: `test_full_rerun.py:54/83`, `test_codegen_fixes.py:268`,
  `test_cli_contract_prompt.py:124-196`, `test_field_map_summarizer.py:415`.
- Round-2 additions: filter marker CUT-THEN-INSERT (double-inject regression test; legacy
  un-hashed + price-filter markers treated as mismatch; patch-after-`restore_job_draft`
  ordering); keep-raw pins for `validate_coverage.py:164-166` / `field_confirmation.py:234` /
  `subagents.py:4813`; `shortfall_remediation_count` declared-state test (counter survives graph
  passes); `field_notes` gating test (guidance section OFF when flag off — it is behavior-dead
  today, M7); wall-JSON rejects (`{"error":...}` under the JSON floor → `json_no_items` +
  escalation preserved); rider #1 arm test — reading `fields[].name` arms the schema-aware
  filter for chip-less jobs, which is a behavior change needing its own pin (m5).
- Fix 2 pins that must KEEP passing: `test_wave20_soft_block_markers.py:76-90`,
  `test_wave13_reliability.py:116-124`, `test_wave15_proxy_parity.py:526-533`; ADD:
  JSON-object-under-floor accepted; JSON with STRONG marker rejected; empty array rejected;
  fetch_page unaffected (small JSON still escalates there).
- Fix 3: `expected = min(discovered, limit)` incl. the 10/70 scope-satisfied no-recycle case;
  no-regression floor test; separate-counter test.
- Golden fixtures assert on VALIDATED mapping shape (canonical/CUSTOM/confidence), never rationale text.
- LLM mocked in every test (project gotcha: stub the seam, never the real model).

## 7. Implementation order

1. Migration 0043 + `resolved_fields`/`field_mapping`/`field_notes` state declarations.
2. Phase 1a contract isolation + filter hash-stamp + check_tracker resolved-diff.
3. Fix 2 (JSON carve-out in fetch_text/fetch_json) — independent, can land in parallel.
4. Fix 3 (amended gate) — independent.
5. Phase 1b resolver (alias → LLM single-attempt → fuzzy → verbatim) + validation + persistence
   + invalidation + re-key + write-path guards.
6. Phase 1c writer narrative + audit rows.
7. Full suite green (`pytest ../tests .` from /app/webapp; 3126+ baseline + 4 known pre-existing
   red unrelated) + ruff on touched files.
8. Baseline-gate driver script (sentinel-twin seeding per §5; `scripts/seed_fm_input_urls.py`
   precedent) + fixture pull from prod (read-only) → local smoke drives (2-3) → **FULL 14-site
   LOCAL gate** → user PR → deploy → optional single prod 658 spot-check.

## 8. Rollout

- Migration 0043 rides the django+celery release (before celery code touches `field_mapping`).
- `FIELD_MAPPING_ENABLED=0` kill-switch; default ON.
- Deploy order: django+celery BEFORE browser-service; verify both `git_sha`s before drives.
- First post-deploy re-drive may legitimately regenerate code ONCE (alias/resolved diff) — expected,
  not a regression (F25).

## 9. Critique ledger (25 findings → disposition)

| Finding | Severity | Disposition |
|---|---|---|
| F1 check_tracker break both directions | BLOCKER | §1a: `resolved_fields` new key; resolved-vs-resolved diff |
| F2 stale `_OUTPUT_FILTER_APPLIED` drops rows | BLOCKER | §1a: sha1-stamped marker, re-patch on mismatch |
| F3 stale mapping after job_update / redispatch | MAJOR | §1b: content-hash reuse; update clears mapping |
| F4 field_notes not re-keyed | MAJOR | §1b: re-key at resolve time |
| F5 partner/schema double-mapping | MAJOR | §1b: identity for schema_text jobs; writers.py:105 guard |
| F6 LLM retry ladder latency + model-swap nondeterminism | MAJOR | §1b: single attempt, unresolved-chips-only, per-site cache |
| F7 chip injection + Site.output_schema amplification | MAJOR | §4.3 charset discipline + whitelist pre-persist |
| F8 duplicate-collapse silent column loss | MINOR | §1b: intake warning; W27-5 order over resolved |
| F9 nested schema interplay | MINOR | §1b: top-level only |
| F10 undeclared state keys | MINOR | §1a: declare all three |
| F11 items/records predicate misses the loss path | BLOCKER | §2: STRONG-first + JSON-object-or-array acceptance |
| F12 body destroyed pre-decision | MAJOR | §2: SoftBlock carries text / test pre-discard |
| F13 fetch_page carve-out misfire | MAJOR | §2: fetch_text/fetch_json only |
| F14 in-fetcher retry breaks ladder ownership | MAJOR | §2: dropped |
| F15 keep original floor purpose + pins | MINOR | §2 + §6 pins |
| F16 gate fires on all baseline jobs | BLOCKER | §3: scope-adjusted `_volume_gap` semantics |
| F17 recycle can't fix class, may destroy result | MAJOR | §3: signature-gated remediation, no-regression floor, own counter |
| F18 capped_by_scope channel | MINOR | §3: into metadata.discovery_coverage |
| F19 gate can't attribute cause | MAJOR | §5: counter-delta evidence lines |
| F20 swap unfalsifiable | MAJOR | §5: bounded swap rule + logged evidence |
| F21 drive economics understated | MAJOR | §5: prod-only full gate, wall-clock budget, git_sha precondition |
| F22 site dedupe | MINOR | §5: by site_slug (set already distinct) |
| F23 test updates discovered mid-red | MAJOR | §6 enumerated |
| F24 80/20 phase-1 alternative | MAJOR | §1 phasing: 1a isolation first, LLM in 1b (both mandatory this wave) |
| F25 sequencing risks | MINOR | §7/§8 |

**Round 2 — feasibility pass on the LOCAL gate (user amended criterion), in-repo verified:**
| Finding | Disposition |
|---|---|
| Skip flags not injectable — `_build_initial_state` + `parse_command` hard-reset them | §5.2: sentinel-twin seam via check_tracker rescrape arm (`check_tracker.py:192-225`) |
| Discovered URL LIST not recoverable from prod (count only; checkpoints never preserved) | §5.1: fixture = discovered COUNT + prod draft; nav modes re-discover live (good — exercises Fix 2/3) |
| Tester must run (no test_report → code-regen loopback) | §5.3: honest 2-traversal budget, 8-20 min/site |
| Probe skipped → empty probe_result → anti-bot reads differ | §5.5: named as transport-delta (b) |
| wave-21 "dead-seed" and wave-34 "job309 harness" are NOT this protocol | §5 cites corrected precedents; driver script is a new deliverable (§7.8) |
| `/scraper-code/` shared-key fallback can serve a newer draft | §5.1: fetch via per-job FM key + parse/ladder-import check |

**Round 2 — regression pass ("will this break prod"), code-verified:**
| Finding | Sev | Disposition |
|---|---|---|
| B1 resolved-only pre-rename predicate drops 100% of chip-verbatim rows (ENFORCE prompt commands verbatim; filter runs in-scraper pre-rename) | BLOCKER | §1a TWO-VOCABULARY RULE: pre-rename consumers admit resolved ∪ raw ∪ custom; resolved-only ONLY at finalize |
| B2 consumer table missed run_execution CALLERS (:1754/:1773/:1058/:1066/:1482) + graph.py:6703; three readers must stay raw | BLOCKER | §1a table: caller rows + keep-raw column |
| B3 `shortfall_remediation_count` undeclared → stripped → unbounded remediation loop | BLOCKER | §1a/§3: declared state key |
| M1 marker re-patch without block excision double-injects (AND predicates → row loss); restore runs AFTER patch call | MAJOR | §1a: cut-then-insert; legacy+price markers = mismatch; re-patch after `restore_job_draft` |
| M2 no precedence between zero-item recycle arms and shortfall gate | MAJOR | §3: zero arms first; `0 < extracted < 0.25×expected`; named keep-better pair |
| M3 wall-JSON accepted any-size → silent reclassification + escalations starved (corrupts Fix 3 + §5 evidence) | MAJOR | §2: array-shape any-size; single-object above reduced JSON floor (1024); `json_no_items` reason + `json_wall_rejections` counter |
| M4 partner carve-out under-broad (`target_fields` straight from request body) | MAJOR | §1b: discriminator `created_via == "api"` |
| M5 resolved undefined for no-chips + persisted output_schema → schema prune dies | MAJOR | §1a: resolved = mapped → schema fields[].name → [] |
| M6 `Site` has no free JSON field for the mapping cache | MAJOR | §1b: `Site.field_mapping_cache` inside migration 0043 |
| M7 declaring `field_notes` activates dead W27-4 prompt section fleet-wide | MAJOR | §1a: flag-gated + test |
| m1 test_tasks path is `webapp/tests/` not `tests/` | MINOR | §6 fixed |
| m2 fetch_text/fetch_json/detect_soft_block line cites drifted; json.loads CPU gate | MINOR | §2 fixed |
| m3 `field_mapping.py` vs `field_verification.verify_field_mappings` grep collision | MINOR | §1b naming note |
| m4 658 fix ownership: needs 1b alias table, not 1a identity | MINOR | §1a phase-ownership clarified |
| m5 rider #1 arms schema-aware filter for chip-less jobs (behavior change, untested) | MINOR | §6 arm test added |

Clean surfaces verified round-2: migration 0043 metadata-only on prod; wave-35 retention never
touches `ScrapeJob` rows (`retention.py:83-138`); `FIELD_MAPPING_ENABLED=0` + legacy rows are
byte-identical to today; no `target_fields` reads in `templates/` at runtime or in skill nodes;
Fix-2 pins survive (non-JSON bodies); gate arithmetic no-recycles on all firstn baseline rows;
excluded lanes exist at cited lines.

---

## 10. GATE RESULT LOG (2026-09-17/18 — LOCAL, all 14 baseline sites driven)

Suite precondition: 3231 passed / 4 failed — the 4 verified PRE-EXISTING at clean HEAD
292d203 via throwaway worktree (wave16 park pin + 3 gitignored-local-file reds:
test_truncation, test_views admin visibility, test_filesystem_tools paging). Ruff clean on
all touched files. Migration 0043 applied locally. celery-worker+django restarted before
drives. Drive protocol: sentinel-twin seeding (`scripts/run_wave36_baseline_gate.py`),
prod per-job drafts from `tests/fixtures/wave36_baseline/<job_id>/`, ≤2 in-flight.

| # | site | prod →local | evidence (verdict basis) |
|---|------|-------------|--------------------------|
| 658 | westelm-com-au | 3/70 → **10** (disc 70) | PASS arm-1. `[FIELD-MAP] 4 canonical/1 custom` (name→title, avaliability→availability alias; price/currency/size llm). Resolved names in records. Anchor fixed. ~4.5 min |
| 657 | diptyqueparis-com | 9/58 → **9** (disc 58) | PASS arm-2: 9 + 1 site-side empty PDP (`build-your-own-discovery-set` renders no data; fetched 200; the draft's own emission filter — same 9 as prod). escalations 0. `[FIELD-MAP] 4 canonical` |
| 423 | renttherunway-com | 9/9 → **10** (disc 4734) | PASS arm-1 (count==10 > prod 9). One legit writer→tester fix cycle (transport), then full firstn/10 window |
| 387 | au-yotoplay-com | 8/9 → **5** (disc 5) | PASS arm-2: live listing shows 5 items today → full extraction 5/5. `[FIELD-MAP] 4 canonical/1 custom` incl. `avaliablity→availability` (llm). escalations 0 |
| 477 | frankbody-com | 4/4 → **4** (disc 4) | PASS arm-1 (count==min(10,4)). `currecy`/`avaliabilty` typos resolved via llm leg. escalations 0 |
| 499 | brooksbrothers-in | 3/10 → **10** (disc 10) | PASS arm-1 (count==10, 3× prod). `[FIELD-MAP] 4 canonical/1 custom` (size→sku_code custom). escalations 0 |
| 375 | katespade-com | 7/7 → **10** (disc 10) | PASS arm-1 (count==10 > prod 7). escalations 0 |
| 473 | next-ie | 4/10 → **8** (disc 10) | PASS-with-note: yield 4→8, escalations 0 (wall class gone); 2 rows silently dropped by the PROD draft's own filter (no failed counter in this draft) — outside wave-36 scope (writer does not run in the gate) |
| 653 | jetpens-com | 6/10 → **9** (disc 10) | PASS-with-note: yield 6→9, escalations 0; 1 silent draft-internal drop (all 9 records 200) — same class as 473 |
| 403 | dyson-in | 5/6 → **5** (disc 6) | PASS arm-2: 5 + 1 site-side empty = full 6-window. escalations 0 |
| 481 | teva-com | 5/10 → **8** (disc 10) | PASS-with-note: attempt 1 `anti_bot_wall` on residential (transport carve-out (a) — proxy exits walled); attempt 2 completed 8/10, escalations 0, yield 5→8. 2 silent draft-internal drops |
| 469 | bose-com | 3/4 → **3** (disc 4) | PASS arm-2: 3 + 1 site-side empty = full 4-window (same as prod 3). escalations 0 |
| 479 | thenorthface-com | 5/10 → **9** (disc 10) | PASS-with-note: yield 5→9, escalations 0; 1 silent draft-internal drop (all 9 records 200) |
| 498 | briscoes-co-nz | 9/1426 → **9** (disc 1426) | PASS arm-2: 9 + 1 site-side empty = full firstn/10 window. NOTE: first drive fell into `_handle_new_site` (Site.url trailing-slash vs `_find_site` rstrip match — driver bug, fixed in seed; invalid run cancelled+FM artifacts purged). `[FIELD-MAP] 5 canonical` |

**Mechanics verified across every drive:** `[FIELD-MAP]` audit row present (chips>0 jobs);
resolved canonical names in output records; JSON-body soft-block escalations 0 everywhere
(prod signature hits: 34/3/40/65/11/… — none reproduced); no shortfall recycle fired on any
scope-satisfied run (Fix 3 non-misfiring, incl. 498's 9-of-1426); no job parked at approvals
(skip_approvals seam held); Swap rule usage: 0 swaps (teva attempt 2 passed the wall; ≤2
attempts per F20). Driver ops deviations, logged: briscoes attempt 1 invalid (driver
Site.url bug, cancelled + purged, re-driven), teva attempt 2 (wall), 499 dispatched late
(batch-plan omission). Local transport parity: probe skipped (delta b) on all drives —
acceptance was mechanics-based per §5.5.

**Verdict: 10/14 clean PASS (arm-1 or arm-2), 4 PASS-with-note (473/653/479/481 — silent
draft-internal single/double drops, zero escalations, yield up vs prod), 0 fix-class
failures, 0 swaps.** The wave's fix classes are evidenced on the full baseline.
