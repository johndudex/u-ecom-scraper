# Navigation as a bounded, LLM-pruned graph traversal (design)

Status: **isolated prototype** (`experimental/nav_traversal/`). Not wired into the
pipeline; no existing agent modified. This doc is the persistent design record.

## Why

Three subagent investigations (two empirical site probes + an agent audit) showed
the pipeline fails on two simple job boards and *why*:

| Site | What it actually is | What the pipeline did |
|---|---|---|
| **aya** | public JSON API `api.ayahealthcare.com/AyaHealthcareWeb/job/search` — **26,889 jobs**, no auth, 54 GETs | scraped the 10-record *featured preview* `jobsData` blob; missed the API |
| **locumtenens** | stateless `POST /Resources/JobSearch/QuickSearch` → `302` → SSR HTML `SearchResults?sId=…&pgNum=N` (antiforgery token is cosmetic) | mislabeled "playwright/React"; built a 50 KB browser scraper; tripped JS validation; churned |

Root cause: the pipeline does **not** do the obvious human thing — *start at the
homepage, move toward the jobs, fire the search, look at what comes back.* It
follows fixed heuristics, trusts surface labels, and runs a sprawling 40-call
re-discovery agent. The agent audit also found the prompts self-contradict (e.g.
`build_code_writer_message` concatenates "do NOT follow two-phase" with "Two-Phase
REQUIRED"), which is a separate (parallel) fix.

## The model — navigation as graph traversal

Do what a human does, modeled as a bounded BFS over the site:

```
node = (url, reached_by, depth)        # reached_by: GET / POST-body / click
start = homepage
loop (BFS, bounded):
    visit node (HTTP preferred; browser only if CSR)
    GOAL-CHECK (deterministic): is this a job listing?
        yes -> record path, deduce mechanism + filters + pagination, DONE
    extract candidates: same-domain links {href,text} + a search-form "submit" action
    LLM-PRUNE candidates: drop off-goal (marketing/pay/about/login/...), keep on-goal
    enqueue survivors, continue
```

Two non-negotiable design rules:

1. **The LLM only prunes; it never confirms a listing.** The goal-check is
   *deterministic* (a captured API / embedded JSON / ≥N job links / results
   items). So a wrong LLM prune can never make us miss a real listing — it can
   only slow us down.
2. **Hard bounds.** `depth ≤ 3`, `≤ 12 page-visits`, `≤ 4 judge calls` (one
   batched call per node). Without these the traversal regresses to today's
   26-minute crawl. HTTP is preferred over browser (both target sites are SSR).

### Forms are first-class edges

A search form is a traversal edge ("submit-search"), not just a link. When a page
has a keyword box or a multi-select job-search form (Discipline/Specialty/
Location), the traversal fills it (query for text fields; the first valid
`<option>` for required selects — that's what satisfies locumtenens' required
`Specialties`) and follows the result. This is the "fire the search" step.

### From the winning path → mechanism + properties

Mechanism precedence: `api > form_post_ssr > embedded_json > detail_links`.
- A verified backend JSON API (aya) → `api`.
- A POST that returned SSR results (locumtenens) → `http_requests` (form-POST→SSR).
- Determined from the actual response, not a label.

Filters (`<select>` controls) and pagination (`?pgNum=`/`?page=`/`a[rel=next]`)
are read off the goal page.

## Reuse (read-only; no existing agent is modified)

- `agents.nodes.url_judge.judge_candidate_urls` — the per-step LLM prune (the only
  existing-code dependency; imported lazily).
- The small detectors (link filter, job-href regex, results-item count, inline-API
  URL finder + empirical GET-verify, JSON items/count extractor) are **re-implemented
  in the prototype** to keep it self-contained and unit-testable with the LLM + HTTP
  mocked. (They mirror `navigate_explore.py`'s detectors but don't import the heavy
  module.)

## Files (all new)

```
experimental/nav_traversal/
  traversal.py          # the bounded LLM-pruned BFS driver + standalone detectors
  run_traversal.py      # CLI runner: prints path/mechanism/filters/pagination + PROOF
  test_traversal.py     # unit tests, LLM + HTTP mocked
docs/navigation-traversal-design.md   # this file
```

## Expected outcomes (PROVEN live)

- **aya** → traversal prunes `/travel-nursing/` (marketing), reaches the jobs page,
  detects `api.ayahealthcare.com/AyaHealthcareWeb/job/search` by fetching the page's
  external `ayaSearchMenus.js` bundle and reconstructing the JS-built URL, verifies
  by GET → `api` → **26,890 jobs**. Proof fires the API. *(Live: REACHED=True,
  mechanism=api, count=26890, 1 page visited — aya loads the search bundle on the
  homepage so the goal-check hits at depth 0.)*
- **locumtenens** → traversal reaches `/Resources/JobSearch/QuickSearch`, detects
  the POST form, submits `Specialties=<first option>` → `302` → SSR `SearchResults`
  → `http_requests`. *(Live: REACHED=True, mechanism=http_requests, 3 pages visited,
  26 off-goal links pruned; proof POST Specialties=312 → 77 result cards, "1-25 of
  73".)*

Both proven with **plain HTTP, no browser, no auth** — exactly the simple path a
human would write.

## Critique & risks

- **Slowness** — browser traversal is slow; the bounds are mandatory and HTTP is
  preferred. If the prototype exceeds the bounds on a site, it's no better than
  today's agent.
- **Action set** — kept to {follow-link, submit-search}. Generalizing into a
  click-everything agent would re-introduce the bloat this is meant to remove.
- **API detection without a browser** — parses inline scripts for API URL
  templates and *verifies by firing a GET* (the user's "fire it and look"). If a
  site builds the URL in a way no regex catches, fall back to ONE browser `/render`
  to capture the XHR. State which path each site takes.
- **LLM misjudge** — covered by the deterministic goal-check (a real listing is
  never pruned) and, in a future integration, a fallback to the existing
  `navigation_agent` for genuinely hard sites.

## Verification

1. **Unit tests** (`python experimental/nav_traversal/test_traversal.py`): aya-style
   fixture → prunes marketing, finds API (count 26889), mechanism `api`;
   locumtenens-style fixture → fires the POST, reaches SSR results, mechanism
   `http_requests`; budget-exhaustion → best partial. **All 3 pass.**
2. **Live** (`python experimental/nav_traversal/run_traversal.py <url> <ct> <query>`):
   aya → ~26,889 jobs via API; locumtenens → POST→SSR results.
3. **No existing agents touched** — `git status` shows only `docs/` and
   `experimental/nav_traversal/`.

## Out of scope (deliberate)

Modifying any existing agent; integrating the traversal into the graph; the
`code_writer` de-bloat; the audit's deep node-collapse. All deferred until this
prototype is proven live on aya + locumtenens. Integration would be a separate plan.

## Future integration sketch (NOT now)

If proven: the traversal node replaces `navigation_explore` + the unconditional
`navigation_agent` + `navigation_synthesize` on the happy path (keeping
`navigation_agent` as the bounded-budget fallback). Its output is the existing
`navigation_analysis.json` shape, so `code_writer` consumes it unchanged — paired
with the `code_writer` de-bloat (emit one data-model section; split the prompt;
drop SeleniumBase; pin the instruction message against truncation).
