# Browser-Service Resilience + Truthful Health — Implementation Plan (v2, post-critique)

Status: v2 — folds in all three adversarial critiques (C1 correctness: REWORK,
C2 regression: SHIP-WITH-AMENDMENTS, C3 ops: SHIP-WITH-AMENDMENTS). Every v1
diagnosis anchor was independently re-verified by all three critics; the
amendments below fix **interactions between work items and missed consumers**,
not the diagnosis. v1→v2 changes are marked **[v2]**.

Evidence base: 5-way investigation 2026-08-29 — code forensics, 37-job prod
timeline, prior-knowledge consolidation, local reproduction (negative), Railway
runtime forensics (in flight; folds into §Tier-2 via the addendum rule at the
end of that section).

## The governing directive

> "these are not heavy workloads that chrome should fail"

Center of gravity: **footprint discipline + admission control + truthful
health**, not hardware raises. The numbers agree: local saturation with 3
concurrent ephemeral browsers cost ~+0.5 GB total and returned to baseline every
time; the container idles at ~1.3–2.2 GB on *persistent furniture* (2 Chromes +
Xvfb + MCP node driver), one item of which (Scraper Chrome :9223) is already
STAGED FOR REMOVAL by `docs/browser-service-rework-plan.md` (Phase D).

## Diagnosis in one paragraph

Under prod-scale pressure (3 GB cap, ~90% baseline, 2 vCPU → a 6-thread default
executor shared by every endpoint, headed persistent Chromes) an ephemeral
browser launch fails with `OSError: [Errno 11]`. `/navigate`'s catch-all
(`server.py:1437-1444`) converts that to a literal HTTP 502. The failed launch
**orphaned its browser tree** (`_launch_page` partial-start hole, probe.py
:456-470/:484-488 — W1), deepening the pressure → next launch fails → doom
loop. **[v2, C1] The threshold is self-inflicted: `NAVIGATE_MAX_CONCURRENT=3`
(server.py:56, not overridden in compose) × ~0.5 GB/3-concurrent + 1.3-2.2 GB
idle lands the container exactly at the fork-EAGAIN edge against the 3 GB cap.**
During windows, `/health` stays green because (a) it ORs the two persistent
Chromes' CDP (`server.py:635`), (b) it never exercises the ephemeral path that
is actually failing, (c) its own probe queues on the same saturated executor
(`server.py:625`), and (d) nothing consumes a bad status anyway. `/scrape`
(server.py:991) has no admission control and holds executor threads for
minutes. The 30-min cleanup loop (`server.py:260`, :354-359, :396) runs
blocking subprocess/rmtree/proc-BFS work on the event loop. uvicorn is PID 1 on
Railway (Dockerfile:72 `exec uvicorn`, no init) so orphaned grandchildren are
never reaped. And after the fact, prod 502 bursts are un-correlatable:
successful navigations are never logged and uvicorn access logs are disabled
entirely (Dockerfile:72 `--no-access-log`).

---

## Tier 1 — Code

### W1 — Launch-failure orphan guard (the doom-loop link)

**Files:** `browser_service/probe.py` (`_launch_page` :429-488),
`browser_service/server.py` (`_run_navigate_sync` :1167-1303).

The real hole is inside `_launch_page`, not just around it. Verified sequence
(playwright path :484-488; cloak path :456-470 mirrors it):

```python
pw = sync_playwright().start()        # node driver process spawned
browser = pw.chromium.launch(**launch_kwargs)   # chrome tree spawned
page = browser.new_page()             # can raise
page.set_default_timeout(...)         # can raise
return _PageContext(...)              # only HERE does the caller get a handle
```

If `launch` / `new_page` / `set_default_timeout` raise, `pw` and `browser` leak
— the driver + chrome tree. The caller's `finally` (server.py:1294-1299) closes
`ctx`, still `None`. Prod fails launches constantly; local never did — which is
exactly why the local repro could not see the leak.

**Change (v2):**
- Inner guard in both paths of `_launch_page`: try/except that **hard-kills**
  the partial resources — record the driver PID and
  `os.killpg(os.getpgid(pid), SIGKILL)` (pattern proven at server.py:107-116
  and scraper_runner.py:247-270) — then re-raise. **[v2, C1-finding-4] No
  graceful `browser.close()`/`pw.stop()` on this path**: under the very
  pressure this targets (EAGAIN, 90% memory) the node driver may be
  unresponsive, and a blocking close runs on a NAVIGATE_EXECUTOR slot — three
  such hangs wedge all 3 threads and later navigations return 408 instead of
  429. The tree is being discarded; SIGKILL it. Graceful close stays on the
  success path only.
- **[v2, C1-finding-3 / C2-finding-8] The v1 "belt" is DELETED.** Taking a
  pgrep-diff `after` snapshot in the except branch is process-global: under
  concurrency it captures *sibling* navigations' live PIDs, and the `finally`'s
  `difference_update` (server.py:1303) then un-protects those live browsers —
  re-creating the exact D4 bug class. The inner guard already closes the leak;
  the orphan killer reaps stragglers on its normal cycle under W3's fixed gate.
  Counter + WARN log of post-failure leaked PIDs only.
- Known residual (log explicitly, don't pretend): `sync_playwright().start()`
  itself raising (probe.py:484) leaves `pw` unbound — no guard can stop that
  driver; the orphan killer reaps it.

**Config/env:** none. **Risk:** low. **Effort:** S. **Ships independently:** yes.
**Tests:** new `tests/test_browser_resilience.py`. **[v2, C2-finding-12]** The
django/celery test image installs neither playwright nor fastapi (root
`Dockerfile:10`, requirements only) — seed `sys.modules["playwright.sync_api"]`
with a fake whose `sync_playwright().chromium.launch` raises (probe.py imports
it *inside* `_launch_page` at :477, so the fake is picked up cleanly); assert
SIGKILL issued + no context escape. Second case: `new_page` raises → kill path
runs.

### W2 — Process-group hygiene + PID 1

**Files:** `browser_service/browser_pool.py` (:186-190 Xvfb, :241-246 MCP
Chrome, :302-307 Scraper Chrome, `_do_restart` :124-138),
`browser_service/Dockerfile` (:65-73, apt RUN :3-17).

- Pool `Popen` calls get `start_new_session=True`; `_do_restart` escalates
  `terminate()` → `os.killpg(pgid, SIGTERM)` → `os.killpg(pgid, SIGKILL)`
  (pattern proven in scraper_runner.py:247-270). (The MCP node process already
  does this — server.py:141-146 + :107-116. The gap is the two persistent
  Chromes + Xvfb, verified :124-138.)
- **[v2, C2-finding-14]** `tini` joins the **existing first apt RUN block**
  (Dockerfile:3-17 — the only apt layer, which ends with
  `rm -rf /var/lib/apt/lists/*`; a later bare `apt-get install` fails). CMD
  becomes `sh -c` preamble (SingletonLock rm, `ulimit -c 0` — inherited by
  tini and children) ending `exec tini -- uvicorn ...`: **tini is PID 1**,
  reaps reparented orphans, forwards signals. Compose keeps `init: true`
  (:291) — init-in-init is harmless.
- **[v2, C3-finding-9] Honest claim scope:** tini reaps only processes
  *reparented to PID 1* (killed launcher's children). Chrome trees still
  parented to uvicorn are uvicorn's to reap opportunistically. **W1 is the
  causal fix for the launch-failure leak; W2 is hygiene + W3's precondition.**
  Commit 1 must not read as "fixes the doom loop" on W2's back.
- Do NOT extend killpg to the orphan killer's per-PID loop (server.py:373):
  ephemeral chromes share uvicorn's group; `killpg` there would suicide the
  service. Per-PID stays; D4 contract unchanged.

**Risk:** M. **Effort:** S/M. **Ships independently:** yes (image rebuild —
bundle with the Tier-2 deploy). **Tests:** source-contract asserts (`tini` in
the apt RUN, `exec tini -- uvicorn` in CMD, `start_new_session` present, killpg
escalation in `_do_restart`). **[v2]** No compose `ps` runtime check — under
`init: true` docker-init is PID 1 and tini a child, so that check would
red-flag a correct config. Railway-side signal instead: correct
`uptime_seconds` (server.py:653) + flat orphan counts, plus the Tier-2 Start
Command check below.

### W3 — Counter-leak gate: protect by liveness, not by counter

**Files:** `browser_service/server.py` (`_kill_orphan_chrome` :334-379;
`NAVIGATE_ACTIVE_PIDS` :63; call sites :1209, :1303).

The `/navigate` gate (`_navigate_in_flight > 0`, :343) is decremented in the
endpoint's `finally` (:1446) which runs when `asyncio.wait_for` times out —
while the executor thread and its browser are still running (observed live:
92.7s response against a 75s deadline). Next cleanup cycle the gate is open and
still-live ephemeral chromes get SIGKILLed. Do NOT re-derive the orphan killer
(docs/prod-jobs-255-debug-plan.md D4, fixed and test-locked by
`tests/test_f1_orphan_killer.py`) — only harden the gate:

- Gate on `NAVIGATE_ACTIVE_PIDS or _navigate_in_flight`, with entries
  timestamped (`dict[pid, monotonic]`); a PID older than the 180s navigate
  ceiling + 120s grace is fair game (same deadline-failsafe shape as
  `SCRAPE_IN_FLIGHT` :76-86).
- **[v2, C1-finding-5] Prune dead PIDs by `/proc/<pid>/stat` state — NOT
  `os.kill(pid, 0)`**, which **succeeds for zombies**: with uvicorn as PID 1
  (pre-W2) every SIGKILLed chrome whose parent died stays a zombie holding a
  valid PID → the prune never fires → the gate stays pinned until the
  timestamp failsafe. State `Z` → prune.
- **[v2, C2-finding-9] Ship the API change atomically:** `NAVIGATE_ACTIVE_PIDS`
  is currently `set[int]` (:63) mutated at :1209 (`.update`) and :1303
  (`.difference_update`) — inside `_run_navigate_sync`, which W1 edits in the
  same commit. A `dict.update(set)` inserts keys with value `None` → `TypeError`
  in the kill path. Add `_track_navigate_pids(pids)` / `_untrack_navigate_pids(pids)`
  helpers so the API is type-agnostic; both call sites adopt them.
- **[v2] Dependency note:** W3's liveness check is only fully meaningful once
  W2's tini is in the image (zombies stop accumulating) — same commit by design.

**Risk:** low. **Effort:** S. **Tests:** extend `test_f1_orphan_killer.py` —
**[v2]** the harness (:52-56) must also grab `"_kill_orphan_chrome"` and add
`NAVIGATIVE_ACTIVE_PIDS`-equivalent namespace stubs (`NAVIGATE_ACTIVE_PIDS`,
`_navigate_in_flight`, `PERSISTENT_CHROME_PIDS`, `subprocess`) to the exec
namespace (:60-68), else the extension `NameError`s. Cases: live PID → skip;
zombie-state PID → pruned, kill proceeds; expired timestamp → kill proceeds.

### W4 — Admission control (both endpoints) + executor separation

**Files:** `browser_service/server.py` (:31 area constants; `/scrape` :991-1102;
`/navigate` :1374-1393; all 9 `run_in_executor` sites; `/health` :625).

**Executors — [v2, C1-finding-8] enumerate ALL dispatch sites.** v1 named 5 of
the 9 `run_in_executor(None, ...)` sites (:195, :226, :625, :670, :688, :782,
:964, :1054, :1378) — the unnamed ones keep using the implicit default pool,
which asyncio recreates at `min(32, cpu+4)` = 6 threads: the pool we claim to
eliminate survives alongside the new ones and total thread count goes *up*.
All 9 sites are assigned:

- `NAVIGATE_EXECUTOR` — `NAVIGATE_EXECUTOR_THREADS` (default 3; matches
  `NAVIGATE_MAX_CONCURRENT` :56): navigate dispatch (:1378).
- `SCRAPE_EXECUTOR` — `SCRAPE_MAX_CONCURRENT` (default 2): /scrape (:1054).
- `MISC_EXECUTOR` — **default 4 [v2, C2-finding-7]**: `/probe` (:688) can hold
  a thread `timeout+60` = up to 360s, `/render` (:964) likewise, TWO akamai
  probes run concurrently (`AKAMAI_SEMAPHORE=2` :32), `/restart-cdp` (:670),
  probe-single (:782), cleanup helper. 4 covers the realistic concurrent
  holders.
- **Restart gets a dedicated 1-thread executor [v2, C1-finding-8]** —
  `restart_chrome` (:226) can hold a slot ~45-90s (`STARTUP_TIMEOUT=45`,
  browser_pool.py:14) while holding `_restart_lock`; it must never contend
  with health or cleanup.
- **Invariant (in-code comment):** *no endpoint on pool X may block on an
  endpoint served by pool X* — this also covers the self-POST in
  scraper_runner.py:96-107, which must stay off the SCRAPE pool or it
  deadlocks.
- Liveness loop + auto-restart (:195, :226) move to the restart/misc pools as
  appropriate — no `run_in_executor(None, ...)` remains anywhere.

**`/scrape` admission:** `SCRAPE_MAX_CONCURRENT` (default 2) +
**`SCRAPE_MAX_QUEUE` (default 0 — admit-or-429)**
**[v2, C2-finding-4]**: a queue under the fixed HTTP deadline is a lie —
`/scrape`'s deadline is `timeout + 120` measured **from request arrival**
(server.py:1066), so a queued caller waits against a budget that started
before it had a slot → guaranteed 504 without ever starting. Over the limit →
**429** with `retry_after` **in both the JSON body AND a `Retry-After` header**
**[v2, C2-finding-4]** (the generated template already reads the header,
templates/http_navigation_scraper.py:271 — emitting both fixes the navigation
path independently of W8's body-read fix). **[v2, C1-finding-9/C2-finding-4]
`retry_after` is DERIVED, not the hardcoded literal 5** (server.py:1367 —
today's constant manufactures a 5s-interval retry storm into a saturated
container): derive from the oldest in-flight entry's remaining time, clamp
15-60s; for /navigate `5 + 5*nav_queued`, cap 60.

**[v2, C1-finding-7 — highest-leverage addition] `/navigate` pre-launch memory
gate.** Everything else classifies EAGAIN *after* it happens; this refuses
*before* the fork. In the `/navigate` handler, before dispatch: read the cgroup
ratio W6's gauge code already computes
(`/sys/fs/cgroup/memory.current`/`memory.max`); at ≥ `NAVIGATE_MEMORY_GATE`
(default 0.85) return 429 with derived `retry_after` and
`error_class: "memory_pressure"`. Falls open (admit) when the cgroup files are
unreadable (non-Linux/dev). This converts "launch fails at 90% memory" into
"caller told to back off at 85%" — the EAGAIN never happens.

**Risk:** M — biggest behavioral change. **Must land after W8** (see
Sequencing). **Effort:** M. **Tests:** unit — pool assignment map (no default
pool remains), memory-gate threshold + falls-open, /scrape admit-or-429 with
deadline math, derived retry_after clamps; source-contract — 429 carries both
body and header.

### W5 — Get the blocking work off the event loop

**Files:** `browser_service/server.py` (`_periodic_cleanup` :175-181,
`_cleanup_chrome_artifacts` :260-280 (async), `_collect_persistent_pids`
:298-331 BFS, `_kill_orphan_chrome` pgrep :354-359, `_clean_chrome_profile_cache`
:382-400).

- **[v2, C2/C1 restructuring note]** `_cleanup_chrome_artifacts` is
  `async def` coordinating a `threading.Lock` (:265, :273), so it cannot be
  passed to `run_in_executor` directly. Extract the kill cycle + rmtree into a
  plain sync function that runs on MISC_EXECUTOR; only the lock
  acquire/release stays on the loop. `_clean_chrome_profile_cache` also runs
  on the skip path (:267) and moves too.
- Bound the rmtree: cache dirs only (already true :384-390) plus a per-dir
  age/depth guard and a wall-clock budget (~20s) after which it yields to the
  next cycle.
- `CLEANUP_INTERVAL` becomes env `CLEANUP_INTERVAL_S` (default 1800, unchanged).

**Risk:** S. **Effort:** S. **Ships independently:** yes. **Tests:**
source-contract (no `subprocess.run`/`rmtree` reachable from the async cleanup
coroutine without an executor hop); unit for the budget guard.

### W6 — `/health` overhaul: AND-not-OR, lazy-aware, ephemeral truth, gauges

**Files:** `browser_service/server.py` (`/health` :622-656; liveness loop
:184-257), `browser_service/browser_pool.py` (`check_cdp_liveness` :84-108,
`health()` :61-82), `webapp/scraper/views.py` (`_check_browser_service`
:2062-2105).

1. `cdp_ok = mcp_cdp_alive AND (scraper_cdp_alive OR scraper_not_required())`
   **[v2 — the single most cross-confirmed finding: C1-blk-1, C2-blk-1,
   C3-blk-2]** where `scraper_not_required()` is true while
   `SCRAPER_CHROME_LAZY=1` and `ensure_scraper_chrome()` has not yet run.
   Without this, lazy-by-default + AND = **503 from boot** → compose
   healthcheck `curl -f` (docker-compose.yml:331-334) fails → `depends_on:
   condition: service_healthy` (:70-71 django, :125-126 celery-worker) blocks
   dependents from starting **in the very environment the 111-test webapp
   suite runs in**, and the Railway deploy gate (docs/railway-migration.md:175)
   can never go green.
2. `browser_pool.health()` emits `scraper_chrome_state: "lazy_idle" | "up" |
   "down"` **[v2]** so consumers distinguish deliberately-unstarted from dead.
3. **Ephemeral-path truth** — rolling counters (W7 supplies them):
   `navigate_recent = {ok, fail, throttled, crash}`. **[v2, C1-finding-6] The
   window is TIME-bounded (last 300s, min 3 samples), not count-bounded**: 20
   *outcomes* spans 10-100 minutes at prod's real 30-180s/page cadence — up to
   ~15+ min of total failure before v1 would say anything, then a poisoned
   window flaps 503 for tens of minutes after pressure clears. Empty window →
   explicit `no_data` state that falls through to the persistent-AND (not
   "ok"). `throttled` is **excluded** from fail_rate (429 is the system
   working). Degraded (the EXISTING band, server.py:636/:655 — **[v2,
   C1-finding-11] new triggers, not a new status value**) when fail_rate ≥ 0.5
   or launch-failure-class errors > 3 in-window.
4. `/navigate`'s catch-all (:1437-1444) classifies resource-pressure strings
   (`Resource temporarily unavailable`, `[Errno 11]`, `Cannot allocate memory`)
   as launch-failure → `error_class: "resource"` (keeps 502; feeds counters).
5. **[v2, C1-blk-2 / C2-blk-7 / C3-finding-8] `/health` dispatches NOTHING.**
   The liveness loop already computes `check_cdp_liveness` every 15s
   (:195-197) — cache that dict in a module global and read it synchronously.
   Gauges (cgroup memory via v2 files + `/proc/meminfo` fallback, fd count via
   `/proc/self/fd`, chrome process count via one 10s-cached pgrep on the
   dedicated restart executor, per-pool occupancy, `NAVIGATE_ACTIVE_PIDS`
   size) are µs-scale inline reads. Hard ~2s internal deadline: any gauge that
   can't be computed in time degrades to `null` — **/health is always fast and
   never fails because its own instrumentation failed** (compose timeout 10s,
   Django timeout 5s). `_cloak_info` (:602-618, imports cloakbrowser + calls
   `binary_info()` on the loop) moves off the loop or drops out of /health.
6. Response stays a superset of today's keys — additive fields only (Django
   consumer uses `data.get(...)`, verified :2068-2105). **[v2]** The status
   code change (stricter AND) is deliberate and lazy-aware per (1).
7. `webapp/scraper/views.py::_check_browser_service` (already accepts 503 as
   degraded) forwards new `components`: `navigate` (recent counters) +
   `memory` gauge + `scraper_chrome_state`, so the Django /health/ dashboard
   shows *why* browser-service is degraded.

**Consumers and action [v2, C3-finding-6]:** today NOTHING polls browser
/health externally (django /health/ is `@login_required`; `/api/health/raw` is
a Django-liveness stub that never proxies browser-service; compose healthcheck
is a no-op under `restart: unless-stopped`). So: (a) Django components as in
(7); (b) a runbook row at docs/railway-migration.md:382 — "browser /health 503
+ `error_class: resource` + `memory.current ≈ memory.max` → do NOT redeploy;
wait out the window, then check /jobs/"; (c) Railway healthcheck (Tier 2) is
the only automated consumer and gates deploys only; (d) explicit NOT-doing:
auto-restart on ephemeral fail_rate is rejected — precedent server.py:35-42,
the manufactured ~30-min restart storm (81 "CDP liveness DOWN" events, zero
real crashes in 111 jobs).

**Risk:** S/M. **Effort:** M. **Tests:** unit — lazy-aware AND matrix,
time-window decay + no_data, threshold triggers, gauge fallbacks; contract —
Django consumer surfaces new keys; source-contract — no `run_in_executor` in
the /health path.

### W7 — Observability: navigations logged, ring survivable

**Files:** `browser_service/server.py` (`/navigate` handler :1374-1446;
`_periodic_cdp_liveness` :184-257), `browser_service/Dockerfile` (:72).

- **[v2, C3-finding-7 — decision, not hedge]** uvicorn access logs **STAY
  OFF** (Dockerfile:72 unchanged; `%(asctime)s` at server.py:26-29 is the root
  formatter — `uvicorn.access` has its own and would need a log-config
  override to timestamp). Correlation comes solely from the new per-outcome
  INFO line: status class, elapsed_ms, url host (never full querystring),
  error_class, attempt. Plain text is sufficient — the operator tails the
  Railway log UI; there is no log sink, so a JSON formatter adds nothing.
- Liveness chatter: **[v2, C1-finding-12]** interval is 15s (not 2s) and the
  DOWN warning fires ≤3×/incident — still downgrade to fire on state *change*
  only (recovered/escalated), and **[v2, C3-finding-1] never emit "scraper
  DOWN" for a deliberately-unstarted (lazy) Chrome**.
- Counters from W6 double as the post-hoc forensics surface.

**Risk:** S. **Effort:** S. **Tests:** source-contract (INFO log on success
and failure paths; no liveness warning while lazy).

### W8 — Client classification: 429 is backpressure, not breakage

**Files:** `templates/http_navigation_scraper.py` (`_navigate` :226-285;
`_STOP_REASON_PRIORITY` :419-431; discovery call sites :572, :608, :668, :674,
:691, :704), `webapp/agents/nodes/route_after_testing.py`, and the shared
helper below.

- `_navigate` returns a terminal dict on exhaustion —
  `{"success": False, "throttled": True, "status": 429}` — instead of bare
  `None` (the cleaner of the two v1 options). Discovery maps 429-exhaustion to
  NEW `stop_reason="navigate_throttled"`, priority **3** (INCONCLUSIVE, same
  band as `max_pages_hit`) — unproven coverage, NOT a strategy verdict.
  `navigate_error` stays 5 for 502/503/timeouts/blocks. Retry-After is read
  from **header first, then body** (server emits both post-W4; today only the
  body exists, template reads only the header — both wrong, now both right).
- **[v2, C1-finding-10 / C2-finding-6 — the route guard moves AND gains a
  branch]** v1 targeted route_after_testing.py:146-157 (the job-311
  transient-render block) — the wrong place: `:139-140`
  (`items == 0 and is_http_like and not is_traceback → ("strategy", ...)`)
  fires BEFORE the coverage gate is consulted, and a throttled run completes
  cleanly (no traceback) → today's code would strategy-switch on a throttle.
  Amendment: explicit branch immediately after the `is_selector_crash` branch
  (before :137): `report["discovery_coverage"]["stop_reason"] ==
  "navigate_throttled"` → `("scraper", "navigate throttled (browser-service
  backpressure) — re-test, no strategy switch")`. Note
  `_COVERAGE_FAIL_STOP_REASONS = {"navigate_error", "dedup_flat"}` (:76)
  already excludes the new value — and merely omitting it is NOT enough:
  `_discovery_coverage_failure` returning None falls through to
  `("refine", "0 items, low quality")` (:172) → code_writer tweaking fields on
  a zero-item run. The explicit early branch prevents all three wrong verdicts
  (strategy / refine / coverage-FAIL).
- **Shared helper** `webapp/agents/tools/browser_http.py::post_scrape_with_retry()`:
  retry on 429/502/503/504 (**explicitly NOT 404** — **[v2, C2-finding-11]**
  shell_tools.py:329 / run_execution.py:1004 treat 404 as "source invalid", a
  distinct signal), honoring retry_after (header → body), **[v2]** with a
  `total_budget_s` (default ≈ `timeout + 240`) that short-circuits remaining
  attempts — attempt-counts alone allow ~33 minutes inside one tool call
  (3 × 660s + backoff), long enough to trip the celery/LLM layers. Keep the
  transient/fatal classification (job-311 F-C spirit).
- **Call sites — [v2, C2-finding-5 / C2-finding-10] the v1 list was wrong in
  both directions. SIX real sites, minus one exclusion:**
  1. `webapp/agents/tools/shell_tools.py:330` (bare raise_for_status) → adopt.
  2. `webapp/agents/nodes/run_execution.py:~1006` (same) → adopt.
  3. `webapp/agents/nodes/run_execution.py:650-658` (**v1 missed it**): the
     multisource category loop fires up to 5 sequential /scrape calls and does
     `cat_result = resp.json()` with NO status check at all — a 429 body
     silently becomes `output_content = ""`, category skipped, merged output
     under-counts → adopt.
  4. `webapp/agents/nodes/field_confirmation.py:~500` (**worse than v1
     stated**: no status check anywhere in the file, `resp.json()` on any
     body) → adopt.
  5. `webapp/scraper/views.py:1644-1648` (**v1 missed it**): the Django
     site re-run button POSTs /scrape with `timeout: 3600`, httpx
     `timeout=3660`, broad `except Exception` → `product_count = 0` — a 429
     renders as a *successful-looking re-run with zero products*, and a 3600s
     call occupies one of two SCRAPE slots for an hour while the UI request
     hangs on a gunicorn worker → adopt AND cap its /scrape timeout (a 3600s
     synchronous HTTP call is its own hazard W4 makes more likely).
  6. **`webapp/agents/graph.py:3923` is EXCLUDED [v2]** — v1 called it "zero
     5xx tolerance (verified)"; that is FALSE (the plan contradicted itself
     two lines later). The raise sits inside `try/except Exception → (False,
     None)` (:3926-3933): fail-fast-inconclusive, cheap, and load-bearing in
     code_tester's report path (which already documents repeated 900s
     timeouts at :4140). Adopting the helper would turn "inconclusive,
     instant" into "inconclusive, ~13 minutes later". Leave as-is.

**Risk:** M — touches the generated-code template (future drafts only) and
webapp call sites (test-covered). **Effort:** M. **Ships BEFORE W4** (see
Sequencing). **Tests:** template source-contract (`navigate_throttled` in
priority map, header→body retry_after, terminal dict); unit — helper
(httpx mock: 429→retry→200; 502×budget→fatal; 404 never retried; budget
short-circuit; header-vs-body precedence); route_after_testing — throttled
branch returns re-test, never strategy/refine.

### W9 — Footprint: lazy Scraper Chrome as a first-class state (+ honest headless option)

**Files:** `browser_service/browser_pool.py` (`startup` :33-59, guards
:203-205/:270-272, `check_cdp_liveness` :84-108, `health()` :61-82),
`browser_service/server.py` (`_periodic_cdp_liveness` :184-257),
`browser_service/Dockerfile`, `docs/railway-migration.md` (:210, :382).

- **[v2 — the most cross-confirmed blocker: C1-blk-1, C2-blk-2, C3-blk-1]**
  v1's lazy start is silently defeated ~45-90s after every boot:
  `check_cdp_liveness` probes BOTH ports unconditionally (browser_pool.py
  :100-105); after `CDP_MAX_CONSECUTIVE_FAILURES=3` the liveness loop calls
  `restart_chrome("scraper")` (server.py:226), whose `_do_restart`
  (browser_pool.py:124-138) launches the never-started Chrome — the "saved"
  250-400 MB bought back inside a minute, launched from inside a blocking
  `_restart_lock` hold. AND the liveness loop is a consumer v1 never listed.
  Fix — lazy is a first-class state via one predicate
  `scraper_chrome_required()` (False while `SCRAPER_CHROME_LAZY=1` and never
  ensured):
  - `check_cdp_liveness` skips the scraper leg while not required (returns
    `scraper_cdp_alive: None` / not-applicable);
  - the liveness loop never auto-restarts a never-started Chrome;
  - `health()` reports `scraper_chrome_state: "lazy_idle"`;
  - `ensure_scraper_chrome()` (new, lock-guarded, idempotent) called at the
    top of `/scrape` and from `_do_restart`;
  - **[v2, C1-finding-13]** handle `_scraper_chrome_proc is None` BEFORE
    `.poll()` (`None.poll()` raises AttributeError, and None is precisely the
    lazy initial state);
  - W7's chatter-downgrade and W6's AND both consume the same predicate.
  - Test: lazy boot + 4 liveness ticks → `restart_chrome("scraper")` never
    called, no scraper Chrome PID exists.
- **Headless option — real bug found:** the "Railway mode" log line (:39) is
  aspirational; the guards (:203-205/:270-272) bail when Xvfb was skipped, so
  clearing DISPLAY today kills the whole pool. Fix the guards (Xvfb required
  only when headed); **[v2, C2-finding-13] scope the flag to the MCP Chrome
  ONLY** — Phase D deletes the Scraper Chrome (rework-plan :463-465), so
  headless+UA work there is sunk cost; fix its guards as part of lazy anyway.
  `CHROME_HEADLESS=1` (default 0) skips Xvfb + appends `--headless=new` + the
  UA override (MCP Chrome sets an explicit UA :237; without the override
  headless advertises `HeadlessChrome`). **[v2, C1-finding-13]** strip the
  unconditional `--display={DISPLAY}` (:212) and the empty DISPLAY env on the
  headless path. **[v2]** Update docs/railway-migration.md:210 (the DISPLAY=""
  trap whose documented symptom is the /health-503 + "Skipping MCP Chrome"
  pair) and the :382 row in the same commit — fixing the guards turns that
  loud misconfiguration into a quietly-booting service; the runbook row must
  not go stale.
- **[v2, C3-finding-4] Default stays `SCRAPER_CHROME_LAZY=1`** — acceptable
  ONLY because the F1/F2-class guards above land in the same commit and are
  test-locked. Documented alternative: `SCRAPER_CHROME_LAZY=0` via Railway env
  restores eager start without a rebuild.
- **[v2, C3-finding-4]** Update the deploy-checkpoint doc line
  (docs/railway-migration.md:216 `Browser pool ready: Xvfb/MCP/Scraper`) for
  the lazy log shape, or the next operator reads the new boot log as a failure.

**Risk:** M (browser_pool guards are load-bearing for startup). **Effort:** M.
**Tests:** unit — lazy ensure (once, locked, idempotent, None-safe),
liveness-skips-lazy, headless guard matrix (DISPLAY × CHROME_HEADLESS →
Xvfb?/flag/--display-strip/UA).

---

## Sequencing (v2 — order corrected; one push, one checked deploy window)

**[v2, C2-blk-3] v1's "W4 and W8 co-ship in one push" was not a guarantee:**
W8's changes live in the django/celery images (root Dockerfile) and W4's in
the browser_service image — different Railway services, independently
redeployable, and Railway CAN redeploy browser_service alone (Tier-2's memory
change does exactly that). Reorder so the tolerant clients always exist before
the server starts throttling:

| Order | Commit | Contents | Constraint |
|---|---|---|---|
| 1 | `fix(browser-service): launch orphan guard + process groups + tini + gate hardening` | W1, W2, W3 | kill/reap machinery test-locked together (F1); W2 is W3's precondition |
| 2 | `fix(scrapers+webapp): 429 throttled-classification + tester-path retry tolerance` | W8 | must deploy (django+celery images) BEFORE commit 3's image |
| 3 | `fix(browser-service): admission control + executor separation + off-loop cleanup` | W4, W5 | safe only after 2 is live |
| 4 | `fix(browser-service): truthful /health + navigation observability` | W6, W7 | |
| 5 | `feat(browser-service): lazy scraper chrome + headless option` | W9 | image-level; same deploy |

**Deploy rule (explicit):** push → snapshot-clone PR → merge once when /jobs/
is quiet → verify django + celery-worker images redeployed and healthy, THEN
confirm the browser_service image redeployed (same window). Escape hatch: if
ordering is ever violated, `SCRAPE_MAX_CONCURRENT=4` + `SCRAPE_MAX_QUEUE=0`
via Railway env degrades W4 to today's behavior without a rebuild.

**Rollback [v2, C3-finding-3]:** rollback = `git revert` the merge commit via
a new snapshot-clone PR → Railway redeploys. The 5 commits are file-disjoint
enough to revert individually, with one direction rule: **never revert commit
2 (W8) while commit 3 (W4) is live** — reintroduces intolerant clients in
front of a throttling server. Reverting W4 while W8 stays live is safe
(tolerant clients just never see a 429). W6's stricter AND is the only
commit whose revert changes consumer-visible status semantics — revert it
together with W9 if health flips unexpectedly.

Suites to keep green: existing `tests/test_f1_orphan_killer.py`,
`test_f2_cdp_retry.py`, `test_job312_browser_timeout_floor.py` + new
`tests/test_browser_resilience.py` + webapp suite for W8's call-site changes.

## Tier 2 — Railway ops (user actions; documented, not automated)

**Forensics addendum rule:** the runtime-forensics agent's findings fold in
here when they land. If they CONTRADICT the 3 GB-pressure diagnosis,
W-priorities revisit before implementation. If they CONFIRM memory pressure
AND the undocumented ~09:34→10:22 Chrome swap, attribute the swap to the
liveness auto-restart (server.py:227-252) and treat W9's lazy/liveness guard
as load-bearing, not optional. **[v2, C3-finding-10]**

1. **Memory 3→4 GB** on browser-service — **[v2, C3-finding-5] it is itself a
   redeploy (kills in-flight jobs): ride the SAME /jobs/-quiet window as the
   code deploy — one casualty window, not two.** Arithmetic (checkable once
   forensics lands): audited prod peak 2.69 GB against 3 GB (90%); minus W9's
   ~250-400 MB lazy saving → ~2.3-2.45 GB projected peak on 3 GB — under the
   cap but with the 08-28/29 burst class (97× EAGAIN) implying the margin is
   too thin at 3 concurrent; **[v2, C1-finding-7]** pair with
   `NAVIGATE_MAX_CONCURRENT=2` (below) to re-open real headroom. Framing:
   docs/railway-migration.md:214 already specifies "**4 GB RAM / 2+ vCPU**
   minimum" — prod at 3 GB is below documented build spec. This is
   **conformance, not a hardware raise**; the directive stands.
2. **`NAVIGATE_MAX_CONCURRENT=2` on Railway** **[v2, C1-finding-7]** — pure
   env, no code, rides the same window. The 3-slot default is the number that
   produces the audited peak; the pre-launch memory gate (W4) remains the
   code-side backstop for spikes.
3. **Railway healthcheck** (docs/railway-migration.md:213, never applied):
   point at `/health`, **start period ≥ 90s** (docs:213 itself notes Chrome+MCP
   take 30-60s to boot). Honest caveat: gates DEPLOYS only — does not heal a
   wedged container; after W6 it will correctly flap during pressure windows
   (that flap is information, not noise).
4. **PROXY_* copy to celery-worker + celery-events** — the job-35 fix.
   **[v2, C3-finding-11] Explicitly EXCLUDED from this deploy window:**
   different root cause, different blast radius; batching destroys
   attribution if this deploy regresses. Land and verify this plan first, then
   PROXY_* in its own /jobs/-quiet window (check /jobs/ first regardless —
   casualties already: jobs 24, 43).
5. **Pre-deploy read-only check [v2, C3-finding-9]:** confirm the
   browser-service Start Command field is EMPTY in the Railway dashboard —
   if set, the Dockerfile CMD (and tini) are silently ignored and W2 is a
   no-op; replicate the tini wrap into the override instead.
6. **Later, evidence-gated:** `CHROME_HEADLESS=1` on Railway once W6 gauges
   quantify the headed overhead (MCP Chrome only; UA-override mitigated).

## Tier 3 — Explicitly out of scope (existing stalled plan; referenced, not re-derived)

`docs/browser-service-rework-plan.md` Phases B–D stand as written:
Phase B (regenerate the 6 legacy /scrape scrapers + lw.com profiles) →
Phase C (`SCRAPER_EXECUTION_MODE=force_http`, settings.py:274 still "auto") →
Phase D (delete `scraper_runner.py`, `/scrape`, Scraper Chrome, CDP proxy
9223→19223, cloak_stealth_patch). Gate condition unchanged: the 180s
`/navigate` per-call ceiling (server.py:488) vs the 600s browser floor the
tester needs (test_job312) must be resolved BEFORE Phase C forces heavy
discovery through /navigate. What THIS plan contributes to that future:
W6's counters measure /scrape usage empirically (Phase B's exit evidence),
and W4's admission control makes the legacy path predictable while it dies.

## NOT doing, and why

- **Not re-deriving the orphan killer** — D4 is fixed and test-locked
  (jobs 325/328/334); only its safety gate hardens (W3).
- **Not making /navigate's catch-all return 200s or swallowing errors** —
  the 502 is honest; what changes is that it's now counted, classified,
  health-visible, and (W4) preceded by a memory gate that refuses before the
  fork.
- **Not adding retries inside browser_service** — retry policy stays with
  callers (W8); server-side retry under memory pressure amplifies the
  pressure. **[v2, C1-finding-9]** The one server-side change in that
  direction — deriving `retry_after` from real pressure instead of the
  hardcoded 5 — *removes* an existing retry storm rather than adding one.
- **Not raising prod memory as the primary fix** — directive and evidence
  both say the workloads are light; 3→4 GB is conformance with the
  documented spec (Tier 2), and W9 shrinks the idle footprint first.
- **Not touching the ephemeral launch mode** — headless-by-design is part
  of the anti-bot story; only the MCP Chrome gets an option (W9).
- **Not deleting /scrape or the Scraper Chrome in this plan** — Phase D's
  job, gated on Phase B.
- **Not "adding a healthcheck" as the answer to 502 windows** — the
  healthcheck gates deploys; the actual fix is that /health stops lying
  (W6) and the pressure stops happening (W1/W4/W5/W9).
- **Not wiring auto-restart to the ephemeral fail_rate [v2]** — precedent
  server.py:35-42: ungated liveness auto-restart manufactured a ~30-min
  restart storm (81 DOWN events, ZERO real Chrome crashes in 111 jobs).
  Degraded health informs humans and gates deploys; it does not pull
  restart triggers.
- **Not re-enabling uvicorn access logs [v2]** — `--no-access-log` stays;
  the per-outcome INFO line is the correlation surface (W7).
- **No PID-snapshot "belt" on the launch-failure path [v2]** — a global
  pgrep-diff under concurrency un-protects sibling navigations' live PIDs
  (the D4 bug class). The inner guard + the orphan killer's normal cycle
  cover it.
