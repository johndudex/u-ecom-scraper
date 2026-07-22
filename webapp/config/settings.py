from pathlib import Path

from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="dev-secret-key-change-in-production")
DEBUG = config("DEBUG", default=False)
DEBUG_AUTO_LOGIN = config("DEBUG_AUTO_LOGIN", default=False)

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*").split(",")

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
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = config("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config("REDIS_URL", default="redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "cleanup-stuck-jobs": {
        "task": "scraper.tasks.cleanup_stuck_jobs",
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
}

ZAI_API_KEY = config("ZAI_API_KEY", default="")
ZAI_BASE_URL = config("ZAI_BASE_URL", default="https://api.z.ai/api/coding/paas/v4/")
ZAI_MAIN_MODEL = config("ZAI_MAIN_MODEL", default="glm-5-turbo")
ZAI_SMALL_MODEL = config("ZAI_SMALL_MODEL", default="glm-5-turbo")
# Per-agent model override for code_writer. Defaults to the main model
# (glm-5-turbo) — the reasoning agents work well on it and code_writer benefits
# from the stronger model now that it gets complete-but-lean summaries instead
# of reading the full analysis JSONs. Set CODE_WRITER_MODEL=glm-4.7-flash to
# A/B test the faster flash model on codegen.
CODE_WRITER_MODEL = config("CODE_WRITER_MODEL", default="glm-5-turbo")
# Per-call HTTP timeout for every LLM call (llm.py ChatOpenAI). The PRIMARY hang
# guard — a stuck request dies here at 600s rather than hanging indefinitely.
# Explicitly defined (previously only referenced in comments; llm.py fell back to
# its getattr default, masking the intent). Override via env if needed.
LLM_REQUEST_TIMEOUT = config("LLM_REQUEST_TIMEOUT", default=300, cast=int)
PLAYWRIGHT_MCP_URL = config("PLAYWRIGHT_MCP_URL", default="http://browser_service:8111/sse")
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
#   EXECUTION_TIMEOUT — hard wall-clock backstop. Default 3600s (1h).
EXECUTION_STALL_TIMEOUT = config("EXECUTION_STALL_TIMEOUT", default=300, cast=int)
EXECUTION_TIMEOUT = config("EXECUTION_TIMEOUT", default=3600, cast=int)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRAPERS_DIR = PROJECT_ROOT / "scrapers"
DATA_DIR = PROJECT_ROOT / "data"
SRC_DIR = PROJECT_ROOT / "src"
CONFIG_DIR = PROJECT_ROOT / "config"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
LOGS_DIR = PROJECT_ROOT / "logs"
