from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("scraper", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SessionLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("user", "User"), ("assistant", "Assistant"), ("system", "System"), ("tool", "Tool")], default="assistant", max_length=20)),
                ("agent", models.CharField(blank=True, default="", max_length=100)),
                ("content", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("seq", models.IntegerField(default=0)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="session_logs", to="scraper.scrapejob")),
            ],
            options={
                "ordering": ["seq"],
            },
        ),
        migrations.AddIndex(
            model_name="sessionlog",
            index=models.Index(fields=["job", "seq"], name="sessionlog_job_seq_idx"),
        ),
    ]
