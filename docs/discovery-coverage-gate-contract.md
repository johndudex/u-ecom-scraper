# Phase 1 Implementation Contract (single source of truth)

> Locks the exact schema, enum values, and flag semantics so parallel agents
> implementing across `navigation_scraper.py`, `http_navigation_scraper.py`,
> `requests_scraper.py`, `shell_tools.py`, `run_execution.py`, `scraper_runner.py`,
> and `state.py` produce CONSISTENT output. Companion to
> `docs/discovery-coverage-gate-design.md` and `docs/discovery-coverage-gate-impl-plan.md`.
>
> **Do not diverge from these names/types/values.** If a target file makes one
> impossible, implement the closest equivalent and NOTE the deviation in your return
> message — do not silently invent a different schema.

## 1. `discovery_coverage` metadata block

Every two-phase scraper MUST emit this inside its existing `metadata` dict in the
output JSON (alongside `scraping_duration_seconds`, `discovered_urls`, etc.):

```python
"discovery_coverage": {
    "stop_reason": "short_page",   # see enum §2 — REQUIRED, always non-null when discovery ran
    "found": 38,                   # int — extracted_items POST-filter (real items), NOT raw discovered count
    "discovered_urls": 45,         # int — raw pre-filter discovered URL count (diagnostic; may duplicate existing key)
    "expected_total": 3771,        # int | None — from baked-in coverage_target.total_items; None if unknown
    "dimensions_iterated": 1,      # int — categories/specialties actually iterated; 0 if no dimension loop
    "dimensions_total": 207,       # int — total dimensions known; 0 if unknown
    "max_pages_hit": false,        # bool — did the loop stop because it hit a NON-None MAX_PAGES cap?
    "ran_phase1": true,            # bool — did Phase 1 discovery actually run (false if skipped via checkpoint/resume)
    "skipped_reason": null         # str | None — if ran_phase1 is false, why: "checkpoint_loaded" | "discover_only_input" | "url_list_mode"
}
```

**Rules:**
- `found` is the count of REAL extracted items (post-filter), NOT `len(discovered_urls)`.
  This is critical (M5): raw discovered counts include nav/redirect/blocked noise.
- If Phase 1 did NOT run (checkpoint resume, `--discover-only` skipped it, url_list
  mode), emit `ran_phase1: false` with `skipped_reason`, and set `stop_reason` to
  `"skipped"`.
- For `url_list` input mode (no discovery), the block may be omitted entirely, or
  emitted with `ran_phase1: false, skipped_reason: "url_list_mode"`.

## 2. `stop_reason` enum (exact string values)

The template MUST track WHY its discovery loop terminated and emit the matching
value. A boolean `exhausted` is FORBIDDEN — it cannot distinguish "genuinely
exhausted" from "gave up due to errors."

| Value | Fires when | Classifier verdict |
|-------|------------|--------------------|
| `"short_page"` | loop ended: a page returned `< items_per_page` items (genuine end) | PASS (Tier 1 exhausted) |
| `"no_next_link"` | loop ended: no next-page element/URL found (genuine end) | PASS (Tier 1 exhausted) |
| `"no_new_items"` | loop ended: consecutive pages returned 0 NEW unique items (dedup worked) | PASS (Tier 1 exhausted) |
| `"max_pages_hit"` | loop ended: hit `MAX_PAGES` cap (which was set / non-None) | INCONCLUSIVE (soft) |
| `"navigate_error"` | loop ended: `_navigate`/fetch returned None or HTTP error / rate-limit (429/502/503) / block | **FAIL** (NOT exhausted) |
| `"dedup_flat"` | unique/seen ratio never flattened across pages (feed injection / broken dedup suspected) | **FAIL** (NOT exhausted) |
| `"skipped"` | Phase 1 did not run (checkpoint/resume/url_list) | n/a (see `skipped_reason`) |

**Implementation note:** the loop's existing termination points (e.g.
`navigation_scraper.py` "No new items on page N, stopping"; `http_navigation_scraper.py`
"page N navigate failed, stopping") each map to exactly one enum value. Thread a
`stop_reason` variable through the loop and set it at each `break`. Default to
`"no_next_link"` if the loop completes without an explicit break.

**`dedup_flat` is best-effort:** if the template's structure makes unique-ratio
tracking hard, it is acceptable to NOT emit `dedup_flat` (fall back to
`no_new_items`). But `navigate_error` MUST be distinguishable from exhaustion — this
is the single most important distinction (H4).

## 3. CLI flags (exact names + behavior)

Add to argparse in ALL THREE templates:

### `--discover-only`
- **Type:** `store_true` (boolean flag).
- **Behavior:** run Phase 1 discovery to exhaustion (subject to `MAX_PAGES`/time
  safety), emit the output JSON WITH the `discovery_coverage` block populated, then
  **SKIP Phase 2 extraction**. The output's item list will be empty or contain only
  discovered URLs (not extracted items). `found` reflects post-filter count (0 when
  Phase 2 is skipped — that's expected; the consumer reads `discovered_urls` /
  `stop_reason` / `dimensions_*` instead).
- **Purpose:** lets code_tester probe real discovery yield without extracting
  thousands of items.

### `--fresh-discovery`
- **Type:** `store_true` (boolean flag).
- **Behavior:** IGNORE any existing `discovered_urls_checkpoint.json` and run Phase 1
  from scratch (do not load the checkpoint). Still write a checkpoint as normal.
- **Purpose:** fixes H3 checkpoint cross-contamination. `run_execution` passes this
  flag so the execution phase does not silently reuse the test phase's checkpoint.

### Existing flags to preserve
- `--sample`, `--limit`, `--input`, `--query` — unchanged.
- **Known dead interaction (Phase 4 will fix):** `--sample` forces `limit=5`
  unconditionally (`navigation_scraper.py:567`). For Phase 1, leave this as-is; the
  coverage probe (Phase 4) will use `--discover-only` WITHOUT `--sample`.

## 4. Checkpoint file — naming + cleanup (H3)

- **Filename:** `discovered_urls_checkpoint.json` (unchanged).
- **Location:** `SCRIPT_DIR` (i.e. `os.path.dirname(os.path.abspath(__file__))`).
  `navigation_scraper.py:72` currently uses `os.getcwd()` — **change to `SCRIPT_DIR`**
  to match `http_navigation_scraper.py:142` and avoid cross-site leakage when cwd
  differs (L1).
- **Lifecycle:** the checkpoint is written during/after Phase 1 and loaded at Phase 1
  start IF present AND `--fresh-discovery` was NOT passed.
- **Cleanup ownership:**
  - The SCRAPER does not delete its own checkpoint (browser_service Chrome-crash
    retry at `scraper_runner.py:120` is the legitimate consumer).
  - `run_execution` passes `--fresh-discovery` for the execution phase.
  - `browser_service/scraper_runner.py` deletes the checkpoint in `_post_run` (or
    equivalent post-run hook) after a successful run, so a subsequent invocation
    starts fresh.
  - If run in-process (no browser_service), `run_execution` deletes the checkpoint
    after the run completes.

## 5. `run_scraper` long-timeout probe (shell_tools.py)

- `webapp/agents/tools/shell_tools.py:run_scraper`: add a kwarg
  `coverage_probe: bool = False`.
- When `coverage_probe=True`: use `timeout=1800` (30 min) for the in-process
  `subprocess.run` AND pass a correspondingly longer timeout to the browser_service
  dispatch (currently `timeout+60`). Do NOT change the default (300s) path — only the
  coverage probe gets the long timeout.
- Rationale (H1): a 300s timeout SIGKILLs the probe on large sites and the
  timeout-shaped crash string makes `classify_test_failure` misroute to a false
  strategy switch. The coverage probe is the ONLY legitimate long-timeout caller.
- Phase 4 (code_tester) will pass `coverage_probe=True` when invoking the
  `--discover-only` probe. Phase 1 just adds the knob.

## 6. State field

`webapp/agents/state.py`: add to `ScrapeState` (TypedDict, optional):
```python
discovery_coverage: dict   # the discovery_coverage block read from the scraper output
```
`run_execution` populates it from the output JSON's `metadata.discovery_coverage`
(replacing the current behavior of discarding metadata). Phase 3's classifier reads
it from `test_report` at test time; this state field is for the runtime/Option-B path.

## 7. Consistency checklist (each agent must verify before returning)

- [ ] `discovery_coverage` keys match §1 EXACTLY (names + types).
- [ ] `stop_reason` values match §2 EXACTLY (lowercase, underscores).
- [ ] `--discover-only` and `--fresh-discovery` flags added with EXACT names.
- [ ] `found` = post-filter extracted count, NOT raw discovered count.
- [ ] `navigate_error` is distinguishable from `short_page`/`no_new_items`/`no_next_link`.
- [ ] Checkpoint path uses `SCRIPT_DIR`.
- [ ] No template imports added that break the no-playwright/selenium constraint for
      `http_navigation_scraper.py` / `requests_scraper.py`.
- [ ] Existing output schema (top-level `site`, output_key list, `metadata`) unchanged
      except for the added `discovery_coverage` block.
- [ ] Return message lists: files changed, any deviation from this contract with
      reason, and a 3-line diff summary per change.
