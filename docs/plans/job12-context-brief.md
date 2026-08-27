# Job 12 failure — canonical context brief for fix planning

> Status: EVIDENCE-BASED (2026-08-27). Two forensic passes: (A) Railway worker logs via GraphQL, (B) Django admin + File-Master artifacts. All facts below are verified, not inferred. This file is the single source of truth for planners and critics. Do not re-derive; extend or challenge explicitly.

## The failure

Job 12 — priceline.com.au, intake, `list_page` (`/c/gifts` + Marc Jacobs product URL), target_fields `["current_price","description","previous_price","ratings","remarks","scraped_at"]`. Ran 42m51s on Railway (16:40–17:23 UTC, 2026-08-27), FAILED. Prod deploy = full 36a91f0-equivalent tree (PR #17, all 9 services same commit; corruption defenses, quality-gate fix, 8 codegen fixes all ACTIVE — this failure happened with every known fix deployed).

### Failure chain (verified)

1. **Poison `api_endpoint` → wrong strategy.** `browser_traverse` captured `https://pricelineau.api.useinsider.com/api/info/824.24` (Insider personalization widget; NO product data). Deterministic strategy gate `webapp/agents/graph.py:3062-3073`:
   ```python
   if (_data_source == "api" and (_nav_api.get("url") or _nav_api.get("api_url"))
       and isinstance(_api_items, int) and _api_items > 0 and _api_count != 0):
       strategy = "internal_api"
   ```
   `count=null` passes `!= 0`. Product_analysis had corrected toward playwright/cx-state; the deterministic verdict overrode it. Contradictory authoritative inputs went to code_writer.
   - Yesterday's fix #2 (word-boundary `url_looks_like_data_api` + config-path blocklist, `experimental/nav_traversal/traversal.py:937`, wired :1059) kills **ketchcdn** (substring `"product"` ⊂ `production/config.json`). It does NOT catch useinsider (genuinely contains `/api/` segments). Poison-endpoint class is bigger than one URL.

2. **Retry amplification (3 codegen cycles):** cycle 1 truncated mid-write → *"Sorry, need more steps to process this request."* (tester correctly reported "scraper was never generated" — that path works). Cycle 2 built the useinsider scraper → 0 items → STRATEGY switch retry. Cycle 3: 10 min, 49 tool calls (search 34×, read 15×), **zero writes**. Context ballooning live: 127 deterministic trims this run, peak 350,460 chars, monotonic climb.

3. **Proximate cause: provider 429 killed the job.** Small model via DIRECT `api.z.ai/api/coding/paas/v4/chat/completions` (code_tester, glm-5-turbo): 4× HTTP 429 (`code 1302 "Rate limit reached for requests"`) in an 8s burst. Classified-retry: 3 attempts, sleeps 1.6s / 4.2s / 1.7s → exhausted in ~7.5s → `Error code: 429` propagated → whole graph FAILED. Main model via `llm.johnjf.xyz` LiteLLM proxy: 98/98 HTTP 200. Grep for the retry impl: `llm classified-retry` log string; also `classified retry` in `webapp/agents/` / `src/`.
   - The 429 never entered SessionLog (thrown after the last logged line; only in `error_message`).
   - It pre-empted the retry-exhaustion → `human_approval` interrupt path (0 approval rows — user was never asked).

### Ruled out (verified, do not re-investigate)
- Artifact corruption: job 12's `test_report.json` valid; F2/F6/M4 defenses deployed and functioning.
- Events worker gap: EventOutbox empty is DESIGNED (`emit()` gates on `created_via="api"`; job 12 was intake). celery-events live on Railway, healthy, 30s sweeps, no backlog.
- Deploy skew: all services on commit 4d299017 (PR #17); tree byte-identical to local 36a91f0 at deploy time.

## The five problem areas for the plan

**P1 — 429 / provider-error retry policy** (small, high leverage). Current: fixed ~1.6–4.2s sleeps, 3 attempts, job-fatal on exhaustion. Need: exponential/jittered backoff scaled to provider rate limits; decide whether rate-limit exhaustion should fail the job or pause/resume (note LLM_ASYNC_EXECUTION stays OFF — prefork incompatibility, do not propose async).

**P2 — Poison-endpoint / strategy-gate trust** (the real root cause). The gate has no way to say "this URL is not a data API". Yesterday's critique established: amnhealthcare's REAL product API is cross-domain (`api.amnhealthcare.io`, `count:null`) — so no domain-sameness rule and no "count must be non-null" rule. vistastaff's artifact carries `doubleclick.net` as api_endpoint (poison class 3, inert by luck). lw.com Coveo is the explicit-zero case the `count != 0` guard exists for. Candidate direction to stress-test: content-shape verification (fetch the endpoint once, require product-ish array evidence) vs. better URL heuristics vs. demoting the selector's confidence / letting corrected product_analysis win.

**P3 — Artifact completeness (validity ≠ completeness).** Published `product_analysis.json` was a `repair_json_text` pass-2 prefix salvage: parses clean, canonical `indent=1`, ends mid-regex with balanced closers, contains **1 of 6 requested fields**. The repair ladder converts truncated artifacts into valid-but-truncated ones that every downstream consumer accepts. Idea space: completeness scoring vs. `target_fields`/schema, salvage provenance marking, read-time warnings, refusing to publish salvaged artifacts as authoritative. Defense layers today: sanitize-on-write (`webapp/agents/tools/filesystem_tools.py:52` sanitize_json_content, :291 write_file, :353 edit_file), repair ladder (`webapp/agents/graph.py:281` repair_json_text, :355 _fix_json_artifact → renames unrepairable to `.corrupt`), copy guards (`filesystem_tools.py:108` guard_json_bytes — byte-identical passthrough by design for valid JSON).

**P4 — Date-bomb (trivial, in scope).** `webapp/scraper/management/commands/recompute_date_reliability.py:29` `FIXED_AT = datetime(2026,8,27)` midnight; comments claim end-of-day inclusive; `scraped_at` is `auto_now_add` → window silently excludes all rows from 2026-08-27 onward → recompute scans 0 rows (no error). Hand-widened 3× already (Aug 25→26→27). 2 tests currently fail because of it (`test_recompute_date_reliability.py::test_recovers_valid_dates`, `test_admin_recompute.py::test_apply_fixes_row`). Fix direction: unbounded/far-future end or drop the upper bound — plus a test that proves rows created "today+1" are included. It is LIVE in prod right now.

**P5 — Stale-artifact re-injection on resume** (amplifier, lower priority). Jobs 9/10's FM analysis artifacts were rehydrated into job 12's workspace (check_tracker skip flags: site_analysis skipped). Same bad inputs → identical wrong decision. Rehydration passes through `guard_json_bytes` (`webapp/scraper/setup_workspace.py:104`) — validity guard only, no freshness/consistency check.

## Hard constraints (from the project's critique history — violating these = plan rejected)

1. **No new per-run LLM cost** (no "ask an LLM to validate the endpoint/artifact" designs unless gated to near-never).
2. **Must not break the working sites**: amnhealthcare (cross-domain API, count:null), lw.com (Coveo, count:0 explicit), vistastaff, aya (26,742 via API), locumtenens, adameve, rmwilliams, zquiet, abercrombie (job 8: 90 products, slow-but-completed), books/quotes/gutenberg toscrape. A 25%-of-prior regression gate was ALREADY rejected for false-FAILing `scope=firstn` samples.
3. **Do not undo yesterday's critique-hardened fixes** (8 codegen fixes in `dbb52e0`, word-boundary tokens, catalog-guidance restoration, banded prior-count). Extend or supersede with evidence, never silently.
4. **Streaming stays on for all LLM calls** (proxy 504 workaround) — the lenient `parse_partial_json` path is a fact of life; F2 makes it safe at write time.
5. **No async refactor** (LLM_ASYNC_EXECUTION broken in prefork). No new infrastructure/services. Web-UI-only Railway deploys (user has no CLI).
6. **Deterministic scraper_analyzer stays deterministic** (it was de-LLM'd deliberately).
7. Suite baseline: 719 passed / 2 failed (the P4 date-bomb) / 2 skipped, ~723 tests, TDD is the house style. Fixes must come with failing-test-first plans.

## Success criteria for the plan

- Job 12's exact sequence (poison endpoint → wrong strategy → thrash → 429 → fail) becomes structurally impossible or non-fatal.
- Every change is generic (site-agnostic), evidenced against the constraint-1 site list, and test-locked.
- Artifact consumers can distinguish "complete and valid" from "repaired salvage" and behave accordingly.
- Recompute works regardless of when it's run.
- Each fix names: files, mechanism, failing tests first, rollout order, rollback, and what could break.

## Source docs for depth
- `docs/plans/codegen-fix-critique.md` (why yesterday's fixes are shaped as they are)
- `docs/plans/codegen-contract-audit.md`, `docs/plans/codegen-regression-analysis.md` (job 9/10 anatomy)
- `docs/plans/artifact-corruption-rootcause.md`, `docs/plans/artifact-corruption-fixdesign.md` (defense design + deliberate deviations: F1-lite NOT implemented; M2 lenient parser accepted)
- Forensics raw: job-12 test_report at `/tmp/job12_forensics/artifacts/pl_test_report.json` (may not survive reboot; facts above are extracted)
