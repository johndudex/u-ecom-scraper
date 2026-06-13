from __future__ import annotations

from celery import Celery

app = Celery("scraper")

app.config_from_object("django.conf.settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
