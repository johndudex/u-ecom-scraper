from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scraper", "0043_field_mapping"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="parked_seconds",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="last_parked_at",
            field=models.FloatField(null=True, blank=True),
        ),
    ]
