# Adds user FK to ScrapeJob (per-user ownership). Nullable so auto-queued/system
# jobs don't break. Backfills existing rows to the first superuser (today
# everything was effectively the admin in dev).

from django.conf import settings
from django.db import migrations, models


def backfill_user_to_admin(apps, schema_editor):
    """Assign all existing jobs to the first superuser (the de-facto owner
    in the pre-user-scoping era)."""
    User = apps.get_model("auth", "User")
    ScrapeJob = apps.get_model("scraper", "ScrapeJob")
    admin = User.objects.filter(is_superuser=True).order_by("id").first()
    if admin:
        ScrapeJob.objects.filter(user__isnull=True).update(user=admin)


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("scraper", "0030_scrapejob_title_saved"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="scrape_jobs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_user_to_admin, migrations.RunPython.noop),
    ]
