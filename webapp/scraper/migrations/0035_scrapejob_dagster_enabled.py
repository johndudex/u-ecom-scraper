from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0034_apikey_multiple_per_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="dagster_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
