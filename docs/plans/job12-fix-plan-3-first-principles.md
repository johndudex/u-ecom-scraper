# Job 12 fix plan — Planner 3: FIRST-PRINCIPLES / CHALLENGE THE PREMISES

> Lens: for each of P1–P5, ask whether the brief's proposed fix direction treats a symptom.
> State the invariant that actually matters, then pick the most direct mechanism that
> enforces it. Every claim below is grounded in a file:line read on 2026-08-27.
>
> Companion docs: `job12-context-brief.md` (evidence), `job12-fix-plan-1-*`, `-2-*` (other lenses).

---

## 0. The one-line diagnosis the brief understates

Job 12's root cause is not "a bad URL got through a heuristic." It is that **one
unverified string — `navigation_analysis.api_endpoint.url` — is consumed by four
independent decision points, each of which re-derives trust from scratch, and
each of which degrades the run in a different direction.**

| # | Consumer | File:line | What it does with the string | Job-12 effect |
|---|----------|-----------|------------------------------|---------------|
| 1 | Strategy gate | `webapp/agents/graph.py:3051-3058` | `data_source=="api"` + url + `items_per_page>0` + `count!=0` → `strategy="internal_api"` | Wrong strategy |
| 2 | code_writer API section | `webapp/agents/subagents.py:2679` | **Presence of `url` alone** → "CRITICAL … do NOT use Playwright/Selenium, do NOT parse the DOM" | Overrode the strategy field even if #1 were fixed |
| 3 | product_analyzer api_hint | `webapp/agents/subagents.py:1452` | `data_source=="api"` + url → "do NOT browse the page" | Field map starved of page evidence |
| 4 | Coverage-gate exemption | `webapp/agents/nodes/validate_coverage.py:181-189` | `api_url` present → **skips the target_fields coverage gate entirely** | 1-of-6 fields sailed through at 17% |

Three of the four check *more* than presence; one (#2) checks **nothing but
presence**. Any fix that only patches the strategy gate leaves #2, #3 and #4
poisoned. **The fix must land where the string is born, not at any consumer.**

A fifth finding, and the cheapest one in this plan: `verify_api` already fetches
the endpoint body and extracts `sample_keys` (`traversal.py:495`) — and
`graph.py:2393-2396` **deliberately throws `sample_keys` away** when building
`navigation_analysis.api_endpoint`, keeping only `url`, `count`, `items_per_page`.
The evidence that would have exposed useinsider was captured and discarded at the
state boundary.

---

## P1 — 429 / provider-error retry policy

### Premise challenged

The brief frames P1 as *"need exponential/jittered backoff scaled to provider rate
limits; decide whether rate-limit exhaustion should fail the job or pause/resume."*
That framing accepts that the fix lives inside `llm.py`.

**That is the wrong layer.** Three facts make it so:

1. **The retry ladder is already jittered exponential** (`llm.py:88-91` — full-jitter
   `uniform(0, min(cap, base*2**attempt))`, base 1.5, cap 30) and already honors
   `Retry-After` (`llm.py:75-85`). Job 12's 1.6/4.2/1.7s sleeps are *that ladder
   working as designed*. Z.AI's `code 1302` carries **no `Retry-After` header** —
   the code falls to `_backoff_delay`, which cannot know that a per-minute quota
   window needs 30–60s, not 2–4s. Tuning the ladder cannot fix a window it cannot see.
2. **`code_tester` is the only LLM phase with an unprotected invoke.**
   `graph.py:3715-3718` calls `agent.invoke(...)` raw — no `_invoke_agent_with_timeout`,
   no `{"_error": ...}` boxing. Every other phase goes through
   `graph.py:1694-1711`, which catches `Exception` and returns `{"_error": str(exc)[:200]}`.
   The 429 therefore escaped the node, escaped the graph, and landed in
   `tasks.py:141` as a job-fatal exception. The brief's "429 propagated" is
   literally true because of a *missing try/except*, not a retry-policy gap.
3. **The resume machinery already exists and is one status bit away.** On failure
   `tasks.py:179-180` sets `Site.status = "failed"`. But
   `check_tracker._handle_in_progress` (`nodes/check_tracker.py:328-345`) already
   computes `skip_site / skip_product / skip_code` from **surviving workspace
   files**, and `_finalize_job`'s `rmtree` only runs on the *success* path
   (`tasks.py:906`) — so after a 429 the workspace still holds `site_analysis.json`,
   `navigation_analysis.json`, `product_analysis.json`, `scraper_analysis.json`,
   `scraper_draft.py`, and `test_report.json`. **A 42-minute pipeline's work is
   already on disk when the job dies.** The failure handler just flips the one bit
   that would let `check_tracker` find it.

So: the invariant is not "retry harder." It is:

> **A provider-side failure must be indistinguishable, in the resume model, from a
> job that was merely interrupted. Completed pipeline work must never be discarded
> by a failure that has nothing to do with the site.**

And the corollary that decides the backoff question:

> **In-process retry should be short; provider rate-limiting is a job-level wait.**
> A 60s sleep inside an agent's react loop holds a Celery worker slot and inflates
> the 900s wall. The same 60s at the Celery layer costs nothing.

### Chosen mechanism (three layers, ordered)

**L1 — box the `code_tester` invoke (prerequisite, not sufficient).**
`graph.py:3715` → route through `_invoke_agent_with_timeout(agent, messages, config, "code_tester", job_id, timeout=<existing code_tester wall>)`.
On exception the node now sees `{"_error": ...}` instead of an escape. This alone
would have turned job 12's fast fail into a *slow* fail (3 more codegen cycles on
a dead provider) — which is why L1 must not ship without L2.

**L2 — classify provider exhaustion as a job-level requeue.**
- New exception `ProviderRateLimited` (and `ProviderUnavailable` for
  timeout/conn/5xx exhaustion) raised by `_handle_retry` (`llm.py:204-209`) on
  rate-limit budget exhaustion, carrying `attempts`, `total_slept`, `model`.
- `llm.py:_retry_classified_sync/_async` let it propagate (it is a *control-flow*
  signal, not an error to swallow).
- `graph.py:1701-1699` boxing path re-raises `ProviderRateLimited` instead of
  boxing it — a provider outage is not "this phase produced nothing," it is
  "the run cannot continue now."
- `tasks.py:141` failure handler: if the exception (or `error_message`) classifies
  as provider-transient → **do not** set `Site.status="failed"`, **do not** write a
  terminal `error_message`; instead `raise self.retry(countdown=<_backoff>,
  max_retries=1)`. `run_scrape_task` is already `bind=True, max_retries=1`
  (`tasks.py:94-98`) and the same-site guard already uses exactly this idiom
  (`tasks.py:133-137`, `countdown=60, max_retries=None`). Countdown: 90s flat for
  attempt 0 → 600s for attempt 1 → then fail for real.
- On re-dispatch, `check_tracker` sees `Site.status == "in_progress"` (unchanged)
  and falls into `_handle_in_progress`, which reads the surviving workspace and
  resumes. `check_accessibility` reuses the probe cache; `setup_workspace`
  preserves whatever the skip flags name. Job 12's re-dispatch would have skipped
  site analysis, re-hydrated `scraper_draft.py` + `test_report.json`, and gone
  straight back to `code_tester`.

**L3 — small, honest in-process floor (only).**
`_backoff_delay` gains a floor for the `rate_limit` class only: `max(2.0, jitter)`
and cap raised 30 → 60. `LLM_RETRY_RATELIMIT_MAX` stays 3. No new budget, no new
wall-clock. Env-gated (`LLM_RETRY_RATELIMIT_FLOOR`, default 2.0) for rollback.

### Files

| File | Change |
|------|--------|
| `webapp/agents/llm.py` | Add `ProviderRateLimited`/`ProviderUnavailable`; raise from `_handle_retry`; rate-limit backoff floor. |
| `webapp/agents/graph.py` | `:3715` code_tester → `_invoke_agent_with_timeout`; boxing path re-raises provider classes. |
| `webapp/scraper/tasks.py` | `:141` classify + `self.retry`; skip `Site.status="failed"` for provider class; set `job.error_message` to a non-terminal "provider pause" note. |
| `webapp/config/settings.py` | `LLM_RETRY_RATELIMIT_FLOOR`, `LLM_RETRY_RATELIMIT_CAP`, `JOB_PROVIDER_RETRY_MAX`. |
| `webapp/scraper/models.py` | No new field — reuse `error_message` text convention (avoids a migration on Railway web-UI-only deploys). |

### Failing tests first

1. `tests/test_llm_provider.py::test_ratelimit_exhaustion_raises_provider_rate_limited` —
   after 3 rate-limit attempts, `_retry_classified_sync` raises `ProviderRateLimited`
   (today: re-raises the raw `openai.RateLimitError`). **RED first.**
2. `tests/test_llm_provider.py::test_ratelimit_backoff_floor` — no `Retry-After`
   header → every `rate_limit` delay ≥ floor. (Today 1.6s < 2.0 floor. RED.)
3. `tests/test_code_tester_invoke_guard.py::test_provider_error_does_not_escape_code_tester_node` —
   stub `create_code_tester` to raise `ProviderRateLimited`; assert it propagates
   as `ProviderRateLimited`, **not** as a job-fatal raw exception, and that
   `_stop_heartbeat` still ran.
4. `tests/test_job_provider_requeue.py::test_provider_failure_does_not_flip_site_to_failed` —
   run `run_scrape_task` with the graph raising `ProviderRateLimited`; assert
   `Site.status == "in_progress"`, `job.status` unchanged/pending-retry,
   `workspace/{slug}/site_analysis.json` still on disk.
5. `tests/test_job_provider_requeue.py::test_provider_resume_reuses_workspace_artifacts` —
   seed a surviving workspace + `in_progress` site, re-dispatch, assert
   `check_tracker` sets `skip_site_analysis=True` and `setup_workspace` preserves
   `scraper_draft.py`.
6. Regression guard: `test_non_provider_failure_still_flips_site_to_failed` — a
   genuine site-side exception (e.g. `RuntimeError("boom")`) **must** still fail
   the job. This is the guard that keeps L2 from laundering real failures.

### Decided AGAINST

- **"Bigger in-process backoff ladder" (the brief's headline).** Rejected as the
  primary fix. It cannot see the quota window (no `Retry-After`), it blocks a
  worker slot while it sleeps, and it pushes the 900s wall. 3 attempts × 60s = 3
  minutes of a held worker to still fail the job.
- **Pause/`WAITING_APPROVAL` for provider errors.** Rejected. `WAITING_APPROVAL`
  requires a pending LangGraph interrupt; a 429 leaves none
  (`_graph_is_interrupted` returns False). Synthesizing a fake approval row to get
  there adds a user-visible gate to a problem the user cannot help with, and
  re-introduces exactly the "user was never asked" confusion the brief flags.
- **Classifying on `error_message` string-matching as the primary signal.** Used
  only as a fallback. String matching is how ketchcdn got through; typed
  exceptions are the fix.
- **`max_retries=None` (unbounded requeue).** Rejected — an out-of-quota account
  would retry forever. `max_retries=1` (already on the decorator) = 2 total
  dispatches, then honest failure.

### Rollback

Kill-switches, all additive: `LLM_CLASSIFIED_RETRY=False` restores pre-Phase-2
`ChatOpenAI` (existing). New `JOB_PROVIDER_REQUEUE=False` makes `tasks.py` treat
`ProviderRateLimited` like any other exception (full restore of today's behavior
in one env var). L1's boxing is reverted by a one-line change at `graph.py:3715`.

---

## P2 — Poison-endpoint / strategy-gate trust

### Premise challenged

The brief offers three directions: *content-shape verification*, *better URL
heuristics*, or *demote the selector / let corrected product_analysis win*.

**"Better URL heuristics" is dead on arrival** and the brief half-knows it:
`url_looks_like_data_api` (`traversal.py:953`) has a hard `if "/api/" in lowered:
return True` fast path. `pricelineau.api.useinsider.com/api/info/824.24` matches
it on the first branch. There is no URL-shaped rule that separates useinsider
from `api.amnhealthcare.io` — both are cross-domain `/api/` paths. Any further
blocklist (add `useinsider`) is site-specific and violates the genericity constraint.

**"Content-shape verification" is already implemented and already insufficient.**
`verify_api` (`traversal.py:472-502`) *does* fire a GET and *does* inspect the
parsed body. It requires a JSON object containing a list of dicts, and rejects
dropdown-shaped key sets. Useinsider's `/api/info/` returned exactly that — a JSON
object with arrays of objects (visitor/experiment metadata). Shape is the wrong
predicate. **A personalization beacon is well-shaped JSON about the wrong subject.**

**"Let product_analysis win" is the most seductive and the most dangerous.**
The brief asks: who should win when two authoritative inputs disagree? But this
framing has a false premise — **they are not peers.** `product_analysis` is an
LLM artifact (stochastic, salvage-repairable, and in job 12 itself truncated to
1-of-6 fields); `navigation_analysis` is captured from a live browser session.
Making a truncated LLM artifact outrank an instrument reading would have made
job 12 *worse* on a different day. And the disagreement is unresolvable in
principle: `scraper_analyzer` is deterministic *by constraint 6*, so "who wins"
must be decided by evidence, not by rank.

### The invariant that actually matters

> **An endpoint may only be declared the site's data source if the records it
> returns are *about the requested subject*.** Shape and location are
> pre-filters. Subject relevance is the verdict.

And the mechanism invariant:

> **Compute the verdict once, at capture time, into the descriptor itself. Every
> consumer gates on the verdict; no consumer re-derives trust from the presence
> of a URL.**

Relevance is checkable for free: `verify_api` already holds `sample_keys`
(`traversal.py:495`), and the content-type registry already holds the field
vocabulary (`src/content_types.py:161` `core_field_names`, `:162`
`optional_field_names`, plus `PRODUCT_FIELDS`/`ARTICLE_FIELDS`/`JOB_FIELDS`
`jsonld_key`s). The test is **key overlap, not similarity** — deterministic, no
LLM, no network, no cost.

### Chosen mechanism

**Step 1 — pass `sample_keys` through the state boundary (one-line bug fix).**
`graph.py:2393-2396` → add `"sample_keys": result.api.get("sample_keys")`.
Today it is captured and dropped.

**Step 2 — new pure function `classify_api_endpoint(api, content_type, query)`**
in `experimental/nav_traversal/traversal.py` (same module the graph already
imports from at `graph.py:2156` and `:2222`; no new infra). Returns a verdict dict
attached to the descriptor:

```python
{
  "url": ..., "count": ..., "items_per_page": ..., "sample_keys": [...],
  "verdict": "data" | "irrelevant",
  "verdict_reason": "0/15 sample keys overlap product vocabulary",
  "key_overlap": ["price", "description"],     # the intersection, for debugging
}
```

Rules, all deterministic:
- Normalize both sides (`lower`, strip `_`/`-`/` `; match `jsonld_key` leaf names,
  e.g. `offers.price` → `price`).
- `overlap = normalized(sample_keys) ∩ normalized(vocabulary)` where vocabulary =
  `core_field_names ∪ optional_field_names ∪ direct_field_names ∪ {jsonld_key leaves}`.
- **`verdict="data"` requires `len(overlap) >= 1`.** One genuine overlap is the
  weakest bar that still excludes a personalization beacon, and the *strongest*
  bar that cannot false-reject a real catalog API (every real product/job/article
  API returns at least a title, a price, a date, or a URL).
- Cross-domain is still allowed (amnhealthcare). `count is None` is still allowed
  (amnhealthcare). Only relevance is added.
- `query` terms participate as a secondary overlap source (an `aya`-style API whose
  keys are opaque but whose URL carries the query token still passes on URL+count
  grounds — see Step 3).

**Step 3 — gate the mechanism, not just the strategy.**
`traversal.py:_pick_mechanism:656-658` and `:1971` — `api` contributes `mechanism="api"`
only when `verdict == "data"`. Otherwise the traverser reports `detail_links` /
`browser_llm` and `data_source` is never `"api"` at all. This is the source fix:
consumers #1–#4 above all key off `data_source == "api"` or the descriptor, and
all four now see the corrected value with **no per-consumer change required.**

**Step 4 — the one consumer that must change: `subagents.py:2679`.**
It gates on bare URL presence. Change to require `data_source == "api"` **and**
`verdict == "data"` (same predicate as #1). The "CRITICAL — do NOT use Playwright"
section then cannot fire against a rejected endpoint. Without this, fixing #1
leaves code_writer being told to use useinsider by a *stronger* instruction than
the strategy field.

**Step 5 — make the disagreement visible instead of silent.**
When `verdict == "irrelevant"`, `graph.py` writes the descriptor anyway with
`strategy_justification` extended: `"api endpoint rejected: <reason>"`. This costs
nothing and turns the next forensic pass from a week of GraphQL archaeology into a
`grep`.

### Why not also flip to product_analysis on disagreement

Explicitly rejected, and worth recording *why*: the strategy gate is
deterministic and constraint 6 forbids de-determinism. The correct resolution of
"two authoritative inputs disagree" is not precedence — it is **removing the
input that had no evidence**. After Step 2/3 there is no disagreement to resolve:
the gate and `product_analysis` agree because the bogus input is gone. Job 12 had
*no* disagreement to adjudicate; it had one instrument reading a beacon.

### Files

| File | Change |
|------|--------|
| `experimental/nav_traversal/traversal.py` | `classify_api_endpoint()`; `verify_api` returns `sample_keys` (already does); `_pick_mechanism` + the `:1971` return gate on verdict. |
| `webapp/agents/graph.py` | `:2393` pass `sample_keys` + verdict; `strategy_justification` records rejection. |
| `webapp/agents/subagents.py` | `:2679` api_section gate; `:1452` api_hint gate (same predicate). |
| `webapp/agents/nodes/validate_coverage.py` | `:181` exemption gate on `verdict == "data"`. |
| `src/content_types.py` | Expose `field_vocabulary(content_type) -> frozenset[str]` (pure helper over existing data). |

### Failing tests first

1. `tests/test_api_endpoint_verdict.py::test_useinsider_info_is_irrelevant` —
   `classify_api_endpoint({"url":"https://pricelineau.api.useinsider.com/api/info/824.24",
   "count":None,"items_per_page":4,"sample_keys":["em","os","ar","ug","vi","bs","sd","pr"]},
   "product", "gifts")` → `verdict == "irrelevant"`. **RED (function absent).**
   This is the job-12 regression lock.
2. `::test_doubleclick_inert_is_irrelevant` — vistastaff's `doubleclick.net`
   descriptor → `"irrelevant"` (poison class 3, currently "inert by luck").
3. `::test_amnhealthcare_cross_domain_null_count_is_data` — cross-domain,
   `count=None`, keys include `title`/`location`/`company` → `"data"`.
   **Constraint-2 site.**
4. `::test_coveo_explicit_zero_count_is_data` — `count=0` must not be flipped to
   irrelevant *by this function*; the existing `count != 0` guard at `graph.py:3056`
   remains the sole owner of that decision. **Constraint-2 site (lw.com).**
5. `::test_aya_taxonomy_vs_jobs_api_ranking_unchanged` — the aya taxonomy
   (`joblookups`, 2217 records, 3 keys) still loses to `/job/search` (90+ keys);
   `_capture_api_from_session._score` unaffected.
6. `tests/test_strategy_gate.py::test_irrelevant_endpoint_does_not_select_internal_api` —
   `navigation_analysis` with an irrelevant verdict → `strategy != "internal_api"`,
   `data_source != "api"`.
7. `tests/test_strategy_gate.py::test_api_section_absent_for_irrelevant_endpoint` —
   `build_code_writer_message` output contains no `"CRITICAL — Backend JSON search API"`.
8. `tests/test_strategy_gate.py::test_coverage_gate_runs_for_irrelevant_endpoint` —
   `validate_coverage` with an irrelevant endpoint and 17% target_fields coverage →
   routes to `human_approval` with `interrupt_reason == "low_coverage"`, not to
   `scraper_analyzer`.
9. `tests/test_strategy_gate.py::test_samples_keys_survive_state_boundary` —
   `graph.py`'s `analysis["api_endpoint"]` carries `sample_keys` and `verdict`.

### Decided AGAINST

- **Extending `_TELEMETRY_RE` / `_NON_DATA_PATH_RE` with `useinsider`.** Site-
  specific; the class is unbounded (the brief already found 3 distinct poison
  shapes). Every future vendor is a new prod incident.
- **Fetching the endpoint *again* at gate time.** `verify_api` already fetched it
  once; a second GET at strategy time doubles latency and adds a failure mode for
  zero new information.
- **`transferSize`/`encodedBodySize` from `performance` entries.** Available in
  `_NETWORK_JS`'s data for free, but cross-origin entries report 0 without
  `Timing-Allow-Origin` — and data APIs are usually cross-origin. A signal that
  is 0 exactly when we need it.
- **LLM endpoint validation.** Constraint 1 forbids it; also unnecessary.
- **Demoting to `strategy_justification`-only and letting the LLM read it.** The
  whole point is that a prompt hint lost to `internal_api` three times in a row
  in job 12's cycle 3 (49 tool calls, zero writes). Prompt hints are not gates.

### Rollback

`classify_api_endpoint` returns `"data"` unconditionally under
`NAV_API_VERDICT_DISABLED=1` (env). The `sample_keys` pass-through is inert
addition. Consumer gates are each a one-line revert. No migration, no new service.

---

## P3 — Artifact completeness (validity ≠ completeness)

### Premise challenged

The brief's idea space is *completeness scoring vs. target_fields/schema, salvage
provenance marking, read-time warnings, refusing to publish salvaged artifacts as
authoritative*.

**"Completeness scoring" is the wrong shape.** This project already rejected a
scoring gate once (the 25%-of-prior regression gate, per constraint 2) because a
threshold produces false FAILs. A completeness score is the same trap: pick 0.5
and you fail `scope=firstn` samples; pick 0.2 and you pass job 12's artifact.
Scores need tuning; tuning needs incidents.

**The premise worth attacking is subtler: the brief treats "refuse to publish" as
one option among four. It is the only correct one, and it is already half-built.**
`_fix_json_artifact` (`graph.py:355-410`) already has two tiers: *repair in place*
and *rename to `.corrupt` so downstream treats it as missing*. The bug is that the
ladder's pass-2/2b/3 salvage outputs land in tier 1 — **a lossy, truncating
transform is treated as a lossless repair.** Job 12's `product_analysis.json`
parses clean, is canonical `indent=1`, ends mid-regex with balanced closers, and
contains 1 of 6 requested fields. Nothing in the pipeline can tell it apart from
a complete analysis.

So the invariant is not "score completeness":

> **A salvage-repaired artifact is never authoritative. It is usable evidence, and
> every consumer must opt in to using it.**

Provenance is the mechanism; and the completeness check is not a score but a
**set-membership test against `target_fields`** — which this codebase already
computes.

### Chosen mechanism

**3a — durable salvage provenance (sidecar, not in-band).**
`repair_json_text` already returns `(text, note)` where the note names the pass
(`graph.py:318`, `:330`, `:346`). `_fix_json_artifact` logs it and discards it.
Change: when `note` is non-empty, write `workspace/{slug}/{name}.repair.json`:

```json
{"salvaged": true, "pass": "pass 2b", "note": "...", "at": "<iso8601>",
 "artifact_sha256": "...", "recovered_top_level_keys": 3}
```

Sidecar, not a top-level `"__salvage"` key: an in-band key leaks into
`normalize_fields`'s iteration, into `_prune_output_to_schema`'s key filter, into
every `for k in analysis` in the tree, and into the FM artifact partners read. A
sidecar is invisible to every existing consumer until it asks.

Same for the copy path: `guard_json_bytes` (`filesystem_tools.py:107-160`) returns
`(bytes, note)`; callers currently log `note` and drop it. It gains an optional
`provenance_path=` so FM→workspace re-hydration writes the sidecar too.

**3b — one read helper, and gate the *optimizations* on it.**
`artifact_is_salvaged(slug, filename) -> bool` (reads the sidecar).

Then — and this is the load-bearing move — **do not fail the job. Gate the skip.**
`check_tracker`'s three `os.path.isfile(...)` calls
(`nodes/check_tracker.py:329-331`) become `artifact_is_reusable(slug, name)`,
where reusable = `isfile AND not salvaged`. A salvaged `product_analysis.json`
therefore looks *missing*, which routes into the existing re-analysis path. **No
new failure mode; one optimization is withdrawn.** This is why it cannot
false-FAIL anything, and why it is a strictly better instrument than a score.

**3c — kill the completeness *score*, keep the completeness *predicate*.**
`validate_coverage` already computes exactly the right thing — `core =
set(target_fields)` (`validate_coverage.py:118-120`), `covered = extracted & core`
(`:158`). No new scoring. The only P3 change here is 3d.

**3d — make `skip_code_generation` respect the gates it bypasses.**
`validate_coverage.py:84-90`: `if skip: goto="code_tester"`. **A resume flag
unconditionally bypasses the coverage gate.** This is the exact structural hole
that let job 12's 1-of-6 artifact through even though the gate existed, was
correct, and used the right target fields. Change: `skip` bypasses the *retry
loop*, not the *measurement* — compute coverage; if it is below `MIN_COVERAGE`,
take the existing `low_coverage` path regardless of `skip`. This is a one-branch
change and it closes the amplifier without any new machinery.

### Files

| File | Change |
|------|--------|
| `webapp/agents/graph.py` | `_fix_json_artifact` writes `{name}.repair.json` when `note` non-empty; deletes stale sidecars on a clean write. |
| `webapp/agents/tools/filesystem_tools.py` | `guard_json_bytes(..., provenance_path=None)`; `write_file`/`edit_file` pass it for `.json`. |
| `webapp/agents/nodes/check_tracker.py` | `:329-331` `isfile` → `artifact_is_reusable`. |
| `webapp/agents/nodes/setup_workspace.py` | `_restore_from_archive` refuses (returns False) when an FM-side `.repair.json` exists; M4 guard logs it. |
| `webapp/agents/nodes/validate_coverage.py` | `:84` split `skip` into "skip retry" vs "skip measurement". |
| `webapp/scraper/tasks.py` | `_publish_analysis_artifacts` carries the sidecar to FM. |
| New `webapp/agents/artifact_provenance.py` | `artifact_is_salvaged`, `write_salvage_sidecar`, `artifact_is_reusable`. |

### Failing tests first

1. `tests/test_salvage_provenance.py::test_pass2_salvage_writes_sidecar` — corrupt
   JSON through `_fix_json_artifact` → `product_analysis.json` parses AND
   `product_analysis.json.repair.json` exists with `salvaged: true`. **RED.**
2. `::test_valid_artifact_writes_no_sidecar` — clean write → no sidecar, and a
   stale sidecar from a prior run is deleted.
3. `::test_guard_json_bytes_reports_provenance` — repair path returns a non-empty
   note and writes the sidecar at `provenance_path`.
4. `tests/test_salvage_provenance.py::test_salvaged_artifact_is_not_reusable` —
   `artifact_is_reusable` False for a salvaged analysis, True for a clean one.
5. `tests/test_check_tracker_salvage.py::test_salvaged_product_analysis_does_not_skip` —
   workspace with a salvaged `product_analysis.json` → `skip_product_analysis is False`.
   **This is the job-12 amplifier lock.**
6. `tests/test_check_tracker_salvage.py::test_clean_artifacts_still_skip` —
   constraint-2 guard: a normal re-scrape with clean artifacts still skips.
7. `tests/test_validate_coverage_skip.py::test_skip_code_generation_does_not_bypass_low_coverage` —
   `skip_code_generation=True` + 1-of-6 target_fields covered → `low_coverage`
   interrupt, not `goto code_tester`.
8. `tests/test_validate_coverage_skip.py::test_skip_still_bypasses_coverage_retry_loop` —
   at/above `MIN_COVERAGE`, `skip=True` still routes to `code_tester`. Constraint-2
   regression guard.

### Decided AGAINST

- **Completeness scoring.** Threshold tuning + false-FAIL risk; the project
  already rejected this shape once. Set membership against `target_fields` is
  already implemented in `validate_coverage` and needs no new number.
- **In-band `"__salvage"` top-level key.** Pollutes every `for k in analysis`,
  `_prune_output_to_schema`, partner-facing FM artifacts, and
  `validate_coverage`'s `fields` iteration. Sidecar is invisible until asked.
- **Refusing to *write* salvaged artifacts at all.** The repair ladder exists
  because a salvage is strictly better than a `.corrupt` rename for
  `site_analysis.json` (9/10 keys recovered on sidley, per the docstring at
  `graph.py:366-370`). Deleting that recovery would undo a documented,
  critique-hardened win (constraint 3). Mark-and-opt-in keeps the win.
- **Read-time warnings emitted to the agent's context.** Adds tokens to a context
  that is already ballooning (job 12: 350,460 chars peak, 127 trims) and the agent
  demonstrably ignores warnings (49 tool calls, zero writes).
- **Deleting the `.repair.json` on publish.** Provenance must survive into the FM
  or P5's re-hydration guard has nothing to read.

### Rollback

Sidecar writing is inert if nobody reads it — ship 3a first, observe, then 3b/3d.
`ARTIFACT_SALVAGE_GATES=False` makes `artifact_is_reusable` return `isfile`
only (exact today behavior). The `validate_coverage` split is a single
`if`-condition revert.

---

## P4 — Date-bomb

### Premise challenged

The brief says *"Fix direction: unbounded/far-future end or drop the upper bound."*

**Dropping the upper bound is right, but for a sharper reason than the brief
gives — and the brief's own comment is the tell.** `recompute_date_reliability.py:25-28`
justifies `FIXED_AT` as *"anything still reliable=False AFTER this instant is
post-fix data … and must not be touched."* But the command **already refuses to
touch exactly those rows**, by construction:

- `:66-69` no raw date string → `unrecoverable`, skip.
- `:73-78` raws exist but none parse → `unrecoverable`, skip.
- `:87-92` posted date equals scrape day, or is future-dated → `still_unreliable`
  (P0-13 rule), skip.
- It writes **only** when a raw date *parses cleanly* and *passes the P0-13 rules*
  (`:93-97`).

The upper timestamp bound is therefore **redundant protection** — a second,
weaker guard bolted in front of a complete one. And it is a weaker guard of a
dangerous kind: it silently returns 0 rows with exit 0, printing
`scanned … : 0`. The hand-widening (Aug 25→26→27) is the smell: the constant
encodes a *guess about the calendar* rather than a property of the data.

> **Invariant: a one-shot incident-repair command's correctness must not depend on
> when it is run.** Its safety comes from the predicate on the data
> (`date_posted_reliable=False` + parse + P0-13), never from a timestamp the
> operator has to remember to bump.

Also: `FIXED_AT` is *already wrong in the other direction*. The fix landed
2026-08-25 (comment `:23`), so `lte=2026-08-27T00:00Z` includes two days of
healthy post-fix rows. It happens to be harmless only because those rows are
excluded by the parse/P0-13 predicates — which is the argument for deleting it.

### Chosen mechanism

- Delete `FIXED_AT` and the `scraped_at__lte` clause (`:56`). Keep `BROKEN_FROM`
  (a genuine historical fact: a66e33f shipped 2026-07-22) as the lower bound.
- Add `--until <iso>` as an **opt-in** narrowing argument, default absent =
  unbounded. Operators who want a window can ask for one; the default is correct
  without them.
- Rename the `scanned` output line to say `scanned (since 2026-07-22, reliable=False)`
  so a 0-row result is legibly a 0-row result.

### Files

`webapp/scraper/management/commands/recompute_date_reliability.py` (only file).

### Failing tests first

1. `tests/test_recompute_date_reliability.py::test_recovers_valid_dates` — already
   failing (per constraint 7); passes once the bound is gone.
2. `tests/test_admin_recompute.py::test_apply_fixes_row` — already failing; same.
3. **New** `tests/test_recompute_date_reliability.py::test_includes_rows_created_after_run_date` —
   create a `JobListing` with `scraped_at = now() + 1 day`, `date_posted_reliable=False`,
   a parsable raw date → assert it is scanned and fixed. **This is the actual
   bomb test**; it fails today at any run date, forever, and is the regression
   lock the brief asks for.
4. **New** `::test_p013_rules_still_exclude_genuinely_unreliable_rows` —
   constraint-2-style guard: a row whose parsed date equals scrape day is counted
   in `still_unreliable`, not fixed, with no upper bound present.

### Decided AGAINST

- **`FIXED_AT = date(2100,1,1)` / "far-future" end.** Same bomb, longer fuse, and
  it silently widens the window over 74 years of rows in a way nobody will audit.
- **`auto_now_add` awareness hacks.** Out of scope and touches a Django behavior
  used everywhere.
- **Hand-widening to 2026-08-28.** This is the third hand-widening. It fixes
  today's run and guarantees the fourth.

### Rollback

Single-file revert. No migration, no data risk (the command only ever sets
`posted_date` + `date_posted_reliable=True` on rows whose raw string parses).

---

## P5 — Stale-artifact re-injection on resume

### Premise challenged

The brief asks: *should resume even rehydrate analysis artifacts, or only
code/templates?*

**Blanket "analysis only, no rehydration" would throw away the feature that makes
selective rescrape affordable** — and it is the wrong cut. The artifacts do not
have equal epistemic standing:

| Artifact | Producer | Recompute cost | Staleness risk |
|----------|----------|----------------|----------------|
| `site_analysis.json` | LLM (site_analyzer) | high (LLM + probes) | low — describes the site, not the job |
| `navigation_analysis.json` | deterministic traverser | **very high** (live browser session) | **high** — carries the strategy-determining `api_endpoint`/`data_source`, and the poisoning history |
| `product_analysis.json` | LLM (product_analyzer) | high (LLM + browse) | **high** — job-specific field map; job 12's was a salvage with 1/6 fields |
| `scraper_analysis.json` | **deterministic** (`scraper_analyzer`) | **near-zero** — pure function of `navigation_analysis` + `content_analysis` | none — recomputable for free |
| `test_report.json` | LLM + runner | medium | low — describes last run's code, which is also restored |

Two of these are cheap to recompute and one of those (`scraper_analysis.json`) is
a **pure deterministic function of artifacts that are already being rehydrated.**
Rehydrating it is not a cache, it is a stale-memo: `setup_workspace.py:182-184`
restores `scraper_analysis.json` from FM whenever `skip_code_generation` is set,
so the gate at `graph.py:3051` runs against *last run's* verdict rather than
re-deriving it from *this run's* inputs. That is exactly how a wrong strategy
survives a rescrape.

And the docstring at `check_tracker.py:74-75` contains a false premise that
produces the bug directly:

> *"product_analyzer is skippable unless nav/search changed (the page-level field
> map is invariant to target_fields changes — normalize_fields re-filters)."*

**`normalize_fields` can only filter down, never up.** It merges
`existing_fields` with `DIRECT_FIELDS` and prunes to schema
(`nodes/normalize_fields.py:208-211`). If the rehydrated map lacks `ratings`, no
amount of re-filtering creates it. So `skip_product = not nav_changed`
(`check_tracker.py:100`) means a target_fields-only change **reuses a deficient
field map while correctly invalidating the code** — `skip_code = skip_product and
not fields_changed` becomes False, and code_writer is asked to emit six fields
from a map containing one.

> **Invariant: rehydration is sound only when the artifact is (a) valid,
> (b) unsalvaged, and (c) sufficient for the *current* job's declared outputs.
> Anything failing (a)–(c) is treated as missing — never as an error.**

### Chosen mechanism

**5a — split the rehydration set.** In `setup_workspace.py:177-184`:
- **Stop rehydrating `scraper_analysis.json`.** It is a deterministic derivation;
  let `scraper_analyzer` recompute it from the rehydrated `navigation_analysis` +
  `content_analysis`. Cost: one pure function call. Benefit: the strategy verdict
  is always derived from the current inputs. *(Concretely: remove line 183.)*
- **Keep rehydrating `site_analysis.json`** — slow-changing site description, no
  strategy authority.
- **Conditionally rehydrate `product_analysis.json` / `navigation_analysis.json`**
  via 5b.

**5b — a deterministic sufficiency predicate, reusing existing code.**
`artifact_is_sufficient(slug, filename, state)`:
- `.repair.json` sidecar present (P3) → False.
- For `product_analysis.json`: `set(target_fields) - DIRECT_FIELD_NAMES ⊆
  set(analysis["fields"])`. Set membership against what the user actually asked
  for — no threshold, no score. `normalize_fields` already imports `DIRECT_FIELDS`
  from `src/content_types.py:81-87`, so `scraped_at`/`remarks`/`url`/`src_url`/
  `status_code` are excluded as always-satisfiable.
- For `navigation_analysis.json`: `discovery.listing_url` non-empty **and**
  (`api_endpoint` absent, or `api_endpoint.verdict == "data"` per P2). A
  rehydrated descriptor with no verdict (pre-fix artifact) is treated as
  insufficient — the conservative direction, and it self-heals after one traverser
  run.
- Wire into `check_tracker`: `_handle_in_progress`'s `skip_product`
  (`check_tracker.py:330`) and `_compute_rescrape_skip_flags`'s
  `skip_product = not nav_changed` (`:100`) both AND in the sufficiency check.
- `setup_workspace._restore_from_archive` refuses when sufficiency fails, so a
  stale file cannot be re-injected behind the flag's back.

**5c — fix the false docstring, fix the flag.**
`skip_product = (not nav_changed) and artifact_is_sufficient(...)`.
`fields_changed` must also invalidate `skip_product` — the current formula lets a
field-map change through because of a comment that is factually wrong about
`normalize_fields`.

### Files

| File | Change |
|------|--------|
| `webapp/agents/nodes/setup_workspace.py` | `:177-184` split rehydration set; `_restore_from_archive` sufficiency refusal. |
| `webapp/agents/nodes/check_tracker.py` | `:74-75` docstring; `:100` formula; `:330` sufficiency AND. |
| New `webapp/agents/artifact_provenance.py` | `artifact_is_sufficient` (co-located with P3's helpers — one module, two exports). |
| `src/content_types.py` | Export `DIRECT_FIELD_NAMES` (trivially derived from `DIRECT_FIELDS`). |

### Failing tests first

1. `tests/test_resume_rehydration.py::test_scraper_analysis_is_not_rehydrated` —
   seed FM `analysis/scraper_analysis.json` with `strategy="internal_api"` +
   `skip_code_generation=True` → after `setup_workspace`, the workspace file is
   absent (recomputed later by the deterministic analyzer). **RED.**
2. `::test_scraper_analysis_recomputed_from_rehydrated_inputs` — rehydrated
   `navigation_analysis` + `content_analysis`, no `scraper_analysis.json` →
   `scraper_analyzer` runs and writes a fresh one.
3. `::test_product_analysis_missing_target_field_is_not_sufficient` — rehydrated
   map has 1 of 6 `target_fields` → sufficiency False → `skip_product_analysis is False`.
   **The job-12 lock.**
4. `::test_product_analysis_covering_target_fields_is_sufficient` — constraint-2
   guard: a complete rehydrated map still skips (selective rescrape keeps working).
5. `::test_direct_fields_do_not_block_sufficiency` — `target_fields =
   ["current_price","scraped_at","remarks"]` with only `current_price` mapped →
   sufficient (DIRECT_FIELDS are always satisfiable). Guards against a
   false-rejection regression on every intake job.
6. `::test_fields_changed_invalidates_product_skip` — same nav/search, different
   `target_fields` → `skip_product_analysis is False`. **Fails today**
   (`check_tracker.py:100`); locks the docstring fix.
7. `::test_pre_verdict_navigation_analysis_is_not_sufficient` — descriptor without
   `verdict` → sufficiency False.

### Decided AGAINST

- **"Analysis artifacts are never rehydrated" (the brief's second option).** Costs
  a full LLM site analysis + a live browser traversal on every rescrape, to fix a
  problem that is actually *conditional* rehydration. The selective-rescrape
  design is sound; its rehydration set and its predicate are not.
- **Freshness TTLs (mtime/age) on artifacts.** Sites change on their own schedule;
  a 7-day-old artifact can be perfectly good and a 20-minute-old one can be a
  salvage. Age is a proxy for the thing we can measure directly (sufficiency).
- **Hashing job config into the artifact name.** Turns the FM into a
  content-addressed store and breaks every existing `artifacts.scrapers_key` call
  site plus partner API surface. Constraint 5 (no new infra).
- **Rehydrating `test_report.json`.** Kept — it describes the code that is also
  restored, so it is internally consistent, and `route_after_testing` needs it.

### Rollback

Restoring line 183 (rehydrate `scraper_analysis.json`) is a one-line revert.
`RESUME_SUFFICIENCY_GATES=False` makes `artifact_is_sufficient` return True.
The `check_tracker.py:100` formula revert restores today's behavior exactly.

---

## Rollout order (dependency-driven, not priority-driven)

1. **P4** — one file, live in prod, two tests already red. Ship first.
2. **P1-L1** (box `code_tester` invoke) — prerequisite for everything in P1;
   no behavior change on the happy path.
3. **P2 Step 1** (`sample_keys` pass-through) — inert one-liner; unblocks Step 2.
4. **P2 Steps 2–5** + tests. Highest-value change in the plan.
5. **P1-L2/L3** (provider classification + requeue).
6. **P3a** (sidecar writing) — inert alone; observe one deploy.
7. **P5a/5b/5c** — depends on P3a (reads its sidecars) and P2 (reads `verdict`).
8. **P3b/3d** — depends on P3a and P5 (gates the same skip flags from the other side).

Rationale: P5 and P3b read signals produced by P2 and P3a, so shipping them first
would mean shipping gates that always evaluate to their default. Order also keeps
each deploy independently revertible.

---

## What this plan deliberately does not do

- No new LLM calls anywhere (constraint 1).
- No async, no new services, no migrations (constraint 5).
- No change to `scraper_analyzer`'s determinism — it gains *better inputs*
  (constraint 6).
- No new site blocklists, no domain-sameness rule, no `count`-must-be-non-null
  rule (all three rejected by the brief's own amnhealthcare/lw.com evidence).
- No undoing of the 8 codegen fixes, the word-boundary tokens, or the repair
  ladder — each is extended or given a provenance trail (constraint 3).
- No completeness score, no coverage percentage, no regression band (the project
  has already rejected two threshold gates; this plan adds zero new numbers).
