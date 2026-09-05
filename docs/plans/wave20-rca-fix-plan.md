# Wave-20 RCA + Fix Plan — jobs 359 (michaelhill) & 360 (marimekko)

**Date:** 2026-09-05 · **Evidence:** prod session logs (509 + 475 rows, `/tmp/j35{9}_logs_full.json`, `/tmp/j36{0}_logs_full.json`), job APIs, tester test reports, `route_after_testing.py`, `src/http_fetch.py`, `webapp/agents/graph.py`.

Both jobs are **honest fails** — the pipeline refused to ship junk. But both died of
**fixable systemic defects**, not site difficulty. Both sites are easy
(no anti-bot, plain HTTP 200, complete JSON-LD): the probe proved it, and in
360's cycle 2 the scraper extracted a perfect PDP (title/price/images in 9.5s).

---

## 1. What actually happened

### Job 359 — michaelhill.com.au (`code_writer produced an identical draft twice`)

| time (UTC) | event |
|---|---|
| 09:39 | Probe: `direct_http` SUCCEEDED, `js_rendering_needed: False`, `anti_bot: False`. Listing probe: 1.15 MB, 200 OK. **Site is trivially accessible.** |
| 09:42 | Navigator reached the listing, found 20 item links. Discovery worked. |
| 09:49 | product-analyzer mapped fields, confirmed **complete Product JSON-LD** (name/sku/offers/aggregateRating), flagged 3 site-analysis corrections. |
| 10:01–10:22 | Test cycle 1: **FAIL** — Phase 2 extracted 0/5 PDPs ("No substantive data extracted"), Phase 1 discovered 0 URLs (`empty_first_page`) despite **341 anchors** in every listing fetch. Router: `[CASCADE] strategy-switch → http_requests, reason=http_requests returned no items (empty)`. |
| 10:41–10:48 | Test cycle 2: **FAIL — byte-identical symptoms**. Tester's own RCA: (a) *parser never executes* — the complete JSON-LD is IN the fetched bodies; (b) *soft-block gate mis-fires* — `captcha`/`_abck` substrings flagged real 1 MB bodies, and "200 but 0 product links" was misread as a block signature → **burned 4 proxy tiers + a real 403 residential ban + 45 s waits on a no-anti-bot site**. Router: `strategy-switch → http_navigation`. |
| 10:48–11:09 | Writer regenerated → **identical draft sha** → wave-17 draft-freeze gate fired → honest FAIL. |

### Job 360 — marimekko.com (`stop_reason=all_tiers_blocked`, honest 0-item refusal)

| time (UTC) | event |
|---|---|
| 09:39 | Probe: direct HTTP fine, listing 575 KB, no anti-bot. `browser_traverse` **FAILED** at 09:40 (fallback path recovered). |
| 09:41–10:35 | product-analyzer OK (JSON-LD Product present). Test cycle 1 hit a 429/captcha wall during testing (see §2-B). Router: `strategy-switch → http_requests`. |
| 10:35–11:05 | Test cycle 2: **Phase 2 SOLVED** (1/1 PDP, perfect fields, 9.5 s). New HIGH bug: production discovery invocation logged **`Phase 1: SKIPPED (url_list mode with 1 seed URLs)`** and returned in **0.0 s** — the draft's own mode gate let the *existence of `input_urls.json`* override the explicit `--fresh-discovery --discover-only --limit 50` flags. Tester: `remediation.target="scraper"`, field `discovery`, "reproduced deterministically". Router instead read `discovery all_tiers_blocked (gave up, not exhausted)` → `strategy-switch → http_navigation`. |
| 11:05–11:14 | Writer regenerated; test cycle 3: **same mode-gate bug, UNFIXED** (tester said so, again with `target="scraper"`). Deterministic discovery probe: dead yield → tier escalation → still dead → `all_tiers_blocked` → zero-yield honest-fail guard refused execution. |

**The bitter irony in 360:** the "all tiers blocked" verdict is false — Phase 1 never
*ran* (0.0 s, self-skipped). The probe escalated network identities against a
**code bug**, then reported the code bug as site blocking.

---

## 2. Root causes

### A (systemic, both jobs): the router ignores `remediation.target="scraper"` on zero-item runs

`webapp/agents/nodes/route_after_testing.py`:

- `_classify` fires the zero-item branches **before** consulting the test report:
  `items == 0 and is_http_like → ("strategy", "{strat} returned no items")` (line ~258).
- The only remediation consultations are (a) the early **mapping** promo — reachable
  only when `items > 0` (line ~293), and (b) a `refine`-only upgrade at line ~1614
  (`if _action == "refine"`). **`remediation.target == "scraper"` is never promoted.**

So when the tester — who ran the draft and read its output — says "the CODE is broken,
here is exactly where", the router overrides the expert diagnosis with a coarse
heuristic, burns a strategy switch, and the writer regenerates a near-identical draft
against the same context. In 359 that ended at the draft-freeze gate; in 360 it ended
at the honest 0-item refusal. The targeted-fix arm (`action="scraper" → code_writer`
with the tester's diagnosis injected) **never fired in either job**.

### B (systemic, live on prod TODAY): `SCRAPER_SOFT_BLOCK_MIN_BYTES=20000` armed generic-substring challenge detection

`src/http_fetch.py:detect_soft_block` short-circuits OFF when `floor <= 0`
(`soft_block_min_bytes()` default `"0"` = prod default until **today**). Setting the
floor to 20000 (wave-19 T0.1, correct for the myhouse tiny-wall class) **also armed**
the `CHALLENGE_MARKERS` scan for the first time — and that list contains bare
substrings **`captcha`, `_abck`, `akamai`** (http_fetch.py:110-122).

SFCC/Akamai-fronted sites embed `_abck` (telemetry cookie name) and "captcha" (help
links) in **every real page**. Result on michaelhill: every 1 MB real body classified
`challenge_marker` → full proxy-ladder burn → a **self-inflicted 403 residential ban**
on a site that needed zero proxies → inflated test cycles (~25 min each) →
"blocked" verdicts feeding the router's strategy switches. Marimekko's cycle-1
"429/CAPTCHA wall" and the tier burns are the same freshly-armed detector.

A 1 MB body with 341 anchors is not a challenge page, whatever substrings it contains.

### C (job 360 primary): writer's draft gates Phase 1 on `input_urls.json` existence

The local templates honor `--fresh-discovery`/`--discover-only` correctly; the string
`"Phase 1: SKIPPED (url_list mode with 1 seed URLs)"` exists in **no template** — the
writer invented that gate while adapting. In navigation/list_page jobs the pipeline
always seeds `input_urls.json` with the sample URL, so the writer's gate guarantees
Phase 1 skips in every navigation-mode run of that draft.

### D (job 359 primary): writer's parser is inert — JSON-LD present but never extracted

Two independent drafts fetched real 1 MB bodies and extracted 0/5. **Verified, not
just the tester's word:** cycle-2 evidence shows all 5 PDPs returned HTTP 200 with
~1,074,548-byte real server-rendered bodies (the verified real page size) and STILL
emitted `products=[]` — so the fetch path was healthy and the parse was the failure.
(A counter-hypothesis — that defect B's soft-block rejection explained Phase 2 too —
is refuted by exactly this evidence: soft-block-rejected fetches never record
"200, real page size, then no data".) Mechanism per tester: the analyzer's
browser-captured `js_extraction` JS expressions embedded verbatim (or parse
exceptions silently swallowed into "No substantive data extracted") instead of being
ported to pure-Python `json.loads` over the
`script[type="application/ld+json"]` blocks the same fetch already returned.
Defect-B damage in 359 is real but confined to Phase 1 (the 4-tier escalation burn
against real listing bodies).

---

## 3. Fix plan (TDD; each task = failing test first)

### T0 — Split challenge markers into STRONG/WEAK (defect B) — **do first, it's live**

`src/http_fetch.py`:

- `CHALLENGE_MARKERS` → two tuples:
  - `CHALLENGE_MARKERS_STRONG` (structural challenge phrases, page-shaped):
    `attention required`, `access denied`, `verify you are human`,
    `verifying you are human`, `checking your browser`, `just a moment`,
    `cf-chl`, `cf_chl`.
  - `CHALLENGE_MARKERS_WEAK` (tokens that appear inside real pages):
    `captcha`, `_abck`, `akamai`.
- `detect_soft_block(text, body_bytes=None)` rule:
  - strong hit → `SoftBlock("challenge_marker", hits, …)` regardless of size;
  - weak hit **only when** `len(text) < floor` (tiny-body corroboration — this keeps
    the wave-19 tiny-wall defense fully intact);
  - big body + only weak tokens → `None` (real page).
- Tests (`tests/test_http_fetch_module.py`): 1 MB body containing `_abck`+`captcha`
  +`akamai` with floor 20000 → `None`; 5 KB body with `_abck` → `SoftBlock`;
  1 MB body with "just a moment" → `SoftBlock`; floor 0 → always `None` (regression).

**Keep the env var at 20000 on Railway.** The floor is not the problem; the unsplit
marker list is.

### T1 — Promote `remediation.target="scraper"` to the targeted-fix arm (defect A)

`webapp/agents/nodes/route_after_testing.py`:

- In `_classify`, before the zero-item strategy branches **and before the
  `_discovery_coverage_failure` return (~line 306)**: if the report's
  `remediation.target == "scraper"` **and** the diagnosis is concrete
  (`remediation.field` or non-empty `issues`), return
  `("scraper", f"tester diagnosis: {field}: {first issue[:70]}")`.
  The coverage-failure placement is not optional: 360's cycle 2 extracted 1 seed
  product (`items=1`), so the zero-item branches were skipped and the verdict came
  from `_discovery_coverage_failure` → `all_tiers_blocked` → strategy-switch. A
  promotion that only guards the zero-item branches would have missed this job.
- Keep every existing guard: selector-crash, absent-draft, 429-retest, throttle,
  navigate_unavailable, transient-render checks stay **ahead** of this (an unproven
  run is still unproven). The mapping promo (job-118) and anti-bot downgrade are
  untouched. Bound: `MAX_TEST_RETRIES` already caps the loop; the strategy switch
  remains the fallback once retries exhaust.
- The `refine`-only upgrade at ~1614 gains no regression (it still fires for refine).
- Tests (new `tests/test_wave20_router_remediation.py`, fixtures shaped like the two
  prod reports): 359-shape (items=0, http_like, remediation target=scraper,
  field=extraction) → `("scraper", …)` not strategy; 360-shape (discovery_coverage
  `ran_phase1=False`, target=scraper, field=discovery) → `("scraper", …)`;
  negative: no remediation → legacy strategy verdict preserved; target=mapping with
  items>0 → mapping (job-118 regression stays green).

### T2 — `ran_phase1=False` is a CODE verdict, not `all_tiers_blocked` (defects A+B2)

`webapp/agents/graph.py` `_probe_phase1_discovery` (~5406-5415): when the draft's own
coverage says `ran_phase1=False` (it skipped itself), stamp
`stop_reason = "phase1_skipped"` instead of `all_tiers_blocked` **and skip the
identity/tier escalation re-run entirely** — escalating network identities against a
draft that self-skipped Phase 1 is the "all_tiers_blocked against a code bug"
absurdity that produced 360's misleading final verdict; the skip verdict must be
decided BEFORE `_state_at_next_tier` is consulted. Keep `feedback_for_writer`
(already names the discovery fix). In `route_after_testing.py`, classify
`phase1_skipped` as the `scraper` action (never a tier verdict, never
strategy-switch). Tests: probe-unit test with a coverage dict
`{"ran_phase1": False, "discovered_urls": 0}` → `phase1_skipped` and **no second
probe run**; router test → scraper action.

### T3 — Writer gotchas (defects C+D), via message-builder contract tests

- code_writer system/message builder (+ `.opencode/agents/code_writer.md` "Learned:"):
  - **Mode precedence (C):** in `navigation|list_page|search_term` jobs, explicit
    discovery flags (`--fresh-discovery`, `--discover-only`, `--query`,
    `SCRAPER_LISTING_URL`) OVERRIDE the existence of `input_urls.json`. Never gate
    Phase 1 on the seed file; url_list mode is for `input_mode=url_list` only.
  - **JSON-LD porting (D):** `extraction_methods.structured_data` may carry JS
    expressions captured from the browser — PORT to pure Python (`json.loads` of
    ld+json tags, walk `@graph`). Never embed JS in the draft; never swallow parse
    exceptions into "no substantive data" — log them.
- Tests: contract tests asserting the gotchas are present in the builder output
  (same pattern as the wave-19 T1.9 gotcha test).

### T4 (optional, small) — compile-gate JS-ism scan

Wave-17 loud compile gate gains one static check: a draft containing browser-isms
(`document.`, `window.`) outside strings/comments → loud error before testing.
Cheap insurance for defect D. Skip if it misfires on legit code during validation.

---

## 4. Validation & rollout

1. Unit suites: `test_http_fetch_module.py`, new wave-20 router/probe tests, writer
   contract tests, plus the wave-13/15/17/19 regression files (router is
   regression-dense: T1.7(a), job-118, uindex, A1/QW-3 arms all live there).
2. Full pytest (target: 1932+ green, ruff baseline unchanged).
3. Ship via fork PR (aimleap main → Railway; django+celery image first per deploy
   rule). No browser-service change required (T0 lives in `src/`, shipped in the
   scraper venv used by browser-service too — sync both images per rsync rules).
4. **Proof = re-drive 359 (michaelhill) + 360 (marimekko)** — they are exactly the
   wave-18 never-succeeded class. Expect: tester diagnosis → targeted writer fix →
   michaelhill completes on JSON-LD (no proxy burn), marimekko completes once the
   mode gate respects the flags.

## 5. Guardrails / non-goals

- Do **not** remove `SCRAPER_SOFT_BLOCK_MIN_BYTES` (tiny-wall defense stands).
- Do **not** loosen the draft-freeze gate — it correctly stopped 359's waste.
- Do **not** change the zero-item strategy default when no remediation exists —
  the "strategy never got a fair window" concern (uindex) is still real; we only
  add "unless the tester, who actually ran it, says the code is the problem".
- Sephora.de stays excluded from the campaign (done, job 320).
