# Wave-19 Fix Plan (audit-corrected)

Status: PROPOSED — not yet green-lit. Every item below was adversarially audited
(3 read-only agents, 2026-09-05) against the current tree; file:line refs are from
that audit. Supersedes the pre-audit 7-item wave-19 sketch.

## Failure classes driving this wave

**Job 324 (myhouse, phase attribution corrected):** tester PASSED (0.97), then the
tester node's own deterministic Phase-1 zero-yield force-FAIL gate
(`graph.py:5757-5806`) killed the job — `execution` `started_at=None`, never ran.
The draft (api_scraper-family, `internal_api`) had a custom `_http_get` with
`proxy_config.is_banned()` but NO `proxies=` kwarg; `DIRECT_ONLY_TIERS` defined but
never consumed; `SCRAPER_PROXY_TIER`: 0 occurrences. All 3 discovery paths zeroed =
egress-IP throttling. Root enablers: (1) the zero-yield probe env
(`graph.py:5189`) stages NO `_stealth_env` — it tests a weaker identity than
execution would use; (2) the api template emits no `metadata.discovery_coverage`,
which gates OFF the already-shipped execution-time strategy+tier recycle
(`graph.py:3694-3699`) and the empty_render transient suppression; (3)
`.opencode/agents/code-writer.md:52-56,194` falsely tells the writer the fetch
machinery is "already wired" and lists `_http_get` as correct-as-written.

**Job 323 (theiconic, 4-defect compound):** (D1) browser_traverse cross-job
contamination — concurrent myhouse job's page (shared MCP Chrome) was judged and
recorded as theiconic's listing (NAV-SUMMARY: working_url=myhouse/…); S17 then
measured `direct_http_residential` against the WRONG site's listing. (D2)
product_analyzer 1800s INVOKE-TIMEOUT → no product_analysis.json. (D3) THE KILLER:
validate_coverage set `error_message` on the missing-analysis interrupt, recovery
left it set, finalizer `elif job.error_message: FAILED` outranks a 4-product
execution (`tasks.py:~1318`). (D4) tester failed 3 cycles ("Ready for execution:
false") but intake jobs run `skip_approvals` → human_approval auto-approved the
exhausted arm into execution (jobs-79/80 patch covered the writer-failed arm only).

## Tier 0 — small, independent, do first

| # | Fix | Where | Size |
|---|-----|-------|------|
| T0.1 | Stage `SCRAPER_SOFT_BLOCK_MIN_BYTES=20000` on **celery-worker (primary)** and browser_service (secondary). The detector (`src/http_fetch.py:169-186`) is env-gated OFF by default and only reachable where `src.http_fetch` runs — which is the in-process celery path (`run_execution.py:1259-1265`), not browser_service (0 hits there). Code default stays 0. | `docker-compose.yml` both env blocks | 2 lines |
| T0.2 | Stage `_stealth_env(state)` into the Phase-1 probe env — the gate that killed 324 tests a strictly weaker identity than execution. | `graph.py:5189` | ~3 lines |
| T0.3 | Make `HTTP_METHODS` prefix-derived (`_HTTP_METHOD_PREFIXES`) so fingerprint listing wins aren't mislabeled `needs_browser=True` (forces the S4 upgrade; pre-existing 2-line bug). Seed listing probe with PDP fingerprint wins. | `probe_tools.py:64,669,732-733` | ~4 lines |
| T0.4 | Preserve `needs_browser` + `fingerprint_profile` into `connectivity` (currently dropped at `graph.py:1832-1848`; `js_rendering_needed` is written but never read) and into `access_recipe` (`:4150-4160`). Parse profile from `method_that_worked`. | `graph.py` ×2 | ~10 LOC |
| T0.5 | **api template emits `metadata.discovery_coverage`** (stop_reason, discovered_urls, per-path outcomes incl. all-paths-blocked), mirroring `requests_scraper.py:546-606`. Arms the ALREADY-SHIPPED execution recycle (`graph.py:3694-3699` → `_escalate_tier_axis`) and the transient suppression. Highest leverage per line in this wave; also fixes the empty `stop_reason=` in 324's error. | `templates/api_scraper.py` main() | ~20 LOC |

## Tier 1 — the walls

| # | Fix | Notes |
|---|-----|-------|
| T1.1 | **Structural ladder-preservation gate** in `webapp/agents/draft_safety.py` (has only 3 helpers today), modeled on `cli_contract_violation` (`run_execution.py:207-273`): for nav-mode drafts, AST-scan for (i) `from src.http_fetch import` OR (ii) any HTTP GET/POST call carrying `proxies=` OR (iii) `create_fetch_*`/`get_escalation_tier`. Absent all three → force-FAIL at writer→tester with fix directive + refusal at run_execution. Do NOT key on `DIRECT_ONLY_TIERS` (dead token — defined, never consumed). THE only intervention that prevents the 324 draft class from existing. | consume in `_invoke_code_tester` + `run_execution` |
| T1.2 | **Identity escalation at the actual death point**: `_probe_phase1_discovery_once` (`graph.py:5134-5349`), NOT `_maybe_retry_execution_listing` (unreachable for this class). On dead yield: ONE re-run at next configured rung of `_PROXY_TIER_LADDER` (`graph.py:3457`), skip-unconfigured via `_tier_configured`, never downgrade (`:3810-3816`), no-op at residential. Honesty: attach `{"tier_escalation": {...}}`; on double failure set a FAIL-class `stop_reason="all_tiers_blocked"` and ADD it to `_COVERAGE_FAIL_STOP_REASONS` (`route_after_testing.py:115-118`) or routing sends it to cleanup anyway. | bounded, reuses shipped ladder semantics |
| T1.3 | **api template Phase-1 ladder → shared module**: replace inline `fetch_api` ladder (`api_scraper.py:113-133`) with `_get_fetch_json()` (already used in Phase 2, `:301-330`) so Phase 1 inherits `detect_soft_block` + real escalation; make `http_navigation_scraper.py:_http_get` (`:62-84`) propagate `SoftBlock` instead of collapsing to `("",0)`. | removes the strippable-ladder invitation |
| T1.4 | **Fix code-writer prompt false claims**: `.opencode/agents/code-writer.md:52-56` ("proxy + fetch machinery already wired") and `:194` (`_http_get` listed as correct-as-written). Zero runtime cost; removes the license the 324 writer accepted. | docs |
| T1.5 | **Stale-error poison (323/D3)**: clear `error_message` (or move to `interrupt_history`) on every recovery path out of validate_coverage-style interrupts; finalizer must rank a productive execution (items>0) above a stale interrupt-time error. TDD in finalizer + validate_coverage tests. | `validate_coverage.py:162-172`, `tasks.py:1318` |
| T1.6 | **Same-domain assertion on traverse results (323/D1)**: working_url, listing_url, api_endpoint, item_links must match the target's registrable domain — else discard nav analysis + one forced-homepage re-traverse, else honest fail. S17 listing probe verifies host BEFORE measuring tier. | browser_traverse post-processing + S17 |
| T1.7 | **skip_approvals conservative defaults (323/D4)**: testing-exhausted arm routes to cleanup under `skip_approvals` (mirror the jobs-79/80 writer arm at `graph.py:4764-4777`) instead of auto-approving execution of a failed draft. | `route_after_testing` exhausted arm |
| T1.8 | **Scope tool workspace (323/D2-twin)**: root search/read guards at the job's own slug (tester globbed 7 other sites' workspaces). | guards |

## Tier 2 — fingerprint recipe (corrected #2), behind `SCRAPER_HTTP_FINGERPRINT_REROUTE=1`

Prereqs T0.3/T0.4 first. Then:
1. `_derive_strategy` branch: `fingerprint_*` AND `js_rendering_needed is False` → `http_requests` (funko-class SPA sites MUST stay on browser — the probe's own `needs_browser` measurement, `probe.py:1156-1161`).
2. Exempt `fingerprint_*` from the tier reset at `graph.py:4106-4107`.
3. `_enforce_anti_bot_strategy` (`graph.py:419-477`): do not rewrite strategies carrying a measured HTTP-fingerprint win with `needs_browser=False` (else the reroute is silently reverted on exactly its target sites).
4. `src/http_fetch.py`: profile tuple read per-factory in `_ladder_clients` (import-time `CURL_IMPERSONATE` at `:79` can't express fallback), one rung per profile from `resolve_tiers`, recompute `tiers_total`/`min_tier_floor` (6 pinned tests: `tests/test_http_fetch_module.py:118-128,224-259`), map `SCRAPER_HTTP_MIN_TIER` so measured wins don't re-walk `none→dc→res`. Note fingerprint egress is hardcoded residential (`:260-262,370-372`).
5. api/shopify templates: inline discovery ladders → shared module (they have no fingerprint rung at all).
Real cost: 120-180 LOC + tests. Deploy second, after Tier 0/1 soak.

## Tier 3 — knowledge / browser layer (heavily corrected)

| # | Fix | Audit correction |
|---|-----|------------------|
| T3.1 | http_navigation discovery tier: `DISCOVERY_PROXY_TIER` fallback + `_effective_discovery_proxy_tier()` + `proxy_tier` param on `_navigate` + 4 Phase-1 call sites; `--no-proxy` overrides both; add the missing source-scan test (sibling of `test_wave17_s17_s20_recipe_tiers.py:280-326`). ~25-35 LOC, NOT 10. | form-search HTTP path can't be fixed by env var (`_http_get` is ladder-driven; `_http_post` has no proxy at all) — out of scope, say so. Consider log-first: log when tiers diverge for one wave, implement only if it bites. |
| T3.2 | Skill rewrite (akamai-detection + anti-bot-handling) against real machinery: BOTH ladder orders (browser_service per-tier `probe.py:39-49,93-104`; webapp gate HTTP-first `probe_tools.py:42-65`), exact 15 names, deprecated uc_chrome aliases, unconfigured-tier skip, same-tier cloak bypass, `/probe-akamai` + `AKAMAI_SEMAPHORE` don't exist, retire UC-for-Akamai + `akamai_stealth_scraper.py`; scraper-side http_fetch ladder as separate section (never conflate the two ladders). Preserve/deliberately drop `## Learned:` tails and VERIFY the FM seed at worker boot (`skills_store.seed_from_image` can silently no-op on drifted copies). ALSO audit the injected template hints (`subagents.py:2998-3012`) — that's where code_writer's effective anti-bot guidance lives. Fix the dead `require_non_akamai_tool` guard (`guards.py:121-144`, fires only on retired `uc_chrome_*`) + CLAUDE.md's stale guard description. | |
| T3.3 | Akamai classifier LITE (passive signals only): `_detect_akamai(html, status, url, headers, cookies) -> dict` + `_detect_akamai_bool()` keeps routing byte-identical; `/akam/<gen>/<hash>` regex, `bazadebezolkohpepadr` input name, URL bitfield decode, pixel-hash arithmetic. DROP the 201-trap oracle (needs sensor POSTs/CDN capture — false premise). Plumb reporting through `_format_probe_result` (`probe_tools.py:156-207`) + `page_analysis.format_probe_result` (`:205-228`) or agents never see it. Fixture tests + false-positive corpus. HIGHER-VALUE USE: persist an `akamai_fronted` bit (ProbeCache column) → start ladder at `cloak_{tier}`, saving 3-4 rungs on the worst sites. Also probe-order: defer the same-tier cloak bypass until after that tier's fingerprint rungs (`probe_tools.py:414-475`) — saves a 15-45s browser launch per Akamai-detected tier at zero happy-path cost. | |
| T3.4 | Cookie persistence: DROPPED as designed (`_launch_page` takes no URL — can't key a jar; `cookie_manager` revival imports seleniumbase onto the hot path; its `has_valid_abck` classifies unsolved `~-1~` as valid — repo's own sephora jar proves it; no failing job needs it). If ever wanted: state-side pass-through of `final_cookies` via existing `/navigate cookies=` contract, per-job scope, behind a flag. Also delete the 3 stale `data/akamai-cookies/*` files and guard the dir in Dockerfile (`COPY data/` bakes them in). | |

## Explicitly rejected by audit

- Fix #1 in `_maybe_retry_execution_listing` for this class (job never reaches it).
- Browser_service-only staging for the soft-block env (ladder runs in celery).
- `DIRECT_ONLY_TIERS` marker gate (dead token — cosmetic).
- web-re-toolkit as dependency (63MB sidecar, frozen, different problem; intel only — classifiers adopted in T3.3).
- Bundling tier+listing into one retry budget (orthogonal axes; diagnose then pick).

## Test plan pointers

- T1.1: tests modeled on `cli_contract_violation` tests; CORRUPT draft = a 324-shaped `_http_get` (banned-check, no proxies).
- T1.2: extend `tests/test_job85_zero_yield_gates.py`; new stop_reason routing case in `route_after_testing` tests.
- T0.5/T1.3: template source-scans (api emits discovery_coverage; api Phase-1 uses shared module).
- T1.5: finalizer ladder tests (4-product execution + stale error → COMPLETED).
- T2: update 6 pinned `test_http_fetch_module.py` cases; add `_derive_strategy` fingerprint matrix × `js_rendering_needed` × `_rendering`; `_enforce_anti_bot_strategy` non-rewrite case.
- T3.2/T3.3: fixture-based signal tests + false-positive corpus; golden test that `needs_akamai_bypass` is unchanged on `test_wave15_proxy_parity.py` scenarios.
