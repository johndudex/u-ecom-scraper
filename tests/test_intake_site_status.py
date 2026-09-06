"""[site-status] Prod-status dashboard — per site+product roll-up of all jobs.

The operator has been asking for a regenerated ``prod_job_status_by_site_product.csv``
after every campaign; this pins the dashboard that replaces that ritual:

- any logged-in user (opened up from superuser-only — read-only roll-up,
  no cross-user mutation; ``@login_required`` is the only gate);
- one row per (site, product_url): final status of the LATEST attempt,
  attempt count, succeeded-at-first-attempt, success job link;
- summary tiles: products, sites, per-status counts, first-try rate,
  never-succeeded count;
- ``?format=csv`` reproduces the exact CSV the operator was hand-building
  (same column names, same booleans);
- ``?status=`` filters on final status;
- /intake carries a Prod Status button visible to every logged-in user
  (the Maintenance button next to it stays superuser-only).
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys

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
from scraper.models import ScrapeJob  # noqa: E402

CSV_HEADER = [
    "site_name", "site_url", "product_url", "listing_search_page_urls",
    "final_status", "succeeded_at_first_attempt", "success_job_url",
]


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("status_admin", password="x")


@pytest.fixture
def admin_client(superuser):
    c = Client()
    c.force_login(superuser)
    return c


def _job(url, status, site_name="", days_ago=1, **kw):
    from django.utils import timezone

    j = ScrapeJob.objects.create(
        url=url, product_url=url, status=status, site_name=site_name, **kw
    )
    # created_at is auto_now_add — backdate for deterministic ordering
    ScrapeJob.objects.filter(pk=j.pk).update(
        created_at=timezone.now() - timezone.timedelta(days=days_ago)
    )
    return j


@pytest.fixture
def mixed_history(db):
    # Site A: first-try success
    _job("https://a.com/p/1", ScrapeJob.STATUS_COMPLETED, "Site A", days_ago=3)
    # Site A: failed once, then succeeded on retry → final completed, NOT first-try
    _job("https://a.com/p/2", ScrapeJob.STATUS_FAILED, "Site A", days_ago=3)
    _job("https://a.com/p/2", ScrapeJob.STATUS_COMPLETED, "Site A", days_ago=1)
    # Site B: never succeeded
    _job("https://b.com/p/3", ScrapeJob.STATUS_FAILED, "Site B", days_ago=2)
    _job("https://b.com/p/3", ScrapeJob.STATUS_FAILED, "Site B", days_ago=1)
    # Site B: still in flight
    _job("https://b.com/p/4", ScrapeJob.STATUS_RUNNING, "Site B", days_ago=1)


class TestPermissions:
    def test_regular_user_gets_200(self, regular_client):
        r = regular_client.get(reverse("intake_site_status"))
        assert r.status_code == 200

    def test_anonymous_redirects_to_login(self, db, settings):
        # The local dev stack injects DebugAutoLoginMiddleware (env
        # DEBUG_AUTO_LOGIN) which would mask the redirect — strip it to test
        # the production middleware stack.
        settings.MIDDLEWARE = [
            m for m in settings.MIDDLEWARE if "DebugAutoLogin" not in m
        ]
        r = Client().get(reverse("intake_site_status"))
        assert r.status_code == 302

    def test_superuser_gets_200(self, admin_client):
        r = admin_client.get(reverse("intake_site_status"))
        assert r.status_code == 200


class TestAggregation:
    def test_one_row_per_site_product_with_final_status_and_attempts(
        self, admin_client, mixed_history
    ):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        # a.com/p/2 has TWO attempts collapsed into ONE row, final completed
        assert html.count("https://a.com/p/2") == 1
        assert "https://a.com/p/1" in html and "https://b.com/p/4" in html

    def test_summary_counts(self, admin_client, mixed_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        # 4 products across 2 sites; final statuses: 2 completed, 1 failed,
        # 1 running; first-try: 1; never-succeeded: 1
        assert 'data-summary="products">4<' in html
        assert 'data-summary="sites">2<' in html
        assert 'data-summary="completed">2<' in html
        assert 'data-summary="failed">1<' in html
        assert 'data-summary="running">1<' in html
        assert 'data-summary="first_try">1<' in html
        assert 'data-summary="never_succeeded">1<' in html

    def test_first_try_flag_and_success_link(self, admin_client, mixed_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        # a.com/p/1: first-try yes; a.com/p/2: first-try no, success link to
        # its completed job
        row1 = _row(html, "https://a.com/p/1")
        assert "first-try" in row1
        row2 = _row(html, "https://a.com/p/2")
        assert "/jobs/" in row2


class TestCsvExport:
    def test_csv_header_matches_operator_csv(self, admin_client, mixed_history):
        r = admin_client.get(reverse("intake_site_status"), {"format": "csv"})
        assert r.status_code == 200
        assert r["Content-Type"].startswith("text/csv")
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        assert rows[0] == CSV_HEADER

    def test_csv_row_contents(self, admin_client, mixed_history):
        r = admin_client.get(reverse("intake_site_status"), {"format": "csv"})
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        by_purl = {row[2]: row for row in rows[1:]}
        # first-try success row — link points at ITS completed job
        # (id derived from DB: sequences don't reset between tests)
        first_job = ScrapeJob.objects.filter(
            url="https://a.com/p/1", status=ScrapeJob.STATUS_COMPLETED
        ).first()
        assert by_purl["https://a.com/p/1"][4] == "completed"
        assert by_purl["https://a.com/p/1"][5] == "true"
        assert by_purl["https://a.com/p/1"][6].endswith(f"/jobs/{first_job.id}/")
        # retry-then-succeed row: final completed, first-try false
        assert by_purl["https://a.com/p/2"][4] == "completed"
        assert by_purl["https://a.com/p/2"][5] == "false"
        # never-succeeded row: final failed, empty success link
        assert by_purl["https://b.com/p/3"][4] == "failed"
        assert by_purl["https://b.com/p/3"][5] == "false"
        assert by_purl["https://b.com/p/3"][6] == ""

    def test_status_filter_narrows_rows(self, admin_client, mixed_history):
        r = admin_client.get(
            reverse("intake_site_status"), {"format": "csv", "status": "completed"}
        )
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        finals = {row[4] for row in rows[1:]}
        assert finals == {"completed"}
        assert len(rows) - 1 == 2


def _row(html, needle):
    """The single <tr> containing needle. A regex anchored on the document's
    first <tr> bleeds NEIGHBOURING rows into the span (a failed row's Retry
    form leaking into a later row's extraction) — split instead."""
    for frag in html.split("<tr>"):
        if needle in frag:
            for row in frag.split("</tr>"):
                if needle in row:
                    return row
    raise AssertionError(f"no table row containing {needle!r}")


class TestSiteDedup:
    def test_site_name_drift_does_not_split_rows(self, admin_client, mixed_history):
        # The SAME product attempt recorded site_name "a.com" (domain
        # fallback, older job) and "Site A" (human Site name, newer job).
        # Grouping by per-job site_name used to emit TWO rows for one real
        # product — the operator's "adairs twice" complaint. Grouping must
        # key on the product_url's host; display name prefers the human one.
        _job("https://a.com/p/5", ScrapeJob.STATUS_FAILED, "a.com", days_ago=2)
        _job("https://a.com/p/5", ScrapeJob.STATUS_COMPLETED, "Site A", days_ago=1)
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        assert html.count("https://a.com/p/5") == 1
        row = _row(html, "https://a.com/p/5")
        assert "<td>Site A</td>" in row, "human site name must win over the domain fallback"
        assert "<td>a.com</td>" not in row
        assert ">completed</span>" in row, "final status comes from the LATEST attempt"

    def test_summary_sites_counts_real_sites_not_name_variants(
        self, admin_client, mixed_history
    ):
        # 4 products across hosts a.com + b.com — name drift must not inflate
        # the sites tile even when a site_name variant exists.
        _job("https://a.com/p/5", ScrapeJob.STATUS_FAILED, "a.com", days_ago=2)
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        assert 'data-summary="sites">2<' in html


class TestRetryButton:
    def test_retriable_rows_have_restart_form(self, admin_client, mixed_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        row = _row(html, "https://b.com/p/3")
        assert "/jobs/" in row and "/restart/" in row, "failed row needs a Retry action"
        assert "csrfmiddlewaretoken" in row, "retry must POST (prefetch-safe), not GET"
        assert ">retry<" in row.lower()

    def test_completed_and_inflight_rows_have_no_retry(self, admin_client, mixed_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        done = _row(html, "https://a.com/p/1")
        assert "/restart/" not in done, "completed rows must not offer Retry"
        inflight = _row(html, "https://b.com/p/4")
        assert "/restart/" not in inflight, "in-flight rows must not offer Retry"


class TestCanRetriedLabel:
    def test_tile_label_says_can_be_retried(self, admin_client, mixed_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode().lower()
        assert "can be retried" in html
        assert "never succeeded" not in html


class TestIntakeWiring:
    def test_intake_has_status_button_for_all_logged_in_users(self):
        src = open(
            os.path.join(
                ROOT, "webapp", "scraper", "templates", "scraper", "intake.html"
            )
        ).read()
        assert 'id="status-link"' in src, "topbar needs a Prod Status button"
        # NOT gated behind {% if is_superuser %} — every logged-in user sees
        # it. The anchor and any conditional must not share a line.
        line = next(ln for ln in src.splitlines() if "status-link" in ln)
        assert "is_superuser" not in line, (
            "the Prod Status button must not be superuser-gated"
        )
        assert "/intake/status/" in line

    def test_maintenance_button_stays_superuser_only(self):
        src = open(
            os.path.join(
                ROOT, "webapp", "scraper", "templates", "scraper", "intake.html"
            )
        ).read()
        assert 'id="maintenance-btn"' in src, "topbar needs a Maintenance button"
        m = re.search(
            r"\{% if is_superuser %\}[\s\S]*?maintenance-btn[\s\S]*?\{% endif %\}", src
        )
        assert m, "the Maintenance button must stay superuser-only"


# regular_user fixture shared with the maintenance test's pattern
@pytest.fixture
def regular_client(db):
    u = User.objects.create_user("status_peasant", password="x")
    c = Client()
    c.force_login(u)
    return c


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
