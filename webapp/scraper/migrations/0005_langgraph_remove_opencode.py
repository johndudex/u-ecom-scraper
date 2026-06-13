from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0004_scrapejob_full_extraction"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="scrapejob",
            name="opencode_session_id",
        ),
        migrations.RemoveField(
            model_name="approval",
            name="opencode_permission_id",
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="graph_thread_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="celery_task_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="approval",
            name="interrupt_value",
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
        migrations.AddField(
            model_name="approval",
            name="human_response",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
