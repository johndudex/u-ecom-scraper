"""[wave-50 T6b] Intake refuses a TERMLESS search job.

Job 492 (operator misfire): nav_method=search posted WITHOUT search_keywords
was accepted; the graph ran a search_term job with criteria='' — discovery
walked the UNFILTERED Algolia catalog (nbHits=228) and execution shipped
catalog-order rows as COMPLETED. The Partner API has refused this exact shape
since wave-27 (writers.py: ``search_term`` without search_criteria → 422);
the UI intake is the asymmetry. A URL-shaped search (search_url alone) stays
legal — the UI's deep-link re-run path posts it.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from scraper.models import ScrapeJob

HOME = "https://www.example.com/"


class TestTermlessSearchGuard(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        fm = patch("scraper.views._fm_exists", MagicMock(return_value=False))
        self.mock_fm_exists = fm.start()
        self.addCleanup(fm.stop)
        exists = patch("src.artifacts.exists", MagicMock(return_value=False))
        self.mock_artifact_exists = exists.start()
        self.addCleanup(exists.stop)
        write = patch("src.artifacts.write_json", MagicMock(), create=True)
        self.mock_write_json = write.start()
        self.addCleanup(write.stop)

    def _post(self, **over):
        data = {"url": HOME, "nav_method": "search"}
        data.update(over)
        with patch("scraper.tasks.run_scrape_task", MagicMock()):
            return self.client.post(
                reverse("intake_create_job"), data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_termless_search_is_422_and_creates_no_job(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 422)
        self.assertIn("missing_search_keywords", resp.json())
        self.assertEqual(ScrapeJob.objects.count(), 0)

    def test_blank_keywords_is_termless_too(self):
        resp = self._post(search_keywords="   ")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(ScrapeJob.objects.count(), 0)

    def test_search_url_alone_stays_legal(self):
        """The deep-link re-run path posts a URL-shaped search with no
        keywords — that is a concrete landing page, not a termless walk."""
        resp = self._post(search_url="https://www.example.com/search?q=x")
        self.assertNotEqual(resp.status_code, 422)
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "search_term")
        self.assertEqual(job.search_url, "https://www.example.com/search?q=x")

    def test_keywords_reach_the_job_row(self):
        resp = self._post(search_keywords="personalized book")
        self.assertNotEqual(resp.status_code, 422)
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "search_term")
        self.assertEqual(job.search_criteria, "personalized book")
