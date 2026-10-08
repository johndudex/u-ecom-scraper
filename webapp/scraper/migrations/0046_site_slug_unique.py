# [wave-55 H14] Dedupe Site.slug, then make it unique.
#
# Audit (2026-10-05): prod carried 101 duplicate slug groups (766 Site rows /
# 651 distinct slugs). check_tracker keys the Site row on the SUBMITTED URL,
# so two sample PDPs of one host minted two rows with the same slug, and
# every `Site.objects.filter(slug=...).first()` then read an arbitrary member
# (Site.Meta.ordering = ["-created_at"], so the newest row won and
# same-second rows tie nondeterministically).
#
# Rule per group: keep the LOWEST id — the canonical row the
# scrapers/{slug}/ folder was built from — repoint the only FK that
# references Site (JobListing.site, on_delete=SET_NULL, so a straight delete
# would have silently null-ed every listing), fill the keeper's blank
# knowledge fields from the rows it absorbs, then delete the rest and let
# the constraint hold the line. Blank slugs are exempt (an unparseable URL
# yields slug="" and that tolerance predates this wave), hence the partial
# constraint matching the model declaration.

from django.db import migrations, models

# Knowledge merged from an absorbed row into the keeper when the keeper's
# own value is blank. Mutable run-state (status, product_count,
# last_scraped_at, archived_at) is deliberately NOT merged — the keeper's
# own state is the one that is live.
_MERGE_FIELDS = (
    "name", "platform", "scraping_method", "currency",
    "default_scraper_path", "has_scraper", "output_schema", "fields_extracted",
)


def _blank(value) -> bool:
    """Field-level 'unset' — the defaults a fresh Site row ships with."""
    if value is None or value is False:
        return True
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return len(value) == 0
    return False


def dedupe_site_slugs(apps, schema_editor):
    Site = apps.get_model("scraper", "Site")
    JobListing = apps.get_model("scraper", "JobListing")

    groups = (
        Site.objects.values("slug")
        .annotate(n=models.Count("id"))
        .filter(n__gt=1)
        .values_list("slug", flat=True)
    )
    for slug in groups:
        if not slug:
            # Blank slugs are not identity — leave them alone (the partial
            # unique index does not police them either).
            continue
        rows = list(Site.objects.filter(slug=slug).order_by("id"))
        if len(rows) < 2:
            continue  # a concurrent migration beat us to it
        keeper, absorbed_ids = rows[0], [r.pk for r in rows[1:]]

        dirty: list[str] = []
        for row in rows[1:]:
            for field in _MERGE_FIELDS:
                if field in dirty:
                    continue
                value = getattr(row, field)
                if _blank(getattr(keeper, field)) and not _blank(value):
                    setattr(keeper, field, value)
                    dirty.append(field)
        if dirty:
            keeper.save(update_fields=dirty)

        JobListing.objects.filter(site_id__in=absorbed_ids).update(site_id=keeper.pk)
        Site.objects.filter(pk__in=absorbed_ids).delete()


class Migration(migrations.Migration):

    # atomic=False is REQUIRED on Postgres: the RunPython above leaves
    # pending trigger events on the rows it repointed/deleted, and building
    # the unique index in the SAME transaction then fails with
    # "cannot CREATE INDEX ... because it has pending trigger events".
    # Each operation therefore commits on its own. Re-run safe: the dedupe
    # is idempotent (groups already reduced to one row are skipped), so a
    # constraint failure (leftover duplicates on a foreign dataset) leaves a
    # clean re-apply path.
    atomic = False

    dependencies = [
        ('scraper', '0045_alter_scrapejob_status'),
    ]

    operations = [
        migrations.RunPython(dedupe_site_slugs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='site',
            constraint=models.UniqueConstraint(
                condition=~models.Q(('slug', '')),
                fields=['slug'],
                name='site_slug_unique',
            ),
        ),
    ]
