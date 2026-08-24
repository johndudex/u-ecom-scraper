# Partner API Plans — Round-2 Critique 2: BUILD READINESS

**Date:** 2026-08-23 · **Reviewer:** Round-2 Critic 2 (adversarial, build-readiness focus)

**VERDICT: BUILDABLE-AFTER-FIXES.** The merged architecture is sound and the
fold's decisions are the right ones. What would actually stop an engineer is
narrower and more mechanical than Round 1's findings: (1) the fold's own
sequencing puts Phase-1b prerequisites inside Phase-1a steps, (2) both plan
*bodies* still contain pre-fold text that contradicts the fold in their
operative (step-list) sections — only Planner B's header says "the fold wins",
Planner A's plan carries no such note, (3) the B5 events queue has no routing
mechanism specified anywhere, and (4) three of the fold's mandated tests fail
on day one against specs/DB state that already exist. None of this requires
redesign; all of it requires a ~1-page errata pass over both plans plus a
handful of added steps before code is written.

Verification notes: all file:line citations checked on `file-master-artifacts`
this session; Postgres behavior verified empirically against the live
`docker compose` postgres (16.14); live row counts from the same DB. Baseline
spec tests confirmed green (14 passed, `tests/test_api_docs_views.py`).

Inputs: `api-plans-fold.md` (authoritative), `api-sync-implementation-plan.md`
(A), `api-async-implementation-plan.md` (B), `api-plans-critique.md` (R1).

---

## 1. Sequence hazards (walked in phase order)

### S1 — Phase 1a step "create (with SSRF, atomic+emit)" requires Phase 1b's emitter

Fold line 139 puts `atomic+emit` inside Phase 1a; fold line 143-144 creates
`emit()` in Phase 1b step 1. An engineer at 1a's create step (A §8 step 5,
`api-sync-implementation-plan.md:717-719`) is instructed by the fold to call
`events.emit(job.created)` — a module that does not exist and is not built
until the next phase. Three exits, two of them wrong:

- **Skip the emit** (A's plan as written has no emit — verified: the only
  `emit`/`atomic` mention in A's entire file is the M4 comment at line 205).
  Per R1-M12's own logic, `job.created` rows missing from the outbox are
  *permanently* missing (the outbox doubles as the Phase-2 replay log, B D8).
  Every partner job created during 1a silently lacks its first event.
- **Stub it** — a stub that logs and returns None is indistinguishable from
  the M4 no-op gate; nothing fails, the gap is discovered in production.
- **Pull Phase 1b step 1 forward** (correct answer: outbox model + migration +
  emit() are ~1 day and have zero dependency on the rest of 1a).

**Fix:** move "outbox model + `emit()` skeleton (created_via-gated)" to the
front of Phase 1a, or explicitly mark 1a's create step as "atomic-ready, emit
lands in 1b" with the data-loss caveat accepted in writing.

### S2 — Fold M12 as worded reintroduces the M3 race at the create endpoint

Fold line 95-96: "create endpoint wraps **create+dispatch** in
`transaction.atomic()` and calls `events.emit(job.created)` inside it."
Dispatch = `run_scrape_task.delay(job.id)` (A §4.1, line 320-323). Celery on
the same private network can dequeue and start the task before the creating
transaction commits; `run_scrape_task` opens with
`ScrapeJob.objects.get(pk=job_id)` (`tasks.py:100`) → `DoesNotExist` → the
partner's first job fails on a fast worker. This is *exactly* the bug class
R1-M3 condemned in B's dispatcher ("Celery may start the task before the beat
transaction commits"), re-created one endpoint earlier by the fold's own fix.
A's plan as written (no atomic) is safe; the fold's M12 makes it unsafe
unless dispatch moves to `transaction.on_commit`.

**Fix:** M12 must read "create + JobCallback row + `emit()` in one atomic
block; `run_scrape_task.apply_async` via `transaction.on_commit`." One
sentence, and it resolves S1's atomic half cleanly.

### S3 — A's §8 step 1 (migrations) still creates the columns the fold dropped

A's plan was revised inside §2.2 (the `JobCallback` model block,
`api-sync-implementation-plan.md:209-223`) but not in the operative sections:

- §4.1 mapping table (lines 300-301): `callback_url → callback_url`,
  `callback_secret → callback_secret` — columns on ScrapeJob that fold B2
  explicitly dropped ("callback columns on ScrapeJob are dropped", fold
  line 37-38).
- §8 step 1 (lines 709-710): "Migrations — ApiKey + ScrapeJob changes (url
  1000, search_criteria TextField, **callback_url/callback_secret**)".
- §9 still carries "Planner B is blocked on this" (Q1) and — most dangerous —
  "**No rate limiting in v1** … gunicorn's 2 sync workers are the natural
  backstop" (line 777), which directly contradicts human decision 4.

Only B's file carries a "THE FOLD WINS" header (`api-async-implementation-plan.md:7-9`).
A's file has a "REVISED" note in §2.2 only. An engineer implementing A's plan
faithfully adds dead columns and ships without the rate limiter the human
mandated. **Fix:** same header treatment for A + a 6-line errata block.

### S4 — B's operative sections carry the same stale-text disease

Verified contradictions inside B's body (decisions table is revised; the body
is not):

| B location | Says | Fold says |
|---|---|---|
| §2.1 step 4 (line 53) | no-op when `job.user_id` is null | M4: gate on `created_via="api"` (fold:69-70) |
| §3 (lines 127-128) | `JobCallback.secret_encrypted TextField`, `last_failure DateTimeField` | Decision 2: raw `secret` CharField; A's model has `last_failure` TextField + `delivered_count`/`last_delivered_at` (A:216-221) |
| §4 task 2 (line 153) | "the beat sweeper is the retry driver" | M1: legs ≥1m self-scheduled via `apply_async(countdown=…)`, sweep is the safety net (fold:62-63) |
| §9.8 (line 241) | E2E drives `field_confirmation → cleanup → finalizer` and asserts `sample_ready` in the sequence | B1: `sample_ready` is emitted at `_invoke_code_tester` (graph.py:3425 region) — §9.8 never invokes the node that emits it |

The §2.1/§3 rows are day-one compile/runtime breaks (`JobCallback.secret_encrypted`
does not exist under the fold's schema); §9.8 as written is a test that can
only pass by accident.

### S5 — Callback GET/PATCH is in the fold's Phase 1a but in neither plan's build list

Fold line 140-141 includes "callback GET/PATCH" in Phase 1a. The sync spec
defines it (`sync_api.yaml:1065-1153`, both operations, verified landed).
A's plan — the document that owns the URL table and §8 sequencing — lists
"9 paths / 10 operations" (A:253-266) with no callback path, and §8 has no
step for it. An engineer following A's §8 in order reaches step 12 (spec-lock
tests) and the mandated test **fails on a path count**: the spec has 11
paths / 12 operations, the plan builds 9/10. Same failure class for the
Phase-1b routes nobody owns: `/api/v1/jobs/{job_id}/events` (B:163 says
"Planner A owns the `/api/v1/` prefix layout"; A's plan never mentions it)
and `POST /api/v1/ws-token` — which the async spec explicitly delegates to
the sibling OpenAPI spec (`async_api.yaml:63`) while the sync spec contains
no such path (verified: no `ws-token` in `sync_api.yaml` paths). M13's
resolution (SSE via `?token=`, fold:97-98) is therefore unbuildable
conformantly: the token endpoint exists in neither spec's paths nor either
plan's URL table.

**Fix:** one canonical URL table in the fold listing every route, its phase,
and its owning plan; amend `sync_api.yaml` with `/api/v1/ws-token` before 1b.

### S6 — M8's Phase-enum lock fails against live data without a backfill

Live DB today: `scraper_step` carries 147 job_ids with **both** a
`browser_traverse` row and a `"Browser Navigation"` row; 138 done + 2 failed +
7 running rows are stored under the display string. M8 normalizes
`PHASE_MAP`'s value (graph.py:724) so *new* writes use the enum token — but
`api_job_status` passes existing Step rows through (A §4.4), so every
historical navigation job still emits `phase: "Browser Navigation"`, outside
the spec enum (`sync_api.yaml:1638-1641`). The fold's mandated Phase-enum
comparison test (fold:82-84) fails against any pre-existing job. M8's
"dedupe the seeded browser_traverse step vs the live row" is one sentence
for what is actually a per-job data migration (merge timestamps onto the
seeded row, delete the display-string row, or vice versa).

Display-side check passes: `Step.PHASE_CHOICES` already maps
`browser_traverse → "Browser Navigation"` (models.py:225-246) and templates
render `step.get_phase_display` (job_detail.html:97), so the UI is safe —
the fold's worry about template breakage is already resolved by the choices
label. Only the stored value and the duplicate row need work.

### S7 — M7's fix is assigned to A but appears in no step list, and has two call sites

Fold line 76-79: "`create_recursion_approval` routes through
`skip_approvals` … A's §4.2/§9 corrected." Verified: A §4.2 (line 353) still
asserts "job self-resumes" — the correction never landed in A's file, and
neither A's §8 nor the fold's Phase 1a sequence contains a step that touches
`services.py:413-455`. The function has **two** callers
(`services.py:259`, `tasks.py:414`); a fix at the definition covers both,
but nobody wrote that down. See also R1 hazard R5 below (loop risk).

### S8 — M10 forks A's §1.4 design mid-build

A §1.4 (lines 103-147) specifies the streaming window scanner + LRU as *the*
output design, with the page index dismissed as "future optimization (not
v1)" (A:761-763). Fold M10 (line 88-91) makes the finalize-time page index
the primary mechanism ("**replaces** the streaming window reader") while
keeping the LRU. An engineer building step 8 (A §8:722-723) must now decide:
scanner, index, or both? Both is the only correct answer (pre-index jobs
have no index file), and the index writer lives in `_finalize_job` on the
celery worker — a tasks.py change inside Phase 1a that A's plan never
mentions. Fold's open-items line 160 acknowledges the coordination but
defers it; it cannot be deferred, the endpoint and the writer must land
together or the endpoint 503s on every legacy job.

### S9 — M5's one-line prune fix does not match the code's shape

Fold line 71-72: "Prune loop condition gains `and job.created_via != "api"`."
The prune loop (`tasks.py:862-881`) iterates **FM key strings** from
`artifacts.list_keys()`, not job rows — there is no `job` in scope and no
`created_via` on a filename. The real implementation is an exempt-set query
(`ScrapeJob.objects.filter(created_via="api", output_file__in=_outs)`) run
per finalize, or a precomputed exempt set. Small, but the fold's instruction
is literally unimplementable as phrased, which is exactly the kind of
mismatch that makes an engineer improvise silently.

---

## 2. Missing steps (checklist — required by the build, present in no document)

**Settings (`webapp/config/settings.py`)**
- [ ] `CELERY_TASK_ROUTES` (or per-task `queue="events"`) routing
  `deliver_callback` + `dispatch_pending_callbacks` — **the load-bearing
  missing line.** Without it, both tasks land on the default queue and the
  events worker (B5) receives nothing while scrape workers make partner HTTP
  calls — B5's hazard reproduced verbatim (see D1 below).
- [ ] `CELERY_TASK_BEAT_SCHEDULE` entry for the 30s dispatch sweep
  (pattern exists at settings.py:150-163).
- [ ] Rate-limit knobs (window, burst, creates/hour, stream caps) + the
  SSE global-budget constants (M9) as settings, not literals.
- [ ] Optional but real: `CACHES` is absent (0 matches) — A's LRU is
  module-level by design, fine; but the M9 Redis stream-budget and M11
  fixed-window limiter need a shared Redis client helper. `views.py:123-126`
  builds one ad-hoc at import; the api package should not duplicate that.

**Requirements (`webapp/requirements.txt`)**
- [ ] `python-ulid` (B D14 — verified absent).
- [ ] Dev-only: `fakeredis` (B §9.6 mandates fakeredis pub/sub — absent),
  and a time-control strategy for the keepalive test (no `freezegun` in the
  repo; either add it or inject a clock into the SSE generator).
- [ ] `httpx` present (requirements:8). `cryptography` NOT needed (decision 2
  killed Fernet) — worth stating so nobody adds it.

**Routes / specs**
- [ ] `/api/v1/jobs/{job_id}/events` and `/api/v1/ws-token` in a URL table
  (see S5); `ws-token` added to `sync_api.yaml`.
- [ ] M11's spec amendment is **not landed**: `sync_api.yaml` contains zero
  occurrences of `429` and no RateLimits section (verified). B3/M1/M6/M14
  amendments are all in the file; M11 is the one the fold words as
  "spec gains …" without a "(done)". Must land before the rate limiter or
  A's own spec-lock test fails on the error-code vocabulary.
- [ ] `sync_api.yaml:137` tag description still says "check-site is
  deliberately NOT exposed (cross-tenant data leak)" while the same spec
  defines a scope-limited `/api/v1/check-site` at line 143. Stale prose; an
  engineer reading the tag note will skip building it and fail the lock test.

**Django plumbing**
- [ ] Admin registration for `JobCallback` + `EventOutbox` (A step 11 covers
  `ApiKey` only; fold m10's "job event timeline" admin view has no owner or
  step).
- [ ] Data migration for M8 (S6).
- [ ] CORS decision for browser SSE (M13's browser story): no
  `corsheaders` anywhere (INSTALLED_APPS/MIDDLEWARE, settings.py:37-58).
  A cross-origin `EventSource` gets no CORS headers → the browser story M13
  promised still doesn't exist. Either add the middleware for the two
  endpoints or write down "browser clients must same-origin proxy".
- [ ] `artifacts.exists()` timeout is hardcoded 30.0 (`src/artifacts.py:82`)
  with no parameter — M10's "5s + fail-fast 503 from API views" requires a
  signature change or a wrapper. Nobody lists touching `src/artifacts.py`.
- [ ] Sample-hook growth gate: `_persist_partner_sample` (A §6) writes
  `samples/sample-{job_id}.json` for **every** job — internal included — with
  no `created_via` gate and no prune path covering `samples/` (the FM prune
  targets `output_*.json` only, tasks.py:868-871). M4 gates `emit()`, not the
  sample write. Either gate the hook or accept + document unbounded FM keys.

**Deploy (see §5 for detail)**
- [ ] Compose service for the events worker (`--queues=events`).
- [ ] Railway service for the same, with the shared-vars gate from
  `docs/railway-migration.md` (PYTHONPATH etc.) — nobody owns this step in
  either plan; B §10 step 7 says "deploy book for Railway" as future
  hardening, but the worker must exist for Phase 1b to function at all.

---

## 3. Race conditions (interleavings written out)

### R1 — Cancelled partner jobs never emit a terminal event (design hole, not just a race)

Interleaving:
1. Partner POSTs `/jobs/{id}/cancel`. A's `api_job_cancel` (A §4.10, mirroring
   `views.py:454-473`) sets `status=cancelled`, saves `update_fields=["status"]`
   — **`completed_at` is never set** (verified in the internal view; the live
   DB confirms the pattern: 15 cancelled + 20 failed jobs have NULL
   `completed_at`).
2. The celery task is revoked; the finalizer never runs, so B's `job.failed`
   hook sites (finalizer ladder tasks.py:921-934 / watchdogs :1196/:1400)
   never fire.
3. B's safety net — the reconciler — is keyed on `completed_at` (fold M2,
   line 65). `completed_at > last_sweep` is not true for NULL. B's own §2.4
   claims coverage of `views.py:462`, but the fold's M2 re-keying broke
   exactly that coverage.

Result: the partner's callback for a cancelled job is **never delivered, by
any mechanism**, and the SSE stream only closes because the view polls
internal status (not because an event arrived). The 4-state projection says
`cancelled → failed`; no `failed` event exists.

**Fix (pick one, write it down):** cancel views set `completed_at`; or the
cancel path calls `emit()` directly (a 1b addition to A's cancel step); or
the reconciler keys on `status` change detection (needs `updated_at` — R1-M2
already rejected that for lack of a column; adding it to A's migration is a
one-line alternative that makes the reconciler's life generally easier).

### R2 — Double Redis publish on dedupe hit (reconciler + explicit emit)

Interleaving:
1. Finalizer, inside its atomic block, emits `job.failed` with
   `dedupe_key="failed"` → row inserted, `on_commit(publish)` registered.
2. The reconciler sweep (30s, same or next tick) computes the same terminal
   projection for the same job and calls `emit()` with the same dedupe key.
3. B §2.1 step 2's `get_or_create` returns the existing row (created=False) —
   no duplicate row, no duplicate callback (the dispatcher scans rows). Good.
4. But B §2.1 step 3 registers `transaction.on_commit(_publish_redis(...))`
   **unconditionally, before knowing whether step 2 created or found** — the
   plan's own numbered order publishes the reconciler's envelope too.

Result: SSE subscribers see the terminal event twice (harmless to callbacks,
visible to partners, and it makes the "events delivered in order per job"
promise at `async_api.yaml:427-430` noisier to honor). `get_or_create` itself
is concurrency-safe under Postgres (unique constraint on
`(job, event_type, dedupe_key)` + Django's internal savepoint/`select_for_update`
re-get), so the DB side holds; the publish side does not. Note the loser's
`select_for_update` re-get blocks until the winner's transaction commits —
bounded by finalizer transaction length, acceptable, worth a comment.

**Fix:** register `on_commit` only when `created=True`. One `if`. Also state
the IntegrityError policy explicitly (Django's get_or_create handles it; raw
`create` anywhere in the emitter must not bypass that).

### R3 — M12's atomic-wrapped dispatch (see S2). The interleaving, concretely:

T1 (gunicorn): `atomic { create job; emit; run_scrape_task.delay(id) }` →
message on Redis before COMMIT. T2 (celery worker, prefetched): `objects.get(pk)`
→ `DoesNotExist` (or, worse on a re-scrape id collision path, the *old* row).
With `max_retries=1` on `run_scrape_task` (tasks.py:95-99), the partner's job
dies once and the retry usually wins the race — a flaky 5%-style failure that
will resist diagnosis. `on_commit` for the dispatch is the only safe form.

### R4 — M7 auto-approve + recursion can form a tight loop

Interleaving: partner job (skip_approvals=True) hits `GraphRecursionError` →
fold M7 routes `create_recursion_approval` through the skip-approvals path →
auto-approve → graph resumes from the checkpoint → the same deterministic
recursion condition re-fires → auto-approve again. Nothing in the existing
skip-approvals machinery (`human_approval.py:116-124`) bounds resume counts —
it was built for *validation* interrupts where the state advances. A
recursion loop consumes a worker slot for the full soft-time-limit (7200s
default, tasks.py:85-90) per cycle.

**Fix:** auto-approve at most N recursion pauses (N=1) then fail honestly
with `error_message` set — which also gives the correct `job.failed` event.

### R5 — SSE budget vs token single-use (M13 + M9 interaction)

Interleaving: partner exchanges a ws-token (single-use, `GETDEL`,
`async_api.yaml:59-61`), connects to SSE, and is rejected by the **global**
stream budget (M9 cap=2, 1 reserved) → 503. The token is already consumed;
the partner must re-exchange. Correct but wasteful; worse, the reverse order
(token check after budget check) lets an attacker burn tokens. Specify:
budget check **before** token consumption, and document that a 503 does not
consume the token.

### R6 — Dispatch CAS-claim (`locked_until`) is sound only if both sweep and self-scheduled retries claim

M1's fold resolution (self-scheduled ≥1m legs + 30s sweep) creates two
enqueuers for the same row: the sweep (`select_for_update(skip_locked=True)`,
`locked_until = now()+5min`) and the leg's own `apply_async(countdown=…)`.
B §4 (as written, pre-fold) has only the sweep. The fold's design requires
`deliver_callback` itself to CAS-claim (`UPDATE … WHERE locked_until IS NULL
OR locked_until < now()` and check rows-affected) before POSTing — otherwise
the countdown task and a sweep that fires in the same window both deliver.
M3's resolution ("delivery state lives in the row, CAS-claim via locked_until",
fold:67-68) says this, but B's body never implements it and B's step list has
no step for the claim. Also unspecified: the self-scheduled leg must be
enqueued `on_commit` too (it is a dispatch, same discipline).

### R7 — Fixed-window limiter boundary (minor, note only)

10 req/s fixed window admits ~2× the limit across a window boundary
(30 burst at t=window-edge). Acceptable at "minimum limits," but
`Retry-After` must be computed from the window TTL, not a constant, or
partners will hammer the boundary. One sentence in the spec's RateLimits
section covers it.

---

## 4. Test infrastructure gaps (fold-mandated tests vs what exists)

| Mandated test (fold M15/others) | Buildable today? | Gap |
|---|---|---|
| Partner-shaped E2E, `sample_only=True` asserts no sample at field_confirmation (fold:30-32) | Yes | Node is in-process (`field_confirmation.py:239-256` early-returns before any interrupt); needs a DB job row + state dict. Pattern exists (`webapp/tests/test_browser_traverse_integration.py`). No blocker. |
| Two-sweep dispatch overlap (fold:102-103) | **New harness required** | Needs two DB connections observing `select_for_update`. The repo has **zero** `TransactionTestCase` and zero `django_db(transaction=True)` usage (verified). pytest-django's transactional `db` fixture makes row locks invisible to a second connection. Buildable via `@pytest.mark.django_db(transaction=True)` + threads, but nothing in-repo demonstrates it; budget a day, and decide sqlite (tests/) vs postgres (only `test_settings.py` uses sqlite, and **nothing uses `test_settings`** — verified). |
| Cross-tenant SSE 404 (fold:103-104) | Yes, partially | The 404 happens before the generator starts → `RequestFactory` + direct view call works, no async client needed. The **cap-503** half of the SSE tests needs Redis: no fakeredis pinned; `views.py:123-126` builds a module-global client at import, so injection = monkeypatching the module attribute. Specify the injection point in B's plan. |
| Phase-enum lock (fold:82-84) | Fails today | S6: 147 live dual-phase jobs + `"Browser Navigation"` rows. Needs the M8 data migration first, or the test must scope to post-migration jobs (weaker lock — say which). |
| Outbox-growth no-op under internal traffic (fold:104-105) | Yes, but only after S4's fix | B §2.1 step 4 as written gates on `user_id` — under that gate the test **fails** (all internal creates set user). The test is only correct against the fold's `created_via` gate. This is the one test that will catch the stale-plan bug — sequence it first in 1b. |
| Keepalive/25s quiet (B §9.6) | Needs clock control | No freezegun; a 25s real sleep per test is unacceptable. Inject a clock or pull the keepalive constant. |
| Spec-lock (A §7.13) | Fails today | 11 spec paths vs 10 plan ops (S5); 429 absent from the error model (§2 checklist); `invalid_callback_url` (PATCH, `sync_api.yaml:1149`) vs `validation_failed` (create, A §5) — same failure class, different code+status across endpoints; the lock test must encode both or the envelope drifts. |

Additional harness facts an engineer will hit on day one:
- The `db` fixture / pytest-django config: `webapp/conftest.py` exists;
  **`tests/` has no conftest and there is no pytest.ini/setup.cfg/pyproject**
  (verified). A's plan places `tests/test_partner_api.py` at repo root and
  describes using pytest-django's `db` fixture — whether that fixture
  resolves at the repo root depends on pytest-django auto-configuration via
  the `DJANGO_SETTINGS_MODULE` env set in A's run command (A:700-701), which
  works, but no existing root-level test exercises a DB, so there is no
  precedent to copy. Recommend putting the partner tests under `webapp/tests/`
  where the conftest and DB patterns already live.
- `CELERY_TASK_ALWAYS_EAGER` exists **only** in
  `webapp/config/test_settings.py:12`, which no test imports. The two-sweep
  and dispatcher tests need either eager mode (wrong for CAS testing) or
  direct function invocation + `on_commit` forcing (`django.captureOnCommitCallbacks`
  in Django 4.2+ is available on Django 5.1 and is the right tool — nobody
  mentions it).
- FM mocking pattern exists and is good (`tests/test_f8_f16_output_selection.py:46-54`
  sys.modules stub) — A's §7 relies on it correctly.

---

## 5. Deploy / rollback reality

**Migrations on live Railway Postgres — verified safe.** Empirically on the
local postgres 16.14 (same major as Railway): `ALTER COLUMN … TYPE
varchar(1000)` from varchar(200), and `varchar → text`, both execute as
catalog-only ALTERs (no table rewrite; the over-length INSERT that fails
before the widen is a *data* error, not a migration blocker). Live data
poses no risk: 0 rows with `length(url) > 200`, 0 with
`length(search_criteria) > 500` (verified by query). Table is 190 rows /
2871 steps — index creation and any conceivable rewrite are instant. The
`Site.url` unique URLField (models.py:383, still 200) is untouched by either
plan — correct, but say so, or someone will "fix" it too.

**Beat-before-worker is the real deploy-order hazard.** B's own risk note
("a queued-but-unacked Celery task dies with the worker; a row does not")
cuts both ways: if the beat schedule entry + task routes deploy before the
events worker exists (Railway services deploy independently; nothing orders
them), `dispatch_pending_callbacks` messages pile in the `events` queue with
no consumer — invisible backlog, silently growing, exactly B5's failure mode
mirrored. **Fix:** land `CELERY_TASK_ROUTES` + the worker service in the
same change, or route to `events` only after the worker is verified green
(routes are what make the queue choice; without routes everything is on
`celery` and B5 is unfixed — see D1). Also note the `django_celery_beat`
DatabaseScheduler requires a beat restart to pick up new
`CELERY_BEAT_SCHEDULE` entries — a Railway service redeploy of beat, which
is a separate service from the worker; the runbook needs both.

**Who creates the Railway events worker?** Nobody. `docker-compose.yml` has
no events service (verified — 8 services, none events); B's §10 step 7 defers
"deploy book for Railway" to hardening; the fold's B5 resolution mandates the
worker but assigns no step. `docs/railway-migration.md` documents the
per-service shared-vars gate (PYTHONPATH etc.) that a new service must pass —
copy the celery-worker phase block, change the start command. This is a
30-minute task with a 3-hour failure mode if the vars gate is missed
(live-verified failure class per that doc).

**Rollback story is absent (both phases).** Migrations add columns/tables
only (safe to leave in place on rollback). But: `created_via` defaults
`intake`, so a rollback of the API leaves harmless data; the **outbox** has
no such innocence — if 1b ships and is rolled back, pending rows stop being
delivered with no signal (the reconciler/dispatcher are gone). One paragraph
in the runbook: "rollback of 1b = disable beat entry + events worker; pending
outbox rows persist; partners reconcile via sync API" — matches the no-replay
contract already in the spec.

**Internal SSE view must join the M9 budget — a file neither plan owns.**
M9's resolution is a *global* (internal + partner) stream budget counted in
Redis. The internal `job_events` (views.py:1123-1215) has no Redis counting
and no keepalive, and its DB-poll fallback pins a worker for up to 40 min
(views.py:1176-1206). B's §5 instruments only the partner view. Somebody must
add the internal view to the same `sse:open` set — a change to
`views.py` that appears in no step list (fold assigns M9 to B; B's plan was
not revised for it — its §5 cap text still describes the partner-only set,
B:172).

---

## 6. Size sanity (LOC estimates from the plans)

**Phase 1a** — 8 new modules under `webapp/scraper/api/` + graph.py hook +
tasks.py index-writer + migrations + command/admin + tests:

| Piece | Est. LOC |
|---|---|
| Migrations (ApiKey, JobCallback, created_via, widenings, indexes, M8 data migration) | 150-220 |
| errors/auth/state/ssrf | 450-550 |
| create.py (+JobCallback wiring, atomic, on_commit dispatch) | 400-500 |
| status + list (+sample_ready derivation) | 350-450 |
| sample endpoint + `_persist_partner_sample` hook in graph.py | 200-250 |
| output_stream.py (scanner **and** page-index reader) + index writer in `_finalize_job` + download | 500-650 |
| scraper-code, cancel, callback GET/PATCH | 350-450 |
| Rate limiter (M11) + decorator across ~12 ops | 200-300 |
| Management command + admin + settings | 150 |
| Tests (A's 14 groups + fold-mandated additions) | 1,200-1,600 |
| **Phase 1a total** | **~3,900-4,900** |

**Phase 1b** — events package + 8 hook sites across graph.py/tasks.py/services.py
+ dispatcher + SSE + ws-token + reconciler + infra:

| Piece | Est. LOC |
|---|---|
| EventOutbox + emit() + envelope/ULID | 350-450 |
| Hook wiring (8 sites, atomic discipline, created_via gates) | 250-350 |
| dispatch + deliver (CAS-claim, self-scheduled legs, HMAC, per-attempt SSRF) | 450-600 |
| SSE view (framing, keepalive, close rules, budget, ?token=) | 300-400 |
| ws-token + reconciler + prune + beat/settings/routes | 300-400 |
| Internal-SSE budget join (views.py) + admin/observability | 150-250 |
| Compose + Railway worker + runbook | infra (~100 yaml + doc) |
| Tests | 900-1,300 |
| **Phase 1b total** | **~2,700-3,750** |

Combined **~7-9k LOC** — the largest single feature in this repo's recent
history (for scale: the Wave 1/2 simplification *removed* ~3,500). Three
pieces are secretly projects and should be re-scoped or split out:

1. **The output path (scanner + page-index + writer + LRU + download)** is
   ~1,000 LOC of its own with genuinely hard edge cases (varied key order,
   512 MB guard, index-vs-scanner fallback). Recommend making it 1a's final,
   separately-reviewable slice — it has zero dependency on create/auth.
2. **The rate limiter** touches every endpoint and needs the spec amendment
   first (§2 checklist). It is small in isolation but cross-cutting; ship it
   as the last 1a slice behind a settings kill-switch.
3. **M8** looks like a one-word change and is a data migration + test-scoping
   decision (S6). Budget accordingly.

Recommend splitting Phase 1a into 1a-i (migrations, auth, read-only
endpoints: check-site/validate-schema/status/list) and 1a-ii (create, sample,
output, callback, cancel, rate-limit). 1a-i is independently shippable and
de-risks the auth/tenancy core; 1a-ii concentrates all the cross-plan seams.

---

## 7. Summary of required pre-build fixes (the errata pass)

1. Add "THE FOLD WINS" + errata block to A's plan; fix A:300-301, :709-710,
   :353, :761-763, :777. Fix B:53, :127-128, :153, :241. (S3, S4)
2. Rewrite M12: atomic = create+callback-row+emit; dispatch via on_commit. (S2, R3)
3. Move outbox+emit() skeleton to the front of 1a, or accept the permanent
   job.created gap in writing. (S1)
4. Add `CELERY_TASK_ROUTES`/`queue="events"` + compose service + Railway
   service as explicit steps; deploy routes and worker together. (D1, §5)
5. Fix the cancelled-job terminal-event hole (R1) — cheapest: cancel paths
   set `completed_at`, or cancel emits directly.
6. `emit()` publishes only when `created=True` (R2); state the
   IntegrityError policy.
7. Canonical URL table incl. `/events`, `/ws-token`; add ws-token to
   `sync_api.yaml`; land the M11 RateLimits/429 spec amendment; fix the
   stale check-site tag note (S5, §2).
8. M8 gets a data migration and a test-scoping decision (S6).
9. M7 gets a real step (both call sites) + a bounded resume count (S7, R4).
10. Decide the output design (scanner + index both) and list the
    `_finalize_job` writer + `artifacts.exists` timeout change as steps (S8).
11. CORS decision for browser SSE; budget-check-before-token-consume (R5).
12. `deliver_callback` CAS-claim step written into B's body (R6); internal
    SSE view joins the M9 budget (§5).
