# Railway Migration Guide

**Goal:** run the app on **Railway** for production using **only Railway-managed services** (no S3/R2 or other external stores), while keeping the **same architecture working on local docker-compose**.

**Repo:** `github.com/al-johnf/ExtractorBuilderAi`
**Railway docs:** <https://docs.railway.com/guides/docker-compose>

---

## TL;DR — read this first

Railway does **not** run `docker-compose.yml`. Each compose service maps to a **Railway service** built from the same GitHub repo + Dockerfiles; managed **Postgres + Redis** replace the `postgres:`/`redis:` images. Each service keeps its own container — **celery-worker and browser_service stay separate**.

Railway volumes mount to a **single service** (not shared), and there's no managed object store — so the existing "everyone bind-mounts `scrapers/` + `workspace/`" pattern can't work across separate containers. The fix, used identically in **compose and Railway**, is a **File Master** service: one small stateful service that owns all artifacts on a volume and serves them over HTTP to the others. No Postgres blobs, no S3, one code path everywhere.

Everything else (Chrome flags, headless, managed DBs, env wiring) is config, not code.

---

## 1. Target architecture (File Master in both environments)

```
                    ┌──────────────────────────────┐
   public HTTPS ───▶│  django  (web + SSE + admin)  │  gunicorn :8000
                    │  reads artifacts via File Master│
                    └──────┬─────────────────────────┘
                           │ private net (HTTP)
            ┌──────────────┼──────────────────────────────┐
            ▼              ▼                               ▼
   ┌────────────────┐    ┌──────────────────────┐   ┌─────────────────┐
   │ Postgres       │    │  celery-worker        │   │  redis          │
   │ (metadata only)│    │  reads/writes via FM  │   │  (managed)      │
   │ (managed)      │    └──────────┬───────────┘   └─────────────────┘
   └────────────────┘               │
                                     │
              ┌──────────────────────┼────────────────────────────────┐
              │                      │                                │
              ▼                      ▼                                ▼
   ┌─────────────────────┐  ┌──────────────────────────┐      ┌────────────────┐
   │  ★ file-master ★    │  │  browser_service          │      │  celery-beat   │
   │  FastAPI + VOLUME   │  │  reads scraper via FM,    │      │  (1 replica)   │
   │  owns workspace/    │  │  writes outputs to FM     │      └────────────────┘
   │  + scrapers/        │  │  uvicorn :8001 + MCP :8111│
   │  HTTP :8002         │  │  + 2 headless Chromes     │
   └─────────────────────┘  └──────────────────────────┘
                                                                       
                    ┌────────────────┐
                    │  flower        │  optional, private-only
                    └────────────────┘
```

Same in compose (the File Master is a `docker-compose` service) and Railway (the File Master is a Railway service with a volume).

---

## 2. The File Master service (the one piece of new code)

### Why
Today, `django`, `celery-worker`, and `browser_service` share `workspace/` + `scrapers/` via bind mounts, and the worker hands the browser service a **file path** it then executes. Railway volumes are per-service, so that contract dies. Rather than scatter a dual-mode abstraction across ~30 call sites, we introduce one service that owns the artifacts and a clean HTTP API — used by every service, in both environments.

### The service — `file_master/`
A minimal FastAPI app + a persistent volume. No DB, no business logic — just a typed key/value file store.

```python
# file_master/app.py
from fastapi import FastAPI, Request, Response, HTTPException
from pathlib import Path
ROOT = Path("/data")                     # volume mount (compose: ./shared-data) or Railway volume

app = FastAPI()

@app.put("/artifacts/{key:path}")
async def put(key: str, request: Request):
    p = ROOT / key
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(await request.body())
    return {"ok": True, "size": p.stat().st_size}

@app.get("/artifacts/{key:path}")
async def get(key: str):
    p = ROOT / key
    if not p.is_file(): raise HTTPException(404)
    return Response(p.read_bytes(), media_type="application/octet-stream")

@app.head("/artifacts/{key:path}")
async def exists(key: str):
    p = ROOT / key
    if not p.is_file(): raise HTTPException(404)
    return Response()

@app.get("/list")
async def lst(prefix: str = ""):
    return [str(p.relative_to(ROOT)) for p in (ROOT/prefix).rglob("*") if p.is_file()] if (ROOT/prefix).exists() else []

@app.get("/health")
async def health(): return {"ok": True}
```

### The client — `src/artifacts.py` (single mode, always HTTP)
```python
import os, httpx
BASE = os.environ["FILE_MASTER_URL"].rstrip("/")          # e.g. http://file-master:8002

def write(key: str, data: bytes):
    httpx.put(f"{BASE}/artifacts/{key}", content=data, timeout=120)

def read(key: str) -> bytes:
    r = httpx.get(f"{BASE}/artifacts/{key}", timeout=120)
    if r.status_code == 404: raise FileNotFoundError(key)
    return r.content

def exists(key: str) -> bool:
    return httpx.head(f"{BASE}/artifacts/{key}", timeout=30).status_code == 200

def list_keys(prefix: str = "") -> list[str]:
    return httpx.get(f"{BASE}/list", params={"prefix": prefix}, timeout=30).json()

def public_url(key: str) -> str:
    # django proxies GET /api/artifact?key=... → File Master (so users never hit FM directly)
    return f"/api/artifact?key={key}"
```

### How the app changes (mechanical)
- **~30 call sites** in `webapp/agents/graph.py`, `run_execution.py`, `shell_tools.py`, and django views: replace `os.path.join(root, "workspace"|"scrapers", …)` with `artifacts.write/read(key, …)`.
- **`run_scraper`**: POST `{"scraper_key": "workspace/{slug}/scraper_draft.py"}` to `browser_service /scrape`. `browser_service` does `src = artifacts.read(scraper_key)` → writes `/tmp/scraper_{uuid}.py` → runs it → `artifacts.write(output_key, output_bytes)`. (No shared disk between worker and browser_service.)
- **Services that execute scrapers** (worker `code_tester`, browser_service): always `read()` to local `/tmp`, run, `write()` outputs back. Local tmp is per-container — fine.
- **Django** serves outputs by proxying `GET` to the File Master (a thin view), so users stay on the django public domain.

### Both environments, one codebase
| | Local compose | Railway |
|---|---|---|
| File Master | `file-master` compose service, volume `./shared-data` | Railway service, Railway volume at `/data` |
| `FILE_MASTER_URL` | `http://file-master:8002` | `http://file-master.railway.internal:8002` |
| Code path | **identical** | **identical** |

No `ARTIFACT_STORAGE` switch, no local-FS branch. Dev and prod are architecturally identical — what works locally works on Railway.

### Operational notes
- **Singleton + persistent volume.** 1 replica. If it's down, artifact reads/writes fail and the pipeline retries (browser_service/worker already retry on HTTP errors). Keep it dead-simple (the code above is the whole service).
- **Pruning:** extend the `cleanup`/`skill_learner` nodes to `DELETE` (or overwrite) old artifacts; add a `DELETE /artifacts/<key>` endpoint. Keeps the volume from growing unbounded.
- **No secrets in keys.** Keys are paths like `scrapers/{slug}/output_*.json` — never include credentials.
- **Swap backend later (optional):** if you ever want S3, only the File Master's internals change — the 30 call sites keep calling `artifacts.*`. That's the value of centralizing.

---

## 3. Prerequisites

```bash
npm i -g @railway/cli && railway login
railway init && railway link
```
Dashboard: **New → GitHub Repo → al-johnf/ExtractorBuilderAi → branch `lg-upgrade`**. Each service points at this repo with a **Root Directory** + **Dockerfile path** + **Start Command**.

---

## 4. Per-service migration

### 4.1 Postgres — Railway managed plugin (metadata only)
- **Approach:** managed Postgres. **No artifact blobs** — only job/site/approval/session rows.
- **Provision:** New → Database → PostgreSQL (rename `postgres`), or `railway add --plugin postgres`.
- **Wire:** `webapp/config/settings.py` reads **`DB_HOST/DB_NAME/DB_USER/DB_PASSWORD/DB_PORT`**. On django / celery-worker / celery-beat set:
  ```
  DB_HOST=${{postgres.RAILWAY_PRIVATE_DOMAIN}}
  DB_PORT=5432
  DB_NAME=${{postgres.PGDATABASE}}
  DB_USER=${{postgres.PGUSER}}
  DB_PASSWORD=${{postgres.PGPASSWORD}}
  ```
- **Enable PgBouncer** (transaction pooling) — django + worker + beat all hold connections.
- **Migrate data (one-time):**
  ```bash
  docker compose --profile full exec -T postgres pg_dump -U scraper -d scraper --no-owner --no-privileges -Fc > scraper.dump
  pg_restore --no-owner --no-privileges --dbname="$DATABASE_PUBLIC_URL" --on-error-stop scraper.dump
  ```

### 4.2 Redis — Railway managed plugin (Upstash)
- **Approach:** managed Redis. Codebase needs only `REDIS_URL` (broker + result backend + lock client in `services.py`).
- **Provision:** New → Database → Redis. Set on consumers:
  ```
  REDIS_URL=${{redis.REDIS_PRIVATE_URL}}     # rediss:// — keep the scheme verbatim
  ```
  Upstash exposes only DB 0 (code uses `/0`). Pin Redis + worker to the same region.
- **Persistence:** losing the volume is safe (broker/results/locks ephemeral). Drain workers before cutover.
- **Follow-up bug:** `webapp/scraper/views.py` health view reads `getattr(settings,"REDIS_URL",…)` but settings only defines `CELERY_BROKER_URL` → falsely reports Redis down. Fix: add `REDIS_URL = config("REDIS_URL",…)` as a setting.

### 4.3 ★ file-master ★ (the artifact store)
- **Dockerfile:** `file_master/Dockerfile` (tiny — `python:3.12-slim`, `fastapi`, `uvicorn`, `httpx`).
- **Start command:** `uvicorn file_master.app:app --host 0.0.0.0 --port 8002`.
- **Volume:** Railway Settings → Volumes → mount at `/data`. (In compose, bind `./shared-data:/data`.)
- **Port:** `8002`, **private only** (`file-master.railway.internal:8002`). No public domain.
- **Healthcheck:** Path `/health`, short grace (~10s — it boots instantly).
- **Resources:** smallest plan that fits the working set; size the **volume** for accumulated artifacts (a few GB + pruning).
- **Replicas = 1.** Singleton file store.

### 4.4 django (web)
- **Dockerfile:** `./Dockerfile` · **Start command:** Dockerfile `CMD` (`gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2`). **Never `runserver` in prod.**
- **Pre-Deploy:** `python manage.py migrate --noinput && python manage.py collectstatic --noinput` (wrap migrate in a retry loop — no `depends_on`).
- **Port:** auto from `EXPOSE 8000`. Public domain: Settings → Networking → Generate Domain.
- **Static files (gotcha):** no `whitenoise` in requirements; under `DEBUG=False` admin/dashboard CSS 404. Add `whitenoise` + `WhiteNoiseMiddleware` after `SecurityMiddleware`.
- **Healthcheck:** Path `/health/`.
- **Artifacts:** `FILE_MASTER_URL=http://file-master.railway.internal:8002`. Add a django view `/api/artifact?key=…` that proxies `GET` to the File Master (stream the bytes) so dashboard links resolve on django's own domain.

### 4.5 celery-worker
- **Dockerfile:** `./Dockerfile` · **Start command:** `celery -A config worker -l INFO --concurrency=2 -Ofair` (from `/app/webapp`).
- **Env:** `PYTHONPATH=/app`, `PROJECT_ROOT=/app`, DB+REDIS reference vars, `ZAI_*` secrets, `CODE_WRITER_MODEL=glm-5.2`, `BROWSER_SERVICE_URL=http://browser-service.railway.internal:8001`, `PLAYWRIGHT_MCP_URL=http://browser-service.railway.internal:8111/sse`, `FILE_MASTER_URL=http://file-master.railway.internal:8002`, `SECRET_KEY` (same as django), `CELERY_TASK_ACKS_LATE=true`.
- **Artifacts:** reads/writes all workspace+scrapers artifacts via the File Master. `run_scraper` POSTs `scraper_key` (not a path). `code_tester` reads the draft to `/tmp`, runs, writes results back.
- **No shared volume needed** — the worker uses the File Master + local `/tmp`. (An ephemeral `/tmp` is enough; nothing persistent required.)
- **Resources:** **8 GB / 4 vCPU** (LLM agent contexts are RAM-heavy). Keep `CELERY_WORKER_MAX_MEMORY_PER_CHILD=2621440`, `CELERY_WORKER_MAX_TASKS_PER_CHILD=10`.
- **Replicas = 1** initially. Healthcheck: command `celery -A config inspect ping -d celery@$(hostname) --timeout=10`, start_period 60s.

### 4.6 browser_service
- **Dockerfile:** `browser_service/Dockerfile` (root dir = repo root). Build is heavy (~8–12 min) — keep apt/pip/npm layers before `COPY` for cache.
- **Start command:** the Dockerfile `CMD` (uvicorn `browser_service.server:app` on :8001).
- **Artifacts via File Master:** `/scrape` takes `scraper_key`, does `artifacts.read(key)` → `/tmp/scraper_{uuid}.py` → runs → `artifacts.write(output_key, output_bytes)`. No shared disk with the worker.
- **Chrome `/dev/shm` — already fixed.** `browser_pool.py` passes `--disable-dev-shm-usage --no-sandbox --disable-setuid-sandbox --disable-gpu` on both persistent Chromes; `/probe`+`/navigate` ephemeral launches pass `--no-sandbox --disable-dev-shm-usage`. **Don't add `--single-process`** (crashes). Give several GB ephemeral disk for `/tmp`.
- **Drop Xvfb → headless.** Add `--headless=new` to the two persistent Chromes, gated on `DISPLAY` empty; set `DISPLAY=""` on Railway. CloakBrowser is already `headless=True`. Local compose keeps Xvfb.
- **Ports:** `8001` private; `8111`/`9222`/`9223` **private only**.
- **Resources:** **8–16 GB / 4+ vCPU**. Set `NAVIGATE_MAX_CONCURRENT=2`, `NAVIGATE_MAX_QUEUE=2` until sized.
- **Healthcheck:** Path `/health`, port `8001`, **grace ≥ 90s**.
- **Env:** `FILE_MASTER_URL=http://file-master.railway.internal:8002`, `PYTHONPATH=/app`, `PROJECT_ROOT=/app`.
- **Proxy env (secrets):** `PROXY_DATACENTER_*`, `PROXY_RESIDENTIAL_*`.

### 4.7 celery-beat (scheduler)
- **Dockerfile:** `./Dockerfile` · **Start command:** `celery -A config beat -l INFO`. **Drop `migrate`** (django owns schema).
- **Singleton:** replicas = 1, disable autoscale + sleep. Two beats = duplicate tasks.
- **No volume** — `django_celery_beat.schedulers:DatabaseScheduler` stores schedule in Postgres.
- Healthcheck: rely on exit→restart, or `pgrep -f "celery.*beat"`.

### 4.8 flower (optional)
- **Dockerfile:** `./Dockerfile` · **Start command:** `celery -A config flower --port=${PORT:-5555}`.
- **No public domain without auth** (unauthenticated; task kwargs can contain secrets). Keep private-only or add `--basic-auth`.
- Replicas = 1, smallest plan.

---

## 5. Environment-variable reference map

Reference vars auto-sync; mark keys/passwords as Railway **Secrets**.

| Variable | django | celery-worker | browser_service | file-master | celery-beat | flower | Value |
|---|---|---|---|---|---|---|---|
| `DB_HOST/PORT/NAME/USER/PASSWORD` | ✓ | ✓ | — | — | ✓ | ✓ | `${{postgres.*}}` |
| `REDIS_URL` | ✓ | ✓ | — | — | ✓ | ✓ | `${{redis.REDIS_PRIVATE_URL}}` |
| `DJANGO_SETTINGS_MODULE` | `config.settings` | `config.settings` | — | — | `config.settings` | `config.settings` | |
| `SECRET_KEY` | secret | secret | — | — | secret | secret | real random, **same across services** |
| `PYTHONPATH` | `/app` | `/app` | `/app` | — | `/app` | `/app` | |
| `PROJECT_ROOT` | `/app` | `/app` | `/app` | — | `/app` | `/app` | |
| `FILE_MASTER_URL` | ✓ | ✓ | ✓ | — | — | — | `http://file-master.railway.internal:8002` |
| `BROWSER_SERVICE_URL` | ✓ | ✓ | — | — | — | — | `http://browser-service.railway.internal:8001` |
| `PLAYWRIGHT_MCP_URL` | ✓ | ✓ | — | — | — | — | `http://browser-service.railway.internal:8111/sse` |
| `DEBUG` / `ALLOWED_HOSTS` | `False` / your domain | — | — | — | — | — | |
| `ZAI_API_KEY` / `ZAI_BASE_URL` / `ZAI_MAIN_MODEL` / `ZAI_SMALL_MODEL` / `CODE_WRITER_MODEL` | ✓ | ✓ | — | — | — | — | |
| `CELERY_TASK_ACKS_LATE` | — | `true` | — | — | — | — | |
| `CELERY_WORKER_MAX_MEMORY_PER_CHILD` / `CELERY_WORKER_MAX_TASKS_PER_CHILD` | — | `2621440` / `10` | — | — | — | — | |
| `DISPLAY` | — | — | `""` (→ headless) | — | — | — | |
| `NAVIGATE_MAX_CONCURRENT` / `NAVIGATE_MAX_QUEUE` | — | — | `2` / `2` | — | — | — | |
| `PROXY_DATACENTER_*` / `PROXY_RESIDENTIAL_*` | — | — | secrets | — | — | — | |

> **Service-name gotcha:** Railway private DNS is **lowercase hyphenated** — `file-master.railway.internal`, `browser-service.railway.internal` (underscored names won't resolve).

---

## 6. Deployment order

No `depends_on`; deploy in this order (retries handle races):

1. **Postgres** → healthy.
2. **Redis** → healthy.
3. **file-master** → healthy (`/health`).
4. **django** (Pre-Deploy: migrate + collectstatic).
5. **browser_service** (healthcheck ~90s grace).
6. **celery-worker** (`CELERY_TASK_ACKS_LATE=true`).
7. **celery-beat** (after django migrated).
8. **flower** (optional, last).

Smoke test: submit a job → worker writes scraper_draft to File Master → browser_service reads it, runs, writes output to File Master → django proxies the output to the dashboard.

---

## 7. Gotchas checklist

- [ ] **File Master first** — build `file_master/` + `src/artifacts.py`, refactor ~30 path call sites to `artifacts.*`. Verify on **local compose** (with the new `file-master` service) end-to-end before touching Railway.
- [ ] **`/scrape` takes `scraper_key`** — browser_service resolves via `artifacts.read()`, writes output via `artifacts.write()`.
- [ ] **Django proxy view** `/api/artifact?key=…` → File Master (users never hit FM directly).
- [ ] **Artifact pruning** — add `DELETE` endpoint + schedule cleanup so the volume doesn't grow unbounded.
- [ ] **Headless Chrome** (`--headless=new`, `DISPLAY=""`); `/dev/shm` already mitigated.
- [ ] **Static files** — add `whitenoise` (`DEBUG=False`).
- [ ] **`runserver` → `gunicorn`**; migrate via Pre-Deploy.
- [ ] **beat = 1 replica**, no autoscale/sleep.
- [ ] **flower auth** — no public domain without `--basic-auth`.
- [ ] **Postgres** — enable PgBouncer; metadata only (no blobs).
- [ ] **`CELERY_TASK_ACKS_LATE=true`**.
- [ ] **Service names hyphenated** (`file-master`, `browser-service`).
- [ ] **REDIS health-view bug** — add `REDIS_URL` setting.
- [ ] **Secrets** → Railway Secrets.
- [ ] **Region pin** — Redis + worker + browser_service + file-master same region.

---

## 8. Local dev — add the File Master, drop the shared bind mounts

Local compose now mirrors Railway: add a `file-master` service and point all services at it. This is a **local change** (you accepted it) — but it makes dev and prod architecturally identical.

```yaml
# add to docker-compose.yml services:
file-master:
  profiles: ["full"]
  build: ./file_master
  volumes:
    - ./shared-data:/data          # local artifact store
  environment:
    BROWSER_SERVICE_PORT: "8002"
  ports:
    - "8002:8002"
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8002/health"]
    interval: 10s
    retries: 3
```
And on `django` / `celery-worker` / `browser_service` set `FILE_MASTER_URL=http://file-master:8002` (instead of the `.:/app` bind mount for `scrapers/`+`workspace/`). The bind mounts for those two trees can be removed once all call sites use `artifacts.*`.

```bash
docker compose --profile full up --build        # local dev, now with file-master
```

Repo-side additions:
- `file_master/` (app.py + Dockerfile + requirements.txt).
- `src/artifacts.py` (HTTP client).
- The path→`artifacts.*` refactor across ~30 call sites.
- A Railway-only headless toggle in `browser_pool.py` (gated on `DISPLAY`).
- `docker-compose.yml`: add the `file-master` service + `FILE_MASTER_URL` env on consumers.
- This doc.

---

## 9. Suggested follow-up tickets (post-migration)

1. **`dj_database_url`** so consumers need only `DATABASE_URL`.
2. **Per-replica browser_service** sharded by `site_slug` (services already separate).
3. **Artifact retention** (auto-prune old File Master keys; size the volume with monitoring).
4. **File Master auth** (a shared token on the API) if the private network isn't trusted enough.
5. **Fix the Redis health-view** (`views.py`).
6. **Swap File Master backend to S3** (if ever desired) — only `file_master/app.py` changes; call sites untouched.
