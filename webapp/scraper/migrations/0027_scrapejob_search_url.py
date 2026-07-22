# Adds search_url to ScrapeJob — the search results page URL for search_term
# intake jobs (the nav "I reach them by searching" mode). Distinct from
# search_criteria (keywords). Empty by default → backward compatible.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0026_intake_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="search_url",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
    ]
