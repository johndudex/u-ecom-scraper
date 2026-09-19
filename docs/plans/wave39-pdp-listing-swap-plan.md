# Wave-39: PDP-seed listing swap (search_criteria promotion)

**Status:** DRAFT v1 — awaiting user review
**Date:** 2026-09-19
**Branch:** `file-master-artifacts`
**Evidence:** prod jobs 719-737 retry batch (2026-09-19), 719 mimco COMPLETED count=1

## 1. Problem

Wave-34's T34-2 "PDP-seed honesty" flip (`webapp/agents/graph.py:2214-2260`)
demotes any `list_page` job whose seed URL is a Product (probe JSON-LD) to
`url_list` with the seed as its single item. Correct for intakes that ONLY
have a PDP — but these jobs also carry a same-host **listing URL** in
`search_criteria`, plus a scope (`firstn/10`). The flip ignores both, so:

- prod 719 (mimco retry): `[INTAKE-PDP]` fired, discovery never browsed
  `https://mimco.com.au/collections/jewellery`, output = 1 product.
- The intake/status page's Retry rows are ALL PDP-seeded → every retry lands
  on ~1 product. 15 of the 19 fired retries had to be cancelled (723-737);
  719/720/721/722 ran to 1-item completions.

The demote exists because PDP-seeded discovery harvests cross-domain
recommendation carousels (614/620/622/625/626 contamination-guard deaths).
We keep that protection — but when the job ITSELF names a same-host listing,
the honest reading of the intake is "discover from the listing", not "one
item".

## 2. Design

In `check_accessibility` (graph.py flip block), evaluate a **swap** BEFORE the
existing demote. Swap = rewrite the discovery seed to the listing; stay
`list_page`.

### Swap conditions (ALL required, else fall through to existing demote)

1. `_input_mode == "list_page"` (unchanged scope — navigation/search_term
   keep their modes exactly as today).
2. `_jsonld_product_entity(data.get("jsonld"), url)` is True (seed is PDP —
   unchanged discriminator).
3. `_criteria` (state `search_criteria`) parses to http(s) AND its hostname
   equals the seed's hostname (same equality rule the advisory listing probe
   already uses at graph.py:2184-2186).
4. Positive listing evidence: `listing_probe is not None AND
   not listing_probe.get("blocked") AND (listing_probe.get("method_that_worked")
   or listing_probe.get("needs_browser") is not None)` — the advisory probe
   (graph.py:2191) already measured the listing when conditions 1-3 held; we
   only swap when that measurement REACHED the listing.
5. Listing itself is not a PDP: `_jsonld_product_entity(listing_probe.get("jsonld"),
   _criteria)` is False. (A user pasting two product URLs still demotes.)

### Swap effects

- State: `url ← _criteria`, `product_url ← _criteria` (downstream: traverse
  seed, analyzer prompts, same-domain guards all key off these).
- DB: `ScrapeJob.url ← _criteria` (best-effort, warning on failure — never
  blocks the swap), notes append:
  `[INTAKE-PDP-SWAP] seed <pdp> is a product page; discovery seed swapped to listing <criteria> (listing probe method=<method_that_worked>)`.
- NOT written: 1-item `input_urls.json` (the demote's artifact). The normal
  list_page flow stages it from discovery results later.
- `probe_result` stays keyed to the PDP probe (honest record of what was
  probed); `listing_connectivity` already carries the listing measurement
  (graph.py:2292-2294) — no new probing, zero extra cost.

### Fail-open rules preserved

- No swap criterion met → existing demote, byte-for-byte unchanged (its
  `_write_flip_input_urls` fail-open contract stays).
- Any exception inside the swap branch → log warning, fall through to demote
  (same advisory-probe posture as graph.py:2207-2212).

## 3. Tasks (TDD, each ends with a commit)

### T1 — Swap gate unit tests (`tests/test_wave34_pdp_seed_flip.py` extends)

Reuse the file's `_probe_data()` harness. Cases (each calls the flip block's
extracted helper — see T2):

1. swap fires: PDP seed + same-host criteria + listing_probe reachable,
   non-PDP listing → state url/criteria swapped, input_mode stays list_page,
   NO input_urls.json write, notes carry `[INTAKE-PDP-SWAP]`.
2. no criteria → demote (existing behavior regression-guard).
3. cross-host criteria → demote.
4. listing_probe missing (advisory probe skipped/host-mismatch path) → demote.
5. listing_probe blocked=True → demote.
6. listing JSON-LD is itself a Product → demote.
7. (node-level, shares T3's harness) DB update raises → swap still applied in
   state, warning logged (fail-open).

### T2 — Extract + implement `_pdp_listing_swap()`

New pure-ish helper beside `_write_flip_input_urls` (graph.py ~:2046):
`_pdp_listing_swap(state, url, data, listing_probe) -> dict | None` returning
the Command update (`url`, `product_url`, notes text) or None. The flip block
calls it first; demote becomes the `else`. DB update + notes write live in the
node (models access stays out of the helper). Run: existing 16 flip tests
green + new tests green.

### T3 — Restart-path integration test

`tests/test_wave39_swap_restart_shape.py`: a job built like the retry rows
(`url`=PDP, `input_mode=list_page`, `search_criteria`=same-host listing,
`scope=firstn/10`) driven through check_accessibility with a mocked probe →
asserts the full Command: swapped url, list_page intact, no
`input_urls.json` in the workspace dir, ScrapeJob.url updated, notes marker.

### T4 — Local e2e gate (two drives, `scripts/run_wave39_swap_gate.py`)

New driver modeled on `scripts/run_wave38_concurrent_gate.py` (force_full,
skip_approvals) but taking explicit `--url --criteria` overrides instead of
fixtures:

- **Drive A (swap):** westelm or any local-friendly shopify — url=PDP,
  criteria=its collection listing, firstn 10 → expect COMPLETED, count ≥ 2,
  notes `[INTAKE-PDP-SWAP]`, zero `[INTAKE-PDP]` demote marker.
- **Drive B (control/demote):** same PDP, NO criteria → expect the 419 shape
  (count=1, `[INTAKE-PDP]`).

PASS = both shapes correct + zero cross-domain/wrong-site markers in celery
log (grep set from wave-38 gate).

### T5 — Closeout

Full suite (`pytest ../tests .` from /app/webapp, expect 3334+4 known reds +
new), `ruff check webapp/ src/`, commit series, EB sync per
[[extractorbuilder-sync-rules]] (u-ecom ls-files list), merge-tree clean,
compare link to user. Deploy order unchanged (django+celery only — no
browser-service change). Post-deploy: re-fire 3-5 of the cancelled retry rows
via intake/status — with the swap live they should return ~10-item runs (this
is also the user-visible payoff of the wave).

## 4. Out of scope (parked wave-39 candidates, unchanged)

- W8 park-and-retry on /scrape 429s (job-715 gap)
- cap=1/cap=2 hung-run fairness
- Magento-PWA strategy-picker gap (briscoes 420)
- per-run Chrome isolation (wave-39 Phase-D)

## 5. Accepted risks

- The listing URL is user-provided and probe-verified, but its CONTENT could
  still be sparse (e.g., a filtered collection with 3 items) — scope firstn
  bounds the job; discovery's empty-listing stop reasons still apply honestly.
- Swap changes job.url mid-flight; the UI's "target URL" column now shows the
  listing. This is desired (it IS the scrape target) but is a visible change.
