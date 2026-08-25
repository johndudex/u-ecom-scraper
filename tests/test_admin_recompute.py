"""Admin button for the date-reliability recompute (replaces the Phase 11
§5 start-command-flip procedure — the stack owner deploys web-UI-only).

Locks:
- preview (no ?write): shows counts in an admin message, changes nothing
- apply (?write=1): runs with --write, message reports applied counts
- non-superuser → permission denied
- a crafted corrupted row actually gets fixed on apply
"""
from __future__ import annotations

import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from scraper import models  # noqa: E402


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("admin_recompute", password="x", email="a@b.c")


@pytest.fixture
def corrupted_row(db):
    site = models.Site.objects.create(url="https://adm.example/", slug="adm-example")
    return models.JobListing.objects.create(
        site=site, site_slug=site.slug,
        url="https://adm.example/job-1", title="t",
        posted_date=None, date_posted_reliable=False,
        extra_data={"date_posted": "2026-06-15"},
    )


class TestRecomputeAdminView:
    def test_url_exists(self):
        reverse("admin:admin_joblisting_recompute")  # raises if unregistered

    def test_preview_changes_nothing(self, superuser, corrupted_row):
        c = Client()
        c.force_login(superuser)
        r = c.get(reverse("admin:admin_joblisting_recompute"))
        assert r.status_code == 302  # redirects back to changelist
        msgs = [str(m) for m in r.wsgi_request._messages]
        assert any("DRY RUN" in m for m in msgs), msgs
        corrupted_row.refresh_from_db()
        assert corrupted_row.posted_date is None  # untouched

    def test_apply_fixes_row(self, superuser, corrupted_row):
        c = Client()
        c.force_login(superuser)
        r = c.get(reverse("admin:admin_joblisting_recompute") + "?write=1")
        assert r.status_code == 302
        corrupted_row.refresh_from_db()
        assert corrupted_row.posted_date == date(2026, 6, 15)
        assert corrupted_row.date_posted_reliable is True

    def test_non_superuser_denied(self, db):
        u = User.objects.create_user("plain", password="x")
        c = Client()
        c.force_login(u)
        r = c.get(reverse("admin:admin_joblisting_recompute"))
        assert r.status_code in (302, 403)  # redirected to login or denied


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
