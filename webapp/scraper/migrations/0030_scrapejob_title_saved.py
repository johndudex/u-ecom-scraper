# Adds title (user-facing display name / rename) + is_saved (library bookmark)
# to ScrapeJob for the revised intake UI. Both default empty/false → backward
# compatible (UI falls back to "Job #<id>" when title is empty).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0029_scrapejob_skip_approvals"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="is_saved",
            field=models.BooleanField(default=False),
        ),
    ]
