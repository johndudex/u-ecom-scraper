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
- rows scraped AFTER the fix landed are still scanned (scraped_at is
  auto_now_add, so any upper bound on the window silently excludes them)
- relative phrases ("3 days ago") resolve against the ROW's scraped_at, not
  the run clock — otherwise the recovered date drifts with backlog age
"""
from __future__ import annotations

import io
import json
import os
import re
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
from django.utils import timezone  # noqa: E402

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
            # relative phrase resolves against the row's OWN scrape instant,
            # not the clock the recompute ran on
            (rows[2].scraped_at - timedelta(days=3)).date(),
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

    def test_row_scraped_after_fix_day_is_included(self, window):
        """Date-bomb regression. scraped_at is auto_now_add, so a bounded
        window ("...until the fix landed, inclusive") silently excluded every
        row created on/after that midnight — the command reported
        `scanned: 0` and exited 0. A row scraped tomorrow must still be
        scanned and repaired."""
        site, job = window
        row = _listing(site, job, "2026-06-15")
        models.JobListing.objects.filter(pk=row.pk).update(
            scraped_at=timezone.now() + timedelta(days=1)
        )
        out = io.StringIO()
        call_command("recompute_date_reliability", "--write", stdout=out)
        row.refresh_from_db()
        assert row.posted_date == date(2026, 6, 15)
        assert row.date_posted_reliable is True
        assert "scanned" in out.getvalue()

    def test_relative_phrase_two_runs_write_same_value(self, window):
        """Relative phrases must resolve against the row's scraped_at, not the
        run clock: a row scraped 2026-07-23 carrying "3 days ago" means
        2026-07-20, and re-deriving it a week later must not move the answer.

        Locks idempotency for the one value class (relative phrases) that a
        clock-anchored implementation would write differently every run.
        """
        site, job = window
        row = _listing(site, job, "3 days ago")
        # Pin the scrape instant well inside the window and far from the run
        # date, so a now()-anchored parse writes a different date than a
        # scraped_at-anchored one.
        models.JobListing.objects.filter(pk=row.pk).update(
            scraped_at=timezone.make_aware(datetime(2026, 7, 23, 12, 0))
        )
        row.refresh_from_db()

        call_command("recompute_date_reliability", "--write", stdout=io.StringIO())
        row.refresh_from_db()
        first = row.posted_date
        assert first == (row.scraped_at - timedelta(days=3)).date()

        # The command only picks up reliable=False rows, so put the row back
        # into its corrupted state to make the second run re-derive it.
        models.JobListing.objects.filter(pk=row.pk).update(
            posted_date=None, date_posted_reliable=False
        )
        call_command("recompute_date_reliability", "--write", stdout=io.StringIO())
        row.refresh_from_db()
        assert row.posted_date == first
        assert row.date_posted_reliable is True

    def test_warns_when_rows_scanned_but_none_fixable(self, window):
        """Post-fix-day, the old failure signature (`scanned: 0`) becomes
        `scanned: N, would fix: 0`. That must be loud, not silent."""
        site, job = window
        # In the window, has a raw date, but nothing fixable (equals scrape
        # date → P0-13 keeps it unreliable).
        _listing(site, job, date.today().isoformat())
        out = io.StringIO()
        call_command("recompute_date_reliability", stdout=out)
        text = out.getvalue()
        assert re.search(r"scanned[^0-9]*[1-9]", text), text
        assert "warning" in text.lower(), text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
