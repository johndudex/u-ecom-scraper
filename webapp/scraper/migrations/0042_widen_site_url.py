"""[wave-34] Widen Site.url 200 → 1000.

Prod 626 (whitehouseblackmarket): a 215-char PDP seed made check_tracker's
auto-create throw `value too long for type character varying(200)` — no Site
row was ever created and finalize reported `platform=`. ScrapeJob.url already
carries 1000; Site was the straggler at URLField's default 200.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scraper", "0041_step_skipped_choice"),
    ]

    operations = [
        migrations.AlterField(
            model_name="site",
            name="url",
            field=models.URLField(max_length=1000, unique=True),
        ),
    ]
