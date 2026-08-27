# Job 12 fix plan — PLANNER 2: MINIMAL DIFF / SURGICAL

> Lens: smallest change that makes the failure class impossible. Every claim below is
> grounded in a `file:line` I read in this tree at 36a91f0. Companion to
> `docs/plans/job12-context-brief.md` (the evidence base — not re-derived here).

## The one structural finding this plan is built on

Job 12's chain is not five independent bugs. It is **one weak predicate copied three
times, plus one repair path that launders truncation into authority.**

The predicate is *"an `api_endpoint` with a URL is a real data API."* It appears at:

| Site | File:line | What it decides | Job 12 effect |
|---|---|---|---|
| A | `webapp/agents/graph.py:3051-3058` | `strategy = "internal_api"` | built the useinsider scraper |
| B | `webapp/agents/nodes/validate_coverage.py:178-189` | skips the field-coverage gate | 1-of-6 artifact accepted |
| C | `webapp/agents/subagents.py:2679` | emits "CRITICAL — do NOT drive a browser" | contradictory input to code_writer |

And the laundering path is `_restore_from_archive` → `guard_json_bytes` →
`repair_json_text` pass 2 (`webapp/agents/nodes/setup_workspace.py:104-121` →
`webapp/agents/tools/filesystem_tools.py:146-149` → `webapp/agents/graph.py:311-321`):
a truncated FM artifact is rehydrated as a **valid, canonical, truncated** artifact.

So: **P2 and P5 collapse into one predicate fix; P3 collapses into one rehydration
fix.** P1 and P4 are genuinely separate. Five areas, four edits.

---

## P1 — 429 retry policy: the ladder already exists; only the budget is wrong

### Evidence check (the brief's premise is half-wrong, and that's good news)

The brief asks for "exponential/jittered backoff scaled to provider rate limits."
**That already exists.** `_backoff_delay` is full-jitter exponential
(`webapp/agents/llm.py:88-91`): `uniform(0, min(cap, base * 2**attempt))`.
Job 12's observed 1.6 / 4.2 / 1.7 s is exactly `base=1.5`, `cap=30`, attempts 1-3
(`webapp/config/settings.py:219-221`).

The defect is **two numbers, not a mechanism**:

1. `LLM_RETRY_RATELIMIT_MAX = 3` (`settings.py:220`) → budget exhausted in ~7.5 s.
   A provider rate-limit window is ~60 s. The retry *never reaches the window*.
2. No floor. Full jitter is uniform in `[0, X]`, so attempt 1 can legally sleep 0.0 s.

Everything else already works as designed: `_retry_classified_sync` catches
`openai.RateLimitError` first (`llm.py:166`), honours `Retry-After` capped at 60 s
(`llm.py:75-85`), and `_CALLER_BUG_ERRORS` still fail fast (`llm.py:175-176`).

**Also verified — the mechanism the brief hoped to reuse is inert in prod.**
`_record_breaker` does record rate-limit failures (`llm.py:280-288`: only
`_CALLER_BUG_ERRORS` are excluded), but the deployed config makes the breaker a
no-op: `ZAI_MAIN_MODEL == ZAI_SMALL_MODEL == ZAI_FALLBACK_MODEL == "glm-5-turbo"`
(`settings.py:181-182`, `:213`), and `effective_model` returns `primary` when
`primary == fallback` (`webapp/agents/llm_breaker.py:139`). Even if it tripped, it is
a *pre-call* gate — it cannot rescue the call that is already failing. Do not build on
the breaker.

### The minimal diff (~10 lines)

`webapp/agents/llm.py`:

```python
# _retry_settings() (llm.py:62-72) — add two keys:
    "ratelimit_base": float(getattr(settings, "LLM_RETRY_RATELIMIT_BASE", 8.0)),
    "ratelimit_floor": float(getattr(settings, "LLM_RETRY_RATELIMIT_FLOOR", 5.0)),

# _backoff_delay (llm.py:88-91) — take a base/floor override:
def _backoff_delay(attempt: int, cfg: dict, base: float | None = None,
                   floor: float = 0.0) -> float:
    b = cfg["backoff_base"] if base is None else base
    return max(floor, _r.uniform(0.0, min(cfg["backoff_cap"], b * (2 ** attempt))))

# _handle_retry (llm.py:211) — rate-limit class uses its own base + floor:
    if kind == "rate_limit" and retry_after is None:
        delay = _backoff_delay(attempt, cfg, base=cfg["ratelimit_base"],
                               floor=cfg["ratelimit_floor"])
    else:
        delay = retry_after if retry_after is not None else _backoff_delay(attempt, cfg)
```

`webapp/config/settings.py:220`:

```python
LLM_RETRY_RATELIMIT_MAX = config("LLM_RETRY_RATELIMIT_MAX", default=6, cast=int)
LLM_RETRY_RATELIMIT_BASE = config("LLM_RETRY_RATELIMIT_BASE", default=8.0, cast=float)
LLM_RETRY_RATELIMIT_FLOOR = config("LLM_RETRY_RATELIMIT_FLOOR", default=5.0, cast=float)
```

Result: delays ≈ `uniform(0, 8/16/32/64/90/90)` with a 5 s floor → ~35 s expected,
~5 min ceiling for the whole budget. A per-minute provider window is now inside the
retry envelope. `Retry-After`, when the provider sends it, still wins verbatim
(`llm.py:211` unchanged path).

### Why this is sufficient (mechanism)

Job 12's four 429s were an 8-second burst against a per-minute quota. Spacing the
same four calls over 35 s-5 min makes exhaustion require a *sustained* outage, which
is a different (and legitimate) failure. 98/98 main-model calls succeeded through the
proxy in the same window — the quota was small-model-specific and short-lived.

### Failing tests first (`tests/test_llm_provider.py` — pure functions, no Django)

`_handle_retry` and `_backoff_delay` are importable without a Django DB; this file
already tests them via `_FakeSettings` (`:21`). None of the following can pass today:

1. `test_rate_limit_delay_never_below_floor` — patch `random.uniform` → 0.0; assert
   `delay >= 5.0`. **Fails today** (returns 0.0).
2. `test_rate_limit_budget_default_is_6` — 6 consecutive `RateLimitError`s are retried;
   the 7th raises. **Fails today** (raises on the 4th).
3. `test_rate_limit_total_wait_exceeds_provider_window` — sum the six sleeps with
   `uniform` pinned to 25 % of range; assert `>= 60`. **Fails today** (≈1.9 s).
4. `test_transient_class_unchanged` — locks `transient_max=2`, `base=1.5`, no floor.
   Guards against the edit leaking into the timeout/5xx class.

### What it deliberately does NOT fix

- **Exhaustion is still job-fatal.** The non-fatal variant (pause-for-approval, one
  `elif` at `webapp/scraper/services.py:258` reusing `create_recursion_approval`,
  `services.py:413`) is *deliberately excluded*: it is unproven that
  `graph.invoke(Command(resume=...))` (`tasks.py:410`) can resume a graph whose
  checkpoint is **mid-node after an exception** rather than at an `interrupt()`. The
  only existing caller is the `GraphRecursionError` branch (`services.py:258-259`) and
  it has **zero tests** (`grep GraphRecursionError webapp/tests/` → nothing). Shipping
  a second dependency on an untested resume path is how this codebase accumulates
  dead resilience. If wanted, it is a follow-up that must land *with* a resume test.
- Timeout/5xx budgets (a separate failure class, 46 % of prod failures per memory).
- No async (constraint 5). `time.sleep` in the sync path is unchanged.

### Rollback

Env-only: set `LLM_RETRY_RATELIMIT_MAX=3` and `LLM_RETRY_RATELIMIT_FLOOR=0`. The code
change is inert at those values. No deploy ordering constraint.

---

## P2 — Poison endpoint: one predicate, three consumers (fixes P5 too)

### Evidence: would requiring a positive-int `count` reject amnhealthcare? **No.**

Read the actual artifacts:

- `scrapers/amnhealthcare-com/analysis/navigation_analysis.json` → `api_endpoint` keys
  are exactly `[base_params, method, no_auth_required, notes, returns_structured_json,
  url]`. **No `count`, no `items_per_page`, and `data_source` is absent.**
  amn's API is the *LLM/findings* shape written by
  `navigate_synthesize._best_api_endpoint` (`webapp/agents/nodes/navigate_synthesize.py:183-191`),
  not the `verify_api` descriptor shape.
- Therefore at **consumer A** (`graph.py:3051`) amn already fails the gate on
  `isinstance(_api_items, int)` — tightening `count` changes nothing for amn.
- `scrapers/vistastaff-com/analysis/navigation_analysis.json` → `api_endpoint` is the
  doubleclick URL, also with no `items_per_page` → already rejected at A. Its
  "inert by luck" is actually "inert by `items_per_page`", which is durable.
- `scrapers/ayahealthcare-com/analysis/navigation_analysis.json` →
  `{url, count: 26955, items_per_page: 5}` → the only repo artifact that ever passed
  gate A, and it passes a positive-int rule.
- lw.com Coveo (`count: 0`) is unaffected — the explicit-zero rejection is a subset of
  positive-int.

**But the same rule applied at consumer C *would* regress amn** — that is the trap.
`subagents.py:2679` fires on `url or api_url` alone, and amn's scraper is a working
`api_scraper` (`scrapers/amnhealthcare-com/scraper_analysis.json` →
`strategy: "http_requests"`, hint-driven). So consumer C must distinguish *descriptor
provenance*, not just count.

**Cross-domain, count-null descriptors that exist in this repo, exhaustively:**
I scanned every `navigation_analysis.json` under `scrapers/` and `workspace/` for the
`verify_api` shape. Exactly two have `items_per_page` set:

| Artifact | url | count | items_per_page | Verdict under positive-int rule |
|---|---|---|---|---|
| `scrapers/ayahealthcare-com/...` | api.ayahealthcare.com | 26955 | 5 | passes (correct) |
| `workspace/sidley-com/...` | **null** | **null** | 100 | **blocked (correct — see below)** |

sidley's `sample_keys` are `["text", "value", "count"]` — a people-directory taxonomy,
and the record shape is one key away from the select-option shape that
`traversal.verify_api` already rejects (`experimental/nav_traversal/traversal.py:480,497`).
Its `url` is null so gate A already skips it; consumer B (below) keys on `url` too, so
it is also inert there. No working site is lost.

### The minimal diff (~30 lines net, incl. comments)

One new predicate, placed in `webapp/agents/constants.py` (already the shared
import home for strategy vocabulary — `constants.py:48-49` — and importable from
`graph.py`, `nodes/validate_coverage.py`, and `subagents.py` with no cycle):

```python
def api_endpoint_verified(api: object) -> bool:
    """True only when the captured API PROVED it returns records AND reported a
    non-zero total. count in (None, absent, 0) all reject: None is what a
    personalization/config endpoint returns (job 12: useinsider), 0 is Coveo's
    explicit 'no rows for the generic query' (lw.com)."""
    if not isinstance(api, dict):
        return False
    if not (api.get("url") or api.get("api_url")):
        return False
    items, count = api.get("items_per_page"), api.get("count")
    return isinstance(items, int) and items > 0 and isinstance(count, int) and count > 0
```

**Consumer A — `graph.py:3051-3058`** (the strategy gate). Replace the 5-clause `if`
with:

```python
if _data_source == "api" and api_endpoint_verified(_nav_api):
    strategy = "internal_api"
```
and rewrite the comment block at `:3038-3048` (it currently *documents* `count is None`
passing — that clause is the bug).

**Consumer B — `validate_coverage.py:181`** (one token):

```python
if api_url and api_endpoint_verified(api_ep):
```
This single token is what let the truncated artifact through on job 12 (see P3).

**Consumer C — `subagents.py:2679`** (keep amn, demote poison). Keep the outer `if`,
branch the *text*:

```python
if api_endpoint.get("url") or api_endpoint.get("api_url"):
    ...
    if api_endpoint_verified(api_endpoint) or "items_per_page" not in api_endpoint:
        api_section = (... existing CRITICAL/PREFERRED block, unchanged ...)
    else:
        api_section = (
            f"\n### CAPTURED ENDPOINT FAILED VERIFICATION — do NOT build around it\n"
            f"`{api_endpoint.get('url') or api_endpoint.get('api_url')}` was captured from "
            f"the browser network log but its probe returned items_per_page="
            f"{api_endpoint.get('items_per_page')}, count={api_endpoint.get('count')}. "
            f"It is a personalization/config endpoint, not the catalog API. Use the "
            f"browser strategy; ignore this URL.\n"
        )
```
`"items_per_page" not in api_endpoint` is the provenance test: amn and every
LLM-authored descriptor predate that key and keep today's hint verbatim; only
`verify_api`-shaped descriptors are held to the evidence standard.

**Consumer D — `graph.py:2912`** (the second door, 2 lines). `_ESCALATION` currently
hands out `internal_api` on retry with **no evidence check at all** —
`_decide_strategy:2914-2927` walks `["http_requests","http_navigation","playwright",
"internal_api"]` and overwrites all four strategy keys. Gate the last rung:

```python
_ESCALATION = (["http_requests", "http_navigation", "playwright"]
               + (["internal_api"] if api_endpoint_verified(_nav) .get("api_endpoint") or {} else []))
```
( spelling: `_nav` is already in scope two lines up via `_derive_strategy`; hoist
`_nav_api` if needed. )

### Why this is sufficient (mechanism)

With A tightened, job 12's `count=null` useinsider descriptor cannot select
`internal_api`; with D tightened, the retry ladder cannot select it either; with C
demoted, code_writer is no longer told to prefer it. The strategy falls through to
`_rendering`-driven selection (`graph.py:3007-3027`) — playwright/http_navigation —
which is exactly what `product_analysis` had already concluded. Cycle 2 and cycle 3
never happen, so the small-model 429 burst (which only happens because cycle 3
burned 49 calls) never happens.

### Failing tests first (new file `tests/test_strategy_api_gate.py`)

`_derive_strategy` is a pure function of `state`; call it directly with a synthetic
state (no LLM, no DB).

1. `test_count_null_does_not_select_internal_api` — job 12 descriptor
   `{url: useinsider, count: None, items_per_page: 5}`, `data_source: "api"`,
   `rendering_verified: "browser"` → `strategy == "playwright"`. **Fails today.**
2. `test_count_absent_does_not_select_internal_api` — same with the key missing.
3. `test_aya_descriptor_still_selects_internal_api` — `{count: 26955,
   items_per_page: 5}` → `"internal_api"`. Regression lock on the protected site.
4. `test_coveo_explicit_zero_still_rejected` — `count: 0` → not `internal_api`.
   Locks the existing lw.com behaviour through the rewrite.
5. `test_amn_legacy_descriptor_keeps_preferred_api_hint` —
   `build_code_writer_message` with amn's exact descriptor (no `items_per_page` key)
   → the CRITICAL block is still present. **This is the test that would fail if
   consumer C were naively tightened — it is the amn tripwire.**
6. `test_verified_failure_gets_demote_block` — job 12 descriptor in
   `navigation_analysis` → the demotion text, not the CRITICAL text.
7. `test_coverage_gate_runs_when_api_unverified` — `validate_coverage` with an
   unverified descriptor + a 1-of-6 field map → `interrupt_reason == "low_coverage"`.
   **Fails today** (the `api_url` branch returns early).
8. `test_escalation_never_lands_on_internal_api_without_evidence` —
   `strategies_tried = [playwright]`, unverified descriptor → next strategy is not
   `internal_api`.

### What it deliberately does NOT fix

- The capture side. `url_looks_like_data_api` (`traversal.py:937-976`) still admits
  `/api/` paths, so useinsider is still *captured* and still lands in
  `navigation_analysis.json`. It is captured-and-untrusted rather than captured-and-
  authoritative. Adding useinsider to `_TELEMETRY_RE` (`traversal.py:917-923`) would be
  a one-line blocklist — rejected, because the brief is explicit that the class is
  bigger than one URL and yesterday's word-boundary fix is the same shape of patch.
- `verify_api`'s own shape guard (`traversal.py:497`) stays as-is. Widening
  `_SELECT_OPTION_KEYS` to catch sidley's `{text,value,count}` would be correct, but
  it is a second mechanism chasing the same predicate and sidley is already inert at
  every consumer.
- No content-shape fetch at decision time. `verify_api` *already* fetched the endpoint
  once and threw the evidence away except for `count`/`items_per_page`
  (`graph.py:2393-2399` drops `sample_keys` on the floor). Re-fetching in
  `_derive_strategy` would add a network call to a deterministic node and violate
  constraint 1's spirit.

### Rollback

Revert the predicate's use at consumers A and D (2 sites) — B and C are safe to leave
(they only *restore* gates). `git revert` of the single commit is clean; no schema,
no migration, no persisted shape changes.

---

## P3 — Completeness: stop rehydrating a salvage as authoritative

### Evidence: where job 12's 1-of-6 artifact actually came from

`check_tracker` → `_compute_rescrape_skip_flags` (`webapp/agents/nodes/check_tracker.py:68-110`)
computed `skip_site=True, skip_product=True` (job 12 kept `input_mode`/`search_criteria`
but changed `target_fields` → `skip_code=False`). Then
`check_accessibility` (`graph.py:1492-1499`) took the branch

```python
if state.get("skip_site_analysis"):
    if state.get("skip_product_analysis"):
        if state.get("skip_code_generation"): ... 
        if state.get("scraper_analysis"): ...
        return Command(goto="scraper_analyzer")     # ← job 12 landed here
```

**So `validate_analysis`, `normalize_fields`, and `validate_coverage` never ran at
all.** The 80 % coverage gate (`validate_coverage.py:16,191`) — which would have
measured 1/6 = 17 % and interrupted — was structurally bypassed before it could be
reached. `validate_analysis.py:56` has the same `skip_product_analysis` short-circuit.
Consumer B in P2 was therefore never consulted either; both bypasses are real, and job
12 hit the structural one.

The truncated artifact entered the workspace here —
`setup_workspace._restore_from_archive` (`setup_workspace.py:104-121`):

```python
guarded, note = guard_json_bytes(_bytes)     # → repair_json_text pass 2 (lossy)
...
_bytes = guarded                              # writes the SALVAGE
logger.info("... re-hydrated %s from FM analysis/", filename)
```

`guard_json_bytes` returns a *note* that already names the pass
(`filesystem_tools.py:108-149`); the lossy passes all say **`salvage`**
(`graph.py:318,330,346`), while the lossless ones say `control characters` /
`fixed bad escapes` / `canonicalized on copy`. The distinction is already in the
return value. Nobody consumes it.

### The minimal diff (~15 lines, one function + its one caller)

`webapp/agents/nodes/setup_workspace.py`:

```python
def _restored_from_salvage(note: str) -> bool:
    """Lossy repair (prefix/balanced-closer/truncation salvage) = the artifact is
    valid but INCOMPLETE. Never present that as authoritative; the phase must rerun."""
    return "salvage" in (note or "")
```

In `_restore_from_archive`, extend the existing refusal branch (currently
`if guarded is None`, `:105-111`) so the lossy case refuses the same way:

```python
if guarded is None or _restored_from_salvage(note):
    logger.error("setup_workspace: FM copy of %s not re-hydrated (%s) — "
                 "treating as missing so the phase re-runs", filename, note or "unrepairable")
    return False
```

And in `setup_workspace`, make the skip flag **follow the artifact** (it already runs
*before* `check_accessibility` — `graph.py:4783`, `:4826` — so the flag is still
mutable):

```python
    rehydrated: dict[str, bool] = {}
    if state.get("skip_site_analysis"):
        rehydrated["site_analysis.json"] = _restore_from_archive(slug, workspace_dir, "site_analysis.json")
    if state.get("skip_product_analysis"):
        rehydrated["product_analysis.json"] = _restore_from_archive(slug, workspace_dir, "product_analysis.json")
        rehydrated["navigation_analysis.json"] = _restore_from_archive(slug, workspace_dir, "navigation_analysis.json")
    ...
    cleared: dict[str, Any] = {}
    if state.get("skip_product_analysis") and not (rehydrated.get("product_analysis.json") and rehydrated.get("navigation_analysis.json")):
        cleared["skip_product_analysis"] = False
    if state.get("skip_site_analysis") and not rehydrated.get("site_analysis.json"):
        cleared["skip_site_analysis"] = False        # navigation re-runs too (no skip_navigation flag exists)
    return cleared
```

Navigation has no skip flag of its own; the only route back to `browser_traverse` is
through `skip_site_analysis=False` (`graph.py:1492-1499` → `:1501` onward), so a
missing/salvaged `navigation_analysis.json` necessarily costs a full re-analysis.
That is the honest price of having no usable navigation artifact, and it is paid only
when the artifact is corrupt — the valid-rehydration fast path is untouched.

### Why this is sufficient (mechanism)

Replaying job 12 with this change: both FM artifacts were truncation-salvaged →
neither is rehydrated → both skip flags clear → `check_accessibility` runs the probe →
`site_analyzer` → `browser_traverse` captures fresh (and the P2 gate now polices
whatever it captures) → `product_analyzer` maps 6 target_fields → `normalize_fields` →
`validate_coverage` at 80 % against `target_fields` (`validate_coverage.py:118-120`
already prefers the user's schema). **The job-12 sequence is structurally impossible,
not merely discouraged.**

### Failing tests first

`tests/test_artifact_copy_guards.py` already has the harness
(`test_corrupt_fm_copy_not_rehydrated` at `:239`, `test_repairable_fm_copy_rehydrated_repaired`
at `:250`, `test_valid_fm_copy_byte_identical` at `:257`).

1. `test_salvaged_fm_copy_not_rehydrated` — FM bytes = a truncated JSON that
   `repair_json_text` pass 2 recovers → `_restore_from_archive(...) is False` and the
   workspace file does not exist. **Fails today** (`:250` asserts the opposite).
2. `test_lossless_repairs_still_rehydrated` — control-chars-only and bad-escape
   fixtures → still rehydrated, note logged. Locks the lossless path.
3. `test_salvaged_product_analysis_clears_skip_product_analysis` —
   `setup_workspace(state)` with `skip_product_analysis=True` and a salvaged FM copy →
   returned dict contains `{"skip_product_analysis": False, "skip_site_analysis": False}`.
4. `test_valid_rehydration_keeps_skip_flags` — valid FM copies → returned dict is
   `{}`. Locks the fast path (this is the amn/regression tripwire for P3).
5. Flip `test_repairable_fm_copy_rehydrated_repaired` (`:250`) to the new expectation
   and rename it — recorded here as a **deliberate supersession of M4's**
   *"repairable bytes land repaired"* (`setup_workspace.py:84-85`), per constraint 3.
   M4's goal was stopping *corruption* from becoming durable; this stops *truncation*
   from becoming authority. The unrepairable→quarantine behaviour is unchanged.

### What it deliberately does NOT fix

- **In-run salvage.** `_fix_json_artifact` (`graph.py:394-400`) still writes the
  repaired artifact back into the live workspace and still only logs the note. An agent
  whose own `write_file` truncated mid-object still produces a valid-but-truncated
  artifact. That is accepted because the *consumer* gate (P2's consumer B, restored)
  now measures it against `target_fields` and interrupts at <80 %. Fixing the writer
  too would mean refusing agent writes — a behaviour change F2 deliberately did not
  make (`filesystem_tools.py:23-31`: "the bytes are still written").
- No provenance key, no completeness score, no `_repair` field in artifacts. Zero new
  artifact schema, zero new consumers. The existing `NOTE:` warning
  (`filesystem_tools.py:28-31`) stays the measurement signal.
- `scrapers/{slug}/analysis/` FM bytes are not rewritten; we only decline to copy them.

### Rollback

Revert one commit. `PRESERVE_FILES` / FM keys / artifact shapes unchanged, so a
rollback leaves no residue in the File Master.

---

## P4 — Date bomb: delete the upper bound

### Evidence

`recompute_date_reliability.py:29` —
`FIXED_AT = _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc)` is **midnight**, while
the comment at `:27-28` claims "fix-day end, inclusive". `scraped_at` is
`auto_now_add`, so every row created after 2026-08-27T00:00:00Z is excluded by
`scraped_at__lte=FIXED_AT` (`:55-56`) → `scanned: 0`, exit 0, no error. Both failing
tests confirm it: `tests/test_recompute_date_reliability.py:66` and
`tests/test_admin_recompute.py:63` create rows via `auto_now_add` (now), which is
already past `FIXED_AT`. The admin view calls the *same* command
(`webapp/scraper/admin.py:293`), so one line fixes both.

### The minimal diff (net −7 lines)

Delete `FIXED_AT` (`:29`), its comment (`:25-28`), and the `scraped_at__lte=FIXED_AT`
clause (`:56`). Update the module docstring's window description.

Correctness of dropping rather than widening: the upper bound's only job was "don't
touch post-fix data." Post-fix rows reaching the scan carry
`date_posted_reliable=False`, and the P0-13 arms (`:87-92`) still route
equals-scrape-date and future-dated to `still_unreliable` — untouched. A post-fix row
whose raw date *does* parse and is neither equal-nor-future is a row the fixed parser
can now recover; repairing it is correct, not out of scope. The command becomes
idempotent forever instead of hand-widened a fourth time.

### Failing tests first

1. `test_recompute_date_reliability.py::test_recovers_valid_dates` — already failing.
2. `test_admin_recompute.py::test_apply_fixes_row` — already failing.
3. **New** `test_rows_created_after_fix_day_are_included` — create a row, force
   `scraped_at = now + 1 day` via `JobListing.objects.filter(pk=...).update(scraped_at=...)`
   (the same forced-date trick as `test_outside_window_untouched`, `:109-111`), run
   `--write`, assert it was fixed. This is the test that would have caught the bomb;
   it must fail before the fix and pass after.
4. `test_outside_window_untouched` (`:106`) must stay green — it still guards the
   `BROKEN_FROM` lower bound.

### Rollback

Re-adding a bound is a one-line revert, but there is no reason to: the unbounded form
is strictly more correct.

---

## P5 — Stale re-injection: no new code required (by design)

P5 is fully absorbed by P2 + P3, which is the minimal possible outcome:

- **The poison input was inherited, not freshly captured.** With
  `skip_site_analysis=True`, `check_accessibility` (`graph.py:1492-1499`) can only
  route to `validate_analysis` / `product_analyzer` / `scraper_analyzer` /
  `code_writer` / `code_tester` — **`browser_traverse` is unreachable**, and it is the
  only writer of `navigation_analysis.json` (`graph.py:2414-2427`; the archived
  synthesizer path at `:2708-2725` is commented out and nothing else reads the file
  back into state). So job 12's `data_source: "api"` + useinsider descriptor came off
  disk from jobs 9/10. P2's predicate rejects it at consumers A/B/C/D *regardless of
  how it got there* — rehydrated or fresh.
- **The truncated artifact was inherited, and was made *presentable* by rehydration.**
  P3 refuses the salvage and re-runs the phase.

One optional 1-line hardening, listed separately so it can be dropped without
touching the rest: `_compute_rescrape_skip_flags` (`check_tracker.py:94-97`) computes
`nav_changed` from `input_mode` + `search_criteria` but not `page_type`. A
product→article re-scrape of the same URL would inherit a stale field map while
`target_fields` happened to match.

```python
nav_changed = (
    (state.get("input_mode") or "") != (prior.input_mode or "")
    or (state.get("search_criteria") or "") != (prior.search_criteria or "")
    or (state.get("page_type") or "product") != (prior.page_type or "product")
)
```
Test: `test_page_type_change_reruns_product_analysis` — prior job
`page_type="product"`, new job `page_type="article"`, identical fields/nav →
`skip_product is False`. **Fails today.** (Verify `ScrapeJob` persists `page_type`
before implementing — it does for the intake path via `_build_initial_state`,
`tasks.py:486`.)

Explicitly rejected for P5: an FM mtime/stat freshness check. `src/artifacts.py`
exposes `read`/`exists`/`list_keys` only (`:42-99`) — `exists` does a HEAD and discards
`Last-Modified`. Adding a stat endpoint is new infrastructure (constraint 5) to solve a
problem the predicate already solves.

---

## Rollout order, totals, and the one I'd bet against

| # | Fix | Prod LOC | Test LOC | Risk if wrong |
|---|---|---|---|---|
| 1 | **P4** date bound | −7 | ~25 | none — pure deletion |
| 2 | **P1** rate-limit budget/floor | ~10 | ~50 | low — env-gated, inert at old values |
| 3 | **P2** shared predicate (A, B, C, D) | ~30 | ~120 | medium — touches 4 sites; amn tripwire test #5 |
| 4 | **P3** salvage ≠ authoritative | ~15 | ~70 | medium — flips one existing test |
| — | **P5** page_type in nav_changed | 3 | ~15 | low — optional, independent |

**~51 production lines (net −4 after the P4 deletion), ~280 test lines.** Land P4 and
P1 first: each is independently shippable, and P4 is live-broken in prod right now.

Rollout is web-UI-only (constraint 5): each fix is a single commit on
`file-master-artifacts` → PR → Railway deploy; no FM/data migration, so any fix can be
reverted independently.

**Which minimal fix I suspect is NOT enough: P2's consumer C, and here is the honest
doubt.** The `"items_per_page" not in api_endpoint` provenance test is a *shape
fingerprint* of today's `verify_api` descriptor, not a property of the data. It is
exactly the kind of implicit contract this codebase has been burned by before
(`subagents.py:2208`'s `url` vs `api_url` divergence is in the same object). If
navigate_synthesize ever starts merging `items_per_page` into its LLM-authored
descriptor (`_ensure_api_endpoint_in_analysis` at `navigate_synthesize.py:209-215`
already overlays arbitrary keys from findings), amn silently loses its PREFERRED-API
hint and drops back to browser scraping — a slow-but-working degradation, not an
outage, and test #5 will catch it the moment the shape changes. But a *silent*
regression of a protected site is exactly what constraint 2 forbids, so if the critic
allows one non-minimal move, it should be to make provenance explicit: have
`_capture_api_from_session`'s descriptor carry `"source": "verify_api"` at
`graph.py:2393-2399`, and key consumer C on that string instead of on the absence of a
key. It is ~4 extra lines to buy a real contract instead of a fingerprint.

Secondary doubt, stated for the record: P1 leaves exhaustion job-fatal, and the
pause-for-approval alternative rests on a resume path (`tasks.py:410`) that has never
been tested after a mid-node exception. If the reviewer wants the job to survive a
*genuine* provider outage rather than just outlast a rate-limit window, that test has
to be written first — the one-elif diff is the easy part.
