# Wave-15 Plan — Honest queue, bounded invokes, proxy identity parity

**Status:** RC2 — ALL 5 critics folded (scope, queue, async, proxy, test-plan).
Contested items adjudicated; test pins enumerated; R2 resolved.
**Theme:** Three substrates the failure record keeps tripping over — (1) the queue
is invisible and strands rows that block all auto-scheduling, (2) one LLM invoke
site is still unbounded and the async path that would make every invoke
cancellable is built-but-untested, (3) the proxy identity the probe proves is not
the proxy identity execution uses.
**Sources:** 3 scout reports, reconciled against the wave-14 tree, attacked by
5 adversarial critics. Line cites below are critic-verified against this tree.

---

## 0. Ground truth (corrected by critics)

- Wave-14 landed locally (`40f443d` → `97f3810`, UNMERGED — user merges via
  GitHub UI): `task_failure` handler (tasks.py:162), run registry + `/cancel`
  + hygiene (`4c7cf6d`), F3 TargetClosedError retry (`97f3810`).
- **langgraph is 1.2.9**, not 1.2.11 (requirements.txt:16 pins `>=1.2,<2.0`,
  floats on rebuild). python 3.12.13, langchain-core 1.5.1,
  langchain-openai 1.4.1, celery 5.6.3.
- The stale "AsyncPregelLoop NotImplementedError" premise is live in operator
  memory: **`.env:4` still carries the 2026-08-19 revert note.** The plan
  corrects it explicitly; AsyncPregelLoop no longer exists in langgraph 1.x
  and `ainvoke` is verified working in the live container.
- Migration `0036` is free in BOTH trees (local + ExtractorBuilderAi).
- Nothing in webapp/src ever writes `STATUS_PENDING` — the model default
  (models.py:150) is its only source; nothing legitimately re-PENDINGs an
  approval-parked row.

**Standing constraints honored:** only adds retry/fallback capability; generic
fixes only; tests at end of each PR; failed re-drives one at a time.

---

## PR-1 — Queue honesty + stranding recovery (django+celery)

*Scout 1 + scope critique + queue critique. Items 1+2+3 ship in the SAME
deploy (R4): item 1 removes a racy recovery path, the sweep + keystone replace
it, and the keystone is what makes the sweep's discriminator sound.*

### 1.0 NEW (keystone, queue critique): client-generated task ids persisted BEFORE publish

At all 7 dispatch sites (`views.py:281-283, :690-692, :1632-1634, :2725-2726`;
`tasks.py:1768-1770`; `api/writers.py:162-163` via on_commit;
**`management/commands/scrape.py:76` which NEVER persists the id**):
`task_id = str(uuid4())` → save `celery_task_id` → publish
`apply_async(args, task_id=task_id)`. This makes `celery_task_id=""` strictly
mean "never dispatched" — the property both the sweep's predicate (1.2) and
item 1's same-id re-entry allowance depend on. Today the field is stamped only
at worker entry (tasks.py:254-257), so "" also covers "in queue" (unbounded
window: full queue latency for scrape.py:76; permanently, if the web/beat
process dies between `.delay()` and the save — a Railway deploy). Test
coupling: `tests/test_api_create.py:91-126` patches `.delay` and asserts
`celery_task_id == "task-x"` — update to the `test_api_dispatcher.py:517-521`
shape (patch the task object, assert `apply_async` kwargs); also
`tests/test_api_create_persist.py:140,155,181`.

### 1.1 Claim-by-rowcount at task entry — predicate CORRECTED by queue critique

Site: dedup guard `tasks.py:241-245`. The drafted `filter(status=PENDING)
.update(status=RUNNING)` **breaks the same-site retry**: celery 5.6.3's
`Context.as_execution_options()` re-publishes `self.retry()` with the SAME
task id, and the sibling check (:263-278) raises the retry while the row is
RUNNING (claim already fired) → the retry re-entry claims 0 rows → return →
stranded RUNNING → watchdog kills at ~35 min mislabeled "Worker process lost".

**Required predicate** (preserves the duplicate-dispatch fix AND wave-14's
stamp-theft protection at :249-253):

```python
_task_id = getattr(self.request, "id", "") or ""
pred = Q(status=ScrapeJob.STATUS_PENDING)
if _task_id:  # eager/always_eager tests have no id
    pred |= Q(status=ScrapeJob.STATUS_RUNNING, celery_task_id=_task_id)
claimed = (ScrapeJob.objects.filter(pk=job_id).filter(pred)
           .exclude(status=ScrapeJob.STATUS_WAITING_APPROVAL)
           .update(status=ScrapeJob.STATUS_RUNNING, celery_task_id=_task_id))
if not claimed:
    return  # keep the exact log string "skipping duplicate dispatch" (pin)
```

Do NOT extend the claim to `resume_scrape_task` (:431-435): the stuck-approved
watchdog (:1878) and `_auto_approve_stale_jobs` (:1923) deliberately re-enter
the same row while WAITING_APPROVAL; resume sets RUNNING itself (:459).
Keep the `"false corpse"` comment block (:196-198 pin). Add the missing
entry-guard WAITING_APPROVAL test (today only `_on_task_process_death` covers
that seam — `test_wave14_honest_death.py:142-159`), and pin the `:346` RUNNING
write whose role changes under a claim. Item 1 does close a real seam: today a
child death between the stamp (:257) and the RUNNING save (:352) strands
PENDING forever because `_on_task_process_death` bails on `status != RUNNING`
(:182). Also note: `max_retries=None` (:277) means "decorator default" (=1),
not unlimited → `MaxRetriesExceededError` raises at :274 OUTSIDE the try, is
not in `_PROCESS_DEATH_EXCUSES` (:149-154) → **today that strands
PENDING-with-id forever, blocking all auto-scheduling** (:1715-1725 counts
PENDING as active) — a live bug this PR's honest-FAILED path should catch.

### 1.2 `redispatch_abandoned_pending` beat task — claim mechanism CORRECTED

The drafted "claim via the same rowcount pattern (status=RUNNING)" is
self-defeating: the redispatched task would enter, see RUNNING, and return —
the recovered job never runs. **Claim on the counter, leave status PENDING**
so the redispatched task's own entry claim (1.1) works:

```python
(ScrapeJob.objects
   .filter(pk=jid, status=STATUS_PENDING, celery_task_id="",
           redispatch_count__lt=CAP)
   .update(redispatch_count=F("redispatch_count") + 1))
```

Precedent: `Approval.resume_attempts` (models.py:344, tasks.py:1853-1866).
Cap 2 → honest FAILED. One row per sweep. Rides `events`
(docker-compose.yml:147-149 dedicated worker; `cleanup_stuck_jobs` precedent
settings.py:187). Order inside the sweep: sweep first, then
`_do_schedule_next_site`. **While editing `CELERY_TASK_ROUTES`
(settings.py:183-187), also route `redispatch_stuck_approved_interrupts` —
it was never routed and still runs on the 2-slot scrape pool.** Safety is
contingent on 1.0+1.1 landing in the same deploy.

### 1.3 `/health` queue component — thresholds corrected (scope critique Q4)

`LLEN celery`, `LLEN events`, `ZCARD unacked_index` (kombu maintains
`unacked` hash + `unacked_index` zset — verified), Django-side
`oldest_pending_age_s` and `oldest_running_silent_s` (**compute for the
oldest row only** — per-row silence is an N+1). Alerts:

- `pending_stranded`: **signature** `status=PENDING AND celery_task_id=""`
  AND age > `PENDING_CLAIM_MINUTES` (15 min — same constant as 1.2). NOT
  age>1h: the same-site serializer (tasks.py:269-278, `max_retries=None`)
  legitimately holds PENDING up to 11160s ≈ 3.1h and dominates the age
  gauge. (Age-only floor, if ever wanted: ≈ 6.2h.)
- `queue_backlog`: **`LLEN events > 10`** — events carries only 30s beat
  work; a backlog there means watchdog/scheduler can't run. Scrape-queue
  count alert DROPPED (campaigns routinely push ~20 through one worker).
- `oldest_running_silent_s`: gauge only; if alerted, floor **90 min**
  (`ACTIVE_SILENCE_REVOKE_MINUTES`) — silence under an ACTIVE task is not
  death (wave-14 doctrine, jobs 79/80).
- `unacked_backlog`: keep, **labelled wave-16 enablement** — inert under
  `CELERY_TASK_ACKS_LATE=False` (settings.py:147; acks at delivery → ZCARD
  ~0 during 3h jobs). Needs one Redis cache key for the 2-consecutive-polls
  state (counted).
- **Auth story required:** `health_api` is `@login_required`
  (views.py:2228) — alerts unreadable by external monitors. Scoped
  read-only token or documented cookie poller, else decorative.
- No `status`/`celery_task_id` index today — fine at current row counts.

Views-only + template; first coverage = `tests/test_health_queue.py`.

### 1.4 Per-child ceiling — RESCOPED: code half already done

`settings.py:141-143` already reads `CELERY_WORKER_MAX_MEMORY_PER_CHILD` via
`config(..., cast=int)`. Remaining: (a) Railway env var 1.6 GiB (ops);
(b) parameterize the geometry test — **actual location
`tests/test_job80_watchdog_liveness.py:177-192`** (the plan's :129-149 was
the liveness table — leave that alone). THE TRAP (test critique): the test
process runs in the **django** container (`mem_limit: 1g`,
docker-compose.yml:79), NOT celery-worker's 3g (:135) — do NOT read the test
process's own cgroup. Parse compose's `celery-worker` `mem_limit` (no
precedent helper exists — write one) or read a settings constant. Keep
:192's `>= 1 GiB` floor absolute.

### 1.5 `BROWSER_METHODS` consolidation — DROPPED to wave-16

No failure (the 3 `scraping_method` copies are byte-identical today); the
4th (`probe_tools.py:51`) is a different set in a different domain (probe
STEP names) — merging would be a bug. Wave-16: 3-way rename + rename
probe_tools' constant to `BROWSER_PROBE_STEPS`. Note: `views.py:1667` copy
is nested inside a function — the consumer moves too.

**R1 — rewritten honestly (queue critique):** a claimed-then-crashed row is
RUNNING with an id, `started_at=None` (set only at :347), zero SessionLogs →
**the sweep cannot recover it (RUNNING + id-set); only the watchdog can,
~35 min** (30-min silence :1463 + up to 300s beat), mislabeled as worker-loss
(:1612-1616) with a revoke of a task that no longer exists (:1645).
`_on_task_process_death` covers WorkerLost/TimeLimit (parent survives) and
fires for NOTHING when the whole container dies (Railway deploy / OOM).
**Fold in a free speedup:** a RUNNING row with `started_at is None` and zero
SessionLogs is provably pre-graph → fail it on first sighting (faster
fallback, not a cap — constraint (a) respected).

---

## PR-2a — Bind the last unbounded invoke (SAFE — async critic confirmed)

1. **W15-A: bound `_invoke_cleanup`** — bare `agent.invoke(...)` at
   `graph.py:5581` (exact line) against **8** dispatcher sites (not 9). Wrap
   with `_invoke_agent_with_timeout` + heartbeat. `WallClockTimeout` shape
   already handled by `_persist_agent_logs` (graph.py:6130-6140).
   Contract pins to respect: `test_failure_evidence_archive.py:96-105`
   requires the body to still contain `_archive_failure_evidence(` with
   `_promote_scraper(` BEFORE it — an in-place invoke→wrapper swap is safe;
   do not rename/reorder. Emit `[INVOKE-TIMEOUT]` (no new tag — protects the
   postmortem grep, `test_wave14_honest_death.py:230-234`).
2. **DELETE `create_agent_with_retry`** — zero callers (only re-exported,
   `nodes/__init__.py:13,36`); retry_wrapper.py is unrouted. Both critics
   concur.
   Test seam: mock the factory + call the node directly
   (`test_artifact_copy_guards.py:171-206` pattern); hang = `_Stub.invoke`
   blocking on `threading.Event().wait` (`test_iteration_economics.py:63-77`
   shape, which also pins `_async_execution_enabled` — see PR-2b).

## PR-2b — Per-phase async allowlist + harness (default OFF, env canary, then flip)

*Async critique Q1-Q4. The dispatcher is the single choke point
(`_invoke_agent_with_timeout` graph.py:1938; gate `_async_execution_enabled`
:1855 is the only `LLM_ASYNC_EXECUTION` reader); all three code_writer sites
(:4344 main, :3972 syntax-fix, :4091 CLI-contract-fix) funnel through it with
`phase="code_writer"`. `ainvoke` verified live: sync `pre_model_hook` runs on
the executor thread, agents carry no checkpointer (the outer graph's
PostgresSaver is a different compiled object — do not "fix" it).*

1. **W15-B: harness, hardened to the three real traps** — new
   `webapp/tests/test_agent_async.py`:
   (a) streaming-lane stub must pass `streaming=True` through the
   **constructor** (langchain-core's `_should_stream` requires
   `model_fields_set`; a class default silently exercises `_agenerate` and
   proves nothing — verified). `get_llm` already does this (llm.py:549-558),
   and `ClassifiedRetryChatOpenAI._astream` (llm.py:465-485) carries the same
   classified ladder + breaker as `_stream` → **R2 RESOLVED SAFE: no
   retry/fallback capability is lost; async backoffs (:306-328) become
   cancellable — strictly better.**
   (b) tool-in-flight timeout pin: **the async wall clock is NOT hard** —
   `asyncio.run` (graph.py:1902) runs `shutdown_default_executor()`, which
   JOINS sync tools/pre_model_hook on the default executor. Measured: 2s
   deadline, 6s tool → wall 6.01s (sync path: 2.00s, leaked thread). The
   comment at graph.py:1869-1870 ("abandoned … same shape as today") is
   factually wrong. **Fix in the same PR: manual loop management that skips
   `shutdown_default_executor`** (or explicitly accept + pin the overshoot).
   (c) same agent object across two `asyncio.run` loops (the syntax-fix loop
   pattern) — cross-loop httpx client reuse is unsupported
   (langchain-openai caches `async_client` on the instance); empirically
   tolerated, but a `RuntimeError: Event loop is closed` keep-alive would
   die with ZERO retries — that class is in neither retry arm
   (llm.py:317-327). **Add it to the classified transport errors** (adds
   fallback; constraint (a)).
   Django trap: keep DB access AFTER `asyncio.run` returns (async-unsafe ORM
   guard) — assert on the `_error_class` return, not the SessionLog row;
   never set `DJANGO_ALLOW_ASYNC_UNSAFE=1`.
2. **W15-C: allowlist, DEFAULT EMPTY** (resolves the RC1 contradiction where
   W15-B said OFF and W15-C said ON): `_ASYNC_PHASES = set()` default,
   `AGENT_ASYNC_PHASES` env to enable `code_writer` for the canary, flip the
   default only after a local e2e shows a full code_writer phase under async.
   The gate takes a `phase` argument now (call :1961); **update the two
   0-arity monkeypatches in the same PR** (`test_job81_tester_wall_clock_
   contract.py:134`, `test_iteration_economics.py:69` — the latter would
   otherwise silently fall into the async path and fail with
   `_error_class == "AttributeError"`).
   Rationale rewrite (async critique): code_writer is NOT "no long-held
   resource" — its toolset includes `run_scraper` (600s floor,
   shell_tools.py:35). The honest justification: the 900s abandonments it
   suffers are LLM-streaming hangs (the cancellable case), and the
   run_scraper overshoot is bounded by the (1b) fix.
   `LLM_ASYNC_EXECUTION` orphan (docker-compose.yml:113, settings.py:252,
   `.env:4` note): keep as an explicit all-phases override, delete the
   compose var in the same PR — pick one; either way log the effective
   allowlist at worker boot and correct the `.env` counter-note.
   site_analyzer/product_analyzer/code_tester extend only after the canary
   survives a real job with a playwright tool in flight.

**Park permanently:** full async worker / gevent / async service — sync
`PostgresSaver` raises NotImplementedError on every `a*` and `context.py`
(module-global dict by design, verified propagating across executor threads)
depends on prefork. Negative ROI, verified.

---

## PR-3 — Proxy identity parity (browser_service + templates + docs)

### 3.1 Probe tier recording — REAL PATH CORRECTED (proxy critique)

The plan's `probe.py:244` fix is a **no-op for real jobs**: production goes
`check_accessibility` (graph.py:1721) → `run_probe_with_captcha_check`
(probe_tools.py:286) → `/probe-single` with `"method": "cloak_none"`
**hardcoded at probe_tools.py:413**, recorded at :460. probe.py:244 is
reachable only via the agent-facing `probe_page` tool on a cold cache.
**Fix probe_tools.py:413 + :460** (pass the tier that actually detected the
block) AND decide `continue` vs `return` at probe.py:248-249 — today the
bypass `return`s on failure and, since only `direct_http` tier "none" sets
`needs_akamai_bypass` (:844), **a proxied deployment that detects Akamai
never tries datacenter/residential at all**. Also fix `/probe-single`'s
`cloak_none` rung omitting `country` (server.py:1551-1553) while other cloak
rungs pass it. No consumer breakage (all prefix/suffix-based:
constants.py:55, probe_tools.py:51-61, graph.py:3576-3582,
run_execution.py:265-272).

### 3.2 `country` in the `_navigate` payload — REINSTATED (adjudicated)

The scope critic's refutation was a line misattribution: the
`country = request.country or _detect_country(request.url)` fallback lives in
**`/probe-single`** (server.py:1476-1479), not `/navigate`. Verified directly:
`NavigateRequest.country` defaults None (:1197) and the endpoint (:2039)
passes `request.country` straight into `_run_navigate_sync` (:2135-2147) →
`_launch_page(country=None)` → `build_proxy_url(tier, country=None)` omits
the `-country-cc` suffix (src/proxy.py:186-190). **The probe proves
`residential-country-au`; execution runs pool-less residential — a different
Bright Data peer pool.** Precision from the proxy critique:
- The payload (`templates/http_navigation_scraper.py:256-263`) has NO
  `country` key and NO `src` import — item = add key + import
  (`src/geo.py:70`, same fn the probe uses; `playwright_scraper.py:547`
  already passes country — http_navigation is the odd one out).
- **Tier-gated, sequenced after 3.5:** proxy is only constructed
  `if proxy_tier != "none"` (probe.py:681-684, :713-716), and "none" is both
  the default and the unfilled-`{PROXY_TIER}` outcome — country is dead
  weight at none:
  `"country": (detect_country(SITE_URL) or None) if PROXY_TIER_EFF != "none" else None`
- `~4 LOC` optimistic (+import); E5 still gates the geo-fenced-catalog risk.

### 3.3 `ssr_div_list` broken builder — CONFIRMED, WORSE (httpx 0.28 hazard)

`:80-84` calls `ProxyConfig.from_file`/`cfg.get_proxies` — neither exists;
`except: return None` → always unproxied. Fix is **`build_proxy_url(...)` →
`httpx.Client(proxy=...)`**, NOT `get_proxy_dict` (returns a requests-style
dict; this template feeds httpx, which wants a single URL — and httpx removed
`proxies=` in 0.28 while requirements.txt:6 pins `>=0.27.0` unbounded:
**if the image has httpx ≥0.28, `httpx.Client(proxies=None)` TypeErrors on
every fetch, swallowed by the broad except → the template is dead today.**
→ E6 verifies the installed version. Also `:52` reads `PROXY_TIER` env that
nothing stages (same orphan class as 3.6).

### 3.4 api/shopify cookieless calls — KEEP, right mechanism (not a drop-in)

Confirmed: module-level cookieless `requests.get`, **no Session in either
file** — the job-58 mechanism. The inline tier ladder above both lines
(get_proxy_dict/is_banned/cooldown) is real, so the scope critic is right
that a naive swap would delete a working ladder; the proxy critic is right
that it lacks persistent-Session + curl_cffi fingerprint tier + soft-block
detection. **Mechanism: extend `src/http_fetch` first** (params passthrough +
JSON-returning sibling — `create_fetch_page` returns
`tuple[BeautifulSoup,int] | SoftBlock`, no `.json()`, ~30-40 LOC), **then**
swap (~15 per template). **Include the second cookieless site in each
template:** `scrape_product` at `api_scraper.py:302` and `shopify_scraper.py:252`
(no proxies, no ladder, runs N times in Phase 2 — the bigger reputation
burn); same class at `http_navigation_scraper.py:39-47` `_http_get`.

### 3.5 Env overrides — wire, don't document

`SCRAPER_PROXY_TIER` is read at playwright_scraper.py:63 and written by
NOTHING (the "staged via env_overrides" comment is false; the plumbing
exists — server.py:1141 → scraper_runner.py:390-391). **Wire it** via the
existing `_stealth_env` staging (~5 LOC). Correction: the DELAY override is
NEW ground (playwright_scraper.py:67 has no DELAY env read) — label it as
such, not "parity".

### 3.6 `proxy_tier` on `RenderRequest` — only WITH a caller

Genuinely ~5 server LOC (`:1163-1168` lacks it; `render_page` already accepts
+ filters it, probe.py:283/:317-322), but no caller sends a tier and a
wrongly-sent tier → `all_failed` via the tier filter + `_proxy_tier_configured`
guard (:340-345). **Keep only if a caller lands in the same PR** — the
natural one is `field_verification.py:207` (highest-value caller, sends
neither country nor tier today). Otherwise defer. All four existing
/render callers are safe either way (BaseModel ignores unknown fields).

### 3.7 Doc/skill truthing — DO FIRST (all claims verified + MORE rot found)

- **The docstring the LLM actually reads:** `probe_tools.py:603-613` still
  enumerates UC-era method names, omits cloak AND the fingerprint rungs.
  Fix before/with everything else.
- `akamai-detection/SKILL.md:65,:105-125,:129+`: `/probe-akamai` (deleted,
  T3.2 — probe_tools.py:402,:710 say so), nonexistent
  `akamai_stealth_scraper.py` template, nonexistent `AKAMAI_SEMAPHORE` —
  the only summoner of the unmanaged cookie writer. CLAUDE.md already marks
  the template dead — the two docs contradict each other.
- `site-analyzer.md:143` method enum has no `cloak_*` at all.
- `proxy-config/SKILL.md:153-159` UA-rotation advice (dead code:
  src/proxy.py:251 has only test callers; `strategy.user_agent_rotation`
  written, never read) + `:82-83,:164` false `opencode.json` claim. Soft
  spot: `playwright-mcp-proxy.sh` is an /app path not in the repo — confirm
  it sets no proxy before writing "false".
- `CLAUDE.md:83,:141,:179` "7-step" (live: 9 base + fingerprint rungs ≈15);
  same rot at README.md:184, OVERVIEW.md:118, langgraph-upgrade-plan.md:174-178.
- `railway-migration.md`: Phase 7 (:223-253) missing the 8 PROXY_* vars
  (= `_load_from_env`, src/proxy.py:57-75) AND Phase 6's (:202-210) "the
  ONLY proxy path on Railway" sentence IS the theiconic bug in prose — fix
  both halves.

---

## Evidence gates

| # | Measurement | Gates | Status |
|---|---|---|---|
| E1 | Prod stranded-signature check | PR-1.2 env default | **measured 2026-09-01 via per-job API** (the HTML-status parse is unreliable — status badges regex-span rows; 61/136 completed, 155 running, 153 FAILED 09:44→10:38Z — new RCA item, likely re-drive candidate, 157/156 pending-queued healthy behind 2 busy workers). **Key finding: prod shows `celery_task_id=None` for ALL jobs — the field is never populated on prod (stamping ships with wave-14).** So the sweep's discriminator is un-runnable on prod until wave-14 + PR-1.0 land. Decision: **ship the sweep default-OFF at wave-15 deploy; flip ON via env after N days of `/health` gauge data** (runbook in PR-1). |
| E2 | browser_service cgroup ratio histogram | wave-16 gate ratio | poll `/health gauges.memory` |
| E3 | CDP hijack in prod | wave-16 session substrate | non-cloak job → check egress IP vs Bright Data |
| E4 | Unmanaged cookie writer | PR-3.7 urgency | `ls /app/data/akamai-cookies/` after a non-cloak Akamai run |
| E5 | Four-curl sticky test ×2 (local + Railway) | PR-3.2 geo risk | region-scoped sessions |
| E6 | **httpx version in the live image** (new) | PR-3.3 severity | `httpx.__version__` in browser_service container; ≥0.28 ⇒ ssr_div_list is dead TODAY |

## Test plan (tests at END of each PR)

- **PR-1:** `tests/test_health_queue.py` (new — metrics + corrected
  thresholds incl. signature-stranded + events-backlog); 
  `tests/test_dispatch_claim.py` (new — claim wins/loses/same-id-reentry;
  **MUST patch `scraper.tasks.run_scrape_task.delay` — root `tests/` runs
  Postgres + live Redis + NON-eager (no conftest; files self-boot
  `config.settings`), so an unpatched beat task publishes a REAL broker
  message the live celery-worker consumes**); entry-guard WAITING_APPROVAL
  test (new — zero coverage today); `:346` RUNNING-write role pin; update
  `test_wave14_honest_death.py:184-198` pins by KEEPING the exact strings
  ("skipping duplicate dispatch", "false corpse"); update
  `test_api_create.py:91-126` + `test_api_create_persist.py:140,155,181` to
  the `test_api_dispatcher.py:517-521` apply_async shape; geometry test
  re-homed to compose-parse (NOT the test process's cgroup — django
  container is 1g). Keep the 3× liveness invariant untouched.
- **PR-2a:** timeout unit test via factory-mock + direct node call
  (`test_artifact_copy_guards.py:171-206` pattern); source-read test that
  `create_agent_with_retry` is gone.
- **PR-2b:** `webapp/tests/test_agent_async.py` per the three traps
  (constructor-streaming stub; tool-in-flight overshoot pin — hard wall
  after the `shutdown_default_executor` fix; same-agent-two-loops parity);
  kill-switch tests; update the two `_async_execution_enabled` 0-arity
  monkeypatches; no `# noqa: E402` boilerplate in the new webapp test file
  (RUF100 delta; E402 isn't in ruff 0.16's default select); blocking
  behavior in sync helpers (ASYNC210/230 fire by default); assert DB
  effects only after `asyncio.run` returns.
- **PR-3:** template source contracts via the `_tpl` helpers
  (`test_wave13_reliability.py:33-37`) + `index(a) < index(b)` ordering
  idiom: `{PROXY_TIER}` conditional hard-error (raise only when
  `method_that_worked` is a non-none tier; warn+none otherwise — as-drafted
  unconditional hard error was a constraint-(a) regression), country key
  inside the payload dict + tier-gate, probe_tools.py:413/:460 tier
  passthrough, ssr_div_list on `build_proxy_dict`-correct API
  (fixtures: `test_proxy.py:94-114`; confirm `test_template_output_
  collision.py:32-38` WRITE_TIME/ALL membership undisturbed); `/render`
  tier acceptance test if 3.6 ships with its caller. Ruff gate = DELTA on
  `webapp/ src/` only (PR-3 is almost entirely outside the gate;
  baselines: webapp 942, src 170; image ruff 0.16.4 vs local 0.16.5).

## Deploy & verification order

1. User merges wave-14 (LOCAL ONLY until pushed + merged via GitHub UI).
2. E4 + E6 run before PR-3 (minutes); E1's env decision comes AFTER deploy
   from the new gauges (see gates table).
3. Wave-15 order: **PR-1** (1.0+1.1+1.2+1.3+1.4 same deploy) → **PR-2a** →
   **PR-3** (3.7 doc truthing first; then 3.1, 3.2-after-3.5, 3.3, 3.4,
   3.5, 3.6-with-caller) → **PR-2b** (default-OFF allowlist + harness;
   env canary; flip). django+celery images first, browser-service LAST.
4. Local e2e re-drive validation (arkswimwear/madewell double as canary;
   PR-2b's env-canary rides one of them), then failed re-drives
   133/135/114/115 one at a time.

## Risks

- **R1 (rewritten, honest):** claimed-then-crashed rows are recoverable ONLY
  by the watchdog (~35 min, mislabeled worker-loss); the sweep is blind to
  them; container death covers nothing. Mitigations shipped: pre-graph
  fast-fail (RUNNING + no `started_at` + 0 SessionLogs → fail on first
  sighting) + the sweep for the never-dispatched class + `MaxRetries
  ExceededError` honest-FAILED catch.
- **R2 RESOLVED:** streaming LiteLLM retries are async-safe
  (`_astream` verified; constructor `streaming=True` lane). Residual risks
  owned elsewhere: the executor-join overshoot (PR-2b 1b fix) and
  cross-loop `Event loop is closed` (added to classified transport errors).
- **R3:** `country` changes Bright Data egress per http_navigation job —
  that IS the fix (probe/execution identity parity); geo-fenced catalogs
  (madewell /in/) remain the caveat; E5 gates.
- **R4:** PR-1 items 1.0+1.1+1.2 deploy TOGETHER (keystone → claim → sweep).
- **R5 (new):** `shutdown_default_executor` fix changes loop teardown for
  EVERY dispatcher site — the harness's overshoot pin (before/after) is the
  guard; kill switch = empty allowlist keeps code_writer sync.
