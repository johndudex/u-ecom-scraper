"""[wave-41] job_api exposes the run's url_list seed (prod 876: deep-link
/intake/?job=876 showed an empty "exact urls" box because the ScrapeJob row
carries no URL list — intake persists pastes to scrapers/{slug}/input_urls.json
and the pipeline reads Site.input_urls first with the FM file as fallback).

Contract: ``input_urls`` (display-capped), ``input_urls_count``,
``input_urls_truncated`` mirror the pipeline's own resolution order; non-
url_list jobs report the zero shape."""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from model_bakery import baker
from scraper.models import ScrapeJob, Site
from scraper.views import INPUT_URLS_DISPLAY_CAP

PDP = "https://www.example.com/products/tee-1"
IKEY = "scrapers/example-com/input_urls.json"


class TestJobApiInputUrls(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        # Deterministic FM: no test may read the dev file-master tree.
        self.mock_fm_read = patch(
            "scraper.views._fm_read_json", MagicMock(return_value=None)
        ).start()
        self.addCleanup(patch.stopall)

    def _job(self, **over):
        job = baker.make(
            ScrapeJob,
            url=PDP,
            input_mode=over.pop("input_mode", "url_list"),
            site_folder="scrapers/example-com",
            user=self.user,
            **over,
        )
        return self.client.get(reverse("job_api", args=[job.id])).json()

    def test_site_input_urls_win(self):
        """Pipeline order: Site.input_urls first — file never consulted."""
        Site.objects.create(
            url=PDP, name="example", slug="example-com",
            input_urls=[f"{PDP}/{i}" for i in range(3)],
        )
        body = self._job()
        self.assertEqual(len(body["input_urls"]), 3)
        self.assertEqual(body["input_urls_count"], 3)
        self.assertFalse(body["input_urls_truncated"])
        self.mock_fm_read.assert_not_called()

    def test_fm_file_fallback_when_site_empty(self):
        data = {"urls": [PDP, "https://www.example.com/products/tee-2"]}
        self.mock_fm_read.side_effect = (
            lambda key: data if key == IKEY else None
        )
        body = self._job()
        self.assertEqual(body["input_urls"], data["urls"])
        self.assertEqual(body["input_urls_count"], 2)

    def test_malformed_file_payload_is_ignored(self):
        self.mock_fm_read.side_effect = (
            lambda key: ["not", "a", "dict"] if key == IKEY else None
        )
        body = self._job()
        self.assertEqual(body["input_urls"], [])
        self.assertEqual(body["input_urls_count"], 0)

    def test_non_url_list_jobs_report_zero_shape(self):
        body = self._job(input_mode="navigation")
        self.assertEqual(body["input_urls"], [])
        self.assertEqual(body["input_urls_count"], 0)
        self.assertFalse(body["input_urls_truncated"])

    def test_display_cap(self):
        urls = [f"{PDP}/{i}" for i in range(INPUT_URLS_DISPLAY_CAP + 5)]
        Site.objects.create(
            url=PDP, name="example", slug="example-com", input_urls=urls
        )
        body = self._job()
        self.assertEqual(len(body["input_urls"]), INPUT_URLS_DISPLAY_CAP)
        self.assertEqual(body["input_urls_count"], len(urls))
        self.assertTrue(body["input_urls_truncated"])
