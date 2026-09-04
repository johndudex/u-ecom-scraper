# Wave 17 — RCA & Fix Plan: jobs 287/289/290/291 (2026-09-03 campaign failures)

Method: 8-agent workflow — 4 per-job RCA agents + 1 cross-cutting agent, then 3 adversarial
critics (evidence audit, devil's advocate, fix review). Every load-bearing citation was
re-verified against source and logs before inclusion. Evidence pack: `/tmp/rca/` (full
session logs `logs_<id>.json` with seq citations, tool-call traces, `analysis/` artifacts,
zero-item outputs). Standing constraints honored: generic fixes only; never budget/iteration
caps — only more retry/fallback/verification capability; tests at end; django+celery image
ships BEFORE browser-service image.

## Per-job verified RCA

### 287 nastygal — the AWS WAF token paradox (pipeline bug, 3 layers deep)
- Site = AWS WAF + CloudFront. Plain HTTP PDP fetch returns a **complete 855KB page with a
  fully-populated JSON-LD Product node AND the `awswaf` challenge token injected** (writer's
  own live probe, logs_287 seq 356/357). Browser egress is the BLOCKED leg (923B CloudFront
  ERROR page).
- **Actual root cause**: the generated draft's own pre-extraction guard —
  `if "awswaf" in html[:20000]: return dropped item` — discarded **100% of good items**.
  The writer *proved* this (seq 357 "Breakthrough: the recipe works perfectly right now…
  the regression is inside the draft's own…") and was killed by the LLM iteration cap in
  the same batch ("Sorry, need more steps", seq 384) — zero edits landed.
- Layer 2: `src/http_fetch.py` `CHALLENGE_MARKERS` (:110-122) has **no awswaf/CloudFront
  token** → the shared ladder classified WAF 200s as clean → `anti_bot=false` in
  scraper_analysis. The two block detectors disagree in opposite directions on the same
  page: shared ladder under-fires (accepts WAF as clean), draft guard over-fires (discards
  good pages).
- Layer 3: listing (`/categories/holiday`) serves a 200 challenge over HTTP (283 anchors,
  zero `/product/` links on every tier incl. curl_cffi) and CloudFront-rejects the browser —
  phase-1 discovery never worked on any egress. The tester never exercised phase 1
  (`--sample` runs log `stop_reason: skipped (url_list_mode)`), yet the ground-truth
  override (`route_after_testing.py:1448`, `_override_min = 1` for `list_page`) routed the
  job to execution on sample-run items with a `ready_for_execution: false` verdict on file.
- Writer cycle 3 made zero edits (25 read_file offset probes on a 95,337-char draft, then
  iteration death). Cycle-2 feedback was aimed at the wrong layer (parse regression
  diagnosis; correct access diagnosis only appeared in cycle 3) — `classify_test_failure`
  cannot tell a phase-1 zero from a phase-2 zero (`route_after_testing.py:233` counts a
  whole-run total).

### 289 crocs — the listing was only ever attacked over HTTP; the browser rung was NEVER tried, and phase 2 is PROVEN
- Probe/strategy measured **the sample PDP only** (`graph.py:1713` probes
  `product_url`; the listing URL in `search_criteria` is never probed). PDP: direct_http OK,
  `anti_bot=false`. Listing `/c/kids/footwear`: **429 (none), 429 (datacenter), 403
  (residential), 403 (fingerprint)** — Kasada-tier over HTTP.
- **The decisive fact: the generated draft is the pure-HTTP template** (logs_289 seq 78,
  "HTTP Requests Scraper Template — requests + BeautifulSoup") under an `http_navigation`
  analysis. **No /navigate, no cloak, no browser_traverse ever fetched `/c/kids/footwear`
  in the entire job** — every listing attempt was an HTTP-tier attempt. The browser rung
  (CloakBrowser, first-class via `/navigate?stealth=cloak`, proven healthy that same day by
  8 other jobs) was available and unused. Kasada-class edges are routinely cleared by real
  stealth browsers; on crocs that path is **untested, not disproven**.
- **Phase 2 extraction is PROVEN**: tester cycle 2 field coverage is 100% CORRECT/excellent
  on title/price/currency/availability/url for the seeds that returned good PDPs (seq 192;
  2/5 — the failures include the known-bad `206761` SFCC error URL). The formal run
  extracted 0 only because discovery found 0 URLs to feed it. Crocs is **one access
  problem, not two**.
- **Evidence-integrity blocker**: `navigation_analysis.json` for this job says
  `listing_reached: true`, `pagination: load_more`, `rendering_verified: "browser"` — all
  **fabricated by the fallback branch** (`graph.py:2759-2763` hardcodes
  `listing_reached=True` + load_more when traversal fails; `:2811` hardcodes
  `rendering_verified="browser"`). The real traverse recorded `reached=False,
  mechanism=unknown`. Two RCAs initially cited these fields as measured evidence.
- The "11 archived products" are **not** proof of working discovery: cleanup
  unconditionally copies every `workspace/{slug}/output_*.json` on FAILED jobs
  (`.opencode/agents/cleanup.md` "Copy any output_*.json") — 10 intermediate writer
  self-test / tester `--sample` outputs over 2 unique PDPs (5+2+2+2 items) summed into
  PRODUCT_COUNT 11 (seq 215). The tracker now believes crocs works.
- Template/strategy mismatch shipped unflagged: `http_requests` draft under
  `http_navigation` analysis; the zero-item strategy-switch route
  (`route_after_testing.py:233`) did not fire for this shape.

### 290 sephora.de — two independent defects; the writer died before fixing either
- Defect 1 (discovery): `/navigate` returns 200 on `/shop/parfum-c301/` but 0 `/p/` links
  (~3s/page, `stop_reason=empty_render`). **Not discriminable with current logging** — no
  `blocked_type`/`html_len`/anchor-count is logged, and the analyzer's MCP navigator was
  equally blind (`url_examples=[]`, fallback) — possibly no fleet egress renders this grid.
- Defect 2 (mapping): tester HIGH — items carry title/availability but **price is absent
  entirely**. `product_analyzer` never got a rendered PDP (2.7KB challenge shell), honestly
  emitted `tested: false` candidate selectors — and **no node gates an unverified core-field
  mapping before code generation** (`normalize_fields`/`validate_coverage` check the schema,
  not mapping verification).
- The writer terminated on the iteration cap ("Sorry, need more steps", seq 404) with the
  mapping fix unauthored. Fixing discovery alone would NOT have passed this job.

### 291 balenciaga — the working recipe was measured and recorded, then not used; one knob away
- **The probe PROVED the recipe** (seq 7/27): cloak browser, HTTP 200, **656,039 chars,
  anti-bot False, JSON-LD `@type=Product` containing every user-requested field including
  `offers` (price/currency — the draft even set `CURRENCY = "EUR"` from the rendered page,
  seq 87)**. Direct HTTP is 403 — the product_analyzer said so explicitly and ruled
  `http_requests` out (seq 30/32, "Mechanism: playwright"). Geo is NOT the problem on the
  cloak rung: the en_ZW/no-price story came from datacenter-tier fetches and is demoted to
  secondary.
- **Discovery is PROVEN at scale**: tester cycle 2 phase 1 PASS — 50 URLs, clean
  `max_pages_hit` stop (seq 319/321); the writer's own discovery run found **1060 URLs**
  via cloak (seq 221, `stealth: "cloak"`, 175.6s).
- **Extraction failed on fetch timing, not mapping**: cycle 1 extracted 0 from the
  analyzer-verified PDP in **3.9s** (seq 131) — a page the probe needed ~14s to hydrate.
  `/navigate` settles with a **flat 1.5s** `wait_for_timeout` after
  `domcontentloaded` (`browser_service/server.py:2346`) before extracting — snapshots are
  taken pre-hydration. Cycle 2 compounded it: 4 **concurrent** cloak navigations, items
  completing ~1s apart vs item 1's 14s (writer's own diagnosis, seq 275: "4 concurrent
  cloak browsers ran, while the successful probe was sequential" → "hydrated-but-empty
  fetches").
- **The writer built the exact confirming experiment and was killed mid-flight**:
  `probe_hydration.py` — offline `extract_jsonld` against the proven snapshot + a live
  sequential fetch with longer settle (seq 276) — then "Sorry, need more steps" (seq 308)
  ended the cycle. The diagnosis and fix were one tool-call away.
- Separate confirmed defect: cycle-3 seeds were polluted (`browser_traverse` no item_links
  → first-20-anchors fallback `graph.py:2662-2672` = brand pages; count-only preserve
  `:4388-4408` let them displace the verified set; tester sampled 5 non-product pages →
  filter `5 → 0`), and the no-op gate's `MAX_TEST_RETRIES` arm read **pre-increment**
  `test_retry_count` (`graph.py:4763`) and consumed the final retry on a byte-identical
  draft (`last_tested_draft_fp` :5329 computed, never consumed).
- Strategy-record corruption: `scraper_analysis.json` has `method_that_worked: null` and
  `proxy_tier: "none"` **despite the probe recording cloak_none as the working method** —
  `_derive_strategy` drops the measured rung. The http_navigation template happened to be
  browser-backed (/navigate per page) so the site got *a* browser, but with blind 1.5s
  settle + pool concurrency, and the recorded artifact is evidence-free for downstream
  tuning.

## Systemic defects, ranked (owner file:line verified)

| # | Defect | Jobs | Owner |
|---|--------|------|-------|
| S1 | **Writer non-delivery + stale draft tested anyway**: iteration-cap deaths ("Sorry, need more steps") in 3/4 jobs; read_file offset thrash on 28-100KB drafts; `graph.py:4637` declares a stale draft "usable" → tester re-runs byte-identical code → information-free cycle | 4/4 | `subagents.py` (iteration death, prompt channel), `filesystem_tools.py:235-284` (read contract), `graph.py:4637/:4763/:5329` |
| S2 | **Per-item diagnostics destroyed by the output filter** (`title AND (price OR availability)` drops pre-extraction, no drop_reason) — every "all items empty" claim in this campaign was unfalsifiable | 4/4 | `graph.py:480 _patch_scraper_output_filter`, `templates/http_navigation_scraper.py:218,:1526-1534` |
| S3 | **Block-marker classifier fails in both directions**: shared ladder misses `awswaf` (accepts WAF as clean → `anti_bot=false`); generated drafts over-fire on first-20KB markers (discard good pages) | 287, risk to all WAF sites | `src/http_fetch.py:110-122`, writer guard instructions |
| S4 | **Listing URL never probed**: connectivity proven on the PDP; strategy+proxy derived from a measurement that says nothing about the endpoint discovery needs | 289 (fatal), 290 | `graph.py:1713`, `_derive_strategy` |
| S5 | **navigation_analysis fabrication on fallback**: `listing_reached=True`, `pagination=load_more`, `rendering_verified="browser"` hardcoded when traversal failed — ladders and RCAs tune on synthetic evidence | 289, 290 | `graph.py:2662-2672, :2759-2763, :2811` |
| S6 | **Seed contract pollution + count-only preserve guard**: first-20-anchors fallback; longer-set-wins even when worse | 291 (fatal), 287-adjacent | `graph.py:2662-2672, :4360, :4388-4408`, `src/seed_urls.py` |
| S7 | **Ground-truth override admits sample-run items** (`_override_min=1` for list_page; `_zero_yield_stop` relaxed floor `:436-447`) — routes `ready_for_execution:false` drafts to execution | 287 | `route_after_testing.py:1436-1460, :436-447` |
| S8 | **Cleanup archives intermediate outputs on FAILED jobs and sums their items into PRODUCT_COUNT** — tracker believes failed sites work | 289 | `.opencode/agents/cleanup.md`, cleanup node |
| S9 | **No phase attribution in test-failure classification** → remediation aimed at the wrong layer (287 cycle-2) | 287, 290 | `route_after_testing.py:233` |
| S10 | **Unverified core-field mapping reaches codegen** (`tested:false` selectors implemented verbatim) | 290 | `normalize_fields`/`validate_coverage`/`field_confirmation` |
| S11 | **shell_tools honesty-guard margin** skips viable runs with a self-contradictory message ("needs ~600s but only ~607s remain") | 287 | `shell_tools.py:315-324` |
| S12 | **Writer system prompt is an unbounded, untrimmable channel** (`template_code` appended verbatim, outside the `messages` the `pre_model_hook` trims — `subagents.py:956-966, :1017`) | 4/4 (confound) | `subagents.py` |
| S13 | Concurrency reputation-burn: drafts run 4-worker pools over ONE shared `requests.Session`; the one sequential fetch 60s later succeeded | 287 (suspected) | `src/http_fetch.py:196-226` |
| S14 | **Measured rung dropped from the strategy record**: probe proved `cloak_none` (656KB, full JSON-LD) but `scraper_analysis.method_that_worked = null`, `proxy_tier = "none"` — `_derive_strategy` discards `probe_result`'s method evidence, so strategy/template/tier decisions and every downstream reader are evidence-free | 291 (fatal), 290 | `graph.py` `_derive_strategy` / `scraper_analyzer` outputs |
| S15 | **Pre-hydration snapshots: flat 1.5s settle, no render-completeness gate** — `/navigate` extracts after a fixed `wait_for_timeout(1500)` post-domcontentloaded regardless of whether core fields rendered; a 14s-hydration SPA snapshots empty, and nothing anywhere retries on content-absence in phase 2 | 291 (fatal), 290-adjacent | `browser_service/server.py:2346`, templates' phase-2 fetch |
| S16 | **No browser-rung escalation for a blocked listing + template/strategy mismatch unflagged**: all-HTTP-tiers-429/403 ends the story (`navigate_error`) without ever pointing the browser at the listing; an `http_requests` draft shipped under an `http_navigation` analysis with no gate | 289 (fatal) | `route_after_testing.py:233`, strategy→template selection, draft seeds |

Honest external boundary (narrowed — the campaign's real unknowns, not a verdict): the two
sites the user called winnable **are winnable on the evidence** — crocs' browser rung is
untested (never tried), balenciaga's recipe is measured-and-proven (one settle/concurrency
knob). What remains genuinely unknown: whether cloaked egress renders sephora.de's grid
(may require consent-wall handling — a generic mechanism worth building — or may be
renderer-hostile), and whether Kasada yields to CloakBrowser on crocs (untested; cookie
handoff is the fallback). "Untested" is not "unwinnable"; the plan now treats these as the
primary targets, not a boundary paragraph.

## Critique verdicts that changed the plan

- **REJECTED** (was proposed by the 287 RCA): force phase 2 through `/navigate` because
  `strategy=playwright`. Inverted — the browser is the blocked leg for this site; plain HTTP
  is the working one. Also rejected as-shape: a deterministic transport scanner that bans
  the (validated!) phase1-browser/phase2-HTTP hybrid.
- **REJECTED**: add `awswaf` to `CHALLENGE_MARKERS` as a hard block signal — the token
  coexists with valid Product JSON-LD on the same 200 page; marker-based rejection would
  discard good items. Correct shape: extract first; marker + zero-core-fields ⇒ escalate to
  MORE capability.
- **REJECTED**: "tighten the no-op threshold to 1" — cap-shaped, and the real defect is the
  dead pre-increment arm, not the threshold.
- **CORRECTED**: crocs `listing_reached=true`/`rendering_verified=browser` are synthetic;
  crocs' 11 products are an archival artifact; 290 has a second untreated defect (price
  mapping); 291's "no-op writer decided not to change" was an iteration-cap interruption.

## Fix wave

### PR-A — The win paths (crocs / balenciaga-class; ships first)
1. **Honor the measured rung (S14)**: `_derive_strategy` must consume
   `probe_result.method_that_worked` and write it into `scraper_analysis` (it writes
   `null` today). Constraint, not suggestion: the strategy's per-phase transport must be a
   rung that measured 200-with-content, or carry an explicit `transport_exception`
   justification. 291's probe already said "cloak"; 289's said "direct_http for PDP" —
   with PR-A.3 the listing measurement flips crocs to a browser-discovery strategy.
2. **Render-completeness gate + real settle (S15)**: replace the flat 1.5s
   `wait_for_timeout` with content-aware settling — after the base settle, check
   core-field presence (JSON-LD Product/offers or the mapped primary selector); if absent,
   escalate: wait again (bounded backoff, e.g. 1.5s → 4s → 8s) and re-snapshot before
   returning. Expose `settle_ms` / `wait_for` on `/navigate` so drafts and tester probes
   can request it. This alone converts 291's "hydrated-but-empty" snapshots into the
   probe's proven 656KB result. Extends the pillowtalk F-B empty-render retry from
   discovery to phase-2 extraction.
3. **Browser-rung escalation for blocked discovery (S16)**: when phase-1 discovery is
   blocked on all HTTP tiers (429/403/challenge on the listing), the strategy ladder
   escalates to the **browser rung** — `/navigate?stealth=cloak` fetch + link extraction,
   or the browser navigation template — before declaring `navigate_error`. Requires the
   advisory listing probe (below) to know the listing is the blocked endpoint; fixes the
   "http_requests draft under http_navigation analysis" mismatch at the same time (the
   strategy's transport contract from PR-A.1 is what the writer template must implement;
   mismatch = writer seed warning + tester flag, not a ban).
4. **Advisory listing probe (S4)**: when `input_mode` is navigation/list_page/search_term
   and `search_criteria` is a same-host URL, probe it as
   `probe_result.listing_connectivity` (advisory, never job-ending). With PR-A.1's
   constraint this is what routes crocs to browser discovery while the PDP stays cheap
   HTTP.
5. **Kasada-class cookie handoff (capability, generic)**: after any successful
   browser-rung fetch of a host, persist that context's cookies (same pattern as
   `data/akamai-cookies/`) and let the HTTP phase replay them — phase 2 at requests speed
   behind a browser-won session. Applies to any edge that gates HTTP but admits a real
   browser.
6. **Phase-2 concurrency discipline for browser-rung fetches**: browser-backed extraction
   defaults to sequential-first (workers 1→2 only after a proven-good page), matching the
   probe recipe that worked and removing the 4-concurrent hydration starvation. (S13's
   browser analog; the `requests.Session` fix stays in PR-C.)

### PR-B — Test integrity & diagnostics (makes every future failure one-cycle diagnosable)
1. **Per-item drop diagnostics** via `_patch_scraper_output_filter`: dropped items pass
   through with `drop_reason` (status_code, body bytes, content fingerprint, marker hits,
   phase, **post-fetch wait tried**) + `metadata.drops` aggregate; payload-capped (first
   50 records, aggregate the rest) — capping the DIAGNOSTIC PAYLOAD, never the extraction
   work. (S2)
2. **Phase attribution**: record which phase zeroed (discovery vs extraction) in
   `classify_test_failure` and in the remediation directive. (S9)
3. **Honest navigation_analysis**: fallback writes `listing_reached: false`,
   `pagination: null`, `fallback_reason: <why>`; stop hardcoding `rendering_verified`.
   Downstream consumers audited for the changed fields. (S5)
4. **/navigate observation parity**: log `blocked_type`, `html_len`,
   anchor/product-link count per fetch + the settle escalations tried — discriminates
   empty-shell vs soft-block vs selector-miss. (290 evidence gap)
5. **Cleanup archival honesty**: on non-SUCCESS jobs file intermediate outputs under
   `scrapers/{slug}/failed_runs/`, never sum them into a product count; only outputs from
   the FINAL `run_execution` count. Enforce in the cleanup node, not just cleanup.md. (S8)
6. **shell_tools margin fix**: honest arithmetic + message (S11).

### PR-C — Writer delivery & remediation convergence
1. **Iteration-death continuation**: detect the iteration-cap termination and resume the
   writer with a deterministic post-mortem injection (the interrupted window's findings —
   287's seq 356/357 breakthrough, 291's seq 275 concurrency diagnosis + the already-
   written `probe_hydration.py` — handed to the next invocation). MORE capability, not a
   cap change. This alone likely converts 287 and 291.
2. **read_file contract repair** (`filesystem_tools.py:235-284`): include total char count
   in every page response; normalize line-number-shaped offsets — kills the offset thrash.
3. **No stale-draft testing**: when `graph.py:4637` fires with draft fingerprint ==
   `last_tested_draft_fp` (consume `:5329`), do NOT re-test byte-identical code — route the
   cycle to a *different* verification (targeted-edit writer seeded with the tester
   directive + exact byte range, diversified seeds, browser-rung spot-check). Fix the
   pre-increment read at `:4763`. (S1)
4. **Seed-preserve quality guard**: replace count-only preserve (`:4388-4408`) with
   shape-quality-aware preserve; order seeds (analyzer-verified sample PDP first,
   shape-matching next); fix the fallback substitution itself (`:2662-2672`) to prefer
   anchors matching the sample PDP shape — down-rank, never drop. (S6)
5. **Bound the writer's untrimmable prompt channel** (S12): cap the `template_code` block
   in the system prompt (a size control on the PROMPT, not on agent work).

### PR-D — Classifier & capability hygiene
1. **Classifier direction fix** (S3): stage `SCRAPER_SOFT_BLOCK_MIN_BYTES` (documented
   activation path, env-only, no code); make markers a tiebreaker that routes to MORE
   capability (next tier / browser rung) only when extraction yields zero core fields —
   never a pre-extraction discard.
2. **Sequential-first fetch** (S13): per-thread Sessions (or sequential-first with pool
   fallback) in `src/http_fetch.py`.
3. **Geo-pin capability**: expose country pinning for browser rungs when scraper_analysis
   flags geo-risk (datacenter egress geo-mismatch, 291-secondary).
4. **Consent-wall handling**: generic cookie-consent detection + accept action in
   browser traversal (OneTrust/Usercentrics-class) — the concrete remaining hypothesis
   for sephora.de's empty grid.

### Verification gate (before any deploy)
- Replay the 8 passing sites' artifacts/logs through the PR-A/PR-B logic — the campaign's
  passing sites were argued from mechanism only, not evidence.
- Live canary for the win paths: re-drive balenciaga (recipe proven; PR-A.2 is the fix) and
  crocs (browser-rung escalation + cookie handoff; PR-A.3/A.5) BEFORE the diagnostic PRs
  would otherwise gate them.
- New tests (written at end per convention): drop-diagnostic payload; phase attribution;
  fallback honesty fields; failed_runs filing; read_file total-count contract; stale-draft
  diversion; seed-preserve quality guard; pre-increment arm; listing probe advisory shape;
  settle-escalation behavior; method_that_worked propagation; cookie-handoff replay.
- Deploy order: django+celery image BEFORE browser-service image (PR-A.2 touches both —
  settle gate in browser-service, gate consumers in django/celery).

## Open questions (carried, not blocked)
- Does CloakBrowser clear crocs' Kasada listing? Untested — PR-A.3 answers it in one
  probe; PR-A.5 is the fallback if the edge admits the browser but re-gates raw HTTP.
- Does ANY fleet egress render sephora.de's grid — and is it consent-gated? (PR-B.4 makes
  the answer visible; PR-D.4 is the generic consent mechanism.)
- balenciaga hydration: confirmed settle hypothesis (3.9s vs 14s) — does the settle
  escalation fully close it, or is there a post-hydration DOM mutation the extractor
  misses? PR-B.1's drop diagnostics make the next attempt self-answering.
- Is 1800s `AGENT_INVOKE_TIMEOUT` the binding constraint or the read/edit contract? 287's
  read-only window still exhausted — points at the contract (PR-C.2), but confirm from a
  per-window tool/time breakdown post-fix.

---

## PR-A e2e addendum (2026-09-04, local) — S21/S22 (drives 4-6)

The crocs half of the local e2e proved five more generic defects past the original
13. Each was found by a live drive, fixed generically, and locked by test:

| # | Defect | Fix | Drive |
|---|--------|-----|-------|
| S17 | One `access_recipe.proxy_tier` for two URL classes (listing residential + PDP datacenter → phase 2 challenged forever) | recipe carries `proxy_tier` (PDP) + `discovery_proxy_tier` (listing); S4 raise removed | 325→326 |
| S18 | Template launched all 3 discovery browsers with NO proxy + truncated bot UA | `_DISCOVERY_PROXY` + full UA at all launch sites | 325→326 |
| S20 | `src/discovery.py` hard-failed on 429/5xx while crocs' listing serves the full catalogue behind a 429 | `_status_page_usable` bounded poll → content over status | 325→326 |
| S21 | Template stamped hardcoded Chrome/120 onto CloakBrowser (Chromium **146**) launches — probe's winning rung stamps NO UA; fingerprint/UA mismatch flipped 429-with-content → held challenge, PDPs rendered hollow | `_LAUNCH_UA = None` under `STEALTH_BROWSER=cloak` (binary-native UA), stamp kept for vanilla headless; `_discovery_goto` logs every ≥400 status; usability window floor 10→20s + poll cap | 326→327 |
| S22 | Recipe derived `stealth="none"` from ONE clean PDP snapshot (`anti_bot.detected=false`) while the listing probe was BLOCKED on every rung (403s) and product_analyzer watched the challenge twice | `_derive_strategy` folds per-rung `blocked` into `anti_bot` (plain-404 rungs never count — they set blocked=False) | 327→328 |

Operational rules this campaign wrote into practice:
- **Pre-flight before every drive**: ONE `_try_cloak(listing, tier)` call from the
  browser_service container. The fix-loop itself (tester+writer iterating) hammers the
  listing enough to trip per-IP rate bands (~50 min on crocs); drive-5 (327) was burned
  on a banded edge with no pre-flight.
- **Probe-path code changed ⇒ restart browser_service BEFORE submitting** (stale-evidence
  drives prove nothing — 323/324).
- Drafts are LLM-owned; if a defect is IN the template, cancel and re-drive rather than
  hand-edit the draft mid-loop.

Jobs: 322 balenciaga **COMPLETED 10/10 priced** (hreflang locale escalation). 323/324
(stale evidence), 325 (S17-S20), 326 (S21), 327 (S22) cancelled on RCA. 328 = crocs
drive 6 with the full chain: single identity cloak+datacenter measured live on both
URL classes, S21 native-UA launches, S20 content-over-status patience.

### S23 (drive 6, job 328 FAIL → fixed)

328's writer died honestly (2×900s wall-clock, ballooning class) — but its tester
round 1 surfaced one more genuinely generic template defect independent of the
band: **the draft emitted the Cloudflare interstitial as a successful product** —
title "Just a moment...", price/availability empty, yet `status_code=200`
hardcoded and `failed_products=0`. The interstitial rides in behind HTTP 200, so
no status check can catch it; the main loop's validity check excludes `title`
from substantive fields and the challenge item still carried sku/category from
the interstitial's own JSON, so it passed.

Fix (template, `scrape_product`): after extraction, `_looks_challenged(data)` —
generic managed-challenge title markers ("just a moment", "attention required",
"access denied", "please verify", "request unsuccessful", "unusual traffic", …)
plus a structural arm (no title AND no substantive field). On a hit: one bounded
reload (`CHALLENGE_RETRY_SETTLE_MS` = 10s settle — managed challenges auto-clear
in ~5-15s; retry rides `domcontentloaded` because challenge beacon traffic never
reaches networkidle) → if still challenged, emit an honest failure item: every
data field emptied, `status_code: 403` (block class), remarks naming the
challenge. The main loop's `_BK` validity check then counts it as
`failed_products` — no challenge page is ever a product, and the tester sees
honest failures instead of a green run over an interstitial.

Scope note: S23 landed in `templates/playwright_scraper.py` (the template every
crocs/balenciaga drive used). The sibling browser templates
(`navigation_scraper.py` — seleniumbase UC) have the same hole and are queued for
PR-B; one template per drive kept this round verifiable.

Drive 7 (pending at write time): cool-down ≥60 min from the 03:26 band trip →
browser_service restart (done — S20/S21 discovery + probe code all loaded) →
ONE pre-flight `_try_cloak(listing, 'datacenter')` → submit only on a clean
read. Full suite must be green first.

### PR-B.3 spec sharpened (read-only recon during the drive-7 cool-down, 2026-09-04)

The S5 fabrication is `graph.py` ~2805-2815: the "listing not reached" fallback
branch writes `listing_reached: True` while its own log line says
"listing not reached — falling back". The hardcode exists to keep
`run_execution`'s `--listing-url` chain alive (the gate at `run_execution.py:658`
omits the flag when `listing_reached=False`, and the OMIT arm was written for
"no URL at all", not "user gave us one the probe couldn't verify").

Honest redesign (3-state, no behavior regression):
- Fallback writes `listing_reached: False` + `listing_source: "user_asserted"`
  (list_page) or `"fallback"` (navigation/search_term root) +
  `fallback_reason: "<why>"`. Stop hardcoding `pagination`/`rendering_verified`.
- `run_execution`'s gate becomes: pass `--listing-url` when
  `_listing_reached or not _respect_flag or <the F17-guarded candidate chain is
  non-empty>` — i.e. the OMIT arm fires only when there is genuinely NO
  same-domain listing candidate. The candidate chain already re-derives exactly
  the list (url-shaped search_criteria → job URL → discovery.listing_url →
  search.working_url → listing_url_used), so rmwilliams/job-310/F17 protections
  are untouched.
- Consumer audit (done): `_derive_strategy` reads structural signals + seed
  URLs only — NOT listing_reached. Remaining consumers: graph.py:2772 (branch
  trigger, reads this call's fresh result — safe), graph.py:2825 (log line —
  prints honest value), run_execution.py:658/749 (the gate). Blast radius
  confined; no other modules read the field (grep 2026-09-04).
