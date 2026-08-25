# Audit: docs/railway-migration.md vs the codebase (branch `file-master-artifacts`)

Auditor scope: verify every actionable claim against the actual code, for an
operator who deploys **web-UI only** (no `railway` CLI). No test suites run.
All file references are repo-relative below; repo root is
`/mnt/d/John/u-ecom-scraper`.

Verdicts: **ACCURATE** / **INACCURATE** (correction needed) / **STALE**
(this branch changed the facts) / **MINOR** (gap or nit, fix optional).

---

## 1. "Where do YOU start?" header

| Claim | Verdict | Evidence |
|---|---|---|
| Already-running stacks start at Phase 11; Phases 1–10 are history; Phase 12 optional | ACCURATE | Structure matches the doc's own phases. |
| migration 0033 exists | ACCURATE | `webapp/scraper/migrations/0033_apikey_jobcallback_eventoutbox.py` (dep `0032`, Django 5.2.17, 2026-08-24). |
| `celery-events` is a NEW service the user creates | ACCURATE | `docker-compose.yml` lines 134–170 define it (`profiles: ["full"]`). |
| beat entry automatic on beat restart | ACCURATE | `webapp/config/settings.py` `CELERY_BEAT_SCHEDULE["dispatch-pending-callbacks"]` → `scraper.events.reconciler.dispatch_pending_callbacks` @ `30.0`; DatabaseScheduler installs code-side entries on startup, and the doc says "Restart celery-beat after this deploy." |
| `event-gateway` optional, in compose | ACCURATE | `docker-compose.yml` lines 172–205 (`profiles: ["full"]`). |
| partner API lives on existing `django` service, no new config | ACCURATE | `webapp/scraper/urls.py:126` mounts `path("api/v1/", include("scraper.api.urls"))`; 13 routes in `webapp/scraper/api/urls.py`; docs pages at `webapp/scraper/urls.py:108-109` (`/docs/sync_api`, `/docs/async_api`, `@login_required`); no new env vars required (the 4 new optional vars all have code defaults). |
| **"migration 0033 + the date-window data fixes — runs automatically on `migrate`"** | **INACCURATE** | (a) 0033 contains **no data fixes** — zero `RunPython` (only `AddField`/`AlterField`/`CreateModel`; the only `RunPython`s in the tree are 0019 and 0031). (b) The date recompute is **not automatic** — it is the one-time manual admin button the *same header* calls out two lines later ("One-time post-deploy task: the date recompute (Phase 11 §5)"). The sentence contradicts the header itself. |
| "18 commits ahead of origin" | STALE (cosmetic) | Was true at write time (19 at the docs commit `026d1a3`; its parent is 18). Now **21** ahead of `origin/file-master-artifacts` (165 ahead of `origin/main`). |
| "Verified against … `file-master-artifacts` @ `1ce8ec6`" (line 5) | MINOR | `1ce8ec6` exists and is an ancestor of HEAD — but Phases 11–12 describe code far past it (slices `80799db..948ccc7`, gateway `c934c2d`/`b4682b0`, admin button `3001474`). The verification basis undersells for the new phases. |

**Correction 1 (required):** header bullet 1 — drop "the date-window data fixes"
from the runs-automatically line; say "migration 0033 runs automatically on
`migrate` (it is schema-only; the date repair is the separate one-time admin
button, §5)".

---

## 2. Phase 11 §1 — `celery-events` service table

| Field | Doc says | Compose says | Verdict |
|---|---|---|---|
| Name | `celery-events` | `celery-events` | ACCURATE |
| Start Command | `sh -c "python manage.py migrate --noinput && celery -A config worker -l INFO -Q events -Ofair --concurrency=4"` | identical (YAML line-folded) | ACCURATE — exact match |
| Memory | 512 MB | `mem_limit: 512m` | ACCURATE |

- "Same env vars as celery-worker — no new secrets" — **ACCURATE on the
  load-bearing part**: nothing in `webapp/scraper/events/*.py` or
  `webapp/scraper/api/` reads a new required env var (only `PARTNER_*` /
  `OUTPUT_CACHE_*` with defaults, read on django). Compose deliberately gives
  `celery-events` a **subset** of the worker's vars (no `ZAI_*`,
  `PLAYWRIGHT_MCP_URL`, `CODE_WRITER_MODEL`, LITELLM) — "same" is loose but
  harmless since the doc's method is "duplicate the worker service".
- "a worker without the 0033 tables … **crash-loops**" — **MINOR
  overstatement.** `config/celery.py` imports the events modules at boot
  (model imports don't touch the DB); the worker itself boots fine — the
  30s sweep *task* fails with `ProgrammingError` each cycle until tables
  exist. The migrate-first guidance is still correct; only the verb is wrong.
- "change ONLY" table — **MINOR GAP:** if Railway's duplicate carries the
  worker's `/app/workspace` volume (Phase 7 §4), remove it from
  `celery-events` — the events worker never writes there, and a Railway
  volume cannot be attached to two services. `RAILWAY_RUN_UID=0` would also
  be inherited (harmless, unnecessary).

---

## 3. Phase 11 §2 + §3 — beat entry and routing

- **§2 ACCURATE.** `CELERY_BEAT_SCHEDULE` carries `dispatch-pending-callbacks`
  @ 30s; DatabaseScheduler reads code-side schedules on restart; no DB rows
  needed; doc correctly says restart beat.
- **§3 ACCURATE.** `CELERY_TASK_ROUTES` in settings.py sends all three
  event tasks to `events`; the scrape worker's start command has no `-Q`
  (listens on default `celery` only) and `celery-events` is `-Q events` only
  — queue isolation is real, and "a hung partner endpoint can never touch
  scrape capacity" holds (delivery is `httpx.post` with a 10s timeout at
  concurrency 4 on its own pool).

---

## 4. Phase 11 §4 — post-deploy checks (web-UI / shell-free)

The table is genuinely shell-free (Railway **Logs** tabs); the CLI block is
labeled "alternative". Three log lines verified against code:

| Line | Verdict | Evidence |
|---|---|---|
| (a) `Received task: scraper.events.dispatcher.deliver_callback` | ACCURATE | Task registered under exactly that name (`@shared_task def deliver_callback` in `webapp/scraper/events/dispatcher.py`, imported via `app.conf.imports`; route keys confirm the name). Standard celery "Received task" INFO line. The `KeyError` phrasing also matches celery's real unregistered-task output. |
| (b) `Scheduler: Sending due task dispatch-pending-callbacks` | ACCURATE | Beat's standard line; the schedule name in settings.py is exactly `dispatch-pending-callbacks`. |
| (c) `events sweep: dispatched=... reconciled=...` | **INACCURATE as a fresh-deploy check** | `webapp/scraper/events/reconciler.py:73-76` logs the line **only `if dispatched or reconciled:`** — when both are 0 the line is *never emitted*. On a fresh deploy (no `created_via="api"` jobs, no callbacks — both gates verified in `emitter`/`reconciler`/`claim_due_rows`) the check as written **can never pass**, and the doc's "`dispatched=0` is FINE" phrasing implies a `dispatched=0` line that will never print. |

**Correction 2 (required):** row (c) should read: expect **no** `events
sweep` line until the first API-created job fires an event — silence is the
healthy state; the real proof the sweep runs is beat's row-(b) line plus the
standard `Task scraper.events.reconciler.dispatch_pending_callbacks[…] …
succeeded` INFO line on `celery-events` every 30s.

---

## 5. Phase 11 §5 — recompute as admin buttons

**ACCURATE end-to-end.**

- `/admin/scraper/joblisting/` exists — `JobListingAdmin` registered
  (`webapp/scraper/admin.py:301`).
- Both buttons exist with **exact labels**: template at
  `webapp/scraper/templates/admin/scraper/joblisting/change_list.html`
  renders "Preview date-reliability recompute" and "APPLY recompute
  (--write)" into the changelist's object-tools (top toolbar), via
  `changelist_view` injecting `recompute_url`.
- The click-path is real: `get_urls()` adds `recompute-dates/` wrapped in
  `admin_site.admin_view` (+ an explicit `is_superuser` gate — matches the
  doc's "log in as the superuser"); it calls
  `call_command("recompute_date_reliability", *([\"--write\"] …))` and
  surfaces the output as a green `messages.success` — matches "a green
  message shows DRY RUN counts".
- The command exists (`webapp/scraper/management/commands/recompute_date_reliability.py`),
  dry-run by default, `--write` applies — matches the doc.
- Expected counts sane: `BROKEN_FROM = 2026-07-22` matches the a66e33f date;
  "~1.3k would-fix" matches the actual local run (commit `955ad85`: 1,290
  recovered); "~6k scanned … your numbers will differ" is correctly hedged.
- Idempotency claim holds: the queryset filters `date_posted_reliable=False`,
  so applied rows drop out of subsequent runs; preview never writes.

---

## 6. Phase 11 §6 — partner rollout

- `create_api_key` **exists**
  (`webapp/scraper/management/commands/create_api_key.py`), `--user` required,
  prints the raw key once, refuses superusers, `--rotate` replaces.
- The doc **honestly flags the shell dependency** ("Needs a shell — if
  you're web-UI-only … ask whoever has CLI access, or run it once via a
  temporary start-command prefix") — and the start-command-prefix route *is*
  a web-UI path, so no required step in the primary flow is CLI-only.
- `/admin/scraper/apikey/` exists as claimed: `ApiKeyAdmin` registered with
  prefix / user / label / created_at / last_used_at / revoked_at / is_active
  — read-only surface, cannot mint. Matches the doc exactly.
- `callback.delivered_count` on `GET /api/v1/jobs/{id}/callback` — real
  (`webapp/scraper/api/writers.py:_callback_payload`); "also visible in
  `/admin/scraper/jobcallback/`" — real (`JobCallbackAdmin`, `delivered_count`
  in list_display, secret excluded).

**Verdict: ACCURATE.**

---

## 7. Phase 12 — event gateway

| Claim | Verdict | Evidence |
|---|---|---|
| Service name `event-gateway`, source = repo root, Dockerfile at `event_gateway/Dockerfile` | ACCURATE | compose `build: {context: ., dockerfile: event_gateway/Dockerfile}`. |
| Start Command (image default) `uvicorn app:app --host 0.0.0.0 --port 8100` | ACCURATE | `event_gateway/Dockerfile` CMD (also passes `--no-access-log` — cosmetic omission, "image default" is the point). |
| Env: same DB/Redis vars as django + `PYTHONPATH=/app:/app/webapp` | ACCURATE | compose block has `REDIS_URL`, `DB_*`, `DJANGO_SETTINGS_MODULE`, `SECRET_KEY`, `PYTHONPATH: /app:/app/webapp`. |
| "PYTHONPATH must include `/app/webapp` (compose env overrides the image's)" | ACCURATE and load-bearing | Image sets `ENV PYTHONPATH=/app:/app/webapp`; on Railway the Phase-3 shared `PYTHONPATH=/app` would clobber it — the doc's explicit per-service override is the correct fix. The gateway imports `config.settings` (`event_gateway/app.py:15-19`). |
| Memory 256 MB | ACCURATE | compose `mem_limit: 256m`. |
| Health: `<url>/health` → `{"status":"ok","service":"event-gateway"}` | ACCURATE | `app.py:42-44` returns exactly that (plus `ts`). |
| Client URL `wss://<gateway-host>/ws/v1/jobs` | ACCURATE | `@app.websocket("/ws/v1/jobs")` (`app.py:47`). |
| Auth: single-use token via `POST /api/v1/ws-token` with X-API-Key, or `?apikey=<key>` | ACCURATE | `webapp/scraper/api/sse.py:ws_token` (POST-only, `resolve_api_key` reads the `X-API-Key` header); `app.py:53` reads `ws.query_params.get("apikey")`. |
| "All 9 protocol behaviors verified … 14 consecutive e2e runs" | NOTE (unverifiable) | Historical claim; `event_gateway/test_protocol.py` + `conftest.py` exist but runs aren't auditable from the repo. Not actionable. |
| (missing) PORT guidance | MINOR GAP | Phases 4/5/6 make PORT the healthcheck rule (image hardcodes 8100). Phase 12 gives no healthcheck instruction and no `PORT=8100`. `EXPOSE 8100` usually suffices for domain mapping, but if the user adds a `/health` healthcheck per the house style they hit the same PORT trap the doc documents elsewhere. One line would close it. |

---

## 8. Phases 1–10 + Appendix — staleness scan

Verified-current items (no action): Phase 4 file-master `/health` →
`{"ok": true}` and hardcoded 8002 CMD; Phase 5 pre-deploy `|| true`,
build-time collectstatic (`Dockerfile:38`), gunicorn CMD, `/api/health/raw`
(no slash, `webapp/config/urls.py:9`), `healthcheck.railway.app` host rule;
Phase 6 `DISPLAY=:98` / `MCP_CDP_PORT=19222` / `SCRAPER_CDP_PORT=19223` and
both checkpoint log lines (`browser_service/browser_pool.py:46`,
`browser_service/server.py:159`); Phase 7 worker command + 2.5 GB
self-recycle (`CELERY_WORKER_MAX_MEMORY_PER_CHILD` default 2621440); Phase 8
no-migrate-prefix divergence (deliberate, documented); Phase 10 §3 "all six
tiles" — correct, the template's render order array is exactly those six
(`health.html:202`; `file_master` is fetched by the API but not rendered).

Stale / gap items introduced by this branch:

1. **Line 31 "You will create 8 Railway services in this exact order"** —
   with Phase 11 it is **9** (10 with the optional gateway). The framing
   sentence of the whole runbook. STALE.
2. **Phase 8 §6 (line 315) "it runs three schedules (every 5 min)"** —
   now **four**: three @300s plus `dispatch-pending-callbacks` @30s.
   Material, not just cosmetic: a from-scratch reader following Phases 1–10
   only ends up with beat enqueueing to an `events` queue nobody consumes —
   exactly the "invisible Redis buildup" Phase 11's intro warns about.
   STALE — Phase 8 should mention the 4th entry and point at Phase 11
   ordering.
3. **Appendix rebuild-from-scratch order (line 398)** — missing
   `celery-events` (and optional `event-gateway`). Same material
   consequence as above. STALE.
4. **Appendix "All env vars actually read by the code" (line 399)** — this
   branch added 4 env-readable knobs with working defaults:
   `PARTNER_STREAM_BUDGET`, `PARTNER_STREAM_DEADLINE`
   (`webapp/scraper/api/sse.py:39-41`), `OUTPUT_CACHE_FILES`,
   `OUTPUT_CACHE_BYTES` (`webapp/scraper/api/window_cache.py:58-59`).
   "Set only when tuning" still applies; the list is just no longer
   complete. STALE (minor).
5. Phase 10 §5 failure table has no events-worker row — optional
   enhancement, not stale.

**CLI-only check across the whole doc:** every CLI mention is either labeled
alternative (Phase 10 §1 `railway variables`, Phase 11 §4 CLI block) or sits
in an optional phase (Phase 9 flower private access). The one true shell
need — `create_api_key` — is honestly flagged with a web-UI workaround
(temporary start-command prefix). **No required step is CLI-only.**

---

## 9. Rollback section

**ACCURATE.**

- "events worker is additive: stopping it freezes delivery (rows PENDING —
  they queue, never drop) while REST/SSE keep working" — mechanically
  correct: outbox rows persist in Postgres; SSE reads the Redis pubsub
  channel directly (`emitter._publish_envelope` → `sse.py`), not via the
  worker; REST never touches it.
- "`git revert` the slice commits" — the slices (`80799db..948ccc7`) exist
  and are in HEAD.
- **"the 0033 migration is additive (new tables + nullable columns) and safe
  to leave in place" — verified by reading the migration**: exactly three
  `CreateModel` (ApiKey, JobCallback, EventOutbox) + one `AddField`
  (`created_via`, with default `'intake'`) + two *widening* `AlterField`s
  (`search_criteria` CharField(500) → TextField; `url` URLField(200 default)
  → URLField(1000)). **No `RunPython`, no drops, no nullability removals.**
  (Wording nit: "nullable columns" → "widened columns" is more precise; the
  safety conclusion is right either way.)
- Optional one-liner to add: with the worker stopped, beat keeps enqueuing
  1 sweep message/30s into the `events` queue — harmless (drains on
  restart, sweeps are idempotent claims), but worth saying so nobody
  panics at the queue depth.

---

## Required corrections (apply none of these — audit only)

1. **Header, "runs automatically on migrate" bullet** — remove "the
   date-window data fixes" from that clause; 0033 is schema-only (no
   RunPython) and the date recompute is the manual §5 admin button. The
   current sentence contradicts the header's own last line.
2. **Phase 11 §4 check (c)** — `events sweep: dispatched=… reconciled=…` is
   logged only when either count is non-zero (`reconciler.py:73`), so on a
   fresh deploy the check can never pass. Rewrite: silence is healthy; the
   observable proofs are beat's `Sending due task dispatch-pending-callbacks`
   (row b) and the `Task …dispatch_pending_callbacks… succeeded` INFO line on
   `celery-events` every 30s.
3. **Phase 8 §6** — "three schedules" → four; note the 30s
   `dispatch-pending-callbacks` entry and that it dispatches to the `events`
   queue (cross-reference Phase 11 ordering).
4. **Appendix rebuild order** — append `celery-events` (and optional
   `event-gateway`).
5. **Line 31** — "8 Railway services" → 9 (10 with the optional gateway).
6. **Appendix env-var list** — add `PARTNER_STREAM_BUDGET`,
   `PARTNER_STREAM_DEADLINE`, `OUTPUT_CACHE_FILES`, `OUTPUT_CACHE_BYTES`
   (all defaulted, tune-only).

## Optional polish (not required)

- Header "18 commits ahead of origin" → now 21 (or drop the number).
- Line 5 verification basis `@ 1ce8ec6` predates all Phase 11/12 code.
- Phase 11 §1: "crash-loops" → "its 30s sweep task fails with
  ProgrammingError until `migrate` creates the tables" (worker boots fine).
- Phase 11 §1 "change ONLY" table: add "remove the duplicated
  `/app/workspace` volume if present" (events worker never writes there; a
  Railway volume can't attach to two services).
- Phase 12: one line on `PORT=8100` if the operator adds a `/health`
  healthcheck (house rule from Phases 4/5/6).
- Rollback: one line noting beat keeps enqueueing ~1 msg/30s into `events`
  while the worker is stopped (drains on restart; harmless).
