# Wave-65: Intake mode-truth — surface what the system changes, guard what it can't honor

**Status**: COMPLETE (2026-10-15) — critique-2 blocker incorporated (F2 composed predicate);
critique FINAL APPROVE. TDD 16/16 green. Certifying gate: **5126 passed / 5 failed / 11 errors**
— all 5 failures fail isolated (stash-proven banked reds: truncation ×2 local-file, views-intake,
filesystem_tools paging; template_path_anchor = wave-56 volume-drift environmental), 11 errors =
banked wave-52 retry-stretch class; zero wave-65 names. Local e2e 3/3: (a) PDP-in-listing → 200 +
coercion warning w/ guidance (job 668, cancelled); (b) amazon `/s?` exact-list → 422
`listing_lookalike`, no job; (c) `exact_list_override=1` → job 669 created, cancelled. Build note:
first draft of the F3 template pin read the template via a CWD-relative `Path()` — red only under
full-suite order (upstream chdir leak), fixed with the `__file__`-anchored ROOT idiom from
test_wave55_intake_small_fixes.py. RCA and fixes below.

## RCA (one class, three faces — mode determination at intake is opaque)

**Face A — prod 1417 (replacements.com, "expecting ~200 products", got 1).** User submitted a
listing-mode job; wave-37's `coerce_pdp_intake` (src/intake_coerce.py) flipped it to `pdp` because
the URL matches `/p/<id>` (a single 32-piece-set product page). The coercion note went ONLY into
`job.notes` (views.py:3454-3457). The create-job success response (views.py:3769-3790) already
carries a `warning` slot (wave-55 M4 fragment warning, spread at :3775) and intake.html renders it
via `showBuildWarning` (:2209, call at :2199) — **the coercion never uses either**.

**Face A correction (2026-10-15, user testimony — supersedes the first reading).** The user did
NOT paste a bare PDP: they pasted the `/b/` baccarat catalog (`/crystal-baccarat/b/002-800087`)
into the listing box with the Lismore-set PDP as the main example URL — the exact shape the intake
form invites (its homepage error says to paste "one product, listing…"). Coercion fired on the
PDP-shaped main URL and rebound nav_method BEFORE the listing branch read `listing_urls`, so the
catalog URL was silently DISCARDED — the job ran as a 1-row PDP scrape. Coercion-with-seeds-present
is wrong in every shape; F4 below narrows the trigger.

**Face B — prod 1418 (amazon.in `/s?rh=` listing, FAILED pc=0 after ~2.5h).** Exact-list mode
accepted a search-results URL as a product-URL seed; no intake check covers the "listing submitted
as PDP list" direction. Execution died honestly (`navigate_error`).

**Face C — latent.** `runBuild` (intake.html:2150) silently defaults nav to `'list'` when no radio
is checked; `isRunSampleReady` (:1156) blocks loudly — the two disagree.

## Fixes

1. **F1 — surface the coercion at submit time (Face A).** In `intake_create_job`, after BOTH
   `_coerce_note` (:3454) and `_fragment_warning` (~:3534) exist, compute ONCE:
   `_submit_warning = "; ".join(w for w in (_coerce_note, _fragment_warning) if w)` and replace
   line 3775's spread with a single `**({"warning": _submit_warning} if _submit_warning else {})`
   (critique-1 blocker 4: two `warning` spreads would clobber — later literal wins). In practice
   the two notes are mutually exclusive (coercion rebinds nav_method to `pdp` BEFORE the
   listing/search branches read listing_urls/search_url, so the fragment warning can't co-fire —
   critique-1 blocker 3), but the join is defensive. Extend the wave-37 note text in
   `coerce_pdp_intake` with user guidance: `… Expecting many products? Submit the listing/catalog
   page URL instead.` — wave-37 pins are substring-only (`"/p/" in note`,
   tests/test_wave37_intake_coerce.py:28; no exact-text pins repo-wide, critique-1 §9).
   UI: `submitBuild` hides `#build-warning` at the top of every submit (critique-1 advisory 6: the
   element is never cleared today — a stale warning would persist across clean re-submits);
   rendering needs no new surface.
2. **F2 — search-lookalike guard (Face B), NARROWED per critique-1 blockers 1+2.** Category- and
   collection-shaped seeds in exact-list mode are LEGITIMATE (wave-57 deliberate contract: warn-
   stamp + ship — job 566 shipped 913 rows; tests/test_wave57_listing_seed_intake.py pins 200 +
   stamp). Only SEARCH-shaped seeds hard-fail. New predicate in src/intake_coerce.py, composed
   per critique-2 blocker 1: `looks_like_search_seed(url)` = (first segment `search` AND
   `seed_url_is_listing_shaped(url)` — discovery_quality.py:227, whose item-shaped veto spares
   real PDPs) OR (first segment `s` AND exactly ONE path segment AND non-empty query — the
   amazon `/s?rh=…` family; NOT delegated, because the row floor correctly treats single-root-slug
   `/s` paths as items, so delegation returns False there — the simulated-trace miss critique-2
   caught). Multi-segment `/s/<slug>/<digits>` (nordstrom PDPs) → False on both arms.
   In `intake_create_job`, AFTER the `missing_url_list` gate (:3650), keyed on
   `nav_method == "list"` (coerced pdp never reads "list" — natural exclusion): if any SUBMITTED
   `list_urls` line (read at :3625) satisfies `looks_like_search_seed` and
   `request.POST.get("exact_list_override") != "1"` → 422
   `{"error": "…looks like a search/results page — did you mean listing or search mode?…",
   "listing_lookalike": True}` (shape mirrors :3650). Submitted lines only — NO main-URL and NO
   stored-seed inspection (critique-1 advisory 7: the SEEDS-FROM-FILE arm must stay untouched).
   UI: `submitBuild` branches on `data.listing_lookalike` BEFORE the generic `data.error` branch
   (critique-1 advisory 5 — `showUrlError` would hide the pasted-URL textarea): warning-style
   notice with two buttons — "Switch to listing mode" (move `#nav-list-text` lines into
   `#nav-listing-text`, check the listing radio + swap `.sel`, wave-54 restore DOM pattern
   :1982-2000, then re-fire runBuild) and "Keep exact list" (dup-modal pattern :2262: keep the
   FormData, set `exact_list_override=1`, re-submit).
3. **F3 — no silent mode default (Face C).** runBuild: no `nav` radio checked →
   `showUrlError('Choose how we should find the pages — exact list, listing page, or search.')`
   and return before building FormData. (The `scope || 'all'` fallback is unreachable — firstn
   ships `checked` at intake.html:546; leave it.)
4. **F4 — coercion never fires when seeds were supplied (Face A correction, post-ship).** In
   `intake_create_job`, the `coerce_pdp_intake` call is skipped when the POST carries ANY of
   `listing_urls` / `search_url` / `search_keywords` (non-empty): those submissions are
   definitionally catalog jobs and the PDP-shaped main URL is just the example page. Bare-PDP
   submissions (626-class — wave-40/wave-55 view pins all use that shape) still coerce.
   Wave-37's unit pins are on the function itself and are untouched. Test evidence: RED ×2 →
   GREEN; the first search-mode test draft tripped the wave-32 cross-host gate (test bug, fixed
   in-test). The original MUST-NOT ("no change to wave-37 coercion semantics") is hereby amended
   by evidence: narrowing the TRIGGER to the bare-PDP shape preserves wave-37's purpose (every
   wave-37/wave-40/wave-55 pinned shape still coerces) while closing the 1417 seed-discard class.

## MUST-NOT

No change to wave-37 coercion semantics. **No 422 for category/collection seeds** (wave-57
contract intact — its suite is a re-pin target). No mutation of user input on the F2 path (user
choice via override, never auto-rebind). No parallel listing classifier — delegate to
`seed_url_is_listing_shaped`. No new settings/env. No blocking modal. No inspection of stored
seeds or the main URL on the F2 path. Dashboard re-run path untouched (posts to restart_url →
job_restart views.py:722, never re-enters intake_create_job — critique-1 advisory 7).

## Tests (TDD — webapp/tests/test_wave65_intake_mode_truth.py, mirroring the
webapp/tests/test_wave57_listing_seed_intake.py:26-49 harness: django.test.Client + force_login +
reverse("intake_create_job") + HTTP_X_REQUESTED_WITH + patched run_scrape_task/_fm_exists/artifacts)

- F1: coerced submission (PDP-shaped URL + nav_method=listing) → response JSON `warning` contains
  "coerced" + "item page" + "listing/catalog"; clean listing submission → no `warning` key;
  fragment-bearing listing URL → `warning` contains "fragment" text (wave-55 pins at
  test_wave55_intake_small_fixes.py:108-155 still hold — disjoint arms).
- F2 unit (looks_like_search_seed): `https://www.amazon.in/s?rh=n:1&p_123:2` → True; `/search?q=x`
  → True; `/s` (bare) → True; nordstrom `/s/nordstrom-bleach-safe-flip-flops/3954158` → False
  (item veto); `/products/widget` → False; `/collections/all` → False (not the search namespace);
  `/c/94020` → False.
- F2 view: amazon /s? seed in exact-list → 422 `listing_lookalike: True`; re-submit with
  `exact_list_override=1` → job created; nordstrom-style `/s/<slug>/<digits>` seed → created, no
  422; category seed (`/all/...`) → 200 + `listing_seeds` stamp (wave-57 contract unchanged);
  listing-mode + search URL → no 422 (normal path).
- F3: template pin — intake.html contains the explicit-block guard text
  ("Choose how we should find the pages") and the `listing_lookalike` branch.
- Re-pin pass (verify-then-edit BEFORE certifying suite): grep tests/ webapp/tests/ for
  `coerce_pdp_intake|missing_url_list|showBuildWarning|fragment_warning|listing_seeds|
  test_wave57_listing_seed_intake|seed_url_is_listing_shaped` (critique-1 advisory 11).

## Acceptance bars

1. Targeted suite green + lint clean (webapp/ + src/).
2. Full gate, baseline RE-DERIVED at gate time (not asserted) + new tests; known-reds/artifact
   errors baseline-identical.
3. Local e2e (≤2 running jobs): local stack — (a) PDP-in-listing submit → response `warning`;
   (b) amazon /s? seed in exact-list → 422 listing_lookalike; (c) override re-submit → job
   created, **cancelled immediately**.
4. Verdict gate + ledger row; plan status update; memory.
