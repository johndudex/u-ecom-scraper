"""[wave-55 R12] Tier-3 #26 small fixes on the intake/job surface (views.py).

Three audit defects, one file:

- **M4** — a ``#`` fragment in a listing/search URL never survives to
  discovery: ``src/seed_urls.first_criteria_url`` hands the raw token on and
  nothing in ``webapp/`` or ``src/`` ever urldefrags, so job 1230's
  ``.../costumes#!/order=name&...`` facet was silently discarded before
  Phase 1 ran (the run then walked the unfiltered catalog into a 403 wall).
  Intake must WARN, not block — the bare path sometimes still resolves.
- **M5** — ``_resolve_job_slug`` reads only ``site_folder``/``site_name``,
  both empty until the graph sets them, so every PENDING row's CFG panel
  read ``input_urls_count=0`` while the FM held the 200-URL seed (jobs
  1226-1229/1234). Fall back to the ``_generate_slug(job.url)`` path intake
  itself used to write the seed.
- **H10** — the UI cancel saved only ``status``; the API cancel
  (``api/writers.cancel_job``) stamps ``completed_at`` too. Jobs 1199/1201
  stayed ``completed_at=NULL`` with a ``duration_seconds=None`` run history.

Round-32 gap on this file: the M4 warning had no CONSUMER — it rode
``job.notes`` (a column no template reads) and a ``warning`` key the intake
form discarded, so the operator the audit is about still saw nothing. The
intake form renders it on the job screen and the partner API returns it in
the 202 payload; both surfaces share the one ``_fragment_seed_warning``.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_intake_small_fixes.py -q"
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.test_settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402
from scraper.models import ScrapeJob, Site  # noqa: E402
from scraper.views import (  # noqa: E402
    _job_input_urls,
    _resolve_job_slug,
    intake_create_job,
    job_cancel,
)

RF = RequestFactory()


def _body(response) -> dict:
    """JsonResponse payload (RequestFactory responses have no .json())."""
    return json.loads(response.content)


def _admin():
    """RequestFactory bypasses middleware, so @login_required needs the user
    attached by hand — and intake_create_job stores ``user=request.user``, so
    it must be a real row, not a stand-in."""
    from django.contrib.auth import get_user_model

    user, _ = get_user_model().objects.get_or_create(
        username="intake-small-fixes-admin",
        defaults={"is_superuser": True, "is_staff": True},
    )
    return user


# ─── M4: intake warns (never blocks) on a '#' fragment in a listing URL ─────


def _post_listing_job(listing_urls: str, notes: str = ""):
    request = RF.post(
        "/intake/create-job/",
        {
            "url": "https://costumeparty.example.com/catalogue/costumes",
            "nav_method": "listing",
            "content_type": "product",
            "listing_urls": listing_urls,
            "notes": notes,
        },
    )
    request.user = _admin()
    request.headers = {"x-requested-with": "XMLHttpRequest"}
    return intake_create_job(request)


@pytest.mark.django_db
class TestFragmentWarning:
    def test_a_fragment_url_still_creates_the_job(self):
        """Warn, not block — the seed sometimes still works (audit M4)."""
        response = _post_listing_job(
            "https://costumeparty.example.com/costumes#!/order=name&p=1"
        )
        assert response.status_code == 200, response.content[:200]
        body = _body(response)
        assert body.get("job_id")
        job = ScrapeJob.objects.get(pk=body["job_id"])
        # The submitted criteria is stored verbatim — never rewritten.
        assert "#!/order=name" in job.search_criteria

    def test_the_warning_names_the_fragment_and_the_url(self):
        response = _post_listing_job(
            "https://costumeparty.example.com/costumes#!/order=name&p=1"
        )
        body = _body(response)
        warning = body.get("warning") or ""
        assert "fragment" in warning.lower()
        assert "costumes" in warning  # the offending token is identified

    def test_the_warning_is_persisted_on_the_job_row(self):
        """The row outlives the form — same channel as the W37-NEW-C
        coercion note (job.notes), so the loss is diagnosable post-hoc."""
        response = _post_listing_job(
            "https://costumeparty.example.com/costumes#!/order=name"
        )
        job = ScrapeJob.objects.get(pk=_body(response)["job_id"])
        assert "fragment" in (job.notes or "").lower()

    def test_a_clean_listing_url_gets_no_warning(self):
        response = _post_listing_job(
            "https://costumeparty.example.com/costumes?order=name&p=1",
            notes="existing operator note",
        )
        body = _body(response)
        assert "warning" not in body
        job = ScrapeJob.objects.get(pk=body["job_id"])
        assert job.notes == "existing operator note"

    def test_a_hash_in_search_keywords_is_not_a_fragment(self):
        """'#' inside search KEYWORDS is content ("C# books") — only URL
        tokens carry fragments (the seed_urls token rule)."""
        request = RF.post(
            "/intake/create-job/",
            {
                "url": "https://bookshop.example.com/catalogue/books",
                "nav_method": "search",
                "search_keywords": "C# programming books",
            },
        )
        request.user = _admin()
        request.headers = {"x-requested-with": "XMLHttpRequest"}
        response = intake_create_job(request)
        assert response.status_code == 200, response.content[:200]
        assert "warning" not in _body(response)


# ─── M4 gap: the warning reaches the operator (intake render + partner API) ─


class TestFragmentWarningHasAConsumer:
    """The row note and the JSON key were not enough: ``job.notes`` is read
    by no template and intake.html's ``submitBuild`` discarded ``warning``,
    so job 1230's operator still saw nothing (round-32 GAP 1)."""

    def _create(self, listing_urls: list[str], **extra) -> tuple:
        from django.contrib.auth.models import User
        from scraper.api.writers import create_job
        from scraper.models import ApiKey

        user = User.objects.create_user(
            username=f"_t_frag_{os.urandom(4).hex()}", password="x"
        )
        raw = "pk_test_" + os.urandom(16).hex()
        ApiKey.objects.create(
            user=user, prefix=raw[:8], key_hash=ApiKey.hash_key(raw)
        )
        request = RF.post(
            "/api/v1/jobs",
            data=json.dumps(
                {
                    "url": "https://costumeparty.example.com/p/1",
                    "input_mode": "list_page",
                    "content_type": "product",
                    "listing_urls": listing_urls,
                    "force": True,
                    **extra,
                }
            ),
            content_type="application/json",
            HTTP_X_API_KEY=raw,
        )
        request.api_user = user
        return create_job(request), user

    @pytest.mark.django_db
    def test_the_partner_surface_returns_the_warning_and_persists_it(self):
        response, user = self._create(
            ["https://costumeparty.example.com/costumes#!/order=name&p=1"]
        )
        assert response.status_code == 202, response.content[:200]
        body = json.loads(response.content)
        warnings = body.get("warnings") or []
        assert warnings and "fragment" in " ".join(warnings).lower(), (
            f"202 payload carries no fragment warning: {sorted(body)}"
        )
        job = ScrapeJob.objects.get(pk=body["job_id"], user=user)
        assert "#!/order=name" in job.search_criteria  # stored verbatim
        assert "fragment" in (job.notes or "").lower()  # and on the row

    @pytest.mark.django_db
    def test_the_partner_surface_is_quiet_for_clean_listing_urls(self):
        response, _user = self._create(
            ["https://costumeparty.example.com/costumes?order=name&p=1"]
        )
        assert response.status_code == 202
        assert "warnings" not in json.loads(response.content)

    def test_the_intake_form_renders_the_warning_on_the_job_screen(self):
        """submitBuild must CONSUME the key into a visible surface — and not
        via showUrlError, whose side effects (fields hidden, input flagged
        invalid, run gate re-checked) belong to the URL-check error lane."""
        template_path = os.path.join(
            ROOT, "webapp", "scraper", "templates", "scraper", "intake.html"
        )
        with open(template_path, encoding="utf-8") as fh:
            page = fh.read()
        start = page.index("function submitBuild(fd){")
        end = page.index("var dupFd = null;", start)
        handler = page[start:end]
        assert "data.warning" in handler, (
            "submitBuild still discards the fragment warning the API returns"
        )
        assert "showUrlError(data.warning)" not in handler
        assert 'id="build-warning"' in page, (
            "the intake job screen has no surface for the warning"
        )

    def test_both_surfaces_share_the_one_warning_builder(self):
        """views.py owns the rule; the API imports it — a second copy of the
        regex is how the two surfaces drift apart."""
        views_path = os.path.join(ROOT, "webapp", "scraper", "views.py")
        writers_path = os.path.join(
            ROOT, "webapp", "scraper", "api", "writers.py"
        )
        with open(views_path, encoding="utf-8") as fh:
            views_src = fh.read()
        with open(writers_path, encoding="utf-8") as fh:
            writers_src = fh.read()
        assert views_src.count("def _fragment_seed_warning(") == 1
        assert "_fragment_seed_warning(" in writers_src
        assert "re.findall" not in writers_src, (
            "api/writers.py re-implements the fragment rule instead of "
            "importing views._fragment_seed_warning"
        )


# ─── M5: a PENDING job resolves its slug from the submitted URL ─────────────


@pytest.mark.django_db
class TestResolveJobSlugFallback:
    def test_a_pending_job_with_no_site_fields_still_resolves_a_slug(self):
        job = ScrapeJob.objects.create(
            url="https://www.pending-example.com/p/1",
            input_mode="url_list",
            site_folder="",
            site_name="",
        )
        assert _resolve_job_slug(job) == "pending-example-com"

    def test_the_cfg_panel_reads_the_seed_for_a_pending_job(self):
        """The audit's surface: ``/jobs/<id>/api/`` reported
        input_urls_count=0 while the seed existed under the slug intake
        itself derived from the URL."""
        job = ScrapeJob.objects.create(
            url="https://pending-example.com/p/1",
            input_mode="url_list",
            site_folder="",
            site_name="",
        )
        Site.objects.create(
            url="https://pending-example.com",
            slug="pending-example-com",
            input_urls=[
                "https://pending-example.com/p/1",
                "https://pending-example.com/p/2",
            ],
        )
        seeded = _job_input_urls(job)
        assert seeded["input_urls_count"] == 2
        assert seeded["input_urls"] == [
            "https://pending-example.com/p/1",
            "https://pending-example.com/p/2",
        ]

    def test_an_explicit_site_folder_still_wins(self):
        job = ScrapeJob.objects.create(
            url="https://pending-example.com/p/1",
            site_folder="scrapers/explicit-slug",
            site_name="",
        )
        assert _resolve_job_slug(job) == "explicit-slug"

    def test_a_site_name_still_wins_over_the_url_fallback(self):
        job = ScrapeJob.objects.create(
            url="https://pending-example.com/p/1",
            site_folder="",
            site_name="Pending Example",
        )
        assert _resolve_job_slug(job) == "pending-example"


# ─── H10: the UI cancel stamps completed_at (parity with the API cancel) ────


@pytest.mark.django_db
class TestUiCancelStampsCompletedAt:
    def _cancel(self, status: str) -> ScrapeJob:
        job = ScrapeJob.objects.create(
            url="https://cancelme.example.com/p/1", status=status
        )
        request = RF.post(f"/jobs/{job.id}/cancel/")
        request.user = _admin()
        job_cancel(request, job.id)
        job.refresh_from_db()
        return job

    def test_a_running_cancel_stamps_completed_at(self):
        job = self._cancel(ScrapeJob.STATUS_RUNNING)
        assert job.status == ScrapeJob.STATUS_CANCELLED
        assert job.completed_at is not None
        assert abs(
            (dj_tz.now() - job.completed_at).total_seconds()
        ) < 60, "completed_at is the cancel time, not an epoch"

    def test_a_pending_cancel_stamps_completed_at(self):
        job = self._cancel(ScrapeJob.STATUS_PENDING)
        assert job.status == ScrapeJob.STATUS_CANCELLED
        assert job.completed_at is not None

    def test_a_terminal_job_is_not_touched(self):
        job = ScrapeJob.objects.create(
            url="https://done.example.com/p/1",
            status=ScrapeJob.STATUS_COMPLETED,
            completed_at=dj_tz.now() - dj_tz.timedelta(days=3),
        )
        request = RF.post(f"/jobs/{job.id}/cancel/")
        request.user = _admin()
        job_cancel(request, job.id)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.completed_at is not None
