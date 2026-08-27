"""Repair JobListing date data corrupted by the a66e33f parse bug.

From 2026-07-22 (a66e33f) until 2026-08-25 (31ae2f4), parse_posted_date
was terminated early by a pasted-in function — every JobListing created
in that window got posted_date=NULL + date_posted_reliable=False from
assess_date_reliability's ("missing") arm, regardless of the real date.

The raw date strings survive in extra_data.date_posted (the store node
keeps unknown fields; date_posted is the most common site spelling that
fell through to extra). This command re-derives posted_date + reliability
for the suspect window using the FIXED parser + the original P0-13 rules
(equals_scrape_date / future_dated stay unreliable-and-NULL).

Dry-run by default (reports counts); --write applies.
"""
from __future__ import annotations

import datetime as _dt

from django.core.management.base import BaseCommand
from django.utils import timezone

# a66e33f shipped 2026-07-22; 31ae2f4 (the fix) landed 2026-08-25.
BROKEN_FROM = _dt.datetime(2026, 7, 22, tzinfo=_dt.timezone.utc)
# No upper bound. scraped_at is auto_now_add, so a "fix-day end, inclusive"
# ceiling excluded every row created on/after that midnight: the command
# reported `scanned: 0` and exited 0 (two tests red) while claiming the
# window was repaired. The lower bound is what selects corrupted rows; the
# P0-13 rules below are what leave post-fix data alone (a surviving
# reliable=False is a genuine "unreliable date" verdict, not the bug).


def _raw_dates(listing):
    """Recover candidate raw date strings for a listing."""
    extra = listing.extra_data or {}
    cands = []
    for key in ("date_posted", "posted_date", "postedDate", "datePosted"):
        v = extra.get(key)
        if isinstance(v, str) and v.strip():
            cands.append(v.strip())
    return cands


class Command(BaseCommand):
    help = "Recompute posted_date + date_posted_reliability for JobListings corrupted by the a66e33f parse bug."

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true", help="apply (default: dry-run)")

    def handle(self, *args, **options):
        from scraper.models import JobListing
        from src.job_fields import parse_posted_date

        write = options["write"]
        qs = JobListing.objects.filter(
            scraped_at__gte=BROKEN_FROM,
            date_posted_reliable=False,
        )
        would_fix = 0
        unrecoverable = 0
        still_unreliable = 0
        batch = []
        scanned = 0
        for listing in qs.iterator(chunk_size=500):
            scanned += 1
            raws = _raw_dates(listing)
            if not raws:
                unrecoverable += 1
                continue
            parsed = None
            for raw in raws:
                # Anchor relative phrases ("3 days ago", "today") to the row's
                # own scrape instant, not the run clock — the recovered date
                # must not drift with how old the backlog is.
                parsed = parse_posted_date(raw, now=listing.scraped_at)
                if parsed is not None:
                    break
            if parsed is None:
                # raw strings exist but none parse — leave as-is (missing)
                unrecoverable += 1
                continue
            posted = parsed.date() if hasattr(parsed, "date") else parsed
            # P0-13 rules, evaluated against the ORIGINAL scrape date (the
            # day this row was first seen — scraped_at is first_seen_at)
            scrape_day = (
                listing.scraped_at.date()
                if timezone.is_aware(listing.scraped_at)
                else listing.scraped_at.date()
            )
            if posted == scrape_day:
                still_unreliable += 1  # equals_scrape_date — rule, not bug
                continue
            if posted > scrape_day:
                still_unreliable += 1  # future_dated — rule, not bug
                continue
            would_fix += 1
            if write:
                listing.posted_date = posted
                listing.date_posted_reliable = True
                listing.save(update_fields=["posted_date", "date_posted_reliable"])
        mode = "APPLIED" if write else "DRY RUN (pass --write to apply)"
        self.stdout.write(self.style.SUCCESS(f"recompute_date_reliability — {mode}"))
        self.stdout.write(f"  scanned (broken window, reliable=False): {scanned}")
        self.stdout.write(f"  would fix / fixed:                      {would_fix}")
        self.stdout.write(f"  correctly-still-unreliable (P0-13):     {still_unreliable}")
        self.stdout.write(f"  unrecoverable (no parsable raw date):   {unrecoverable}")
        if scanned and would_fix == 0:
            # The date-bomb's post-fix signature: it used to hide as
            # `scanned: 0` (the upper bound ate everything); unbounded it
            # hides as `scanned: N, would fix: 0`. Make both shapes loud.
            self.stdout.write(self.style.WARNING(
                f"  WARNING: {scanned} row(s) scanned but 0 would be fixed —"
                f" every raw date was unrecoverable or genuinely unreliable."
                f" If that is unexpected, the parser or the P0-13 rules changed."
            ))
