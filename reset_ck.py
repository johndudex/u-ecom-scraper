import os, sys, django
sys.path.insert(0, "/app/webapp")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()

from scraper.models import ScrapeJob, SessionLog, Approval, ProbeCache, Site
from django.db import connection

site = Site.objects.filter(slug="calvinklein-co-uk").first()
if site:
    site.status = "in_progress"
    site.save(update_fields=["status"])
    print("Site reset to in_progress")

ProbeCache.objects.filter(domain__contains="calvinklein").delete()
print("Probe cache cleared")

Approval.objects.filter(job_id__gte=121).delete()
print("Approvals cleared")

cursor = connection.cursor()
for jid in range(121, 125):
    for t in ["checkpoint_writes", "checkpoints", "checkpoint_blobs"]:
        cursor.execute(f"DELETE FROM {t} WHERE thread_id LIKE %s", (f"%job-{jid}%",))

SessionLog.objects.filter(job_id__gte=121).delete()
print("All state cleared")
