FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates libpq-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY webapp/requirements.txt /tmp/requirements.txt
COPY requirements.txt /tmp/scraper-requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/scraper-requirements.txt

RUN adduser --disabled-password --gecos "Scraper" scraper
USER scraper

COPY --chown=scraper:scraper webapp/ /app/webapp/
COPY --chown=scraper:scraper src/ /app/src/
COPY --chown=scraper:scraper config/ /app/config/
COPY --chown=scraper:scraper scripts/ /app/scripts/
COPY --chown=scraper:scraper data/ /app/data/
COPY --chown=scraper:scraper templates/ /app/templates/
COPY --chown=scraper:scraper .opencode/ /app/.opencode/
COPY --chown=scraper:scraper AGENTS.md /app/AGENTS.md

WORKDIR /app/webapp

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
