from django.db import migrations, models


def seed_content_types(apps, schema_editor):
    ContentType = apps.get_model("scraper", "ContentType")
    types = [
        ("product", "Product (URL list)", "Shopping", 0),
        ("product_list", "Product List (listing page)", "Shopping", 1),
        ("product_navigation", "Product Navigation (search/browse)", "Shopping", 2),
        ("article", "Article (URL list)", "Articles", 3),
        ("article_list", "Article List (listing page)", "Articles", 4),
        ("article_navigation", "Article Navigation", "Articles", 5),
        ("job_posting", "Job Posting (URL list)", "Jobs", 6),
        ("job_navigation", "Job Navigation", "Jobs", 7),
        ("forum_thread", "Forum Thread (URL list)", "Forum", 8),
        ("serp", "SERP (search engine results)", "Search", 9),
        ("page_content", "Page Content (URL list)", "Generic", 10),
    ]
    for value, label, group, sort_order in types:
        ContentType.objects.get_or_create(
            value=value,
            defaults={"label": label, "group": group, "sort_order": sort_order},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("scraper", "0018_nav_skill_review_phase"),
    ]

    operations = [
        migrations.CreateModel(
            name="ContentType",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("value", models.CharField(max_length=50, unique=True)),
                ("label", models.CharField(max_length=100)),
                ("group", models.CharField(max_length=50)),
                ("enabled", models.BooleanField(default=True)),
                ("sort_order", models.IntegerField(default=0)),
            ],
            options={
                "ordering": ["sort_order", "group", "value"],
                "verbose_name": "Content Type",
                "verbose_name_plural": "Content Types",
            },
        ),
        migrations.RunPython(seed_content_types, migrations.RunPython.noop),
    ]
