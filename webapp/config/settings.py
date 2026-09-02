from pathlib import Path

from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="dev-secret-key-change-in-production")
DEBUG = config("DEBUG", default=False)
DEBUG_AUTO_LOGIN = config("DEBUG_AUTO_LOGIN", default=False)

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*").split(",")

# Railway / reverse-proxy TLS. Railway terminates TLS at the proxy; Django sees
# HTTP. Without SECURE_PROXY_SSL_HEADER, request.is_secure() is always False →
# CSRF origin checks fail on every POST (login, job submit, approval) → 403.
# Gated on DEBUG so local dev (HTTP, no proxy) is unaffected.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)

# CSRF — REQUIRED on Railway. Without CSRF_TRUSTED_ORIGINS, every form POST
# (which is ALL views — @login_required) returns 403 Forbidden because Django
# 5.x compares the Origin header against this list and ALLOWED_HOSTS does NOT
# auto-populate it (that mapping was removed in Django 4.0). Set to your Railway
# domain(s): https://yourapp.up.railway.app,https://custom.com
_csrf_origins = [o.strip() for o in config("CSRF_TRUSTED_ORIGINS", default="").split(",") if o.strip()]
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = _csrf_origins

# Cookie safety on TLS (opt-in — local HTTP dev stays unsecured)
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=not DEBUG, cast=bool)
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=not DEBUG, cast=bool)

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "scraper",
    "django_celery_beat",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "config.middleware.DebugAutoLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "scraper" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "scraper.context_processors.dashboard_stats",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="scraper"),
        "USER": config("DB_USER", default="scraper"),
        "PASSWORD": config("DB_PASSWORD", default="scraper"),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
        # F4: long-lived celery workers kept using connections the postmaster
        # had killed (postgres OOM restarts) — CONN_MAX_AGE keeps a connection
        # reusable across tasks and CONN_HEALTH_CHECKS pings it before each
        # checkout, replacing it when stale. (Celery skips the request-cycle
        # close_old_connections, so the health check is the load-bearing half.)
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

# PgBouncer transaction-pooling friendly settings (Django 5.1+). Opt-in via
# DB_USE_PGBOUNCER=true on Railway (managed Postgres with PgBouncer enabled).
if config("DB_USE_PGBOUNCER", default=False, cast=bool):
    DATABASES["default"]["CONN_MAX_AGE"] = None  # persistent connections
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
    DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = config("REDIS_URL", default="redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# Explicit (Celery 5.0+ defaults True; make it loud so a future version flip
# doesn't break Railway's parallel-startup where the worker boots before Redis).
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Phase 3 (Per-Phase Execution Contract) — worker recycling. Reclaims leaked
# memory (abandoned threads, large contexts, aya's 26K-record parse) by warm-
# recycling the worker after a task if its RSS exceeds the ceiling, or after N
# tasks. Warm recycle = finish the current task, THEN recycle → no task loss,
# so acks_late is NOT required for this.
#
# [jobs 79/80] GEOMETRY: warm recycle can only fire BETWEEN tasks — it cannot
# intervene mid-task. With concurrency=2 the old 2.5g ceiling allowed
# 2 × 2.5g = 5g against the container's 3g mem_limit, so two long-lived tasks
# crossing the container limit together produced a near-simultaneous double
# child SIGKILL (jobs 79/80 died within 320 ms of each other at 14:00:01).
# 1.2g per child × 2 = 2.4g leaves the parent + headroom under 3g, and the
# (warm, lossless) recycle now fires before the kernel has to.
CELERY_WORKER_MAX_MEMORY_PER_CHILD = config(
    "CELERY_WORKER_MAX_MEMORY_PER_CHILD", default=1228800, cast=int
)
CELERY_WORKER_MAX_TASKS_PER_CHILD = config(
    "CELERY_WORKER_MAX_TASKS_PER_CHILD", default=10, cast=int
)
# acks_late (at-least-once delivery) — DEFAULT OFF. Enabling changes delivery
# semantics: a hard-crashed/lost worker's task is redelivered. Correct ONLY with
# the cleanup_stuck_jobs "mark FAILED before revoke" reorder (so the dedup guard
# lets the redelivery resume from the langgraph checkpoint) + idempotent side
# effects. Keep off until that path is regression-verified; flip per-deploy.
CELERY_TASK_ACKS_LATE = config("CELERY_TASK_ACKS_LATE", default=False, cast=bool)
CELERY_TASK_REJECT_ON_WORKER_LOST = config(
    "CELERY_TASK_REJECT_ON_WORKER_LOST", default=False, cast=bool
)
CELERY_BEAT_SCHEDULE = {
    "cleanup-stuck-jobs": {
        "task": "scraper.tasks.cleanup_stuck_jobs",
        "schedule": 300.0,
    },
    # [wave-15 1.2] Recover never-dispatched PENDING rows (keystone signature:
    # celery_task_id=""). Env-gated OFF until every dispatch site stamps before
    # publish — scheduled BEFORE schedule-next-site so a recovered row is
    # visible to the same-site serializer in the same tick.
    "redispatch-abandoned-pending": {
        "task": "scraper.tasks.redispatch_abandoned_pending",
        "schedule": 300.0,
    },
    "schedule-next-site": {
        "task": "scraper.tasks.schedule_next_site",
        "schedule": 300.0,
    },
    "stuck-approved-watchdog": {
        "task": "scraper.tasks.redispatch_stuck_approved_interrupts",
        "schedule": 300.0,
    },
    # [wave-16 B3] Dependency-park resumer: poll browser_service /health and
    # re-dispatch parked jobs once it recovers. 120s cadence × batch 3 — the
    # 2-slot scrape pool can't absorb more anyway. Kill-switch:
    # BROWSER_RESUME_ENABLED=0 (off by default would strand parked jobs).
    "resume-browser-unavailable": {
        "task": "scraper.tasks.resume_browser_unavailable_jobs",
        "schedule": 120.0,
    },
    # Partner event delivery (async_api.yaml): 30s sweep drives the < 1m
    # backoff legs + lost-task recovery; >= 1m legs self-schedule exact.
    "dispatch-pending-callbacks": {
        "task": "scraper.events.reconciler.dispatch_pending_callbacks",
        "schedule": 30.0,
    },
}

# Fold B5: delivery HTTP must never share the scrape workers' 2-slot pool —
# one hung partner endpoint ≈ 10 min of zero scrape capacity otherwise.
# [jobs 79/80] Same for the watchdog: cleanup_stuck_jobs is beat-scheduled on
# the scrape pool it polices — with both slots busy it queued for 8+ min
# (the "38 min" idle report for a 30-min threshold). The events worker is a
# separate 4-slot pool → true 5-min cadence.
CELERY_TASK_ROUTES = {
    "scraper.events.dispatcher.deliver_callback": "events",
    "scraper.events.dispatcher.dispatch_pending_callbacks": "events",
    "scraper.events.reconciler.dispatch_pending_callbacks": "events",
    "scraper.tasks.cleanup_stuck_jobs": "events",
    # [wave-15 1.2] Recovery/maintenance tasks ride the dedicated events pool:
    # the sweep must not wait behind scraping tasks on the 2-slot scrape pool
    # (redispatch_stuck_approved_interrupts was unrouted before — it worked
    # only by luck of the scrape pool's queueing).
    "scraper.tasks.redispatch_abandoned_pending": "events",
    "scraper.tasks.redispatch_stuck_approved_interrupts": "events",
    # [wave-16 B3] The park resumer polices the scrape pool — same reasoning
    # as the watchdog above, it must not queue behind the jobs it resuscitates.
    "scraper.tasks.resume_browser_unavailable_jobs": "events",
}

# [wave-15 1.2] Master switch for the abandoned-PENDING redispatch sweep.
# DEFAULT OFF: the sweep's signature (PENDING ∧ celery_task_id="") is only
# sound once every dispatch path stamps the task id BEFORE publishing
# (dispatch_scrape_job). Flip on per-deploy after verifying the keystone is
# live (prod rows never carried a stamp before wave-15).
REDISPATCH_SWEEP_ENABLED = config("REDISPATCH_SWEEP_ENABLED", default=False, cast=bool)

# [wave-16 B3] Dependency-park resumer master switch. DEFAULT ON (unlike the
# sweep above): the resumer only acts when browser_service /health reports
# "ok", so it cannot do damage while the dependency is down — and leaving it
# off by default would strand parked jobs behind a deploy step.
BROWSER_RESUME_ENABLED = config("BROWSER_RESUME_ENABLED", default=True, cast=bool)

ZAI_API_KEY = config("ZAI_API_KEY", default="")
ZAI_BASE_URL = config("ZAI_BASE_URL", default="https://api.z.ai/api/coding/paas/v4/")
ZAI_MAIN_MODEL = config("ZAI_MAIN_MODEL", default="glm-5-turbo")
ZAI_SMALL_MODEL = config("ZAI_SMALL_MODEL", default="glm-5-turbo")
# Per-agent model override for code_writer. Defaults to the main model
# (glm-5-turbo) — the reasoning agents work well on it and code_writer benefits
# from the stronger model now that it gets complete-but-lean summaries instead
# of reading the full analysis JSONs. Set CODE_WRITER_MODEL=glm-4.7-flash to
# A/B test the faster flash model on codegen.
# T0.1: an EMPTY env value must fall through to the corpus default — compose
# exports CODE_WRITER_MODEL="" when .env doesn't set it (decouple returns ""
# for present-but-empty, it does NOT substitute the default).
CODE_WRITER_MODEL = config("CODE_WRITER_MODEL", default="glm-5-turbo") or "glm-5-turbo"
# LiteLLM proxy provider routing (code_writer via llm.johnjf.xyz). A model name
# prefixed with one of LITELLM_MODEL_PREFIXES (e.g. CODE_WRITER_MODEL=
# litellm/standardcompute) routes to LITELLM_BASE_URL with the prefix stripped
# client-side; the prefix IS the kill switch (unset it → back to Z.AI, no code
# change). The breaker key stays the full configured string (see llm.py).
LITELLM_ENABLED = config("LITELLM_ENABLED", default=True, cast=bool)
LITELLM_BASE_URL = config("LITELLM_BASE_URL", default="https://llm.johnjf.xyz/v1")
LITELLM_API_KEY = config("LITELLM_API_KEY", default="")
LITELLM_MODEL_PREFIXES = config("LITELLM_MODEL_PREFIXES", default="litellm/")
LITELLM_FALLBACK_MODEL = config("LITELLM_FALLBACK_MODEL", default="")
# Empty = NO breaker fallback for litellm models (deliberate: the proxy exposes
# exactly one model, so any non-empty litellm fallback 404s; a GLM-name fallback
# would be sent to the wrong provider). Leave empty — not a knob.
# Per-agent LLM call timeout overrides (subagents._build_agent). code_writer
# needs a longer per-call budget than the 300s global default — reasoning
# models generating ~500-line drafts can exceed 300s on a single call, and the
# classified-retry layer would multiply that (3×300s) inside the 900s wall.
CODE_WRITER_LLM_TIMEOUT = config("CODE_WRITER_LLM_TIMEOUT", default=600, cast=int)
# Fallback model when the primary trips the per-model circuit breaker (Phase 1,
# contract rollout). Defaults to the small model. The breaker bounds how long a
# bad/stalling model receives traffic: after LLM_CIRCUIT_BREAKER_THRESHOLD
# consecutive failures it routes to this model for LLM_CIRCUIT_BREAKER_COOLDOWN.
ZAI_FALLBACK_MODEL = config("ZAI_FALLBACK_MODEL", default="glm-5-turbo")
LLM_CIRCUIT_BREAKER_ENABLED = config("LLM_CIRCUIT_BREAKER_ENABLED", default=True, cast=bool)
LLM_CIRCUIT_BREAKER_THRESHOLD = config("LLM_CIRCUIT_BREAKER_THRESHOLD", default=4, cast=int)
LLM_CIRCUIT_BREAKER_COOLDOWN = config("LLM_CIRCUIT_BREAKER_COOLDOWN", default=60, cast=int)
# Phase 2 classified retry (Per-Phase Execution Contract). Kill-switch off →
# revert to pre-Phase-2 plain ChatOpenAI with the blind max_retries below.
LLM_CLASSIFIED_RETRY = config("LLM_CLASSIFIED_RETRY", default=True, cast=bool)
LLM_MAX_RETRIES = config("LLM_MAX_RETRIES", default=5, cast=int)  # only used when classified retry OFF
LLM_RETRY_TRANSIENT_MAX = config("LLM_RETRY_TRANSIENT_MAX", default=2, cast=int)   # timeout/conn/5xx
# 429 class has its own ladder (job 12: 4× 429 in 8s burned 3 attempts in ~7.5s).
# Worst case 6×30s = 180s inside the 900s job wall. Rollback to the old budget:
# LLM_RETRY_RATELIMIT_MAX=3, LLM_RETRY_RATELIMIT_BASE=1.5, LLM_RETRY_BACKOFF_FLOOR=0.
LLM_RETRY_RATELIMIT_MAX = config("LLM_RETRY_RATELIMIT_MAX", default=6, cast=int)   # 429 attempts
LLM_RETRY_RATELIMIT_BASE = config("LLM_RETRY_RATELIMIT_BASE", default=2.0, cast=float)  # 429 backoff base
LLM_RETRY_BACKOFF_FLOOR = config("LLM_RETRY_BACKOFF_FLOOR", default=1.0, cast=float)   # 429 min sleep (s)
LLM_RETRY_BACKOFF_BASE = config("LLM_RETRY_BACKOFF_BASE", default=1.5, cast=float)     # transient class
LLM_RETRY_BACKOFF_CAP = config("LLM_RETRY_BACKOFF_CAP", default=30.0, cast=float)
# Phase 2 deterministic context truncation. Replaces the headroom.compress-based
# pre-model hook (which made a SYNC LLM call inside the cancellation path + added
# run-to-run variance). 'deterministic' (default) | 'off' (no-op rollback).
LLM_TRUNCATION_MODE = config("LLM_TRUNCATION_MODE", default="deterministic")
LLM_TRUNCATION_MAX_CHARS = config("LLM_TRUNCATION_MAX_CHARS", default=180000, cast=int)
LLM_TRUNCATION_PER_MSG_CAP = config("LLM_TRUNCATION_PER_MSG_CAP", default=8000, cast=int)
# Async cancellation (Per-Phase Execution Contract). Default OFF. Resolution
# order in agents/graph.py:_async_execution_enabled: this all-phases override
# wins, else the phase must be named in AGENT_ASYNC_PHASES (the wave-15
# canary — e.g. AGENT_ASYNC_PHASES=code_writer; compose passes it through,
# empty by default). This var is deliberately NOT in docker-compose anymore:
# flipping it forces EVERY phase onto the async loop at once.
LLM_ASYNC_EXECUTION = config("LLM_ASYNC_EXECUTION", default=False, cast=bool)
# Phase 5 determinism A/B switch. When True, code-writer + product-analyzer use
# temperature 0 (narrows the codegen distribution; z.ai doesn't reliably honor
# seed, so this narrows but doesn't guarantee). Default False.
LLM_CODEGEN_DETERMINISTIC = config("LLM_CODEGEN_DETERMINISTIC", default=False, cast=bool)
# Phase 1 (JS-listing+pagination class fix): when navigation doesn't reach the
# listing (listing_reached=False), OMIT --listing-url so the scraper's
# DEFAULT_LISTING_URL drives discovery (not the sample detail URL). Default True.
RESPECT_LISTING_REACHED_FLAG = config("RESPECT_LISTING_REACHED_FLAG", default=True, cast=bool)
# CLI-contract L3 honesty floor (docs/cli-contract-plan.md): when discovery-
# critical flags are stripped AND the draft has no wired discovery trigger,
# run_execution refuses the silent seed-only run and fails honestly. A draft
# that reads SCRAPER_LISTING_URL (compliant) proceeds even with stripped flags.
DISCOVERY_CONTRACT_STRICT = config("DISCOVERY_CONTRACT_STRICT", default=True, cast=bool)
# Per-call HTTP timeout for every LLM call (llm.py ChatOpenAI). The PRIMARY hang
# guard — a stuck request dies here at 600s rather than hanging indefinitely.
# Explicitly defined (previously only referenced in comments; llm.py fell back to
# its getattr default, masking the intent). Override via env if needed.
LLM_REQUEST_TIMEOUT = config("LLM_REQUEST_TIMEOUT", default=300, cast=int)
PLAYWRIGHT_MCP_URL = config("PLAYWRIGHT_MCP_URL", default="http://browser_service:8111/sse")
# Explicit BROWSER_SERVICE_URL — views.py reads getattr(settings, ...) for the
# health dashboard + site_rerun. Without this, it falls through to the
# underscored default ("browser_service:8001") which doesn't resolve on Railway
# (Railway private DNS is hyphenated: "browser-service.railway.internal").
BROWSER_SERVICE_URL = config("BROWSER_SERVICE_URL", default="http://browser_service:8001")
# Scraper execution routing (browser_service-rework Step 3). Controls how
# run_execution + the run_scraper tool dispatch a generated scraper:
#   "auto" (default) — _needs_browser decides per-scraper (sniff imports).
#   "force_scrape"   — always route via browser_service POST /scrape (rollback
#                      lane to the subprocess model; rework plan §4).
#   "force_http"     — always run in-process via _run_in_process (forces the new
#                      HTTP navigation model even for legacy Playwright scrapers,
#                      post-migration Phase C).
SCRAPER_EXECUTION_MODE = config("SCRAPER_EXECUTION_MODE", default="auto")
# Execution fail-fast bounds (apply to ALL scrapers — template AND custom
# code_writer code). The template DISCOVERY_DEADLINE_SECONDS only governs
# template-based scrapers; custom scrapers had no bound and could hang for the
# full (multi-hour) subprocess timeout on a JS-blocked/rate-limited site.
#   EXECUTION_STALL_TIMEOUT — kill the scraper if it emits NO stderr output for
#       this many seconds (productive scrapers log page/item progress; a hang
#       stops). Default 300s (legit scrapes log every few seconds).
#   EXECUTION_TIMEOUT — base wall-clock budget. Default 3600s (1h).
#   EXECUTION_MAX_TIMEOUT — absolute ceiling the deadline may progress-extend
#       to (run_execution reads "Progress: [k/N]" off the scraper's stderr and
#       budgets the REMAINING items). Must stay under the celery task soft
#       time limit so a ceiling-capped run still finalizes inside the task.
#       [job-315 citybeach: a healthy 1,317-item extraction (Progress every
#       ~90s) died at 72% under the flat 3600s backstop.]
EXECUTION_STALL_TIMEOUT = config("EXECUTION_STALL_TIMEOUT", default=300, cast=int)
EXECUTION_TIMEOUT = config("EXECUTION_TIMEOUT", default=3600, cast=int)
EXECUTION_MAX_TIMEOUT = config("EXECUTION_MAX_TIMEOUT", default=9600, cast=int)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRAPERS_DIR = PROJECT_ROOT / "scrapers"
DATA_DIR = PROJECT_ROOT / "data"
SRC_DIR = PROJECT_ROOT / "src"
CONFIG_DIR = PROJECT_ROOT / "config"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
LOGS_DIR = PROJECT_ROOT / "logs"
