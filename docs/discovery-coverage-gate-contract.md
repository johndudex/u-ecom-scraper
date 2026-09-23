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
| `"checkpoint_reused"` | Phase 1 ran FRESH and found 0; a validated `discovered_urls_checkpoint.json` was reused instead (wave-40 T14/T15) | PASS-shaped: NOT in `_COVERAGE_FAIL_STOP_REASONS` nor `_ACCESS_WALL_STOP_REASONS` (route_after_testing) — the fresh verdict survives verbatim in `fresh_stop_reason` |
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

### `checkpoint_reused` honesty guarantees (wave-40)

`stop_reason: "checkpoint_reused"` is emitted ONLY by the templates' rescue
branch (`templates/http_navigation_scraper.py`, `templates/navigation_scraper.py`)
when a fresh Phase 1 ended at a rescuable zero and a checkpoint loader verdict of
`ok` supplied the URL set. Three guarantees:

1. **The fresh verdict is never destroyed.** The original stop reason moves
   verbatim to `fresh_stop_reason` (`src.listing_discovery.checkpoint_coverage_patch`),
   alongside `checkpoint_urls` (the reused yield). Nothing is overwritten in
   place — the patch returns a new dict.
2. **It cannot mask a dead listing.** `checkpoint_reused` is in NEITHER
   `_COVERAGE_FAIL_STOP_REASONS` nor `_ACCESS_WALL_STOP_REASONS`
   (`route_after_testing.py`), so a rescued run can never be read as a
   gave-up/access-wall failure. `src.listing_discovery.listing_yield_failure` is
   unaffected: a rescued run reports a real `found` (Phase 2 extracted from the
   reused URLs), and `found != 0` short-circuits to `False` — and the
   `found == 0` case returns `False` too whenever the reused set is above the
   junk floor (a nonzero raw yield with a non-exhaustion-flavored stop reason —
   `"checkpoint_reused"` is not in `_EXHAUSTION_REASONS` — falls through to
   `False` at `src/listing_discovery.py:141`), so the conclusion holds via BOTH
   paths — the execution listing fallback never re-fires on a rescued run.
3. **The rescue is bounded and validated.** One attempt, only on a rescuable
   zero (`zero_discovery_rescuable` — never `navigate_unavailable`, which stays
   an infra verdict), only for a non-probe run, only through
   `load_discovery_checkpoint` (`reason == "ok"`): same-host filter, age cap
   (`SCRAPER_CHECKPOINT_MAX_AGE_S`, default `DEFAULT_MAX_AGE_S` = 1 day),
   reusable-yield floor (`SCRAPER_CHECKPOINT_MIN_URLS`), max-URL cap.

## 2b. Arming — who decides reuse is on (wave-40 T15)

`run_execution._checkpoint_reuse_env(state, workspace_folder)` is the single
arming authority and returns an EXPLICIT value in both directions:
`{"SCRAPER_CHECKPOINT_REUSE": "1"}` when armed, `{"SCRAPER_CHECKPOINT_REUSE":
"0"}` when not. The explicit `"0"` is deliberate — `browser_service` is a
separate container whose ambient env may carry the var while celery's does not,
and the loader below treats an UNSET var as enabled by design, so "send
nothing" would mean "inherit whatever the other container has".

Armed requires ALL of: ambient `SCRAPER_CHECKPOINT_REUSE != "0"` (operator kill
switch), `input_mode` in the Phase-1 set (`run_execution._PHASE1_MODES` — the
same frozenset the `--fresh-discovery` append uses), the checkpoint file present
in the workspace, and NOT a discover-only/force-full run (`state["force_full"]`;
`run_execution` never passes `--discover-only` itself — that is the probe lane's
flag). Default is **OFF**: unset env ⇒ the arm emits `"0"`, and the template
branch additionally gates on `== "1"`, so a deployment that ships the flag
without arming it changes nothing.

The env reaches the scraper subprocess on BOTH execution lanes: merged into the
in-process `Popen` env (`run_execution._run_in_process` via its
`env_overrides`), into the `/scrape` payload `env_overrides`
(`run_execution._run_via_browser_service`), and into the multi-source category
merge lane (`run_execution._run_category_sources`) — every `/scrape` dispatch
carries the explicit armed-or-disarmed value.

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
- **[wave-40] Unconditional at execution.** `run_execution` appends this flag
  for every Phase-1 mode REGARDLESS of whether checkpoint reuse is armed: the
  flag is also the **api family's execution trigger** (declared + consumed in
  the generated draft's argparse; `graph.py` appends it to the probe args too),
  so gating it on reuse would break api-family execution. Armed runs therefore
  take the **fresh-then-validated-rescue lane**: Phase 1 runs fresh, and only
  when it ends at a rescuable zero does the template's T14 rescue branch read
  the staged checkpoint through `load_discovery_checkpoint`. (The templates'
  legacy B-core resume lane — `checkpoint_urls = [] if args.fresh_discovery
  else _load_checkpoint()`, `stop_reason: "skipped"`, no host/age/floor
  validation, no marker — stays reachable only from outside prod phase-1
  execution, because the flag is always appended.)

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

### [wave-40] Reuse + the probe exclusion

- **Probes never see checkpoints.** `_probe_phase1_discovery_once`
  (`graph.py`) contains no checkpoint reference at all — its env is built in
  `graph.py` (never through `run_execution._checkpoint_reuse_env`) and its own
  `extra_files` staging loop stages only `input_urls.json` /
  `discovery_config.json`, never `discovered_urls_checkpoint.json`. A
  `--discover-only` probe therefore always measures a FRESH discovery; that
  exclusion is what kept prod 763 nastygal honest (the probe's yield was the
  site's, not a banked set's). The execution lane's staging tuple
  (`run_execution._run_via_browser_service`) is the only one that stages the
  checkpoint — and only in the browser-service lane, where the two containers
  share no filesystem; the in-process lane reads the file from the workspace
  directly.
- **Two lanes exist; only one is prod-reachable from phase-1 execution.**
  - T14 validated rescue lane (armed): fresh Phase 1 → rescuable zero →
    `load_discovery_checkpoint` → `[DISCOVERY-CHECKPOINT-REUSED]` marker on
    stderr/log, `stop_reason: "checkpoint_reused"` + `fresh_stop_reason` +
    `checkpoint_urls` in the emitted coverage (see §2).
  - Legacy B-core resume lane (unvalidated): load at Phase-1 start, no marker,
    `ran_phase1: false` with `stop_reason: "skipped"`. Unreachable from prod
    phase-1 execution because `--fresh-discovery` is appended unconditionally
    (see §3).
- **`disabled` is overloaded.** `load_discovery_checkpoint` returns
  `reason: "disabled"` for BOTH the explicit `SCRAPER_CHECKPOINT_REUSE=0` kill
  switch AND a non-phase-1 `input_mode`. Arming logic must never read
  `disabled` as "env is 0" — it is a catch-all refusal, which is why
  `_checkpoint_reuse_env` computes its own predicate instead of loading.
- **The floor can bless junk.** The reusable-yield floor
  (`SCRAPER_CHECKPOINT_MIN_URLS`, default `ZERO_YIELD_JUNK_LINKS` = 2) is a
  strict `<` on the BANKED count — a 2-URL banked set is `ok` to the loader
  (never `below_floor`) even though the module's own yield gates would classify
  a 2-link listing as a detail page. The strict comparison is forced by the
  verbatim cross-host test
  (`tests/test_wave40_checkpoint_reuse.py::test_default_floor_rejects_single_url`);
  treat it as known, accepted slack, not a bug to "fix" by tightening silently.
- **Default OFF.** Unset env ⇒ reuse disabled at the arm (the arm emits the
  explicit `"0"`, §2b); the template branch additionally gates on
  `SCRAPER_CHECKPOINT_REUSE == "1"`. Nothing reuses a checkpoint unless
  `run_execution` armed this specific run.

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
