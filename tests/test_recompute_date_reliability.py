"""recompute_date_reliability management command (P0-13 data repair).

Context: a66e33f broke parse_posted_date for a month (pasted-in function
terminated it early) — every JobListing created 2026-07-22..2026-08-25 got
date_posted_reliable=False + posted_date=NULL despite valid raw dates.
The raw strings survive in extra_data (date_posted) and/or in the source
jobs' output JSONs in the File Master.

Locks:
- rows in the window with a recoverable date: posted_date set, reliability
  correctly assessed (equals_scrape_date/future/ok all classified)
- rows outside the window: untouched (control)
- unreliable dates (equals scrape date / future) keep posted_date NULL —
  the P0-13 rule, not the bug
- dry-run default: reports without writing; --write to apply
- idempotent: second run changes nothing
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import date, datetime, timedelta
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.core.management import call_command  # noqa: E402

from scraper import models  # noqa: E402

CUTOFF = datetime(2026, 7, 22)


@pytest.fixture
def window(db):
    """One listing inside the suspect window (created via auto_now_add NOW,
    so dates are forced post-cutoff), carrying a recoverable raw date."""
    site = models.Site.objects.create(url="https://rec.example/", slug="rec-example")
    job = models.ScrapeJob.objects.create(
        url="https://rec.example/j", status="completed",
        input_mode="url_list", page_type="job_posting",
    )
    return site, job


def _listing(site, job, raw_date, reliable=False, created=None):
    return models.JobListing.objects.create(
        site=site, site_slug=site.slug, scrape_job=job,
        url=f"https://rec.example/{os.urandom(4).hex()}",
        title="x", posted_date=None, date_posted_reliable=reliable,
        extra_data={"date_posted": raw_date},
    )


class TestRecompute:
    def test_recovers_valid_dates(self, window):
        site, job = window
        rows = [
            _listing(site, job, "2026-06-15"),                      # plain ISO → ok
            _listing(site, job, "06/10/2026"),                      # US format → ok
            _listing(site, job, "3 days ago"),                      # relative → ok
        ]
        out = io.StringIO()
        call_command("recompute_date_reliability", "--write", stdout=out)
        for r in rows:
            r.refresh_from_db()  # the ACTUAL row objects under assertion
        assert [r.posted_date for r in rows] == [
            date(2026, 6, 15),
            date(2026, 6, 10),
            (datetime.now() - timedelta(days=3)).date(),
        ]
        assert all(r.date_posted_reliable for r in rows)

    def test_unreliable_stays_null(self, window):
        """equals_scrape_date + future_dated keep posted_date NULL (P0-13
        rule, not the bug) and reliable=False."""
        site, job = window
        today = date.today().isoformat()
        future = (date.today() + timedelta(days=30)).isoformat()
        eq = _listing(site, job, today)
        fu = _listing(site, job, future)
        call_command("recompute_date_reliability", "--write", stdout=io.StringIO())
        eq.refresh_from_db(); fu.refresh_from_db()
        assert eq.posted_date is None and eq.date_posted_reliable is False
        assert fu.posted_date is None and fu.date_posted_reliable is False

    def test_dry_run_writes_nothing(self, window):
        site, job = window
        row = _listing(site, job, "2026-06-15")
        out = io.StringIO()
        call_command("recompute_date_reliability", stdout=out)  # no --write
        row.refresh_from_db()
        assert row.posted_date is None
        assert "would fix" in out.getvalue().lower() or "dry" in out.getvalue().lower()

    def test_outside_window_untouched(self, window):
        site, job = window
        row = _listing(site, job, "2026-06-15")
        models.JobListing.objects.filter(pk=row.pk).update(
            scraped_at=datetime(2026, 7, 1)  # pre-cutoff
        )
        call_command("recompute_date_reliability", "--write", stdout=io.StringIO())
        row.refresh_from_db()
        assert row.posted_date is None  # untouched

    def test_idempotent(self, window):
        site, job = window
        row = _listing(site, job, "2026-06-15")
        call_command("recompute_date_reliability", "--write", stdout=io.StringIO())
        first = models.JobListing.objects.get(pk=row.pk)
        call_command("recompute_date_reliability", "--write", stdout=io.StringIO())
        second = models.JobListing.objects.get(pk=row.pk)
        assert first.posted_date == second.posted_date
        assert first.date_posted_reliable == second.date_posted_reliable

    def test_row_without_raw_left_alone(self, window):
        site, job = window
        row = models.JobListing.objects.create(
            site=site, site_slug=site.slug, scrape_job=job,
            url=f"https://rec.example/{os.urandom(4).hex()}",
            posted_date=None, date_posted_reliable=False, extra_data={},
        )
        call_command("recompute_date_reliability", "--write", stdout=io.StringIO())
        row.refresh_from_db()
        assert row.posted_date is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
