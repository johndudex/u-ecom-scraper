# Railway Migration Guide

**Repo:** `github.com/al-johnf/ExtractorBuilderAi` · **Branch:** `file-master-artifacts`
**Railway docs:** <https://docs.railway.com/guides/docker-compose>
**Status:** ✅ Fully implemented + e2e verified (adameve.com, 20 products, 12 FM PUTs)

---

## TL;DR

Railway doesn't run `docker-compose.yml` — each compose service becomes a Railway service. The codebase now uses a **File Master** (FastAPI artifact store on a volume) for cross-service artifact sharing, and **stateless `/scrape`** (scraper source in the POST body). Both work identically in compose and Railway. `workspace/` stays local on the worker.

---

## What was built (13 commits on `file-master-artifacts`)

| Commit | What |
|--------|------|
| `aaeb2b6` | `file_master/` (FastAPI: PUT/GET/HEAD/list/DELETE/stream/health) + `src/artifacts.py` (httpx client) |
| `82f589a` | Stateless `/scrape`: `scraper_source` + `extra_files` in body, `output_content` in response |
| `f0fba8f` `6c6f45e` `0034a7d` | Worker `scrapers/` writes → FM (graph.py, tasks.py, models.py, setup_workspace) |
| `f44882f` | Django `scrapers/` reads → FM (views.py: 28 endpoints + proxy view) |
| `e205d7e` `ee24e3d` | Compose wiring (file-master service, `FILE_MASTER_URL`, drop browser mounts) |
| `04aa61b` | Pruning (keep newest 5 outputs per site) |
| `9e4c473` | `extra_files` in `/scrape` (stage input_urls.json alongside scraper) |
| `b7b20ac` | Railway-ready: whitenoise, REDIS_URL setting, headless Chrome toggle |

### Key design decisions (different from earlier doc iterations)

1. **`/scrape` takes `scraper_source` (bytes) + `extra_files`, NOT a `scraper_key`.** browser_service is **fully stateless** — no FM access, no DB, no volume. The worker reads the local `workspace/{slug}/scraper_draft.py`, POSTs the source + sibling files (input_urls.json, discovery_config.json), and receives the output content in the response.

2. **`workspace/` stays LOCAL on the worker** (not in FM). Only `scrapers/` (published artifacts) goes through FM. This was the "minimal scope" decision — ~47 workspace call sites unchanged.

3. **browser_service does NOT use FILE_MASTER_URL.** It never imports `src.artifacts`. The worker handles all FM interaction.

4. **DB-stored paths are now FM keys.** `job.scraper_file`, `job.output_file`, `Site.default_scraper_path` store logical keys like `scrapers/{slug}/scraper.py` (resolved by django's `_fm_key_for` helper).

---

## Architecture

```
                    ┌──────────────────────────────┐
   public HTTPS ───▶│  django  (web + SSE + admin)  │  gunicorn :8000
                    │  reads artifacts via File Master│  whitenoise for static
                    └──────┬─────────────────────────┘
                           │ private net
            ┌──────────────┼──────────────────────────────┐
            ▼              ▼                               ▼
   ┌────────────────┐    ┌──────────────────────┐   ┌─────────────────┐
   │ Postgres       │    │  celery-worker        │   │  redis          │
   │ (metadata only)│    │  workspace/ LOCAL     │   │  (managed)      │
   │ (managed)      │    │  scrapers/ → FM       │   │                 │
   └────────────────┘    └──────────┬───────────┘   └─────────────────┘
                                     │
                    ┌────────────────┼────────────────────────────┐
                    ▼                ▼                            ▼
          ┌──────────────────┐  ┌──────────────────────┐  ┌──────────────┐
          │  ★ file-master ★ │  │  browser_service      │  │  celery-beat │
          │  FastAPI+VOLUME  │  │  STATELESS: source in │  │  (1 replica) │
          │  scrapers/ tree  │  │  → output out (no FM) │  └──────────────┘
          │  HTTP :8002      │  │  uvicorn :8001        │
          └──────────────────┘  │  +2 headless Chromes  │
                                │  MCP :8111            │
                                └──────────────────────┘
          ┌────────────┐
          │  flower    │  optional, private-only
          └────────────┘
```

---

## Environment variables — COMPLETE reference

### Per-service variable table

Marked 🔒 = Railway Secret. Reference vars use `${{service.VAR}}` syntax.

| Variable | django | celery-worker | browser_service | file-master | celery-beat | flower |
|---|---|---|---|---|---|---|
| **Database** | | | | | | |
| `DB_HOST` | ✓ `${{postgres.RAILWAY_PRIVATE_DOMAIN}}` | ✓ | — | — | ✓ | ✓ |
| `DB_PORT` | ✓ `5432` | ✓ | — | — | ✓ | ✓ |
| `DB_NAME` | ✓ `${{postgres.PGDATABASE}}` | ✓ | — | — | ✓ | ✓ |
| `DB_USER` | ✓ `${{postgres.PGUSER}}` | ✓ | — | — | ✓ | ✓ |
| `DB_PASSWORD` | 🔒 `${{postgres.PGPASSWORD}}` | 🔒 | — | — | 🔒 | 🔒 |
| **Redis** | | | | | | |
| `REDIS_URL` | ✓ `${{redis.REDIS_PRIVATE_URL}}` | ✓ | — | — | ✓ | ✓ |
| **Django** | | | | | | |
| `SECRET_KEY` | 🔒 (same value all services) | 🔒 | — | — | 🔒 | 🔒 |
| `DJANGO_SETTINGS_MODULE` | `config.settings` | `config.settings` | — | — | `config.settings` | `config.settings` |
| `DEBUG` | `False` | — | — | — | — | — |
| `ALLOWED_HOSTS` | your `*.railway.app` domain | — | — | — | — | — |
| `CSRF_TRUSTED_ORIGINS` | `https://yourapp.up.railway.app` | — | — | — | — | — |
| `DJANGO_SUPERUSER_PASSWORD` | 🔒 (for initial createsuperuser) | — | — | — | — | — |
| **Proxy / TLS** (auto-when DEBUG=False) | | | | | | |
| `SECURE_SSL_REDIRECT` | `True` | — | — | — | — | — |
| `SESSION_COOKIE_SECURE` | `True` (default !DEBUG) | — | — | — | — | — |
| `CSRF_COOKIE_SECURE` | `True` (default !DEBUG) | — | — | — | — | — |
| **PgBouncer** | | | | | | |
| `DB_USE_PGBOUNCER` | `True` (when PgBouncer enabled) | `True` | — | — | `True` | `True` |
| **Python** | | | | | | |
| `PYTHONPATH` | `/app` | `/app` | `/app` | — | `/app` | `/app` |
| **File Master** | | | | | | |
| `FILE_MASTER_URL` | ✓ `http://file-master.railway.internal:8002` | ✓ | — | — | — | — |
| **Browser service** | | | | | | |
| `BROWSER_SERVICE_URL` | ✓ `http://browser-service.railway.internal:8001` | ✓ | — | — | — | — |
| `PLAYWRIGHT_MCP_URL` | ✓ `http://browser-service.railway.internal:8111/sse` | ✓ | — | — | — | — |
| **LLM (Z.AI)** | | | | | | |
| `ZAI_API_KEY` | 🔒 | 🔒 | — | — | — | — |
| `ZAI_BASE_URL` | `https://api.z.ai/api/coding/paas/v4/` | ✓ | — | — | — | — |
| `ZAI_MAIN_MODEL` | `glm-5-turbo` | ✓ | — | — | — | — |
| `ZAI_SMALL_MODEL` | `glm-5-turbo` | ✓ | — | — | — | — |
| `CODE_WRITER_MODEL` | — | `glm-5.2` | — | — | — | — |
| **Celery** | | | | | | |
| `CELERY_TASK_ACKS_LATE` | — | `true` | — | — | — | — |
| `CELERY_WORKER_MAX_MEMORY_PER_CHILD` | — | `2621440` | — | — | — | — |
| `CELERY_WORKER_MAX_TASKS_PER_CHILD` | — | `10` | — | — | — | — |
| **Browser service internals** | | | | | | |
| `DISPLAY` | — | — | `""` (empty → headless) | — | — | — |
| `PROJECT_ROOT` | — | — | `/app` | — | — | — |
| `NAVIGATE_MAX_CONCURRENT` | — | — | `2` | — | — | — |
| `NAVIGATE_MAX_QUEUE` | — | — | `2` | — | — | — |
| `PROXY_DATACENTER_HOST` | — | — | 🔒 | — | — | — |
| `PROXY_DATACENTER_PORT` | — | — | `22225` | — | — | — |
| `PROXY_DATACENTER_USER` | — | — | 🔒 | — | — | — |
| `PROXY_DATACENTER_PASS` | — | — | 🔒 | — | — | — |
| `PROXY_RESIDENTIAL_HOST` | — | — | 🔒 | — | — | — |
| `PROXY_RESIDENTIAL_PORT` | — | — | `22225` | — | — | — |
| `PROXY_RESIDENTIAL_USER` | — | — | 🔒 | — | — | — |
| `PROXY_RESIDENTIAL_PASS` | — | — | 🔒 | — | — | — |
| **LLM (Z.AI) — optional tuning** | | | | | | |
| `ZAI_FALLBACK_MODEL` | — | `glm-5-turbo` (set ≠ primary for breaker) | — | — | — | — |
| `LLM_REQUEST_TIMEOUT` | — | `300` | — | — | — | — |
| `LLM_CIRCUIT_BREAKER_ENABLED` | — | `True` | — | — | — | — |
| `LLM_CIRCUIT_BREAKER_THRESHOLD` | — | `4` | — | — | — | — |
| `LLM_CIRCUIT_BREAKER_COOLDOWN` | — | `60` | — | — | — | — |
| `LLM_CLASSIFIED_RETRY` | — | `True` | — | — | — | — |
| `LLM_MAX_RETRIES` | — | `5` (only when classified=off) | — | — | — | — |
| `LLM_TRUNCATION_MAX_CHARS` | — | `180000` | — | — | — | — |
| **Celery task limits (optional)** | | | | | | |
| `CELERY_TASK_SOFT_TIME_LIMIT` | — | `7200` (2h) | — | — | — | — |
| `CELERY_TASK_TIME_LIMIT` | — | `7560` (2h6m) | — | — | — | — |
| `CELERY_TASK_REJECT_ON_WORKER_LOST` | — | `False` | — | — | — | — |
| **Worker scrape runtime (optional)** | | | | | | |
| `EXECUTION_TIMEOUT` | — | `3600` | — | — | — | — |
| `EXECUTION_STALL_TIMEOUT` | — | `300` | — | — | — | — |
| `SCRAPER_EXECUTION_MODE` | — | `auto` | — | — | — | — |

### Variables NOT needed (common mistakes)
- `FILE_MASTER_URL` on browser_service → ❌ browser_service is stateless, never imports artifacts
- `ARTIFACT_STORAGE` → ❌ no dual-mode switch; FM is always HTTP
- `S3_BUCKET` / `S3_ENDPOINT` → ❌ no external storage; FM uses a local volume
- `DEBUG_AUTO_LOGIN` → ❌ no-op on Railway (hardened: requires `DEBUG=True` or pytest context)

> **Service-name gotcha:** Railway private DNS is **lowercase hyphenated** — `file-master.railway.internal`, `browser-service.railway.internal`. Underscored names (`browser_service`) won't resolve. The code defaults (`BROWSER_SERVICE_URL`, `PLAYWRIGHT_MCP_URL`) use underscored names — the Railway env overrides are **required**, not optional.

> **CSRF is the #1 silent killer.** Without `CSRF_TRUSTED_ORIGINS=https://yourapp.up.railway.app`, every form POST (login, job submit, approval) returns 403. Django 5.x removed the `ALLOWED_HOSTS`→`CSRF_TRUSTED_ORIGINS` auto-mapping. Set it explicitly.

> **Pre-Deploy is mandatory.** The Dockerfile CMD is `gunicorn` only — no migrate, no collectstatic, no createsuperuser. Set the django service's **Pre-Deploy command** to: `python manage.py migrate --noinput && python manage.py collectstatic --noinput && python manage.py createsuperuser --noinput --username admin --email admin@example.com`

---

## Per-service Railway config

### Postgres (managed plugin)
- **Provision:** New → Database → PostgreSQL. Enable **PgBouncer** (django + worker + beat all connect).
- **Wire:** consumers use `DB_HOST` etc. (settings.py reads individual components, NOT `DATABASE_URL`).
- **No artifact blobs** — only job/site/approval/session rows.

### Redis (managed plugin / Upstash)
- **Provision:** New → Database → Redis.
- **Wire:** `REDIS_URL=${{redis.REDIS_PRIVATE_URL}}` (keep the `rediss://` scheme verbatim — Celery/redis-py handle TLS).
- Pin to same region as the worker.

### file-master (artifact store)
- **Source:** `file_master/Dockerfile` (root dir = repo root via `build: ./file_master`).
- **Start:** `uvicorn app:app --host 0.0.0.0 --port 8002 --workers 1`
- **Volume:** mount at `/data` (persistent).
- **Port:** `8002`, **private only** — no public domain.
- **Healthcheck:** `GET /health` (boots in ~2s).
- **Replicas:** 1 (singleton).
- **No env vars needed** — `FILE_MASTER_ROOT` defaults to `/data`.

### django (web)
- **Source:** `./Dockerfile` (root dir = repo root).
- **Start:** Dockerfile `CMD` (`gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2`). Never `runserver` in prod.
- **Pre-Deploy command:** `python manage.py migrate --noinput && python manage.py collectstatic --noinput` (wrap migrate in a retry — no `depends_on`).
- **Port:** auto from `EXPOSE 8000`. Generate public domain.
- **Static files:** ✅ `whitenoise` installed + `WhiteNoiseMiddleware` active (commit `b7b20ac`).
- **Healthcheck:** `GET /health/`.
- **Artifacts:** `FILE_MASTER_URL` set; django reads via `_fm_read_text` / `_fm_read_json` / `/fm/artifact/<key>` proxy view.

### celery-worker
- **Source:** `./Dockerfile`.
- **Start:** `celery -A config worker -l INFO --concurrency=2 -Ofair` (from `/app/webapp`).
- **`workspace/`** stays LOCAL — needs a Railway volume at `/app/workspace` (persistent for in-flight jobs across restarts).
- **`scrapers/`** → File Master (no local volume needed for scrapers/).
- **Replicas:** 1 (single shared browser_service Chrome).
- **Healthcheck:** `celery -A config inspect ping --timeout=10` (drop `-d celery@$(hostname)` — Railway's container hostname doesn't reliably match Celery's nodename), start_period 60s.
- **Resources:** 8 GB / 4 vCPU (LLM agent contexts are RAM-heavy).

### browser_service (stateless)
- **Source:** `browser_service/Dockerfile` (root dir = repo root).
- **Start:** Dockerfile `CMD` (uvicorn on :8001).
- **NO `FILE_MASTER_URL`** — browser_service never touches FM. It receives `scraper_source` + `extra_files` in the POST body, stages to `/tmp`, runs, returns `output_content` in the response.
- **`DISPLAY=""`** on Railway → headless mode (`--headless=new`, no Xvfb). ✅ Implemented (commit `b7b20ac`).
- **Chrome `/dev/shm`:** already mitigated (`--disable-dev-shm-usage` on all Chrome launches).
- **Ports:** `8001` private (django/worker reach it); `8111`/`9222`/`9223` **private only**.
- **Healthcheck:** `GET /health` on :8001, grace ≥ 90s.
- **Resources:** 8–16 GB / 4+ vCPU.

### celery-beat
- **Source:** `./Dockerfile`.
- **Start:** `celery -A config beat -l INFO` (from `/app/webapp`). Drop `migrate` (django owns it).
- **Replicas:** 1, disable autoscale + sleep. Two beats = duplicate tasks.
- **No volume** — `DatabaseScheduler` stores schedule in Postgres.

### flower (optional)
- **Start:** `celery -A config flower --port=${PORT:-5555}`.
- **No public domain without auth.** Keep private-only or add `--basic-auth`.

---

## Deployment order

1. **Postgres** → healthy (enable PgBouncer).
2. **Redis** → healthy.
3. **file-master** → healthy (`/health`).
4. **django** (Pre-Deploy: migrate + collectstatic).
5. **browser_service** (DISPLAY="" for headless; healthcheck 90s grace).
6. **celery-worker** (`CELERY_TASK_ACKS_LATE=true`; volume at `/app/workspace`).
7. **celery-beat** (after django migrated).
8. **flower** (optional).

Smoke test: submit a job → worker writes scraper_draft to local workspace → `run_scraper` POSTs source + `extra_files` to browser_service → browser_service returns `output_content` → worker writes output to local workspace → `_finalize_job` publishes output + scraper to FM → django serves from FM.

---

## Gotchas checklist

- [x] **File Master built + verified** — `file_master/` + `src/artifacts.py`, e2e verified (12 FM PUTs, django serving)
- [x] **Stateless `/scrape`** — `scraper_source` + `extra_files` in body, `output_content` in response
- [x] **`extra_files` staging** — input_urls.json + discovery_config.json staged alongside scraper in `/tmp`
- [x] **Django proxy view** `/fm/artifact/<key>` — streams from FM (users never hit FM directly)
- [x] **Artifact pruning** — keep newest 5 outputs per site (Phase 6)
- [x] **Headless Chrome** — `--headless=new` + skip Xvfb when `DISPLAY=""` (commit `b7b20ac`)
- [x] **Static files** — whitenoise installed + middleware active (commit `b7b20ac`)
- [x] **REDIS_URL setting** — explicit `REDIS_URL` in settings.py (fixes health-view bug)
- [ ] **`runserver` → `gunicorn`** — Dockerfile CMD is already gunicorn; ensure Pre-Deploy runs migrate
- [ ] **beat = 1 replica** — set in Railway UI
- [ ] **flower auth** — no public domain without `--basic-auth`
- [ ] **Postgres PgBouncer** — enable in managed plugin settings
- [ ] **Service names hyphenated** — `file-master`, `browser-service`
- [ ] **Region pin** — all services in same region
- [ ] **Worker volume** — mount at `/app/workspace` (persistent for in-flight jobs)

---

## E2E verification results (job 196, adameve.com, search_term=toys)

```
status=completed, product_count=20, platform=custom .NET (aspx)
```

**12 FM PUTs fired during the full pipeline run:**
```
PUT scrapers/adameve-com/scraper.py                    ✅ _promote_scraper
PUT scrapers/adameve-com/jobs/scraper-196.py           ✅ per-job attribution
PUT scrapers/adameve-com/jobs/dagster-196.py           ✅ _invoke_dagster_converter
PUT scrapers/adameve-com/adameve-com_dagster.py        ✅ dagster production
PUT scrapers/adameve-com/output_2026-07-29_*.json ×3   ✅ _finalize_job
PUT scrapers/adameve-com/analysis/*.json ×5            ✅ _finalize_job + _preserve + skill_learner
PUT scrapers/adameve-com/analysis/learning_report.json ✅ skill_learner
```

**Django served from FM (3 GETs):**
```
GET scrapers/adameve-com/jobs/scraper-196.py    ✅ scraper served to dashboard
GET scrapers/adameve-com/jobs/dagster-196.py    ✅ dagster served
GET scrapers/adameve-com/output_2026-07-29_*.json ✅ output served to dashboard
```

**Stateless `/scrape` invocations:**
- code_writer self-tests (2 invocations, exit code 0 on the second after edit)
- code_tester self-tests (2 invocations)
- probe (--discover-only, rc=0)

**No LLM timeouts** (product_analyzer ~3 min, code_writer ~12 min — both under 900s).

---

## Local dev — unchanged

```bash
docker compose --profile full up --build
```

`docker-compose.yml` includes the `file-master` service + `FILE_MASTER_URL` on django + celery-worker. `workspace/` stays bind-mounted on the worker. browser_service has no `scrapers/`/`workspace` mounts (stateless). The code path is identical to Railway.
