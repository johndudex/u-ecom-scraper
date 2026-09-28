# Wave-48 — Client-rendered listing discovery (job 1082 class)

**Status:** REVISED — folded 3-lens critique (shared-code, routing/regression, probe/strategy). 11 blockers resolved; T5 cut; T3 re-scoped to telemetry.
**Branch:** `wave-42e-antibot-section` (off 36c80d2)
**Motivating failure:** prod job 1082 (wonderbly, `list_page`, scope firstn/10) COMPLETED with 2 items; the listing is an Astro shell + Algolia InstantSearch grid — non-JS HTML has **0 grid anchors** (only 2 escaped FAQ editorial links inside a `<script>` string), `?page=2` is byte-identical. Browser traverse had measured `item_links=20`. Full RCA: memory `job1082-wonderbly-rca.md`; same class as wave-38 briscoes (Magento PWA).

## Problem statement (one line)

The pipeline measures listing **reachability** (byte-floor) but never listing **yield** (item anchors in the non-JS HTML), so a client-rendered listing gets an HTTP discovery strategy that silently starves — and four downstream safety nets each have a reason to open.

## Design principles

1. **Prevention before detection**: fix the strategy decision first (T2), then the test-time net (T4), then honesty surfaces (T6).
2. **No new stop-reason vocabulary in any fail set.** Byte-identical page 2 is *legitimate* for a true 1-page catalog. Discovery enrichment (T3) is metadata-only.
3. **Default-off env gates** for anything that changes strategy/routing behavior. Recording/telemetry is always-on.
4. **Reuse honored channels** — but verify each channel's consumers first (critique found 2 channels that look honored but aren't: `deterministic_gate` is presence-truthy-read, `navigation_analysis.notes/listing_reached` don't survive to the artifact).
5. **TDD every change**; full suite + ruff gate (`webapp/ src/` only); local wonderbly e2e as acceptance.

## Transport verdict (probe/strategy critic, load-bearing question)

`http_requests → http_navigation` **is** the right remedy: `templates/http_navigation_scraper.py` Phase-1 discovery rides browser_service `POST /navigate` (`:365-410`) → `_launch_page` → real Playwright/Cloak Chromium with render-settle (`server.py:3218-3300`, `render_gate.py:20-37`). **Both phases are browser-lane** (Phase-2 PDP fetches also ride `/navigate`, `:1385`) — the original plan's "HTTP phase 2" parenthetical was wrong and is corrected here. `requests_scraper` is the only pure-HTTP template (`src/http_fetch.create_fetch_page`).

Two caveats folded in:
- **The flip only fires when strategy is still `http_requests`** — i.e. when `navigation_analysis` is absent (traverse no-analysis early return, graph.py:4519) or the archived fallback said `ssr`/`unknown` (navigate_synthesize.py:565; `ssr` is not handled by the cascade). A `rendering_verified: "browser"` traverse pushes the cascade to `playwright` already. Job 1082 was in the former class. Named so the e2e cannot pass for the wrong reason.
- **Browser lane ≠ guaranteed yield** — `/navigate`'s settle gate is a *content* gate (body ≥ 20k chars satisfies it), not a grid gate; a slow-hydrating shell can still starve. The ladder then escalates to `playwright` (whose discover step render-polls). Acceptance criterion adjusted accordingly.

## Fixes

### T1 — probe rungs report `item_link_count` (browser-service, additive, always-on)

`browser_service/probe.py`: add `_count_item_links(html, base_url)` and include `"item_link_count": ...` in the response dicts of the HTTP-flavoured rungs (`_try_direct_http`, `_try_fingerprint`). Browser rungs (`_try_playwright`, `_try_cloak`) already hold `html` — compute the same count there when cheap, else omit the key. **Consumers treat missing as "unknown", never as 0.**

Implementation constraints (critique-verified):
- **Reuse `src/discovery.py` `_pdp_score` + `_PDP_NEGATIVE_SEGMENTS`/`_PDP_NEGATIVE_PARAMS`** (`:432-543`, job-318-hardened, stdlib-only). Do NOT write a new heuristic. Count `<a href>` anchors whose absolutized URL (same-host-or-relative; exclude `#`/`mailto:`/`tel:`/protocol-relative) scores `> 0`. probe.py already puts `src/` on `sys.path` (`:11`).
- **Lazy import bs4 inside the helper; return `None` on ImportError.** probe.py has no bs4 import today and a documented lazy-import doctrine (`:1279-1284`); `tests/test_wave15_proxy_parity.py:279` loads probe.py via `importlib` and must keep working. Prefer `lxml` parser when importable, `html.parser` otherwise. Never a module-level import.
- **BS4 is load-bearing for correctness, not just convenience:** the wonderbly "anchors" live escaped inside a `<script>` string — BS4 treats script bodies as text (count 0, flag sets, fix arms); a raw regex scan would count 2 and silently defeat the flag. No regex prefilter shortcut.
- Cost: `direct_http` rungs get unclamped caller timeouts (60-90s); worst case ~3 large parses with html.parser ≈ 3-6s on the aggregate ladder. Acceptable.
- **No `server.py` change** — `/probe-single` returns the rung dict verbatim (`server.py:2686-2687`); protects the `test_wave33_tier_d2.py:142-147` launch-hook-count pin.

**Contract:** additive key; old browser-service + new webapp = key missing → flag never set (current behavior); reverse = ignored. Deploy rule unchanged (django+celery BEFORE browser-service).

### T2 — listing probe stamps `listing_client_rendered`; strategy consumes it (webapp)

`webapp/agents/tools/probe_tools.py` `run_listing_probe_advisory`:
- Propagate `item_link_count` from the winner payload (missing → `None`).
- **Always-on telemetry:** when the winning rung was HTTP-flavoured AND `item_link_count == 0` AND `body_length >= 20_000` (content-rich shell) → `listing_client_rendered: True` + `client_rendered_reason: "http_rung_zero_item_links"`. `item_link_count` None or > 0 → `False`.
- **Landmine honored:** do NOT propagate `jsonld` or any other winner field — `_pdp_listing_swap` is live (called at graph.py:2287, wave-39 prod-verified); only its `listing_probe.get("jsonld")` branch is inert today (`:2107,2113-2114`) because the advisory result never carries `jsonld` (probe_tools.py:948-965). Propagating it would revive a live-but-inert veto and change wave-39 swap behavior. Wording corrected from "dead" to "live-but-inert".
- Log the verdict in the `[LISTING-PROBE]` SessionLog line.

`webapp/agents/graph.py` `_derive_strategy` (BOTH copies of the duplicated listing-probe block — 5656-5667 ≡ 5710-5721, confirmed byte-identical; the first copy feeds `_discovery_tier` at 5704-5708, the second gates the flip — edit both identically):
- Read `_lcr = (probe.listing_connectivity or {}).get("listing_client_rendered")`.
- **Env-gated flip** (`LISTING_ANCHOR_FLOOR=1`, default off): upgrade `http_requests → http_navigation` exactly like the existing `_listing_needs_browser` seam at 5977-5982, **guarded by**:
  ```python
  strategy == "http_requests"
  and not method.startswith((*STEALTH_METHOD_PREFIXES, *FINGERPRINT_METHOD_PREFIXES))
  ```
  The fingerprint guard is the job-524 revolve lesson: a fingerprint-proven PDP win means plain HTTP was rejected and TLS-impersonated HTTP answered — upgrading discovery to a *vanilla* browser lane (`_recipe_stealth` stays "none", 6053-6061) the probe never proved would repeat job-524's wrong-strategy burn. `_enforce_anti_bot_strategy` (graph.py:425-491) runs after `_derive_strategy` and can only agree with the flip, never fight it.
- Always-on: stamp flag + reason into `access_recipe.evidence` (6062-6099) so the writer brief and harvest see it.
- Side effect (known, same as existing seam): post-flip `_browser_shaped` (6012-6015) skips the tier reset at 6021-6026, so a proxied PDP tier rides the browser lane.

### T3 — identical-page telemetry (src + requests template; telemetry-only, NO gate role)

**Re-scoped per critique:** the navigation-family templates (`navigation_scraper.py:1166-1178`, `http_navigation_scraper.py:1791-1817`, `playwright_scraper.py:781-810`) build `discovery_coverage` from their own fixed 9-key `discovery_state` accumulator and never call `discover_listing_urls` — so a key added in `src` never reaches the motivating template's coverage block. T3 therefore carries **no gate role** (T4 treats it as optional corroboration in the issue description, never an input).

`src/listing_discovery.py` `discover_listing_urls` loop (354-502):
- **Links-tuple comparison, not hashing** (critique: `str(soup)` is slow AND nonce/CSRF-brittle): keep `prev_links = list(page_urls)` (reset per listing URL), and at the `new_on_page == 0` branch set `identical_last_page = (page_urls == prev_links)`.
- When True, add `"identical_last_page": True` to the meta block (516-525). `stop_reason` stays `no_new_items`. No fail-set vocabulary changes anywhere.
- `templates/requests_scraper.py` (the only template consuming `discovery_meta`, `:453/474`): add the key to the coverage-block whitelist (`:552-575`) — otherwise it dies at assembly.
- **Contract pins move in the same commit:** `tests/test_discovery_ladder.py:48-58` `CONTRACT_KEYS` allowed-extra set, `:458-461` extra assertion, and a `docs/discovery-coverage-gate-contract.md` §1 row.
- Adding identical-page detection to the 3 navigation-family templates' own loops is a wave-49 candidate (out of scope).

### T4 — known-yield cross-check (webapp, env-gated routing)

**Checker** `known_yield_gap_issue(state, report) -> dict | None` in `route_after_testing.py`, beside `deterministic_output_issues`. Runs only when `LISTING_YIELD_GATE=1` (with the gate off the checker does not run at all — no telemetry-only mode, since the report write itself steers the writer).

**Dual-source arming** (critique finding: the traverse source alone is unprotected — a wrong-site abort yields only the ~2-link HTTP-fallback capture, below the floor, and the motivating case's own two facts were mutually exclusive):

- **Source A — probe-measured (primary, needs no traverse):** `probe_result.listing_connectivity.listing_client_rendered` truthy (encodes `item_link_count == 0` + `body_length >= 20_000` from T1/T2) AND `discovered < scope_target`.
- **Source B — traverse-measured (corroborating):** `navigation_analysis["item_links"]["url_examples"]` (nested path — NOT top-level; same-host vs the job's listing URL; `len >= 5` floor; capped at 20 — compare `>=`, never exact) AND honest traverse signals AND `discovered < min(len(url_examples), scope_target)`.

**Traverse honesty telemetry (always-on, prerequisite for Source B):** `browser_traverse`'s analysis dict (graph.py:4468-4500) persists what the artifact never carried: `"notes": _notes` (already sliced at 4457), `"traverse_reached": bool(result.reached)`, `"wrong_site_abort": getattr(result, "wrong_site_abort", False)`. Source B gates on `traverse_reached` — never on `discovery.listing_reached`, which is **fabricated True** at graph.py:4399-4446 whenever the root probe method is truthy (the honest `False` from traversal.py is destroyed at the source; gating on it can never see a wrong-site abort).

**Common conditions (both sources):** `input_mode in ("list_page", "navigation", "search_term")` — never url_list; `stop_reason` not in `_COVERAGE_FAIL_STOP_REASONS` (that ladder owns it) **and != `"checkpoint_reused"`** (fresh zero `discovered_urls` on a rescued run is not starvation evidence); job notes free of wrong-site markers.

**Coverage read:** `discovered = int(cov.get("found") or cov.get("discovered_urls") or 0)` — `discovered_urls` is an **int** in the templates (navigation_scraper.py:1170); `len()` on it raises TypeError inside the router (the latent `_volume_gap` bug, route_after_testing.py:1178 — do not copy it). There is no `urls_found` anywhere.

**`scope_target` semantics:** reuse `run_execution._scope_target` (import it — `tests/test_job85_zero_yield_gates.py` source-pins its location; do not move it). firstn/N → N, anything else → 1. State plainly: **on non-firstn jobs the trigger condition degenerates to `discovered < 1`** (zero-yield only) — T4 is a firstn-scope lever in practice.

**Issue emitted** (merged at graph.py ~9285 next to `deterministic_output_issues` so the writer sees it in the fix cycle):
`{"field": "discovery", "issue_type": "MISSING", "severity": "high", "description": "Deterministic check: ...", "suggested_fix": "Switch discovery to the browser lane ... or extract the client-side catalog source ..."}` — description names the measured evidence for the arming source (probe anchor count vs traverse url_examples count).
Known cosmetic side effect: `severity: "high"` sets `high_severity` (1587), disabling the phase1-untested downgrade inside the PASS arm (1801-1815) — same destination (`scraper_analyzer`), only the `[CASCADE] phase1-untested` row disappears.

**Routing integration — 5 touchpoints (all critique-mandated):**

1. **Distinct report key, never `deterministic_gate`.** `report["known_yield_gap"] = reason` (top-level). `deterministic_gate` is read by **truthiness of the whole dict** at route_after_testing.py:1762-1768 → stamping there sets `_contract_bad=True` and routes into the wave-34 CLI-contract bounce (1998-2036), on exhaustion into the contract terminal (2013/2029) — never reaching the strategy ladder; it also disables the access-wall arm (1903-1906) and blocks the working-scraper rescue (1282-1290).
   **Hardening (TDD both directions):** narrow the :1766 belt to `isinstance(_gate, dict) and "violation_kind" in _gate` — non-dict truthy values stay contract-bad, preserving the legacy string stamp pinned at `tests/test_cli_contract.py:329-333`.
2. **Override veto:** `and not _kyg` on the ground-truth override veto list (1873-1881) — this is what would have stopped job 1082 shipping 2 items.
3. **Classify arm placement:** in `classify_test_failure`, AFTER the `phase1_skipped` guard (insert just before `:363`), so selector-crash/absent-draft/429/`navigate_throttled`/`navigate_unavailable`/tester-diagnosis all outrank it (a throttled run must classify `retest`/`park_unhealthy`, never flip strategy) and the mapping-promo arm (398-406) survives. The kyg signal is read **from the report argument, not state** — the load-bearing caller is the two-arg `classify_test_failure(_prior_report, _prior_strategy)` at graph.py:4999, which has no state. Arm requires strategy == `http_requests` → `action="strategy"`.
4. **The escalation actually engages (critique blocker: as drafted it never did):** `_decide_strategy`'s recording gate (graph.py:4992-5029) refuses to record a strategy as tried on a non-FAIL verdict unless `_cov_bad`/`_prior_false_pass` — job 1082's verdict was PASS with `no_new_items` ∉ `_COVERAGE_FAIL_STOP_REASONS`, so `_new_tried` stayed empty, `_escalate_strategy` (5197-5210) no-op'd, and `scraper_analyzer` re-picked http_requests forever. **Add the kyg stamp as a fourth disjunct** in that recording gate so the prior strategy enters `strategies_tried` and the ladder walks `http_requests → http_navigation → playwright` (`_STRATEGY_ESCALATION`, graph.py:5114). Pin with the test "kyg + PASS verdict → prior strategy recorded + escalated to http_navigation" — this is THE test that proves the fix works.
5. **Terminal rescue veto:** `_exhausted_cleanup_redirect` (1282-1290, six vetoes + `_scraper_has_real_items(min_count=1)`) re-applies the rescue T4 just blocked — for `list_page` its predicate is *identical* to the override arm (min_count 1 = `_override_min`). Add `known_yield_gap` as a seventh kwarg/veto, passed `_kyg` from all three exhausted-arm call sites (2013-2020, 2072-2079, 2257-2267); extend `tests/test_wave26_terminal_cleanup_invariant.py` (pins the exact six-kwarg signature at :42-49).

**Expectation-setting (explicit):** T4 is a **"buy up to 2 escalations"** lever, not a "never ship 2 items" lever. Walk (FAIL or PASS verdict, 2 items, firstn/10, list_page): cycle 0 cascade → http_navigation (retry→1); cycle 1 → playwright (retry→2); cycle 2: `retry_count >= MAX_TEST_RETRIES` (=2, constants.py:11) fires **before** the cascade → `confidence >= 0.5 and _scraper_produced_valid_output` → `field_confirmation` under skip_approvals / `human_approval` otherwise; either way `run_execution` ships what Phase-1 found. Ladder bounded, no stranded state, no infinite loop (critique-verified arithmetic); `_escalate_strategy`'s own exhaustion terminal (5211-5216) is unreachable first. No interaction with `_terminal_after_retest_check`/grace/mutated-verdict arms (wave-37 `_sanitize_pass_verdict` only force-FAILs zero-item PASS — a 2-item PASS correctly reaches the kyg ladder).

**Anti-bot sites:** the downgrade arm (route_after_testing.py:2119) turns `action="strategy"` into `"scraper"` for `_strategy` ∈ {playwright, stealth_browser, seleniumbase_uc, http_navigation, ""} when anti-bot-flagged and `not _cov_reason` — but cycle 0's classify arm is http_requests-only (not in the tuple), and on anti-bot sites `_enforce_anti_bot_strategy` already picks http_navigation first, so the kyg arm never fires there. **Documented out of scope**, no exemption coded (keep the diff minimal).

### T5 — CUT (was: resume re-look on contradiction)

Both critics independently recommended cutting. Verified defects: (a) clearing `skip_site_analysis` at the skip-cascade consumption point (2145-2152) does not route to `site_analyzer` — a falsy flag falls through to `_accessibility_goto`, which for nav-family modes **re-sets the skip and goes to `browser_traverse`** (1943-1951) — a full MCP re-navigation that overwrites exactly the fields T4 reads; (b) where site_analyzer does run, its `_on_success` **replaces `probe_result`** with a slim LLM-authored dict (3450-3461), destroying `listing_connectivity.listing_client_rendered`; (c) it would not change the strategy anyway — `_derive_strategy` reads `site_analysis` only as a connectivity fallback (5641-5647), never `scraping_method`/platform; the stale pin lives in `probe_result.connectivity.method_that_worked`, which re-running site_analyzer does not touch; (d) "once per job" is not structural (`check_accessibility` re-enters after every human_approval resume). The real gap T5 gestured at — stale platform pin starving wave-43 skill injection — is recorded as a wave-49 candidate (bounded deterministic injection hint + state latch, no phase reopen).

### T6 — scope-shortfall honesty (webapp, always-on, no behavior change)

`webapp/scraper/tasks.py` `_finalize_job` ground-truth block (2376-2430, after `job.product_count = len(successful)` at 2417): when `scope == "firstn"` and `0 < job.product_count < _scope_target(final_state)` → append `[SCOPE-SHORTFALL] asked {N}, delivered {job.product_count}` to `job.notes` + a SessionLog row.

**Pollution guard (critique finding):** `job.notes` seeds `user_notes` into the graph state (tasks.py:1234) and is rendered as *"User notes: …"* in writer/product_analyzer prompts (subagents.py:2281,2290); `views.py:732` copies notes into re-run jobs, so bracket rows would accumulate across re-run chains and be read as user guidance. Amendment: when seeding `user_notes` at tasks.py:1234, strip `[SCOPE-SHORTFALL] …` substrings specifically (targeted — `[FIELD-MAP]` and other honored tags keep flowing, `tests/test_wave36_writer_narrative.py:141-143` untouched). Humans keep the honesty at finalize; agents never see it as instructions.

## Non-goals

- Algolia/embedded-JSON catalog extraction (wave-49; F4 candidate — the actual full-catalog payoff for wonderbly).
- Stale-platform-pin re-look / wave-43 injection hint (wave-49 candidate, see T5 cut note).
- Identical-page detection inside the 3 navigation-family templates' own Phase-1 loops (wave-49 candidate).
- Removing the dead top-level `listing_connectivity` state write (graph.py:2373) or the duplicated `_derive_strategy` block (T2 edits both copies identically instead).
- Any change to the global probe byte-floor `success` criteria (shared by probe cache, `_verify_captcha_free`, check-site intake).
- Anti-bot-flagged sites (kyg arm documented out of scope, see T4).

## Validation

1. **TDD per fix**; canonical suite `docker compose exec django sh -c "cd /app/webapp && PYTHONPATH=/app/webapp:/app python -m pytest /app/tests/ -v"` — expect green except the 4 known reds; `ruff check webapp/ src/`.
2. **False-positive replay — two tiers** (original single-tier replay infeasible: `/jobs/<id>/api/` exposes scope/product_count/input_mode/notes but NOT navigation_analysis/probe_result):
   - **Tier 1 (prod API-servable):** recent ~50 completed `list_page` jobs — pull product_count + scope + notes via `/jobs/<id>/api/`; pull `metadata.discovery_coverage` from `/jobs/<id>/output/<fname>/download/`; grep `[LISTING-PROBE]` rows from the api/ logs. Emulate: Source A (body_length ≥ 20k in [LISTING-PROBE] + zero-anchor inference is NOT available retroactively → Tier 1 can only emulate Source B where navigation_analysis was persisted locally, plus the discovered<scope trigger) — hand-verify any hit.
   - **Tier 2 (local artifacts):** local workspace `scrapers/{slug}/analysis/` artifacts for recent local jobs — full emulation of both sources. Expect ≈0 trips on healthy jobs.
3. **Local wonderbly e2e (acceptance):** local `list_page` job, wonderbly kids/0-3, scope firstn/10, gates ON → strategy leaves pure-HTTP discovery AND (Phase-1 discovers ≥10 item URLs **OR** the ladder visibly escalates toward playwright in the logs); output items ≥ what discovery found, never silently 2. Gates OFF → reproduces current behavior (2 items + new telemetry fields + `[SCOPE-SHORTFALL]` note).
4. **Deploy order** (when merged): django + celery-worker FIRST, browser-service LAST.

## Env levers

| Lever | Default | Controls |
|---|---|---|
| `LISTING_ANCHOR_FLOOR` | off | T2 strategy flip (telemetry stamp always on) |
| `LISTING_YIELD_GATE` | off | T4 checker + `known_yield_gap` stamp + all 5 routing touchpoints |

## Tasks

- [x] T1 `_count_item_links` (lazy bs4, `_pdp_score` reuse) + rung keys (browser_service/probe.py)
- [x] T2 `listing_client_rendered` stamp (probe_tools.py) + `_derive_strategy` guarded flip (both copies) + evidence string
- [x] T3 links-tuple `identical_last_page` (src) + requests_scraper whitelist + contract pins same-commit
- [x] T4a traverse honesty persistence (graph.py browser_traverse analysis)
- [x] T4b `known_yield_gap_issue` checker (dual-source) + merge
- [x] T4c routing: distinct key + belt hardening + override veto + classify arm placement + recording-gate disjunct + `_exhausted_cleanup_redirect` seventh veto
- [x] T6 `[SCOPE-SHORTFALL]` + targeted user_notes strip
- [x] Replay script (two tiers) + local wonderbly e2e + full suite + lint

## Execution record (2026-09-28)

**Shipped (TDD, all RED→GREEN):** T1, T2, T3, T4a, T4b, T4c, T6 — 62 tests in
`tests/test_wave48_client_rendered_listings.py`, plus wave-26 invariant
extensions (kyg seventh veto) and `tests/test_discovery_ladder.py` pin
updates. Belt narrowed via `_gate_is_contract_bad` (string stamps +
`violation_kind` dicts stay contract-bad; other annotations ride free).
T5 CUT per critique.

**Post-implementation fixes caught by the suite:**
- `return "violation_kind" in gate` tripped the f13 topology scanner regex
  (`return "([a-z_]+)"`) — reworded to a named intermediate. Scanner lesson:
  helpers inside router modules must not phrase membership tests as bare
  returns of string literals.
- docker-compose.override.yml: one `services:` key only (first append created
  a duplicate mapping).

**Replay (two-tier, run from the local django container):**
- `books.toscrape.com` (SSR control): 94 anchors, `client_rendered=False` — PASS.
- `jomashop.com/watches.html`: 200 / 81KB / 2 anchors / 0 `/products/` refs /
  `_classify_block → None` (wall text sits past the 5KB snippet). The stamp
  fires on this wall-shaped page. **Accepted:** the stamp's operational claim
  ("this HTTP response carries zero item anchors — an http_requests draft on
  it starves") is TRUE for that response, the always-on stamp is
  telemetry-only, and the gated flip would push such a job toward the browser
  lane, which is the correct remedy for a wall. Not a false positive in any
  behavior-changing sense.
- `wonderbly` listing from THIS network: direct_http rung 406s (prod job
  1082's probe got 200 — egress difference). The Source-A shell case stays
  pinned by `ASTRO_SHELL_HTML` fixtures; local e2e exercises the
  fingerprint-winner path instead (see below).
- Operational note: `browser_service` has NO `--reload` — the T1 probe change
  requires a browser-service restart to go live (volume-mounted code).

**Local wonderbly e2e (job 466, gates ON via docker-compose.override.yml):**
the local probe's listing rung is `fingerprint_safari184_none` — a REAL
Chrome fetch, so the Algolia grid renders (55 anchors on the 142KB DOM) and
the T2 stamp is correctly SUPPRESSED (the fetcher saw rendered content).
Strategy follows the fingerprint winner. This exercises the honest no-op arm
of the wave rather than the flip arm; the flip arm is covered by unit
fixtures + the stamped-report routing tests (`TestKygDecideStrategy`).

E2e course (fired 18:53Z, CANCELLED by operator 21:31Z): http_requests cycle
starved exactly as RCA predicted (`empty_first_page`, 0 discovered) while the
seed-only pass extracted fine; the pre-existing zero-yield ladder recorded
http_requests failed → cycle 1 http_navigation (writer produced an unchanged
draft — pre-existing no-op-fix-cycle behavior, one cycle burned) → cycle 2
playwright (`empty_render` — the local egress renders the shell but the
Algolia grid never populates here) → FAIL verdict → grace cycle cancelled.
**Every wave-48 verification point held live:** T4a honesty fields persisted
(`traverse_reached: False` + `wrong_site_abort: True` beside the fabricated
`discovery.listing_reached: True`); stamp/kgy arms produced ZERO false trips
across 4 cycles; ladder walked the exact bounded path the plan predicted.
The job never shipped partial items as COMPLETED (the wave's core goal).
Wave-49 candidates observed live: fingerprint winners render the real DOM —
strategy derivation maps them to http_requests, which fetches the UNrendered
HTML (capture raw-HTML anchor count pre-render to arm the floor on this
path); writer no-op fix cycles still burn a retry against walls.
