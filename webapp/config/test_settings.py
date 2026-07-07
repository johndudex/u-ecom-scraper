from config.settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = True

SECRET_KEY = "test-secret-key-not-for-production"
DEBUG = True
# Tests hit @login_required views; the DebugAutoLoginMiddleware auto-logs in the
# first superuser when this is set (conftest.py creates one). Avoids per-test login.
DEBUG_AUTO_LOGIN = True
