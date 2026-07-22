# Adds the intake-UI columns to ScrapeJob: the user-facing knobs from
# templates/scraper-intake.html (field chips, scope, notes). All default empty
# so existing jobs and the home view are unaffected.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0025_browser_traverse_phase"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="target_fields",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="scope",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="scope_value",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="notes",
            field=models.TextField(blank=True, default=""),
        ),
    ]
