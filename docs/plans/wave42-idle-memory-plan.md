# Wave-42 — Idle-Memory: MCP tab disposal + idle MCP-Chrome recycle

**Status:** BUILT + LOCAL GATE GREEN (2026-09-24) — suite 3629 passed / 4 known reds / 2 skipped; ruff clean on touched gated files; dev live-verified (disposal navigate + fresh-session landing, walk-claim endpoint + real 4200s claim from django). Both flags default OFF.
**Evidence base:** docs/railway-memory-optimization-review.md (7-day RAM series), tab-lifecycle audit (2026-09-24), dev-verified MCP experiments (this doc).
**Priority frame from user:** "the system is working very well now it should not break" → every change flag-OFF by default, guards before savings, critique pass below.

## Context

Railway bills actual RAM-seconds (~$10/GB-month). The fleet idles at ~3–5 GB continuous.
The dominant avoidable slice is **browser-service's idle band (0.5–2.5 GB)**, root-caused to:

1. **The MCP Chrome is never recycled** (`recycle_policy.py:13` — "never MCP"). Its floor is
   ~250–400 MB plus every tab it retains.
2. **The walk tab is never disposed after a job.** `browser_traverse` ends with a quiesce
   (`experimental/nav_traversal/traversal.py:2280-2282`) — the last job's listing page
   renderer (~50–150 MB, `server.py:1180-1183` own estimate) stays resident until a Chrome
   restart. Dev reproduced it: one `https://example.com/` walk tab held alive for 39 h.
3. **The reaper cannot fix this**: `_reap_tabs_sync` keeps the OLDEST `MCP_TAB_KEEP=4` http
   tabs (`server.py:1424`), and the walk tab IS the oldest — exempt by construction.
4. The MCP **node process** accumulates per-tab network records across jobs (the wave-38
   capture-bleed surface) and only a restart flushes them.

Dev-verified mechanics (2026-09-24, against the live dev MCP server, `@playwright/mcp`
0.0.78): `browser_navigate {url: "about:blank"}` succeeds; a fresh one-shot session lands on
the SAME tab (evaluate returned `about:blank`); `/json/list` then shows the heavy renderer
gone, tab identity preserved, no tab growth. The W38-A1 wrong-site gate already classifies
`about:blank` as a navigation state that never feeds its counter (`traversal.py:2115-2118`).

## Tasks

### T1 — Post-walk tab disposal (celery side; flag `NAV_TAB_DISPOSAL`, default OFF)

`browser_traverse` becomes a thin wrapper over `_browser_traverse_impl` (body unchanged —
no 270-line reindent):

```python
def browser_traverse(start_url, content_type, query, *, mcp_tools=None, ...):
    try:
        result = _browser_traverse_impl(start_url, content_type, query, mcp_tools=mcp_tools, ...)
    finally:
        _dispose_walk_tab(mcp_tools)   # never raises
    return result
```

`_dispose_walk_tab(mcp_tools)`:
- Reads `NAV_TAB_DISPOSAL` env at call time; OFF → return immediately.
- Resolves `playwright_browser_navigate` (+ `playwright_browser_evaluate` fallback) from
  `mcp_tools`; absent → return.
- `nav.invoke({"url": "about:blank"})`; on any exception → one evaluate fallback
  (`location.replace('about:blank')`); on any exception → `logger.debug` and done.
- All exceptions swallowed: disposal can never alter a walk result or fail a node.
- Runs inside the traversal lock window (graph.py:4122 holds it around the walk), so
  serialized walks cannot collide with it.

File: `experimental/nav_traversal/traversal.py` (+ graph.py untouched).

### T2 — Idle MCP-Chrome recycle (browser-service; flag `MCP_IDLE_RECYCLE`, default OFF)

**Guard discovery that shaped this task:** every MCP tool call is a ONE-SHOT SSE session
(`webapp/agents/tools/playwright_tools.py:292-349`), so `_mcp_client_connected()` is only
true *while a call is in flight* — between a walk's steps (150 s+ LLM turns) it reads false.
The reaper tolerates this because keep-oldest preserves the walk tab; a RESTART must not.

1. **Walk-claim endpoint** (browser-service `server.py`): `POST /mcp/walk-claim`
   `{"ttl_s": N}` → `_MCP_WALK_CLAIMED_UNTIL[0] = monotonic() + ttl`. Airtight walk-window
   signal; TTL makes a crashed claimant self-heal.
2. **Celery claim-sender**: top of `_invoke_navigation_traverse` (graph.py:4096) fires a
   best-effort `POST {BROWSER_SERVICE_URL}/mcp/walk-claim` with
   `ttl_s = NAV_TRAVERSE_MAX_TIMEOUT + 600`, fired unconditionally, failures swallowed
   (covers walk + re-traverse + navigate_explore fallback; covers whole node budget).
3. **Recycle decision** — pure logic in `recycle_policy.py` (stdlib home, directly testable):
   `mcp_recycle_due(flag, claim_until, now, last_recycle, cooldown_s, client_connected)`:
   fires only when flag ON **and** `now > claim_until` **and** not `client_connected`
   **and** `now - last_recycle >= cooldown_s` (`MCP_IDLE_RECYCLE_COOLDOWN_S`, default 6 h).
4. **Maintenance hook** in `_periodic_cleanup` after the scraper-recycle leg, dispatched on
   `RESTART_EXECUTOR`: calls `browser_pool.restart_chrome("mcp")` → `sleep(2)` →
   `_start_mcp_process()` — the exact proven sequence of the CDP-liveness auto-restart
   (`server.py:1578-1592`). On errors: log only; liveness auto-restart is the backstop.
   On success: stamp `last_recycle`.

A restart flushes retained tabs AND the node process's network records; next walk lands on
a fresh blank tab and navigates as usual.

### T3 — (variable, user-scheduled) `MCP_TAB_KEEP=1`

Railway variable on browser-service (env-backed at `server.py:418`; `MCP_TAB_HARD_CAP=8`
stays). Requires a browser-service redeploy — schedule in a quiet window, it kills in-flight
jobs. Honest expectation-setting: with T1 still OFF, keep=1 pins the OLDEST tab — the walk
tab itself — so C alone trims popup/excess-tab piles, not the pinned renderer. Full value
arrives with T1/T2 enabled.

## CRITIQUE — every way this could break the working system, and the answer

| # | Attack | Answer |
|---|---|---|
| 1 | Disposal navigates away a tab someone still needs | The walk result is fully computed before `finally` runs; every consumer (product_analyzer, tester) navigates explicitly and never assumes the tab's page. Capture (`_capture_api_from_session`, `_extract_item_links`) completes before every `return` in the impl (verified traversal.py:2206-2271). |
| 2 | A blank tab trips the W38-A1 wrong-site abort | Gate requires non-empty registrable to feed the counter; `about:blank` → `_s_reg == ""` → "navigation state", explicitly never feeds (`traversal.py:2115-2118`). Dev-verified landing works. |
| 3 | MCP rejects `about:blank` | Empirically rejected: dev MCP 0.0.78 returned `page.goto('about:blank')` success (2026-09-24). Residual risk covered by evaluate fallback + swallow-all. |
| 4 | Disposal fires while ANOTHER walk is mid-flight (lock-free proceed path) | Same-tab interleaving is today's existing accepted risk (proceeds-without-lock walks interleave pages, graph.py:3934-3937); `about:blank` is the most benign possible interleave — the gate tolerates it and their next goto recovers. Flag-off default until gate evidence. |
| 5 | Idle recycle kills a walk mid-step | Cannot fire while a claim is live (T2.2 stamps the whole node budget incl. re-traverse/fallback) NOR while a tool call is in flight (live client check). Claim TTL self-heals a crashed claimant. |
| 6 | Idle recycle kills an analyzer/tester MCP phase | Their calls are one-shot; a restart *between* two calls leaves the next call reattaching to the fresh Chrome on its blank tab — the phase's next call navigates explicitly, so it degrades to a retry blip, not corruption; restart *during* a call drops that call → retry ladder (`playwright_tools.py:373-389`). Accepted residual, documented here. |
| 7 | Recycle races the CDP-liveness auto-restart | `restart_chrome` holds `_restart_lock` (RLock) — serialized. A liveness probe during the dead window adds 1 of 3 required consecutive failures (45 s) — a normal restart completes well inside tolerance. |
| 8 | Recycle fires while the container is busy elsewhere | Decision only claims the MCP Chrome; Scraper Chrome, ephemeral browsers, /scrape lanes untouched. Cooldown (6 h default) bounds exposure frequency. |
| 9 | Walk-claim POST fails / browser-service lacks endpoint | Swallowed; recycle then relies on the live-client check alone — the system falls back toward today's behavior, never worse than it. |
| 10 | Flag wiring mistakes ship behavior changes | Both flags read env at call time, default OFF = today's exact behavior. Railway enablement is a separate, deliberate step AFTER gate evidence. |
| 11 | Wrapper rename breaks imports/monkeypatches | Public name, signature, and module stay identical; graph.py imports `browser_traverse` unchanged. |
| 12 | Restart wipes a profile state the scrapers need | MCP Chrome profile (`/tmp/chrome-profiles/mcp`) persists on disk across restarts — same as today's crash-restart path; nothing session-critical lives only in RAM. |

Deploy order (standing rule): django+celery BEFORE browser-service.

## Verification

1. Unit (new, tests/):
   - T1: fake tools — flag OFF → no disposal invoke; flag ON → `about:blank` invoked after
     result; disposal raise → swallowed, result intact; `mcp_tools=None` → no crash.
   - T2: decision matrix for `mcp_recycle_due` (each guard independently blocks); walk-claim
     endpoint sets/overruns TTL; maintenance hook no-ops flag-off.
2. Full suite `pytest ../tests .` from /app/webapp (baseline 3607 pass / 4 known reds).
3. Local live gate (compose): drive one real navigation job with `NAV_TAB_DISPOSAL=1` →
   after the node, `curl browser_service:19222/json/list` shows `about:blank` where the
   listing page was; walk verdict unchanged.
4. Local recycle gate: `MCP_IDLE_RECYCLE=1`, cooldown=60 s → watch maintenance log line,
   confirm `/json/list` resets to a single blank tab and a follow-up MCP evaluate lands
   healthy; confirm a walk-claim TTL blocks firing.
5. Prod enablement (separate, after evidence): set `NAV_TAB_DISPOSAL=1` (celery) +
   `MCP_IDLE_RECYCLE=1` (browser-service) + `MCP_TAB_KEEP=1` in one quiet-window deploy
   cadence; watch `mcp_page_count` in /health and the memory series for a week.
