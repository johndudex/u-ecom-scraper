from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scraper', '0040_wave27_archive_lineage'),
    ]

    operations = [
        migrations.AlterField(
            model_name='step',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('running', 'Running'),
                    ('done', 'Done'),
                    ('failed', 'Failed'),
                    ('skipped', 'Skipped'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
