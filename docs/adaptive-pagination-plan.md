# Adaptive Pagination — Investigation + Critiqued Plan

> Status: **Fully investigated (20+ agents), critiqued, regression-assessed.** Ready for staged implementation.
> Origin: desidime.com job 254 (21 items, page 1 only). Investigation spanned design (4 agents) → critique (3 agents) → verification (3 agents) → re-critique (3 agents) → regression analysis (3 agents) + skills analysis (3 agents).
> Related: [`discover-fields-plan.md`](./discover-fields-plan.md), [`code-writer-context-ballooning.md`](./code-writer-context-ballooning.md).

## What shipped (Layer 0) — proven ✅

Commit `f3e20f2`: Added `has_rel_next_page_param` signal to `_PAGE_STATE_JS` (`traversal.py:1219-1225`) + a `page_param` branch in `_pag_type` BEFORE `scroll_hint` (`traversal.py:1865-1875`) + `trust_start_as_listing` flag for list_page/search_term suppression gating.

**Proven**: desidime.com went from 21 → **1,015 items** (job 209). The `discovery_config.json` correctly emitted `type: "page_param", page_param_name: "page"`.

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
| **1** | **Fix rescrape routing** — force `browser_traverse` re-run on nav-mode rescrape (not just purge stale file) | `check_tracker.py:99` OR `graph.py:1110` + `setup_workspace.py` load-to-state | LOW (existing path already broken; fix makes it better) | Write rescrape tests first |
| **2** | **Fix `config_from_dict` + field-name pipeline** — map `offset_param` → `config_for_page_param`; fix `graph.py:2557` to read navigator's actual field names | `graph.py:2557` + `discovery.py:768` | ZERO existing jobs; HIGH future jobs if field-name fix skipped | Fix field names in same change |
| **3** | **Unify pagination patterns** — `src/pagination_patterns.py` single source of truth for selectors + regexes (kills Layer A/C drift) | new file + imports in `traversal.py` + `discovery.py` | ZERO (additive, same data) | — |

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
