# Adds skip_approvals to ScrapeJob — True for intake-UI jobs (which run
# unattended, skipping all human-approval gates); False for homepage jobs
# (which keep the approval stage). Default False → backward compatible.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0028_scrapejob_scraper_dagster_file"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="skip_approvals",
            field=models.BooleanField(default=False),
        ),
    ]
