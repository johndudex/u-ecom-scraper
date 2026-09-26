"""[wave-42e] Anti-bot bucket on the intake status board.

Operator ask (prod, 2026-09-26): sites whose latest attempt ended
captcha_blocked / akamai_blocked kept landing back in "can be retried" with
a fresh Retry button — but Retry just walks into the same wall. A wall is
not backlog: those rows must leave the retryable bucket (no Retry button,
excluded from the retryable tile, the ?retryable=1 filter and the CSV) and
get their own "anti-bot" bucket + tile + ?antibot=1 toggle instead.

Bucket rules:
- anti_bot = never succeeded AND latest attempt is captcha/akamai-blocked;
- a row that succeeded EARLIER but whose latest attempt was walled is
  neither anti-bot nor retryable (it has a success link, like any
  failed-after-success row);
- plain failed-latest rows stay retryable (regression pin);
- summary tiles stay computed over the UNFILTERED set; the new anti-bot
  tile follows the same rule.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_intake_status_antibot.py -q
"""
from __future__ import annotations

import csv
import io
import os
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


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("antibot_admin", password="x")


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
def wall_history(db):
    # W1: failed, then captcha-blocked on retry → anti-bot, NO retry button
    _job("https://walled.example/p/1", ScrapeJob.STATUS_FAILED,
         "Walled", days_ago=3)
    _job("https://walled.example/p/1", ScrapeJob.STATUS_CAPTCHA_BLOCKED,
         "Walled", days_ago=1)
    # W2: akamai-blocked on the only attempt → anti-bot, NO retry button
    _job("https://walled.example/p/2", ScrapeJob.STATUS_AKAMAI_BLOCKED,
         "Walled", days_ago=1)
    # W3: succeeded EARLIER, latest attempt walled → neither anti-bot nor
    # retryable (it has a success link)
    _job("https://lucky.example/p/3", ScrapeJob.STATUS_COMPLETED,
         "Lucky", days_ago=2)
    _job("https://lucky.example/p/3", ScrapeJob.STATUS_CAPTCHA_BLOCKED,
         "Lucky", days_ago=1)
    # W4: plain failed-latest, never succeeded → STILL retryable (pin)
    _job("https://hard.example/p/4", ScrapeJob.STATUS_FAILED,
         "Hard", days_ago=1)


def _row(html, needle):
    """The single <tr> containing needle (see test_intake_site_status)."""
    for frag in html.split("<tr>"):
        if needle in frag:
            for row in frag.split("</tr>"):
                if needle in row:
                    return row
    raise AssertionError(f"no table row containing {needle!r}")


class TestAntiBotBucket:
    def test_walled_rows_leave_retryable_tile(self, admin_client, wall_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        # retryable = ONLY the plain-failed row now; the two walled rows
        # moved into the new anti-bot tile
        assert 'data-summary="never_succeeded">1<' in html
        assert 'data-summary="antibot">2<' in html

    def test_walled_rows_have_no_retry_button(self, admin_client, wall_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        for purl in ("https://walled.example/p/1", "https://walled.example/p/2"):
            row = _row(html, purl)
            assert "/restart/" not in row, f"{purl} must not offer Retry"
        # ...while the plain-failed row keeps its button
        assert "/restart/" in _row(html, "https://hard.example/p/4")

    def test_success_then_walled_is_neither_bucket(self, admin_client, wall_history):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        row = _row(html, "https://lucky.example/p/3")
        assert "/restart/" not in row
        # success link still present (deep-links into the intake UI)
        assert "/intake/?job=" in row
        # and it is NOT counted in the anti-bot tile (only W1+W2 are)
        assert 'data-summary="antibot">2<' in html

    def test_failed_latest_row_still_retryable(self, admin_client, wall_history):
        # regression pin for the pre-existing retryable semantics
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        assert "/restart/" in _row(html, "https://hard.example/p/4")
        assert 'data-summary="never_succeeded">1<' in html


class TestAntiBotFilter:
    def test_antibot_param_shows_only_walled_rows(self, admin_client, wall_history):
        html = admin_client.get(
            reverse("intake_site_status"), {"antibot": "1"}
        ).content.decode()
        assert "https://walled.example/p/1" in html
        assert "https://walled.example/p/2" in html
        assert "https://hard.example/p/4" not in html
        assert "https://lucky.example/p/3" not in html

    def test_retryable_param_excludes_walled_rows(self, admin_client, wall_history):
        html = admin_client.get(
            reverse("intake_site_status"), {"retryable": "1"}
        ).content.decode()
        assert "https://hard.example/p/4" in html
        assert "https://walled.example/p/1" not in html
        assert "https://walled.example/p/2" not in html

    def test_tiles_stay_unfiltered(self, admin_client, wall_history):
        html = admin_client.get(
            reverse("intake_site_status"), {"antibot": "1"}
        ).content.decode()
        assert 'data-summary="products">4<' in html
        assert 'data-summary="antibot">2<' in html
        assert 'data-summary="never_succeeded">1<' in html


class TestAntiBotCsv:
    def test_unfiltered_csv_still_lists_walled_rows(self, admin_client, wall_history):
        r = admin_client.get(reverse("intake_site_status"), {"format": "csv"})
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        by_purl = {row[2]: row for row in rows[1:]}
        assert by_purl["https://walled.example/p/1"][4] == "captcha_blocked"
        assert by_purl["https://walled.example/p/2"][4] == "akamai_blocked"

    def test_retryable_csv_excludes_walled(self, admin_client, wall_history):
        r = admin_client.get(
            reverse("intake_site_status"), {"format": "csv", "retryable": "1"}
        )
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        purls = {row[2] for row in rows[1:]}
        assert purls == {"https://hard.example/p/4"}

    def test_antibot_csv_only_walled(self, admin_client, wall_history):
        r = admin_client.get(
            reverse("intake_site_status"), {"format": "csv", "antibot": "1"}
        )
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        purls = {row[2] for row in rows[1:]}
        assert purls == {"https://walled.example/p/1", "https://walled.example/p/2"}
