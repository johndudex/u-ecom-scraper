# Partner API — Event/Async Half Implementation Plan

> **POST-CRITIQUE REVISION (2026-08-23):** this plan was revised per
> `api-plans-fold.md`. D4 (sample_ready hook) was WRONG — critique B1
> proved `field_confirmation` is dead code for partner jobs
> (`sample_only=True` short-circuits at field_confirmation.py:239-256
> BEFORE the sample block). D4, §2.2's sample row, §2.3, and D9/D12 are
> superseded by the fold decisions below. Where this file and the fold
> disagree, THE FOLD WINS.

Planner B. Contract: `docs/specs/async_api.yaml` (AsyncAPI 3.0, 1140 lines — normative).
Out of scope (Planner A): sync REST endpoints, `X-API-Key` auth/middleware, `POST /api/v1/jobs` route mechanics, the ApiKey model.

All `[DECISION]` marks are mine where the spec is silent or self-contradictory.

---

## 1. Decisions (with evidence)

| # | Decision | Evidence / rationale |
|---|----------|----------------------|
| D1 | **Explicit `emit()` calls + `transaction.on_commit`, NOT Django signals.** | Status writes are scattered over ~20 sites (`webapp/scraper/tasks.py:153,161,223,260,275,330,404,419,435,725,927-934,1196,1395`; `views.py:462`; `services.py:433`) and several saves carry unrelated `update_fields` (e.g. `celery_task_id` at `tasks.py:110-112`) — `post_save` would fire noise on those and cannot know `previous_state`. The codebase's own convention is explicit publish (`_publish_job_status`, `tasks.py:69-74`). |
| D2 | **Beat-published outbox, not post-commit fan-out to Celery.** | The spec prescribes it (`async_api.yaml:447-456`): `dispatch_pending_callbacks` beat task + `deliver_callback` task. Beat cadence pattern exists (`settings.py:150-163`, three 300s watchdogs). Post-commit → `delay()` gives lower latency but loses the durability the outbox exists for (a queued-but-unacked Celery task dies with the worker; a row does not). |
| D3 | **Envelope events on a dedicated Redis channel `job:{job_id}:envelope`.** | Legacy frames on `job:{id}` / `:status` / `:syslog` (`views.py:1136-1138`) are raw passthrough with no envelope. A separate channel means the partner SSE view and the Phase 1.5/2 gateway never have to sniff frame shapes, and the internal UI is untouched. |
| D4 | **SUPERSEDED (critique B1) — `sample_ready` is emitted in `_invoke_code_tester` after `_preserve_test_report` (graph.py:3413/3424), Planner A's hook.** | `field_confirmation` was NEVER reached with its sample block for partner jobs: `tasks.py:564` sets `sample_only: not full_extraction` and `full_extraction=False` for API-created jobs → `field_confirmation.py:239-256` bounces to `run_execution` before the sample write. The `code_tester` retry-cycle objection is handled by EMIT-GATING on the testing step's first completion (idempotent `get_or_create` + dedupe key `sample:{job_id}`), not by moving the hook. The async spec's citation (657-661 → field_confirmation.py:288-313) is an erratum, now corrected in the spec. |
| D5 | **`scraper_ready` is emitted in-graph at cleanup promotion; the later `job.failed` from the finalizer is legal.** | Spec pins scraper_ready to cleanup promotion (`async_api.yaml:80,93`, `graph.py:3597-3603`) and pins artifact order sample→scraper_code→output (`:643-648`). The finalizer's ladder (`tasks.py:921-934`) can still fail a job after cleanup (e.g. `error_message`); the state model already allows `failed` "from any state" (`:70`). Partners must treat `job.failed` as authoritative terminal. |
| D6 | **SSE close rule: close on `job.failed` immediately; after `job.scraper_ready` only once internal status is terminal AND the output artifact event has been relayed.** | Closing on `scraper_ready` naively would cut the stream before `job.artifact.available(kind=output)`, which the finalizer publishes *after* the graph ends (`async_api.yaml:645-648`). Also note the internal view's `terminal_states` (`views.py:1126-1130`) omits `captcha_blocked`/`akamai_blocked` — those streams never close today; the partner view must close on them (both project to `failed`, `async_api.yaml:81-82`). |
| D7 | **Phase 1 = Django SSE on gunicorn with a hard, Redis-counted cap; callbacks are the reliability story; Phase 1.5 = standalone SSE gateway; Phase 2 = WSS on that same gateway.** | See §6. This is the honest reading of the worker math. Spec locks WSS to Phase 2 (`async_api.yaml:37-47`); Phase 1.5 is additive toward it (same service gains a WS route), so no work is thrown away. |
| D8 | **Outbox table doubles as the event log for Phase 2.5 replay** (`since_event_id`, `async_api.yaml:349-351,765-767`). | One table, two consumers (callback dispatcher + future replay). ULID lexicographic ordering is the cursor. |
| D9 | **SUPERSEDED (fold decision 2) — raw secret in a never-serialized column.** | Hashing is impossible (we sign every attempt). Human chose raw-column over Fernet (no new env key/rotation duty); blast radius = DB compromise which already exposes all job data. Test-locked: no endpoint/log/admin serialization. Spec erratum corrected in async_api.yaml. |
| D10 | **5 retries after the initial POST (6 attempts total), consuming all five backoff values.** | `async_api.yaml:417` "Retries: 5 attempts" vs `x-retry.backoff` list of five values (`:466-470`) — 5 attempts total needs only 4 gaps. **Spec ambiguity to raise.** The chosen reading uses every listed value. |
| D11 | **`ws-token` in Redis (`EX 300` + atomic `GETDEL`), not DB, not signed-stateless.** | Single-use is a hard spec requirement (`:59-61,133`) and cannot be enforced statelessly; the WSS gateway already needs Redis (pub/sub), so this adds no new dependency and no DB credential on the gateway. |
| D12 | **SUPERSEDED (critique M4) — gate on `created_via="api"`, not user_id.** | ALL internal intake paths set `user` (views.py:248,669,1543,2539) — the user_id gate would fill the outbox with internal traffic (30-60 rows/job). New `ScrapeJob.created_via` column (A's migration) set to `api` only by the partner create endpoint; `emit()` no-ops otherwise. |
| D13 | **New code lives in `webapp/scraper/events/`; models go in `scraper/models.py`.** | Single model registry is what all existing migrations assume; a package inside the existing app avoids new `INSTALLED_APPS` boilerplate. |
| D14 | **ULID via `python-ulid` + process-local monotonic guard.** | Spec mandates ULID and monotonicity-per-producer (`:513-517`); no ULID lib is currently pinned (`webapp/requirements.txt` — verified absent). The guard serializes generation under a `threading.Lock` with same-ms increment, since bare `ULID.generate()` is random within a millisecond. |

---

## 2. Event emission architecture

### 2.1 The emitter

New module `webapp/scraper/events/emitter.py`:

```
emit(job_id, event_type, data, dedupe_key=None) -> EventOutbox | None
```

Behaviour:
1. Build `EventEnvelope` — `event_id` (ULID, D14), `type`, `occurred_at` (UTC ISO-8601), `job_id`, `user_id`, `data`. Fields exactly per `async_api.yaml:504-553`; `additionalProperties: false`.
2. `get_or_create` on `(job, event_type, dedupe_key)` (§3) — idempotent across graph resume/finalize re-entry.
3. Register `transaction.on_commit(lambda: _publish_redis(job_id, envelope, channel="job:{id}:envelope"))`. If no transaction is active, Django runs the callback immediately — so call sites need no branching.
4. Return `None` without writing when `job.user_id` is null (D12).

Transactional discipline: the four *state* emission sites (created / inprogress / terminal) are wrapped in `with transaction.atomic():` together with their `job.save()` — that is what makes the outbox row and the status change a single write. Artifacts/phase/log events are informational; standalone insert is fine.

### 2.2 Hook points (file:line today)

| Event | Hook point | Notes |
|---|---|---|
| `job.created` | Partner submit handler (Planner A's `POST /api/v1/jobs`), in the same `atomic()` block as the 202-determining save. | Payload per `async_api.yaml:555-580`: state `inprogress`, url, content_type, input_mode, callback echo. **Interface contract with Planner A:** he calls `events.emit(...)` after creating the job; the signature is above. |
| `job.inprogress` | `_run_graph_job` at the RUNNING transition — `tasks.py:223-230`, piggyback the existing `_publish_job_status` call. `previous_state: null`. | Resume path (`tasks.py:330-332`) emits it too — `previous_state` read from the last outbox state row. |
| `job.sample_ready` + `job.artifact.available(kind=sample, records=[…])` | **REVISED (B1):** `_invoke_code_tester` after `_preserve_test_report` (graph.py:3413/3424), co-located with Planner A's `_persist_partner_sample` hook. Dedupe key `sample:{job_id}` makes retry cycles idempotent (first pass wins). | `item_count` = records length; `sample_url` = `/api/v1/jobs/{id}/sample`. |
| `job.scraper_ready` + `job.artifact.available(kind=scraper_code)` | `_invoke_cleanup` after `_promote_scraper` success — `graph.py:3597-3605`, adjacent to `_notify_phase(cleanup, "done")` at `:3600`. Emit **only when** `execution_status == "SUCCESS"` and `scraper_path` is truthy (gate mirrors `graph.py:1006`). | `scraper_code_url` = `/api/v1/jobs/{id}/scraper-code`. Per-job key `scrapers/{slug}/jobs/scraper-{job_id}.py` already written at `graph.py:995` — `size_bytes`/`sha256` computable there via `src/artifacts`. |
| `job.failed` | (a) Finalizer ladder `tasks.py:921-934`, next to `_publish_job_status(job.id, job.status)` at `:1079`. (b) Safety net §2.4 covers the unwrapped sites (`tasks.py:1196`, `:1395`, `views.py:462`). | `reason` = `job.error_message` (`async_api.yaml:596-600`). `captcha_blocked`/`akamai_blocked`/`cancelled` all project here. |
| `job.artifact.available(kind=output)` | `_finalize_job`, in the FM publish loop region (`tasks.py:806-811`) — after bytes land in FM so `size_bytes`/`sha256` are real; `item_count` = `job.product_count` (ground truth set at `tasks.py:762-786`). | Fires between `scraper_ready` and terminal status, matching `async_api.yaml:645-648`. |
| `job.phase.updated` | `_notify_phase` (`graph.py:870-895`) — already the single choke point; it already upserts `Step` and publishes. | `phase` from `PHASE_MAP`; `phase_status` ∈ running/done/failed. `dedupe_key=None` (duplicates allowed). |
| `job.approval.required` | The two approval publishers: `services.py:365-374` and `services.py:443-451`. | Partner state stays `inprogress` (`async_api.yaml:684-689`). Payload `{approval_id, approval_type, question, options}`. |
| `job.log.appended` | `_ScrapeCallbackHandler.on_llm_end` (`services.py:115-129`). | Opt-in per `subscribe.events` (`:747-764`). **Never sent over callbacks** — `partnerCallback` messages list (`:302-315`) excludes it. Filter applied at delivery, not emission. |

### 2.3 Prerequisite: sample persistence (spec-flagged)

`async_api.yaml:657-661` is explicit: structured sample records do not exist today; persisting `scrapers/{slug}/samples/sample-{job_id}.json` at field_confirmation time is **required** before the sample event can inline `records`. Today `field_confirmation.py:280-313` builds `sample_text` (a formatted string) only. Add: write the parsed record array to FM via `src/artifacts.write(scrapers_key(slug, "samples", f"sample-{job_id}.json"))`. This is also the backend addition the sync spec needs (Planner A's endpoint 3) — one shared change, both planners consume it.

### 2.4 Safety net: the projection reconciler

Because status writes are scattered (D1) and three terminal sites bypass `_publish_job_status` entirely (`tasks.py:1196`, `:1395`, `views.py:462`), the dispatch beat task also runs a cheap reconciler:

```
for job in terminal-status jobs updated since last sweep:
    expected = project(job.status)            # 8 → 4 map, async_api.yaml:75-82
    last     = last state event in outbox for job
    if expected != last: emit(job.failed | job.scraper_ready, ...)
```

Cost: one indexed query per 30s sweep. This guarantees a partner *never* misses a terminal event even when the watchdog SIGKILLs a worker mid-finalize. Latency ≤30s for the pathological path only; the happy path emits inline.

Projection map (`async_api.yaml:75-82`): `pending|running|waiting_approval → inprogress`; `completed → scraper_ready` (already emitted in-graph; reconciler no-ops); `failed|cancelled|captcha_blocked|akamai_blocked → failed`.

---

## 3. Outbox design

Model in `webapp/scraper/models.py` (D13), migration `scraper/migrations/0xxx_eventoutbox_*.py`.

```
EventOutbox:
  id             BigAutoField pk                      # debug ordering only
  event_id       CharField(26, unique, db_index)      # ULID — dedupe + future replay cursor
  job            ForeignKey(ScrapeJob, db_index, on_delete=CASCADE)
  user_id        IntegerField()                       # denormalized at emit: delivery never re-reads the job
  event_type     CharField(30, db_index)
  payload        JSONField()                          # the COMPLETE EventEnvelope, byte-exact on the wire
  dedupe_key     CharField(100, null=True, blank=True)
  created_at     DateTimeField(auto_now_add=True, db_index)

  # callback delivery (only path that needs delivery state — SSE/WS are live-only)
  status         CharField(choices=[pending, no_callback, delivered, exhausted, skipped])
  attempts       IntegerField(default=0)
  next_attempt_at DateTimeField(null=True, db_index)
  locked_until   DateTimeField(null=True)             # dispatch lease
  last_error     TextField(blank=True)
  delivered_at   DateTimeField(null=True)

  unique_together: (job, event_type, dedupe_key)      # Postgres: multiple NULLs allowed →
                                                      # log/phase events (dedupe_key NULL) may repeat
```

Conventions:
- State events: `dedupe_key` = target state (`"sample_ready"`). Artifact events: `dedupe_key` = kind (`"output"`). Phase/log: `NULL`.
- `status='no_callback'` is set at insert when the job has no active `JobCallback`; the dispatcher ignores such rows. They remain the event log (D8).
- Prune (new beat task, daily): delete `delivered`/`exhausted` rows older than 30d. Mirrors the FM prune pattern (`tasks.py:866-886`). `pending` rows never pruned.

**Shared boundary with Planner A — `JobCallback` model** (his submit/PATCH surface, my dispatcher's read):

```
JobCallback: job OneToOne, url URLField, secret_encrypted TextField,
             status [active|disabled], disabled_reason TextField, last_failure DateTimeField
```

`async_api.yaml:723-734` defines its projected shape (`url, status, disabled_reason, last_failure`).

---

## 4. HMAC callback delivery

Spec-exact (`async_api.yaml:434-470`):

- **Headers**: `X-Scraper-Signature: t=<unix_ts>,v1=<hex(hmac_sha256(f"{t}." + raw_body, callback_secret))>`; `X-Scraper-Event-Id`; `X-Scraper-Job-Id`. Sign the **exact request bytes** — serialize once, sign and send the same buffer.
- **One EventEnvelope per POST** (`:411`); body = the stored `payload` column verbatim (event_id stable across retries, `:418`).
- **Timeouts**: connect 10s, read 10s (`:469`) — `httpx.post(..., timeout=httpx.Timeout(10.0, connect=10.0))`. `httpx` already pinned (`requirements.txt:8`).
- **Success** = any 2xx. Everything else, or timeout/transport error, schedules a retry.
- **Retry schedule** (D10): attempt 1 immediately; retries at **+10s, +1m, +10m, +1h, +6h**.
- **On exhaustion**: mark the `JobCallback` `status='disabled'` + `disabled_reason` + `last_failure`; remaining `pending` rows for the job become `skipped`; data stays fetchable via sync API (`:424-431`). Re-enable = partner PATCH (Planner A); on re-enable, delivery resumes from *now* — **no backlog replay** [DECISION, spec silent; the spec's own answer for the disabled window is sync fetch].
- **Railway egress IPs are not static** (`:180-184`) — the signature *is* the authenticity proof; do not attempt IP pinning.

### Two Celery tasks (mirrors the spec's own design, `:448-456`)

1. **`dispatch_pending_callbacks`** — beat, **every 30s** (`settings.py:150-163` pattern; 30s not 300s because the shortest backoff is 10s). Each run:
   - Selects `status='pending' AND next_attempt_at <= now() AND (locked_until IS NULL OR locked_until < now())` with `select_for_update(skip_locked=True)` (single beat container — compose `:121-152` — but this makes overlap harmless).
   - Sets `locked_until = now() + 5min`, enqueues `deliver_callback.s(event_id)`.
   - Runs the §2.4 reconciler.
2. **`deliver_callback(event_id)`** — one POST. Records outcome + `next_attempt_at` from the table (explicit `countdown`-style scheduling; Celery's built-in `retry_backoff` is 2^n and its `retry_backoff_max` default of 600s would silently clamp the 1h/6h legs — do **not** use `autoretry_for`/`retry_backoff` despite the spec naming them; the beat sweeper is the retry driver). Never raises past exhausting the schedule.

`acks_late` is default-False (`settings.py:141-146`); a killed `deliver_callback` loses only the lease, which expires in 5min and the sweeper re-enqueues — at-least-once holds without flipping the fleet-wide setting.

Partner-side dedupe on `event_id` is their contract (`:418-422`); our `event_id` unique index prevents double-insert of the same logical event.

---

## 5. SSE design (Phase 1 — Django)

New view `partner_job_events` at `GET /api/v1/jobs/{job_id}/events` (route lands in `webapp/scraper/urls.py` — `config/urls.py:9` includes it at `""`; Planner A owns the `/api/v1/` prefix layout).

- **Auth**: Planner A's API-key decorator; owner check `job.user_id == key.user_id`, 404 on mismatch — mirrors `_get_job` (`views.py:33-39`) and the spec's non-leaking `job_not_found` (`:801-818`).
- **Framing**: `event: <message-name>` (e.g. `job.sample_ready`) + `data: <EventEnvelope JSON>`. Byte-identical payload to WS frames and callback bodies (`:504-509`). **No `id:` line** [DECISION] — emitting one would invite `EventSource` auto-reconnect with `Last-Event-ID`, which we do not honor (`:156-158, 260-262`); silence is more honest than a lie.
- **`retry: 5000` on connect** (spec SHOULD, `:262`).
- **Keepalive**: rewrite the legacy `pubsub.listen()` loop (`views.py:1151`) as `pubsub.get_message(timeout=25)`; on `None` → `yield ": ping\n\n"`. **25s** [DECISION — spec says "at least every 30 s" at `:259` and 25s for WS at `:376`; one shared constant, comfortable margin under typical 60s proxy idle timers]. This is the critique-round fix the spec calls out (`:256-263`) — the internal feed has no keepalive at all, and the in-graph "heartbeat" (`graph.py:762` region) writes SessionLog rows for the stuck-job watchdog, not transport frames (`:371-378`).
- **Open**: initial frame = the job's most recent *state* envelope re-emitted byte-identical from the outbox (one indexed query), or a synthesized `job.inprogress` (`previous_state: null`) if none. Satisfies "opens with the current state" (`:386-388`) without inventing a non-envelope frame shape.
- **Close** (D6): after `job.failed` — and note `captcha_blocked`/`akamai_blocked`/`cancelled` are terminal here even though the legacy `terminal_states` set (`views.py:1126-1130`) misses them. After `job.scraper_ready` the stream stays open until internal `ScrapeJob.status` is terminal *and* the output artifact event has been relayed, then closes. On close, emit a final `event: done` comment marker so clients stop cleanly.
- **Reconnect contract**: live-only, no replay — re-GET, then reconcile via the sync job endpoint (`:156-158`). The `subscribe.ack` snapshot in Phase 2 (`:773-797`) is the richer version of the same idea.
- **Concurrency cap**: Redis set `sse:open` of per-stream keys `sse:{uuid}` with `EX 120`, refreshed by the keepalive loop each ping; over cap → `503` + `Retry-After: 30` [DECISION — capacity, not rate]. The TTL makes the counter self-healing if gunicorn SIGKILLs a worker (plain INCR/DECR would leak).

### 6. Worker-occupancy math and verdict (the hard problem)

Facts: gunicorn **2 sync workers, timeout 3600, graceful-timeout 60** (`Dockerfile:41`). A `StreamingHttpResponse` with a sync generator pins one full worker for the stream's duration. Jobs run **10–60 minutes** (`async_api.yaml:22`).

| Concurrent partner streams | HTTP capacity remaining |
|---|---|
| 0 | 100% |
| 1 | 50% — for up to 60 min |
| 2 | **0%** — web UI, `/api/health/raw` (`config/urls.py:7`), and Planner A's entire sync API all starve |

Two compounding failure modes:
- **Railway healthcheck death-spiral.** Healthchecks are path+timeout (`docs/railway-migration.md:45`). Two open streams → healthchecks time out → Railway restarts the service → both streams die anyway. "Accept 2 streams" is really "accept 0 reliable streams under load."
- **Deploy kills.** `graceful-timeout 60` means every deploy terminates all open streams mid-job. Clients must reconnect + reconcile — which is exactly why the `retry:` hint and the no-replay contract matter.

**Verdict:**
- **Phase 1 (ship): Django SSE, cap = 2, documented as best-effort.** The spec itself makes callbacks the primary Phase 1 push channel (`:28-31`); SSE is the subscription *alternative*. Position SSE as the dev/preview transport; partners who need guaranteed delivery use callbacks or poll the sync API. This costs zero new infrastructure and matches "small change to the existing view, not new infrastructure" (`:252-255`).
- **Phase 1.5 (triggered, not scheduled): standalone event gateway** — FastAPI + uvicorn (the `file_master` pattern: `file_master/Dockerfile:15-16`, single worker, private network, no auth of its own), own compose entry + Railway service, serving the *same* path `/api/v1/jobs/{job_id}/events`. Async pub/sub consumer → SSE; concurrency bounded by file descriptors, not gunicorn workers; Django never holds a stream. Auth via the same Redis-backed short-lived token as `ws-token` (issue an `sse-token` on connect redirect, or accept `X-API-Key` verified against Postgres read-only). **Triggers**: first starvation incident, ≥2 partners wanting concurrent streams, or first paid SLA.
- **Phase 2: add WS routes to that same gateway service.** Phase 1.5 is therefore an investment in Phase 2, not a detour — the subscription registry, Redis fan-out, and auth all carry over verbatim.
- Do **not** convert Django to ASGI or pull WSS forward — the spec locks both (`:37-47`, critique round).

Note: the *internal* `job_events` (`views.py:1123`) already has this pathology today (its DB-polling fallback holds a worker for 1200×2s = 40min, `views.py:1176-1206`). Benign at current internal usage; the partner endpoint is what turns it into a cliff.

---

## 7. ws-token design

`POST /api/v1/ws-token` (route mechanics = Planner A; semantics = here), per `async_api.yaml:49-63, 129-134`:

- **Issue**: `secrets.token_urlsafe(32)`. Redis `SET ws-token:{token} <user_id> EX 300`. Response `201 {token, expires_in: 300, connect_url: "/ws/v1/jobs?token=…"}`.
- **Consume**: gateway does `GETDEL ws-token:{token}` — atomic, hence genuinely single-use. Missing/expired → handshake reject with `subscribe.nack`-style `token_expired` (`:811`).
- **Cleanup**: none needed — Redis `EX` is the expiry. No DB rows, no beat task. [DECISION] also re-check a stored `iat` server-side to bound clock/restore weirdness.
- **Leak bound**: query-param exposure into access logs is bounded by the 5-min TTL + single use (`:60-62`).
- **Ownership**: the claim carries only `user_id`; every subscribe is still checked as `job.user_id == claim.user_id` — same rule as `views.py:33-39`.
- Non-browser clients may skip this entirely and send `X-API-Key` on the handshake (`:491-494`).

---

## 8. WSS gateway — Phase 2

Declared-not-built (`:37-47, 114-128, 244`). **A separate deployable; Django stays WSGI.**

**Service shape** (`event_gateway/`, mirroring `file_master/app.py` + `file_master/Dockerfile`):
- FastAPI + `uvicorn --workers 1`, port **8003**, `EXPOSE 8003`, own `requirements.txt` (fastapi, uvicorn[standard], redis, psycopg, cryptography), own compose entry + Railway service (private network only; no public port — it fronts through the same domain/router as Django).
- **Auth on handshake**: `X-API-Key` (non-browser) or `?token=` from §7 (browser). API-key verified by direct read-only Postgres query [DECISION — same private-network trust `file_master` enjoys; avoids an HTTP hop and a service-to-service credential on every subscribe].
- **One multiplexed connection** at `/ws/v1/jobs`; subscribe/unsubscribe by `job_id` (`:191-207`) — N per-job connections would cost N handshakes/proxy slots/token exchanges.
- **Control protocol** (`:323-336, 848-928`): in — `subscribe {job_id, events?, since_event_id?}`, `unsubscribe {job_id}`, `heartbeat.pong`. Out — `subscribe.ack {job_id, state, snapshot{last_event_id, item_count, sample_url, output_url, scraper_code_url}}`, `subscribe.nack {job_not_found|token_expired|invalid_message}`, `unsubscribe.ack`, `heartbeat.ping {server_time}`, `error`, plus the nine event types.
- `subscribe.ack`'s snapshot is the late-joiner/reconnect recovery mechanism and the *only* guarantee in early releases (`:773-797`) — built from Postgres (status→projection, `product_count`) + outbox (`last_event_id`).
- **Event fan-out**: per-subscription Redis subscribe on `job:{job_id}:envelope` (D3) — zero changes to emitters.
- **`heartbeat.ping` every 25s of silence** (`:371-378, 819-829`) — app-level, not protocol-level, because proxies enforce their own idle timers regardless of ping visibility.
- **Event filtering** per `subscribe.events` (`:747-764`); default = the four state events + `job.artifact.available`. `job.log.appended` only on explicit request.
- `since_event_id` accepted and **ignored** in 2.0 (`:765-767`); the Phase 2.5 Redis Streams replay buffer is the designated upgrade and the outbox already holds the data (D8).
- [DECISION] Disconnect policy on silent clients: track last client activity; drop after 75s (3 missed pings) of two-way silence. Spec only says the client SHOULD pong.
- **wss:// *outbound* callbacks are Phase 2** and require an always-on outbound WS client that fits the Celery worker model poorly (`:167-171`); https-only until then.

---

## 9. Testing plan

Existing harness: `webapp/tests/` (`test_views.py`, `test_tasks.py`, `test_models.py`), pytest-django.

1. **Unit — envelope**: field set/order per `async_api.yaml:504-553`; `additionalProperties: false` enforced by the serializer; ULID monotonic under 10k rapid calls (D14 guard); the spec's worked examples (`:941-1104`) become golden fixtures the serializer must reproduce byte-for-byte.
2. **Unit — projection**: 8 internal statuses → 4 partner states, table at `:75-82`.
3. **Unit — HMAC**: known-vector signature over `f"{t}.{body}"`; header format `t=…,v1=…`; fresh `t` per attempt; retry-schedule arithmetic (attempt N → expected `next_attempt_at`).
4. **Integration — outbox atomicity**: emit inside `atomic()` + forced rollback → no row, no Redis publish (`on_commit` never fires). Emit outside a transaction → row + immediate publish.
5. **Integration — reconciler**: terminal job with empty/partial outbox → synthetic terminal event emitted once, idempotent on re-run.
6. **Integration — SSE view**: fakeredis pub/sub; initial state frame; keepalive `: ping` under an artificial 25s quiet (freeze-time); close rules D6 including the `captcha_blocked` case the legacy set misses; cap enforcement → 503.
7. **Contract — callback receiver**: a pytest ASGI/httpx MockTransport partner that recomputes the HMAC, asserts `event_id` stability across simulated retries, and records duplicates for dedupe verification.
8. **E2E (playground)**: seed a workspace per `docs/testing_guide.md`, drive `field_confirmation` → cleanup → finalizer; assert the full event sequence `job.created → inprogress → (phase…) → sample_ready → artifact(sample) → scraper_ready → artifact(scraper_code) → artifact(output) → terminal` in `event_id` order.

---

## 10. Sequencing

| Step | Delivers | Depends on |
|---|---|---|
| 1 | `events/` package: ULID helper, envelope serializer, projection map, `EventOutbox` + migration, `emit()` | — |
| 2 | Hook points §2.2 + `atomic()`/`on_commit` wiring; sample persistence §2.3 | 1 (and Planner A's `JobCallback`) |
| 3 | `dispatch_pending_callbacks` + `deliver_callback` + HMAC + disable policy + beat entry (30s) | 2 |
| 4 | Reconciler safety net + outbox prune task | 3 |
| 5 | Partner SSE view (framing, keepalive, close rules, cap) | 2 (envelopes on the wire) |
| 6 | `ws-token` semantics (Redis) | — |
| 7 | Hardening: metrics/logging, load-test the cap, deploy book for Railway | 5 |
| 8 | Phase 1.5 event gateway (triggered, §6) | 5 |
| 9 | Phase 2 WSS on the gateway | 6, 8 |

Steps 3 and 5 are independent of each other; both block on 2. Step 6 is independent of 1–5.

---

## 11. Risks / open questions

**Spec errata to raise with Planner A (blocking-ish):**
1. `:443-444` "stored hashed at rest" is incompatible with signing every attempt (D9). Propose "encrypted at rest."
2. `:417` vs `:466-470` — 5 attempts vs 5 backoff values (D10). Needs one word.

**Risks:**
- **SSE starvation** (§6) — the dominant Phase 1 risk. Cap + callbacks-as-primary + documented best-effort status; Phase 1.5 escape hatch pre-designed.
- **`scraper_ready` → `failed` inversion** (D5): rare (finalizer fails a job after successful cleanup). Legal per the state model but must be called out in partner docs or it will be read as a bug.
- **Sample persistence prerequisite** (§2.3): without it, `job.artifact.available(kind=sample)` cannot inline `records`. Shared dependency with Planner A's endpoint 3 — sequence it first.
- **Outbox growth**: `job.phase.updated` + `job.log.appended` are high-frequency. D12 (partner jobs only) + phase events carrying no dedupe key + the 30d prune bound it. Watch row counts in week 1.
- **Beat SPOF**: single beat container; `restart: unless-stopped` (compose `:147-149`) mitigates. Missed sweeps only delay callbacks; the outbox never loses events.
- **Deploy-time stream death**: `graceful-timeout 60` kills all SSE mid-job on every deploy. Inherent to Phase 1; the reconnect+reconcile contract is the mitigation, and the Phase 1.5 gateway inherits it.
- **No replay anywhere in Phase 1/2.0** (`:347-351, 156-158`): a partner disconnected during a terminal transition must reconcile via sync API. The `subscribe.ack` snapshot (Phase 2) and Redis Streams buffer (Phase 2.5) progressively close this.

**Open questions for Planner A:**
- Exact `JobCallback` write path on `POST /api/v1/jobs` / `PATCH` (who owns validation of the callback URL and secret encoding).
- Where the `/api/v1/` URL prefix mounts (`config/urls.py:9` includes `scraper.urls` at `""` — a new `api/v1/` include is the natural spot).
- Whether the API-key model exposes a `user_id` the gateway can read directly from Postgres (Phase 2 auth, §8).
