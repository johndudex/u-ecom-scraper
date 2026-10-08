"""[wave-55 H14] Site slugs are unique and the slug lookups are deterministic.

Audit (2026-10-05): prod carried 101 duplicate slug groups (766 Site rows /
651 distinct slugs — ``rmwilliams-com-au`` x5, ``sysmartlink-en-made-in-china-com``
x5, ...). ``check_tracker`` keys the Site row on the submitted URL, so two
sample PDPs of one host minted two rows with the same slug, and every
``Site.objects.filter(slug=...).first()`` then read an ARBITRARY member
(``Site.Meta.ordering = ["-created_at"]``, so the newest row won, and rows
created in the same second tie nondeterministically).

Every test here runs on the EPHEMERAL pytest database inside pytest-django's
rolled-back transaction — the only database it can touch. Duplicating a slug
needs the constraint out of the way for a moment, so
``_allow_duplicate_slugs`` drops the index first; the rollback puts it back
(byte-for-byte, because Postgres DDL is transactional). Migration 0046's own
RunPython is exercised DIRECTLY (the migration module's function, runtime
models) rather than by replaying the migration chain against a scratch
database — a scratch DB is a second database the historical-model ORM can
silently write around, and nothing here may reach outside this test DB.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_site_slug_uniqueness.py -q"
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.db import connection  # noqa: E402
from django.db.utils import IntegrityError  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402
from scraper.models import JobListing, ScrapeJob, Site  # noqa: E402
from scraper.views import _job_input_urls, intake_check_site  # noqa: E402

SLUG = "dup-example-com"


@pytest.fixture
def _allow_duplicate_slugs():
    """Drop the unique index so a test can plant the audit's duplicate rows.

    The whole test is one transaction (pytest-django's non-transactional
    ``django_db``), so the DDL rolls back with the rest of it.
    """
    with connection.cursor() as cursor:
        cursor.execute('DROP INDEX IF EXISTS "site_slug_unique"')
    yield


def _plant_dup_sites(**a_kw) -> tuple[Site, Site]:
    """Two rows, one slug: ``a`` is the LOWEST id, ``b`` the newest
    ``created_at`` — exactly the pair an unordered ``.first()`` muddles."""
    a, b = Site.objects.bulk_create([
        Site(url="https://dup.example.com/p/a", slug=SLUG,
             input_urls=["https://dup.example.com/p/a"], **a_kw),
        Site(url="https://dup.example.com/p/b", slug=SLUG, platform="sfcc",
             input_urls=["https://dup.example.com/p/b"]),
    ])
    now = dj_tz.now()
    Site.objects.filter(pk=a.pk).update(created_at=now - timedelta(hours=2))
    Site.objects.filter(pk=b.pk).update(created_at=now - timedelta(hours=1))
    return a, b


class TestSlugLookupIsDeterministic:
    @pytest.mark.django_db
    def test_job_input_urls_reads_the_lowest_id_row(
        self, _allow_duplicate_slugs
    ):
        _a, _b = _plant_dup_sites()
        from model_bakery import baker

        job = baker.make(ScrapeJob, input_mode="url_list",
                         site_folder=f"scrapers/{SLUG}")

        seeded = _job_input_urls(job)
        assert seeded["input_urls"] == ["https://dup.example.com/p/a"]
        assert seeded["input_urls_count"] == 1

    @pytest.mark.django_db
    def test_intake_check_site_reads_the_lowest_id_row(
        self, _allow_duplicate_slugs
    ):
        _a, _b = _plant_dup_sites(platform="shopify", fields_extracted=["title"])
        from django.contrib.auth.models import User
        from model_bakery import baker

        request = RequestFactory().post(
            "/intake/check-site/", {"url": "https://dup.example.com/p/1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = baker.make(User)
        data = json.loads(intake_check_site(request).content)

        assert data["known_site"] is True
        assert data["platform"] == "shopify"
        assert data["fields"] == ["title"]

    @pytest.mark.django_db
    def test_created_at_ties_still_resolve_by_id(self, _allow_duplicate_slugs):
        a, b = _plant_dup_sites()
        # Same timestamp → the -created_at ordering cannot break the tie; the
        # explicit order_by("id") must.
        Site.objects.filter(pk__in=[a.pk, b.pk]).update(created_at=dj_tz.now())
        from model_bakery import baker

        job = baker.make(ScrapeJob, input_mode="url_list",
                         site_folder=f"scrapers/{SLUG}")

        assert _job_input_urls(job)["input_urls"] == ["https://dup.example.com/p/a"]


# ─── migration 0046: the data half, on this test DB ──────────────────────────


def _dedupe_fn():
    """The migration's RunPython body, imported straight from the module
    (its name starts with a digit, so it can only be imported by path)."""
    module = importlib.import_module("scraper.migrations.0046_site_slug_unique")
    return module.dedupe_site_slugs, module._MERGE_FIELDS


class _RuntimeApps:
    """The historical-``apps`` shim the data function expects, handing back
    the runtime models so the writes land on THIS test database."""

    _MODELS = {"Site": Site, "JobListing": JobListing}

    @classmethod
    def get_model(cls, app_label, model_name):
        assert app_label == "scraper"
        return cls._MODELS[model_name]


class TestMigration0046:
    @pytest.mark.django_db
    def test_duplicates_dedupe_lowest_id_wins_and_listings_repoint(
        self, _allow_duplicate_slugs
    ):
        dedupe_site_slugs, _merge = _dedupe_fn()

        keep = Site.objects.bulk_create([
            Site(url="https://dup.example.com/p/a", slug=SLUG, name="Dup Site",
                 platform="", has_scraper=False, default_scraper_path="",
                 output_schema={}, fields_extracted=[]),
            Site(url="https://dup.example.com/p/b", slug=SLUG, name="",
                 platform="shopify", has_scraper=True,
                 default_scraper_path=f"scrapers/{SLUG}/scraper.py",
                 output_schema={"content_type": "product"},
                 fields_extracted=["title"]),
            Site(url="https://dup.example.com/p/c", slug=SLUG, name="",
                 platform="magento", has_scraper=False,
                 default_scraper_path="", output_schema={}, fields_extracted=[]),
        ])
        keeper, rich, newest = keep
        moved = JobListing.objects.create(site=rich, site_slug=SLUG,
                                          title="on the dup")
        stayed = JobListing.objects.create(site=keeper, site_slug=SLUG,
                                           title="on the keeper")
        solo = Site.objects.create(url="https://solo.example.com/",
                                   slug="solo-example-com")
        Site.objects.create(url="https://blank1.example.com/", slug="")
        Site.objects.create(url="https://blank2.example.com/", slug="")

        dedupe_site_slugs(_RuntimeApps, None)

        rows = list(Site.objects.filter(slug=SLUG).order_by("id"))
        assert [r.pk for r in rows] == [keeper.pk]

        # The keeper's own non-blank values are never overwritten; its blanks
        # are filled from the rows it absorbed (the newest one loses).
        assert rows[0].name == "Dup Site"
        assert rows[0].platform == "shopify"
        assert rows[0].has_scraper is True
        assert rows[0].default_scraper_path == f"scrapers/{SLUG}/scraper.py"
        assert rows[0].output_schema == {"content_type": "product"}
        assert rows[0].fields_extracted == ["title"]

        # The only FK that references Site followed the keeper; no listing
        # rows were destroyed by the dedupe.
        assert JobListing.objects.get(pk=moved.pk).site_id == keeper.pk
        assert JobListing.objects.get(pk=stayed.pk).site_id == keeper.pk

        # A different slug and the blank-slug tolerance survive untouched.
        assert Site.objects.filter(pk=solo.pk).exists()
        assert Site.objects.filter(slug="").count() == 2

    @pytest.mark.django_db
    def test_an_idempotent_second_run_is_a_noop(self, _allow_duplicate_slugs):
        dedupe_site_slugs, _merge = _dedupe_fn()
        keeper, _absorbed = _plant_dup_sites(platform="shopify")
        dedupe_site_slugs(_RuntimeApps, None)

        dedupe_site_slugs(_RuntimeApps, None)

        assert [r.pk for r in Site.objects.filter(slug=SLUG)] == [keeper.pk]
        assert Site.objects.filter(slug="").count() == 0


class TestUniqueConstraint:
    @pytest.mark.django_db
    def test_a_second_row_with_a_used_slug_is_rejected(self):
        Site.objects.create(url="https://dup.example.com/p/a", slug=SLUG)

        with pytest.raises(IntegrityError):
            Site.objects.create(url="https://other.example.com/", slug=SLUG)

    @pytest.mark.django_db
    def test_blank_slugs_stay_legal(self):
        # An unparseable URL yields slug=""; that tolerance predates the
        # constraint, so the constraint is partial.
        Site.objects.create(url="https://blank1.example.com/", slug="")
        Site.objects.create(url="https://blank2.example.com/", slug="")
        assert Site.objects.filter(slug="").count() == 2


class TestModelDeclaresTheConstraint:
    def test_site_declares_the_unique_slug_constraint(self):
        named = {c.name: c for c in Site._meta.constraints}
        assert "site_slug_unique" in named
        constraint = named["site_slug_unique"]
        assert constraint.fields == ("slug",)
        # Partial on purpose: slug="" is the unparseable-URL case and was
        # legal long before this wave.
        assert constraint.condition is not None

    @pytest.mark.django_db
    def test_models_and_migrations_agree(self):
        # makemigrations --check reads django_migrations for consistency, so
        # this needs a DB — but it never writes one.
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command(
            "makemigrations", "scraper", "--check", "--dry-run",
            verbosity=0, stdout=out,
        )
        assert out.getvalue().strip() == "", out.getvalue()

    def test_the_migration_dedupes_before_it_constrains(self):
        # Load-bearing order: the data migration must run BEFORE the
        # constraint, or the first duplicate group aborts the deploy.
        module = importlib.import_module("scraper.migrations.0046_site_slug_unique")
        kinds = [type(op).__name__ for op in module.Migration.operations]
        assert kinds == ["RunPython", "AddConstraint"], kinds

    def test_the_migration_constrains_the_same_slug_the_model_does(self):
        from django.db import migrations as dj_migrations

        module = importlib.import_module("scraper.migrations.0046_site_slug_unique")
        declared = {c.name: c for c in Site._meta.constraints}["site_slug_unique"]
        migrated = next(
            op.constraint for op in module.Migration.operations
            if isinstance(op, dj_migrations.AddConstraint)
        )
        assert migrated.name == declared.name
        assert migrated.fields == declared.fields == ("slug",)
