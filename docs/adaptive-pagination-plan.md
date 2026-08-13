# Adaptive Pagination — Investigation + Critiqued Plan

> Status: **Fully investigated (20+ agents), critiqued, regression-assessed.** Ready for staged implementation.
> Origin: desidime.com job 254 (21 items, page 1 only). Investigation spanned design (4 agents) → critique (3 agents) → verification (3 agents) → re-critique (3 agents) → regression analysis (3 agents) + skills analysis (3 agents).
> Related: [`discover-fields-plan.md`](./discover-fields-plan.md), [`code-writer-context-ballooning.md`](./code-writer-context-ballooning.md).

## What shipped (Layer 0) — proven ✅

Commit `f3e20f2`: Added `has_rel_next_page_param` signal to `_PAGE_STATE_JS` (`traversal.py:1219-1225`) + a `page_param` branch in `_pag_type` BEFORE `scroll_hint` (`traversal.py:1865-1875`) + `trust_start_as_listing` flag for list_page/search_term suppression gating.

**Proven**: desidime.com went from 21 → **1,015 items** (job 209). The `discovery_config.json` correctly emitted `type: "page_param", page_param_name: "page"`.

## Implementation status (2026-08-13) — Fixes 2 & 3 shipped; Fix 1 reverted

Fixes 2 and 3 are implemented with unit tests (40 pass). Fix 1 was implemented, then **reverted** after a 3-agent deep critique (see below). E2E on desidime (live) + lw.com (DOM analysis):

| Fix | Status | Evidence |
|-----|--------|----------|
| **1 rescrape routing** | **REVERTED** | Implemented as `skip_site = not nav_mode`, which forces the full normal flow (browser_traverse → … → code_writer) on every nav-mode rescrape. code_writer is brittle/stochastic → it ballooned on desidime job 210 (159k chars, 900s timeout). Rescrapes were completing fine before (cascade → `code_tester`, reused the archived scraper, code_writer never ran). The revert restores that. The empty-`navigation_analysis` bug Fix 1 targeted is **latent** again (only bites fields_changed/nav_changed rescrapes — pre-existing). |
| **2 offset_param** | shipped | Unit: navigator-key shape (`page_param`/`page_size`) → `config_for_page_param`; `_apply` no longer `TypeError`s on navigator-only keys; offset math (`start` → `(page-1)*ipp`, e.g. page 3 × 24 = `?start=48`). Live SFCC e2e not run — calvinklein.us is anti-bot-blocked. |
| **3 unify patterns** | shipped | desidime (live): `discovery_config.json type=page_param` — NOT false-flipped. lw.com (Coveo, DOM analysis): conservative presence set detects the real Coveo `coveo-pager-next` pager (→ correct `load_more`) while dropping 6 `button[aria-label*='more']` false positives (facet checkboxes for "More**head**"/"**Skidmore**"/"**Swarthmore**" — visible+enabled, so the gate can't filter them). |

### Fix 1 deep critique (3 agents) → revert + staged hybrid

The naive fix (force `browser_traverse` on every nav-mode rescrape) is wrong because the skip cascade at `graph.py:1110-1117` **conflates "which analysis to re-run" with "whether to regenerate code"** — once `skip_site=False`, code_writer runs unavoidably. Three solutions were critiqued:
- **Revert** (chosen): minimal, restores fast rescrapes, HIGH confidence. Tolerates the pre-existing fields_changed/nav_changed gaps.
- **Surgical gate** (deferred): correct but ~35-50 LOC + retry guard + a **mandatory `template_version` stamp** (without it, every future code_writer fix silently fails to reach completed sites). Too much surface for now.
- **Re-hydration** (rejected): internally inconsistent — the cascade makes "skip code_writer AND reach scraper_analyzer" unreachable; doesn't fix desidime (archive already fresh; failure is retry-escalation).

**Staged hybrid follow-up** (when rescrape-correctness is prioritized over minimalism):
1. Narrow the gate from `nav_mode` → `nav_changed` (fresh `browser_traverse` only when nav *actually* changed — rare, user-initiated).
2. Fix the dead re-hydration: load archived `navigation_analysis.json` into state in `setup_workspace` (load nav **only**, not `scraper_analysis`, to avoid the `graph.py:1114` route-to-code_writer hazard). Closes the fields_changed gap — `scraper_analyzer` reads correct archived nav.
3. (Separate enhancement) `template_version`/prompt-hash stamp in archived `scraper_analysis.json` so deploy-time staleness is detectable.



**Design change vs. the original plan (data-driven):** Fix 3 is now **two-facet**, not a flat unify. Layer A classification uses `load_more_presence_selectors` (6 unambiguous class-substring selectors: load-more/loadMore/show-more/showMore/pager-next/pagerNext); the broad `button[aria-label*='more' i]` / `a[aria-label*='next' i]` / `.coveo-magicbox-load-more` stay in `load_more_selectors` for Layer C **click fallback only**. Rationale: a false `has_load_more=true` MISROUTES the whole job to load_more; a wrong click in a click fallback only no-ops. The JS probe also gained a visibility/clickability gate (skip hidden/disabled/`aria-hidden`) so Layer A's presence-check matches Layer C's visible+enabled click contract.

**Pre-existing blocker (NOT a regression from these fixes):** full end-to-end nav extraction is currently blocked by the code_writer 900s timeout + code_tester context ballooning (see [`code-writer-context-ballooning.md`](./code-writer-context-ballooning.md)). desidime 210 validated classification+routing (pre-timeout) but code_writer returned empty / code_tester ballooned (159k chars). All pagination-specific code paths are validated; the extraction stall is the separate, documented ballooning issue.



## Root problems found (beyond desidime)

### Problem 1: Stale `navigation_analysis` on rescrape (LATENT BUG)
`skip_site_analysis=True` on completed-site rescrape → `check_accessibility:1110` **skips `browser_traverse`** → `state.navigation_analysis` is empty → `_derive_strategy` / `_select_template_file` / `build_code_writer_message` all lose navigation context → wrong strategy/template/scraper. The re-hydrated file is dead code (nothing loads it into state). **This is already broken for every completed-site rescrape where anything changed.**

### Problem 2: Unmapped pagination types in `config_from_dict`
`offset_param` (SFCC `?start=N&sz=M`) and `page_numbers` (numbered buttons) are detected by the navigator but UNMAPPED by `config_from_dict` (`discovery.py:749-784`). Both fall through to `config_for_navigation_job()` (try all 4 strategies). BUT: `page_param_name=None` makes the `page_param` primitive dead in the fall-through → offset sites can't paginate via URL even when the strategy is "all 4".

**Field-name pipeline bug**: navigator emits `page_param`/`page_size` (`navigate_explore.py:650-652`); `scraper_analyzer` reads `page_param_name`/`items_per_page` (`graph.py:2557`) — different keys, values silently dropped.

### Problem 3: Unknown pagination types (Workday POST, cursor, Relay)
The 4 DOM primitives (`load_more`, `infinite_scroll`, `page_param`, `next_button`) can't handle:
- **JSON-body POST** (Workday/Taleo/UKG) — the #1 real-world gap (kirkland FAIL). `api_scraper.py` is GET-only; `verify_api` probes GET-only; navigator hardcodes `method: 'GET'`.
- **Cursor REST** (`next_cursor`/`starting_after`) — Algolia, Shopify Admin, Greenhouse. Needs network/XHR interception.
- **GraphQL Relay** (`endCursor`/`hasNextPage`) — Shopify headless, myntra (hand-rolled workaround exists).

**~23% of shipped scrapers (5/22) hit unhandled pagination.** 2 rescued by LLM custom loops (HTTP-path, not Playwright-path). 3 capitulated.

### Problem 4: Skills are LLM-advisory only — deterministic code is skill-blind
The skills system (`.opencode/skills/`) feeds markdown to LLM agents via `load_skill`. Zero deterministic code reads skill content. Pagination detection runs as JS probes + Python classifiers — no bridge from skills to deterministic code. The knowledge exists in `navigation-patterns/SKILL.md` but is independently re-encoded in `traversal.py` + `discovery.py`, with **selector drift** between the two (Layer A has 3 load-more selectors; Layer C has 9).

## Critiqued plan (right-sized)

### Ship now (safe, high-ROI)

| # | Fix | Scope | Regression risk | Prerequisite |
|---|-----|-------|----------------|--------------|
| ✅ | **Layer 0** (desidime fix) | shipped | none | — |
| **1** | **Fix rescrape routing** — force `browser_traverse` re-run on nav-mode rescrape (not just purge stale file) | `check_tracker.py:99` OR `graph.py:1110` + `setup_workspace.py` load-to-state | **REVERTED** — see Implementation status. Naive `skip_site=False` forces code_writer → ballooning on every nav-mode rescrape. Staged as a narrower hybrid (gate on `nav_changed` + re-hydrate nav into state). | — |
| **2** | **Fix `config_from_dict` + field-name pipeline** — map `offset_param` → `config_for_page_param`; fix `graph.py:2557` to read navigator's actual field names | `graph.py:2557` + `discovery.py:768` | ZERO existing jobs; HIGH future jobs if field-name fix skipped | Fix field names in same change |
| **3** | **Unify pagination patterns** — `src/pagination_patterns.py` single source of truth for selectors + regexes (kills Layer A/C drift); **two-facet** (conservative `load_more_presence_selectors` for Layer A classification, full 9 for Layer C clicking) + visibility gate in the JS probe | new file + imports in `traversal.py` + `discovery.py` | LOW — NOT zero (see Implementation status); broad `aria-label*='more'` matches non-pagination buttons, so it's excluded from classification | — |

### Defer (needs more infrastructure)

| # | Fix | Why deferred |
|---|-----|-------------|
| Engine extensibility (`custom_strategies` in `DiscoveryConfig`) | Sound design (~40-60 LOC) but ZERO proven Playwright-path need. Ship when a site requires a custom DOM strategy. Needs tests first (`tests/test_discovery.py` doesn't exist). |
| Escape hatch (conditional prompt for code_writer) | Re-introduces the original sin (LLM dropping MAX_DISCOVER_PAGES) without a post-gen AST guard. Needs significant safety infrastructure. |
| `discovery_override.json` feedback loop | Over-engineered — cache invalidation (item 1) achieves the same goal in fewer LOC. |
| Workday POST | NOT standalone (~7 files, needs CDP network capture). Build when network interception infrastructure lands. |
| Cursor REST / GraphQL Relay | Needs `PageLike` Protocol network surface. Highest unblocked value but largest investment. |

### Explicitly rejected (per critique)

- ~~Demote scroll_hint entirely~~ — regresses lw.com (Coveo SPA). Use positive signal instead (Layer 0's approach).
- ~~Always-emit config safety net~~ — dead branch (page_param_name=None), guard regression.
- ~~Reorder (prefer page_param over load_more)~~ — regresses SPA sites (dollartartree, lw.com).
- ~~page_numbers mapping~~ — no primitive exists. Leave on fall-through.

## Key file references

- `experimental/nav_traversal/traversal.py:1217-1228` — `_PAGE_STATE_JS` (Layer 0 fix shipped here)
- `experimental/nav_traversal/traversal.py:1862-1883` — `_pag_type` classifier (Layer 0 fix shipped here)
- `experimental/nav_traversal/traversal.py:1791-1799` — start-page suppression (Layer 0 fix shipped here)
- `webapp/agents/graph.py:1110-1117` — **routing bug** (skip_site_analysis bypasses browser_traverse)
- `webapp/agents/nodes/check_tracker.py:99-110` — `_compute_rescrape_skip_flags`
- `webapp/agents/nodes/setup_workspace.py:148-155` — dead re-hydration
- `webapp/agents/graph.py:2549-2563` — **field-name pipeline bug** (page_param vs page_param_name)
- `src/discovery.py:749-784` — `config_from_dict` (unmapped types)
- `src/discovery.py:74-86` — `DEFAULT_LOAD_MORE_SELECTORS` (9 selectors, drifted from Layer A's 3)
- `src/discovery.py:541-546` — `_PRIMITIVES` dict (the 4 built-ins)
- `src/discovery.py:609-687` — orchestrator loop (the dispatch + guard)
- `templates/api_scraper.py:119` — GET-only fetch (Workday POST gap)
- `templates/playwright_scraper.py:245-263` — `discovery_config.json` reader + fallback

## Latent bugs found during investigation

1. **Routing bug** (`graph.py:1110`): `skip_site_analysis` bypasses `browser_traverse` on rescrape → empty navigation_analysis → wrong strategy/template/scraper regeneration.
2. **Dead re-hydration** (`setup_workspace.py:152`): restores file but never loads into state.
3. **Field-name pipeline bug** (`graph.py:2557`): navigator emits `page_param`/`page_size`; `scraper_analyzer` reads `page_param_name`/`items_per_page`.
4. **Probe bypass** (`playwright_scraper.py:383`): `--discover-only` probe calls `discover_item_urls` directly, bypassing any custom `discover_product_urls` body → false green for custom discovery.
5. **`--fresh-discovery` × `--discover-only` entanglement**: probe passes both flags; `--fresh-discovery` runs full uncapped discovery first, then capped probe runs against a closed page.
6. **`api_scraper.py:235`**: references undefined `HEADERS` (should be `API_HEADERS`). Latent crash on the per-detail fallback path.
7. **Selector drift**: Layer A (`_PAGE_STATE_JS`) has 3 load-more selectors; Layer C (`discovery.py`) has 9. A Coveo site where Layer C clicks load-more gets classified `infinite_scroll_tall` by Layer A.
8. **Zero tests for `src/discovery.py`**: the critical-path module for every navigation/list_page/search_term job has no test file.
