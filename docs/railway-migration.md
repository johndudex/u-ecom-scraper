# Railway Migration Runbook (v2 — full rewrite)

> **v2 replaces v1 entirely.** v1 had errors that would break the deployment (headless-Chrome instruction kills browser_service; wrong file-master root dir; login-protected healthcheck; Pre-Deploy that fails on every redeploy) and an env-var matrix that was hard to follow. This version is a **click-by-click runbook**: do the steps in order, paste the blocks as given, and it works.
>
> Verified against: Railway docs (guides/docker-compose, variables, volumes, healthchecks, pre-deploy-command, networking, databases) + the repo at `file-master-artifacts` @ `1ce8ec6` (every env var below is grep-verified against the code that reads it).

---

## Where do YOU start? (already-running stacks)

**If your Railway project already runs the 8 services from the original
migration (Phases 1–10, done ~2026-08-18) — start at Phase 11.** That's
the only mandatory section; everything before it is the from-scratch
history you already lived. Phase 12 (WSS gateway) is optional-by-demand.

This deploy (branch `file-master-artifacts`) adds:
- migration 0033 (schema only — new tables + widened columns) — runs
  automatically on `migrate` in the start commands
- **Phase 11**: `celery-events` service (NEW — you create it) + a beat
  schedule entry (automatic on beat restart)
- **Phase 12**: `event-gateway` service (OPTIONAL — add when a partner
  wants websockets)
- the partner API itself (`/api/v1/*`), docs pages, admin surfaces —
  all live on the existing `django` service, no new config

One-time post-deploy task: the date recompute (Phase 11 §5) — a MANUAL
admin-button step; `migrate` does NOT repair the historical rows (0033
is schema-only).

## How to read this runbook

- You will create **9 Railway services** (10 with the optional event-gateway) in this exact order. Each phase = one service = one paste block.
- **Variables are pasted into each service's "Variables" tab.** Click the service → **Variables** tab → **Raw Editor** (or `+ New Variable`) → paste the whole block. Railway stages the change; click **Deploy** when prompted.
- Lines starting `#` inside blocks are comments — safe to paste, Railway ignores them.
- `${{service.VAR}}` is **Railway reference syntax** — paste the braces exactly. These resolve to values from the Postgres/Redis services you create in Phases 1–2, which is why those come first.
- **Secrets:** for any password/key, click the variable's ⋮ menu → **Seal** after pasting (values like `SECRET_KEY`, `ZAI_API_KEY`, `DB_PASSWORD`, `DJANGO_SUPERUSER_PASSWORD`, proxy creds).
- **API tokens (for programmatic audits):** a PROJECT token must be sent with the **`Project-Access-Token:` header** — NOT `Authorization: Bearer` (which the CLI/`me` path uses for account tokens). A project token + Bearer returns plain `Not Authorized` on everything and looks like a dead token (live failure #9). Introspect scope with: `query { projectToken { projectId environmentId } }`. Sealed values can't be read back even with a valid token — presence-check only.

### The service names matter — do not rename

Private networking resolves `<service-name>.railway.internal`. These blocks hardcode:

| Name it exactly | Why |
|---|---|
| `postgres` | the `${{postgres.*}}` references below match this name |
| `redis` | the `${{redis.*}}` reference matches |
| `file-master` | `FILE_MASTER_URL=http://file-master.railway.internal:8002` |
| `browser-service` | `BROWSER_SERVICE_URL=http://browser-service.railway.internal:8001` |
| `django`, `celery-worker`, `celery-beat`, `flower` | your own sanity |

Railway's DNS has no port-mapping layer — the port in the URL is the port the app actually listens on (8002/8001/8111). Don't change them.

---

## Phase 0 — Before you start

1. A Railway account on a **Hobby plan or higher** (Free = 0.5 GB RAM total, this stack needs ~4 GB+ for browser-service alone; Free also force-sleeps idle services → random 502s).
2. Your repo pushed to GitHub (branch `file-master-artifacts`).
3. On hand: your **z.ai API key**, and (optional) proxy credentials (Bright Data style: host/user/pass/port). Proxy note: `config/proxy.json` is excluded from all Docker images — **env vars are the only proxy path on Railway**.

### What Railway will NOT carry over from docker-compose (you do it manually here)

| compose feature | Railway equivalent (done in this runbook) |
|---|---|
| `depends_on` + health conditions | nothing — deploy order is manual (Phases are ordered), code already retries Redis/DB at boot |
| healthchecks | re-entered per service (Railway has only **path + timeout**, default 300s) |
| `restart: unless-stopped` | Railway restarts crashed containers by default — nothing to set |
| `mem_limit` | Settings → Resources per service (values given per phase) |
| `init: true` | no equivalent — harmless gap (browser_service has its own orphan killer) |
| `shm_size: 2g` | no equivalent — code already passes `--disable-dev-shm-usage` to its own Chromes; keep browser-service RAM ≥ 4 GB |
| compose `command:` overrides | Railway **Start Command** per service (worker/beat/flower need it; see phases) |

---

## Phase 1 — Project + Postgres

1. Railway dashboard → **New Project** → name it (e.g. `ecom-scraper`).
2. In the project, press **⌘/Ctrl+K** (or the **+ New** button) → **Database** → **PostgreSQL**.
3. **Rename the service to `postgres`** (click the service name). Keep the default plan but check it has **≥ 1 GB RAM** — the compose config pins postgres to 1 GB after two OOM-kills in production (checkpoint blobs + session-log writes are heavy). Upgrade the plugin plan if needed.
4. Wait for it to show **healthy/running**.
5. PgBouncer: optional. If you later enable it, also set `DB_USE_PGBOUNCER=True` on django/worker/beat. Day one: skip.

> You never read `DATABASE_URL` — the app reads `DB_HOST/DB_NAME/DB_USER/DB_PASSWORD/DB_PORT` individually. Railway's Postgres exposes all of them for referencing (that's what Phases 4/6/7 paste).

## Phase 2 — Redis

1. **⌘/Ctrl+K** → **Database** → **Redis**. Rename to `redis`.
2. Same region as everything else (check the region tag on each service as you create them — mixed regions = latency).
3. Done. The app reads `REDIS_URL=${{redis.REDIS_URL}}` — Railway's Redis plugin exposes exactly `REDIS_URL` (internal, TLS) and `REDIS_PUBLIC_URL`. ⚠️ There is **no `REDIS_PRIVATE_URL`** on Railway's Redis (that naming is Postgres-only) — a wrong reference name resolves to an **empty string silently**, and the app logs `Could not initialize Redis client for SSE: Redis URL must specify one of the following schemes`. When in doubt, type `${{redis.` in the value field and let Railway's autocomplete list the real names (live-verified failure #3).

## Phase 3 — Shared Variables (define once, use everywhere)

Project **Settings → Shared Variables** → select your **Production** environment → add each (then use the **Share** button to share them with django, celery-worker, celery-beat, flower):

```
PYTHONPATH=/app
DJANGO_SETTINGS_MODULE=config.settings
SECRET_KEY=<invent a long random string — seal it>
FILE_MASTER_URL=http://file-master.railway.internal:8002
BROWSER_SERVICE_URL=http://browser-service.railway.internal:8001
```

Why these five: identical on every Django-family service. Sharing them once is why the per-service blocks below stay short.
*(Shared variables are raw text — `${{...}}` references do NOT resolve inside them, which is why DB vars are pasted per-service instead.)*

---

## Phase 4 — file-master (artifact store)

1. **+ New** → **GitHub Repo** → pick your repo + branch.
2. **Service Settings → Build:**
   - **Root Directory:** `/file_master`  ← ⚠️ **critical**. The Dockerfile does `COPY app.py` from the build context; with root dir `/` the build fails immediately.
   - Dockerfile: default detection finds `Dockerfile` inside `/file_master`.
3. **Variables:** add exactly one — `PORT=8002`. Railway's healthcheck probes the port named in `PORT`; the Dockerfile hardcodes uvicorn on 8002 and never reads `PORT`, so without this variable the check hits a dead port and fails the deploy with "service unavailable" for the full 5-minute retry window. ✅ **Live-verified on the first real migration attempt** (2026-08-18: fixed the failure, healthcheck green after adding it). (`FILE_MASTER_ROOT` defaults to `/data` — already matches the volume; `FILE_MASTER_PORT` is a no-op — the CMD hardcodes 8002.)
4. **Volume:** right-click the service on the canvas (or ⌘/Ctrl+K → Volume) → attach to this service → **Mount Path `/data`**. This holds every published scraper + output JSON — size it ≥ 5 GB.
5. **Settings:** replicas **1** (it's a singleton with a disk). **Deploy → Enable Serverless: OFF** (it must never sleep).
6. Healthcheck (Settings → Healthcheck): path `/health`. It returns `{"ok": true}` in ~2s.
7. **Deploy.** Verify: service logs show uvicorn on :8002, health green. No public domain — nothing outside the private network should reach it.

## Phase 5 — django (web UI)

1. **+ New** → GitHub Repo → same repo/branch.
2. **Build:** Root Directory `/` (repo root), Dockerfile auto-detected. The image's CMD is already production-correct: `gunicorn ... :8000`. **Do not** copy compose's dev command (migrate/runserver).
3. **FIRST — confirm the shared variables arrived.** Open the Variables tab: you must SEE all five (`PYTHONPATH`, `DJANGO_SETTINGS_MODULE`, `SECRET_KEY`, `FILE_MASTER_URL`, `BROWSER_SERVICE_URL`). Creating a shared variable is not enough — you must have pressed **Share** and checked this service. ⚠️ **If `PYTHONPATH` is missing, the Pre-Deploy below dies instantly with `ModuleNotFoundError: No module named 'src'`** (live-verified on the first real migration). Missing? → Project Settings → Shared Variables → Share to this service — or just add `PYTHONPATH=/app` as a raw variable here.
4. **Variables tab → Raw Editor → paste:**

```
# ── from Shared Variables (use the "Share" button instead of re-typing) ──
# PYTHONPATH, DJANGO_SETTINGS_MODULE, SECRET_KEY, FILE_MASTER_URL, BROWSER_SERVICE_URL

# ── database (references resolve because 'postgres' exists) ──
DB_HOST=${{postgres.PGHOST}}
DB_PORT=${{postgres.PGPORT}}
DB_NAME=${{postgres.PGDATABASE}}
DB_USER=${{postgres.PGUSER}}
DB_PASSWORD=${{postgres.PGPASSWORD}}          # ⋮ → Seal after saving

# ── celery/redis ──
REDIS_URL=${{redis.REDIS_URL}}

# ── superuser for the Pre-Deploy below ──
DJANGO_SUPERUSER_PASSWORD=<invent>            # ⋮ → Seal

# ── healthcheck port (gunicorn binds 8000; see note) ──
PORT=8000
```

   Why each (one-liners):
   - `PYTHONPATH=/app` — without it, gunicorn crashes on boot at `from src.schema_validation import ...` (same class of failure that killed celery-beat in prod).
   - `DB_*` — settings.py reads these five **components**; pasting `DATABASE_URL` instead does nothing.
   - `REDIS_URL` — Celery broker + the live-log SSE pubsub.
   - `FILE_MASTER_URL` / `BROWSER_SERVICE_URL` — the UI's artifact reads and health dashboard. Code defaults use underscored hostnames that don't resolve on Railway; these overrides are **required**.

5. **Settings → Deploy → Pre-Deploy Command** (runs between build and start, on every deploy):

```
python manage.py migrate --noinput && (python manage.py createsuperuser --noinput --username admin --email admin@example.com || true)
```

   ⚠️ The `|| true` is load-bearing: `createsuperuser --noinput` exits non-zero once `admin` exists, and a failing Pre-Deploy **blocks every future deploy**. (v1 of this doc omitted it — that's a redeploy brick.)
   Note: Pre-Deploy runs in a **separate container** — it can reach Postgres but its **filesystem is discarded**. That's fine for migrate (external DB) — but it means **collectstatic can NOT run here** (live failure #5: static files vanished → `No directory at: /app/webapp/staticfiles/` at boot → unstyled pages). Collectstatic runs at **build time inside the Dockerfile** now (verified: 127 files land in the image).

6. **Deploy.** First boot: migrate + collectstatic + superuser run, then gunicorn on :8000.
7. **Networking:** Settings → Networking → **Generate Domain**. If asked for a port, enter `8000` (gunicorn binds it, hard-coded).
8. **Now add the two domain vars** (new Variables):

```
ALLOWED_HOSTS=<your-app>.up.railway.app,healthcheck.railway.app
CSRF_TRUSTED_ORIGINS=https://<your-app>.up.railway.app
```

   - `CSRF_TRUSTED_ORIGINS` is the **#1 silent killer**: without it every form POST (login, job submit, approvals) returns 403. Django 5 does not derive it from ALLOWED_HOSTS.
   - Redeploys after this are automatic.

9. **Healthcheck:** path `/api/health/raw` — **no trailing slash** (`/api/health/raw/` 404s). ⚠️ Not `/health/` (login-protected → 302 → failed deploy).
   **Host-header trap (live failure #4):** Railway probes with the hostname `healthcheck.railway.app`. Once you set `ALLOWED_HOSTS=<your-domain>` (step 8), that probe Host is rejected → Django 400 → healthcheck "service unavailable" even though the app is fine. Fix: include the probe host —
   `ALLOWED_HOSTS=<your-app>.up.railway.app,healthcheck.railway.app`
   **Values must be UNQUOTED** (live failure #6): the Raw Editor stores what you paste verbatim — `PORT="8000"` keeps the quote characters, and a quoted PORT breaks the probe URL at the connection level ("service unavailable" with a perfectly healthy app).
   **Escape hatch (live-verified #7):** a failing healthcheck BLOCKS the deployment from going active — the domain 404s ("Application not found") even though the app serves fine. If stuck: Settings → Healthcheck → **clear the path** → redeploy → deployment activates, domain routes. Re-add later after verifying `curl https://<app>.up.railway.app/api/health/raw` → ok. A healthcheck is protection, not a requirement.
9. Serverless: **OFF**. Resources: 1 GB is fine.

✅ **Checkpoint:** open `https://<app>.up.railway.app/api/health/raw` → `ok`. `/accounts/login/` renders with CSS (proves whitenoise+collectstatic).

## Phase 6 — browser-service (Chrome)

1. **+ New** → GitHub Repo → same repo/branch.
2. **Build:** Root Directory `/` (repo root — the Dockerfile lives at `browser_service/Dockerfile`; set **Dockerfile Path** to `browser_service/Dockerfile` in build settings, or via `RAILWAY_DOCKERFILE_PATH`). First build is slow (Chrome + node + playwright downloads) — that's normal.
3. **Variables → paste:**

```
# ⚠️ REQUIRED — see notes; getting these wrong is the #1 way this service dies
PORT=8001                                     # same healthcheck-port rule as file-master (live-verified)
DISPLAY=:98
MCP_CDP_PORT=19222
SCRAPER_CDP_PORT=19223

# ── proxy (the ONLY proxy path on Railway — proxy.json is dockerignored) ──
# leave all unset to run proxy-less; set both groups if you have creds
# PROXY_DATACENTER_HOST=...
# PROXY_DATACENTER_PORT=22225
# PROXY_DATACENTER_USER=...        # ⋮ → Seal
# PROXY_DATACENTER_PASS=...        # ⋮ → Seal
# PROXY_RESIDENTIAL_HOST=...
# PROXY_RESIDENTIAL_PORT=22225
# PROXY_RESIDENTIAL_USER=...       # ⋮ → Seal
# PROXY_RESIDENTIAL_PASS=...       # ⋮ → Seal
```

   Why:
   - **`DISPLAY=:98` — do NOT set it to empty.** v1 said `DISPLAY=""` for headless; that's a documented-looking trap: with DISPLAY empty the code skips Xvfb, both Chrome starters refuse to launch without Xvfb, `/health` returns 503 degraded forever. `:98` = virtual framebuffer, Chrome runs headed-against-Xvfb exactly like compose.
   - **`MCP_CDP_PORT`/`SCRAPER_CDP_PORT` are required** even though they look internal: without them the code has a default mismatch (the MCP process points at `:19222` while Chrome binds `:9222`) and browser automation silently breaks. These values make everything self-consistent.
4. **No start-command override** — the Dockerfile CMD handles stale-lock cleanup + uvicorn :8001. Keep it.
5. **Healthcheck:** path `/health` (returns 503 while degraded — that's correct behavior; Chrome+MCP take ~30–60s to boot).
6. Serverless: **OFF** (sleeping Chrome = first-scrape 502s). Resources: **4 GB RAM / 2+ vCPU** minimum. Replicas: 1.
7. No public domain. No volume (stateless — scraper source arrives in the POST body).

✅ **Checkpoint:** logs show `Browser pool ready: Xvfb=:98, MCP Chrome=:19222, Scraper Chrome=:19223` and `Playwright MCP started ... 0.0.0.0:8111`.

## Phase 7 — celery-worker (the pipeline)

1. **+ New** → GitHub Repo → same repo/branch. Root Directory `/`, Dockerfile auto.
2. **Settings → Deploy → Start Command** (required — the image CMD is gunicorn, wrong for a worker):

```
celery -A config worker -l INFO --concurrency=2 -Ofair
```

3. **FIRST — confirm the shared variables arrived** (same gate as Phase 5): the Variables tab must show `PYTHONPATH`, `DJANGO_SETTINGS_MODULE`, `SECRET_KEY`, `FILE_MASTER_URL`, `BROWSER_SERVICE_URL`. ⚠️ Without `PYTHONPATH` the worker dies at boot inside the celery CLI (`app.conf` load → `urls.py` → `ModuleNotFoundError: No module named 'src'` — live failure #8). Share them to this service or add `PYTHONPATH=/app` raw.
4. **Variables → paste** (plus the 5 shared ones via Share):

```
# ── database ──
DB_HOST=${{postgres.PGHOST}}
DB_PORT=${{postgres.PGPORT}}
DB_NAME=${{postgres.PGDATABASE}}
DB_USER=${{postgres.PGUSER}}
DB_PASSWORD=${{postgres.PGPASSWORD}}          # ⋮ → Seal

# ── redis ──
REDIS_URL=${{redis.REDIS_URL}}

# ── LLM ──
ZAI_API_KEY=<your z.ai key>                   # ⋮ → Seal
ZAI_BASE_URL=https://api.z.ai/api/coding/paas/v4/
ZAI_MAIN_MODEL=glm-5-turbo
ZAI_SMALL_MODEL=glm-5-turbo
CODE_WRITER_MODEL=glm-5.2
# LiteLLM proxy (optional — flip CODE_WRITER_MODEL to litellm/standardcompute
# to route code_writer through the proxy; see docs/codewriter-litellm-plan.md).
# Rotate the key BEFORE sealing it (rotation after sealing = two sealed-var
# updates + a window where local and Railway use different keys).
LITELLM_BASE_URL=https://llm.johnjf.xyz/v1
LITELLM_API_KEY=<your litellm key>            # ⋮ → Seal
# CODE_WRITER_MODEL=litellm/standardcompute

# ── browser automation endpoint for the agents ──
PLAYWRIGHT_MCP_URL=http://browser-service.railway.internal:8111/sse
```

   Why:
   - DB vars: the worker writes jobs/steps/approvals **and** the LangGraph checkpoint tables (its own connection string built from the same settings).
   - `FILE_MASTER_URL` (shared): the worker is the **only writer** to the artifact store.
   - `PLAYWRIGHT_MCP_URL`: the agents' browser client. Default is underscored → dead on Railway; this override is required.
   - Everything LLM-tuning (`LLM_CIRCUIT_BREAKER_*`, `EXECUTION_TIMEOUT`, …) has sane code defaults — leave unset until you need them. (Don't set `CELERY_TASK_ACKS_LATE=true`: the settings file explicitly marks it unsafe until regression-verified.)
4. **Volume — full instructions (read the permission trap first):**
   **Why:** `/app/workspace` is the pipeline's scratch space (`workspace/{site_slug}/` — drafts, analysis JSONs, discovery checkpoints). Without a volume it's **ephemeral**: a worker restart/redeploy mid-job wipes it, so an in-flight job loses its files (completed artifacts are safe in file-master; resume state is in Postgres). With a volume, in-flight jobs survive restarts.

   **How to attach (Railway UI):**
   1. Click the **celery-worker** service on the project canvas.
   2. Open the **Settings** tab (or press ⌘/Ctrl+K → "Volume" — either path works).
   3. Find **Volumes** → **Add Volume** (or right-click the service → Add Volume).
   4. **Mount Path:** enter exactly `/app/workspace` — leading slash, lowercase, no trailing slash. This path must match where the code writes; a typo means the volume silently mounts where nothing reads it.
   5. **Size:** ≥ **5 GB** (a big site's discovery checkpoint + drafts can run 1–2 GB per job; the volume also carries old job leftovers until pruned).
   6. Save/deploy when prompted. NOTE: attaching a volume to a running service causes a brief restart (Railway must remount).

   **⚠️ THE PERMISSION TRAP (this WILL bite if skipped):** Railway mounts volumes as **`root`**, but this image runs as user **`scraper`** (uid 1000). The moment the volume is attached, every write under `/app/workspace` fails with `PermissionError`/`EACCES` — the job dies in `setup_workspace`/`code_writer` with mkdir/open errors, and the cleanup agent burns tool calls trying `sudo` (not installed). **The fix — do BOTH of these together:**
   - Add the service variable **`RAILWAY_RUN_UID=0`** (Variables tab → New Variable). This is Railway's documented escape hatch: it runs the container as root, so root-owned volume writes succeed. The app itself doesn't care about uid.
   - Redeploy after adding it (variable changes stage a deploy — click through it).

   Alternative if you dislike running as root: keep no volume (current working state — workspace is ephemeral), or chown the mount path in a custom start command (`sh -c "chown -R scraper:scraper /app/workspace && exec celery -A config worker ..."` — requires root anyway via RAILWAY_RUN_UID, so the simple form above is preferred).

5. Serverless: **OFF** (a worker polling Redis never idles anyway, but be explicit). Resources: **4 GB RAM / 2 vCPU** (compose parity 3 GB; the worker self-recycles at 2.5 GB). Replicas: **1** — it shares one Chrome with the world.

✅ **Checkpoint:** logs show `celery@... ready`, no `ModuleNotFoundError` (that error = missing `PYTHONPATH` shared var).

## Phase 8 — celery-beat (scheduler)

1. **+ New** → GitHub Repo → same repo/branch. Root Directory `/` (repo root), Dockerfile auto-detected — same build config as django/worker.
2. **Settings → Deploy → Start Command** (required — the image CMD is gunicorn, wrong for a scheduler):

```
celery -A config beat -l INFO
```

   ⚠️ **No `migrate` prefix** — compose has one, but on Railway migrations are django's Pre-Deploy job (exactly once, from one service). Beat crash-loops with DB errors until django's first deploy has created the `django_celery_beat` tables — **so Phase 5 must be green before this service can stay up.** A crash-looping beat right after creation is expected in that case; it self-heals once the tables exist.
3. **FIRST — confirm the shared variables arrived** (same gate as Phase 5): the Variables tab must show all five — `PYTHONPATH`, `DJANGO_SETTINGS_MODULE`, `SECRET_KEY`, `FILE_MASTER_URL`, `BROWSER_SERVICE_URL`. ⚠️ Without `PYTHONPATH`, beat dies at boot inside the celery CLI (`app.conf` load → `urls.py` → `ModuleNotFoundError: No module named 'src'` — the identical live failure the worker hit, #8). Missing? → Project Settings → Shared Variables → **Share** each to this service (or add `PYTHONPATH=/app` raw).
4. **Variables tab → Raw Editor → paste** (on TOP of the five shared ones):

```
# ── database: WHY — beat's DatabaseScheduler reads/writes its schedule
#    (django_celery_beat_* tables) in Postgres; same 5 components as django
#    (settings.py never parses DATABASE_URL) ──
DB_HOST=${{postgres.PGHOST}}
DB_PORT=${{postgres.PGPORT}}
DB_NAME=${{postgres.PGDATABASE}}
DB_USER=${{postgres.PGUSER}}
DB_PASSWORD=${{postgres.PGPASSWORD}}          # ⋮ → Seal after saving

# ── message broker: WHY — beat dispatches the scheduled tasks onto the same
#    Redis queue the worker consumes; rediss:// TLS handled by celery ──
REDIS_URL=${{redis.REDIS_URL}}
```

   **That is the complete list.** Deliberately NO `ZAI_*` (beat never calls an LLM), NO `FILE_MASTER_URL` usage (it never touches artifacts — it's shared but inert here), NO browser/proxy vars (it never scrapes). If you paste extra vars from the worker block, they're harmless but noise.
5. **Settings:** Replicas **1** (two beats = every scheduled task fires twice — the watchdogs would auto-fail jobs in duplicate), autoscaling **off**, Serverless/sleep **off** (a sleeping scheduler silently stops all watchdogs).
6. **Heads-up — what beat will start doing immediately:** it runs FOUR schedules from settings.py: three every 5 min — `cleanup-stuck-jobs` (auto-FAILs jobs stuck >30 min — with `[EXEC-ALIVE]` rows counting as liveness, so long executions are safe), `stuck-approved-watchdog` (re-dispatches approved-but-stuck approvals), and `schedule-next-site` (auto-queues NEW jobs for any Site with stored URL lists — dormant on a fresh DB) — plus **`dispatch-pending-callbacks` every 30s** (partner-event delivery, Phase 11). **Ordering matters:** that 30s entry enqueues to the `events` queue, which only the `celery-events` service (Phase 11) consumes — deploy celery-events BEFORE or immediately after beat restarts, or tasks pile up unconsumed in Redis (harmless but invisible).

✅ **Checkpoint:** beat logs show `DatabaseScheduler: Schedule changed` then beat entries firing (`Scheduler: Sending due task cleanup-stuck-jobs ...`) every 5 minutes, with no `ModuleNotFoundError` and no DB connection errors after the first minute.

## Phase 9 — flower (optional — the Celery task dashboard)

Flower shows running/completed scrape tasks, retries, and per-task logs in a web UI. Skip it entirely if you don't need it — nothing else depends on it.

1. **+ New** → GitHub Repo → same repo/branch. Root Directory `/`, Dockerfile auto-detected (same image as the other celery services).
2. **Settings → Deploy → Start Command** (required — image CMD is gunicorn):

```
celery -A config flower --port=5555
```

   Flower binds :5555 (hardcoded in the command above).
3. **FIRST — confirm the shared variables arrived** (same gate as every celery service): all five visible on the Variables tab. `PYTHONPATH` matters here for the same import-time reason.
4. **Variables tab → Raw Editor → paste** (plus the five shared):

```
# ── broker + result backend: WHY — flower watches the Redis queue the
#    worker consumes and reads task results from the same backend ──
REDIS_URL=${{redis.REDIS_URL}}

# ── database: WHY — importing the celery app pulls Django settings
#    (config/celery.py → django.conf.settings), which touches the DB config
#    at import time; without these the import can fail even though flower
#    itself never queries Postgres ──
DB_HOST=${{postgres.PGHOST}}
DB_PORT=${{postgres.PGPORT}}
DB_NAME=${{postgres.PGDATABASE}}
DB_USER=${{postgres.PGUSER}}
DB_PASSWORD=${{postgres.PGPASSWORD}}          # ⋮ → Seal after saving
```

5. **Networking — keep it PRIVATE (no public domain):** the flower UI has NO authentication — anyone with the URL sees your task names, job URLs, and can cancel tasks. Two safe options:
   - **Private only (simplest):** generate no domain; reach it from your machine via `railway run` / an SSH tunnel when needed.
   - **Public with a password:** change the Start Command to `celery -A config flower --port=5555 --basic-auth=<user>:<pass>` and ONLY THEN generate a public domain. Pick a real password — it's basic-auth over the wire.
6. Serverless/sleep: **OFF** if you want it always watchable (it costs ~nothing idle). Replicas 1.

✅ **Checkpoint:** logs show flower serving on :5555; opening it (privately) lists the `celery-worker` node and any tasks the moment a job runs.

---

## Phase 10 — Verify everything

**1. Env references actually resolved.** Railway renders unresolved `${{...}}` as **empty strings — silently**. For each of django/worker/beat: service → Variables tab → confirm every `${{postgres.*}}`/`${{redis.*}}` row shows a resolved value (or run `railway variables` via CLI). Empty `DB_HOST` = the reference's service name doesn't match (typo'd/renamed `postgres`).

**2. Unauthenticated checks:**

```bash
curl -fsS https://<app>.up.railway.app/api/health/raw    # → ok
curl -fsI https://<app>.up.railway.app/admin/login/      # → 200
```

**3. Log in** (admin + the `DJANGO_SUPERUSER_PASSWORD` you set) → open **`/health/`** → all six tiles green (Django, PostgreSQL, Redis, Celery Worker, Celery Beat, Browser Service).

**4. Submit a real job:** `/intake/` → "I have the exact list" with 2–3 product URLs (use a scrapable site, e.g. books.toscrape.com product pages) → submit → job page → watch the SSE logs. A healthy run shows, in order: worker picks up the task → browser-service logs `Scraper run attempt 1/N` → file-master logs `PUT /artifacts/scrapers/<slug>/...` (scraper.py, output_*.json, analysis/*) → job `completed` with a non-zero item count.

**5. Common failure → cause → fix:**

| Symptom | Cause | Fix |
|---|---|---|
| every POST 403s | missing `CSRF_TRUSTED_ORIGINS` | Phase 5 step 7 |
| login page unstyled | static files missing | Dockerfile ≥ this branch's build-time collectstatic; redeploy |
| any Django-family service (incl. django's Pre-Deploy) dies: `ModuleNotFoundError: No module named 'src'` | shared `PYTHONPATH` not *shared* to this service | Phase 5 step 3 gate; Share it or add raw |
| browser-service `/health` 503 + log `Skipping MCP Chrome — Xvfb not running` | someone set `DISPLAY=""` | set `DISPLAY=:98` |
| browser automation connects to nothing | missing `MCP_CDP_PORT`/`SCRAPER_CDP_PORT` | Phase 6 block |
| job pages error on artifacts | `FILE_MASTER_URL` unset/wrong on django or worker | Phase 3 |
| django deploy blocked after first success | Pre-Deploy `createsuperuser` failing | ensure `|| true` in the command |
| intermittent 502s after idle | Serverless/sleep on | off on all services |
| file-master build fails at `COPY app.py` | Root Directory not `/file_master` | Phase 4 step 2 |
| healthcheck "service unavailable" full 5m | app port ≠ Railway's `PORT` | add `PORT=<app-port>` (8002 FM / 8001 browser / 8000 django) |
| django healthcheck fails but app serves fine | probe Host `healthcheck.railway.app` rejected by `ALLOWED_HOSTS` | append `,healthcheck.railway.app` to ALLOWED_HOSTS |
| healthcheck fails + domain 404s "Application not found" | failed healthcheck blocks deployment activation | escape hatch: clear the healthcheck path, redeploy, verify via curl |
| healthcheck "service unavailable", all config verified | quoted value (`PORT="8000"`) breaks probe URL | retype values unquoted in Raw Editor |
| agent tools: `mkdir scrapers: Permission denied` / `sudo: not found` | image never created `/app/scrapers` (root-owned `/app` + uid-1000 worker) | fixed in Dockerfile `754bc6c` — dir pre-created + chowned; redeploy worker |
| healthcheck 404 | trailing slash in the path field | use `/api/health/raw` exactly |

---

## Appendix — quick reference

- **Deployment order if you ever rebuild from scratch:** postgres → redis → (shared vars) → file-master → django → browser-service → celery-worker → **celery-events** (before beat — it consumes the 30s `events` queue) → celery-beat → flower. (`event-gateway` last, optional — Phase 12.)
- **All env vars actually read by the code, with defaults:** the tunables not in the paste blocks (`LLM_*` retry/circuit/truncation knobs, `EXECUTION_TIMEOUT=3600`, `EXECUTION_STALL_TIMEOUT=300`, `SCRAPER_HTTP_TIMEOUT=7200`, `PROBE_TIMEOUT=180`, `NAVIGATE_MAX_CONCURRENT=3`, `NAVIGATE_MAX_QUEUE=4`, `XVFB_RESOLUTION=1920x1080x24`, `STARTUP_TIMEOUT=45`, proxy vars) all have working defaults — set only when tuning. (`CELERY_TASK_SOFT/TIME_LIMIT` are **not** env-readable — hardcoded 7200/7560; ignore v1's advice about them.) Partner-API additions, all defaulted: `PARTNER_STREAM_BUDGET=1` (concurrent SSE streams before 503), `PARTNER_STREAM_DEADLINE=3600` (max SSE stream seconds), `OUTPUT_CACHE_FILES=4` / `OUTPUT_CACHE_BYTES=134217728` (output page cache bounds on django).
- **Postgres sizing:** ≥ 1 GB effective (compose pins `mem_limit: 1g` after production OOM incidents).
- **Railway docs used:** guides/docker-compose, /variables, /variables/reference, /volumes, /deploy/healthchecks, /deployments/pre-deploy-command, /networking/private-networking, /networking/public-networking, /databases/postgresql, /databases/redis, /deployments/scaling + restart-policy + serverless.

## Phase 11 — Partner API deploy (events worker + beat entry)

The partner API (slices 1a + 1b, commits 80799db..948ccc7) adds one
service and one beat entry. Deploy order matters — beat enqueues to the
`events` queue every 30s; a missing worker means invisible Redis buildup
(the same class of failure as a beat without tables).

### 1. New service: `celery-events`

Duplicate the `celery-worker` service in Railway, change ONLY:

| Field | Value |
|---|---|
| Name | `celery-events` |
| Start Command | `sh -c "python manage.py migrate --noinput && celery -A config worker -l INFO -Q events -Ofair --concurrency=4"` |
| Memory | 512 MB (delivery is I/O, not LLM) |

`migrate` first: Railway starts services in parallel; a worker without
the 0033 tables (ApiKey/JobCallback/EventOutbox) crash-loops. Same env
vars as celery-worker — no new secrets.

### 2. Beat picks up the schedule automatically

`CELERY_BEAT_SCHEDULE` (settings.py) now carries
`dispatch-pending-callbacks` @30s. django_celery_beat's DatabaseScheduler
reads it from code on beat restart — no DB rows to add. Restart
`celery-beat` after this deploy.

### 3. Routes are code-side

`CELERY_TASK_ROUTES` sends `deliver_callback` +
`dispatch_pending_callbacks` to `events`. The scrape worker does NOT
listen on `events` and celery-events does NOT listen on `celery` (the
default) — a hung partner endpoint can never touch scrape capacity.

### 4. Post-deploy checks (2 minutes)

**Web UI (no CLI needed):**

| Check | Where in the Railway dashboard | What you want to see |
|---|---|---|
| a) events worker registered the tasks | `celery-events` → **Logs** | celery banner on boot; no `KeyError: ...deliver_callback` anywhere. First delivery shows `Received task: scraper.events.dispatcher.deliver_callback` |
| b) beat's entry is live | `celery-beat` → **Logs** | `Scheduler: Sending due task dispatch-pending-callbacks` every ~30s |
| c) first sweep ran | `celery-events` → **Logs** | the worker booted with NO `KeyError` and NO crash-looping. (The `events sweep: dispatched=...` line only prints when something is due — on a fresh deploy there are zero partner jobs (`created_via="api"` gates emission), so silence + a healthy container IS the pass signal. After your first API job, the line appears every 30s.) |

**CLI alternative (if you have `railway` installed):**

```bash
railway ssh -s celery-events -- celery -A config inspect registered | grep deliver_callback
railway ssh -s celery-beat -- celery -A config inspect scheduled | grep dispatch_pending
railway logs celery-events --tail 50 | grep "events sweep"
```

### 5. The production data repair (recompute date reliability)

The a66e33f parse bug corrupted JobListing dates on Railway's DB too
(every row since 2026-07-22 marked unreliable). The repair ships as an
**admin button** — no shell, no start-command edits:

1. Log into Django admin (`/admin/`) as the superuser.
2. Go to **Job Listings** (`/admin/scraper/joblisting/`).
3. Click **"Preview date-reliability recompute"** (top toolbar) — a green
   message shows DRY RUN counts (expect ~6k scanned, ~1.3k would-fix on
   data shaped like the local DB; your numbers will differ).
4. Happy with the counts? Click **"APPLY recompute (--write)"** — the
   same message now reports the applied counts. Done. (Idempotent; the
   preview button is always safe to click.)

### 6. Rolling out partner traffic

1. Create the partner's service account + key:
   `python manage.py create_api_key --user <partner>` (prints the raw key
   ONCE). Needs a shell — if you're web-UI-only, the same admin-button
   pattern can be added on request; until then, ask whoever has CLI
   access, or run it once via a temporary start-command prefix.
   `/admin/scraper/apikey/` shows existing keys (prefix, active, last
   used) but cannot mint.
2. First partner job validates the whole chain end-to-end: create →
   pipeline → outbox rows → sweep → HMAC POST → their 200.
3. Watch `callback.delivered_count` on
   `GET /api/v1/jobs/{id}/callback` — the partner-visible health signal
   (also visible in `/admin/scraper/jobcallback/`).

### Rollback

The events worker is additive: stopping it freezes delivery (rows PENDING
— they queue, never drop) while REST/SSE keep working. `git revert` the
slice commits and redeploy; the 0033 migration is additive (new tables +
nullable columns) and safe to leave in place.

## Phase 12 — Event gateway (WSS, optional)

The Phase-2 WSS gateway (`event_gateway/`) gives partners one multiplexed
websocket per client (subscribe/unsubscribe by job_id, state snapshots,
live event fan-out, ≤25s heartbeats) instead of per-job SSE streams.
**Skip this phase unless a partner asks for websockets** — callbacks
(Phase 11) + SSE (on django) already cover partner push; this adds a 10th
service to monitor for a transport nobody is using yet.

Built and verified locally: 17 unit tests + 14 consecutive full e2e runs
(9 protocol behaviors each) against the compose service.

### 1. Create the service (same pattern as every phase)

1. **+ New** → GitHub Repo → same repo/branch. Root Directory `/`
   (repo root) — Railway auto-detects `event_gateway/Dockerfile` ONLY if
   you set **Settings → Build → Dockerfile Path** to
   `event_gateway/Dockerfile` (the repo root also has the main Dockerfile;
   Railway picks the root one by default — this is the one setting people
   miss).
2. **Settings → Deploy → Start Command**: leave EMPTY — the image CMD is
   already `uvicorn app:app --host 0.0.0.0 --port 8100`.
3. **Variables** (paste block — same DB/Redis as django, plus the two
   gateway-specific ones):

```
# ── from Shared Variables (use the "Share" button — same set as django) ──
# DJANGO_SETTINGS_MODULE, SECRET_KEY

# ── database (references resolve because 'postgres' exists) ──
DB_HOST=${{postgres.PGHOST}}
DB_PORT=${{postgres.PGPORT}}
DB_NAME=${{postgres.PGDATABASE}}
DB_USER=${{postgres.PGUSER}}
DB_PASSWORD=${{postgres.PGPASSWORD}}          # ⋮ → Seal after saving

# ── redis ──
REDIS_URL=${{redis.REDIS_URL}}

# ── the two gateway-specific ones ──
PYTHONPATH=/app:/app/webapp
PORT=8100
```

Two traps, both bit during local bring-up:
- **`PYTHONPATH` must be `/app:/app/webapp`** — the gateway imports
  `config.settings` and `scraper.*`; without the second entry it dies at
  boot with `ModuleNotFoundError: No module named 'config.settings'`.
  Railway's variables OVERRIDE the Dockerfile's ENV — setting just
  `/app` breaks it.
- **`PORT=8100`** — same trap as file-master's Phase 4: Railway probes
  the port named in `PORT`; the image hardcodes 8100. Without the
  variable the healthcheck hits a dead port and the deploy fails after
  the full retry window.

4. **Settings → Resources**: 256 MB (delivery is I/O, not LLM).
5. **Settings → Healthcheck**: path `/health`, leave the timeout default
   (the endpoint is a static JSON blob — fast).

### 2. Networking

**Settings → Networking → Generate Domain** only if a partner needs a
PUBLIC websocket endpoint — then note two things:
- Railway terminates TLS at its edge; the client URL becomes
  `wss://<your-gateway-domain>/ws/v1/jobs`.
- A public gateway is an internet-reachable auth surface. The auth is
  sound (single-use tokens / full key state machine), but if you'd
  rather not expose it, leave the domain off and use Railway's private
  networking from a partner-facing proxy later — the service works
  either way; nothing about the app requires a public URL.

### 3. Post-deploy check (web UI, 1 minute)

1. `event-gateway` → **Logs**: you want `Uvicorn running on
   http://0.0.0.0:8100` and NO `ModuleNotFoundError` (that's the
   PYTHONPATH trap). Healthcheck flips green within a minute.
2. If you generated a domain: open `https://<domain>/health` in your
   browser → `{"status":"ok","service":"event-gateway",...}`.
3. End-to-end (needs one valid API key): in any browser console on your
   django domain, mint a token and connect —

```js
const r = await fetch('/api/v1/ws-token', {method:'POST',
  headers:{'X-API-Key':'<pk_...>'}});
const {token} = await r.json();
const ws = new WebSocket(
  `wss://<gateway-domain>/ws/v1/jobs?token=${token}`);
ws.onopen = () => ws.send(JSON.stringify(
  {op:'subscribe', data:{job_id:<a real job id>}}));
ws.onmessage = e => console.log(JSON.parse(e.data));
// expect: subscribe.ack with a state snapshot, then job.* events
```

### 4. What partners should know (already in /docs/async_api)

- One connection per client; subscribe/unsubscribe by `job_id`; the ack
  carries a state snapshot (the ONLY reconnect guarantee — no replay).
- Terminal events retire a subscription but the connection stays open
  for their other jobs.
- `heartbeat.ping` every ≤25s of silence; clients SHOULD pong.
- Browser clients authenticate with the single-use token exchange
  (`POST /api/v1/ws-token`); non-browser clients may pass `?apikey=`.

### Rollback

The gateway is stateless (no DB writes, no queue consumption) — deleting
the service or setting it to sleep loses nothing. Partners fall back to
SSE + callbacks transparently; they were never asked to choose.
