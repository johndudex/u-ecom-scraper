# Job 12 fix plan — 1 of 6: RELIABILITY / SRE

> Planner 1, lens: **failure-is-boring, resilience-is-designed.**
> Evidence base: `docs/plans/job12-context-brief.md` (P1–P5 + hard constraints 1–7). Every file/line below was read, not inferred.
>
> Companion plans: 2–6 (other lenses). This plan is self-contained and shippable on its own.

---

## 0. Failure taxonomy (the lens, applied to job 12)

Before any fix: decide, per signal, whether the correct response is **RETRY**, **DEGRADE** (proceed with reduced capability), **PAUSE** (for a human), or **FAIL**. The whole plan follows from this table.

| # | Signal (job 12 instance) | Class | Response | Evidence for the classification |
|---|---|---|---|---|
| F1 | HTTP 429 from the LLM provider, 4× in an 8s burst | **Transient, provider-side demand control** | **RETRY** (bounded, jittered, budget-capped) → then **DEFER** the job | Self-heals in seconds-to-minutes (quota windows are per-minute). Failing discarded 42m51s of paid work to recover from a condition that lasts ~60s. Retry-After was absent (z.ai returns `code 1302` in the body), so the ladder had to carry the timing. |
| F2 | Poison `api_endpoint` (useinsider personalization widget) | **Permanent defect in an upstream *dependency*, not in the job** | **DEGRADE**: verify-then-refute, fall back to a browser strategy | The job is perfectly scrapeable (product_analysis said so). Only the strategy *input* was wrong. Failing or pausing here would be wrong; retrying without fixing the input reproduces the failure exactly. |
| F3 | Salvaged artifact, valid JSON, 1 of 6 requested fields | **Silent data loss that parses clean** | **DEGRADE** + observability; **RETRY** only at 0-of-N | A 1-of-6 artifact still carries information — discarding it throws away paid work. A 0-of-N artifact is a hallucination shell: publishing it as authoritative is a silent failure and must be refused. |
| F4 | Chosen strategy extracted 0 items | **Normal negative result** (designed path) | **RETRY** via the existing strategy cascade — but fix the cascade's terminal hole | Already designed and bounded (`MAX_TEST_RETRIES = 2`, `webapp/agents/constants.py:11`). It has a bug (see P2-D) that made cycle 3 a no-op. |
| F5 | Rehydrated artifact from jobs 9/10 steering a new job | **Cache with no invalidation key** | **DEGRADE**: re-verify only the *deciding* field; invalidate on changed decision inputs | Re-running analysis on every rescrape violates constraint 1 (new LLM cost). Cache-when-identical, invalidate-on-diff is free. |
| F6 | `recompute_date_reliability` scans 0 rows and exits 0 | **Silent no-op maintenance job** | **FAIL LOUDLY** (warn + non-zero signal) | A repair command that silently does nothing is strictly worse than one that errors: it produces false confidence in the data. This is the observability hole that let the date-bomb survive 3 hand-widenings. |
| F7 | Auth / 400 / 422 from the provider | **Permanent caller bug** | **FAIL fast** (unchanged) | Already classified in `webapp/agents/llm.py:48-54,175-176`. Retry cannot fix config. |

**Meta-rule derived from the table:** job 12 needed *zero* new failure modes that end in FAILED. Two of its five problems (P2, P3) are data-quality problems that should degrade, not fail. Only P4 is a genuine "fail loudly" case.

**The F1 verdict — defer, not pause, and not fail.** The brief asks whether 429-exhaustion should fail the job or pause it for human intervention. Three options were evaluated against the infrastructure that actually exists:

| Option | Verdict | Why |
|---|---|---|
| **FAIL** (today) | rejected | Discards 42m51s of paid work to recover from a ~60s condition. Worst of the three. |
| **PAUSE** (`STATUS_WAITING_APPROVAL` + an `Approval` row, the `create_recursion_approval` precedent at `services.py:412-455`) | rejected | The approval path is exactly what job 12 bypassed — intake jobs set `skip_approvals` (`models.py:152`), and job 12 produced **0 approval rows** with the user never asked. A provider outage must not depend on a human gate the job is configured to skip. It would also put a provider problem in the approval queue, where the auto-approve timer (`_auto_approve_stale_jobs`, 10 min, `auto_queued` only) may resolve it by accident. |
| **DEFER** (`STATUS_DEFERRED` + `self.retry`) | **chosen** | Automatic, needs no human, frees the worker slot during the backoff, resumes on a timer rather than on attention, and after 3 attempts still fails honestly. |

---

## 1. New shared surfaces (introduced once, used by several fixes)

### 1a. `webapp/agents/api_verify.py` (NEW — ~90 lines)

Single home for "is this discovered endpoint actually a data API?" Reuses the production verifier rather than duplicating it: `experimental/nav_traversal/traversal.py:472 verify_api` is already the only body-level check in the repo and is **live in prod** (imported at `webapp/agents/graph.py:2222`).

```python
def classify_api_body(body: str) -> tuple[bool, int, int | None, list[str]]:
    """(is_data_api, items_per_page, count, sample_keys) from an HTTP body.
    Lives in traversal.py next to _extract_items_count/_SELECT_OPTION_KEYS so
    verify_api and this classifier cannot diverge."""

def verify_data_api(url: str, *, timeout: float = 8.0, query: str = "") -> dict:
    """{verdict: verified|refuted|unknown, items_per_page, count, sample_keys, detail}"""

def api_endpoint_usable(nav_api: dict) -> bool:
    """THE shared predicate. True iff a truthy url/api_url AND verdict != 'refuted'.
    Missing verification ⇒ True (fail open)."""
```

**Three-state verdict — this is the load-bearing design decision:**

| Verdict | Meaning | Behaviour |
|---|---|---|
| `verified` | Body fetched, JSON-parsed, contains a list of dicts with ≥1 record, not a dropdown-taxonomy shape | current behaviour |
| `refuted` | Body fetched AND parsed as JSON, but contains **no** list-of-dicts record array (or only `{disabled,group,selected,text,value,label,key,id}` option objects) | **strip the endpoint**, demote `data_source`, fall through the normal strategy cascade |
| `unknown` | Fetch raised / non-2xx / body was HTML / timed out | **fail open** — behave exactly as today |

Fail-open is what makes this safe against hard constraint 2: the only behaviour change is on *positive* evidence that the URL is not a data API. A transient probe failure cannot flip a working site.

### 1b. `webapp/agents/llm.py` — typed rate-limit exception

```python
class LLMRateLimitExhausted(RuntimeError):
    """Provider 429 outlived the retry budget (or the phase sleep budget)."""
    # attrs: model, attempts, sleep_spent, retry_after, reason ("exhausted"|"phase_budget")
    # __str__ MUST contain "429" and "rate limit" so existing error_message
    # greps and _BLOCKED_RE-style matching keep working.
```

### 1c. Cross-worker 429 ledger in the Django cache (Redis — already the broker + cache backend)

`llm_breaker.py` documents its own limitation ("per-worker … a Redis-backed cross-worker breaker is a future refinement"). We need exactly that, for the rate-limit dimension only:

```python
def note_rate_limited(model: str, ttl: float) -> None   # cache.set(f"llm:rl:{model}", now, ttl)
def rate_limited_until(model: str) -> float             # 0.0 when unset
```

This costs one Redis GET/SET — no LLM calls, no DB, no new service (constraint 5).

**Finding worth recording separately:** in the shipped config `ZAI_FALLBACK_MODEL` defaults to `"glm-5-turbo"`, which is *also* `ZAI_MAIN_MODEL` and `ZAI_SMALL_MODEL` (`webapp/config/settings.py:181-182,211`). `effective_model` returns `primary` when `primary == fallback` (`llm_breaker.py:139`), so **the circuit breaker is a structural no-op in default prod** — it looks like a resilience layer and does nothing. This plan does not change the fallback model (that is a cost/quality decision for another lens) but it is why the ledger, not the breaker, is the carrier for P1.

---

## 2. P1 — 429 / provider-error retry policy

### Classification: RETRY (in-call) → DEFER (job) → FAIL (after 3 deferrals)

### 2a. The numbers

Measured failure: 4× 429 in 8s, sleeps 1.6s / 4.2s / 1.7s, exhausted in **7.5s**. z.ai coding-plan quotas are per-minute windows; a 7.5s ladder cannot ride out a 60s window by construction. The current ladder is also too *short* in amplitude to ever matter: `_backoff_delay(attempt, cfg) = uniform(0, min(30, 1.5·2^attempt))` gives mean sleeps of 1.5s / 3s / 6s.

`webapp/agents/llm.py` + `webapp/config/settings.py`:

| Setting | Now | Proposed | Notes |
|---|---|---|---|
| `LLM_RETRY_RATELIMIT_MAX` | 3 | **5** | 4 sleeps |
| `LLM_RATELIMIT_BACKOFF_BASE` | — (shared `1.5`) | **4.0** | new key; the shared `LLM_RETRY_BACKOFF_BASE` stays 1.5 so the **transient** class is untouched |
| `LLM_RATELIMIT_BACKOFF_CAP` | — (shared `30`) | **45.0** | new key |
| Retry-After handling | raw, capped 60s | **`uniform(0.5, 1.5) · retry_after`**, capped 120s | z.ai sends none today, but when a provider does, N workers currently wake in lockstep — a thundering herd re-triggers the very limit we are backing off from |

Ladder arithmetic (mean / max per attempt): 4s / 8s, 8s / 16s, 16s / 32s, 22.5s / 45s, 22.5s / 45s.
**Mean total ≈ 73s, max ≈ 146s.** That spans a per-minute quota window with ~1.2× headroom at the mean. Compare: 7.5s today.

**Bounded-wall check:** worst added wall time per call is 146s of sleep. A 429 returns immediately (no generation time), so this does not multiply the `CODE_WRITER_LLM_TIMEOUT` concern documented at `settings.py:205` — that comment is about a *300s generation* being retried 3×, not about sleeps.

### 2b. Phase-level sleep budget (the missing circuit-breaker-for-demand)

A per-call budget alone is insufficient: a codegen phase makes ~50 LLM calls. 50 × 146s of backoff is a hung job that merely looks alive. Add an **instance-level accumulator** on `ClassifiedRetryChatOpenAI` (`self._rl_sleep_spent`) — one instance per phase agent, so this is naturally per-phase, with no global state and no cross-job interference.

`LLM_RATELIMIT_PHASE_SLEEP_BUDGET = 180` (s). When cumulative 429 sleep in this phase exceeds the budget, stop retrying and raise `LLMRateLimitExhausted(reason="phase_budget")` immediately. After 3 minutes of continuous 429s the provider is not going to recover mid-phase; continuing to hammer is both rude and useless.

### 2c-0. FIRST: preserve the exception type (a precondition, discovered in the code)

There are **two** invoke paths in the graph and they behave oppositely on an LLM exception:

| Path | Site | Exception fate |
|---|---|---|
| raw `agent.invoke` | code_tester `graph.py:3716`, cleanup `:3934`, skill_learner `:3978` | propagates **with its type intact** → `run_scrape_task:139` generic `except` → FAILED. **This is job 12's path.** |
| `_invoke_agent_with_timeout` | code_writer `graph.py:3518`, site/product/navigation `:1820/:1875/:3203/:3322` | `graph.py:1698-1699` does `result_box[0] = {"_error": str(exc)[:200]}` — **the exception is swallowed and its type is destroyed** |

So a provider outage inside `code_writer` is silently converted into `{"_error": "…"}` with no `messages`, which downstream reads as *budget exhausted / no draft written* — an infrastructure problem becomes a codegen problem and feeds the retry loop. Job 12's cycle-1 "Sorry, need more steps" arm is adjacent.

Fix (small, and required before anything else in P1):

```python
# graph.py:1699
result_box[0] = {"_error": str(exc)[:200],
                 "_rate_limited": isinstance(exc, LLMRateLimitExhausted)}
```

and in `_invoke_budgeted_agent` (`:1820`, `:1875`) + `_invoke_code_writer` (`:3518`): if `result.get("_rate_limited")` → **re-raise** `LLMRateLimitExhausted` instead of continuing. A provider outage must never be laundered into "the agent made no progress".

### 2c. Job-level: DEFER, don't FAIL

`webapp/scraper/tasks.py` — `run_scrape_task` (decorator `:94-99`, `bind=True`, `max_retries=1`, soft/hard limits 7200/7560s). Its `except Exception` at `:139-185` sets FAILED and **does not re-raise** (Celery sees the task as successful), so the typed handler must sit **above** it:

```python
except LLMRateLimitExhausted as exc:
    attempt = self.request.retries                    # the counter already exists — no column
    if attempt >= 2:                                  # 3rd deferral → honest failure
        job.status = FAILED; job.error_message = f"...429... after 3 deferrals"; return
    job.status = ScrapeJob.STATUS_DEFERRED
    job.error_message = f"provider rate limit (429): deferred attempt {attempt+1}/3, next in ~{d}s"
    # SessionLog row, ROLE_SYSTEM, agent=<phase>, NO "[HEARTBEAT]" prefix
    raise self.retry(countdown=RETRY_DELAYS[attempt], exc=exc, max_retries=None)
RETRY_DELAYS = (120, 300, 900)
```

Three lifecycle facts this design was checked against (all verified in code):

1. **`max_retries=1` on the decorator** would cap us at one deferral. The repo already has the override pattern — the same-site serialization retry at `tasks.py:122-137` passes `max_retries=None` per call. Copy it.
2. **The dedup guard** (`tasks.py:112-116`) silently returns when status ∈ {RUNNING, WAITING_APPROVAL}. A deferred job **must not** stay RUNNING, or the retried task no-ops. Hence a distinct status.
3. **`_do_schedule_next_site`** (`tasks.py:1322-1335`) is a *global* gate: it returns "skipped" whenever **any** job is RUNNING/PENDING/WAITING_APPROVAL. Two consequences:
   - Reusing `STATUS_PENDING` would block every other site's dispatch for the whole deferral window. Rejected.
   - A new `STATUS_DEFERRED` is **excluded** from `active_statuses`, so other sites proceed during the backoff — which is the correct blast-radius behaviour.
   - It is also **added** to the same set's *site-scoped* duplicate-dispatch guard, so the scheduler does not start a second job for the deferred site while one is pending. Conservative default; bounded cost.

**New status, no new column:** `STATUS_DEFERRED = "deferred"` added to `STATUS_CHOICES` (`models.py:108-117`, display "Deferred — provider rate limited"). The attempt count lives in `self.request.retries`, so no migration beyond the choice tuple. Verified safe against the three status-driven behaviours: `_TERMINAL_JOB_STATUSES` (`models.py:670-676`) excludes it → the `post_save` approval-closer does not fire; `cleanup_stuck_jobs` (`tasks.py:1202`) only inspects RUNNING → no reaping; the watchdog's SessionLog freshness check excludes only `[HEARTBEAT]`/`[PROBE]` prefixes (`:1229-1235`) → the deferral row we write counts as activity anyway.

**Why `self.retry` and not a beat sweep:** zero new moving parts, zero new infrastructure (constraint 5), and the Celery task's own `request.retries` is the retry counter. Redis-broker visibility timeout defaults to 1h; the 900s ceiling keeps the ETA safely under it (no redelivery / duplicate execution).

**Why not the existing `STATUS_WAITING_APPROVAL` + Approval row** (the `create_recursion_approval` precedent at `services.py:412-455`, which converts the non-human `GraphRecursionError` into an approval): because intake jobs run `skip_approvals`, and job 12 proved the approval path is exactly what gets skipped — 0 approval rows, user never asked. A provider outage must not depend on a human gate that the job is configured to bypass.

**Pre-flight gate (this is what makes deferral cheap):** at task start, if `self.request.retries > 0` **and** `rate_limited_until(model) > now` → re-defer immediately without entering the graph. Without this, each deferral re-runs navigation + codegen for ~10 minutes before hitting the next 429, and the "cheap pause" becomes an expensive loop. With it, a deferral costs one Redis GET. Also `note_rate_limited()` on **every** 429 (not just exhaustion) with `ttl = last delay`, so the ledger is hot the moment a burst starts.

### 2d. Observability of the silent failure

The brief's sharpest SRE finding: *"The 429 never entered SessionLog (thrown after the last logged line; only in `error_message`)."* Fix at the deferral point (2c) — one `SessionLog` row, `ROLE_SYSTEM`, `agent=phase`. Per-attempt detail stays in the existing `logger.info` at `llm.py:212` (Railway logs); the SessionLog row is the durable, UI-visible record.

### 2e. Exact files

- `webapp/agents/llm.py` — `_retry_settings()` (new keys), `_backoff_delay` (per-class constants), `_parse_retry_after` (cap 60→120), `_handle_retry` (jittered Retry-After, raise typed exception, budget accumulation), `ClassifiedRetryChatOpenAI.__init__`/`_generate`/`_agenerate` (`_rl_sleep_spent`), new `LLMRateLimitExhausted`.
- `webapp/agents/graph.py` — `:1698-1699` (`_rate_limited` marker), `:1820`/`:1875` (`_run_budgeted_agent` re-raise), `:3518` (`_invoke_code_writer` re-raise).
- `webapp/agents/llm_breaker.py` — `note_rate_limited`, `rate_limited_until`.
- `webapp/config/settings.py:217-222` — 5 new/changed keys.
- `webapp/scraper/models.py:108-117` — `STATUS_DEFERRED` choice.
- `webapp/scraper/tasks.py` — typed `except` above `:139`, pre-flight gate near `:107`, SessionLog row, `active_statuses` / duplicate-dispatch set at `:1322`.

### 2f. Failing tests first (`tests/test_llm_rate_limit.py`, NEW)

House style per `tests/test_llm_provider.py` (`_FakeSettings`, no network, patch `super()._generate`):

1. `test_ratelimit_ladder_survives_a_60s_window` — **the job-12 regression.** Stub fn always raises `RateLimitError`; record sleeps; assert `len(sleeps) == 4` and `sum(sleeps) >= 60`. Fails today (3 sleeps, 7.5s).
2. `test_ratelimit_bounds_grow_exponentially_and_respect_cap` — patch `random.uniform` to capture `(lo, hi)`; assert `hi == min(cap, base·2^attempt)` and `lo == 0`.
3. `test_transient_class_constants_unchanged` — guard: transient still uses `base=1.5, cap=30, max=2`. Pins constraint 3 (no silent widening of the other class).
4. `test_retry_after_is_jittered_not_raw` — `Retry-After: 30` → sleep ∈ [15, 45], not 30.
5. `test_exhaustion_raises_typed_exception_with_429_in_str` — `__cause__` is the original `RateLimitError`.
6. `test_phase_sleep_budget_stops_retrying` — budget 5s, always-429 fn → stops before the 5th attempt with `reason="phase_budget"`.
7. `test_caller_bug_still_fails_fast` — `BadRequestError` propagates on attempt 0 with zero sleeps.
8. `test_429_trips_the_cross_worker_ledger` — `note_rate_limited` called; `rate_limited_until` > now.
9. `test_agent_timeout_wrapper_preserves_the_rate_limit_marker` — **the path test.** Patch `agent.invoke` to raise `LLMRateLimitExhausted`; assert `_invoke_agent_with_timeout` returns `{"_error": …, "_rate_limited": True}` (fails today: the key does not exist and the type is destroyed by `str(exc)[:200]`).
10. `test_budgeted_agent_reraises_on_rate_limit` — `_run_budgeted_agent` with a `_rate_limited` result → raises rather than returning a budget-exhausted Command. Guards the "provider outage ≠ agent made no progress" invariant.
11. `test_agent_timeout_wrapper_still_swallows_other_errors` — a generic `ValueError` still becomes `{"_error": …}` with `_rate_limited` absent. Pins the existing timeout/abandonment behaviour (constraint 3).

`webapp/tests/test_rate_limit_deferral.py` (NEW — Django `TestCase` + `model_bakery`, per the `webapp/tests/` convention; the repo-root `tests/` tree is mostly static-source verification and is the wrong home for ORM assertions):
12. task handler → `retry(countdown=120|300|900)` per attempt with `max_retries=None` (overriding the decorator's `max_retries=1`), `status=deferred`, SessionLog row present and **not** prefixed `[HEARTBEAT]`.
13. 3rd exhaustion → `status=failed`, message contains "429", no re-raise (the task still returns).
14. `STATUS_DEFERRED` ∉ `active_statuses` in `_do_schedule_next_site` → another site may dispatch during a deferral.
15. `STATUS_DEFERRED` ∉ `_TERMINAL_JOB_STATUSES` → pending approvals are not closed by a deferral.
16. pre-flight: `retries>0` + hot ledger → graph **not** invoked, immediate re-deferral.
17. ledger cold + `retries>0` → graph invoked normally (no deferral lock-in).

### 2g. Rollout / rollback

- **Rollout:** (0) the §2c-0 marker + re-raise (no behaviour change for non-rate-limit errors — test 11 pins that); (1) ladder constants + typed exception + tests, behind the existing `LLM_CLASSIFIED_RETRY` kill switch; (2) phase budget (its own env key, default on); (3) `STATUS_DEFERRED` + task deferral + pre-flight (its own env key `LLM_RATELIMIT_DEFER=1`, default on). Each is independently revertible by env var.
- **Rollback:** unset `LLM_RATELIMIT_DEFER` → behaviour returns to fail-fast (the status choice and the marker are then inert). Unset `LLM_CLASSIFIED_RETRY` → pre-Phase-2 blind SDK retry (`llm.py:354-356`) — the existing documented kill switch. The migration is a choice-tuple addition only; reverting it after jobs have been created with `status="deferred"` would need a one-line data update, so revert the *code* first and let existing rows be terminal.

### 2h. What could break, and why it won't

- **Longer ladders delay genuine failures.** Worst case a job that would have died at 7.5s now dies at ~150s. Acceptable: a false *late* failure costs 2.5 minutes; a false *early* failure costs a 42-minute run.
- **A deferral re-runs the graph from the top.** True, and wasteful — which is why the pre-flight gate (2c) is not optional. With the gate, a deferral that lands inside a live outage costs one Redis GET.
- **`self.retry` inside a prefork Celery worker.** `CELERY_TASK_ACKS_LATE=False` (`settings.py:146`), so the original message is already acked; `retry()` publishes a fresh one. No acks_late/reject_on_worker_lost interaction.
- **A new status ripples through the lifecycle.** Checked, not assumed: `STATUS_DEFERRED` is deliberately **outside** `_TERMINAL_JOB_STATUSES` (`models.py:670-676`, so pending approvals survive), **outside** `cleanup_stuck_jobs`'s RUNNING-only sweep (`tasks.py:1202`, so it is not reaped mid-backoff), **inside** the duplicate-dispatch guard so the scheduler does not start a second job for the same site, and **outside** `_do_schedule_next_site`'s `active_statuses` (`tasks.py:1322-1335`) so other sites keep flowing. Tests 14-15 pin all four.
- **The exception wrapper change alters failure semantics for code_writer.** Today a code_writer LLM exception becomes `{"_error": …}` and the run limps on. After the change it raises and defers. That is the intended correction, and test 11 pins that every *non*-rate-limit error keeps today's swallow behaviour, so the blast radius is exactly one exception class.
- **Constraint 1 (no new per-run LLM cost).** Satisfied — the pre-flight is a cache GET, not an LLM ping. No "ask a model whether we're rate-limited" anywhere.
- **Constraint 5 (no async).** Nothing here touches `LLM_ASYNC_EXECUTION`; every sleep is `time.sleep` on the existing sync path.

### Confidence: **HIGH (0.85)**. Biggest risk: adding `STATUS_DEFERRED` touches four lifecycle behaviours at once (terminal set, watchdog, scheduler active set, duplicate-dispatch guard). All four were read and enumerated in §2h with tests 14-15 pinning them, but a fifth status-sensitive consumer added later would silently mis-handle the new state. The exception-type risk is resolved by construction (§2c-0), not left to discovery.

---

## 3. P2 — Poison-endpoint / strategy-gate trust

### Classification: DEGRADE (verify the untrusted dependency; fall back; never fail)

### 3a. The SRE framing

An SRE reading `graph.py:3049` sees the textbook anti-pattern: **an unverified upstream dependency is trusted as authoritative.** Worse, the repo has *five* independent trust points and only one of them checks anything.

| Consumer | Line | Trusts |
|---|---|---|
| Strategy gate | `webapp/agents/graph.py:3049-3058` | `items_per_page>0` **and** `count!=0` (the only real check) |
| Template selector | `webapp/agents/graph.py:3356-3359` | bare `url` truthiness |
| code_writer `api_section` | `webapp/agents/subagents.py:2679` → **2809** | bare `url` truthiness |
| Coverage-gate skip | `webapp/agents/nodes/validate_coverage.py:179-189` | bare `url` truthiness |
| product_analyzer API sample | `webapp/agents/subagents.py:1449-1470` | bare `url` truthiness |

**This is the plan's central finding:** fixing only the strategy gate does **not** fix job 12. `subagents.py:2809` does `navigation_section = api_section` — the api_section **replaces the entire two-phase/pagination instruction block**, and its text says *"do NOT use Playwright/Selenium, do NOT parse the DOM."* Even with the strategy gate correctly returning `playwright`, code_writer was still ordered onto the API path. The strategy gate is the *least* influential of the five consumers.

### 3b. Mechanism

**(A) Verify at capture, cache in the artifact.** `webapp/agents/graph.py:2393` — where `api_endpoint` is written into `navigation_analysis`, run `verify_data_api(url)` once and embed:

```json
"api_endpoint": {"url": "...", "count": 26955, "items_per_page": 5,
                 "verification": {"verdict": "verified", "at": "...Z",
                                  "items_per_page": 5, "count": 26955,
                                  "sample_keys": ["job_title", "location"]}}
```

Cost: **one httpx GET, ≤8s, once per run, zero LLM calls.** `sample_keys` matters: `graph.py:2393` currently *discards* them, which is why the artifact carries no shape evidence at all.

Two of the three known poison classes never reach the probe (already filtered upstream at `traversal.py:1056` by `_TELEMETRY_RE`: doubleclick, and ketch via the word-boundary fix). useinsider — the class that actually killed job 12 — genuinely contains `/api/` and must be caught by body shape, which is exactly what the probe is for.

**(B) Gate on the shared predicate.** `graph.py:3049` gains one conjunct:

```python
and api_endpoint_usable(_nav_api)
```

The existing `items_per_page>0 and count!=0` logic is untouched (constraint 3 — extend, never silently supersede). The two layers compose rather than collapse: Coveo's `totalCount=0` is *shape-verified* but still rejected by the explicit-zero rule; useinsider is *shape-refuted*.

**(C) Blank-and-warn, F17-style.** On `refuted`, do not merely ignore: set `navigation_analysis["api_endpoint"] = {"refuted": True, "reason": <detail>}` and demote `data_source` from `"api"` to `"none"` so the cascade re-derives from `rendering_verified`. Precedent: `_sanitize_nav_domains` (`graph.py:2143-2199`) blanks rather than rejects for exactly this reason — leaving the URL in the artifact lets code_writer improvise on it.

**(D) Fix the terminal-strategy hole (independent bug, same node).** `_ESCALATION = ["http_requests", "http_navigation", "playwright", "internal_api"]` at `graph.py:2912`. Job 12: gate picks `internal_api` → 0 items → recorded in `strategies_tried` → re-derive picks `internal_api` again → `_ESCALATION.index("internal_api") == 3` → `_ESCALATION[4:]` is **empty** → the escalation loop body never runs → **the same failed strategy is re-picked with no change.** That is cycle 3: 10 minutes, 49 tool calls, zero writes, no strategy change. Fix:

```python
if _chosen in _all_tried and not any(_next not in _all_tried for _next in _ESCALATION[_idx+1:]):
    # terminal rung failed — fall back to the most capable browser-backed rung
    for _fb in ("playwright", "http_navigation"):
        if _fb not in _all_tried:
            analysis["strategy"] = analysis["recommended_strategy"] = _fb
            analysis["strategy_justification"] = (
                f"Deterministic escalation: {_chosen} tried+failed at the terminal rung "
                f"-> {_fb} (browser-backed fallback)")
            break
```

**(E) The four remaining consumers** (`3356-3359`, `subagents.py:2679`, `validate_coverage.py:179`, `subagents.py:1453`) each replace `api_ep.get("url")` truthiness with `api_endpoint_usable(api_ep)`. Same predicate, imported from one module, so they cannot drift.

### 3f. Exact files

- `experimental/nav_traversal/traversal.py` — add public `classify_api_body(body)` (refactor `_extract_items_count`/`_SELECT_OPTION_KEYS` to be used by both it and `verify_api`; no behaviour change to `verify_api`).
- `webapp/agents/api_verify.py` (NEW).
- `webapp/agents/graph.py` — `:2393` (embed verification), `:3049` (gate conjunct), refuted-blanking near `:2393`, `:2912-2931` (terminal-rung fallback), `:3356-3359` (template).
- `webapp/agents/subagents.py` — `:2679` (api_section gate), `:1453` (sample downgrade).
- `webapp/agents/nodes/validate_coverage.py` — `:179`.

### 3g. Failing tests first

`tests/test_api_verify.py` (NEW — body classification, canned strings, no network):
1. `test_useinsider_info_shape_is_refuted` — a personalization-config object with no dict-array → `refuted`. **The job-12 regression.**
2. `test_aya_job_search_shape_is_verified` — `{count: 26955, results: [dict, …]}` → `verified` (constraint-1 guard).
3. `test_amn_cross_domain_null_count_is_verified` — cross-domain URL, `count=None`, 5 dicts → `verified`. Pins the reason domain-sameness and count-must-be-non-null rules are both forbidden.
4. `test_coveo_zero_total_is_shape_verified` — `totalCount=0` with 15 dicts → shape `verified`. Asserts the count rule still rejects it at the gate (layers compose).
5. `test_dropdown_taxonomy_is_refuted`; 6. `test_html_body_is_unknown`; 7. `test_fetch_exception_is_unknown`; 8. `test_non_2xx_is_unknown`.

`tests/test_strategy_gate_poison.py` (NEW — extends `tests/test_content_types.py:619` `test_scraper_analyzer_message_with_navigation`):
9. `refuted` → strategy is **not** `internal_api` and follows the rendering cascade.
10. `refuted` → `api_endpoint` is blanked with `refuted: True` and `data_source` demoted.
11. `unknown` → `internal_api` (fail-open pinned — this is the no-regression guarantee).
12. no `verification` key → `internal_api` (fail-open when never probed — this is what keeps every existing site and every existing test green).
13. `verified` → `internal_api` (positive path).

`tests/test_code_writer_api_hijack.py` (NEW):
14. `refuted` endpoint → message must **not** contain `"CRITICAL — Backend JSON search API"` and **must** contain the two-phase section. **The most load-bearing new test in this plan.**
15. `verified` → api_section present, `navigation_section == api_section` (pins the current precedence and `webapp/tests/test_embedded_json_model.py:298`).

`tests/test_strategy_escalation_terminal.py` (NEW):
16. `internal_api` in `strategies_tried` → next strategy is `playwright`, justification mentions "terminal rung". **The cycle-3 regression test.**

`tests/test_validate_coverage_api_skip.py` (NEW):
17. `refuted` endpoint → the coverage gate is **not** skipped (low coverage still interrupts).

### 3h. Rollout / rollback

- **Rollout:** (1) `classify_api_body` + `api_verify.py` + pure tests (no behaviour change at all); (2) embed `verification` in the artifact at `:2393` (additive — nothing reads it yet); (3) flip the five consumers, one commit each, gate (B) last; (4) terminal-rung fallback (independent, can land first — it is a strict improvement with no dependency).
- **Rollback:** each consumer flip is one-line revertible. The `verification` key in the artifact is inert to old readers. Nothing needs a data backfill.

### 3i. What could break, and why it won't

- **The probe hurts a working site.** The `unknown` verdict is fail-open by construction, and it is the verdict every transient failure produces. A site only changes behaviour if its endpoint is *fetched successfully, parses as JSON, and demonstrably contains no record array* — which is the definition of the bug. amn (verified shape), aya (verified), lw.com (verified shape, rejected by count), locumtenens/adameve/rmwilliams/zquiet/abercrombie/toscrape (no `data_source == "api"`, so the probe never even runs for them).
- **Probe latency.** ≤8s worst case, 1 call per run, in the deterministic analyzer. Compare the ~20 minutes of codegen thrash it prevents. Sites with no API candidate pay nothing.
- **An endpoint that needs POST/auth/geo returns 403 → `unknown` → fail open.** This is why the verdict is three-state and not a boolean. A probe that cannot *positively* refute never changes behaviour.
- **Constraint 6 (scraper_analyzer stays deterministic).** Satisfied — a probe + a shape predicate is more deterministic than what it replaces, and it contains no LLM.
- **Constraint 3.** Nothing is removed. The word-boundary token list, `count != 0`, `items_per_page > 0`, and the anti-bot override all survive; the new conjunct is strictly additional.

### Confidence: **HIGH (0.8)**. Biggest risk: useinsider's `/api/info/824.24` may happen to contain *some* array of objects (e.g. a goals list) and classify `verified`. Test 1 pins the real body shape; if it does contain a record array, the backstop is the terminal-rung fallback (§3b-D), which converts cycle 3's no-op thrash into a deliberate browser-strategy retry — the failure becomes non-fatal even when the probe misses.

---

## 4. P3 — Artifact completeness (validity ≠ completeness)

### Classification: DEGRADE + observability; RETRY only at 0-of-N

### 4a. Mechanism

The repair ladder (`graph.py:281` `repair_json_text`, 5 passes) converts a truncation into a *valid, canonical, truncated* file. Every downstream consumer then treats it as complete, because validity is the only thing anyone checks. The repair already knows what it did — `repair_json_text` returns a `note`, and `guard_json_bytes` returns a `note` that is non-empty exactly when a repair/canonicalization happened (`filesystem_tools.py:146-149`) — **and then the note is dropped.** The provenance exists for one log line and vanishes.

**(A) Completeness scorer** — `src/artifact_quality.py` (NEW, pure, no Django, ~50 lines):

```python
def completeness(artifact: dict, required_fields: list[str]) -> dict:
    # {"present": [...], "missing": [...], "score": float}
    # a field counts as present only with a truthy, non-empty value,
    # at top level OR under field_mappings / fields
def is_salvage(note: str) -> bool: ...   # non-empty repair note ⇒ salvage
```

**(B) Provenance sidecar** — one file per workspace, not one per artifact: `workspace/{slug}/artifact_provenance.json`, written by `_fix_json_artifact` (`graph.py:394-400`) and by the `guard_json_bytes` callers (`filesystem_tools.py:291` write_file, `:353` edit_file, `:108` copy path). Record `{filename: {note, repaired_at, original_bytes, repaired_bytes}}`. Additive; no existing reader breaks.

**(C) Read-time warning** — `_read_json_artifact` (`graph.py:1214`) checks the sidecar and `logger.warning`s `"<file> is a repaired salvage (<pass>): field coverage N/M"`. **No behaviour change** — pure observability of a currently-invisible state.

**(D) The one behaviour change (deliberately narrow):** in `_run_budgeted_agent`, after `artifact_fix_fn` repairs a file, if `is_salvage(note)` **and** `completeness(...)["score"] == 0` → treat the artifact as MISSING and take the *existing* `missing_artifact_*` interrupt path (`graph.py:1899-1960`). No new interrupt reason, no new routing, no new approval type — the rerun path already exists and is tested.

### 4b. Exact files

- `src/artifact_quality.py` (NEW).
- `webapp/agents/graph.py` — `_fix_json_artifact` (sidecar write), `_read_json_artifact` (warning), `_run_budgeted_agent` (0-coverage gate).
- `webapp/agents/tools/filesystem_tools.py` — surface the `note` at `:291`/`:353`/`:108` to the sidecar writer.
- `webapp/agents/nodes/check_tracker.py:330` — `skip_product` currently keys on **file existence**; add "and not (salvage and score == 0)" so a rehydrated 0-coverage shell does not skip content analysis. (Pairs with P5.)

### 4c. Failing tests first

`tests/test_artifact_completeness.py` (NEW, pure):
1. `test_job12_shape_scores_one_of_six` — a fixture with only `field_mappings` populated, 6 requested fields → `score == pytest.approx(1/6)`, `missing` lists the other 5.
2. `test_empty_dict_and_empty_list_are_missing`.
3. `test_nested_field_mappings_and_fields_are_read`.
4. `test_no_required_fields_is_perfect`.
5. `test_is_salvage_distinguishes_note_from_empty`.

`tests/test_artifact_provenance.py` (NEW):
6. `_fix_json_artifact` on a truncated file writes the sidecar with the pass note + both byte counts.
7. A valid file writes **nothing** (no sidecar created).
8. Unrepairable → `.corrupt` rename **and** a provenance entry.
9. `test_read_json_artifact_warns_on_salvage` (`caplog`).

`tests/test_salvage_gate.py` (NEW):
10. Salvage + `score == 0` → missing-artifact interrupt path taken (`budget_exhausted`/`missing_artifact_product` reason preserved verbatim).
11. Salvage + `score >= 1/6` → **proceeds**, with the warning. (Job 12's own artifact is 1/6 → still proceeds. This is the deliberate choice not to fail the job over partial data.)
12. Clean artifact → no sidecar read, no warning, path unchanged.

### 4d. Rollout / rollback

- **Rollout:** (1) scorer + pure tests; (2) sidecar write (additive); (3) read-time warning (additive); (4) the 0-coverage gate (the only behaviour change, last, behind `ARTIFACT_SALVAGE_GATE=1`).
- **Rollback:** unset `ARTIFACT_SALVAGE_GATE`. Sidecars left on disk are inert.

### 4e. What could break, and why it won't

- **The gate refuses an artifact a working site depended on.** It fires only on `score == 0` — an artifact with *no* requested field and *no* core field. Every constraint-1 site that completes today has a real field map, so `score > 0` and the gate is a no-op.
- **Extra file I/O on the hot path.** One sidecar read per phase exit; the workspace is a local mount.
- **Sidecar drifts from the artifact** (M4 copy guards are byte-identical by design and won't copy it). Accepted and explicit: the sidecar is workspace-scoped observability, not a File-Master citizen. Copying it to the File Master is a deliberate follow-up, not part of this fix.
- **Constraint 7 (719 passed / 2 failed).** The two failures are P4's and are fixed in this plan; nothing here touches a passing test.

### Confidence: **MEDIUM-HIGH (0.75)**. Biggest risk: `product_analysis.json`'s field-map shape varies by agent version (top-level vs `field_mappings` vs `fields`), so the scorer can under-count and fire the gate spuriously. Mitigation: read all three locations (test 3) and ship the gate behind an env flag for one cycle.

---

## 5. P4 — Date-bomb (`recompute_date_reliability`)

### Classification: FAIL LOUDLY (a repair job that silently no-ops is a defect)

### 5a. Mechanism

`FIXED_AT = datetime(2026, 8, 27)` is **midnight**, while the comment at `recompute_date_reliability.py:28-29` claims "end-of-day inclusive". `scraped_at` is `auto_now_add`, so every row created on 2026-08-27 or later has `scraped_at > FIXED_AT` and the `scraped_at__lte` filter excludes it — the command scans **0 rows and exits 0**. Live in prod right now; it is why 2 tests fail.

**Fix: delete the upper bound.** Keep `BROKEN_FROM` (the lower bound is the real integrity guard) and keep `FIXED_AT` only as an optional `--to` override for forensic runs.

**Why an unbounded upper window is provably safe — this is the argument the reviewer needs:** a post-fix row (created after 31ae2f4, 2026-08-25) with `date_posted_reliable=False` got that way from the *fixed* deterministic parser, which returns unreliable only for `equals_scrape_date`, `future_dated`, or *no raw date*. The command re-derives with the same unchanged parser and routes all three to `still_unreliable` / `unrecoverable` (`:87-92`, `:75-77`). The parser is pure and has not changed since 31ae2f4, so re-scanning a post-fix row **cannot change its state**. Idempotency is already tested (`test_idempotent`); the upper bound adds no protection, only a bomb.

**(B) Loudness on a silent no-op:** when `scanned == 0`, emit a `WARNING`-style block — `window matched 0 rows; if you expected repairs, check BROKEN_FROM/--to against the data` — instead of a green "scanned: 0". This is the observability fix that would have caught the bomb three hand-widenings ago. The admin view at `webapp/scraper/admin.py:293` wraps `call_command`, so the message surfaces in the Django message frame with no new view code.

### 5b. Exact files

- `webapp/scraper/management/commands/recompute_date_reliability.py` — `:29` (comment + constant), `:54-58` (filter), `:98-103` (zero-row warning), `add_arguments` (`--to`).
- No other consumer: `admin.py:293` is the only caller.

### 5c. Failing tests first

1. `test_recovers_valid_dates` — **already failing**; passes once the bound is gone (rows are created `auto_now_add` = now, i.e. after `FIXED_AT`).
2. `test_admin_recompute.py::test_apply_fixes_row` — **already failing**; same root cause, same fix.
3. `test_rows_created_after_fix_day_are_scanned` (NEW) — force `scraped_at = now() + 1 day` via `.update()`; assert it is scanned **and** fixed. This is the test that would have prevented all three hand-widenings.
4. `test_zero_rows_scanned_warns` (NEW) — empty window → output contains "matched 0 rows" and the run is not silently green.
5. `test_to_override_still_bounds_the_window` (NEW) — `--to 2026-08-01` excludes a later row (the forensic use is preserved).
6. `test_outside_window_untouched` — existing, must keep passing (the lower bound is untouched).

### 5d. Rollout / rollback

- **Rollout:** ship first of everything in this plan. It is a 6-line change to a management command with zero pipeline surface, it un-breaks 2 of the 719 baseline tests, and it is independently verifiable (`--write` dry-run first, then apply through the admin view).
- **Rollback:** `git revert` the single commit. No schema, no env, no data migration.

### 5e. What could break, and why it won't

- **Unbounded window is slow.** `iterator(chunk_size=500)` is already there; the filter is `date_posted_reliable=False`, a narrow subset. If it ever matters, `--to` bounds it.
- **It touches rows it shouldn't.** Ruled out above: the P0-13 rules + deterministic parser make post-fix rows fixed points. `test_unreliable_stays_null` and `test_outside_window_untouched` pin both edges.

### Confidence: **VERY HIGH (0.97)**. Biggest risk: none material. Ship it first.

---

## 6. P5 — Stale-artifact re-injection on resume

### Classification: DEGRADE (cache-when-identical, invalidate-on-diff) — never force a re-run

### 6a. Mechanism

`_compute_rescrape_skip_flags` (`webapp/agents/nodes/check_tracker.py:68-110`) is a cache lookup with an incomplete key. It diffs only `target_fields` (`:93`) + `input_mode`/`search_criteria` (`:94-97`), and then sets `skip_site = True` **unconditionally** (`:99`) on the theory that "site_analyzer reads zero config fields; the site structure hasn't changed." Job 12 shows the hole: the rehydrated analysis carried a *strategy-relevant* claim that this run's own fresh evidence contradicted.

**(A) Widen the fingerprint — no migration.** `page_type` and `site_type` already exist as `ScrapeJob` columns (the test at `tests/test_recompute_date_reliability.py:50` creates a job with `page_type="job_posting"`). Add them to the diff:

```python
config_changed = (
    fields_changed
    or (state.get("page_type") or "") != (prior.page_type or "")
    or (state.get("site_type") or "") != (prior.site_type or "")
)
skip_site = True            # still True — but see (C)
skip_product = not nav_changed
skip_code = skip_product and not config_changed
```

`skip_code` is where `target_fields` genuinely matters (the generated scraper bakes in the field map); `page_type` belongs there too, not just in `fields_changed`.

**(B) Stamp rehydrated artifacts.** `webapp/agents/nodes/setup_workspace.py` `_restore_from_archive` (`:77-125`) is the rehydration point. It already calls `guard_json_bytes` at `:104`, keeps the repaired bytes (`_bytes = guarded`, `:117`), and has the repair `note` in hand at `:112-116` — where it only logs. Write the same `artifact_provenance.json` sidecar as P3 there: `{source: "rehydrated from scrapers/{slug}/analysis/<file>", at: iso, repair_note: note}`. Zero schema change; the File Master needs no new key.

Worth stating explicitly because it narrows the problem: **rehydration only happens when the matching skip flag is already set** (`setup_workspace:177-184` — `site_analysis.json` under `skip_site_analysis`, `product_analysis.json` + `navigation_analysis.json` under `skip_product_analysis`, `scraper_analysis.json` + `test_report.json` under `skip_code_generation`), and only when the local workspace copy is missing (`:89-90`, because `_finalize_job` rmtree's it). So a stale artifact can only steer a run that `check_tracker` has already decided is a resume. Fixing the skip decision (A) and the deciding artifact (C) covers the whole path.

**(C) Re-verify the deciding artifact.** This is the part that actually matters for job 12. When a *rehydrated* `navigation_analysis.api_endpoint` is about to decide the strategy, P2's probe re-runs (cheap, ≤8s, no LLM) and its verdict overrides the cached one. A cached `internal_api` vote from jobs 9/10 no longer survives contact with a fresh fetch. This is the standard cache discipline: **validate a cached decision before acting on it, when acting on it is expensive.**

Adjacent, recorded but not scoped here: `Site.last_scraped_at` (`models.py:414`) is written at finalize (`tasks.py:1060`) and **never read** by `check_tracker` or `setup_workspace` — the pipeline has an age signal and throws it away. `check_tracker`'s in-progress branch (`check_tracker.py:329-331`) decides "existing artifact" purely by local file existence with no timestamp. If P5 ever needs a real TTL, that read site is where it goes; this plan deliberately does not add one (a TTL forces re-analysis, i.e. new LLM cost, violating constraint 1).

### 6b. Exact files

- `webapp/agents/nodes/check_tracker.py` — `_compute_rescrape_skip_flags` (fingerprint), `check_tracker` (sidecar stamp on the rehydration it triggers).
- `webapp/agents/nodes/setup_workspace.py` — `:112-120` (sidecar write on rehydration, next to the existing `note` log).
- P2's `api_verify.py` supplies the re-verification; no new logic here.

### 6c. Failing tests first

`tests/test_rehydration_fingerprint.py` (NEW):
1. `test_same_fingerprint_still_skips` — identical `target_fields`/`page_type`/`site_type`/`input_mode` → all three skip flags True. **The no-regression pin** (rescrape cost stays flat).
2. `test_changed_page_type_blocks_skip_code` — `page_type` product → job_posting → `skip_code` False.
3. `test_changed_site_type_blocks_skip_code`.
4. `test_changed_target_fields_blocks_skip_code` (existing behaviour, now pinned).
5. `test_rehydration_writes_provenance_sidecar` — `_restore_from_archive` rehydration creates the `artifact_provenance.json` entry with `source` + `repair_note` (mirrors `tests/test_artifact_copy_guards.py:224` `TestRestoreFromArchiveGuard`, which already monkeypatches `artifacts.exists`/`artifacts.read` — reuse that harness).

`tests/test_rehydration_probe.py` (NEW):
6. A rehydrated `api_endpoint` with a stale `verified` verdict, whose fresh probe returns `refuted` → the strategy gate does **not** pick `internal_api`.
7. Fresh probe `unknown` (no network in test) → cached verdict stands. Fail-open again.

### 6d. Rollout / rollback

- **Rollout:** last, after P2 (it depends on `api_verify.py`).
- **Rollback:** revert `check_tracker.py`; the sidecar entries are inert.

### 6e. What could break, and why it won't

- **Widening the fingerprint makes rescrapes expensive.** Only when the user actually changed `page_type`/`site_type` — in which case re-analysis is *correct*, because the cached artifact describes a different content type. Identical-config rescrapes (the common case, and every constraint-1 site's repeat run) keep all skip flags and cost nothing.
- **Forcing a re-run would violate constraint 1.** Explicitly not done: nothing in this fix triggers an LLM phase that previously didn't run. The probe is an HTTP GET.
- **`prior` may be None on first run** — unchanged from today (`check_tracker.py:89-91` returns `(False, False, False)`).
- **A rehydrated `navigation_analysis.json` has no `verification` key** (it was written before P2 shipped). That is why the gate reads the cached verdict *and* re-probes when the artifact is stale — the fail-open default in §1a means an un-probed endpoint behaves exactly as it does today, so the first deploy after P2 cannot regress a resume.

### Confidence: **MEDIUM (0.7)**. Biggest risk: `site_type` may be null on legacy `ScrapeJob` rows, making `("" != None)` diff True and silently disabling skip for old sites. Coerce both sides with `(x or "")` on **both** sides of every comparison (as written above) and cover it in test 1 with a `prior` whose `site_type` is NULL.

---

## 7. Rollout order (one sequence, safest-first)

| Step | Fix | Why this position | Independently revertible |
|---|---|---|---|
| 1 | **P4** date-bomb | 6 lines, fixes 2 broken baseline tests, zero pipeline surface, live in prod today | `git revert`, no env |
| 2 | **P2-D** terminal-rung escalation fallback | Independent bug; converts cycle-3 no-op thrash into a deliberate retry; no dependency | one-line revert |
| 3 | **P1** exception-type preservation (`_rate_limited` marker) | Precondition for everything else; also stops a provider outage being mis-read as "agent made no progress" | one-line revert |
| 4 | **P1** ladder + typed exception + phase budget | Highest leverage per line; behind the existing `LLM_CLASSIFIED_RETRY` kill switch | env kill switch |
| 5 | **P1** `STATUS_DEFERRED` + task deferral + pre-flight | New behaviour (a job can now be deferred) and one migration (a status choice); behind `LLM_RATELIMIT_DEFER=1` | env kill switch |
| 6 | **P2** `classify_api_body` + `api_verify.py` + artifact embed | Purely additive — nothing reads it yet | no-op revert |
| 7 | **P2** flip the five consumers (template, api_section, coverage, sample, gate) | The behaviour change; one commit each so a regression is bisectable to one consumer | one-line reverts |
| 8 | **P3** scorer → sidecar → warning → 0-coverage gate | Gate is behind `ARTIFACT_SALVAGE_GATE=1`, last | env kill switch |
| 9 | **P5** fingerprint + rehydration provenance + re-verify | Depends on P2's `api_verify` | file revert |

Constraint 7 (TDD, failing tests first) applies at every step; constraint 5 (web-UI-only Railway deploys) is satisfied by env-var kill switches at every behaviour change, so a rollback never needs a shell.

---

## 8. Hard-constraint compliance

| Constraint | How this plan respects it |
|---|---|
| 1 — no new per-run LLM cost | Zero LLM calls added anywhere. The P2 probe is an httpx GET; the P1 pre-flight is a Redis GET. No "ask a model to validate" design exists in this plan. |
| 2 — must not break the constraint-1 sites | Every new check is **fail-open on `unknown`** and fires only on *positive* refutation. P4 and P2-D touch no site-specific path. Tests 2/3/4 in `test_api_verify.py` pin aya, amn (cross-domain, `count=null`), and Coveo (`count=0`) explicitly. No coverage-percentage regression gate is introduced (that design was already rejected). |
| 3 — do not undo yesterday's fixes | `count != 0`, `items_per_page > 0`, the word-boundary token list, banded prior-count, and the repair ladder all survive. P2 adds one conjunct to the gate; P3 extends the ladder with a provenance writer rather than changing its passes. Transient-class retry constants are pinned unchanged by test 3. |
| 4 — streaming stays on | Untouched. `streaming=` in `get_llm` is unaffected; the lenient `parse_partial_json` path stays and F2 still sanitizes at write time (P3 only *records* what F2 already computed). |
| 5 — no async, no new infra, web-UI-only deploys | No `LLM_ASYNC_EXECUTION` change; every sleep is sync `time.sleep`. No new service/container/queue — the 429 ledger uses the existing Django Redis cache. All behaviour changes have env kill switches. The one schema touch is a `STATUS_CHOICES` tuple entry (no new column, no data migration); Railway runs `migrate --noinput` in every service's start command (`docker-compose.yml:39,144,210`), so it needs no CLI. |
| 6 — deterministic scraper_analyzer stays deterministic | P2 makes it *more* deterministic: a cached probe verdict + a shape predicate, no LLM, no temperature. |
| 7 — TDD, 719/2/2 baseline | 2 failing tests are P4's and are fixed in step 1. Every fix above names its failing tests before the implementation. |

---

## 9. Confidence + biggest risk, per fix

| Fix | Confidence | Biggest risk |
|---|---|---|
| **P1** retry ladder + deferral | **0.85 HIGH** | Adding `STATUS_DEFERRED` touches four lifecycle behaviours (`_TERMINAL_JOB_STATUSES`, `cleanup_stuck_jobs`, `_do_schedule_next_site`'s active set, the duplicate-dispatch guard). All four were read and are enumerated in the risk list with tests 14-15 pinning them — but a fifth consumer added later would silently mis-handle the new status. The exception-preservation risk (§2c-0) is resolved by construction, not left to discovery. |
| **P2** poison-endpoint verification | **0.80 HIGH** | useinsider may coincidentally contain a dict-array and classify `verified`. Pinned by test 1 against the real body. Even then, the terminal-rung fallback (P2-D) makes the failure non-fatal — the probe is belt, the fallback is braces. |
| **P3** completeness + provenance | **0.75 MED-HIGH** | `product_analysis.json`'s field-map shape varies by agent version → the scorer can under-count and gate spuriously. Mitigated by reading three locations and by shipping the gate behind a flag for one cycle. |
| **P4** date-bomb | **0.97 VERY HIGH** | None material. Ship first. |
| **P5** rehydration fingerprint | **0.70 MEDIUM** | NULL `site_type` on legacy rows makes the diff spuriously True and disables skip for old sites. Coerce both sides of every comparison; covered by test 1. |

---

## 10. The one thing to take away

Job 12 was not one failure. It was a **trust chain with no verification anywhere**: an unverified endpoint decided the strategy, four other consumers independently amplified it, the escalation ladder had no exit from the strategy it had just picked, and when the provider pushed back the system spent 7.5 seconds and then threw away 42 minutes of work. The cheapest interventions are at the two ends — **verify the dependency before trusting it (one HTTP GET)** and **don't discard paid work over a transient (a real backoff ladder)** — and both are fail-open by construction, so neither can break a site that works today.
