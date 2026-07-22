# CSR / Strategy / Div-Listing Fix — Design Plan

> **Status:** PROPOSED (not yet implemented). 3-round deep analysis complete.
> This document is for review before any code changes.
> Created 2026-07-21.

## Problem

Several sites fail because the pipeline picks the wrong scraping strategy for
their page-rendering model:

| Site | Expected | Got | Root cause |
|------|----------|-----|------------|
| **dystaffing** | ~488 jobs | 0 | Goal detector (anchor-biased) missed 488 `<li data-job-id>` divs; stopped at `/crna-jobs` (9 featured jobs) instead of `/job-search` (488) |
| **calvinklein UK** | ≥80 watches | 0 | Anti-bot + CSR listing; `rendering_verified` hardcoded to `"browser"` (not `"csr"`) → wrong strategy (`http_requests` instead of `http_navigation`) |
| **americaneagle** | ≥3 products | 0 | Same anti-bot + strategy issue |
| **kirkland** | ≥5 jobs | 15 cat pages | Workday iframe invisible to top-frame detector |

Plus the broader class: any CSR SPA listing (Vue/React/Angular) where the job/product
links render client-side → the `http_requests` strategy's static-HTML discovery finds 0 links.

## 3-Round Deep Analysis (completed 2026-07-21)

Three sequential agent-driven analysis rounds traced the impact graph-wide,
per-site, and adversarially. Key findings:

### Round 1 — Graph data-flow map

Traced every producer/consumer of the signals the fix touches:
`rendering_verified`, `data_source`, goal-detection signals, `extract_links`,
and the listing→strategy→code_writer→execution chain.

**Key findings:**
- `rendering_verified` is hardcoded `"browser"` at `graph.py:1493` — the
  `_derive_strategy` consumer expects `"csr"` → the CSR→browser cascade
  (`graph.py:2085`) is **dead code for every browser_traverse run**.
- `data_source` has **13+ branch points** across `graph.py`, `subagents.py`,
  `navigate_explore.py`. A new value (e.g. `"ssr_div_list"`) is invisible to
  ALL of them without adding new branches.
- `extract_links` (`traversal.py:139`) is **anchor-only** — div-based listings
  produce zero extracted URLs → starves `product_analyzer` + `code_writer`.
- The `_on_start` guard (`traversal.py:1636-1638`) suppresses homepage signals
  — it's the ONLY thing preventing the DOM-repetition detector from
  false-positive-firing on a homepage content grid.

### Round 2 — Per-site impact

| Site | Current | Part A | Part B | Part C | Verdict |
|------|---------|--------|--------|--------|---------|
| locumtenens | PASS 25 (http_requests, SSR) | unchanged (raw HTML has anchors → ssr) | unchanged (listing already detected) | unchanged (has `<a>` detail URLs) | **safe** |
| ayahealthcare | PASS 26,742 (internal_api) | unchanged (API override wins) | unchanged | unchanged | **safe** |
| adameve | PASS 36 (http_requests, SSR) | unchanged (raw HTML has anchors) | unchanged | unchanged | **safe** |
| **myntra** | PASS 216 (http_requests + `__myx`) | **REGRESSION** (CSR → flips to http_navigation, loses `__myx` parser) | unchanged | unchanged | **AT RISK** |
| vistastaff | marginal 1 (anti-bot 403) | unchanged | unchanged | unchanged | **safe** (separate anti-bot issue) |
| dollartree | marginal 1 (url_list) | N/A (no nav) | N/A | N/A | **safe** |
| wildsecrets | marginal 1 (url_list) | N/A | N/A | N/A | **safe** |
| calvinklein UK | FAIL 0 (anti-bot) | would help IF fetch works (see Hole 2) | N/A | N/A | **conditional** |
| americaneagle | FAIL 0 (anti-bot execution) | same | N/A | N/A | **conditional** |
| kirkland | FAIL 15 (Workday iframe) | unchanged (iframe not accessible) | unchanged | unchanged | **safe** (separate issue) |
| **dystaffing** | FAIL 0 (div-listing, wrong goal) | would help (with B+C) | **FIXES goal pick** | **FIXES extraction** | **FIXED with full A+B+C** |

### Round 3 — Adversarial corners (the holes)

Found **three critical holes** that make the original proposal (Parts A+B+C)
**not implementable as written**:

#### Hole 1 — Part A is a guaranteed no-op

`_verify_rendering` is never invoked on the browser_traverse path. Wiring it
requires constructing a `findings["listing_page"]` argument with `product_links`
+ `url` — but `TraversalResult.signals` at `traversal.py:1692` is just
`{"is_listing": True, "reason": "..."}` (no product_links). Without this,
`_verify_rendering`'s early-return at `navigate_explore.py:1883` returns
`"unknown"` → Part A is dead.

Even with wiring: `_verify_rendering` uses **bare `httpx.get`** (no browser
fallback). On anti-bot CSR sites (calvinklein — Part A's target audience),
httpx gets 403 → error path → `"unknown"` → `http_requests` → **unfixed**.

#### Hole 2 — The myntra guard is broken

myntra's inline JSON is `window.__myx = {"searchData": {...}}` — an **object**
literal. The embedded-JSON detector regex at `navigate_explore.py:1115` requires
`NAME = [{` (an **array** of objects). Object ≠ array → no match → `emb_count=0`
→ the override at line 1928 never runs → `_raw_html_has_embedded_json` is never
called. Strengthening it does nothing. myntra passes today (216) via
`http_requests` + custom `__myx` parser — Part A would regress it.

#### Hole 3 — Part B(1) has unmitigable false-positives (no LLM veto)

The override at `traversal.py:1646` is `False → True` only. The LLM can say "not
a listing" but the deterministic signal FORCES `True`. Any page with ≥5 same-class
div siblings sharing a path prefix (blog grids, related-items rails, search
facets) → false listing. The LLM's veto doesn't exist.

## Amended Proposal

Given the 3-round findings, Parts A+B+C need the following amendments before
implementation:

### Part A — Rendering signal (fixes CSR routing)

**Original:** replace hardcoded `"browser"` with `_verify_rendering()`.

**Amended — three sub-parts:**

| Sub-part | What | Why |
|----------|------|-----|
| **A1: Wiring** | In `_invoke_navigation_traverse` (`graph.py:1493`), construct a `findings["listing_page"]` dict with `product_links` (extracted from `result.goal_url` via `_default_fetch` + `extract_links`) + `url` (`result.goal_url`), then call `_verify_rendering(findings, goal_url)`. | Without product_links, `_verify_rendering` early-returns `"unknown"`. |
| **A2: Fetch path** | Switch `_verify_rendering`'s raw fetch from bare `httpx.get` to `_default_fetch` (which has the browser_service `/render` fallback for anti-bot). Accept the risk that the browser fallback renders the page → false-`"ssr"` for genuine CSR sites. **OR** document explicitly that anti-bot CSR sites (calvklein) are NOT fixed by Part A and need `_enforce_anti_bot_strategy` (which already routes them to `http_navigation`). | Without this, calvinklein-class sites stay unfixed (httpx gets 403). |
| **A3: myntra guard** | Either: (a) extend the rendered-DOM detector regex at `navigate_explore.py:1115` to also match `NAME = { ... "results" ... "products" ... }` object assignments (new false-positive surface to assess), OR (b) add a pre-check for known inline-JSON patterns (`window.__myx`, `window.__INITIAL_STATE__`, `window.__APOLLO_STATE__`), OR (c) **simplest**: exclude sites whose current scraper passes via `http_requests` + custom inline-JSON parser from the Part A CSR flip (guard: `if current_strategy == "http_requests" and existing_scraper_passes: skip`). | Without this, myntra regresses (216 → fewer). |

**Sites Part A newly flips (after amendments):**
- With A2 (`_default_fetch`): calvklein (anti-bot) — IF the browser fallback renders the page → false-"ssr" → stays http_requests. So A2 may make Part A a **no-op for calvklein too** (the /render fallback shows rendered links → "ssr").
- **Net**: Part A may fix NO sites after amendments (A2's false-ssr + A3's exclusion). This needs honest assessment: **is Part A worth the complexity?**

### Part B — Goal detector (fixes div-listing detection)

**Original:** relax anchor requirement + "View All" gate.

**Amended:**

| Sub-part | What | Why |
|----------|------|-----|
| **B1: Relax detector** | In `_PAGE_STATE_JS` (`traversal.py:1170-1191`), when a ≥5-sibling group has no inner `<a href>`, fall back to counting siblings with `data-*-id`/`data-sku` attributes OR repeated-class + visible-text. Keep all existing guards (≥5 siblings, path-prefix ≥1, ≥60% visible-text, non-nav/footer/aside parent). | Needed for dystaffing's `<li data-job-id>` divs. |
| **B1-veto: LLM override fix** | Change `traversal.py:1646` from `result["is_listing"] = True` (force) to `result["is_listing"] = result.get("is_listing") or (signals fire)` — so the LLM's True stands, but the signal can't override the LLM's False. | Without this, B(1) false-positives on blog/related-item/search-facet grids are unmitigable. |
| **B2: Goal-quality gate** | When `signals` count is low (<15) AND the page has a clickable whose text matches `/all\|browse\|search\|see all\|view all/i`, record the current page as a *candidate* but keep traversing one more step toward that link. Pick the page with the larger count at the end. Define edge cases: (a) no "View All" → accept current; (b) button vs `<a>` → check clickables (both included); (c) multiple → pick the first with "all"/"browse"; (d) chained landings → max 2 extra steps. | Needed for dystaffing to reach `/job-search` (488) instead of stopping at `/crna-jobs` (9). |

### Part C — Div-listing data model (fixes dystaffing extraction)

**Original:** new `data_source="ssr_div_list"` + code_writer guidance.

**Amended:**

| Sub-part | What | Why |
|----------|------|-----|
| **C1: New data_source value** | Set `data_source = "ssr_div_list"` in `_invoke_navigation_traverse` (graph.py:1490) when the detector (Part B1) fired on data-attr/repeated-class siblings (not anchors). | Drives downstream branches. |
| **C2: code_writer URL-seeding** | In `_invoke_code_writer` (graph.py:2274), add: `if na.get("data_source") == "ssr_div_list": seed [goal_url] into input_urls.json` (the listing URL itself, not per-item URLs). | Without this, `input_urls.json` is empty/nav-links → `code_tester --sample` fails. |
| **C3: code_writer guidance section** | In `build_code_writer_message` (subagents.py:~2543), add a new section (parallel to `_embedded_json_code_writer_section`) for `ssr_div_list`: "The items are server-rendered as divs with `data-*-id` attributes on the listing page — extract records directly from the listing DOM, do NOT construct per-item URLs." | Without this, code_writer fabricates per-item URLs (like dystaffing's `/job/{id}` → 404). |
| **C4: NOT `_promote_data_richest_listing`** | Round 3 proved `_promote_data_richest_listing` is only called from the **archived** `navigate_explore` path, NOT browser_traverse. Do NOT add a branch there — it's irrelevant. | Round 2 was wrong about this being load-bearing. |

## Implementation plan (sequenced, with risks)

### Phase 0 — Safety fix (1 line, no risk)
**B1-veto**: change `traversal.py:1646` override direction so the LLM can veto false-positives.
This is a prerequisite for ALL of Part B. Without it, B(1) is dangerous.

### Phase 1 — dystaffing fix (Part B1 + B2 + C1-C3)
This is the most self-contained, highest-value piece. It doesn't touch Part A
(rendering signal) at all.

1. **B1**: relax `_PAGE_STATE_JS` detector (add data-attr/repeated-class counting).
2. **B1-veto**: (Phase 0, already done).
3. **B2**: goal-quality gate (low-count + "View All" affordance → keep traversing).
4. **C1**: stamp `data_source = "ssr_div_list"` when B1 fires on non-anchor siblings.
5. **C2**: seed listing URL into `input_urls.json` for `ssr_div_list`.
6. **C3**: code_writer guidance section for div-list extraction.

**Risk**: B1 false-positives on non-listing pages (blog/related-item grids).
Mitigation: B1-veto (LLM can say "not a listing") + existing guards (≥5 siblings,
path-prefix, ≥60% text, non-nav/footer parent).

**Verify**: re-run dystaffing → expect 488 jobs. Re-run locumtenens + adameve →
expect no regression.

### Phase 2 — Rendering signal (Part A, IF needed)
**Honest assessment needed first**: after Phase 1, does any site STILL fail due to
CSR routing? If calvinklein/americaneagle are already routed to `http_navigation`
via `_enforce_anti_bot_strategy`, Part A may be unnecessary. Check:
- Is `_enforce_anti_bot_strategy` already catching calvinklein? (It should —
  calvinklein is detected as anti-bot.)
- If yes, Part A's only remaining value is for non-anti-bot CSR sites (rare in
  the suite). **Consider deferring Part A entirely.**

If Part A IS needed:
1. **A1**: wire `_verify_rendering` into `_invoke_navigation_traverse`.
2. **A2**: decide fetch path (`_default_fetch` vs bare httpx — see analysis above).
3. **A3**: myntra guard (exclude or fix the regex).

**Risk**: myntra regression (216 → fewer). Mitigation: A3 guard + myntra regression
test before merge.

### Phase 3 — Production-site scope check
Before merging ANY of the above, run Part A/B/C's flip prediction against ALL
25+ production scrapers (not just the regression suite). Any current
`http_requests` site where the listing's raw HTML lacks anchors is at risk.

## Decisions for the user

1. **Proceed with Phase 1 (dystaffing fix: B1+B2+C1-C3)?** — highest value,
   most self-contained, doesn't touch rendering/strategy routing.
2. **Defer Phase 2 (Part A rendering signal)?** — given the 3 holes, Part A may
   not be worth the complexity. The anti-bot sites are already handled by
   `_enforce_anti_bot_strategy`.
3. **Accept the B1-veto change as a standalone safety fix?** — one line,
   independently valuable (fixes a safety property ALL of Part B depends on).

## Appendix: file:line references

- `webapp/agents/graph.py:1493` — hardcoded `"browser"` (Part A target)
- `webapp/agents/graph.py:2075,2085` — `_derive_strategy` CSR cascade
- `webapp/agents/graph.py:2102` — `_derive_strategy` API override
- `webapp/agents/graph.py:2274` — `_invoke_code_writer` URL-seeding (Part C branch)
- `webapp/agents/nodes/navigate_explore.py:1861-1943` — `_verify_rendering`
- `webapp/agents/nodes/navigate_explore.py:1115,1781` — embedded-JSON regex (myntra miss)
- `webapp/agents/nodes/navigate_explore.py:1928` — `emb_count >= 3` gate
- `experimental/nav_traversal/traversal.py:1170-1191` — `_PAGE_STATE_JS` detector (Part B1)
- `experimental/nav_traversal/traversal.py:1636-1638` — `_on_start` guard
- `experimental/nav_traversal/traversal.py:1646-1656` — `is_listing` override (B1-veto target)
- `experimental/nav_traversal/traversal.py:139-170` — `extract_links` (anchor-only)
- `experimental/nav_traversal/traversal.py:1692` — `TraversalResult.signals` (no product_links)
- `webapp/agents/subagents.py:2543-2566` — code_writer embedded_json section (Part C parallel)
- `webapp/agents/subagents.py:1143-1156` — product_analyzer `pick_candidates`
- `scrapers/myntra-com/scraper.py:306` — `window.__myx = {...}` (object, not array)
