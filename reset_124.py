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

# Seed probe cache with known-working method
ProbeCache.objects.update_or_create(
    domain="www.calvinklein.co.uk",
    defaults={
        "method": "uc_chrome_none",
        "needs_akamai_bypass": False,
        "captcha_detected": False,
    }
)
print("Probe cache seeded with uc_chrome_none")

Approval.objects.filter(job_id=124).delete()

cursor = connection.cursor()
for t in ["checkpoint_writes", "checkpoints", "checkpoint_blobs"]:
    cursor.execute(f"DELETE FROM {t} WHERE thread_id LIKE %s", ("%job-124%",))
SessionLog.objects.filter(job_id=124).delete()
print("Job 124 state cleared")
