from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scraper', '0039_maintenancelockevent'),
    ]

    operations = [
        migrations.AddField(
            model_name='site',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='scrapejob',
            name='field_notes',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='scrapejob',
            name='origin_job',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=models.SET_NULL, related_name='+',
                to='scraper.scrapejob',
            ),
        ),
        migrations.AddField(
            model_name='scrapejob',
            name='parent_job',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=models.SET_NULL, related_name='reruns',
                to='scraper.scrapejob',
            ),
        ),
    ]
