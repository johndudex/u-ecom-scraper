"""W32-B3: host gate at intake AND restart — declines exactly what F17
would drop (docs/plans/wave32-fix-plan.md).

587: intake accepted a loveamika.com listing on a marimekko.com job; the
pipeline's F17 seed filter silently dropped every cross-host listing URL at
runtime, discovery starved, and the job burned 2h22m to a writer wall. The
gate declines at the door with 422 instead. Comparator = the pipeline's own
registrable-domain comparison (run_execution._registrable_of, promoted to
src.registrable) — subdomains (shop.marimekko.com) and www fold to the same
registrable domain and are ACCEPTED, matching F17.
"""
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from model_bakery import baker
from scraper.models import ScrapeJob

JOB_URL = "https://www.marimekko.com/us_en/harhautus-unikko-cardigan"
CROSS_HOST_LISTING = "https://www.loveamika.com/collections/all"
SAME_HOST_LISTING = "https://shop.marimekko.com/us_en/c/knitwear"


class TestRegistrableOf(TestCase):
    """The promoted comparator (pure unit — no views)."""

    def test_matches_pipeline_helper_semantics(self):
        from src.registrable import registrable_of

        self.assertEqual(registrable_of(JOB_URL), "marimekko.com")
        self.assertEqual(registrable_of("https://shop.marimekko.com/c/x"), "marimekko.com")
        self.assertEqual(registrable_of("https://www.loveamika.com/a"), "loveamika.com")
        # two-part TLD
        self.assertEqual(registrable_of("https://shop.jomashop.co.uk/x"), "jomashop.co.uk")
        self.assertEqual(registrable_of("not a url"), "")

    def test_cross_host_detect(self):
        from src.registrable import registrable_of

        self.assertNotEqual(registrable_of(JOB_URL), registrable_of(CROSS_HOST_LISTING))
        self.assertEqual(registrable_of(JOB_URL), registrable_of(SAME_HOST_LISTING))


class TestIntakeHostGate(TestCase):
    """POST /intake/create-job/ — cross-host listing/search URLs → 422."""

    URL = reverse("intake_create_job")

    def setUp(self):
        self.client = Client()

    def _post(self, **extra):
        # [wave-40 T5] non-empty list_urls: an empty url_list payload is 422'd
        # at intake now, and these tests are about the host gate.
        # [T5 r1] hermetic FM: never read/write the dev file-master.
        data = {"url": JOB_URL, "list_urls": JOB_URL}
        data.update(extra)
        dispatch = MagicMock()
        dispatch.delay.return_value.id = "test-task-id"
        with patch("scraper.tasks.run_scrape_task", dispatch), patch(
            "src.artifacts.exists", MagicMock(return_value=False), create=True
        ), patch("src.artifacts.write_json", MagicMock(), create=True):
            return self.client.post(
                self.URL, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )

    def test_intake_rejects_cross_host_listing(self):
        r = self._post(nav_method="listing", listing_urls=CROSS_HOST_LISTING)
        self.assertEqual(r.status_code, 422)
        d = r.json()
        self.assertIn("loveamika.com", d["error"])
        self.assertEqual(ScrapeJob.objects.count(), 0)

    def test_intake_cross_host_with_force_still_422(self):
        """No force escape: the pipeline DROPS these URLs today — declining
        is strictly honest, there is nothing to click through."""
        r = self._post(nav_method="listing", listing_urls=CROSS_HOST_LISTING,
                       force="1")
        self.assertEqual(r.status_code, 422)

    def test_intake_accepts_subdomain_and_www_listing(self):
        r = self._post(nav_method="listing",
                       listing_urls=f"{SAME_HOST_LISTING}\nhttps://marimekko.com/us_en/c/dresses")
        self.assertEqual(r.status_code, 200, r.content)

    def test_intake_rejects_cross_host_search_url(self):
        r = self._post(nav_method="search", search_keywords="knit dress",
                       search_url=CROSS_HOST_LISTING + "?q=knit")
        self.assertEqual(r.status_code, 422)

    def test_intake_search_without_url_unaffected(self):
        r = self._post(nav_method="search", search_keywords="knit dress")
        self.assertEqual(r.status_code, 200, r.content)

    def test_intake_url_list_mode_unaffected_by_gate(self):
        """url_list seeds FROM the sample url — F17 does not filter item
        seeds the same way, and the gate is nav-only."""
        r = self._post()
        self.assertEqual(r.status_code, 200, r.content)


class TestPartnerApiHostGate(TestCase):
    """POST /api/v1/jobs — list_page listings cross-host → 422 host_mismatch."""

    def setUp(self):
        import os

        from scraper import models
        from scraper.api.writers import create_job

        self.create_job = create_job
        self.rf = RequestFactory()
        self.user = User.objects.create_user(username="_t_gate", password="x")
        raw = "pk_test_" + os.urandom(16).hex()
        models.ApiKey.objects.create(
            user=self.user, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw),
        )
        self.raw_key = raw

    def _post(self, body):
        req = self.rf.post(
            "/api/v1/jobs", data=json.dumps(body),
            content_type="application/json", HTTP_X_API_KEY=self.raw_key,
        )
        return self.create_job(req)

    def test_partner_api_422_host_mismatch(self):
        r = self._post({
            "url": JOB_URL, "input_mode": "list_page",
            "listing_urls": [CROSS_HOST_LISTING, SAME_HOST_LISTING],
        })
        self.assertEqual(r.status_code, 422)
        d = json.loads(r.content)
        self.assertEqual(d["code"], "host_mismatch")
        offending = d["details"]["offending_urls"]
        self.assertEqual(len(offending), 1)
        self.assertIn("loveamika.com", offending[0])

    def test_partner_api_accepts_same_registrable(self):
        r = self._post({
            "url": JOB_URL, "input_mode": "list_page",
            "listing_urls": [SAME_HOST_LISTING],
        })
        self.assertEqual(r.status_code, 202)

    def test_partner_api_search_term_has_no_urls_to_check(self):
        """search_keywords are words, not URLs — nothing for the gate to
        check (the intake search_url field has no partner-API twin)."""
        r = self._post({
            "url": JOB_URL, "input_mode": "search_term",
            "search_keywords": "knit dress loveamika",
        })
        self.assertEqual(r.status_code, 202)


class TestRestartHostGate(TestCase):
    """POST /jobs/<id>/restart/ — the LIVE hole: intake Re-run posts the
    edited config here, so a fresh cross-host search_criteria enters past
    any intake-only gate."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="_t_restart", password="x")
        self.client.force_login(self.user)
        self.job = baker.make(
            ScrapeJob, url=JOB_URL, status=ScrapeJob.STATUS_COMPLETED,
            input_mode="list_page", search_criteria=SAME_HOST_LISTING,
            user=self.user,
        )
        self.URL = reverse("job_restart", args=[self.job.id])

    def _post(self, **extra):
        data = {"prompt": "redo"}
        data.update(extra)
        dispatch = MagicMock()
        dispatch.delay.return_value.id = "test-task-id"
        with patch("scraper.tasks.run_scrape_task", dispatch):
            return self.client.post(
                self.URL, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )

    def test_restart_rejects_cross_host_criteria(self):
        r = self._post(search_criteria=CROSS_HOST_LISTING)
        self.assertEqual(r.status_code, 422)
        self.assertIn("loveamika.com", r.json()["error"])
        self.assertEqual(ScrapeJob.objects.count(), 1)

    def test_restart_inherited_criteria_not_rechecked(self):
        """No POST key → the old job's (clean) config carries; the gate does
        not re-check inherited values."""
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ScrapeJob.objects.count(), 2)

    def test_restart_accepts_same_host_criteria(self):
        r = self._post(search_criteria=SAME_HOST_LISTING)
        self.assertEqual(r.status_code, 200)
