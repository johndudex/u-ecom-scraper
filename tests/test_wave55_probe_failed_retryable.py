"""[wave-55 H1] A probe-failed verdict is retryable backlog, not anti-bot.

The 2026-10-05 audit: 27 hosts vanished from the retryable backlog because
their all-fail probe was labelled ``captcha_blocked`` — the wave-42e anti-bot
tile. A probe that fails every rung with zero captcha evidence is now a plain
STATUS_FAILED row with a ``[PROBE-FAILED n/m captcha]`` error prefix, so the
board must keep offering Retry for it, keep it out of the anti-bot bucket,
and say on the row that the failure was a probe failure.

No new status string: ``ScrapeJob.STATUS_CHOICES`` is Django-side validation
(migration 0045 is a pure AlterField — Postgres has no CHECK on the column),
but a status the terminal-status sets in tasks/sse/state don't know would be
re-driven by the watchdog, so the verdict rides the existing ``failed`` row.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest ../tests/test_wave55_probe_failed_retryable.py -q"
"""
from __future__ import annotations

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
from scraper.views import PROBE_FAILED_MSG_PREFIX, _site_status_rows  # noqa: E402

PROBE_FAILED_MSG = (
    "[PROBE-FAILED 0/15 captcha] all probe methods failed "
    "(direct_http http=403, cloak_residential http=403)"
)


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("w55_admin", password="x")


@pytest.fixture
def admin_client(superuser):
    c = Client()
    c.force_login(superuser)
    return c


def _job(url, status, error_message="", site_name="Probe", days_ago=1):
    from django.utils import timezone

    j = ScrapeJob.objects.create(
        url=url,
        product_url=url,
        status=status,
        site_name=site_name,
        error_message=error_message,
    )
    # created_at is auto_now_add — backdate for deterministic ordering
    ScrapeJob.objects.filter(pk=j.pk).update(
        created_at=timezone.now() - timezone.timedelta(days=days_ago)
    )
    return j


@pytest.fixture
def probe_history(db):
    # P1: the job-1220 shape — all-fail probe stamped as an honest failure
    _job("https://probed.example/p/1", ScrapeJob.STATUS_FAILED,
         PROBE_FAILED_MSG)
    # P2: a real captcha wall — stays in the anti-bot bucket
    _job("https://walled.example/p/2", ScrapeJob.STATUS_CAPTCHA_BLOCKED,
         "Captcha detected: turnstile. 15/15 probe methods returned captcha")
    # P3: a plain failure with no probe verdict — untouched semantics
    _job("https://plain.example/p/3", ScrapeJob.STATUS_FAILED,
         "testing cascade exhausted")


def _row(html, needle):
    for frag in html.split("<tr>"):
        if needle in frag:
            for row in frag.split("</tr>"):
                if needle in row:
                    return row
    raise AssertionError(f"no table row containing {needle!r}")


class TestProbeFailedRow:
    def test_probe_failed_row_is_flagged(self, probe_history):
        rows = {
            r["product_url"]: r
            for r in _site_status_rows()
        }
        row = rows["https://probed.example/p/1"]
        assert row["probe_failed"] is True
        assert row["anti_bot"] is False, (
            "zero captcha rungs is not an anti-bot wall"
        )
        assert row["never_succeeded"] is True, (
            "a probe failure is backlog the operator can re-drive"
        )

    def test_probe_failed_prefix_constant_matches_the_message(self):
        assert PROBE_FAILED_MSG_PREFIX == "[PROBE-FAILED"
        assert PROBE_FAILED_MSG.startswith(PROBE_FAILED_MSG_PREFIX)

    def test_other_failures_are_not_flagged(self, probe_history):
        rows = {r["product_url"]: r for r in _site_status_rows()}
        assert rows["https://plain.example/p/3"]["probe_failed"] is False
        assert rows["https://walled.example/p/2"]["probe_failed"] is False

    def test_probe_failed_row_keeps_the_retry_button(
        self, admin_client, probe_history
    ):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        assert "/restart/" in _row(html, "https://probed.example/p/1"), (
            "Retry must render for a probe-failed row"
        )

    def test_probe_failed_row_shows_the_probe_failed_badge(
        self, admin_client, probe_history
    ):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        row = _row(html, "https://probed.example/p/1")
        assert "probe-failed" in row
        # the anti-bot rows and the plain failure carry no such badge
        assert "probe-failed" not in _row(html, "https://walled.example/p/2")
        assert "probe-failed" not in _row(html, "https://plain.example/p/3")

    def test_probe_failed_row_stays_out_of_the_antibot_filter(
        self, admin_client, probe_history
    ):
        html = admin_client.get(
            reverse("intake_site_status"), {"antibot": "1"}
        ).content.decode()
        assert "https://probed.example/p/1" not in html
        assert "https://walled.example/p/2" in html

    def test_probe_failed_row_stays_in_the_retryable_filter(
        self, admin_client, probe_history
    ):
        html = admin_client.get(
            reverse("intake_site_status"), {"retryable": "1"}
        ).content.decode()
        assert "https://probed.example/p/1" in html
        assert "https://walled.example/p/2" not in html

    def test_antibot_tile_excludes_the_probe_failed_row(
        self, admin_client, probe_history
    ):
        html = admin_client.get(reverse("intake_site_status")).content.decode()
        assert 'data-summary="antibot">1<' in html
        assert 'data-summary="never_succeeded">2<' in html

    def test_csv_labels_the_probe_failed_row_as_failed(
        self, admin_client, probe_history
    ):
        """The audit's backfill shape: final_status stays 'failed' — there is
        no new status string to migrate."""
        import csv
        import io

        r = admin_client.get(reverse("intake_site_status"), {"format": "csv"})
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        by_purl = {row[2]: row for row in rows[1:]}
        assert by_purl["https://probed.example/p/1"][4] == "failed"


class TestProbeFailedRetryGate:
    """The retry gate itself: /restart/ accepts the probe-failed row (it is a
    plain FAILED row) and refuses the walled one — the exact split the audit
    said was inverted."""

    @pytest.fixture
    def captured_dispatch(self, monkeypatch):
        import scraper.tasks as tasks_mod

        seen = []
        monkeypatch.setattr(
            tasks_mod, "dispatch_scrape_job", lambda job_id, **kw: seen.append(job_id)
        )
        return seen

    def test_probe_failed_row_can_be_re_driven(
        self, admin_client, db, captured_dispatch
    ):
        from django.utils import timezone

        j = ScrapeJob.objects.create(
            url="https://redrive.example/p/9",
            product_url="https://redrive.example/p/9",
            status=ScrapeJob.STATUS_FAILED,
            input_mode="list_page",
            error_message=PROBE_FAILED_MSG,
        )
        ScrapeJob.objects.filter(pk=j.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=1)
        )
        res = admin_client.post(reverse("job_restart", args=[j.id]), {})
        assert res.status_code == 302
        assert captured_dispatch, (
            "an all-fail probe is a retryable failure — /restart/ must re-drive it"
        )

    def test_captcha_walled_row_is_still_refused(
        self, admin_client, db, captured_dispatch
    ):
        """The refused branch redirects without dispatching (the retry-landing
        contract) — a wall stays a wall."""
        j = ScrapeJob.objects.create(
            url="https://stillwalled.example/p/10",
            product_url="https://stillwalled.example/p/10",
            status=ScrapeJob.STATUS_CAPTCHA_BLOCKED,
            input_mode="list_page",
            error_message=(
                "Captcha detected: turnstile. 15/15 probe methods returned "
                "captcha pages."
            ),
        )
        res = admin_client.post(reverse("job_restart", args=[j.id]), {})
        assert res.status_code == 302  # legacy fallback redirect
        assert captured_dispatch == [], "a wall must not re-drive"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
