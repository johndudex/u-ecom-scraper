# Production jobs 255+ — Root Causes & FINAL Fix Plan (v4)

> Status: **IMPLEMENTED — all fixes committed on file-master-artifacts (16 commits, 143 tests green). Awaiting user deploy.**
> Server 154.38.174.128 @ `1d2e5fb` READ-ONLY. All fixes land locally; user deploys.
> Census: 51 jobs ≥255 — only 320 (511) + marginally 335/319 produce defensible data; 5 completed-at-zero; 331 (wrong-domain) + 337 (zero core fields) are data-quality disasters shipped as COMPLETED.
> 287-318 gap: benign (ids burned in the postgres crash; nothing lost). Closed.

## Part 1 — Diagnosis (final, D1-D8 + N1-N6)

| # | Jobs | Root cause |
|---|---|---|
| D1 | 255-281, 329 (24) | Legacy `/` form (no URL-list field; multi-URL paste → varchar(200) 500s; single URL → doomed url_list job) + FM fallback dead (never seeded). Deploy b86aa06→1d2e5fb innocent. |
| D2 | 263/266/327 | Cancel → COMPLETED (`tasks.py:846-857`); Site flipped complete w/ 0 products. +N3: approval rows recorded `approved` on Cancel (`views.py:722-726, 788-792`). |
| D3 | 285, 319 | `run_execution.py:305-331` strips undeclared discovery flags (logs, doesn't fail) + env-gate exists only in playwright template → seed-file fallback wins. 319: picked the tester's 5-item sample (written 32s pre-execution) — real data, but `description==title` 5/5 (N5). |
| D4 | 325/328/334, 272 | Orphan-killer kills persistent Chromes' children every 30 min (`server.py:247-283`; only 2 top-level PIDs; `/scrape` not counted). **Still live in prod** (48 kills/24h). Retry classifier vetoes `connect_over_cdp` w/ traceback (`scraper_runner.py:219`). |
| D5 | 284/332/333 | postgres OOM (212/256MiB; `mem_limit: 256m`) → task death mid-finalize; except-path re-dies; beat dead 10 days (missing `PYTHONPATH: /app`, no restart policy). Worker idle ~2 days. Job 333 heartbeat timer still leaking. |
| D6 | 272, 322 | Command-union shadow: static/conditional edges + `Command(goto=...)` both execute (mechanically confirmed: static edge = extra writer after Command's). product_analyzer (3 paths) + site_analyzer (2 paths). |
| D7 | 257 | Ownerless approvals invisible in approval views; **jyo is not superuser** so F11-as-v2-scoped helps only `admin`. 322 is visible, just ignored. |
| D8 | 324/327/336 | Nav 0-item completions; `run_execution`'s own outputs were 0 (324/336's 5-item files are TESTING-phase samples, 2-5 min older — NOT rescuable; see F16). |
| N1 | 337 | Ground-truth override hole: `route_after_testing.py:481-488` + `_scraper_has_real_items` (:252-261) uses `any(core) OR has_substantive_field` → `brand` alone passes → FAIL/0.35 report shipped COMPLETED, 36 price-less rows. |
| N2 | 331 | Cross-domain contamination: nav followed footer locale link → all artifacts `.com.au` under `.us` job; url_judge has zero domain logic; `input_urls.json` contamination is downstream (slug derivation is correct — no sharing bug). |
| N5 | 320/319/285 | Field-sanity gaps pass coverage (constant availability 511/511, description==title, 98% empty description, price churn $40→$2300→₹5600). **No production fix — F14 assertions only; documented known hole.** |
| N6 | 330 (95%), 335 (87%) | failed/discovered ratio never gated. |

## Part 2 — Final fixes

### Deploy 1 — Substrate + correctness (with the 3 undeployed pagination commits)

**Ops first (user executes; commands prepared by us):**
- **O3** postgres `mem_limit: 256m→1g` + `restart: unless-stopped`. *Most urgent single line.*
- **O1-env** beat: `PYTHONPATH: /app` + `restart: unless-stopped`. Schedules stay OFF initially; re-enable `cleanup-stuck-jobs` + `redispatch-stuck-approved` after Deploy-1 regression (they auto-fail 284/332/333 = O4). Keep `schedule-next-site` OFF. **Heartbeat answer: [EXEC-ALIVE] rows count** (change prefix at `run_execution.py:568,725` + remove from exclude list `tasks.py:1077-1078`); optional threshold ≤45min.
- **O5** drain 257 (admin/psql) + 322 (jyo; expect one extra cancel — corrupt artifact re-interrupts).
- Hygiene: delete `webapp/core.7`; never commit `scrapers/books-toscrape-com|desidime-com`.

**Code (each its own commit, test-carrying):**
1. **F1+M1** orphan killer: `SCRAPE_IN_FLIGHT: dict[rid→monotonic deadline]` (deadline = `timeout*max_retries + 600s` grace); register/deregister inside the executor fn (try/finally — held until child reaped); `_restart_lock.acquire(blocking=False)` + skip cycle; children-walk under the lock. **M1: `Popen`+`communicate` (NOT `subprocess.run` — it reaps before your handler), `start_new_session=True`, `killpg(proc.pid)`** (pgid==pid while members live; cloak Chromium is detached by Playwright and escapes — covered by counter+killer by design).
2. **F2+M2** retry: relax `_has_traceback` veto only when stderr names the CDP endpoint (`connect_over_cdp`/`9223`); N-second restart cooldown for concurrent /scrapes.
3. **F3+M3+N3** cancel: `is_cancel`-equivalent on `final_state["human_response"]` → STATUS_CANCELLED; **STATUS_CANCELLED early-return** in `_finalize_job`; Site stays in_progress on cancel; approval rows get `STATUS_REJECTED` when `_build_resume_value` yields reject (**both** `approval_inline:722-726` and `approval_detail:788-792`). +U5.
4. **F4+M5** DB: get_state retry once (pool min_size=0 self-heals; psycopg errors, not Django's — keep `except Exception`); get_state-fail-twice → FAILED (not silent COMPLETED); also wrap `job.refresh_from_db()` (:647); `close_old_connections()` in except-path; base-dict `CONN_MAX_AGE=60`+`CONN_HEALTH_CHECKS=True` (PgBouncer block overrides cleanly; health-check is the load-bearing half for celery).
5. **F5+M4** heartbeats: try/finally at `graph.py:1420-1422, 2881-2884, 3057-3061`; `_beat` self-cap (60 beats) + terminal-status check.
6. **F18** path-fn Command crash: `human_approval` node sets `test_retry_count=FINAL_RETRY_SENTINEL` + `human_feedback` when reason==testing_exhausted AND label=="Provide feedback for final retry" AND feedback; `graph.py:3744-3750` returns plain `"scraper_analyzer"`. (Node-before-path-fn ordering verified; all 6 sentinel consumers satisfied.)
7. **F15** ground-truth hole (moved to Deploy 1 — independent, 2 call-sites): in `_scraper_has_real_items` + OUTPUT-AS-TRUTH rescue (`route_after_testing.py:250-310`), **when `output_filter_fields(ct)` is non-empty, drop the `or has_substantive_field` escape and require `any(filter_fields)`** — NOT all-fields (would risk 320). Kills the 337 path.

**Deploy-1 regression:** aya + locumtenens + count assertions.

### Deploy 2 — Extraction quality

8. **F8+F16** output selection: `_find_best_output(*dirs, slug=None, mtime_floor=None)`; candidates filtered by floor; **rank by (substantive_item_count DESC, mtime DESC)**; no FM fallback when floored (tmpfile mtime laundering); floor passed ONLY by `run_execution.py:651` (=start, already captured :552); graph.py callers (:535, :3353) keep floor=None. **324/336 intentionally NOT rescued** (their 5-item files are tester samples; converting them = the 319 laundering pattern) — F9 fails them honestly. Commit message must say so.
9. **F9** quality gate (final spec): nav modes only; `good` = ≥1 core field (shared predicate with F15/F16); `bad` = failed_products + core-less items; **denominator = processed (good+bad), never total_discovered** (limit-truncation false-positives); fire when processed ≥5 AND fail-ratio ≥0.8 → `execution_status=FAILED` + `error_message` (U6); warn-only log at 0.5. **Site → failed (accept existing semantics; moots the rollback-stall note).** Only gate catching 330.
10. **F7+M6** `--listing-url` chain `discovery.listing_url or search.working_url or listing_url_used` (each candidate domain-guarded per F17); dedupe the env sources (:271-283 vs :695-701).
11. **F6** per-template env gates (exact code in round-3a report): requests_scraper (force the discovery branch too — in-place `[:]`); http_navigation (feed `args.listing_url/fresh_discovery`, BELOW FORM_ACTION); navigation_scraper (2-liner after :746); ssr_div_list (env-first at :221); **api_scraper declares `--fresh-discovery`** (zero-caller-change). New-generations-only (existing scrapers keep old behavior until regenerated — say so).
12. **F17** domain guard: `_sanitize_nav_domains(analysis, job_url)` helper using `_registrable`; blank-and-warn, placed **before** the site-root fallback (root fallback then re-fills on the CORRECT domain); patch BOTH exits of `_invoke_navigation_traverse` (:1793-1875 main + :1756-1767 MCP-fallback); run_execution env computations guarded; url_judge pre-filter (demote cross-registrable candidates, verdict "wrong"). For PLT-US: listing blanked → root fallback `.us` → correct seeds; if nothing found, F9 fails loudly. No slug-derivation fix needed (correct already).

**Deploy-2 regression:** aya (url_list happy path) + one nav-mode site + one page_param site + **F14 quality assertions** (availability not constant across N; description ≠ title; price stable across two tester runs; currency matches job locale). **Then O2** (FM seeding with rails: Site-backed slugs only, first-URL shape check, dry-run report, run server-side).

### Deploy 3 — UI + intake + topology

13. **F10** `/` POST rejection via `home.html`'s existing `context["error"]` pattern (:136-141 — NOT a bare 400; the form is plain HTML POST) + repopulated fields; varchar(200) guard; error text links to /intake/ AND **U3: add /intake/ to the sidebar nav** (base.html:423-441).
14. **+U1** "Cancelled" filter chip in job_list.html:22-26.
15. **F11** (needs user decision): ownerless approvals visible to all authenticated OR unowned bucket; widen **all three** queries atomically (list :763, count :774, detail :779 — else badge says 0 while list shows N).
16. **+I1** intake FM write hard-fails (502) instead of soft-warn (`views.py:2496-2505`).
17. **+I2 → F12** intake shrink guard at `views.py:2493` FIRST (route through `Site.save()`/`_sync_input_urls_file` semantics); **F12 (check_tracker seeds Site.input_urls from FM) ships only after I2**; job_restart accepts list_urls; surface parsed-URL count in the AJAX response (CSV junk lines are silently dropped today).
18. **F13** topology (final spec): delete `:3981-3988` (site_analyzer conditional) + `:4017` (product_analyzer→normalize edge); `_on_success` returns `Command(update=..., goto=...)` — site: `goto=_route_after_site_analyzer(st)` (**KEEP the fn — tests depend on it**); product: `goto="normalize_fields"`; **MANDATORY `fallback_goto` param on `_run_budgeted_agent`** converting the two terminal no-artifact returns (:1547, ~:1560) to `Command(goto=fallback_goto or "human_approval")` — else silent dead-end. Callers: site→"update_tracker_analysis", product→"normalize_fields". Keep :4018. Annotation pins: no-op (already `| Command`). Tests: update `test_browser_traverse_integration.py:82-95` (survives via kept fn); NEW test asserting product success reaches normalize_fields (goto typos fail SILENTLY); F3 updates `test_views.py:144-163` (post `choice=`, assert rejected); F18 + F17 new tests per round-3a §Tests.
19. **D2 backfill**: one-line psql to reset the 3 wrongly-complete Sites (reiss/prada/next).

### Known holes (accepted, documented)
- **N5** field-sanity: no production gate — F14 assertions only. Constant-availability/desc==title jobs ship between regressions.
- D6 class: F13 fixes the 2 known sites, not the class (topology rule not enforced globally).
- Existing `scrapers/{slug}/scraper.py` keep the flag-stripping hole until regenerated (F6 is new-generations-only).

## Part 3 — Verification checklist
Per-fix tests enumerated in round-3a §Tests (F13 reachability, F18 str-return, F17 domain filter, F3 approval-status, F8/F16/F9 behaviors, F1 Chrome survival during kill cycle, F2 classifier, F4 DB-drop). Deploy regressions as listed. Rollback: every fix its own revert; bind-mounts mean revert+restart, no rebuild.

## ══ IMPLEMENTATION RECORD (final) ══

| Commit | Fixes |
|---|---|
| f06414b | O3+O1 — postgres mem 1g + restart policy; beat PYTHONPATH + restart |
| 751b642 | [EXEC-ALIVE] rows count as liveness (beat-revival pre-condition) |
| 4cc2cf0 | F1+M1 — orphan killer protects full Chrome trees + running scrapes; killpg on timeout |
| 6525f00 | F2+M2 — retry CDP-connect failures despite traceback; restart cooldown |
| fa075bf | F4+M5 — finalize survives dead DB connections; checkpoint-read failure fails the job |
| c1c8206 | F5+M4 — heartbeat try/finally + chain self-termination |
| 71e374c | F3+M3+N3 — cancels finalize CANCELLED, Sites stay in_progress, approvals record rejected |
| dee3856 | F15 — ground-truth override requires core fields (job 337) |
| 4e1051d | F18 — path-fn Command crash on final-retry-with-feedback |
| 4ece325 | F8+F16 — mtime floor + best-of-N output selection (job 319/336) |
| 2d26317 | F9 — nav-mode extraction-quality gate (jobs 330/335/337/324/327/336) |
| 46d861b | F6+F7+M6+F17 — template env gates, guarded listing chain, cross-domain sanitizer (285/319/331) |
| 41f2828 | F10+F11+F13 — legacy-form guard, ownerless approvals, Command-only routing (D1/D7/D6) |
| b418651 | O2 — FM seeding script (dry-run default, rails on) |

**Tests: 143 green** (12 fix-specific files + pagination/discovery suites, in-container).

## ══ DEPLOYMENT RUNBOOK (user executes, in order) ══

**Deploy 1 — substrate + pagination (already-committed pagination commits ride along):**
1. `git push` + on the server: `git pull && docker compose up -d --build` (compose changes: postgres 1g+restart, beat env — f06414b).
2. Restart celery-worker once (kills job 333's leaked heartbeat timer).
3. Verify beat starts clean (`docker compose logs celery-beat | tail`), then confirm the two stuck-job watchdogs auto-FAIL 284/332/333.
4. `schedule-next-site` stays OFF until after Deploy 2's regression (settings.py CELERY_BEAT_SCHEDULE — edit on server or in a follow-up commit).
5. Regression: run aya + locumtenens; assert item counts match priors and no orphan-kill/CDP-DOWN churn in `docker compose logs browser_service` (the 30-min loop should be gone).

**Deploy 2 — extraction quality (this branch already contains it — deploys with Deploy 1's push since it's one branch):**
6. Regression: one url_list site (aya), one nav-mode site, one page_param site. Assert: completed jobs have items; 331/337-class jobs now FAIL loudly instead of shipping garbage.
7. FM seeding: `docker compose exec -T django python /app/scripts/seed_fm_input_urls.py` (review) then `--apply`.

**Deploy 3 — already in the same push (F10/F11/F13 with 41f2828):**
8. F13 topology is live at restart; the 2 waiting_approval jobs (257 via admin, 322 via jyo) should be drained before or shortly after — expect one extra cancel on 322 (corrupt artifact).

**Post-deploy watchlist:** browser_service logs (kill loop gone), postgres memory headroom, no new `unhashable type` (F18), approval list shows ownerless rows (F11), legacy-form rejections render (F10).

## ══ KNOWN RESIDUALS (documented, not fixed) ══
- N5 field-sanity (constant availability, description==title, price churn) — F14 assertions only; needs the product_analyzer quality work.
- Existing scrapers/{slug}/scraper.py keep pre-F6 behavior until regenerated.
- 320-class variant spam (511 rows = 188 base products) — expected two-phase behavior, surfaced to user.
