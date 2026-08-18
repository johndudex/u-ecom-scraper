FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates libpq-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY webapp/requirements.txt /tmp/requirements.txt
COPY requirements.txt /tmp/scraper-requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/scraper-requirements.txt

RUN adduser --disabled-password --gecos "Scraper" scraper
# Create workspace + logs dirs owned by scraper so Railway volumes (mounted as
# root) don't block the non-root worker's os.makedirs at setup_workspace.
RUN mkdir -p /app/workspace /app/logs /app/scrapers && chown -R scraper:scraper /app/workspace /app/logs /app/scrapers
USER scraper

COPY --chown=scraper:scraper webapp/ /app/webapp/
COPY --chown=scraper:scraper src/ /app/src/
COPY --chown=scraper:scraper config/ /app/config/
COPY --chown=scraper:scraper scripts/ /app/scripts/
COPY --chown=scraper:scraper data/ /app/data/
COPY --chown=scraper:scraper templates/ /app/templates/
COPY --chown=scraper:scraper .opencode/ /app/.opencode/
COPY --chown=scraper:scraper experimental/nav_traversal/ /app/experimental/nav_traversal/
COPY --chown=scraper:scraper experimental/__init__.py /app/experimental/__init__.py
COPY --chown=scraper:scraper AGENTS.md /app/AGENTS.md

WORKDIR /app/webapp

# Collect static at BUILD time. Railway's Pre-Deploy runs in a separate
# container whose filesystem is discarded, so collectstatic there is a no-op
# (live failure #5: "No directory at: /app/webapp/staticfiles/" at runtime).
# Settings import needs zero env vars (every config() has a default) — only
# PYTHONPATH=/app for the `from src...` imports.
RUN PYTHONPATH=/app DJANGO_SETTINGS_MODULE=config.settings \
    python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "3600", "--graceful-timeout", "60", "--worker-tmp-dir", "/dev/shm"]
