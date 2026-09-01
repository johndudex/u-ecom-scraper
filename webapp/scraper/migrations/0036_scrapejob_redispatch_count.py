from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0035_scrapejob_dagster_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="redispatch_count",
            field=models.IntegerField(default=0),
        ),
    ]
