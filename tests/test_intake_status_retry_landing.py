"""[wave-42c] Intake status-board Retry must land in the intake UI.

User report (prod, 2026-09-25): clicking Retry on /intake/status/ landed on
the legacy /jobs/<id>/ dashboard. Cause: the board's Retry is a plain
non-AJAX form POST (deliberately — /restart/ spawns a NEW job per call, so a
GET link would fire on prefetch), and job_restart's non-AJAX branch
redirects to job_detail — the legacy page.

Fix: the Retry form posts a ``from=intake_status`` marker; job_restart
redirects that caller to the intake dashboard deep-link
(/intake/?job=<id>) in BOTH exit branches (new-job success and the
not-restartable fallback). Every other caller is untouched:

- legacy job_detail/job_list GET Re-run links → /jobs/<id>/ (unchanged);
- intake.html AJAX re-run (X-Requested-With header) → JsonResponse
  (unchanged);
- bare non-AJAX POST without the marker → legacy redirect (unchanged).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_intake_status_retry_landing.py -q
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

import scraper.tasks as tasks_mod  # noqa: E402
from scraper.models import ScrapeJob  # noqa: E402

STATUS_BOARD = "intake_status"  # the template's hidden-marker value


@pytest.fixture
def client(db):
    user = User.objects.create_superuser("retry_admin", password="x")
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def completed_job(db):
    return ScrapeJob.objects.create(
        url="https://retry-landing.example/products/widget",
        product_url="https://retry-landing.example/products/widget",
        status=ScrapeJob.STATUS_COMPLETED,
        input_mode="list_page",
    )


@pytest.fixture
def captured_dispatch(monkeypatch):
    """Stub the publish (no broker in tests) and record the new job id."""
    seen = []
    monkeypatch.setattr(
        tasks_mod, "dispatch_scrape_job", lambda job_id, **kw: seen.append(job_id)
    )
    return seen


def _restart_url(job_id):
    return reverse("job_restart", args=[job_id])


class TestRetryLandsInIntakeUI:
    def test_status_board_retry_redirects_to_intake_dashboard(
        self, client, completed_job, captured_dispatch
    ):
        res = client.post(
            _restart_url(completed_job.id), {"from": STATUS_BOARD}
        )
        assert res.status_code == 302
        new_id = captured_dispatch[0]
        assert res["Location"] == f"{reverse('intake')}?job={new_id}"

    def test_bare_post_keeps_legacy_job_detail_redirect(
        self, client, completed_job, captured_dispatch
    ):
        """The legacy job_detail POST path (no marker) is byte-identical."""
        res = client.post(_restart_url(completed_job.id), {})
        assert res.status_code == 302
        new_id = captured_dispatch[0]
        assert res["Location"] == reverse("job_detail", args=[new_id])

    def test_legacy_get_link_keeps_legacy_redirect(
        self, client, completed_job, captured_dispatch
    ):
        """job_detail.html's Re-run is a GET link — unchanged behavior."""
        res = client.get(_restart_url(completed_job.id))
        assert res.status_code == 302
        new_id = captured_dispatch[0]
        assert res["Location"] == reverse("job_detail", args=[new_id])

    def test_not_restartable_marker_lands_on_intake_too(
        self, client, db, captured_dispatch
    ):
        """The 409/fallback branch honours the marker: RUNNING job → intake
        dashboard of the OLD job (not the legacy page)."""
        running = ScrapeJob.objects.create(
            url="https://retry-landing.example/products/other",
            product_url="https://retry-landing.example/products/other",
            status=ScrapeJob.STATUS_RUNNING,
            input_mode="list_page",
        )
        res = client.post(_restart_url(running.id), {"from": STATUS_BOARD})
        assert res.status_code == 302
        assert res["Location"] == f"{reverse('intake')}?job={running.id}"
        assert captured_dispatch == []  # nothing fired

    def test_ajax_branch_untouched_by_marker(
        self, client, completed_job, captured_dispatch
    ):
        """Intake re-run keeps its JSON contract even if a marker leaks in."""
        res = client.post(
            _restart_url(completed_job.id),
            {"from": STATUS_BOARD},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert res.status_code == 200
        assert res.json()["job_id"] == captured_dispatch[0]


class TestTemplatePinsMarker:
    def test_retry_form_carries_marker(self):
        """The board's Retry form must POST the marker (and stay POST-only)."""
        path = os.path.join(
            ROOT, "webapp", "scraper", "templates", "scraper",
            "intake_site_status.html",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert 'name="from" value="intake_status"' in src
        assert 'method="post"' in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
