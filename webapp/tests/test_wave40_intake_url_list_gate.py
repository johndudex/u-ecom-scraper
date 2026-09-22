"""[wave-40 T5] view layer: coerced PDP intake is seeded (job created,
url_list, URL stored); a truly-empty url_list intake returns 422 and creates
NO ScrapeJob row; job_restart refuses an unrecoverable url_list job."""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from model_bakery import baker
from scraper.models import ScrapeJob, Site

PDP = "https://www.example.com/products/tee-1"
HOME = "https://www.example.com/"


class TestIntakeUrlListGate(TestCase):
    """Intake view facts (verified): requires HTTP_X_REQUESTED_WITH=
    XMLHttpRequest (else 400); nav_method ABSENT on a PDP-shaped url
    coerces to url_list (W37-NEW-C)."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        # Deterministic File Master: the gate must not depend on the dev
        # file-master's contents, and no test may write to it.
        fm = patch("scraper.views._fm_exists", MagicMock(return_value=False))
        self.mock_fm_exists = fm.start()
        self.addCleanup(fm.stop)
        write = patch("src.artifacts.write_json", MagicMock(), create=True)
        self.mock_write_json = write.start()
        self.addCleanup(write.stop)

    def _post(self, **over):
        data = {"url": over.pop("url", PDP), "list_urls": over.pop("list_urls", "")}
        data.update(over)
        with patch("scraper.tasks.run_scrape_task", MagicMock()):
            return self.client.post(
                reverse("intake_create_job"), data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_coerced_pdp_is_seeded_not_rejected(self):
        resp = self._post()
        self.assertNotEqual(resp.status_code, 422)
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "url_list")
        # ADAPT the store, never the contract: the intake seeding branch
        # persists the list into scrapers/{slug}/input_urls.json (File
        # Master) — Site.input_urls stays empty at intake time.
        writes = {
            c.args[0]: c.args[1]
            for c in self.mock_write_json.call_args_list
            if len(c.args) >= 2
        }
        self.assertIn("scrapers/example-com/input_urls.json", writes)
        self.assertEqual(writes["scrapers/example-com/input_urls.json"],
                         {"urls": [PDP]})

    def test_truly_empty_url_list_is_422_and_no_job_row(self):
        resp = self._post(url=HOME)
        self.assertEqual(resp.status_code, 422)
        self.assertTrue(resp.json()["missing_url_list"])
        self.assertFalse(ScrapeJob.objects.filter(url=HOME).exists())

    def test_explicit_url_list_with_urls_passes(self):
        resp = self._post(
            list_urls="https://www.example.com/p/1\nhttps://www.example.com/p/2")
        self.assertNotEqual(resp.status_code, 422)
        self.assertTrue(ScrapeJob.objects.exists())

    def test_listing_mode_unaffected(self):
        resp = self._post(nav_method="listing",
                          list_urls="https://www.example.com/collections/all")
        self.assertNotEqual(resp.status_code, 422)
        self.assertTrue(ScrapeJob.objects.exists())

    def test_job_restart_blocked_when_url_list_unrecoverable(self):
        job = baker.make(ScrapeJob, url=HOME, input_mode="url_list",
                         status=ScrapeJob.STATUS_FAILED, user=self.user)
        with patch("scraper.tasks.run_scrape_task", MagicMock()):
            resp = self.client.post(
                reverse("job_restart", kwargs={"job_id": job.id}),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 422)
        self.assertTrue(resp.json()["missing_url_list"])


class TestPartnerApiEmptyItemUrls(TestCase):
    """The partner-API writer is the other url_list producer: an item list
    that dedupes to nothing must never yield a url_list job (the same
    instant-fail class — 422 like the missing-item_urls case)."""

    def setUp(self):
        import os

        from scraper import models

        self.user = User.objects.create_user(username="_t_t5_api", password="x")
        raw = "pk_test_" + os.urandom(16).hex()
        models.ApiKey.objects.create(
            user=self.user, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw),
        )
        self.raw_key = raw

    def test_blank_item_urls_are_422_not_a_job(self):
        import json

        from django.test import RequestFactory
        from scraper.api.writers import create_job

        req = RequestFactory().post(
            "/api/v1/jobs", data=json.dumps(
                {"url": PDP, "input_mode": "url_list", "item_urls": ["", "   "]}),
            content_type="application/json", HTTP_X_API_KEY=self.raw_key,
        )
        resp = create_job(req)
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(ScrapeJob.objects.exists())


class TestScrapeCommandUrlListGate(TestCase):
    """manage.py scrape cannot 422 — the same gate hard-exits (SystemExit)
    with the reason text BEFORE any row is created or task enqueued; a
    PDP-shaped url is still seeded (repair-on-coercion)."""

    def _command(self, url):
        from scraper.management.commands import scrape as scrape_cmd

        with patch("scraper.tasks.run_scrape_task", MagicMock()), patch(
            "src.artifacts.exists", MagicMock(return_value=False)
        ), patch("src.artifacts.write_json", MagicMock(), create=True):
            call_command(scrape_cmd.Command(), url, mode="url_list")

    def test_home_url_list_systemexits_with_no_row(self):
        with self.assertRaises(SystemExit):
            self._command(HOME)
        self.assertFalse(ScrapeJob.objects.exists())

    def test_pdp_url_list_is_seeded_and_created(self):
        self._command(PDP)
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "url_list")


class HermeticFmTestCase(TestCase):
    """Base for tests that touch Site rows with input_urls: Site.save() syncs
    scrapers/{slug}/input_urls.json to the File Master — never let a test
    read or write the dev file-master."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        exists = patch("src.artifacts.exists", MagicMock(return_value=False))
        exists.start()
        self.addCleanup(exists.stop)
        write = patch("src.artifacts.write_json", MagicMock(), create=True)
        self.mock_write_json = write.start()
        self.addCleanup(write.stop)


class TestSiteScrapeUrlListGate(HermeticFmTestCase):
    """site_scrape is the site-detail "Scrape" button: same repair+gate. A
    site with no URL list anywhere is refused; a PDP-shaped sample_url is
    seeded into the File Master."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("t5site", password="pw")
        self.client.force_login(self.user)

    def _scrape(self, site):
        with patch("scraper.views._fm_exists", MagicMock(return_value=False)), \
                patch("scraper.tasks.run_scrape_task", MagicMock()):
            return self.client.post(reverse("site_scrape", args=[site.id]))

    def test_site_without_any_url_list_is_422(self):
        site = Site.objects.create(
            url=HOME, name="t5-home", slug="t5-home", sample_url="")
        resp = self._scrape(site)
        self.assertEqual(resp.status_code, 422)
        self.assertTrue(resp.json()["missing_url_list"])
        self.assertFalse(ScrapeJob.objects.exists())

    def test_pdp_sample_url_is_seeded(self):
        site = Site.objects.create(
            url=HOME, name="t5-pdp", slug="t5-pdp", sample_url=PDP)
        resp = self._scrape(site)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ScrapeJob.objects.filter(url=HOME).exists())
        writes = {
            c.args[0]: c.args[1]
            for c in self.mock_write_json.call_args_list
            if len(c.args) >= 2
        }
        self.assertEqual(writes.get("scrapers/t5-pdp/input_urls.json"),
                         {"urls": [PDP]})

    def test_site_with_input_urls_unaffected(self):
        site = Site.objects.create(
            url=HOME, name="t5-list", slug="t5-list",
            input_urls=["https://www.example.com/p/1"])
        resp = self._scrape(site)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ScrapeJob.objects.filter(url=HOME).exists())
