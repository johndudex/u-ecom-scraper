FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY event_gateway/requirements.txt /tmp/req.txt
# The gateway imports django (settings) + its models/emitters — the lean
# dependency set the app actually needs, not the full webapp stack.
RUN pip install --no-cache-dir -r /tmp/req.txt \
    "django>=5.2,<5.3" "python-decouple" "psycopg[binary]" \
    "django-celery-beat" "django-celery-results"

COPY event_gateway/ /app/event_gateway/
COPY src/ /app/src/
COPY config/ /app/config/
COPY webapp/config/ /app/webapp/config/
COPY webapp/scraper/migrations/ /app/webapp/scraper/migrations/
COPY webapp/scraper/forms.py /app/webapp/scraper/forms.py
COPY webapp/requirements.txt /tmp/webapp-req.txt
COPY webapp/scraper/models.py /app/webapp/scraper/models.py
COPY webapp/scraper/__init__.py /app/webapp/scraper/__init__.py
COPY webapp/scraper/api/__init__.py /app/webapp/scraper/api/__init__.py
COPY webapp/scraper/api/sse.py /app/webapp/scraper/api/sse.py
COPY webapp/scraper/events/ /app/webapp/scraper/events/
COPY webapp/scraper/management/ /app/webapp/scraper/management/

WORKDIR /app/event_gateway
EXPOSE 8100
ENV PYTHONPATH=/app:/app/webapp
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
