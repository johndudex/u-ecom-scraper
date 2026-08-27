# Job 12 fix plan — Planner 6 of 6: REGRESSION SAFETY (first, do no harm)

> Lens: **the system currently completes jobs. Every change must prove it preserves that.**
> Evidence base: `docs/plans/job12-context-brief.md` (P1–P5, constraints 1–7).
> Method: for each area, walk the code paths that *currently succeed*, enumerate what the
> fix could disturb, then choose a mechanism that is **structurally incapable** of touching
> the good path (shadow-first / additive-narrowing / warning-first), with an env kill switch
> reachable from the Railway dashboard (constraint 5: web-UI-only deploys).

---

## 0. The one structural fact the other plans miss

The failure chain has a **precondition that is itself a bug**, and it is the cheapest,
safest thing in this plan to fix.

`_invoke_agent_with_timeout` (`webapp/agents/graph.py:1673`) exists precisely so an LLM
exception never kills the graph: on the sync path it catches everything from the agent
thread and converts it to `{"_error": ...}`, and `_run_budgeted_agent` treats that as
"no artifact" → budget-escalation **interrupt** → the user is asked.

Four LLM nodes bypass it entirely and call `agent.invoke(...)` raw:

| node | line | protected? |
|---|---|---|
| `code_tester` | `graph.py:3716` | **NO** — the file's own comment admits it ("this site uses raw agent.invoke with NO timeout wrapper — pre-existing") |
| `cleanup` | `graph.py:3934` | NO |
| `skill_learner` | `graph.py:3978` | NO |
| `dagster_converter` | `graph.py:4069` | NO |

`_invoke_code_tester` has no `except`, so the 429 walked: react loop → node →
`stream_graph` (`webapp/scraper/tasks.py:411`) → `except Exception: … raise`
(`tasks.py:422`) → Celery FAILED. Job 12's 429 was fatal **only because of this bypass**.
Had the identical 429 fired inside `site_analyzer`, the job would have paused at an
approval interrupt with 0 items lost.

Every P1 fix below is designed around this: **make provider errors non-fatal by routing
them into machinery that already exists and is already exercised**, rather than inventing
a new pause/resume subsystem (constraint 5 forbids new infrastructure).

---

## 1. P1 — 429 / provider-error retry policy

### 1a. At-risk working behaviours (what could a longer ladder break?)

The retry already exists and already works. `webapp/agents/llm.py:159-217` +
`settings.py:218-222`:

- `LLM_RETRY_RATELIMIT_MAX = 3`, `LLM_RETRY_TRANSIENT_MAX = 2`,
  `LLM_RETRY_BACKOFF_BASE = 1.5`, `LLM_RETRY_BACKOFF_CAP = 30.0`.
- `_backoff_delay` (`llm.py:88`) is **full-jitter**: `uniform(0, min(cap, base*2**attempt))`.

Observed job-12 sleeps 1.6 / 4.2 / 1.7 s = `uniform(0,3)`, `uniform(0,6)`, `uniform(0,12)`
— exactly this ladder, total 7.5 s. So the ladder is *already* exponential-with-jitter; it
is just **too short and can draw ~0 s**.

Working behaviours that a naive "make backoff bigger" fix would disturb:

1. **`_AGENT_INVOKE_TIMEOUT = 900 s` wall (`graph.py:1598`).** Worst-case per-call cost
   today is already `3 × (600 s timeout + 12 s sleep)` ≈ 30.7 min for `code_writer`
   (`CODE_WRITER_LLM_TIMEOUT = 600`), so the wall already fires before the ladder in the
   timeout case. The *specific* hazard of lengthening the ladder is that on the **sync
   path the timeout handler abandons a daemon thread** (`graph.py:1698-1706`) that keeps
   its socket and ~350 K-char context alive until Celery `time_limit` SIGKILLs the worker —
   the documented celery-OOM mode. **Any ladder whose worst-case sleep approaches 900 s
   converts a fast 429 into a worker-killing thread leak.** This is the single biggest
   regression risk in the whole plan.
2. **Beat-periodic jobs and concurrency.** `time.sleep` is blocking under prefork. A long
   ladder occupies a worker slot; a site that hits a few 429s and today recovers in 8 s
   would instead hold the slot.
3. **Sites that *rely* on the short ladder succeeding.** Every constraint-2 site that has
   ever hit one 429 and recovered on attempt 2 currently succeeds in <5 s. A longer ladder
   does not break correctness for them — only latency — but the *sleeping* site is also
   the site most likely to be near the wall.
4. **`retry_after` honouring (`llm.py:75-85`).** Capped at 60 s. If z.ai ever sends a real
   `Retry-After`, a large ladder would multiply it (`3 × 60 s`). Keep the cap.

### 1b. Fix 1a — widen the ladder by CONFIG ONLY (SHIP, no shadow)

Numbers, not code. `_backoff_delay` and `_handle_retry` are untouched.

| setting | now | new | why |
|---|---|---|---|
| `LLM_RETRY_RATELIMIT_MAX` | 3 | **6** | job 12 saw 4× 429 in an 8 s burst; 3 attempts could not survive 4 |
| `LLM_RETRY_BACKOFF_BASE` | 1.5 | **2.0** | pushes attempt-6 ceiling to 64 → capped at 30 |
| `LLM_RETRY_BACKOFF_CAP` | 30.0 | **30.0 (unchanged)** | keeps worst case far below the 900 s wall |
| **new** `LLM_RETRY_BACKOFF_FLOOR` | — | **1.0** | `uniform(0, X)` can legally return ~0.0 s → an instant guaranteed-repeat 429. Floor makes every retry do *something*. |

New `_backoff_delay`: `uniform(FLOOR, min(CAP, BASE * 2**attempt))`.

**Worst-case budget proof (the non-interference argument):** 6 attempts × ≤30 s sleep =
≤150 s of sleep per LLM call, on a call that has *already failed fast* (a 429 carries no
600 s wait). 150 s is 17 % of the 900 s wall and does not compound with the
timeout-parallel case because a 429 call never enters the 600 s wait. Timeout-class
retries are unchanged (`TRANSIENT_MAX=2`), so the timeout worst case is byte-identical to
today.

**Kill switch:** `LLM_RETRY_RATELIMIT_MAX=3`, `LLM_RETRY_BACKOFF_BASE=1.5`,
`LLM_RETRY_BACKOFF_FLOOR=0` (floor 0 ⇒ exact old `uniform(0, X)`) → all three in the
Railway worker's Variables tab. Behaviour is then bit-identical to `36a91f0`.

**Would this alone have saved job 12?** 4 429s over 8 s. With 6 attempts and a ≥1 s floor
the client would have kept trying for ~30-90 s — comfortably past a per-minute burst
window. Yes, with high probability.

### 1c. Fix 1b — make code_tester/cleanup/skill_learner provider errors non-fatal

**SHADOW FIRST, then enforce.** This changes four nodes' control flow; it is the only
change here with real blast radius, so it earns a shadow period.

Mechanism — reuse the existing wrapper, do not write a new one:

- **Shadow phase:** wrap the four raw `agent.invoke` calls in a helper that
  `except openai.RateLimitError / _TRANSIENT_ERRORS` →
  `logger.error("provider-error-shadow: %s raised %s — WOULD have paused job %d for approval, continuing to fail", …)`
  → **re-raise**. Identical behaviour, perfect observability: every occurrence is a
  counter of times the enforcement would have changed an outcome.
- **Enforcement phase:** same helper, but returns `{"messages": [], "_provider_error": …}`
  instead of re-raising. `_invoke_code_tester` then falls into its **existing** no-report
  branch (`graph.py:3772-3812`), which already records an honest failure and hands
  `route_after_testing` (`nodes/route_after_testing.py:396-431`) a decision it already
  knows how to make — including the real-items rescue (`_scraper_has_real_items`) so a
  finished extraction is never thrown away.

**Critical detail — do not let this feed the codegen thrash.** `route_after_testing`'s
no-report path retries via `scraper_analyzer` (`MAX_TEST_RETRIES = 2`,
`webapp/agents/constants.py:11`). A provider outage would burn 3 codegen cycles × 900 s
wall ≈ 45 min of the exact thrash job 12 exhibited. So the enforcement branch must set
`interrupt_reason="provider_rate_limited"` and route to `human_approval` *before* the
retry loop, not after. Add `"provider_rate_limited": "validation"` to
`INTERRUPT_TO_APPROVAL_TYPE` (`webapp/scraper/services.py:47`) — unmapped reasons already
default safely to `TYPE_EXECUTION` and log (`services.py:388-390`), so this is additive,
not load-bearing. **`cleanup`/`skill_learner`/`dagster_converter` get the logging wrapper
only, never the interrupt** — they are post-output and must stay able to fail loudly
without adding a new approval surface.

**Kill switch:** `PROVIDER_ERROR_NONFATAL=off|shadow|enforce` (default `shadow`).
`off` = the wrapper is not even installed.

### 1d. Rollout + e2e

1. Ship 1a alone. Run the toscrape trio + one API site locally (§6). No behaviour change
   is expected on any green job.
2. Ship 1b in `shadow`. Grep Railway worker logs for `provider-error-shadow:` daily for
   5-7 days. Expected: near-zero hits. Any hit is a job that today FAILED.
3. Flip `PROVIDER_ERROR_NONFATAL=enforce` via Railway env (no redeploy of code needed if
   the flag is read lazily — it is, `settings.config(...)` is read at call time in the
   helper).
4. **Rollback trigger (any one):**
   - `llm classified-retry: rate_limit on attempt` with cumulative sleep > 300 s for a
     single agent phase → the ladder is eating the wall; revert `RATELIMIT_MAX` to 3.
   - `_invoke_agent_with_timeout[…]: agent.invoke exceeded 900 s` appearing where it
     previously did not → revert the ladder.
   - any `provider-error-shadow:` hit followed by a job that the user *wanted* to fail
     hard → keep `shadow` indefinitely.
   - `provider_rate_limited` interrupts that users cannot resolve → set `off`.
5. **Prod regression canary:** `grep "classified-retry: rate_limit exhausted"` (job-fatal
   today) and `grep "provider-error-shadow:"`. Both should trend to zero.

### 1e. Tests that change meaning

- **None flip.** `tests/test_llm_provider.py` locks provider routing and breaker
  recording; it never asserts a backoff value or attempt count (grep for
  `RateLimitError|_backoff_delay|RATELIMIT_MAX` in `tests/` returns only the recompute
  file, by coincidence). The retry ladder is **currently untested** — that is the gap.
- **New (failing first):**
  - `test_llm_retry_backoff.py::test_ratelimit_survives_burst_of_4` — drive
    `ClassifiedRetryChatOpenAI._generate` with a patched super that raises
    `RateLimitError` 4× then succeeds; assert success and `attempt == 5`. **Fails at
    `MAX=3`** — this is the TDD lock for 1a.
  - `…::test_backoff_never_below_floor` — seed `random`, assert every delay ≥ floor.
  - `…::test_transient_budget_unchanged` — assert `TRANSIENT_MAX` is still 2 (guards
    against someone "fixing" timeouts the same way and creating the 900 s leak).
  - `…::test_worst_case_ratelimit_sleep_under_wall` — asserts
    `RATELIMIT_MAX * BACKOFF_CAP <= 180` (a *spec* test, not an implementation test: it
    pins the invariant that protects the 900 s wall, and will fail loudly if a future
    change raises either knob).
  - `test_provider_error_nonfatal.py::test_shadow_reraises` and
    `::test_enforce_returns_provider_error` — the flag grammar.

---

## 2. P2 — Poison-endpoint / strategy-gate trust (the real root cause)

### 2a. At-risk working behaviours

The gate (`graph.py:3049-3058`) requires `data_source=="api"` AND a URL AND
`isinstance(items_per_page, int) and > 0` AND `count != 0`. Sites that **currently enter
the `internal_api` branch**, and the evidence each carries:

| site | `count` | `items_per_page` | how the guard treats it |
|---|---|---|---|
| **aya** (26,742 jobs) | 26,955 | >0 | passes on `count>0` |
| **amnhealthcare** | **`null`** | >0 | passes on `count != 0` (cross-domain `api.amnhealthcare.io`) |
| **vistastaff** | — | — | carries `doubleclick.net` as `api_endpoint` (poison class 3, **inert by luck** — the gate did not fire or the strategy was overridden downstream) |
| **lw.com** (Coveo) | **`0` explicit** | >0 | **rejected** by `count != 0` — the reason that guard exists |
| priceline job 12 | `null` | >0 | **wrongly passed** (useinsider) |

**So there is no signal currently in the gate that separates amnhealthcare from
priceline.** `count` cannot be used (both `null`). Domain cannot be used (amn is
cross-domain). That is exactly why the critique rejected those rules, and why any fix must
introduce a *new* signal rather than tighten an existing one.

**The signal already exists and is being thrown away.** `verify_api`
(`experimental/nav_traversal/traversal.py:472-504`) fetches each candidate once, rejects
select-option shapes, and returns `sample_keys = list(items[0].keys())[:15]` — the
response's own field names. It is already captured for **every** candidate, including the
losers (`traversal.py:1141-1146` scores all of them by `(has_count, n_keys)`).

Then `graph.py:2393-2398` projects the winner down to **`{url, count, items_per_page}`**
and drops `sample_keys` on the floor. Job 12's shape evidence was fetched, scored, and
discarded.

Discriminating power, checked against a real amn record
(`tests/fixtures/amn_job_item.json`): keys `jobTitle, location, city, state, payRate,
descriptionLong, datePosted, requiredQualifications, …` → overlap an entity-field
vocabulary on `title`, `location`, `description`, `date/posted`. The useinsider
personalization-info blob is config-shaped (`SiteDir`/`WebAction`/campaign/segment keys) →
no such overlap.

### 2b. The non-interference mechanism — ADDITIVE NARROWING, DEFAULT-PASS ON ABSENCE

Three pieces, each individually incapable of changing a green job.

**(i) Compute the verdict where the evidence already lives** (`traversal.py`, next to
`verify_api`). New pure function `api_shape_verdict(sample_keys, expected_fields)`:
tokenise both sides with the *same* word-boundary splitter `url_looks_like_data_api`
already uses (`traversal.py:940`) — `jobTitle` → `{job,title}`, vocab `title` → `{title}`
→ match. Return `{product_shaped: bool, matched_keys: [...]}`.

Threshold is deliberately left as an **evidence question for shadow**, not a guess:
require `>= 2` distinct matching keys, OR `>= 1` match against the job's own
`target_fields`. Both numbers come from `src/content_types.py:161-249` (core+optional
field names across all 6 domains) plus the user's `target_fields` — no site names, no new
vocabulary.

**(ii) Thread it through without touching any consumer's semantics.** Add
`"product_shaped"` and `"api_shape_matched"` to the projection at `graph.py:2393`.
Every consumer reads named keys (`subagents.py:2676-2700`, `graph.py:3147`, `:3356`);
`_ensure_api_endpoint_in_analysis` (`nodes/navigate_synthesize.py:195-221`) merges unknown
keys through `for k, v in existing.items()` — additive, harmless. **Deliberately do NOT
add `sample_keys` to this dict**: it would reach `build_code_writer_message`
(`subagents.py:346-347`) and change a prompt, i.e. change LLM behaviour on every API site.
The verdict travels, the raw keys do not.

**(iii) Narrow the gate only on an EXPLICIT `False`.**

```python
# graph.py, immediately before the internal_api override
_api_shape = _nav_api.get("product_shaped")
_api_shape_ok = _api_shape is not False   # None/absent → legacy behaviour
```
and add `and _api_shape_ok` to the `if` at `:3054`.

**Default-pass-on-absence is the load-bearing property.** Every `navigation_analysis.json`
written before this deploy — including everything a rescrape rehydrates from the File
Master (§5) — has no `product_shaped` key, so the gate behaves byte-identically to
`36a91f0` for them. A green job can only be affected if *today's* traversal ran the new
code *and* explicitly said `False`. That is a strictly narrower failure surface, and it is
the only kind of narrowing allowed here.

It also means the fix fires on job-12's replay (fresh site, fresh traversal → verdict
present) but never jeopardises a resumed job for an old site.

### 2c. Shadow mode is mandatory, not optional

Because the amn/aya/lw/vistastaff artifacts are the things we must not break, and we
cannot prove the threshold from the repo alone:

- `API_SHAPE_GATE=off|shadow|enforce`, default **`shadow`**.
- `shadow`: compute, then
  `logger.warning("api-shape-gate[shadow]: url=%s product_shaped=%s matched=%s verdict=%s would_have_used=internal_api fallback=%s", …)`
  and **leave `strategy` exactly as the old code chose it**. Zero behaviour change.
- `enforce`: apply `_api_shape_ok`.

**What shadow must show before `enforce`:** for every constraint-2 site that traverses
during the shadow window, `product_shaped=True` on the endpoint the gate picked (aya,
amnhealthcare), and `product_shaped=False` on `doubleclick.net` (vistastaff, poison
class 3 — currently inert, this makes it actively rejected). lw.com is unaffected either
way (`count==0` still blocks first). **If shadow shows any working API site with
`product_shaped=False`, the threshold is wrong — widen the vocabulary, do not ship
enforce.** That is the explicit abort condition.

### 2d. What this does NOT touch (deliberately)

- `_ESCALATION = ["http_requests","http_navigation","playwright","internal_api"]`
  (`graph.py:2912`) — the amplifier that turned one wrong pick into 3 codegen cycles, and
  which ends on `internal_api` so a wrong `internal_api` has nowhere to go. **Left
  alone.** Changing the retry ladder's semantics is a bigger behaviour change than the
  gate itself and is not needed once the entry is correct. Recorded as a known follow-up,
  explicitly not in this plan.
- `url_looks_like_data_api` word-boundary + `_NON_DATA_PATH_RE` (`traversal.py:930-960`) —
  yesterday's critique-hardened fix, untouched (constraint 3). It cannot catch useinsider
  (genuinely has `/api/` segments); we are not trying to make it.
- `_enforce_anti_bot_strategy` (`graph.py:402-460`) — untouched; it already overrides
  `internal_api` → `http_navigation` for anti-bot sites and runs *after* `_derive_strategy`.

### 2e. Rollout + e2e

1. Land with `API_SHAPE_GATE=shadow` (the default). No production behaviour changes.
2. Local e2e (§6) must show, for books/quotes/gutenberg (no API at all): the gate never
   reaches the `internal_api` branch and no `api-shape-gate` line is emitted. For the API
   site: `product_shaped=True`, `internal_api` still chosen, item count within the
   pre-change band.
3. **Golden replay test** — the cheapest and strongest proof: a fixture descriptor with
   useinsider's URL + config-shaped `sample_keys`, asserted to produce
   `product_shaped=False` and, under `enforce`, to fall back to the rendering-cascade
   strategy. Plus the amn fixture asserting `True`.
4. Shadow ≥ 1 week / ≥ 5 traversals. Then flip `enforce` in the Railway dashboard.
5. **Rollback trigger:** any API-strategy site producing 0 items after `enforce` → set
   `API_SHAPE_GATE=shadow` (immediate, env-only) and inspect
   `api-shape-gate[shadow]`-equivalent output. Because `enforce` only ever *removes*
   `internal_api`, the failure signature is "site that used to work now falls back" —
   detectable from one job.
6. **Prod regression canary:** new log line `api-shape-gate` (all modes); plus
   `_decide_strategy: … tried+failed -> escalating to` (the thrash signature, present
   today at `graph.py:2927`). Job 12's shape = 2+ escalation lines. Target: zero.

### 2f. Tests that change meaning

- `tests/test_content_types.py:623-668 test_scraper_analyzer_message_with_navigation`
  **PASSES UNCHANGED.** Its `api_endpoint` dict has no `product_shaped` key →
  default-pass-on-absence → still asserts `internal_api`. This is the test that would have
  flipped under a naive "require positive shape" design, and the reason the design is
  shaped this way. Its `count: 26955` is the aya shape; it is **right** (it locks the
  spec "a demonstrably-returning API wins") and we keep it verbatim.
- **New (failing first under `enforce`):**
  - `test_api_shape_gate.py::test_useinsider_shaped_descriptor_does_not_pick_internal_api`
  - `::test_absent_verdict_preserves_legacy_choice` — the back-compat lock, asserted
    explicitly so a future refactor cannot silently make absence mean `False`.
  - `::test_amn_fixture_is_product_shaped`
  - `::test_sample_keys_not_leaked_into_code_writer_prompt` — pins (ii)'s restraint.

---

## 3. P3 — Artifact completeness (validity ≠ completeness)

### 3a. At-risk working behaviours

The repair ladder (`graph.py:281 repair_json_text`, `:355 _fix_json_artifact`) is
**load-bearing for green jobs**. `_fix_json_artifact` is called as `artifact_fix_fn` from
`_run_budgeted_agent` (`graph.py:1821`, `:1877`) for site/product/navigation, and from
`_invoke_code_tester` (`graph.py:3741`). It converts a truncated write into something
every downstream consumer accepts. Any "refuse to publish salvage" rule would:

- turn today's **successful** priceline-class jobs into missing-artifact → budget
  interrupt → user friction, on sites where the salvage was actually *complete enough*;
- interact with `_enforce_anti_bot_strategy` (`graph.py:402`), which **rewrites and
  rewrites the artifact file on disk** after the repair pass — a provenance marker inside
  the artifact could be clobbered or could leak into that re-write;
- interact with the FM publish path, where `guard_json_bytes`
  (`filesystem_tools.py:108`) is **byte-identical passthrough by design for valid JSON**
  (stable diffs). Mutating the artifact to add provenance would break that invariant and
  change every subsequent copy's hash.

Also load-bearing and easy to break: pass 2 of the ladder is what saved priceline's
*valid* `test_report.json` in job 12 (brief: "job 12's test_report.json valid"). The
ladder is doing its job.

### 3b. The non-interference mechanism — SIDECAR ONLY, never touch the artifact

`repair_json_text` already returns `(repaired_text, note)` and `_fix_json_artifact`
already logs the note (`graph.py:399`). We are one `json.dump` away from full provenance
at zero risk.

- **Write a sidecar** `workspace/{slug}/{name}.salvage.json`:
  `{"salvaged": true, "note": "pass 2: …", "parse_error_pos": N, "top_level_keys": [...],
  "bytes_recovered": X, "bytes_total": Y, "recovered_at": <iso>}`.
- **Never** add a key to the artifact, never rename it, never re-format it, never block
  the write. Files that parse clean on pass 0 (`_fix_json_artifact`'s first `return`, at
  `graph.py:390`) produce **no sidecar and no code path difference at all** — the good
  path is untouched by construction.
- Copy paths (`guard_json_bytes`) are untouched: the sidecar travels as its own FM key and
  is only written on the repair path.

**Completeness scoring = read-time warning, `warn` by default, never a gate.**
New `src/artifact_completeness.py: `completeness(artifact: dict, target_fields: list[str],
output_schema: dict|None) -> {missing_core: [...], coverage: float}`. Called at the two
places that *read* an analysis to hand it onward — `_read_json_artifact` in graph.py and
the message builders' artifact loads — logging
`artifact-completeness: {name} salvaged (pass 2 …) covers 1/6 target_fields, missing: [...]`.
No state change, no routing change, no LLM call (constraint 1: deterministic string ops).

Job 12's exact artifact would have logged
`product_analysis.json salvaged … covers 1/6 target_fields, missing: description,
previous_price, ratings, remarks, scraped_at` — and the operator would have seen the
poisoned input *before* 43 minutes of codegen.

### 3c. Explicitly NOT in this plan

- **"Refusing to publish salvaged artifacts as authoritative" — NO-SHIP.** We have no
  inventory of how many green jobs currently run on salvaged artifacts. Enabling that rule
  today would fail jobs we cannot count. Revisit only after the sidecar has produced a
  real-world salvage rate; if the rate is ~0 among green jobs, a `refuse` mode can be
  added behind `ARTIFACT_COMPLETENESS=refuse` later, still default-off.
- **Changing `parse_partial_json` / the lenient path.** Constraint 4: streaming stays on,
  the lenient path is a fact of life, F2 already makes it safe at write time.

### 3d. Kill switch / rollout

- `ARTIFACT_COMPLETENESS=off|warn` (default `warn`). `off` disables the sidecar *and* the
  scoring log. Single env var, Railway dashboard.
- Rollout order: (1) sidecar, (2) completeness log, both in one PR because the sidecar is
  inert. No shadow period needed — **nothing reads the sidecar**; it is pure telemetry.
- **Rollback trigger:** disk-volume growth from sidecars on healthy jobs → the sidecar is
  being written on the good path, which would indicate pass-0 is failing more than it
  should; investigate, then `off`.
- **Prod canary:** `grep "artifact-completeness"` — should be rare; a cluster of them on
  one site is an early warning of the truncation class returning.

### 3e. Tests that change meaning

- `tests/test_artifact_repair_v2.py` and `tests/test_artifact_fix_coverage.py` lock the
  ladder's outputs. **None flip** — the ladder's return values are unchanged; we only add
  a file write next to it. One *addition* per existing test (assert the sidecar exists
  after a pass-2 salvage, and does **not** exist after a pass-0 clean parse). The pass-0
  negative assertion is the real regression lock here.
- **New (failing first):**
  - `test_artifact_completeness.py::test_salvage_writes_sidecar`,
    `::test_clean_parse_writes_no_sidecar`,
    `::test_completeness_reports_missing_target_fields` — fixture = the actual job-12
    shape: a JSON object with 1 of 6 `target_fields`, prefix-truncated mid-regex.
  - `::test_copy_path_remains_byte_identical_for_valid_json` — already covered by
    `test_artifact_copy_guards.py`; extend with "and the sidecar does not perturb it".

---

## 4. P4 — Date-bomb (trivial, LIVE in prod, ship first)

### 4a. At-risk working behaviours

`FIXED_AT = datetime(2026, 8, 27)` midnight (`recompute_date_reliability.py:29`) with
`scraped_at__lte=FIXED_AT` (`:56`). `scraped_at` is `auto_now_add`, so **every row created
from 2026-08-27 onward is already excluded** — the command scans 0 rows, silently, and
`self.stdout.write` still prints a success banner (`:99`). This is live in prod *right
now*, and the Django admin button (`admin.py`, `admin_joblisting_recompute`) makes it
one click to run a no-op that *reports* success.

The narrow property to preserve: the window must keep **excluding pre-bug rows**
(`scraped_at__gte=BROKEN_FROM`, `:24`, `:55`) — `tests/test_recompute_date_reliability.py:106
test_outside_window_untouched` locks a 2026-07-01 row as untouched, and that lock is
**right**.

### 4b. Fix — far-future sentinel, not "today-derived"

```python
# Deliberately NOT derived from today's date. The previous value
# (datetime(2026, 8, 27)) was "fix day, end-of-day inclusive" and silently
# excluded every row created on/after that day, because scraped_at is
# auto_now_add. Hand-widening it was the bug (3 widens: Aug 25→26→27).
# Rows created after the parser fix are still safe to include: the P0-13
# rules inside the loop (equals_scrape_date / future_dated) already refuse
# them, and the parser is the same one that marked them, so any row this
# widens the window onto is a row the current parser says is recoverable.
FIXED_AT = _dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc)
```

**Monotonicity proof (the non-interference argument):** widening the upper bound can only
*add* rows to the scan, and every added row must already satisfy
`date_posted_reliable=False` + have a parsable raw string + not be equals_scrape_date /
future_dated to be written. Those are exactly the rows the current parser classifies as
recoverable-and-wrong. The change cannot un-fix or corrupt a correct row. The only
behaviour that changes for existing data is "rows get fixed that were silently skipped".

Cost note: the scan grows to all `date_posted_reliable=False` rows since 2026-07-22.
Bounded, `.iterator(chunk_size=500)`, one indexed-ish filter. Acceptable; log
`scanned` (already printed, `:100`) so growth is visible.

**Kill switch:** none needed (a management command, not a runtime path — it cannot affect
a running job). Rollback = `git revert` + web-UI redeploy. Deliberately *not* env-driven,
to avoid adding a knob nobody needs.

### 4c. Rollout + e2e

Ship **first and alone**. It touches zero code in the request path. Verify locally with
`python manage.py recompute_date_reliability` (dry-run) against a DB containing a
post-2026-08-27 row, then the admin button. No e2e scrape job needed.

### 4d. Tests that change meaning

- `tests/test_recompute_date_reliability.py::test_recovers_valid_dates` and
  `tests/test_admin_recompute.py::test_apply_fixes_row` — **currently failing, and both
  are RIGHT.** They lock the spec "recompute includes rows created today" (both fixtures
  build rows via `auto_now_add` = now). **Fix the code, keep the tests verbatim.** This is
  the correct side of the "stale lock vs. locks-the-spec" distinction: the tests were
  never stale; the constant was.
- **New (failing first, and the actual point):**
  `test_recompute_date_reliability.py::test_rows_created_after_the_constant_are_included`
  — create a row, then `JobListing.objects.filter(pk=…).update(scraped_at=now()+1 day)`
  (auto_now_add forces post-create date patching), run `--write`, assert `posted_date` is
  set. **This is the anti-bomb test**: it fails against `FIXED_AT=today` *forever*, not
  just this week, which is what the three hand-widenings failed to prevent.
- `::test_outside_window_untouched` still passes (lower bound untouched).

---

## 5. P5 — Stale-artifact re-injection on resume

### 5a. At-risk working behaviours

`_restore_from_archive` (`webapp/agents/nodes/setup_workspace.py:77-125`) runs only under
`skip_site_analysis` / `skip_product_analysis` / `skip_code_generation` (`:185-193`),
which `check_tracker` sets (`nodes/check_tracker.py:157-159`, `:340-364`). **Every
rescrape of every constraint-2 site goes through here** — that is the point of resume
logic, and it is what keeps re-scrapes cheap (fewer LLM phases). Any freshness gate that
*blocks* rehydration forces a full re-analysis: more LLM calls (against constraint 1),
slower, and a second pass on a site whose first pass already succeeded.

Guard today = `guard_json_bytes` (`setup_workspace.py:103-119`) — validity only, by
design, and M4-correct: unrepairable bytes are quarantined, not restored.

Two specific things a naive "freshness" check would break:
1. **Age is unknowable and age is not staleness.** amnhealthcare's 6-month-old
   `site_analysis.json` is *correct*. Blocking on age breaks every legitimate rescrape.
2. **`site_status` / skip flags are the resume contract.** Silently declining to restore
   would change `route_from_human_approval`'s expectations downstream.

### 5b. The non-interference mechanism — CONSISTENCY, not freshness; WARN ONLY

Add a **read-only comparison** after a successful restore, keyed on the one thing that
genuinely makes an old artifact unable to serve a new job:

```python
# setup_workspace.py, immediately after a successful _restore_from_archive
_consistency_warning(slug, filename, state)
```
It compares the restored artifact's **declared intent** against the **current job's
request** and logs when they disagree:
- `product_analysis.json.target_fields` vs. current job `target_fields`
  (job 12: artifact said 1 field, job asked for 6 → immediate, loud mismatch);
- `site_analysis.json.page_type` / `site_type` vs. current job's `content_type_config`;
- `navigation_analysis.json.api_endpoint.product_shaped` (once §2 ships) vs. nothing —
  just logged for correlation.

Output is a **log line only**:
`rehydration-mismatch: workspace/{slug}/{name} from FM declares target_fields=[...] but job {id} asks [...] — analysis was NOT re-run (ARTIFACT_STALENESS=warn)`.

**Never block, never delete, never re-run.** The rehydrated artifact still lands, the
graph proceeds identically, the job completes exactly as it would have. The only change
is that the operator can see the amplifier working.

This is the same shape as §3: telemetry that is structurally incapable of altering an
outcome, shipping ahead of any enforcement.

### 5c. Explicitly NOT in this plan

- **Blocking rehydration on staleness — NO-SHIP.** It would convert cheap, working
  rescrapes into full re-analyses across the entire constraint-2 site list, and the
  failure mode (a job that now costs 4× more LLM calls and runs 20 min longer) is far
  more damaging than the bug it prevents. Enforcement is only conceivable after §2's
  `enforce` mode has been live long enough that a bad rehydrated `navigation_analysis.json`
  can no longer cause a wrong strategy — at which point the amplifier has no teeth and
  blocking it buys nothing.
- No `freshness` / timestamp field, no new FM metadata. Old artifacts have none, and
  requiring one would break every existing FM object.

### 5d. Kill switch / rollout

- `ARTIFACT_STALENESS=off|warn` (default `warn`).
- Ship with or after §3 (shares the read helper). No shadow phase — it is already
  observe-only by construction.
- **Rollback trigger:** log-volume noise on multi-rescrape sites → `off`. Nothing else
  can go wrong, because nothing downstream reads the line.
- **Prod canary:** `grep "rehydration-mismatch"` on any job that failed after a resume —
  this is the diagnostic that would have explained job 12 in one line instead of two
  forensic passes.

### 5e. Tests that change meaning

- **None flip.** `tests/test_artifact_copy_guards.py` locks `guard_json_bytes`
  passthrough/quarantine; untouched. Rehydration is currently under-tested, so new tests
  are pure additions:
  - `test_rehydration_consistency.py::test_mismatch_logs_but_still_restores` (the
    non-blocking lock — the most important assertion in this section),
  - `::test_matching_artifact_is_silent`,
  - `::test_flag_off_is_silent_and_still_restores`.

---

## 6. Consolidated rollout — what ships together, what waits

| # | fix | mode | blast radius | ships |
|---|---|---|---|---|
| 1 | P4 far-future `FIXED_AT` | on | mgmt command only, zero request path | **day 0, alone** |
| 2 | P1a ladder widening | on | numbers only; worst-case sleep ≤150 s vs 900 s wall | **day 0** |
| 3 | P3 sidecar + completeness log | `warn` | telemetry only, nothing reads it | **day 0** |
| 4 | P5 consistency check | `warn` | telemetry only, never blocks | **day 0** |
| 5 | P2 shape verdict + gate | **`shadow`** | none while shadow; narrows one branch | **day 0 in shadow** |
| 6 | P1b provider-error non-fatal | **`shadow`** | none while shadow; re-raises | **day 0 in shadow** |
| 7 | P2 `enforce` | — | narrows `internal_api` entry | **day 8-14, after shadow evidence** |
| 8 | P1b `enforce` | — | adds an interrupt reason | **day 8-14, after shadow evidence** |

**Nothing that changes an outcome ships without a shadow log proving what it would have
done.** Items 1-4 are provably incapable of altering a job outcome (a management command,
numeric constants inside a bounded envelope, and two log-only paths).

### Local e2e gate before any Railway deploy

`python3 tests/test_e2e_local.py` (existing harness, `tests/test_e2e_local.py:1-40`),
non-Docker, browser stack up. Four runs, each compared against a pre-change baseline run
on the same commit's parent — the comparison, not the absolute number, is the evidence:

| run | `TEST_URL` / `TEST_SLUG` | proves |
|---|---|---|
| A | `https://books.toscrape.com/` | http_requests path unaffected; no `api-shape-gate` lines; `internal_api` never chosen |
| B | `https://quotes.toscrape.com/` | page_content domain unaffected |
| C | `https://www.gutenberg.org/ebooks/search/?q=sherlock` | article domain unaffected |
| D | one API-strategy site (amnhealthcare) | `product_shaped=True`, `internal_api` **still** chosen, item count within the pre-change band, and `API_SHAPE_GATE=shadow` logs the verdict the enforcement would use |

Plus a **no-network unit gate** that runs in CI and needs no browser: the golden-replay
tests in §2e and §3e. Those are what actually pin job 12's shape, and they are the tests
that will catch a regression after everyone has forgotten this incident.

Full-suite gate: `719 passed / 0 failed / 2 skipped` — i.e. **the 2 P4 failures must go
green and nothing else may move.** Any other movement is a meaning-flip that needs an
explicit justification in the PR.

### Kill switches (all Railway-dashboard env vars, no CLI)

| switch | values | reverts to |
|---|---|---|
| `LLM_RETRY_RATELIMIT_MAX` / `_BACKOFF_BASE` / `_BACKOFF_FLOOR` | `3` / `1.5` / `0` | `36a91f0` ladder, bit-identical |
| `PROVIDER_ERROR_NONFATAL` | `off` (default `shadow`) | raw `agent.invoke`, exception propagates → FAILED |
| `API_SHAPE_GATE` | `off` / `shadow` (default `shadow`) | `36a91f0` gate |
| `ARTIFACT_COMPLETENESS` | `off` (default `warn`) | no sidecar, no log |
| `ARTIFACT_STALENESS` | `off` (default `warn`) | no log |

Every switch is read lazily via `config(...)`/`getattr(settings, …)` exactly as
`_retry_settings()` (`llm.py:62`) and `_async_execution_enabled()` (`graph.py:1613`)
already do, so a Railway variable change takes effect on worker restart without a code
deploy.

### Detecting a prod regression fast

| symptom | log line to grep | where |
|---|---|---|
| a 429 killed a job again | `llm classified-retry: rate_limit exhausted` | worker |
| the ladder is eating the wall | `_invoke_agent_with_timeout[…]: exceeded 900 s` | worker |
| the gate would have / did block | `api-shape-gate[…]` | worker |
| wrong-strategy thrash returned | `_decide_strategy: … tried+failed -> escalating to` (≥2 per job) | worker |
| truncated artifacts returning | `artifact-completeness:` and `salvage` notes | worker |
| the resume amplifier is active | `rehydration-mismatch:` | worker |
| a job failed where shadow said we'd have paused | `provider-error-shadow:` | worker |
| a job silently scanned 0 rows (P4 returned) | `scanned (broken window, reliable=False): 0` | `recompute_date_reliability` output, Django admin message |

---

## 7. Verdicts

| fix | verdict | the one-line reason |
|---|---|---|
| P1a ladder widening | **SHIP** (no shadow) | numbers inside a proven ≤150 s envelope against a 900 s wall; alone it would very likely have saved job 12 |
| P1b provider-error non-fatal | **SHADOW, then ship** | it changes four nodes' control flow; the shadow log makes the decision evidence-based |
| P2 shape verdict + gate | **SHADOW, then ship** | default-pass-on-absence makes it non-interfering, but the threshold is an evidence question only traffic can answer |
| P3 sidecar + completeness log | **SHIP** (no shadow) | nothing reads the sidecar; pass-0 clean parses take an untouched path |
| P3 refuse-to-publish salvage | **NO-SHIP** | no inventory of green jobs running on salvage; would fail jobs we can't count |
| P4 far-future `FIXED_AT` | **SHIP, first, alone** | zero request-path exposure; live in prod right now |
| P5 consistency warning | **SHIP** (warn-only) | observe-only by construction |
| P5 blocking stale rehydration | **NO-SHIP** | converts every cheap working rescrape into a full re-analysis |
| `_ESCALATION` ladder change | **NO-SHIP** | bigger behaviour change than the gate, not needed once the entry is correct |

### Too risky to fix now (say it plainly)

1. **Refusing to publish salvaged artifacts** (P3). The premise — that salvage is always
   worse than the truth — is unproven, and the blast radius is "every job whose artifact
   needed repair, for the first time, in prod". Ship the provenance, decide later.
2. **Blocking stale rehydration** (P5). Its cost is paid by every working rescrape and its
   benefit disappears once P2 enforcement is live (the amplifier then has no teeth).
   Pure downside until then.
3. **Rewriting the `_ESCALATION` retry ladder** (P2-adjacent). It is genuinely part of the
   thrash — it terminates at `internal_api`, so a wrong `internal_api` has no escape — but
   changing it alters retry semantics for every failing site in the constraint-2 list, and
   `MAX_TEST_RETRIES = 2` was itself a deliberate cut. Fix the entry, not the ladder.
4. **`_invoke_code_tester`'s missing wall-clock wrapper in full** (P1b enforcement). Worth
   doing, but note it fixes *hangs* too — meaning its first enforcement day could surface
   900 s walls on sites that currently "pass" by finishing slowly inside an unprotected
   call. That is precisely why it ships behind a shadow flag rather than as a drive-by
   hardening.
