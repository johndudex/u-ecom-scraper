# Wave-21 RCA + Fix Plan: Dead-Seed Poisoning (local e2e jobs 335/336)

Date: 2026-09-05 · Status: PLAN (awaiting green light) · TDD mandatory (RED→GREEN)

## What surfaced this

Local e2e of wave-20 on the two prod-failure sites (michaelhill = prod 359,
marimekko = prod 360) FAILED — jobs 335 (draft-freeze gate) and 336 (testing
cascade exhausted). Both failures are a NEW defect family, independent of
wave-20's fixes (none of T0–T3 shapes were exercised): I dispatched guessed
seed PDP URLs and both were dead — and the pipeline let the dead seeds
poison every downstream stage.

## Root cause chain (both jobs, identical shape)

```
dispatched seed URL is a 404 (my operator error — guessed, not verified)
        │
        ▼
[D1] check_accessibility probe declares 404 SUCCESS (root cause)
     direct_http rung: has_content = len(html) > 2000 and not blocked
     _classify_block flags 401/403/429/503 — NOT 404
     michaelhill/marimekko 404 pages are fully-styled ~0.5–1.1 MB pages
     → probe logs literally "Status code: 404 … SUCCEEDED — real content"
     → dead seed enters probe cache as a WORKING method
        │
        ▼
     product_analyzer runs against the dead sample URL
     → every mapping an unverified guess; 336's analyzer honestly wrote
       "verdict": "TARGET_URL_DEAD" into product_analysis.json
        │
        ▼
[D2] validate_coverage ignores both the guesses and the verdict
     wave-13 presence-credit rule: any field with method/selector counts,
     downgrade ONLY on tested=="empty" (proven dead by live-render check)
     — but the live-render check can't run on a dead page, so nothing is
     "proven dead" → unverified guesses get FULL credit → 100% coverage
     → gate passes, code gen proceeds
        │
        ▼
     code_tester correctly diagnoses: "TARGET PRODUCT URL IS DEAD — HTTP 404"
     remediation.target = "mapping" (right: mappings are the symptom)
        │
        ▼
[D3] mapping-remedy remap re-runs product_analyzer against THE SAME dead
     sample URL (state.sample_url never replaced) → identical analysis →
     code_writer regenerates an identical draft → draft-freeze gate (335)
     / testing-cascade exhaustion (336)
        │
        ▼
     Job fails honestly. Freeze/cascade gates did their job; the poison
     entered 4 stages earlier and no gate could name it.
```

## Evidence

| Job | Row/step | Fact |
|-----|----------|------|
| 335 | sessionlog 66927 | `Probe result … Status code: 404` + `direct_http SUCCEEDED — real content` |
| 336 | sessionlog 66929 | Same: `Status code: 404` + `SUCCEEDED — real content` |
| 336 | sessionlog 67188 | product_analysis.json contains `"verdict": "TARGET_URL_DEAD"` — never read downstream |
| 335 | sessionlog 67384 | tester BLOCKER: "TARGET PRODUCT URL IS DEAD — returns HTTP 404" |
| 335 | steps table | no normalize_fields/validate_coverage interrupt; product_analysis → scraper_analysis clean |
| code | probe.py:982 | `_classify_block`: only 401/403/429/503 are blocks; 404 unclassified |
| code | probe.py:1034, 1154, 1353, 1425 | four success sites, all `len(html) > 2000 and not blocked/class` — no status gate |
| code | probe_tools.py:743 | escalation ladder trusts `data["success"]` only, never `status_code` |
| code | validate_coverage.py:71 | only `tested=="empty"` loses credit; unverified = full credit |
| code | graph.py:2496–2528 | remap path re-invokes product_analyzer with unchanged state.sample_url |

## Fixes (in order; strict TDD, one failing test each before code)

### T4 — probe.py: success requires HTTP 2xx (THE root fix)
In all four fetch paths (direct 1034, fingerprint 1154, browser 1353, 1425):
`has_content = 200 <= status_code < 300 and len(html) > 2000 and <block check>`.
- 404/410 pages return `success: False`, `blocked: False`, `needs_browser: True`
  (a retry/other-rung may still serve it; keep the ladder semantics).
- NOT `_classify_block` changes — 404 is not a "block", it is a miss; keep
  block semantics (401/403/429/503 escalate to stronger rungs) untouched.
- Blast-radius note: a stronger rung returning 200+content on a URL that
  404'd at direct_http still succeeds — strictly better than today.

### T5 — probe_tools.py: 404/410 is a TERMINAL ladder verdict
Escalation ladder: first rung returning 404/410 → stop the ladder (no proxy
fixes a missing page; today it would burn all 7 rungs + cloak bypass),
return failure with distinct `verdict: "not_found"` so check_accessibility
can END the job with "seed URL does not exist (HTTP 404)" instead of a
generic unreachable. LISTING-PROBE inherits via the same rung machinery.
Tests: 404-on-first-rung ⇒ exactly 1 method tried + not_found verdict;
403 ⇒ ladder continues (unchanged anti-bot semantics).

### T6 — validate_coverage honors the analyzer's own dead-seed verdict
`_load_product_analysis` result with `verdict` ∈ {TARGET_URL_DEAD, …404…}
(or `critical_finding` dead-seed) ⇒ treat like missing analysis: interrupt
(retry arm), message names the dead seed. The analyzer already reports this
honestly — the gate just never reads it. Deliberately NOT touching the
wave-13 presence-credit rule (job-12 bypass shapes stay intact).

### T7 (optional, loop-breaker) — remap replaces a proven-dead sample URL
In `_invoke_product_analyzer` remap mode: if current product_analysis says
TARGET_URL_DEAD and workspace `input_urls.json` has live URLs, point
`state.sample_url` at the first live URL (log loudly) before re-running the
analyzer. Best-effort: even a live non-PDP page beats re-analyzing a 404.

## Explicitly out of scope
- Soft-404 (HTTP 200 + "Page Not Found" content) content sniffing — both
  incident sites returned REAL 404 statuses; add later if seen in the wild.
- Discovery-quality gap 335 exposed (nav-link fallback harvested 25 listing
  pages, 0 PDPs — michaelhill PDPs are distinguishable `/p/<slug>-<id>.html`)
  — real, but second-order once dead seeds are gated; separate plan.
- Wave-13 presence credit, `_classify_block` semantics, job-118 mapping promo.

## Validation
1. New unit tests (RED→GREEN): probe 404 gates ×4 paths; ladder terminal
   404; validate_coverage verdict interrupt; remap sample swap.
2. Full root suite + webapp/tests green; ruff clean.
3. Local e2e RE-RUN with REAL seeds (michaelhill `/p/…html` PDPs from its
   product sitemap; marimekko PDP likewise) — expect happy-path COMPLETED.
4. Negative-path proof: re-dispatch the dead seed URL locally and expect a
   FAST, clearly-worded failure at check_accessibility (not 3 freeze cycles).
5. Only then: wave-20 + wave-21 PR → Railway → prod re-drive 359/360.
