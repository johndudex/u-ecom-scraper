# Spec ↔ Implementation Conformance Audit

Branch `file-master-artifacts`. Surfaces audited: `docs/specs/sync_api.yaml` (OpenAPI 3.1) +
`docs/specs/async_api.yaml` (AsyncAPI 3.0) against `webapp/scraper/api/*`, `webapp/scraper/events/*`,
`webapp/agents/graph.py`, `webapp/scraper/tasks.py`, `event_gateway/*`. No test suites were run.
All line references are to the current working tree.

---

## VERDICT: **DRIFT** — 89 findings recorded (25 PARTNER-VISIBLE, 64 DOC-ONLY)

Six entries are the same defect seen from both specs (A4-1/B7-3/C-1, A4-2/B7-2,
A9-4/B1/B6-9, A13-3/B5-6) — 83 unique defects.

The two specs were written pre-implementation and amended during plan-folds; the TDD build then
shipped a *narrower* surface than either spec promises. The drift is not random — it clusters:

1. **Response bodies are thinner than spec'd.** Six of eight documented response schemas are
   emitted with different field names or with documented fields simply absent (`JobCreated`,
   `SampleResponse`, `OutputPage`, `ScraperCode`, `StateChangeData`, heartbeat).
2. **Three create-time request fields are read but never landed** (`search_keywords`,
   `listing_urls`, `item_urls`) — the spec documents server-side work the code does not do.
3. **Status-code drift is systematic**: spec says 409 `not_ready`, code returns 404, on three
   different artifact endpoints; spec says 400/422 in specific places, code picks the other.
4. **The async spec's honesty flags are stale in both directions** — `channels.jobs` still says
   `planned` while the gateway is built and both its operations say `live`; `deliverCallback`
   says `planned` while the outbox/dispatcher/beat are built.
5. The **event envelope itself is conformant** (exact 6 keys, ULID, ISO timestamps) and the
   **4-state projection tables match** in all three places (state.py, gateway.py, sse.py) —
   but the REST `state` field never reaches `sample_ready`, so REST and events disagree at
   runtime on the same job.

---

## A. SYNC SPEC (`sync_api.yaml`) vs `webapp/scraper/api/`

### A1. `POST /api/v1/check-site` (spec 151–201)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A1-1 | SPEC-BACKEND **PARTNER-VISIBLE** | Spec documents `content_type` in the 200 body (sync 196–199); code emits `site_type` instead and never emits `content_type` — `readers.py:53-62`. | code fix (rename) or spec fix |
| A1-2 | SPEC-BACKEND | Spec says the disclosure is "boolean + platform string only; never fields or outputs" (sync 157–160, 164–165); code also returns `site_type`, `site_name`, `scraping_method`, `last_scraped_at` — `readers.py:53-62`. Wider than the documented bounded disclosure (no field/output leak, but more than promised). | spec text widen or code trim |
| A1-3 | DOC-ONLY | `Site.objects.filter(url__icontains=host)` (`readers.py:45`) is a substring match — a host that appears as a substring of another site's URL reports `known_site: true`. Spec implies host-level matching. | code fix (low priority) |

### A2. `POST /api/v1/jobs` (spec 202–385)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A2-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **`search_keywords` is never read.** Spec maps `search_keywords` → stored as `search_criteria` (sync 227, 1611–1614). Code reads only `body.get("search_criteria")` (`writers.py:44`) and rejects a spec-conformant search_term create with 422 "search_criteria is required" (`writers.py:58-59`). | code fix |
| A2-2 | SPEC-BACKEND **PARTNER-VISIBLE** | **`listing_urls` is validated then dropped.** Spec: "this API accepts an array and performs that join server-side" into `search_criteria` (sync 226). Code checks non-empty (`writers.py:56-57`) and never joins — a list_page job is created with empty `search_criteria`, so the pipeline receives no listing URLs at all. | code fix |
| A2-3 | SPEC-BACKEND **PARTNER-VISIBLE** | **`item_urls` is validated then dropped.** Spec: "Deduplicated server-side; persisted to `scrapers/{slug}/input_urls.json`" (sync 1600, 225). Code only checks count/length (`writers.py:60-61`) — no dedupe, no `artifacts.write_json`, no `Site.input_urls`. `_build_initial_state` falls back to exactly that file (`tasks.py:526-542`), which nobody wrote → url_list jobs run with 0 URLs. Compare `intake_create_job`, which does write it (`views.py:2588-2605`). | code fix |
| A2-4 | SPEC-BACKEND **PARTNER-VISIBLE** | **202 body is `{job_id, status_url}` only** (`writers.py:145-147`). Spec `JobCreated` requires `state` + `created_at` and documents `sample_url`, `output_url`, `output_download_url`, `scraper_code_url` (sync 1680–1702) plus a `Location` header (sync 302–308). None emitted. | code fix |
| A2-5 | SPEC-BACKEND | Spec assigns missing/invalid `url`, non-http(s) URL, and non-absolute `item_urls` to **400** (sync 322–332). Code raises **422** `validation_failed` for missing url / bad input_mode (`writers.py:48-53`) and performs no scheme validation on `url` or `item_urls` at all. (Malformed JSON does get 400, `writers.py:29`.) | code fix or spec fix — pick one |
| A2-6 | SPEC-ONLY | Documented request validation not enforced anywhere: `url` maxLength 1000, `listing_urls` maxItems 50, `search_keywords` maxLength 500, `target_fields` maxItems 100, `scope` enum, `content_type` enum (sync 1578–1651). `notes`/`title` are silently truncated (`writers.py:113-114`) rather than rejected. | spec text fix (state actual behavior) |
| A2-7 | CODE-ONLY | `INPUT_MODES` accepts `navigation` (`writers.py:32`); sync spec's `InputMode` deliberately excludes it — "not offered to partners in v1" (sync 1514–1523). | code fix (drop) or spec widen |
| A2-8 | DOC-ONLY | Spec's 422 `schema_invalid` example carries `issues[].severity`/`path` (sync 359–383); code emits only `{code, message}` per issue (`writers.py:72`). The dataclass has both (`src/schema_validation.py:61-66`) — one-line fix. | code fix |
| A2-9 | DOC-ONLY | `callback_secret` (32–256) is only validated when `callback_url` is also present (`writers.py:77-88`); sent alone it is silently ignored. Spec documents it as an independent field. | spec text note |
| A2-10 | OK | 409 `duplicate_running_job` + `details.existing_job_id` ✓ (`writers.py:90-99`). `skip_approvals=True`, `full_extraction=False` ✓ (`writers.py:116-117`). `title` at create ✓. CSRF-exempt + X-API-Key ✓. | — |

### A3. `GET /api/v1/jobs` (spec 387–481)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A3-1 | SPEC-ONLY **PARTNER-VISIBLE** | **`state` and `input_mode` query filters are documented (sync 401–409) and not implemented** — `readers.py:172-178` reads only `page`, `page_size`, `created_since`. A client sending `?state=failed` gets an unfiltered 200 (silently wrong, worse than an error). | code fix |
| A3-2 | CODE-ONLY | List rows are the full `JobStatus` payload minus `phases` (`readers.py:192-196`), i.e. they add `internal_status`, `platform`, `scraping_method`, `current_phase`, `sample_available`, `output_available`, `scraper_available`, `output_filename`, `callback`, `started_at` beyond the documented `JobSummary` (sync 1890–1918). Additive — harmless but undocumented. | spec text widen |
| A3-3 | DOC-ONLY | `page < 1` is clamped to 1 (`readers.py:172`) rather than 422 as the spec's parameter contract states (sync 467–472); `page_size` out of range raises `invalid_page_size` for both params (`readers.py:177`). | spec text fix |
| A3-4 | OK | Envelope `jobs/page/page_size/total_items/total_pages` ✓; newest-first ✓; `created_since` unparseable → 422 `invalid_created_since` ✓ (`readers.py:178-185`); no superuser see-all bypass ✓. | — |

### A4. `GET /api/v1/jobs/{job_id}` (spec 486–675)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A4-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **`state` never becomes `sample_ready`.** Spec's authoritative table: `running` + sample resolvable → `sample_ready` (sync 504). Code computes `state = partner_state(job.status)` (`readers.py:95`), and `_STATE_MAP` maps `running` → `inprogress` unconditionally (`state.py:21-30`). The sample signal only drives the separate `sample_available` flag (`readers.py:97`). A client branching on `state == "sample_ready"` — the documented polling loop (sync 88–92) — never sees it. **The event stream DOES emit `job.sample_ready` (`sample_persist.py:81`), so REST and events disagree on the same job.** | code fix (derive, don't just flag) |
| A4-2 | SPEC-BACKEND **PARTNER-VISIBLE** | `failure.code` for internal `failed` is **`pipeline_failed`** (`state.py:38-45`) — not in the spec's `FailureCode` enum (sync 1540–1551). Conversely none of the five documented stage codes (`validation_failed`, `code_generation_failed`, `execution_failed`, `no_output_produced`, `internal_error`) is ever emitted; every non-blocked failure collapses to `pipeline_failed`. | spec text fix (replace the 5 stage codes) or code fix |
| A4-3 | SPEC-BACKEND | `sample_available` is hard-gated to live jobs (`state.py:48-56`, the m4 fix) → always `false` on terminal jobs. Spec's `failed` example shows `sample_available: true` (sync 661) and the info-block guarantee "failed does NOT imply no data … GET …/sample still return whatever exists" (sync 104–105). The code comment explains why (finalize stamps `completed_at` on never-run steps) — the spec text has not caught up. | spec text fix |
| A4-4 | SPEC-BACKEND | `JobStatus` documented fields never emitted: `duration_seconds` (sync 1882), `output_download_url` (1885), `scraper_code_url` (1887). Model has a `duration_seconds` property (`models.py:200`) that the builder ignores. | code fix |
| A4-5 | SPEC-BACKEND | `PhaseStatus` enum is `[pending, running, done, failed]` (sync 1705–1708), but `_notify_phase` is also called with `"skipped"` (`graph.py:2560`, `3676`) → Step rows with `status="skipped"` are returned verbatim in `phases[]` (`readers.py:98-106`). Schema-invalid value. | spec text fix (add `skipped`) or code fix |
| A4-6 | DOC-ONLY | `current_phase` spec: "Phase currently running (or the failed one)" (sync 1832–1834); code only matches `status == "running"` (`readers.py:126`) — never the failed phase. | spec text fix |
| A4-7 | DOC-ONLY | `output_filename` returns the full File Master key (`job.output_file`, e.g. `scrapers/<slug>/output_….json`, `readers.py:132`); spec shows the basename (sync 1852–1854). | spec text fix |
| A4-8 | DOC-ONLY | Spec's `Phase` enum has 17 values (sync 1713–1732); `_seed_pipeline_steps` seeds 14 (`tasks.py:193-208`) — `navigation_analysis` and `content_analysis` are in the enum and in `state.PHASE_ENUM` (`state.py:62-68`) but never seeded, so "every phase present in the live response" (sync 1839) holds only for 14. | spec text fix |
| A4-9 | DOC-ONLY | `normalize_phase()` (`state.py:71-75`) exists to map legacy display strings onto the enum but is never called — `_job_status_payload` passes raw Step values, so pre-migration rows ("Browser Navigation") are emitted schema-invalid. | code fix (call it) |
| A4-10 | DOC-ONLY | `failure.message` can be `null` (`readers.py:113`) but the schema requires a string (sync 1743–1751). `JobStatus.callback` adds `pending_count` beyond the documented 3 properties (sync 1857–1872) — additive. | spec text fix |
| A4-11 | OK | All 8 internal statuses map per the table (`state.py:21-30`); monotonic `Step(testing).completed_at IS NOT NULL` derivation ✓ (`readers.py:92-94`); cross-tenant 404 ✓ (`views.py:55-68`); `internal_status` exposed read-only ✓. | — |

### A5. `GET /api/v1/jobs/{job_id}/sample` (spec 680–807)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A5-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **Response shape is entirely different.** Spec `SampleResponse`: `{job_id, state, source, output_key, item_count, items, field_coverage, generated_at}` (sync 2010–2052). Code returns `{job_id, records, record_count}` (`writers.py:297-301`). `items`→`records`, `item_count`→`record_count`, and five documented fields absent (incl. `field_coverage`, which the spec sources from the test report). | code fix |
| A5-2 | SPEC-BACKEND **PARTNER-VISIBLE** | Spec documents **409 `not_ready`** with `details.{state, current_phase, retry_after_seconds}` (sync 793–806); code raises **404** `not_ready` with no details (`writers.py:290, 293, 296`). Same drift on the other artifact endpoints (A6-4, A8-3). | code fix or spec fix — pick one, consistently |
| A5-3 | DOC-ONLY | Spec says the sample is "OVERWRITTEN on later retry cycles — contents may change between fetches" (sync 698-700); code is first-write-wins via `dedupe_key=f"sample:{job.id}"` (`sample_persist.py:81-85`) — frozen at first PASS. Code behavior is the better one; spec text is stale. | spec text fix |
| A5-4 | DOC-ONLY | Spec's provenance note still says the sample is written at `field_confirmation.py:288-313` (sync 709-716); the shipped hook is `_invoke_code_tester` after `_preserve_test_report` (`graph.py:3473-3475`, docstring in `sample_persist.py:1-9`) — the fold's B1 relocation. Spec never amended. | spec text fix |
| A5-5 | OK | Never paginated, ≤5 records ✓ (`MAX_SAMPLE_RECORDS = 5`, `sample_persist.py:20`); File Master key `scrapers/{slug}/samples/sample-{job_id}.json` exactly as spec'd ✓ (`sample_persist.py:75`). | — |

### A6. `GET /api/v1/jobs/{job_id}/output` (spec 812–951)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A6-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **`OutputPage` shape mismatch.** Spec requires `{job_id, state, output_key, output_filename, total_items, page, page_size, total_pages, items}` (sync 2054–2084). Code returns `{site, <content-type key>: items, metadata, page, page_size, total_items, total_pages}` (`output_index.py:159-162`) — no `job_id`, no `state`, no `output_filename`, no `output_key`, and the records live under the content-type key (`products`/`jobs`/…) rather than a flat `items`. | code fix |
| A6-2 | SPEC-ONLY | `X-Total-Count` response header documented (sync 853–857); never set (`writers.py:321-334`). | code fix |
| A6-3 | DOC-ONLY | 0-item output: index is built but `read_output_page` 404s on an empty items list (`output_index.py:138-139`), so a `scraper_ready` job with `item_count: 0` (explicitly allowed, sync 100-102) can never page its output. | code fix (edge) |
| A6-4 | SPEC-BACKEND | Spec documents **409 `not_ready`** while running (sync 925–937); code returns 404 `output_not_found` (same code as "never produced"), so a client cannot distinguish "still running" from "failed without output" as the spec's 404/409 split promises (sync 910–924). | code fix |
| A6-5 | OK | Bounds match exactly: `page_size` default 100 / max 500 / min 1 (`output_index.py:132-133`, default `writers.py:328`); `page > total_pages` → 422 with `details.total_pages` ✓ (`output_index.py:142-145`). Normative perf requirement satisfied — byte-offset index at finalize + window cache, no per-request full parse (`output_index.py`, `window_cache.py`). `site`+`metadata` on every page ✓. | — |
| A6-6 | DOC-ONLY | Spec documents resolution precedence "job.output_file first, else newest output_*.json in the job's run window" (sync 835–840); code uses `job.output_file` + index only, no fallback (`output_index.py:134-139`). Simpler and safe; spec overstates. | spec text fix |

### A7. `GET /api/v1/jobs/{job_id}/output/download` (spec 953–994)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A7-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **Normative streaming requirement violated.** Spec (sync 967–971): the internal view's whole-file buffering "is unacceptable at 97 MB. This endpoint MUST stream via `artifacts.stream_url()`". Code buffers the entire file into memory — `_fm_read_bytes` → `artifacts.read()` (`writers.py:312-318, 342-347`). `stream_url` exists (`src/artifacts.py:101`) and is called nowhere in the codebase. | code fix |
| A7-2 | SPEC-BACKEND | Missing output → 404 `output_not_found` (`writers.py:340-345`); spec refs the `NotReady` 409 response for the running case (sync 991–992). Same 404-vs-409 drift. | code fix (with A6-4) |
| A7-3 | OK | `Content-Disposition: attachment; filename="…"` ✓ (`writers.py:346-348`); FM-miss fail-fast rather than hang ✓. | — |

### A8. `GET /api/v1/jobs/{job_id}/scraper-code` (spec 999–1076)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A8-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **`ScraperCode` shape mismatch.** Spec requires `{job_id, filename, language, code}` + `state`, `strategy`, `source` (sync 2086–2111). Code returns `{code, filename, size_bytes, url}` (`writers.py:373-378`) — `job_id`, `language`, `strategy`, `source` absent; `size_bytes`/`url` undocumented. | code fix |
| A8-2 | SPEC-BACKEND **PARTNER-VISIBLE** | **No production fallback.** Spec resolution order: per-job `job.scraper_file` first, *then* the site's production `scrapers/{slug}/scraper.py`, with `source: production` marking the second leg (sync 1014–1023, 2105–2108). Code serves only `job.scraper_file` and 404s otherwise (`writers.py:362-367`) — `source` can never be `production`. | code fix |
| A8-3 | SPEC-BACKEND | Spec documents **409 `not_ready`** (sync 1062–1074); code returns 404 `not_found` (`writers.py:363, 367`). | code fix |
| A8-4 | OK | `?format=raw` → `text/x-python` + attachment header ✓ (`writers.py:369-372`); JSON default ✓. | — |

### A9. `GET/PATCH /api/v1/jobs/{job_id}/callback` (spec 1081–1168)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A9-1 | SPEC-BACKEND | Absent registration: spec's 200 schema is `CallbackStatus` directly (sync 1095–1100); code returns `{"callback": null}` — one level of wrapping (`writers.py:209`). The prose "Absent registration → `callback: null`" (sync 1093) arguably matches the prose, not the schema. | spec text fix (make it explicit) |
| A9-2 | SPEC-BACKEND **PARTNER-VISIBLE** | **`{"action":"reenable"}` does not "reset the delivery attempt counter"** (sync 1114–1118). Code flips `status` and clears `disabled_reason` only (`writers.py:236-241`). Worse: exhausted rows are `STATE_PERMANENTLY_FAILED` with `next_attempt_at=None` (`dispatcher.py:117-125`) and the dispatcher only claims `PENDING`/`LEASED` (`dispatcher.py:54`) — so events that exhausted are **never redelivered after re-enable**, contradicting "the next dispatcher sweep resumes delivery of PENDING outbox rows… PENDING events that queued while disabled are delivered after re-arm" (async 452–455). Only still-pending rows resume. | code fix or spec text fix |
| A9-3 | CODE-ONLY | Re-enable cooldown returns **429 `rate_limited`** (`writers.py:233-235`); the PATCH operation documents no 429 response (sync 1136–1168). | spec text fix |
| A9-4 | CODE-ONLY **PARTNER-VISIBLE** | Re-enable emits an outbox event of type **`callback.reenabled`** (`writers.py:240`) — a type absent from the async spec's `EventEnvelope.type` enum (async 570–579, 9 values). It is delivered as a signed callback POST and fanned out to SSE/WSS subscribers, so partners receive an envelope whose `type` is not in the contract. | spec text fix (add it) or code fix |
| A9-5 | OK | `CallbackStatus` field set matches spec exactly — `status, url, disabled_reason, last_failure, delivered_count, pending_count, created_at, last_delivered_at` (`writers.py:188-201`). 409 `callback_already_active` ✓, 422 `invalid_callback_url` ✓, rotate re-SSRF-validates and re-arms ✓ (`writers.py:244-264`). Secret never serialized; admin `exclude = ("secret",)` ✓ (`admin.py:257`). | — |
| A9-6 | DOC-ONLY | Spec: "The action is audit-logged under the partner's key" (sync 1121–1122); the only record is the outbox row. | spec text fix |

### A10. `POST /api/v1/jobs/{job_id}/cancel` (spec 1170–1217)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A10-1 | SPEC-BACKEND **PARTNER-VISIBLE** | 200 body is `{job_id, state, failure:{code, message:null}}` (`writers.py:164-165, 182-183`), not the documented `JobStatus` (sync 1186–1190). Also `message: null` violates `Failure.message: string`. | code fix |
| A10-2 | SPEC-BACKEND | 409 `details.state` is `"completed"` for a completed job (`writers.py:168`) — **not a valid `JobState` value**; the spec `$ref`s `JobState` (sync 1209–1210), so it must be `scraper_ready`. | code fix |
| A10-3 | DOC-ONLY | Spec is self-contradictory: 200 says "or was already terminal — idempotent" (sync 1186) while 409 says "already terminal… not cancellable" (sync 1198). Code picks 200 for already-cancelled and 409 for other terminal. | spec text fix |
| A10-4 | OK | Sets `cancelled` + `completed_at`, emits `job.failed` `{reason: cancelled}` deduped `failed` (`writers.py:170-174`) — collides benignly with the reconciler's key (first write wins, both payloads agree). Celery revoke best-effort ✓. Approval supersede happens via the `post_save` hook as spec'd ✓. | — |

### A11. `POST /api/v1/ws-token` (spec 1219–1260)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A11-1 | SPEC-BACKEND **PARTNER-VISIBLE** | `connect_url` is **`null`** unless the client sends an undocumented `job_id` body field (`sse.py:266-273`). The spec has **no requestBody at all** and marks `connect_url` required with "Ready-to-use stream URL with the token appended" (sync 1239–1252). A spec-conformant call (empty body) gets `connect_url: null`. | code fix |
| A11-2 | SPEC-ONLY | Spec: "Rate-limited with the global per-key limits (429 with Retry-After)" (sync 1230-1231) and `x-rate-limits.ws_token.per_minute_per_key: 10` (sync 128). `ws_token` bypasses `api_view` entirely (`urls.py:37` → bare `@csrf_exempt` view) — **no rate limit of any kind is applied**, and the 10/min limit exists nowhere in the codebase. | code fix |
| A11-3 | OK | TTL 300 matches `TOKEN_TTL = 300` (`sse.py:43`); single-use atomic consume via `GETDEL` (`sse.py:70-79`); 201 ✓; 401 on bad key ✓. | — |

### A12. `POST /api/v1/validate-schema` (spec 1262–1334)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A12-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **Request field name and type both wrong.** Spec requestBody: `schema_text`, a *string* (sync 1283–1300). Code reads `body.get("schema")` and requires a dict/list, 422-ing on anything else (`readers.py:68-72`). A spec-conformant `{"schema_text": "…"}` call fails. | code fix (accept both) |
| A12-2 | SPEC-BACKEND | Missing/invalid body → 422 `schema_invalid` (`readers.py:70-72`); spec documents **400** (sync 1325–1327). | code fix or spec fix |
| A12-3 | OK | 200 `SchemaValidationResult` `{valid, issues[{code,message,severity,path}], derived_fields, detected_content_type}` matches exactly (`readers.py:76-86` ↔ sync 2129–2148); 200-with-`valid:false` convention preserved ✓. | — |

### A13. `x-rate-limits` (spec 124–130) vs `ratelimit.py`

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A13-1 | OK | `requests: {sustained_per_second: 10, burst: 30}` ✓ — `RATE_RPS = 10`, `RATE_BURST = 30` (`ratelimit.py:12-13`), enforced per key on every `api_view` endpoint (`views.py:34-40`), 429 + `Retry-After` ✓, per-key identity (key hash, not IP) ✓, Redis-down fail-open ✓. | — |
| A13-2 | SPEC-ONLY | `creates: {per_hour_per_key: 60}` — **not enforced anywhere** (no per-hour counter in `writers.py` or `views.py`). | code fix or delete from spec |
| A13-3 | SPEC-BACKEND **PARTNER-VISIBLE** | `event_streams: {concurrent_per_key: 1}` — the budget is **global across all partners and shared with internal streams**, not per-key: a single Redis counter `sse:open:global` with `PARTNER_STREAM_BUDGET` default **1** (`sse.py:39, 82-91`). Two different partners can never stream concurrently. Over-budget returns **503** with code `rate_limited` (`sse.py:139-144`); spec documents **429** + `Retry-After` (sync 129, 1403–1410). | spec text fix (document the global budget honestly) or code fix |
| A13-4 | SPEC-ONLY | `ws_token: {per_minute_per_key: 10}` — not enforced (see A11-2). | code fix or delete from spec |

### A14. Security notes (spec 1339–1361)

| # | Tag | Finding | Move |
|---|-----|---------|------|
| A14-1 | OK | **Superuser-key rejection at auth time** ✓ — `auth.py:33-34` (`403 forbidden`), independently re-implemented in the gateway (`gateway.py:64-86`) and on the SSE token path (`sse.py:128-130`). The CODE-LEVEL MANDATE is honored. | — |
| A14-2 | OK | **404-not-403 tenancy** ✓ — `_api_get_job` (`views.py:55-68`), SSE (`sse.py:132-136`), gateway `_job_row` (`gateway.py:128-140`). | — |
| A14-3 | see A1-2 | known_site bounded disclosure is *wider* than spec'd (no field/output leak). | spec text fix |

---

## B. ASYNC SPEC (`async_api.yaml`) vs events/graph/tasks/gateway

### B1. Message catalog — emit sites vs spec'd payloads

| Event | Emit site | Verdict |
|---|---|---|
| `job.created` | `writers.py:125-135` | Payload `{state, url, content_type, input_mode, callback}`. Required set `{state,url,content_type,input_mode}` ✓ satisfied; documented `previous_state` and `search_criteria` absent (async 598–624). `callback` is `null` when unregistered — spec types it as `CallbackStatus` (an object) with no null variant (async 615–616). **DOC-ONLY.** |
| `job.inprogress` | `tasks.py:217-227` | Data is `{internal_status: <status>}`. `StateChangeData` requires `{state, previous_state}` (async 632) — **neither present**, and `internal_status` is not a documented field (async 626–651). **PARTNER-VISIBLE.** |
| `job.sample_ready` | `sample_persist.py:81-85` | Data `{item_count, sample_url}`. Missing required `state`, `previous_state`. Also `item_count` = `len(sample)` (≤5), while the spec defines it as "internal product_count" (async 644–648) — a partner sees 5 in the event and a different number on the REST job. **PARTNER-VISIBLE.** |
| `job.scraper_ready` | `graph.py:1054-1055` | Data is **`{}`** — empty. Missing required `state`/`previous_state` and every documented convenience field (`item_count`, `output_url`, `scraper_code_url`, async 1046–1068 example). **PARTNER-VISIBLE.** |
| `job.failed` | `writers.py:174` (cancel), `reconciler.py:53` | Data `{reason}`. Missing required `state`/`previous_state`. `reason` carries a code token (`pipeline_failed`/`cancelled`/…) on the reconciler path, but the spec defines it as "the job's error_message" human-readable text (async 640–643). **PARTNER-VISIBLE.** |
| `job.phase.updated` | `graph.py:908` | Data `{phase, phase_status}` matches `PhaseUpdatedData` (async 710–723) ✓. But `phase_status` can be **`"skipped"`** (`graph.py:2560, 3676`) — not in the documented `[running, done, failed]` enum. **PARTNER-VISIBLE.** |
| `job.artifact.available` | ×3: `sample_persist.py:86-90` (sample), `graph.py:1056-1060` (scraper_code), `output_index.py:228-236` (output) | **Shape mismatch on all three.** Spec requires a nested `data.artifact` descriptor `{kind, url, size_bytes, sha256, item_count}` with `records` inlined only for samples (async 653–708). Code emits **flat** `{kind, url, …}` at the top level of `data`. `size_bytes` and `sha256` are never emitted for any artifact. The scraper_code emit adds `key: promoted` — an undocumented field that leaks the internal File Master key (`graph.py:1058`). The sample emit omits `item_count`. **PARTNER-VISIBLE.** |
| `job.artifact.available` / `dagster_code` | none | `ArtifactDescriptor.kind` includes `dagster_code` (async 666); no dagster artifact event is ever emitted (the dagster converter exists but has no emit hook). **SPEC-ONLY.** |
| `job.approval.required` | **none** | No emit site anywhere in `webapp/`. The spec describes it as the supplementary event for exactly the budget-escalation case the sync spec admits is reachable for API jobs (sync 505; async 725–743). Status does flip to `waiting_approval` (`tasks.py:273-275`) — silently, with no event. **SPEC-ONLY, PARTNER-VISIBLE.** |
| `job.log.appended` | **none** | No emit site. The `subscribe.events` filter documents opting into it (async 795–811); nothing can ever produce it. **SPEC-ONLY.** |
| `callback.reenabled` | `writers.py:240` | **Emitted type outside the `EventEnvelope.type` enum** (async 570–579). Delivered as a signed callback and fanned out to SSE/WSS. **CODE-ONLY, PARTNER-VISIBLE** (see A9-4). |

**Artifact ordering** — spec's corrected ordering sample → scraper_code → output (async 686–691) matches the code: sample at code_tester PASS, scraper_code in-graph at cleanup promotion, output in the finalizer after the schema prune. ✓

### B2. `EventEnvelope` (async 547–595) vs `emitter.py`

| # | Tag | Finding | Move |
|---|-----|---------|------|
| B2-1 | OK | **Exact key set match** — `emit` builds `{event_id, type, occurred_at, job_id, user_id, data}` and nothing else (`emitter.py:82-89`), satisfying `additionalProperties: false`. The SSE-side envelope builder matches too (`sse.py:146-155`). | — |
| B2-2 | OK | `event_id` is a real 26-char Crockford base32 ULID — 48-bit ms timestamp + 80-bit randomness, process-local monotonicity guard (`emitter.py:22-45`). `occurred_at` is `isoformat()` UTC ✓. `user_id` present ✓. | — |
| B2-3 | DOC-ONLY | The `type` enum is violated in practice by `callback.reenabled` (B1) — the enum, not the envelope shape, is what breaks. | spec text fix |

### B3. `x-retry` (async 498–508) vs `dispatcher.py`

| # | Tag | Finding | Move |
|---|-----|---------|------|
| B3-1 | OK | `attempts: 6` ✓ (`MAX_ATTEMPTS = 6`, `dispatcher.py:31`); `backoff: [10s, 1m, 10m, 1h, 6h]` ✓ (`BACKOFFS = [10, 60, 600, 3600, 21600]`, `dispatcher.py:29`); `timeout: {connect: 10s, read: 10s}` ✓ (`httpx.Timeout(10.0, connect=10.0)`, `dispatcher.py:243`); `on_exhaustion: disable_callback` ✓ (`mark_attempt_failed` sets `STATUS_DISABLED` + `disabled_reason`, `dispatcher.py:117-129`). | — |
| B3-2 | SPEC-BACKEND | **"legs >= 1 m are self-scheduled … and are honored exactly" is not what happens.** `deliver_callback` filters `state=STATE_LEASED` and returns immediately otherwise (`dispatcher.py:176-182`). After a failed attempt `mark_attempt_failed` sets the row back to `PENDING` (`dispatcher.py:132`), so the self-scheduled task firing at `countdown=gap` (`dispatcher.py:189-194`) finds a PENDING row and **no-ops** — only the 30 s sweep can lease it. The exact-countdown mechanism exists but is inert; every leg is sweep-quantized. | code fix (self-schedule should claim, not require a lease) or spec text fix |
| B3-3 | DOC-ONLY | Exhaustion message is "delivery exhausted after 6 attempts over the retry ladder" (`dispatcher.py:122-124`); spec's `CallbackStatus.disabled_reason` example is "…over 7h 1m" (sync 1765). Cosmetic. | — |
| B3-4 | OK | The `< 1m` quantization caveat, the dead-worker lease reclaim that burns an attempt (`dispatcher.py:73-88`), and the `(job, event_type, dedupe_key)` idempotency constraint (`models.py:548-555`) all behave as documented. | — |

### B4. Signature header (async 460–476) vs `_deliver`

| # | Tag | Finding | Move |
|---|-----|---------|------|
| B4-1 | OK | **Exact match.** `X-Scraper-Signature: t=<unix_ts>,v1=<hex hmac_sha256("<t>." + body, secret)>` — `dispatcher.py:229-235` signs `f"{ts}.".encode() + body.encode()` with SHA-256 and emits `f"t={ts},v1={sig}"`. `t` is send time, re-signed per retry ✓. `X-Scraper-Event-Id` ✓, `X-Scraper-Job-Id` ✓, `User-Agent` bonus. | — |
| B4-2 | OK | Delivery-time SSRF re-validation on **every** attempt with `follow_redirects=False` and immediate `permanently_failed` + callback disable on violation (`dispatcher.py:210-226`) — the B4 hard requirement. | — |
| B4-3 | SPEC-ONLY | "re-armed rows stay subject to the 30-day outbox prune" (async 455) — **no 30-day outbox prune exists**; delivered rows accumulate forever. | code fix or spec text fix |
| B4-4 | DOC-ONLY | Rotation's 30 s dual-secret acceptance window (async 456–458) is advice to verifiers only; code swaps atomically with no window state. Acceptable. | — |

### B5. SSE channel `jobEventsSse` (async 263–306, 397–420) vs `sse.py`

| # | Tag | Finding | Move |
|---|-----|---------|------|
| B5-1 | OK | Envelope-per-`data:`-frame ✓ — the stream subscribes to `job:{id}:envelope` and forwards each message verbatim as one `data:` frame (`sse.py:157-192`). Initial state frame ✓ (terminal envelope, or `job.inprogress`, `sse.py:166-174`). Close on terminal ✓ (`sse.py:196-219`, both the seen-terminal and the silent-channel DB check). | — |
| B5-2 | OK | Keepalive: spec's MUST is "at least every 30 s" (async 275–279); `KEEPALIVE_SECONDS = 25` with a 1 s poll plus an immediate `: ping` on open (`sse.py:42, 164, 204-206`). Comfortably inside. | — |
| B5-3 | SPEC-ONLY | Spec's SHOULD "set SSE `retry:` on connect" (async 279–280) is not implemented — no `retry:` field is emitted. Note the spec contradicts itself: the bridge notes assert "no id/retry fields emitted" (async 173–174). | spec text fix (resolve the contradiction) |
| B5-4 | OK | **401-before-stream** ✓ — every failure path returns a `JsonResponse` *before* `StreamingHttpResponse` is constructed (`sse.py:102-144`). Browser token auth via the same ws-token machinery, 300 s single-use `GETDEL` ✓ (`sse.py:117-130`). | — |
| B5-5 | DOC-ONLY | A valid token whose owner is superuser/inactive returns **403** (`sse.py:128-130`); the bridge notes say "missing/expired/consumed token or bad key returns 401" (async 168–170). | spec text fix |
| B5-6 | SPEC-BACKEND | Stream-budget exhaustion returns **503** `rate_limited` (`sse.py:139-144`); the spec documents only 429 for limits (async has no 503 anywhere). See A13-3. | spec text fix |
| B5-7 | CODE-ONLY | **Undocumented hard 3600 s stream deadline** (`STREAM_DEADLINE_SECONDS`, `sse.py:41, 182`) — a healthy long-lived stream is closed with no terminal frame and no protocol notice. Partners on 60-minute jobs will hit it. | code fix or document |
| B5-8 | DOC-ONLY | The initial/terminal frames' `data` is `{internal_status}` / `{reason: <status>}` (`sse.py:170, 174`) — same `StateChangeData` shortfall as B1, plus `reason` carrying an internal status token. | code fix (with B1) |

### B6. WSS channel `jobs` + operations vs `event_gateway/`

| # | Tag | Finding | Move |
|---|-----|---------|------|
| B6-1 | SPEC-ONLY **PARTNER-VISIBLE** | **`channels.jobs` `x-status: planned` (async 261) while the gateway is built and shipped** (compose service `event-gateway`, `Dockerfile`, tests) and while `servers.production-wss` says `x-status: live` (async 133) and **both** its operations say `x-status: live` (async 353, 387). Same stale flag on `channels.partnerCallback` (async 332) and the `deliverCallback` operation (async 496) despite the outbox/dispatcher/beat being live. The file header still says "No websocket server exists in this stack today" (async 14–15). A client reading the channel block concludes WSS is not built. | spec text fix — flip the flags |
| B6-2 | SPEC-BACKEND **PARTNER-VISIBLE** | **The `subscribe.events` filter is accepted and ignored.** Spec: filter, "Default is the four state events plus job.artifact.available" (async 795–811). `handle_control` reads only `data.job_id` (`gateway.py:203-214`) and the pump forwards **every** envelope on the channel to every subscriber with no type filter (`app.py:83-113`). A client subscribing with `events: [job.scraper_ready]` still receives `job.phase.updated`, `job.created`, etc. | code fix |
| B6-3 | SPEC-BACKEND | `heartbeat.ping` `data` is `{"ts": <unix float>}` (`app.py:126`); spec `HeartbeatData` documents `server_time` as an ISO date-time (async 866–876). Field name and format both wrong. | code fix |
| B6-4 | OK | Heartbeat cadence 25 s ✓ (`HEARTBEAT_SECONDS = 25`, `gateway.py:35`, checked every 5 s in `app.py:121-128`) matching `x-heartbeat` (async 388–395). Terminal events retire the subscription but keep the connection open ✓ (`app.py:108-113`, `_TERMINAL_OPS`). | — |
| B6-5 | OK | `subscribe.ack` snapshot matches `SubscribeAckData` exactly — `{job_id, state, snapshot:{last_event_id, item_count, sample_url, output_url, scraper_code_url}}` (`gateway.py:154-168` ↔ async 820–847). `subscribe.nack` `job_not_found` for missing **and** cross-tenant (no oracle) ✓ (`gateway.py:210-212`). `unsubscribe.ack` `{job_id}`, idempotent discard ✓. `error`/`ProtocolErrorData` `{error_code, message}` ✓. `heartbeat.pong` consumed silently ✓. `since_event_id` ignored — which the spec itself admits for 2.0 (async 786–790, 812–814) ✓. | — |
| B6-6 | DOC-ONLY | `NackData.error_code` enum includes `token_expired` (async 859); the gateway never emits it — a token failure closes the socket with code **4401** (`app.py:60-62`), a close code the spec does not document. Malformed subscribes get an `error` frame with `invalid_message`, not a nack. | spec text fix (document 4401) |
| B6-7 | SPEC-BACKEND | Non-browser WS auth: spec says non-browser clients "MAY authenticate the handshake with X-API-Key" via the `apiKeyHeader` scheme (`in: header`, async 521–528, 49–54); the gateway reads a **`?apikey=` query param** (`app.py:53`). Query-param key exposure is exactly what the spec's token design exists to avoid. No header/subprotocol path is implemented (the docstring at `app.py:50` claims one; the code reads the query param). | code fix or spec text fix |
| B6-8 | DOC-ONLY | `build_snapshot` emits `item_count: null` while `inprogress` (`gateway.py:163`); spec types `snapshot.item_count` as integer (async 836). | spec text fix |
| B6-9 | CODE-ONLY | The pump forwards **any** envelope type on the channel, so partners can receive the undocumented `callback.reenabled` (see A9-4). | spec text fix |

### B7. 4-state projection tables vs `state.py`

| # | Tag | Finding | Move |
|---|-----|---------|------|
| B7-1 | OK | **All three implementations agree**: `state.py:21-30`, `gateway.py:114-125`, `sse.py:45-59` map the same 8 internal statuses to the same 4 partner states. `pending/running/waiting_approval → inprogress`, `completed → scraper_ready`, `failed/cancelled/captcha_blocked/akamai_blocked → failed` ✓ matches both specs' tables (sync 500–510, async 75–82). | — |
| B7-2 | SPEC-BACKEND | `failure.code` values: `cancelled`/`captcha_blocked`/`akamai_blocked` ✓ match by name; internal `failed` maps to **`pipeline_failed`**, which is in neither spec's vocabulary (sync `FailureCode` 1540–1551; async has no failure-code concept at all). See A4-2. | spec text fix |
| B7-3 | SPEC-BACKEND | The sync table's `running` + sample-resolvable → `sample_ready` row is honored by the **event** side (`sample_persist.py:81`) but not by REST `state` (A4-1). The async spec's table derives `sample_ready` from the testing-phase event (async 79–80, 90–93) — that part is correct. | code fix (A4-1) |

---

## C. CROSS-SPEC CONSISTENCY

| # | Tag | Finding | Move |
|---|-----|---------|------|
| C-1 | SPEC-BACKEND **PARTNER-VISIBLE** | **REST and events disagree at runtime on `sample_ready`.** For the same running job the event stream emits `job.sample_ready` while `GET /jobs/{id}` reports `state: "inprogress"` with only `sample_available: true` (A4-1 vs B1). Both specs present the 4-state model as one shared vocabulary ("do not fork", async 66). | code fix |
| C-2 | OK | **`CallbackStatus` shapes are identical across specs** — same 8 fields (sync 1753–1780 ↔ async 766–781), and the code emits exactly that set (`writers.py:188-201`). | — |
| C-3 | OK | **Artifact ordering** sample → scraper_code → output is stated once (async 686–691) and matches the emit sites (B1). The sync spec does not contradict it. | — |
| C-4 | OK | **`scraper_ready` superseded-by-`failed` caveat is present in both** — sync `JobState` description (1498–1505) and async info.description (96–100) — and is real: the reconciler can emit `job.failed` after `job.scraper_ready` for the same job (dedupe keys `scraper_ready`/`failed` are distinct, `reconciler.py:33-36`). | — |
| C-5 | DOC-ONLY | `InputMode` vocabulary forks: the async spec's `JobCreatedData.input_mode` enum includes `navigation` (async 611–613) while the sync spec's `InputMode` deliberately excludes it (sync 1514–1523). Code accepts it (A2-7). | spec text fix (one side) |
| C-6 | DOC-ONLY | `item_count` semantics fork: sync defines it as `ScrapeJob.product_count` (sync 1849–1851); async's `sample_ready` event emits `len(sample)` (≤5) instead (B1). | code fix or spec text fix |
| C-7 | DOC-ONLY | `previous_state` is required by `StateChangeData` and **no emitter ever sets it** — there is no state-tracking anywhere in `emit()` (B1). Both specs' message examples show it populated. | code fix |
| C-8 | DOC-ONLY | `failure.code` exists only in the sync spec; the async `job.failed` event carries only a free-text `reason`. A partner consuming both cannot correlate failure classification. | spec text fix |

---

## Consolidated PARTNER-VISIBLE list (25)

These would break or mislead a client coded against the published specs:

1. **A2-1** `search_keywords` never read — spec-conformant search_term create returns 422.
2. **A2-2** `listing_urls` never joined into `search_criteria` — list_page jobs run with no listing URLs.
3. **A2-3** `item_urls` never persisted/deduped — url_list jobs run with 0 URLs.
4. **A2-4** Create 202 body missing `state`, `created_at`, and all four artifact URLs + `Location`.
5. **A3-1** `state` / `input_mode` list filters documented, not implemented (silent no-op filter).
6. **A4-1** REST `state` never reports `sample_ready` (event stream does) — REST/events disagree.
7. **A4-2** `failure.code: "pipeline_failed"` is outside the documented `FailureCode` enum; the five documented stage codes are never emitted.
8. **A5-1** `/sample` returns `{job_id, records, record_count}`, not the documented `SampleResponse`.
9. **A5-2 / A6-4 / A7-2 / A8-3** Systematic 409-`not_ready`-vs-404 drift on sample, output, download, and scraper-code.
10. **A6-1** `/output` page lacks `job_id`, `state`, `output_key`, `output_filename`, and a flat `items` array.
11. **A7-1** `/output/download` buffers the whole file, violating the spec's NORMATIVE streaming requirement.
12. **A8-1** `/scraper-code` response lacks `job_id`, `language`, `strategy`, `source`.
13. **A8-2** No production-scraper fallback — `source: "production"` can never occur.
14. **A9-2** Re-enable does not reset the attempt counter; exhausted events are never redelivered.
15. **A9-4 / B1** `callback.reenabled` event type is outside the `EventEnvelope.type` enum and is pushed to partners.
16. **A10-1** Cancel 200 body is not `JobStatus`; `failure.message` can be `null`.
17. **A10-2** Cancel 409 `details.state: "completed"` is not a valid `JobState`.
18. **A11-1** `ws-token` returns `connect_url: null` unless an undocumented `job_id` body field is sent.
19. **A12-1** `/validate-schema` reads `schema` (object), not the documented `schema_text` (string).
20. **A13-3** Stream budget is global (default 1 total), not per-key, and returns 503 not 429.
21. **B1** All four state events omit the required `state`/`previous_state`; `job.scraper_ready` carries an empty `{}` payload.
22. **B1** `job.artifact.available` emits a flat `{kind, url, …}` instead of the documented nested `artifact` descriptor; `size_bytes`/`sha256` never emitted; scraper_code emit leaks the internal FM `key`.
23. **B1** `job.approval.required` and `job.log.appended` have no emit site.
24. **B6-1** `channels.jobs`, `partnerCallback`, and `deliverCallback` still carry `x-status: planned` while built and live (and the file header still says no WS server exists).
25. **B6-2 / B6-3** WSS `subscribe.events` filter is ignored (all events forwarded to all subscribers); `heartbeat.ping` sends `{ts}` not `{server_time}`.

---

## Recommended disposition

- **Spec-text fixes (cheap, no client impact):** B6-1 honesty flags; A1-2 disclosure widening; A2-6/A2-7 validation reality; A4-2 collapse `FailureCode` to what is emitted; A4-3 the m4 terminal-sample gate; A4-6/A4-7/A4-8 status-field reality; A5-3/A5-4 sample mutability + provenance; A6-6 output precedence; A9-1/A9-3/A9-6 callback shape + 429 + audit; A10-3 cancel ambiguity; A13-2/A13-4 delete unenforced limits; B3-3, B4-3, B5-3/B5-5/B5-6, B6-6/B6-8, C-5/C-8.
- **Code fixes that unblock partners (highest value first):** A2-1/A2-2/A2-3 (the create-field landing — these are functional bugs, not just drift), A4-1 (REST `sample_ready`), B1 (state/previous_state + nested artifact descriptor), A5-1/A6-1/A8-1/A2-4 (the four thin response bodies), A7-1 (stream the download), A12-1, A11-1, B6-2, B6-3.
- **Pick one convention for not-ready:** either implement 409 `not_ready` with `retry_after_seconds` as both specs promise, or amend both specs to 404 — but do it once, across all four artifact endpoints.
