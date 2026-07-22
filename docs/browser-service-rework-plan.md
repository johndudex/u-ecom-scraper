# Browser-Service Rework — Execution Model Migration

**Status:** Steps 1-4 IMPLEMENTED + tested. Step 5 Phase A (observation) deployed.
**Branch:** `lg-upgrade` (changes on working tree)
**Date:** 2026-07-15 (implementation); 2026-07-14 (design)

> **Implementation status:**
> - **Step 1** ✅ — `POST /navigate` endpoint live and tested (basic nav, CSS extraction, cloak stealth, actions, concurrency 429, orphan killer fix). 601 lines added.
> - **Step 2** ✅ — `templates/http_navigation_scraper.py` created. httpx + BeautifulSoup, zero Playwright imports, two-phase discovery + extraction, per-page retry, checkpoint.
> - **Step 3** ✅ — `_run_in_process` heartbeat fix (CRITICAL — was missing), timeout 3600→7200, `SCRAPER_EXECUTION_MODE` feature flag.
> - **Step 4** ✅ — `http_navigation` strategy in subagents.py + code-writer.md prompt + route_after_testing.py + graph.py `_enforce_anti_bot_strategy`. 8/8 tests pass.
> - **Step 5** 🔄 Phase A deployed (deprecation log on /scrape). Phases B-D pending migration of 6 legacy scrapers.
**Scope:** Move generated scrapers out of the `browser_service` subprocess model into
in-process HTTP-per-page execution against a new `POST /navigate` endpoint.

> **Governing principle:** every step is independently shippable and independently
> revertable. Nothing in this plan requires a big-bang cutover. The subprocess path
> stays alive until Phase D explicitly removes it.

---

## 1. Context & Problem

### How execution works today

`run_execution` (`webapp/agents/nodes/run_execution.py:181`) dispatches a generated
scraper via `_needs_browser(...)`:

- **Browser scrapers** → `_run_via_browser_service(...)` POSTs `scraper_path` to
  `POST /scrape` (`browser_service/server.py:845`). `/scrape` calls
  `scraper_runner.run_scraper_script` (`scraper_runner.py:110`), which runs the
  scraper as `subprocess.run(["python3", scraper_path] + args)`. The subprocess
  attaches (via CDP) to a single long-lived **Scraper Chrome** on port `19223`
  (proxied externally as `9223`), or — when `STEALTH_BROWSER=cloak` — launches its
  own CloakBrowser binary via the `.pth` monkeypatch.
- **Non-browser scrapers** → `_run_in_process(...)` runs `subprocess.run` directly
  inside the Celery worker.

### Why this is fragile

1. **Chrome crash = job death.** The scraper, the Chrome it drives, and the output
   it is writing all live in one process tree inside `browser_service`. When Chrome
   dies (OOM, `Target closed`, `Browser has been closed` — the markers enumerated in
   `scraper_runner.py:24-36`), `scraper_runner` retries by restarting Scraper Chrome
   up to `max_retries=3`. Beyond that the whole scrape fails, even if 49 of 50 pages
   already succeeded. There is no per-page isolation.
2. **No partial progress on subprocess crash.** A crash mid-Phase-2 loses everything
   not yet checkpointed. The checkpoint in `navigation_scraper.py` only covers Phase 1
   discovery URLs (`discovered_urls_checkpoint.json`); Phase 2 has no resumption.
3. **Heartbeat asymmetry.** `_run_via_browser_service` starts a 240 s SessionLog
   heartbeat (`run_execution.py:467-475`) so the 30-min `cleanup_stuck_jobs` watchdog
   (`tasks.py:774`, `STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES=30`) doesn't reap it.
   `_run_in_process` does **not** (`run_execution.py:398-444`). Long in-process
   scrapes are silently killed at 30 min.
4. **Tight memory budget.** `browser_service` is capped at `mem_limit: 1536m`
   (`docker-compose.yml:190`) and must hold: uvicorn, Xvfb, **two** persistent
   Chromes (MCP `19222` + Scraper `19223`), the Playwright MCP node process, plus
   every ephemeral browser the probe helpers launch, plus the scraper subprocess and
   its Chrome child. Each scraper subprocess + Chrome child peaks ~250-450 MB.
5. **`/scrape` has no concurrency control.** No lock, no semaphore — multiple
   scrapers share the single Scraper Chrome CDP endpoint. The only implicit bound is
   `uvicorn --workers 1` + the default thread pool.
6. **Ownership hack.** `browser_service` runs as root; Celery/Django run as uid 1000.
   `scraper_runner._post_run` (`scraper_runner.py:70-107`) walks the scraper dir and
   `os.chown(..., 1000, 1000)` every file so the worker can read output. This is
   fragile and only exists because output is written inside the wrong container.

### What does NOT change

- The probe pipeline (`/probe`, `/probe-akamai`, `/render`) stays as-is. Probes are
  short, lock-serialized, and already launch ephemeral browsers.
- The Playwright MCP agent-browsing path (`/sse`, port `8111`) is untouched.
- Generated **non-browser** scrapers (`requests`/`api`/`shopify`/article/job/forum/
  generic templates) already run in-process and are unaffected.

---

## 2. Target Architecture

```
                            CELERY WORKER (1536m)                         BROWSER_SERVICE (1536m)
                            ┌──────────────────────────┐                  ┌──────────────────────────────────┐
  LangGraph                 │ run_execution node       │                  │ FastAPI (uvicorn, 1 worker)      │
  run_execution ───────────▶│  _needs_browser()?       │                  │                                  │
        ▲                   │   ├─ False (httpx import)│                  │  POST /probe     [PROBE_LOCK]     │
        │                   │   │  → _run_in_process   │   POST /navigate │  POST /render    [PROBE_LOCK]     │
        │ checkpoint +      │   │     + heartbeat      │ ──── (per page) ▶│  POST /navigate  [NAVIGATE_SEM=3] │
        │ output.json       │   │     + ThreadPool     │                  │      └─ ephemeral Playwright/     │
        │                   │   │       (max 4)        │                  │         cloak browser per call    │
        │                   │   └─ True (legacy)       │   POST /scrape   │  POST /scrape    [no lock]        │
        │                   │      → /scrape (legacy)  │ ──── (legacy) ▶  │      └─ scraper_runner subprocess  │
        │                   │                          │                  │                                  │
        │                   │ httpx ─► BROWSER_SERVICE │                  │  persistent: MCP Chrome (19222)   │
        │                   │   _url (env, already set)│                  │               Scraper Chrome(19223)│
                            └──────────────────────────┘                  └──────────────────────────────────┘
```

### The contract

A generated **HTTP navigation scraper** runs entirely inside the Celery worker. It
uses `httpx` to call `POST /navigate` once per page. Each call is an isolated unit:

- `browser_service` launches a fresh browser (Playwright or CloakBrowser) for that
  call, performs the requested actions, extracts the requested selectors, returns
  HTML + data + the final URL, then tears the browser down.
- The scraper does all **link discovery, JSON-LD parsing, field extraction, and
  pagination** locally on the returned HTML — no shared browser session, no CDP
  attach, no subprocess.
- A Chrome crash inside one `/navigate` call returns `503` to the scraper, which
  retries that **one page** with exponential backoff. Other pages, other workers,
  and the rest of the job are unaffected.

### Why this is better

| Concern | Today (subprocess) | After (HTTP per page) |
|---|---|---|
| Chrome crash blast radius | Whole job fails after 3 retries | One page retries; rest untouched |
| Memory location | ~250-450 MB peak in `browser_service` per scrape | Ephemeral browser freed within the call; worker holds only HTML strings |
| Heartbeat | Asymmetric (browser path only) | Uniform — scraper runs in worker, heartbeat always applies |
| Partial progress | Phase 1 only (discovery URLs) | Per-page; Phase 2 retries resume from checkpoint |
| Concurrency control | None on `/scrape` | Explicit `NAVIGATE_SEMAPHORE=3`, HTTP 429 backpressure |
| Ownership | `chown` hack to fix root→1000 | Output written by Celery worker (uid 1000) directly |

---

## 3. Implementation Steps

Each step lists **what changes**, **what files**, the **API/contract**, and
**corner cases**. Steps are ordered by dependency in §4, not by number.

### Step 1 — `POST /navigate` endpoint (browser_service, server-side)

**Goal.** Add a stateless, per-call browser endpoint that probes a single URL,
performs a small action script, and returns everything the scraper needs to do
extraction locally.

**Files.**
- `browser_service/server.py` — new route + `NavigateRequest` / `NavigateResponse` Pydantic models; new `NAVIGATE_SEMAPHORE = asyncio.Semaphore(3)`.
- `browser_service/probe.py` — refactor the four duplicated `_try_*` helpers (`_try_playwright` `probe.py:465`, `_try_cloak` `probe.py:547`, `_try_uc_chrome` `probe.py:644`, `_try_direct_http`) into three reusable functions:
  - `_launch_page(method, proxy_tier, country) -> (browser, page)` — single launch path covering plain Playwright **and** direct `cloakbrowser.launch()` (the pattern `_try_cloak` already uses — Option 2, **not** the `.pth` monkeypatch).
  - `_run_actions(page, actions[]) -> final_url` — apply `fill`/`select`/`click`/`wait`/`sleep`/`evaluate` in order; return the post-submit URL.
  - `_extract_page_data(page, selectors) -> dict` — factored from the copy-pasted block (`is_blocked`, `extract_jsonld`, `extract_meta_tags`, `extract_title`, body-text snippet, selector results). The probe helpers then call the same functions.

**Request / response contract (load-bearing — Step 2 depends on every field).**

```jsonc
// POST /navigate
{
  "url":          "https://shop.example.com/search",
  "actions":      [ {"type":"fill","selector":"#search","value":"boots"},
                    {"type":"click","selector":"button[type=submit]"},
                    {"type":"wait","state":"domcontentloaded"},
                    {"type":"sleep","ms":8000} ],
  "extract":      { "selectors": {"price": "span.price", "title": "h1"} },
  "stealth":      "cloak",          // "none" | "cloak" ; drives _launch_page
  "proxy_tier":   "none",           // "none" | "datacenter" | "residential"
  "timeout":      60                // seconds, per-call cap
}

// 200 OK
{
  "success":      true,
  "url":          "https://shop.example.com/search?q=boots&page=2",  // FINAL url (post-actions) — MANDATORY
  "html":         "<!doctype html>...",
  "data":         { "price": ["$89"], "title": ["Search results"] }, // selector results
  "blocked":      false,
  "cookies":      [{"name":"sessionid","value":"...","domain":"..."}],
  "method_used":  "cloak",
  "error":        null
}

// 503 (Chrome crash / pool exhausted)
{ "success": false, "error": "browser launch failed", "retry_after": 5 }

// 429 (NAVIGATE_SEMAPHORE full)
{ "success": false, "error": "navigate concurrency limit reached", "retry_after": 2 }
```

**Action types** (the closed set the scraper may emit): `fill`, `select`, `click`,
`wait` (`state` ∈ `domcontentloaded|load|networkidle`), `sleep` (`ms`), `evaluate`
(raw JS). Mirrors what `navigation_scraper.py` does inline today via
`page.fill/select/click/wait_for_load_state/wait_for_timeout/eval_on_selector_all`.

**Concurrency & isolation.**
- `NAVIGATE_SEMAPHORE = 3` (asyncio) — bounds concurrent ephemeral browsers to the
  memory budget. Excess callers get `429 + retry_after`.
- **Independent of `PROBE_LOCK`** — probes and `/navigate` do not block each other.
- **Ephemeral browser per call.** No CDP attach, no reuse of the persistent Scraper
  Chrome (port `19223`). Browser/context/page created in the handler, closed in a
  `finally`.
- **Chrome crash → `503 + retry_after`** (do not return 500). Caller retries.

**Corner cases.**
- The `url` field in the response **MUST be the post-actions URL** (after the search
  submit). If the server returns the request URL, URL-constructed pagination in
  Step 2 breaks silently. This is Risk 1.
- `_periodic_cleanup` (`server.py`, every 1800 s) runs `_kill_orphan_chrome` — pgrep
  for `chrome`, SIGKILL anything not in `PERSISTENT_CHROME_PIDS`. It **MUST** be
  taught to also exclude PIDs belonging to in-flight `/navigate` calls, or it will
  kill ephemeral browsers mid-scrape (Risk 5). Track active navigate browser PIDs in
  a module set; union into the persistent-PID allowlist before killing.
- `stealth="cloak"` uses `cloakbrowser.launch()` directly (Option 2) — **not** the
  `.pth` monkeypatch. This is the same pattern `_try_cloak` uses today, so it
  already works; we just stop relying on the global patch for this path.

---

### Step 2 — `http_navigation_scraper.py` template (scraper code-side)

**Goal.** A new scraper template that does two-phase discovery + extraction using
only `httpx` against `/navigate` — no Playwright import, no CDP attach.

**Files.**
- `templates/http_navigation_scraper.py` — new (mirrors `navigation_scraper.py`'s
  744-line structure but swaps the Playwright body for httpx).
- Reuses `src/page_analysis.extract_jsonld` (`src/page_analysis.py:61`),
  `src.proxy.build_proxy_url`, and the checkpoint pattern from
  `navigation_scraper.py:66-96`.

**Constants / placeholders.** Same substitution surface as `navigation_scraper.py`
so `code_writer`'s existing fill logic carries over: `{SITE_NAME}`, `{SITE_URL}`,
`{PLATFORM}`, `{SITE_SLUG}`, `{SEARCH_URL_PATTERN}`, `{SEARCH_BOX_SELECTOR}`,
`{SEARCH_SUBMIT_SELECTOR}`, `{CATEGORY_URLS}`, `{PAGINATION_TYPE}`
(`page_param`/`next_button`/`infinite_scroll`), `{NEXT_BUTTON_SELECTOR}`,
`{PAGE_PARAM_NAME}`, `{ITEMS_PER_PAGE}`, `{MAX_PAGES}`, `{TOTAL_COUNT_SELECTOR}`,
`{ITEM_CONTAINER_SELECTOR}`, `{ITEM_LINK_SELECTOR}`, `{ITEM_URL_PATTERN}`,
`{SCRAPING_METHOD}`, `{PROXY_TIER}`, `{DELAY_BETWEEN_REQUESTS}`, `{OUTPUT_KEY}`,
`{CONTENT_TYPE}`, `{CURRENCY}`.

> **Note on `STEALTH`.** There is **no** `{STEALTH}` placeholder in
> `navigation_scraper.py` today — stealth is a runtime concern (the `STEALTH_BROWSER`
> env var consumed by `scraper_runner.py:143`). The new template introduces a plain
> module constant `STEALTH = "cloak"` (or `"none"`) that it forwards in every
> `/navigate` body. This is a net-new pattern, deliberately explicit so the scraper
> is self-describing rather than env-coupled.

**Structure.**

```python
import httpx, lxml.html, json, ...
from src.page_analysis import extract_jsonld
from src.proxy import build_proxy_url

BROWSER_SERVICE_URL = os.environ["BROWSER_SERVICE_URL"]   # already in celery env
STEALTH = "cloak"                                          # {site-specific}
# ... same placeholders as navigation_scraper.py

def _navigate(url, actions=None, extract=None, *, retry=0):
    """POST /navigate with exponential backoff. Returns response dict or raises."""
    # retryable: 5xx, 429, httpx.TimeoutException, httpx.ConnectError
    # terminal:  404 (page genuinely gone), blocked=True (anti-bot; do NOT retry)

# ---- Phase 1: discovery ----
def _discover_via_search(query, max_pages):
    r = _navigate(SEARCH_URL_PATTERN.replace("{query}", query),
                  actions=[{"type":"fill",...}, {"type":"click",...},
                           {"type":"wait","state":"domcontentloaded"},
                           {"type":"sleep","ms":8000}])
    final_url = r["url"]            # post-submit URL — drives pagination
    links = _extract_item_links(r["html"])      # lxml/bs4, same 3-tier fallback
    next_url = _next_page_url(final_url, page=2)  # construct ?page=N or click action
    ...
    _write_checkpoint(all_links)   # discovered_urls_checkpoint.json (Celery-side now)

# ---- Phase 2: extraction (concurrent) ----
def _extract_one(item_url):
    r = _navigate(item_url, extract={"selectors": FIELD_SELECTORS})
    jsonld = extract_jsonld(r["html"])           # reuse existing helper
    fields = _parse_fields(r["html"], jsonld)    # CONTENT_TYPE-aware
    return fields

with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(_wrap_try_except(_extract_one), discovered_urls))
```

**Concurrency.** `ThreadPoolExecutor(max_workers=4)` for Phase 2. With
`NAVIGATE_SEMAPHORE=3` server-side, one of the four workers will routinely get `429`
on a tight site — handled by `_navigate`'s backoff (Risk 2). Do **not** raise
`max_workers` above 4 without raising the semaphore.

**Session continuity.** The post-submit `final_url` (returned by `/navigate`)
carries any session/query params the form submit added — use it as the base for
`page_param` pagination. Optionally forward cookies: the first `/navigate` response
includes `cookies[]`; subsequent calls can pass them back in a `cookies` field (TBD
whether needed — most sites encode session in the URL).

**Retry policy (per page).**
- **Retryable** (exponential backoff, base 2, cap 60 s, ~4 attempts): HTTP `5xx`,
  `429`, `httpx.TimeoutException`, `httpx.ConnectError`.
- **Terminal** (do not retry, record as error item, continue): HTTP `404`,
  `blocked=true` (anti-bot wall — retrying just burns the budget).

**Checkpoint.** Phase 1 writes `discovered_urls_checkpoint.json` next to the scraper
(on the Celery-side filesystem now, not inside `browser_service`). On restart,
Phase 1 is skipped and Phase 2 resumes. This is unchanged from
`navigation_scraper.py:578` in intent, just located correctly.

**Error handling.** Per-item `try/except` inside `_extract_one`; failures become
error dicts (`status_code: 0`, `remarks: "Error: ..."`) appended to output, exactly
as `navigation_scraper._error_item` does today. The job does not fail on item
errors.

**No Playwright imports.** This is what makes `_scraper_needs_browser`
(`shell_tools.py:61`, the substring scan for `playwright`/`seleniumbase`/etc.)
return `False`, routing the scraper to `_run_in_process` for free. No detection
changes needed in Step 3 for routing to work.

**Corner cases.**
- The local link extractor must reproduce `navigation_scraper._extract_item_links`'s
  3-tier fallback (container+link → bare link → `a[href]` scan filtered by
  `_is_product_url`). The regex in `{ITEM_URL_PATTERN}` is the primary filter.
- `time.sleep(8)` after submit (the HARD RULE in `code-writer.md:177-179`) becomes a
  `{"type":"sleep","ms":8000}` action — same semantics, server-side.

---

### Step 3 — `run_execution` routing + heartbeat fix (Django/Celery)

**Goal.** (a) Fix the pre-existing heartbeat hole that the new model makes worse,
(b) raise the in-process timeout, (c) add an explicit execution-mode feature flag.

**Files.**
- `webapp/agents/nodes/run_execution.py` — add heartbeat to `_run_in_process`
  (`:398`); raise its timeout `3600 → 7200`; optional `SCRAPER_EXECUTION_MODE` switch.
- `webapp/config/settings.py` — define `BROWSER_SERVICE_URL` once (today it is
  re-read from env in 7+ modules with dead `getattr(settings, ...)` fallbacks); add
  `SCRAPER_EXECUTION_MODE` if adopted.

**Routing — already correct, no detection change.** `_scraper_needs_browser`
(`shell_tools.py:61`) is a lowercased substring check for
`{seleniumbase, undetected_chromedriver, selenium, playwright.sync_api, playwright}`.
An `httpx`-based template imports none of these → returns `False` → `_run_in_process`
is selected. The new template routes itself.

**Critical fix: heartbeat.** `_run_in_process` currently runs a bare
`subprocess.run(..., timeout=3600)` with **no** `_start_heartbeat` call. The
`cleanup_stuck_jobs` watchdog (`tasks.py:774`) reaps any `RUNNING` job whose newest
`SessionLog` row is older than 30 min. So any in-process scrape longer than 30 min
is marked `FAILED` (with the misleading message *"Worker process crashed ... Likely
OOM killed"*) even though the subprocess is still running. This bug exists today but
is masked because in-process scrapers are currently all short (`requests`/`api`).
The HTTP navigation template changes that — a 200-page discovery scrape can easily
exceed 30 min.

Fix: mirror `_run_via_browser_service` (`run_execution.py:467-475`):

```python
from webapp.agents.graph import _start_heartbeat, _stop_heartbeat
hb = _start_heartbeat(state["job_id"], "run_execution", interval=240)
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, cwd=cwd)
finally:
    _stop_heartbeat(hb)
```

This benefits **every** in-process scraper, not just the new one.

**Timeout.** Raise `3600 → 7200` to match `_run_via_browser_service` (`:460`) and the
`/scrape` body timeout (`ScrapeRequest.timeout` ceiling `7200`, `server.py:301`).

**Feature flag (optional but recommended).** Introduce `SCRAPER_EXECUTION_MODE`:

| Value | Behavior |
|---|---|
| `auto` (default) | `_needs_browser` decides — today's behavior, HTTP template routes in-process |
| `force_http` | Ignore browser detection; always `_run_in_process` (forces the new model even for legacy Playwright scrapers — only set once a Playwright scraper has been ported) |
| `force_scrape` | Always `/scrape` (rollback lane — see §4) |

No new env wiring is needed for the common case: `BROWSER_SERVICE_URL` is already
injected into `celery-worker` (`docker-compose.yml:87`).

**Multisource stays browser-only.** `_run_category_sources` (`run_execution.py:265`)
fans out navigation jobs across up to 5 category URLs via `/scrape`. Leave it on the
`/scrape` path — HTTP navigation scrapers self-discover categories via
`{CATEGORY_URLS}`, so multisource is redundant for them. (While here, fix the
inconsistency that `_run_category_sources:343-347` reads `BROWSER_SERVICE_URL`
directly from `os.environ` instead of via `_get_browser_service_url()`.)

**Corner cases.**
- The watchdog does **not** revoke the Celery task (`celery_app.control.revoke` is
  nowhere in `cleanup_stuck_jobs`). With the heartbeat in place this rarely matters,
  but a truly dead worker can still write back to an already-`FAILED` job. Out of
  scope for this rework; flagged in §6.
- `ScrapeJob` has no `last_heartbeat` column — liveness is inferred from
  `SessionLog`. The heartbeat therefore writes a `SessionLog` row, not a field update.

---

### Step 4 — `code_writer` emits the HTTP template

**Goal.** Make `http_navigation` the default strategy for browser-needing sites, with
the LLM authoring scrapers against the `/navigate` calling pattern.

**Files.**
- `webapp/agents/subagents.py:1806-1827` — template-selection branch in
  `build_code_writer_message`. Add `http_navigation` ahead of the existing
  `navigation_scraper.py` branch.
- `webapp/agents/subagents.py:2737-2743` — `code_reviewer` capability matrix string.
  Add an `http_navigation` row.
- `webapp/agents/route_after_testing.py:16` — add `http_navigation` to
  `_HTTP_LIKE_STRATEGIES` so the "items==0 → switch strategy" rule applies.
- `webapp/agents/route_after_testing.py:358-376` — anti-bot route flip. Today it
  flips back to `("scraper", ...)` when strategy ∈
  `("playwright","stealth_browser","seleniumbase_uc","")`. Extend to also recognize
  `http_navigation + cloak` as a valid anti-bot combo (don't flip it away).
- `webapp/agents/graph.py:175-228` — `_enforce_anti_bot_strategy` rewrites strategy
  to `"playwright"` when anti-bot detected. Change the target to `"http_navigation"`
  (the new preferred anti-bot strategy, since `/navigate` supports `stealth:"cloak"`).
- `.opencode/agents/code-writer.md` — add the `http_navigation` row to the template
  table (`:148-154`); teach the `/navigate` calling pattern (the `_navigate` helper
  is pre-written in the template; the LLM fills actions/selectors).

**Strategy → template mapping.** Today this is an inline formula at
`subagents.py:1806`, not a registry. Add the branch:

```python
if input_mode in ("navigation", "list_page", "search_term") and mechanism in _browser_strategies:
    if strategy == "http_navigation":
        template_file = "http_navigation_scraper.py"
    else:
        template_file = "navigation_scraper.py"   # legacy Playwright path retained
```

**Backward compatibility.** The `playwright` strategy and `navigation_scraper.py`
template are **retained**. Existing generated scrapers under `scrapers/*/` keep
running via `/scrape`. Only **new** scrapers default to `http_navigation`.

**Rollback.** If the new template misbehaves, set the analyzer back to
`strategy: "playwright"` (or `_enforce_anti_bot_strategy` target back to
`"playwright"`) and everything falls back to the legacy path. No redeploy of
`browser_service` required.

**Corner cases.**
- The inline formula `f"{mechanism}_scraper.py"` (`subagents.py:1822`) is already
  buggy — it yields `http_requests_scraper.py` and `internal_api_scraper.py` which
  do not exist on disk (the LLM follows the prose table in `code-writer.md` and
  picks the right file anyway). Worth fixing while in here, but not blocking.
- Strategy strings are scattered across ≥8 overlapping constant sets (see Risk 7).
  Adding `http_navigation` means touching each one consistently.

---

### Step 5 — Deprecate `/scrape` + `scraper_runner` (phased)

**Goal.** Remove the subprocess execution path once all scrapers are HTTP-based.

**Phases.**

| Phase | Duration | Action | Rollback |
|---|---|---|---|
| **A — Observation** | 1 week | Log every `/scrape` invocation (caller, scraper_path, site). Confirm no production scraper still needs it. | — |
| **B — Migration** | 2-4 weeks | Re-run the 6 legacy browser scrapers (those using `navigation_scraper.py` / `playwright_scraper.py` / `undetected_chromedriver_scraper.py`) through the new pipeline so they regenerate as `http_navigation`. | Per-site: set `strategy:"playwright"` to regenerate legacy |
| **C — Soft removal** | — | `SCRAPER_EXECUTION_MODE=force_http` routes all callers to `_run_in_process`. `/scrape` still exists but is unreachable in default config. | Flip flag to `force_scrape` |
| **D — Hard removal** | — | Delete the dead code listed below. | Revert the commit |

**Dead code removed in Phase D.**
- `browser_service/scraper_runner.py` (entire file — `run_scraper_script`,
  `_post_run` chown hack, Chrome-death detection).
- `browser_service/server.py` — the `/scrape` route + `ScrapeRequest` model.
- `browser_service/cloak_stealth_patch.py` + the `.pth` install in `Dockerfile:58-59`
  + the stray `cloak_stealth.pth` at repo root. `/navigate` uses direct
  `cloakbrowser.launch()` (Option 2), so the global monkeypatch is no longer needed.
- The persistent **Scraper Chrome** on port `19223` (env `SCRAPER_CDP_PORT`) and its
  CDP proxy (`server.py:_start_cdp_proxy` `9223 → 19223`). No HTTP scraper attaches
  to it. MCP Chrome (`19222`) stays — Playwright MCP still uses it.
- The hardcoded `BROWSER_CDP_ENDPOINT = "http://127.0.0.1:9223"` in
  `scraper_runner.py:145` (goes away with the file).
- `run_execution._run_via_browser_service` and `_run_category_sources` collapse into
  the in-process path (or multisource is dropped — HTTP scrapers self-discover).

**Keep (refine, do NOT remove).**
- `_periodic_cleanup` / `_kill_orphan_chrome` — still needed to reap leaked
  ephemeral `/navigate` browsers, but **must** exclude active navigate PIDs (Step 1
  corner case). Refine the allowlist, don't delete the killer.
- `_periodic_cdp_liveness` — still guards MCP Chrome.

**Memory savings.** ~250-450 MB peak per scrape (no scraper subprocess + Chrome child
inside `browser_service`). The container's steady-state drops to: uvicorn + Xvfb +
MCP Chrome + MCP node + at most 3 ephemeral navigate browsers. Comfortable inside
1536 MB.

---

## 4. Migration Path

### Ordering (dependency-driven)

```
Step 1 ──┐                              (additive: new endpoint, nothing breaks)
         │
Step 3 ──┤ (independent)                (heartbeat fix benefits ALL in-process scrapers today)
         │
Step 2 ──┴──► Step 4 ────► Step 5       (template + prompt, then deprecation)
         (parallel: 2 & 4 can be built together)
```

1. **Step 1 first** — `POST /navigate`. Purely additive. No caller uses it yet; zero
   breakage risk. Ships behind the existing `/probe` / `/scrape` surface.
2. **Step 3 (heartbeat) in parallel** — the `_run_in_process` heartbeat fix is
   standalone and benefits every existing in-process scraper immediately. Ship it
   even if the rest of the rework slips.
3. **Step 2 + Step 4 together** — the template and the `code_writer` changes are
   co-dependent: the prompt must teach the pattern the template provides. Build them
   as one PR. Requires Step 1 deployed first (the template calls `/navigate`).
4. **Step 5 last** — phased deprecation only after all new scrapers run HTTP.

### Feature flags & rollback

| Lever | Where | Effect |
|---|---|---|
| `SCRAPER_EXECUTION_MODE=force_scrape` | `settings.py` (new) | Full rollback to subprocess model. `/scrape` path for everything. |
| `SCRAPER_EXECUTION_MODE=force_http` | `settings.py` (new) | Force in-process even for legacy Playwright scrapers (post-migration). |
| `strategy: "playwright"` in `scraper_analysis.json` | per-site | One site falls back to `navigation_scraper.py` + `/scrape`. No redeploy. |
| `_enforce_anti_bot_strategy` target | `graph.py:175` | Revert `http_navigation → playwright` to restore legacy anti-bot routing. |

Every step is revertable independently. Step 5 Phase D (hard delete) is the only
irreversible action, and it ships only after Phase B confirms zero `/scrape` usage.

---

## 5. Risk Register

| # | Risk | Step | Mitigation |
|---|---|---|---|
| 1 | **`/navigate` returns request URL, not post-submit final URL** → URL-constructed pagination breaks silently | 1↔2 | Make `final_url` a mandatory field in `NavigateResponse`; assert non-empty in the template's `_navigate` before parsing links. Integration test in §7. |
| 2 | **4 workers vs `NAVIGATE_SEMAPHORE=3`** → 1 worker routinely gets `429` | 2 | Expected; `_navigate` treats `429` as retryable with `retry_after` backoff. Do **not** raise `max_workers` above 4 without raising the semaphore. |
| 3 | **`_run_in_process` heartbeat missing** → 30-min watchdog kills long HTTP scrapes | 3 | Step 3 fix is mandatory before Step 2/4 ship. Without it, any 200+ page scrape is reaped. |
| 4 | **Ordering: `code_writer` emits `http_navigation` before `/navigate` exists** → generated scrapers 404 at runtime | 4↔5 | Step 1 must deploy before Step 4. Gate the `http_navigation` strategy branch on a server capability check (call `/health`, or just trust deploy order). |
| 5 | **Orphan Chrome killer (`_kill_orphan_chrome`, every 1800 s) kills ephemeral `/navigate` browsers** | 1 | Track active navigate browser PIDs in a module set; union into the allowlist before SIGKILL. Verified by §7 chaos test. |
| 6 | **3 concurrent ephemeral browsers + persistent MCP Chrome in 1536 MB** → OOM | 1 | `NAVIGATE_SEMAPHORE=3` is the bound; each ephemeral browser is closed in `finally`. Monitor `container_memory` in Phase A; if OOM-killed, drop semaphore to 2. |
| 7 | **Strategy constants scattered across ≥8 sets** → `http_navigation` added to some, missed in others → routing drift | 4 | Add a single `STRATEGY_GROUPS` registry (or at least a single `BROWSER_STRATEGIES` frozenset) and have every consumer import it. Audit: `_HTTP_LIKE_STRATEGIES`, `BROWSER_METHODS` (×3), `_browser_strategies`, `_bad`, `BROWSER_IMPORTS`. |
| 8 | **Watchdog doesn't revoke the Celery task** → dead worker writes back to FAILED job | 3 (out of scope) | Pre-existing. Heartbeat (Step 3) makes it rare. Separate PR to add `celery_app.control.revoke` in `cleanup_stuck_jobs`. |
| 9 | **Cookie/session continuity across `/navigate` calls** → paginated search loses session | 2 | Most sites encode session in the post-submit URL (handled by `final_url`). For cookie-dependent sites, forward `cookies[]` back into subsequent calls. Phase A observation will surface these. |
| 10 | **`/scrape` had no concurrency control; `/navigate` adds a semaphore** → throughput regression vs current model on high-volume sites | 1↔2 | `max_workers=4` × per-page latency is comparable to one Playwright scraper doing sequential pages. Phase A: benchmark before/after. |

---

## 6. What Disappears (after Phase D)

| Artifact | Location | Why gone |
|---|---|---|
| `scraper_runner.py` | `browser_service/` | No more subprocess scrapers |
| `/scrape` route + `ScrapeRequest` | `browser_service/server.py:845` | Unused |
| `_post_run` chown hack | `scraper_runner.py:70` | Output written by Celery (uid 1000) directly |
| `cloak_stealth_patch.py` + `.pth` | `browser_service/` + `Dockerfile:58-59` | `/navigate` uses direct `cloakbrowser.launch()` |
| Persistent Scraper Chrome (port `19223` / `SCRAPER_CDP_PORT`) | `browser_pool.py` | No CDP attach from scrapers |
| CDP proxy `9223 → 19223` | `server.py:_start_cdp_proxy` | Scraper Chrome gone |
| `_run_via_browser_service` | `run_execution.py:446` | Collapses into `_run_in_process` |
| `_run_category_sources` (or simplified) | `run_execution.py:265` | HTTP scrapers self-discover categories |
| Hardcoded `BROWSER_CDP_ENDPOINT = http://127.0.0.1:9223` | `scraper_runner.py:145` | File deleted |

**Kept (refined):** `_periodic_cleanup` / `_kill_orphan_chrome` (now excludes active
navigate PIDs), `_periodic_cdp_liveness` (still guards MCP Chrome), MCP Chrome
(`19222`), Playwright MCP (`/sse`, `8111`), the entire `/probe` pipeline.

---

## 7. Verification

Per-step end-to-end tests. Run in the agent playground (`docs/testing_guide.md`)
and against a real site.

**Step 1 — `/navigate`**
- `curl -X POST localhost:8001/navigate -d '{"url":"https://example.com","actions":[],"extract":{"selectors":{"title":"h1"}},"stealth":"none","proxy_tier":"none","timeout":30}'`
  → assert `success:true`, `url` equals final URL after any redirects, `html`
  non-empty, `data.title` present.
- Submit a search form: `actions=[fill,click,wait,sleep]` → assert `url` is the
  **post-submit** URL (Risk 1 regression test).
- Force a Chrome crash (kill the launched browser mid-call) → assert `503 +
  retry_after`, not `500`.
- Fire 5 concurrent calls → assert exactly 3 succeed, 2 get `429 + retry_after`.
- Run `_kill_orphan_chrome` during an in-flight call → assert the navigate browser
  is **not** killed (Risk 5 regression test).

**Step 2 — template**
- Generate `http_navigation_scraper.py` for a known site (e.g. the CK UK reference
  site from `docs/scraper_agents.md`). Run directly:
  `python scrapers/{slug}/scraper.py --sample 5` from inside the Celery container.
  → assert Phase 1 discovers ≥ the same links as the legacy `navigation_scraper.py`,
  Phase 2 extracts title+price for ≥80% of sample.
- Kill `browser_service` mid-Phase-2 → restart → re-run → assert Phase 1 is skipped
  (checkpoint hit) and Phase 2 resumes.
- Feed a 404 item URL → assert it is recorded as an error item, not a job failure.

**Step 3 — heartbeat + timeout**
- Submit a job whose in-process scraper `time.sleep(2400)` (40 min). Pre-fix: job
  reaped at 30 min. Post-fix: job runs to completion; `SessionLog` shows
  `[HEARTBEAT]` rows every 240 s.
- Assert `_run_in_process` timeout is `7200` (grep the literal or assert via a
  deliberately-long scraper hitting the cap).

**Step 4 — code_writer**
- Playground: run `scraper_analyzer → code_writer` for a navigation job on an
  anti-bot site → assert `scraper_draft.py` imports `httpx` (not `playwright`),
  contains `STEALTH = "cloak"`, and calls `_navigate(...)` with `stealth:"cloak"`.
- Assert `code_reviewer` does not flag `http_navigation + cloak` as a strategy
  mismatch.
- Assert `route_after_testing` treats `http_navigation` as http-like (items==0 →
  strategy switch path fires).

**Step 5 — deprecation**
- Phase A: grep container logs after 1 week → confirm `/scrape` call count and
  which sites still use it.
- Phase C: set `SCRAPER_EXECUTION_MODE=force_http` → run a previously-`/scrape`
  scraper → assert it now runs in-process (no `/scrape` log line).
- Phase D: after deletion, `docker compose build` succeeds; `browser_service`
  container starts; `/health` returns `ready:true`; `/probe` still works; MCP agent
  browsing still works.

**Full pipeline (post-Step 4).** Run a real `navigation` job end-to-end through the
Django UI. Assert: job completes without human approval; output JSON has
`metadata.scraping_duration_seconds` reasonable; `discovered_urls` and
`extracted_items` counts match expectation; no `browser_service` OOM-kills in logs.

---

## References

- `browser_service/server.py` — endpoints, `ScrapeRequest`, concurrency primitives
- `browser_service/probe.py` — `_try_*` helpers to refactor; `extract_jsonld` usage
- `browser_service/scraper_runner.py` — the subprocess path being deprecated
- `browser_service/browser_pool.py` — persistent Chrome lifecycle
- `browser_service/cloak_stealth_patch.py` — the `.pth` monkeypatch being removed
- `webapp/agents/nodes/run_execution.py` — `_run_in_process` (heartbeat gap), `_run_via_browser_service`, `_run_category_sources`
- `webapp/agents/tools/shell_tools.py:61` — `_scraper_needs_browser` (substring scan)
- `webapp/agents/route_after_testing.py` — `_HTTP_LIKE_STRATEGIES`, anti-bot flip
- `webapp/agents/graph.py:175` — `_enforce_anti_bot_strategy`; `:580` — `_start_heartbeat`
- `webapp/agents/subagents.py:1703` — `build_code_writer_message`; `:1806` template branch; `:2737` reviewer matrix
- `webapp/scraper/tasks.py:774` — `cleanup_stuck_jobs` (30-min watchdog)
- `templates/navigation_scraper.py` — the template being replaced (placeholders, checkpoint, ROTATE_EVERY)
- `src/page_analysis.py:61` — `extract_jsonld` (reused by the new template)
- `docker-compose.yml:87,100,190` — env + memory limits
- `docs/scraper_agents.md` — CK UK case study (reference site for verification)
