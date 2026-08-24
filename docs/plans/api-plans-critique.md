# Partner API Plans — Adversarial Critique (Stage 3)

**VERDICT: NO-GO** — Planner B's central `sample_ready` emission hook is provably dead
code for partner jobs (the exact C1 bug class this review was asked to catch), the two
plans ship **contradictory data models and secret-storage policies for the same
callback concept**, and the AsyncAPI's disable/re-enable policy has no implementing
endpoint in either plan or the sync spec — three independent "cannot ship as written"
defects, all fixable by decisions rather than redesign.

Every claim below was verified against the code on `file-master-artifacts`. Where a
planner's citation is wrong, the verified location is given.

Plans reviewed:
- A = `docs/plans/api-sync-implementation-plan.md`
- B = `docs/plans/api-async-implementation-plan.md`
- Contracts = `docs/specs/sync_api.yaml`, `docs/specs/async_api.yaml`

---

## BLOCKERS

### B1 — Planner B's `sample_ready` hook is dead code for partner jobs (B did not find C1; B's plan emits nothing)

**Affected:** B (fatal); A (agrees with the fix, must be made the single owner)
**Evidence:**
- B D4 (`api-async-implementation-plan.md:17`): "`sample_ready` is emitted at the end
  of `field_confirmation` … the spec itself pins sample persistence
  (`async_api.yaml:657-661` → `field_confirmation.py:280-313`)"
- B §2.2 row for `job.sample_ready`: hook = "End of `field_confirmation` … after the
  new sample file write (§2.3)". B §2.3 instructs: "Add: write the parsed record array
  to FM … at field_confirmation time."
- Verified: `webapp/agents/nodes/field_confirmation.py:239-256` — `if
  state.get("sample_only", False): … return Command(goto="run_execution")` fires
  **before** the sample-run block (`:265-331`) and before
  `_persist_field_confirmation_sample` (`:354`).
- Verified: `webapp/scraper/tasks.py:564` — `"sample_only": not job.full_extraction`;
  `webapp/scraper/views.py:2584` — `full_extraction=False` on every intake-style
  create. Partner jobs (A §4.1 "Fixed creation flags") inherit exactly this.
- Verified: `route_after_testing.py:531,552` routes PASS → `field_confirmation`, so B's
  premise "field_confirmation is the node that runs after a pass" is true — but under
  `sample_only` the node immediately bounces to `run_execution` without executing any
  of the code B wants to hook.

**Failure scenario:** B is implemented as written. Every partner job runs
`sample_only=True`. The `field_confirmation` persistence write never executes; the
`job.sample_ready` and `job.artifact.available(kind=sample)` emits never fire. Partners
receive `job.created → job.inprogress → … → terminal`. Meanwhile A's REST derivation
(correctly) reports `sample_ready` from the testing step. **The event stream and the
REST state machine disagree for every partner job, forever, silently.**
Compounding evidence: B's own test §9.8 ("E2E: drive field_confirmation → cleanup →
finalizer; assert the full event sequence … sample_ready …") would fail against B's own
design had it been run mentally against a partner-shaped job — the plan was not
executed even in the author's head against `full_extraction=False`.

**Decision demanded:** One hook, owned by one plan: `_invoke_code_tester` after
`_preserve_test_report` (A §6, `graph.py:3413/3424`). B's §2.2 row, §2.3, and D4 must
be rewritten to consume A's hook, and the async spec erratum
(`async_api.yaml:657-661` citing `field_confirmation.py:288-313`) must be added to B's
§11 errata list — it is currently absent, meaning B flagged two spec errata but not the
one that falsifies its own design.

---

### B2 — The two plans define incompatible data models and incompatible secret policies for the same concept

**Affected:** A + B (seam)
**Evidence:**
- A §2.2: `callback_url` / `callback_secret` are **columns on `ScrapeJob`**
  (`callback_secret = models.CharField` — raw at rest, C2 recommendation, §8 Q1).
- B §3: "**Shared boundary with Planner A — `JobCallback` model** (his submit/PATCH
  surface, my dispatcher's read): `job OneToOne, url URLField, secret_encrypted
  TextField, status [active|disabled], disabled_reason, last_failure`" — a **separate
  table**, secret **Fernet-encrypted** via a `CALLBACK_SECRET_KEY` setting (D9).
- B §11 "Open questions for Planner A" lists the `JobCallback` write path as open — but
  A's plan is final and contains no `JobCallback`, no PATCH, no Fernet key, and no
  `CALLBACK_SECRET_KEY` env var anywhere in its migration or settings steps.
- Both found the hashed-vs-signing erratum; A recommends raw-at-rest, B mandates
  Fernet. A §8 Q1 says "Planner B is blocked on this" — B unilaterally decided.

**Failure scenario:** Whichever plan lands second silently breaks the first: B's
dispatcher reads `JobCallback.secret_encrypted` (nonexistent under A's schema) or
A's endpoint writes a column B never decrypts. If A lands and B "adapts," the
encryption decision is made by accident in a merge, not by the human. Two final plans
that both claim to have verified their citations produced two mutually exclusive
schemas for the same three fields.

**Decision demanded:** One model, one secret policy, one owner for the
create/patch/dispatch boundary — decided now, before either migration is written.
This is the top question for the human (see QUESTIONS).

---

### B3 — The AsyncAPI's disable/re-enable policy is unimplementable: no PATCH, no callback visibility, in either plan or the sync spec

**Affected:** B (spec conformance); A (missing endpoint); both specs
**Evidence:**
- `async_api.yaml:424-431`: on exhaustion "the callback is marked disabled (`status:
  "disabled"`, `disabled_reason`, `last_failure` — **visible on the job in the sync API
  as `callback`**) … Re-enabling is an explicit partner action (**PATCH the job's
  callback**)."
- `async_api.yaml:723-734` defines the projected `CallbackStatus` shape as "readable
  via the sync API."
- `sync_api.yaml` defines **no PATCH operation and no `callback` field on `JobStatus`**
  (grep: `callback` appears only in `CreateJobRequest`, the create description, and the
  footer note "this API only stores the URL").
- A's 10 operations (§3 table) contain no PATCH and no callback-status surface. A §9
  risks do not mention it.

**Failure scenario:** A partner's endpoint goes down for 8 hours. Delivery exhausts,
the callback is disabled per B §4. The partner now has **no way to learn this** (no
read surface) and **no way to re-enable** (no write surface). B's disable policy
dead-ends in a state the partner can only escape by creating a new job. The AsyncAPI
ships promising a control loop its sibling spec cannot express.

**Decision demanded:** Either add `PATCH /api/v1/jobs/{job_id}/callback` + a `callback`
field on `JobStatus` to the sync spec and A's plan, or delete the disable/re-enable
policy from the async spec and replace it with "delivery stops; inspect via support."
Shipping both specs as-is ships a contradiction.

---

### B4 — Delivery-time SSRF is owned by no one: A flags it, B never picks it up, and the 6-hour retry window is the rebinding amplifier

**Affected:** A (incomplete) + B (absent)
**Evidence:**
- A §5 end: "Re-run the same validator **at delivery time** is Planner B's half — flag
  in the handoff: DNS rebinding between create and send means B should re-validate or
  pin the resolved IP."
- B §4 (HMAC callback delivery) contains **no re-validation, no IP pinning, no
  `follow_redirects` statement, nothing** — the handoff flag was dropped.
- B's retry schedule keeps attempting for **6+ hours** (`async_api.yaml:468`), i.e. a
  validated-at-create URL is fetched up to 6 hours later, repeatedly.
- Verified: `src/artifacts.py` and B's `httpx.post` path never re-resolve; A's
  `ssrf.py` runs only at create.

**Failure scenario:** Partner registers `https://cdn.partner.example/` (resolves
public at create). At hour 5 of the retry ladder they repoint DNS to
`169.254.169.254` or `10.x` (cloud metadata / internal services). Our `deliver_callback`
POSTs the full event envelope — including every scraped record inlined for sample
events — to the internal target. This is a textbook TOCTOU rebinding SSRF with a
6-hour window and repeated attempts, on a path the spec explicitly worried about
(`sync_api.yaml:1466-1471`).

**Decision demanded:** Assign delivery-time validation to B as a hard requirement
(re-resolve + re-check `ipaddress` before every attempt, and set
`follow_redirects=False` explicitly), or pin the resolved IP from create-time for the
life of the job. Also decide redirect policy in writing.

---

### B5 — `deliver_callback` runs on the scrape workers: one slow partner endpoint starves the entire scraping product

**Affected:** B (operational, unmitigated)
**Evidence:**
- B §4: `dispatch_pending_callbacks` "enqueues `deliver_callback.s(event_id)`" — the
  default Celery queue. There is no `CELERY_TASK_ROUTES` in
  `webapp/config/settings.py` and no queue routing anywhere in the repo.
- Verified: `docker-compose.yml:78` — `celery -A config worker … --concurrency=2
  -Ofair`, one worker process pool, default queue, shared by `run_scrape_task`.
- B's own timeouts: connect 10s + read 10s (`async_api.yaml:469`) = up to 20s of
  worker occupancy **per event attempt**, ×5 retries, ×N events.
- B §3: phase events and log events carry `dedupe_key NULL` ("duplicates allowed") and
  `job.log.appended` is "opt-in … **filter applied at delivery, not emission**" — i.e.
  emitted for every job regardless of any subscriber.

**Failure scenario:** A partner endpoint hangs (accepts TCP, never responds — the most
common failure mode). One finished job leaves ~30–60 outbox rows (state + phase +
artifact events). 60 events × 20s / 2 slots ≈ **10 minutes of zero scrape capacity**.
Three such jobs back-to-back, or one partner with several finished jobs, and
`run_scrape_task` queues behind HTTP calls to a dead endpoint. B analyzed gunicorn
worker occupancy for SSE to the second (§6) and never performed the same arithmetic on
the Celery workers its own design loads.

**Decision demanded:** Dedicated queue + dedicated worker (or `deliver_callback` moved
into the beat process with bounded concurrency), plus a cap on in-flight deliveries.
B's reliability story must not be allowed to consume the product's execution capacity.

---

## MAJOR

### M1 — The retry schedule is unimplementable as specified: a 30s beat sweep quantizes the 10s and 1m backoffs, and B explicitly rejected the mechanism that would fix it

**Affected:** B; `async_api.yaml:466-470`
**Evidence:** B §4 task 1 — beat every 30s ("30s not 300s because the shortest backoff
is 10s"); B §4 task 2 explicitly rejects Celery `retry_backoff`/countdown ("the beat
sweeper is the retry driver"). A retry scheduled at `now+10s` waits for the next
30s sweep → effective delay 10–30s (mean ~20s); the 1m leg becomes 1m–1m30s. The
partner-visible contract `backoff: [10s, 1m, 10m, 1h, 6h]` is systematically wrong at
the short end, exactly where partners build retry UX.
**Decision demanded:** Either self-schedule the retry legs (`deliver_callback.apply_async(countdown=…)`
for legs ≥1m, sweep as the safety net) — reversing B's rejection — or amend the spec
to "at least" semantics with 30s resolution.

### M2 — B's reconciler keys on a column that does not exist

**Affected:** B §2.4
**Evidence:** "for job in **terminal-status jobs updated since last sweep**" —
`ScrapeJob` has **no `updated_at`** (verified: `webapp/scraper/models.py` — only
`Site.updated_at:406`; `ScrapeJob` has `created_at/started_at/completed_at`). B claims
"Cost: one indexed query per 30s sweep" — there is no index and no column to use.
**Failure scenario:** the implementer either full-scans every terminal job every 30s
(unbounded as history grows — this deployment already has hundreds of terminal jobs)
or silently keys on `completed_at`, which is correct but is not what the plan says, and
which the finalized-ladder path sets in a `finally`-adjacent block that the unwrapped
write sites (B's own D1 list) bypass.
**Decision demanded:** Name the column (`completed_at`) and add the index
(`status, completed_at`) to B's migration, or add `updated_at` to `ScrapeJob` in A's
migration (A owns models in the same migration file — coordinate).

### M3 — Dispatch race: `select_for_update` + enqueue inside the transaction, with no `on_commit` — the exact discipline B demands of emitters is omitted in its own dispatcher

**Affected:** B §4 task 1
**Evidence:** B locks rows and "enqueues `deliver_callback.s(event_id)`" inside the
same beat transaction (`select_for_update(skip_locked=True)` requires one). B §2.1 is
scrupulous that emitters publish via `transaction.on_commit`. The dispatcher is not.
**Failure scenario:** Celery may start `deliver_callback` before the beat transaction
commits. The deliver task reads the row's *old* committed state (`pending`,
`next_attempt_at <= now`, `locked_until NULL`), delivers, and updates; beat then
commits its lease; the next sweep sees the row eligible again → **double delivery of
the same event**. At-least-once tolerates duplicates, but B's own §4 sells
"our `event_id` unique index prevents double-insert" as if it prevented double-*send*.
It does not.
**Decision demanded:** Enqueue via `transaction.on_commit` in the dispatch task (state
delivered in the row, not the message), or make `deliver_callback` take a lease token
it must CAS-claim before POSTing.

### M4 — B's D12 premise is false: internal intake jobs have non-null `user`, so the outbox grows exactly as D12 claimed to prevent

**Affected:** B (D12, §2.1 step 4)
**Evidence:** D12: "Only partner jobs (non-null `user_id`) produce outbox events …
Prevents unbounded outbox growth from internal/re-scrape traffic." Verified: every
internal creation path sets `user` — home view (`views.py:248,275`), re-scrape
(`:669`), playground (`:1543,1553`), intake (`:2539,2586`). `user` is null only for
system/auto-queued jobs (`models.py:154-159` comment). So `emit()` fires for **every
internal staff job**, each producing ~30–60 rows (phase events have `dedupe_key NULL`
and log events are emitted unconditionally and filtered at delivery — B's own §2.2).
**Failure scenario:** outbox row counts are dominated by internal traffic; the 30d
prune bounds disk but not the delivery scan, the reconciler scan, or the beat
enqueue volume from M1/B5 — all of which B justified by "partner jobs only."
**Decision demanded:** An explicit provenance flag (`created_via="api"` or
`partner_job=True`) set by A's create endpoint and checked by `emit()`. A's plan must
add the column; B's plan must check it. Neither currently does.

### M5 — Artifact retention (keep-newest-5) contradicts the "fetchable forever" guarantee; neither plan notices

**Affected:** both plans; both specs; existing code
**Evidence:** `tasks.py` finalize block prunes `scrapers/{slug}/output_*.json` to the
newest 5 per site (verified in `_finalize_job`, the prune loop after the FM publish
block). `async_api.yaml:429`: "the data remains fetchable via the sync endpoints
**forever**." `sync_api.yaml:99-102`: "`scraper_ready` implies the full output and the
scraper code are both resolvable."
**Failure scenario:** Partner A's job 480 completes (`scraper_ready`, item_count
20,000). Staff run 5 more jobs on the same site. Job 480's output file is deleted;
`GET /jobs/480/output` now returns 404 `output_not_found` while the job still reports
`scraper_ready` (with `output_available: false`). A partner building on "fetchable
forever" loses data with no event, no warning, and no tombstone.
**Decision demanded:** Either exempt partner-owned outputs from the prune, or rewrite
the guarantee in both specs ("retained for N runs / N days") and add a retention event
or documented `output_available` degradation. Silent is not an option.

### M6 — The two specs disagree on whether `scraper_ready` is terminal, and the real pipeline can produce the forbidden sequence

**Affected:** both specs; B D5 documents the sequence without resolving the fork
**Evidence:** `sync_api.yaml:1312-1314` calls `scraper_ready` "terminal success."
`async_api.yaml:69-70` allows `failed` "from any state." B D5: cleanup promotes →
`scraper_ready` emitted in-graph; the finalizer ladder (`tasks.py:921-934`, verified)
can then fail the job (e.g. `error_message` set) → `job.failed` **after**
`job.scraper_ready`. REST then reports `failed`.
**Failure scenario:** A partner reading only the sync spec treats `scraper_ready` as
terminal, stops polling, tears down their listener — and the job subsequently fails.
Their stored dataset is from a run the platform later classified failed, and they never
heard. B says "partners must treat `job.failed` as authoritative terminal" in its own
plan, but the sync spec — the document partners actually integrate against — says the
opposite.
**Decision demanded:** Amend the sync spec's `JobState` description: `scraper_ready`
is terminal *unless superseded by `failed`*; or make the finalizer classify
post-cleanup failures as `scraper_ready` with a warning. Pick one and align both specs.

### M7 — A partner job that hits `GraphRecursionError` hangs in `inprogress` forever; A's "job self-resumes" is false for that path

**Affected:** A §4.2 (waiting_approval → inprogress, "job self-resumes"); B (no resume
event or policy)
**Evidence:** `services.py:413-453` `create_recursion_approval` sets
`status=waiting_approval` **unconditionally** — no `skip_approvals` check (verified).
The auto-approve watchdog requires `job__auto_queued=True`
(`tasks.py:1421-1434`, verified) — partner jobs have `auto_queued=False`
(`models.py:147` default; only `tasks.py:1296` and the CLI set it).
`redispatch_stuck_approved_interrupts` handles APPROVED approvals only
(`tasks.py:1340-1360`). `cleanup_stuck_jobs` explicitly skips WAITING_APPROVAL
(`tasks.py:1131-1145` docstring).
**Failure scenario:** partner job exceeds a recursion budget → `waiting_approval` →
projected `inprogress` → no human exists to approve → no watchdog resumes it → the
partner polls `inprogress` for days. Both plans map the state and neither maps the
liveness.
**Decision demanded:** Either route recursion-approval creation through
`skip_approvals` (auto-approve for unattended jobs, like `human_approval.py:116-124`
does), or add non-`auto_queued` waiting jobs to an existing watchdog with a deadline.
A's §9 risk note asserts self-resume; it must cite a mechanism or be corrected.

### M8 — `browser_traverse` is stored as the phase string `"Browser Navigation"`, which is not in the spec's `Phase` enum — responses will violate the schema on every navigation job

**Affected:** A §4.4 (phases[], current_phase); sync spec conformance
**Evidence:** `graph.py:723` — PHASE_MAP maps `"browser_traverse": "Browser
Navigation"` (capitalized, contains a space). `_notify_phase` (`graph.py:870-895`)
upserts `Step(phase="Browser Navigation")`, while `_seed_pipeline_steps`
(`tasks.py:193-207`) seeds the literal `browser_traverse` — so navigation jobs carry
**two** step rows: a permanently-pending `browser_traverse` and a live
`"Browser Navigation"`. The spec's `Phase.phase` enum (`sync_api.yaml:1520-1537`)
lists `browser_traverse`, not `"Browser Navigation"`.
**Failure scenario:** every navigation/list_page/search_term partner job returns a
`phases[]` entry whose `phase` value is outside the published enum (and a duplicate
pending `browser_traverse`), and `current_phase: "Browser Navigation"` likewise.
Strict clients (OpenAPI-codegen with enum validation) reject the response. A's §4.4
("dynamically-created phases sort last — matches the existing UI") acknowledges the
rows but not the enum violation, and A's spec-lock test #13 compares only
"state/error-code vocabulary," not the Phase enum — the drift passes the test suite.
**Decision demanded:** Either normalize PHASE_MAP's value to `browser_traverse`
(cheapest; check the UI templates that render it) or add `Browser Navigation` to the
spec enum and document the duplication. Add Phase-enum to the spec-lock test.

### M9 — The SSE cap counts only partner streams; the internal UI shares the same 2 gunicorn workers, so the real budget is 2 minus internal usage

**Affected:** B §5/§6
**Evidence:** B's cap is a Redis set of *partner* streams (`sse:open`). The internal
`job_events` view (`views.py:1123`, routed `urls.py:66`) runs on the same gunicorn
service with the same 2 sync workers (`Dockerfile:41`), and its DB-polling fallback
alone holds a worker for up to 40 min (`views.py:1176-1206`, verified). B §6 notes the
internal pathology as "benign today" without subtracting it from the cap. B's own
healthcheck death-spiral analysis (2 streams → 0% capacity → Railway restarts the
service → streams die anyway) applies with 1 partner + 1 internal stream.
**Decision demanded:** A global (internal + partner) stream budget enforced in one
place, or an explicit statement that partner SSE is best-effort and the internal feed
is exempt-but-counted. Also decide whether `/api/health/raw` (`config/urls.py:7`)
needs a worker-reservation guarantee — under B's own math it currently has none.

### M10 — Sequential output pagination pins a gunicorn worker for O(file) per page, and the N×30s FM calls have no circuit breaker — A's own SSE-style starvation, judged by B's standard

**Affected:** A §1.4/§4.7
**Evidence:** A's design streams the whole file per uncached page ("a fresh page costs
one FM pass … seconds for 101 MB"). Sequential next-page walking — the *normal*
pagination pattern — is a cache miss every time (page N+1 must scan past page N's
items), so a partner walking 268 pages pays 268 full passes through
`raw_decode`-over-a-sliding-buffer (CPU-bound Python JSON on 101 MB ≈ 5–15s each) —
tens of minutes of one of two workers. Separately, `artifacts.exists()` and list
resolution use `httpx` with **30s timeouts** (`src/artifacts.py:82-84`) and no
circuit breaker: A's `state=sample_ready` list filter does one HEAD per running row —
an FM outage turns each poll into up-to-30s×N of blocked worker.
**Failure scenario:** one partner paginating aya's 101 MB output starves the API +
UI; an FM blip turns two concurrent polls into a full outage. B's §6 condemns exactly
this worker-occupancy class for SSE; A's §9 acknowledges "O(file) per uncached page"
as a cost and never as an availability risk.
**Decision demanded:** A finalize-time index (A's own "future optimization" —
`{offset, length}` per page written once by the worker) or an explicit page-window
cache keyed by file; plus a short timeout + fail-fast (503) on FM reads from API
views. Decide whether the LRU belongs in-process (2×128 MB on a 1 GB container,
compose `:84`) or in Redis.

### M11 — No rate limiting anywhere, on an API that accepts 1.2 MB bodies, by design, against 2 sync workers

**Affected:** A (explicit deferral §9: "gunicorn's 2 sync workers are the natural
backstop"); B (silent); neither spec defines limits
**Evidence:** A's own numbers: worst-case legal create body ≈ 1.2 MB (10k URLs);
polling partners; pagination walks (M10); SSE (M9). The "natural backstop" **is** the
outage: there is no mechanism that rejects load before it consumes the workers.
**Failure scenario:** one partner with a 1s poll loop + one pagination walk + one SSE
stream = both workers busy for minutes; healthchecks fail (B's own §6 spiral); Railway
restarts; in-flight jobs' API views 500.
**Decision demanded:** Minimum viable limits in v1: per-key request rate (Redis
counter), per-key concurrent-stream cap (already in B), body-size already bounded —
decide numbers, and put them in the spec's error model (429) so partners can code
against them. "None in v1" must at least be a written, human-accepted risk.

### M12 — The `job.created` interface contract is one-sided: B requires A to emit inside an atomic block; A's plan contains neither the emit call nor the atomic block

**Affected:** A + B seam
**Evidence:** B §2.2 `job.created`: "**Interface contract with Planner A:** he calls
`events.emit(...)` after creating the job" and §2.1 requires the state-event sites be
"wrapped in `with transaction.atomic():` together with their `job.save()`." A §4.1
(create.py) specifies a plain `ScrapeJob.objects.create(...)`, dispatch, 202 — **no
`events.emit`, no transaction**, and A's §8/§9 never mention the obligation.
**Failure scenario:** A lands per its sequencing (step 5 = create) before B's step 2.
Every job created in that window has no `job.created` row in the outbox — and since
the outbox doubles as the Phase-2 replay log (B D8), those events are *permanently*
missing, not delayed.
**Decision demanded:** Write the contract into **A's** plan (function signature, call
site, atomic block) or move `job.created` emission into a post-save path B owns. The
handoff note cannot live in only one document — that is how B1 happened.

### M13 — Phase-1 SSE is unusable from a browser: `EventSource` cannot set the `X-API-Key` header, and the token exchange exists only for Phase-2 WSS

**Affected:** B §5; `async_api.yaml:49-63,149-151`
**Evidence:** The SSE channel's only security scheme is `apiKeyHeader`
(`async_api.yaml:150-151`); B §5 uses "Planner A's API-key decorator." The spec's own
ws-token rationale (`async_api.yaml:49-53`: "The browser `WebSocket` API cannot send
`X-API-Key`") applies verbatim to `EventSource` — the browser SSE API has the identical
limitation. No `sse-token` path exists in Phase 1 (B mentions one only for Phase 1.5).
**Failure scenario:** a partner building the obvious thing — a browser dashboard on
the SSE bridge, the use case the spec's model-of-use describes — cannot authenticate.
Non-browser clients can (fetch-with-headers), so the transport is degraded, not dead,
but the spec's browser story silently doesn't exist until Phase 1.5/2.
**Decision demanded:** Either document SSE as non-browser-only in Phase 1, or allow
`?token=` on the SSE endpoint reusing B's §7 ws-token machinery (it is already
Redis/300s/single-use — cheap to allow on both).

### M14 — A invents response codes the spec does not define for `listJobs`, citing the wrong block

**Affected:** A §4.5; sync spec conformance
**Evidence:** A: "Out-of-range `page` → 422 per spec:903-913" and "`created_since`
(ISO-8601; unparseable → 400)." The `listJobs` responses (`sync_api.yaml:402-452`)
define **only** 200/401/403/500. Lines 903-913 are the `/output` endpoint's 422
(page/page_size bounds) — a different operation with different parameters (the list
endpoint has no `total_pages` to violate).
**Failure scenario:** spec-driven clients code against the documented response set;
A's implementation returns 422/400 the spec never promises. A's own standard ("the
spec wins for behavior," §1 header) is violated by A.
**Decision demanded:** Either add 400/422 to the listJobs responses in the spec, or
clamp/ignore bad list params (empty page = page 1). Note that ignoring is what the
sync spec's sibling `/output` does *not* do — be deliberate.

### M15 — Neither test plan would have caught B1, and B's own E2E test contradicts B's design — the highest-value test class is missing from A

**Affected:** A §7; B §9
**Evidence:** A's test #12 tests `_persist_partner_sample` in isolation and that
`_invoke_code_tester` calls it (mock) — it locks the new hook, not the *deadness of
the old one*. Nothing in A creates a `full_extraction=False` job and asserts where the
sample lands end-to-end. B's test #8 asserts a sequence through `field_confirmation`
that a partner job never executes as B describes. Neither plan tests: concurrent
dispatch (M3), cross-tenant SSE 404 (B §5 asserts the rule; B's test #6 doesn't cover
it), outbox growth under internal traffic (M4), or the Phase enum (M8).
**Decision demanded:** Add to A: an integration test that runs the real
`field_confirmation` node with `sample_only=True` and asserts **no** sample artifact is
produced there (locks the C1 fact), plus a partner-shaped end-to-end sample assertion.
Add to B: a two-sweep concurrency test (dispatch overlap, lease expiry), and make
B's E2E #8 use a partner-shaped job — which would have caught B1 before
implementation.

---

## MINOR

### m1 — `job.artifact.available(kind=dagster_code)` is in the spec's enum but has no emission point in B's hook table
`async_api.yaml:623` includes `dagster_code` in `ArtifactDescriptor.kind`; B §2.2
covers sample/scraper_code/output only. Either emit it (dagster promotion exists in the
graph) or narrow the enum. **Decision:** spec edit or a fourth hook.

### m2 — `item_urls` entries have no per-item length cap; a spec-legal 10 MB body exceeds Django's `DATA_UPLOAD_MAX_MEMORY_SIZE`
`sync_api.yaml:1402-1410` caps count (10k) but not item length. A's §4.1 relies on the
2.5 MB default as "comfortably above worst-case legal bodies" — 10k × 1000-char URLs
is legal per spec and dies at the framework layer. Add `maxLength` per item (e.g.
1000, matching `url`) to the spec, or handle `RequestDataTooBig` as a 400.

### m3 — Sample content mutates while `sample_ready` stays monotone
A §6: the hook "Overwrites on code_tester retry cycles — file existence is monotone."
State never regresses but the *records* change between fetches with no signal.
Spec is silent on sample mutability. **Decision:** document it, or version the sample
file per retry.

### m4 — The finalize ladder spuriously sets `completed_at` on never-run steps — safe today, untested
`tasks.py` step-close block sets `status=DONE, completed_at=now` on all RUNNING/PENDING
steps at finalize. A's `sample_ready` fallback ("Step(testing).completed_at IS NOT
NULL") is therefore true for **every finalized job**, including ones that failed before
testing. It happens to be unreachable in those cases (terminal status short-circuits
the derivation), but the invariant is accidental. A's monotonicity test (#4) covers
retry cycles, not this. Add a test locking "the testing-completed signal is only
consulted while status=running."

### m5 — A's `api_view` try/except envelope is incompatible with streaming responses
A §3: the wrapper "wraps the body in a try/except that converts `ApiError` → the spec
envelope." Exceptions raised *inside* a `StreamingHttpResponse` generator occur after
headers are flushed and cannot become the error envelope (this affects `/output`,
`/output/download`, and B's SSE view, which reuses A's decorator). Specify mid-stream
failure behavior (abort + log + trace_id) rather than letting it be discovered.

### m6 — `known_site: true|false` is itself a cross-tenant oracle (spec-mandated)
A's C8 correctly kills the `target_fields` leak, but the boolean + platform still
disclose "another tenant scraped this host." The spec mandates the field
(`sync_api.yaml:176-187`), so this is accepted — but it should be named as a conscious
decision in the spec's security notes, not left implicit.

### m7 — Phase-2 gateway must reimplement the auth state machine in raw SQL
B §8: the gateway verifies `X-API-Key` "by direct read-only Postgres query." A's
auth outcomes (revoked → 403, superuser owner → 403, inactive owner → 403) must be
re-encoded in that query, plus a new DB credential for the gateway. Neither plan
lists the checks to duplicate. Write them down or the gateway will ship with
hash-lookup-only auth.

### m8 — ULID monotonicity is process-local; beat and a worker can emit for the same job
B D14's guard is a `threading.Lock` — beat (reconciler, §2.4) and the celery worker are
different *processes*; interleaved ULIDs for one job can invert causal order in the
exact failover case the spec says partners should use `event_id` ordering for
(`async_api.yaml:419-422`). The dedupe key prevents duplicates, not inversions.
Acceptable if documented; currently undocumented.

### m9 — Spec-lock tests cover only the sync spec
A's test #13 walks `sync_api.yaml` paths. B has no equivalent spec-lock (its §9 has
golden fixtures but no route/enum lock). The async surface (SSE route, ws-token route,
message names, envelope `additionalProperties: false`) can drift freely.

### m10 — Outbox observability is named but not designed
B step 7 says "metrics/logging" — no metric list, no alert on outbox depth or
exhausted-callback count, no admin view for "show me partner X's event timeline."
The outbox *is* the support surface (D8); without a query UI, every partner ticket
becomes a SQL session.

### m11 — Key lifecycle is create/revoke only
A has a management command, admin registration, and `revoked_at`. No rotation flow
(old key valid for N minutes alongside new), no partner self-service, no per-key
scopes. Spec's `Forbidden` mentions "disabled/rotated" (`sync_api.yaml:1246`) —
rotation is not actually implementable today. Fine for v1 if written down.

### m12 — B's citation drift
B cites `tasks.py:223-230` for the RUNNING transition (actual: `tasks.py:217-223`),
`graph.py:3597-3603` for cleanup promotion (actual block `graph.py:3597-3610`),
`services.py:115-129` for the log callback (actual `on_llm_end` at
`services.py:90-131`). Individually trivial; collectively a sign the same plan's
load-bearing citations (B1's `field_confirmation.py:280-313`) were not re-checked.

---

## NITS

- n1 — A §4.2: a `waiting_approval` job with a sample available reports
  `state: inprogress` + `sample_available: true` + `GET /sample` → 200. Internally
  consistent with the spec's table but surprising; a sentence in the spec would help.
- n2 — A's C3 resolution is right that Postgres `varchar→text`/width-increase is
  metadata-only; worth stating that the migration is safe *because* of that, so nobody
  "fixes" it into a table rewrite later.
- n3 — A's `last_used_at` throttle dict is per-process (2 workers → 2 writes/5min/key,
  unbounded dict growth by key count). Harmless; note it.
- n4 — B's §5 "no `id:` line" decision is correct and well-reasoned; keep it.
- n5 — B's D6/SSE-close requires the generator to poll `ScrapeJob.status` (the envelope
  channel carries no internal status) — one cheap query per keepalive; say so, and
  define the close timeout if status never turns terminal (see M7).
- n6 — A's §4.9 `format=raw` mirrors `views.py:510-512` — verified correct.
- n7 — A's C5 (don't enforce the registry's `input_modes`) is right; file the registry
  fix as its own ticket so the exception doesn't calcify.
- n8 — The `check-site` 422 vs create 400 split for the same "bad URL" class is in the
  spec, not the plans; A flagged it. Leave as spec quirk, documented.

---

## WHAT CHECKED OUT (no action)

For balance, the claims that survived verification:

- **A's C1 analysis is correct and complete** — the dead-code chain
  (`tasks.py:564` → `field_confirmation.py:239-256`) is exactly as A describes, and
  A's relocated hook (`graph.py:3413`/`_preserve_test_report`) is the right site.
- **A's D4 monotonicity mechanism** — `_notify_phase` never clears `completed_at`
  (`graph.py:879-881` verified); retry re-fires only reset `status`.
- **A's C8** — the cross-tenant `target_fields` leak is real
  (`views.py:2331-2337`, verified: `ScrapeJob.objects.filter(url__icontains=host)`).
- **A's C3/C4** — `search_criteria` CharField(500) (`models.py:128`) vs 50 listing
  URLs; `url` URLField() → 200 (`models.py:119`) vs spec's 1000. Both real.
- **A's C6 cancel reading, C7 code_review inertness** (PHASE_MAP lacks it,
  PIPELINE_PHASES seeds it — verified), **A's §1.4 FM facts** (no mtime on HEAD, no
  `CACHES`, 2 workers, `artifacts.read` buffers, `/stream` exists at
  `file_master/app.py:113`).
- **B's D1/D2/D3 architecture** (explicit emit + on_commit; beat outbox; dedicated
  envelope channel) is sound, and B's D6 catch that the internal `terminal_states`
  omits `captcha_blocked`/`akamai_blocked` (`views.py:1126-1130`) is correct and
  important.
- **B's §6 gunicorn arithmetic** (2 sync workers, timeout 3600, graceful 60) is
  correct per `Dockerfile:41`; its honest "accept 0 reliable streams" conclusion is
  the right read — the failure is that the cap doesn't account for internal usage (M9).
- **The 4-state projection tables are identical across both plans and both specs**
  (pending/running/waiting_approval→inprogress; completed→scraper_ready;
  failed/cancelled/captcha/akamai→failed) — the specific conformance check requested
  passes, with the terminality caveat in M6.
- **B's D10 retry-count reading** (5 backoff values → 6 attempts) is the sensible
  resolution of a real spec ambiguity; it just isn't deliverable on a 30s sweep (M1).
- **B's D11 ws-token** (Redis `EX 300` + `GETDEL`) is correct and atomic; no
  single-use race.
- **Hash-lookup auth is not timing-attack exposed** — the lookup compares a SHA-256
  digest over a unique index; the `prefix` column is display-only and uninvolved in
  authentication. No finding.

---

## QUESTIONS FOR THE HUMAN

Only the ones that are genuinely yours (policy, not engineering):

1. **`callback_secret` at rest (B2 + A's Q1).** Raw in a never-returned column (A's
   recommendation; zero new dependencies, blast radius = DB compromise), or
   Fernet-encrypted with a new `CALLBACK_SECRET_KEY` secret to manage on Railway (B's
   mandate; new dependency + key-rotation duty)? This blocks both plans' migrations.

2. **Callback data model (B2).** Columns on `ScrapeJob` (A) or a separate
   `JobCallback` OneToOne with `status/disabled_reason/last_failure` (B)? The separate
   table is required if you accept B3's PATCH/visibility fix; the columns are required
   if you want the smallest possible v1. These are mutually exclusive.

3. **Disable/re-enable policy (B3).** Build `PATCH /api/v1/jobs/{job_id}/callback` +
   callback status on `JobStatus` (async spec as written), or delete the re-enable
   promise and make disable terminal (smaller v1, spec edit)?

4. **SLA posture for delivery (B5, M1).** Are callback deliveries allowed to share
   Celery capacity with scrape jobs, or do they get their own worker/queue? And is
   "at-least-once, best-effort timing" the partner-facing promise (in which case M1's
   spec amendment is fine), or do you want the 10s/1m legs honored (own worker +
   self-scheduled retries)?

5. **Retention vs. "fetchable forever" (M5).** How long must a partner's full output
   remain retrievable — indefinitely (exempt partner outputs from the keep-5 prune and
   accept FM volume growth), or N days with a documented degradation?

6. **`scraper_ready` terminality (M6).** Is `scraper_ready` final (post-cleanup
   failures must not regress it — code change in the finalizer), or can `failed`
   supersede it (sync-spec edit + partner docs)?

7. **Rate limits in v1 (M11).** Ship with none (accepted risk, documented), or
   minimum per-key limits now? If now: rough numbers (req/s, streams/key, bytes/day)
   so the spec can publish them.

8. **Internal-vs-partner job marking (M4).** Approve adding a `created_via` /
   `partner_job` flag set by the API create path and read by `emit()`? It is the only
   clean way to keep internal traffic out of the partner outbox.

9. **Browser SSE in Phase 1 (M13).** Ship SSE as non-browser-only and say so, or spend
   the small cost to accept `?token=` on the SSE endpoint via B's existing ws-token
   machinery?
