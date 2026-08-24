# Partner API Plans — Critique Fold (Stage 4)

**Date:** 2026-08-23 · **Status:** All blockers resolved, majors assigned. GO for
implementation of Phase 1 (sync skeleton → auth → jobs CRUD → sample → callbacks).

Inputs: `api-sync-implementation-plan.md` (A), `api-async-implementation-plan.md` (B),
`api-plans-critique.md` (critique, NO-GO verdict). Every finding there was verified
against code before this fold. Human decisions were taken on 2026-08-23 (4 answers,
recorded below); everything else was resolved by evidence in the critique.

---

## Human decisions (2026-08-23)

| # | Question | Decision |
|---|----------|----------|
| 1 | Callback model (B2+B3) | **Full: `JobCallback` table + PATCH/GET `/callback` + status on JobStatus** |
| 2 | Secret at rest (B2) | **Raw column, never serialized** (no Fernet, no new env key) |
| 3 | Retention vs keep-5 prune (M5) | **Exempt partner outputs from the prune** |
| 4 | Rate limits (M11) | **Minimum limits now** (~10 req/s/key, 1 stream/key, 429+Retry-After in spec) |

---

## Blocker resolutions

### B1 — single sample hook (A owns)
`_invoke_code_tester` after `_preserve_test_report` (graph.py:3413/3424) is THE hook.
B's §2.2/§2.3/D4 rewritten to consume it (B's plan file updated 2026-08-23).
Async spec's sample-persistence citation (`async_api.yaml:657-661` →
`field_confirmation.py:288-313`) corrected to the code_tester site. **Test lock:** A's
test #12 extended with a partner-shaped `sample_only=True` job asserting NO sample
artifact is written at field_confirmation (locks the C1 dead-code fact).

### B2 — one model, one policy (resolved by decision 1+2)
`JobCallback` OneToOne: `job`, `url`, `secret` (raw, never-serialized, test-locked),
`status [active|disabled]`, `disabled_reason`, `last_failure`, `delivered_count`,
`last_delivered_at`, timestamps. A's plan §2.2 amended — callback columns on ScrapeJob
are dropped. Async spec's "stored hashed at rest" erratum corrected to the raw-policy
wording (done, tests pass).

### B3 — PATCH + visibility (resolved by decision 1)
Sync spec now defines `GET + PATCH /api/v1/jobs/{job_id}/callback` (CallbackStatus /
CallbackUpdate schemas, 409 already-active, 422 validation) and a `callback` summary
field on JobStatus. Disabled ≠ dropped: PENDING rows queue while disabled; re-enable
resumes the sweep. Spec tests pass (14/14).

### B4 — delivery-time SSRF (assigned to B, hard requirement)
Re-resolve + `ipaddress` re-check before EVERY attempt, `follow_redirects=False`
explicitly, 10s connect/read cap. The spec already promised the URL-class rules at
create; delivery-time validation is now in B's plan §4. Redirect policy in writing:
**no redirects**.

### B5 — dedicated callback queue (B)
`deliver_callback` + `dispatch_pending_callbacks` run on a NEW `events` Celery queue
with its own worker (compose entry, `--queues=events`, concurrency 2-4). Zero shared
capacity with `run_scrape_task`. Never on the default queue.

---

## Major resolutions (owner → action)

- **M1** → B: legs ≥1m self-scheduled via `apply_async(countdown=…)`; 30s sweep stays
  as the safety net. Spec amended: backoff values are MINIMUM delays, 30s sweep
  resolution, attempts=6 (done).
- **M2** → B: reconciler keys on `completed_at` + new index `(status, completed_at)`
  in B's migration.
- **M3** → B: dispatch enqueues via `transaction.on_commit`; delivery state lives in
  the row (CAS-claim via `locked_until`), not the message.
- **M4** → A+B: new `ScrapeJob.created_via` (`intake|api` default `intake`); `emit()`
  no-ops unless `created_via="api"`. In A's and B's migrations/plan steps.
- **M5** → resolved by decision 3: partner outputs exempt from keep-newest-5. Prune
  loop condition gains `and job.created_via != "api"`.
  [M5-alt: N-day retention — REJECTED]
- **M6** → both: sync spec amended (done) — `failed` may supersede `scraper_ready`
  (rare finalizer failure); clients treat `failed` as authoritative terminal.
  No code change (projection already reports the truth); B emits `job.failed` after
  `job.scraper_ready` in that path.
- **M7** → A: `create_recursion_approval` routes through `skip_approvals`
  (auto-approve unattended, mirroring human_approval.py:116-124); A's §4.2/§9
  corrected. Test: partner job + GraphRecursionError → no eternal inprogress.
- **M8** → A: normalize PHASE_MAP `browser_traverse` value to the enum token
  `"browser_traverse"` (check UI templates render it; rename display via template
  map if needed). Spec-lock test gains Phase-enum comparison. Also dedupe the
  seeded `browser_traverse` step vs the live `"Browser Navigation"` row.
- **M9** → B: ONE global stream budget (internal + partner) counted in Redis;
  cap=2 with 1 reserved for health/essential traffic. Phase 1 SSE stays
  best-effort; callbacks primary. Documented in the plan.
- **M10** → A: finalize-time page index (`{offset,length}` written once per job by
  the worker) replaces the streaming window reader; FM reads from API views get
  short timeout (5s) + fail-fast 503; LRU (128 MB/worker) keyed on key+size stays
  for repeat pages.
- **M11** → resolved by decision 4: Redis fixed-window per key: 10 req/s, burst 30;
  1 concurrent stream/key (SSE), 60 creates/hour/key; 429 + Retry-After; spec gains
  a RateLimits section + 429 in the error model.
- **M12** → A: create endpoint wraps create+dispatch in `transaction.atomic()` and
  calls `events.emit(job.created)` inside it (B's contract written into A's plan).
- **M13** → resolved in spec (done): SSE accepts `?token=` via the ws-token
  exchange (same 300s/single-use machinery); browser story documented.
- **M14** → resolved in spec (done): listJobs gains documented 422
  (invalid_page/invalid_page_size/invalid_created_since).
- **M15** → A+B: test plans extended — partner-shaped E2E (locks C1), two-sweep
  dispatch-overlap test, cross-tenant SSE 404, Phase-enum lock, outbox-growth
  (internal traffic no-ops) test.

## Minor/NIT dispositions

- **m1** `dagster_code` artifact kind: emit at dagster promotion (B, one hook) —
  the enum stays.
- **m2** per-item maxLength 1000: **done in spec** (item_urls + listing_urls).
- **m3** sample mutability: **done in spec** (MUTABILITY paragraph on /sample).
- **m4** finalize closes never-run steps with completed_at: A adds the "testing-
  completed consulted only while running" test lock; no code change Phase 1.
- **m5** streaming vs error envelope: A specifies mid-stream failure = abort +
  trace_id in trailing SSE comment frame / chunked-abort for JSON; documented.
- **m6** `known_site` cross-tenant boolean: accepted (spec-mandated); named in the
  sync spec's security notes — A adds the sentence.
- **m7** gateway auth parity: B writes the exact SQL auth-state checks
  (revoked/inactive/superuser) into §8; new read-only DB credential for the gateway.
- **m8** ULID cross-process ordering: documented limitation (dedupe prevents
  duplicates, not inversions); reconciler sorts by (created_at, id) on read.
- **m9** async spec-lock test: B adds one (routes, message names, envelope
  additionalProperties) mirroring A's sync lock.
- **m10** outbox observability: B ships minimal admin view (job event timeline) +
  `outbox_pending_total` + `callbacks_disabled_total` logged per sweep.
- **m11** key lifecycle create/revoke only in v1: accepted; rotation via
  callback-secret rotation (decision 1's PATCH) covers the partner-facing need.
- **m12** B citation drift: folded into the B-plan rewrite (citations re-verified
  during the B1 rewrite).
- **n1-n8**: folded (n2 metadata-only migration note in A; n3 last_used_at note in
  A; n5 SSE close-timeout = terminal-status poll with 60s max silence; others
  documentary).

---

## Implementation sequence (merged, A + B)

Phase 1a (sync skeleton): migrations (ApiKey, JobCallback, created_via, ScrapeJob
field widenings, indexes) → api/ package skeleton + auth + error envelope →
check-site + validate-schema → create (with SSRF, atomic+emit) → status/list →
sample (code_tester hook) → output (page-index) + downloads → cancel → callback
GET/PATCH → rate limiting → spec-lock + partner-shaped tests.

Phase 1b (events): outbox model + emit() (created_via-gated) → job.created hook in
create → state/phase/artifact hooks (code_tester for sample_ready; _promote_scraper
for scraper_ready; finalizer for failed/completed) → events queue + dispatch (on_commit,
CAS-claim, self-scheduled ≥1m legs) → HMAC delivery (re-validate SSRF every attempt,
no redirects) → SSE partner endpoint (global budget, ?token=) → ws-token → reconciler
(completed_at + index) → admin/observability → async spec-lock test.

Phase 2 (unchanged): standalone FastAPI event gateway (WSS) — trigger-based, not
scheduled (first starvation event or second concurrent-stream partner).

---

## Open items (non-blocking, tracked)

- C5 registry `input_modes` matrix fix — separate ticket (n7).
- Rotation flow for API keys themselves (m11) — post-v1.
- Phase-enum UI-template check when M8 lands (display map).
- FM page-index write path lands with A's output endpoint (M10) — coordinate
  with file-master contract (no mtime on HEAD remains true).
