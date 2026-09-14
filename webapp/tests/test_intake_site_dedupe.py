"""W31: site-level duplicate detection (docs/plans/wave31-dedupe-plan.md).

Identity = normalized host (src.seed_urls.normalize_host) — NOT the raw URL
(every teammate pastes a different sample item page) and NOT Site.slug
(collision-prone; the Site row may not exist until mid-job). The gate fires
only on prior COMPLETED jobs (failed/parked attempts never block — product
decision 2026-09-14) and is exempt for archived Sites (archive = deliberate
allow-re-scrape).
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from model_bakery import baker
from scraper.dedupe import site_processing_history
from scraper.models import ScrapeJob, Site


class TestSiteProcessingHistory(TestCase):
    def _job(self, url, status=ScrapeJob.STATUS_COMPLETED, **kw):
        return baker.make(ScrapeJob, url=url, status=status, input_mode="url_list", **kw)

    def test_empty_host_returns_blank_history(self):
        # Intake only requires a non-empty string (views.py:2906); a garbage
        # URL must not gate — url__icontains="" would match the whole table.
        self.assertEqual(
            site_processing_history("not a url"),
            {"host": "", "site_slug": "", "processed": False, "has_scraper": False,
             "site_archived": False, "prior_jobs": [], "attempt_count": 0},
        )

    def test_completed_prior_gates_across_url_variants(self):
        self._job("https://www.example.com/p/one-product", title="First")
        h = site_processing_history("https://example.com/p/totally-different")
        self.assertTrue(h["processed"])
        self.assertEqual(h["host"], "example.com")
        self.assertEqual(h["attempt_count"], 1)

    def test_failed_prior_never_gates(self):
        self._job("https://www.example.com/p/1", status=ScrapeJob.STATUS_FAILED)
        h = site_processing_history("https://example.com/p/2")
        self.assertFalse(h["processed"])
        self.assertEqual(h["attempt_count"], 1)  # context only, not a gate

    def test_waiting_approval_prior_never_gates(self):
        # The realistic prod park state — jobs sit at waiting_approval for hours.
        self._job("https://www.example.com/p/1", status=ScrapeJob.STATUS_WAITING_APPROVAL)
        self.assertFalse(site_processing_history("https://example.com/p/2")["processed"])

    def test_www_folds_but_subdomains_stay_distinct(self):
        self._job("https://www.shop.example.com/p/1")
        self.assertTrue(site_processing_history("https://shop.example.com/p/9")["processed"])
        self.assertFalse(site_processing_history("https://other.example.com/p/9")["processed"])

    def test_substring_trap_jo_com_vs_jo_com_au(self):
        # url__icontains("jo.com") matches "jo.com.au/..." — the exact-host
        # refine must reject it.
        self._job("https://jo.com.au/p/1")
        self.assertFalse(site_processing_history("https://jo.com/p/2")["processed"])

    def test_prior_jobs_newest_first_capped_with_job_urls(self):
        for i in range(7):
            self._job(f"https://example.com/p/{i}")
        h = site_processing_history("https://www.example.com/p/new", limit=5)
        self.assertEqual(len(h["prior_jobs"]), 5)
        self.assertEqual(h["attempt_count"], 7)
        ids = [j["job_id"] for j in h["prior_jobs"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        for j in h["prior_jobs"]:
            self.assertEqual(j["job_url"], reverse("intake") + f"?job={j['job_id']}")
            for key in ("title", "status", "item_count", "created_at", "owner_username"):
                self.assertIn(key, j)

    def test_has_scraper_and_archived_read_from_site_row(self):
        self._job("https://example.com/p/1")
        baker.make(Site, url="https://example.com/p/1", slug="example-com",
                   has_scraper=True, archived_at=None)
        h = site_processing_history("https://example.com/p/2")
        self.assertTrue(h["has_scraper"])
        self.assertFalse(h["site_archived"])
        self.assertEqual(h["site_slug"], "example-com")

    def test_archived_site_flagged(self):
        from django.utils import timezone

        self._job("https://example.com/p/1")
        baker.make(Site, url="https://example.com/p/1", slug="example-com",
                   archived_at=timezone.now())
        h = site_processing_history("https://example.com/p/2")
        self.assertTrue(h["site_archived"])


class TestIntakeCreateJobSiteGate(TestCase):
    """POST /intake/create-job/ — the tier-2 site gate.

    Team-wide (any teammate's completed job gates), completed-only, archived
    sites exempt, force=1 click-through that never bypasses the tier-1
    live-job 409.
    """

    URL = reverse("intake_create_job")
    POST_URL = "https://books.example.com/catalogue/some-item"

    def setUp(self):
        self.client = Client()
        self.me = User.objects.filter(is_superuser=True).order_by("pk").first()

    def _post(self, **extra):
        data = {"url": self.POST_URL}
        data.update(extra)
        dispatch = MagicMock()
        dispatch.delay.return_value.id = "test-task-id"
        with patch("scraper.tasks.run_scrape_task", dispatch):
            return self.client.post(
                self.URL, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )

    def _completed_prior(self, **kw):
        return baker.make(
            ScrapeJob,
            url="https://www.books.example.com/catalogue/other-item",
            status=ScrapeJob.STATUS_COMPLETED, input_mode="url_list",
            product_count=40, **kw,
        )

    def test_409_when_site_processed(self):
        prior = self._completed_prior()
        r = self._post()
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertTrue(d["duplicate"])
        self.assertEqual(d["scope"], "site")
        self.assertEqual(d["prior_jobs"][0]["job_id"], prior.id)
        self.assertFalse(d["prior_jobs"][0]["is_own"])
        self.assertEqual(ScrapeJob.objects.count(), 1)  # nothing created

    def test_force_creates_job(self):
        self._completed_prior()
        r = self._post(force="1")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("job_id", d)
        self.assertEqual(ScrapeJob.objects.count(), 2)

    def test_running_guard_wins_and_force_ignored(self):
        baker.make(ScrapeJob, url=self.POST_URL, status=ScrapeJob.STATUS_RUNNING,
                   user=self.me, input_mode="url_list")
        r = self._post(force="1")
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertEqual(d["scope"], "url")
        self.assertNotIn("prior_jobs", d)

    def test_new_site_unaffected(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)

    def test_payload_job_urls(self):
        prior = self._completed_prior()
        d = self._post().json()
        self.assertEqual(d["prior_jobs"][0]["job_url"], reverse("intake") + f"?job={prior.id}")

    def test_team_wide_other_users_completed_gates(self):
        other = User.objects.create_user(username="_t_teammate", password="x")
        self._completed_prior(user=other)
        d = self._post().json()
        self.assertEqual(d["scope"], "site")
        self.assertFalse(d["prior_jobs"][0]["is_own"])

    def test_is_own_true_for_own_completed(self):
        self._completed_prior(user=self.me)
        d = self._post().json()
        self.assertTrue(d["prior_jobs"][0]["is_own"])

    def test_force_with_no_prior_still_creates(self):
        r = self._post(force="1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ScrapeJob.objects.count(), 1)
