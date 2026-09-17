"""[wave-36] Field-mapping contract — ScrapeJob.field_mapping + Site.field_mapping_cache.

docs/plans/wave36-field-mapping-plan.md §1b:
- ScrapeJob.field_mapping: per-job chip→canonical resolution
  ({chip: {target, confidence, rationale, source}}, + content hash) persisted so
  the finalize prune reads the SAME contract the pipeline enforced.
- Site.field_mapping_cache: (chips-hash → resolved) reuse across jobs on the
  same site (round-2 M6: Site has no free JSON field — output_schema is
  contract-bearing and is read AS the schema at finalize, so the cache must not
  live there).

Both nullable JSON columns → metadata-only ADD COLUMN on Postgres (safe on the
18GB prod DB; wave-35 retention never touches these rows).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scraper", "0042_widen_site_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="field_mapping",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="site",
            name="field_mapping_cache",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
