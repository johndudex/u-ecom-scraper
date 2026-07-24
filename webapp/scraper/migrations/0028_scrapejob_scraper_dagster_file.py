# Adds scraper_file + dagster_file to ScrapeJob — per-job attribution of the
# generated scraper/dagster files, so each job's download serves ITS files
# (independent of the shared scrapers/{slug}/scraper.py that later jobs
# overwrite). Empty by default → backward compatible (views fall back to slug).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0027_scrapejob_search_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="scraper_file",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="dagster_file",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
