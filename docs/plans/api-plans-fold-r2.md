# Partner API Plans — Round-2 Fold (Stage 6)

**Date:** 2026-08-24 · **Status:** All R2 blockers resolved; specs re-tested 14/14.

Inputs: `api-plans-critique-r2-verifier.md` (4 fixes BROKEN), `api-plans-critique-r2-build.md`
(BUILDABLE-AFTER-FIXES), `api-plans-critique-r2-spec.md` (NO-GO narrowly). This fold is
authoritative over the round-1 fold and both plan files.

**R2's theme:** the round-1 fold's DECISIONS were right; its EXECUTION had document
lag — "done" claims that weren't, plan bodies still carrying pre-fold text, and
amendments that created new seams (ws-token homeless, CallbackStatus forked).

---

## R2 blockers → resolutions (all landed)

### R2-B1 — ws-token existed in neither spec
**FIXED:** `POST /api/v1/ws-token` (createWsToken) added to sync_api.yaml — 201 with
`{token, expires_in: 300, connect_url}`, 429 wired. Async's disclaim now resolves.

### R2-B2 — CallbackStatus forked between specs
**FIXED:** async's `last_failure` re-typed string (was date-time), gains
`last_delivered_at`/`delivered_count`/`pending_count` — field-for-field identical to
sync's CallbackStatus now. Vocabulary test extended next commit to lock parity.

### R2-B3 — B4 (delivery-time SSRF) fold claim was false
**FIXED:** written into B's plan §4 as a HARD REQUIREMENT (re-resolve + ipaddress
re-check before EVERY attempt, `follow_redirects=False` always, violation →
permanently_failed + disabled, never POST).

---

## Verifier BROKEN fixes → real resolutions

| Fix | Verifier verdict | R2 resolution |
|-----|-----------------|---------------|
| M5 prune exemption | Unwritable as one-liner | Real design: reverse lookup `ScrapeJob.output_file → job` inside the prune loop (~10 lines); unowned files (no job row / pre-migration outputs) default to PRUNABLE — documented policy, not silent. Owner: A, in the output-endpoint slice. |
| M7 recursion auto-approve | Category-confused (no interrupt to skip; re-invoke re-raises) | FAIL-FAST instead: at `services.py:258` / `tasks.py:414` call sites, when `skip_approvals` is set, do NOT create a recursion approval — set `status=failed`, `error_message="graph recursion limit"`, emit `job.failed`. ~5 lines. The approval flow stays for internal jobs. |
| M11 spec half | Absent | FIXED in spec: `x-rate-limits` info block, `RateLimited` response component, 429 on create/list/ws-token, 6 new error codes added to the Error model's code list, 60 s re-enable cooldown documented. |
| M12 not written into A | Document lag | FIXED: A's §4.1 now carries the full contract — atomic(create, JobCallback, emit) + `on_commit(dispatch)` — with the explicit note that dispatch OUTSIDE the transaction avoids the M3 race at create (Critic 2's finding). |

## Verifier FIXABLE-WITH-NOTES → adopted notes

- **B1 pass-gate**: the sample hook at `_invoke_code_tester` fires on FAILED retry
  reports too — emit ONLY when the test report's pass signal is true AND
  `get_or_create(dedupe_key="sample:{job_id}")` first-wins. In A's hook + B's table.
- **REST-vs-events m4 divergence**: state-gate the REST sample_ready fallback to
  `job.status == "running"` — otherwise finalize's close-block stamps
  `completed_at` on never-run testing steps and REST reports
  `sample_available: true` while events correctly emit nothing (verifier's
  sharpest new finding). A's state.py + a lock test.
- **B5 deploy mechanics**: events worker = compose service running
  `migrate && celery -A config worker -Q events`; without migrate it crash-loops
  into invisible Redis buildup. `CELERY_TASK_ROUTES` in settings routes
  `deliver_callback`/`dispatch_pending_callbacks` to `events`. Railway service
  creation is a deploy step in the sequence (below).
- **M3 poison events**: `attempts` increments on every attempt INCLUDING lease
  expiry (stale-lease sweeper counts it) — otherwise disable-on-exhaustion never
  fires. Lease state machine: pending → leased (locked_until) → delivered |
  pending(retry, next_attempt_at) | permanently_failed. Stale-lease sweep =
  `locked_until < now() - 5m` → requeue with attempts+1.
- **M10 write ordering**: the page index writes AFTER the schema prune rewrites
  the artifact (tasks.py:967) and BEFORE the workspace rmtree (tasks.py:855) —
  in A's output slice, coordinated with B's artifact event (size/sha from the
  FINAL bytes).

## Spec/security fixes landed this round

- Secret policy unified in sync (was double-stated): raw-at-rest wording,
  maxLength 128→256 matching async + CallbackUpdate.
- M6 terminality caveat now in BOTH specs (sync JobState + async state model).
- Re-enable hardening: 60 s cooldown, re-arm delivers queued PENDING rows,
  re-armed rows stay prunable; rotation = atomic swap at next dispatch,
  30 s dual-signature window for in-flight deliveries.
- SSE auth failures: 401 immediately; single-use token means EventSource
  auto-reconnect 401s — clients must onerror → mint fresh → re-GET. Documented
  in the SSE channel notes (this is the honest behavior; token reuse would
  defeat single-use).
- SSE operation-level security now includes `eventTokenQuery` (channel level
  already had it — the operation resolved header-only).
- m6: known_site cross-tenant disclosure named in the sync spec's check-site
  description.
- Error model code list gained: `invalid_page`, `invalid_page_size`,
  `invalid_created_since`, `invalid_callback_url`, `callback_already_active`,
  `rate_limited`.

## Build critic's top-5 → sequence correction

1. **Outbox + emit move to the FRONT of Phase 1a** (create depends on it). New
   slice 1a-i below.
2. Plan-body staleness: FIXED this round (A banner + 6 body sites; B 5 sites +
   test #8 partner-shaped).
3. M12 atomic fix: `on_commit(dispatch)` — in A's §4.1 now.
4. Events queue routing: `CELERY_TASK_ROUTES` + `-Q events` + migrate-on-start
   in the compose/Railway service — added to sequence step 1b-0.
5. **Cancelled-job terminal-event hole**: cancel sets `status` but not
   `completed_at` (15 cancelled + 20 failed live jobs have NULL) — the
   reconciler keyed on `completed_at` would miss them. Fix: cancel view sets
   `completed_at=now()` (one line) + migration backfills NULL `completed_at`
   for terminal-status rows. In A's step 1 + B's reconciler precondition.

## Corrected implementation sequence

**1a-i (foundations, independently shippable):** migrations (ApiKey, JobCallback,
created_via, url/search_criteria widenings, Step phase data-migration,
completed_at backfill) → events/outbox model + `emit()` (created_via-gated,
pass-gated sample hook, dedupe unique constraint) → api/ skeleton + auth +
errors + rate limiting → read-only endpoints (check-site, validate-schema,
status, list).

**1a-ii (seams):** create (atomic + on_commit dispatch + job.created emit,
SSRF) → sample endpoint (code_tester hook + pass-gate + REST state-gate) →
cancel (completed_at fix) → callback GET/PATCH.

**1a-iii (output, separately reviewable ~1k LOC):** page-index write path
(ordered after prune, before rmtree) + output endpoints + downloads + prune
exemption via reverse lookup.

**1b:** 1b-0 deploy mechanics (events queue + routes + worker service w/
migrate + Railway service) → dispatcher (CAS-lease, attempts-on-expiry,
self-scheduled ≥1 m legs) → HMAC delivery (SSRF re-validation, no redirects) →
SSE partner endpoint (global budget, ?token=, 401 semantics) → reconciler →
observability.

**Phase 2:** unchanged (trigger-based gateway).

## Test-infrastructure notes (build critic)

- Two-sweep overlap + SSE tests need `django_db(transaction=True)` + threads —
  zero precedent in tests/; scaffold a `tests/conftest.py` with a
  `transactional_db` fixture marker in slice 1b.
- Phase-enum lock test FAILS today against live data (147 jobs carry both
  `browser_traverse` and `"Browser Navigation"` rows) — the Step data-migration
  in 1a-i is its prerequisite, not a nice-to-have.
- `CELERY_TASK_ALWAYS_EAGER` does not exist in settings — add in 1b for
  dispatcher tests.

## Residual known-good (verified both rounds)

4-state projection complete both directions; migrations empirically safe on
Postgres 16 (catalog-only ALTERs, 0 violating rows); ws-token GETDEL atomic;
FM mocking pattern works; `Step.get_phase_display` already renders
`browser_traverse` (UI safe after data migration).
