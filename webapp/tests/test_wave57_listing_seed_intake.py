"""[wave-57 class 1a] Intake stamps a LISTING-SEEDS warning on url_list
submissions whose every seed is a listing position (job #566 class).

Deliberate scope call: the intake layer WARNStamps honesty — it does not
flip input_mode to list_page. The #566 run PROVED the url_list lane extracts
fine behind listing seeds (913 item rows); flipping mode re-routes the job
through navigation_explore and risks the working lane. The semantic fix
lives in the runtime scope gate (tests/test_wave57_listing_seed_scope.py).
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from scraper.models import ScrapeJob

HOME = "https://www.jjshouse.com/"
LISTING_SEEDS = "\n".join([
    "https://www.jjshouse.com/all/bridesmaid-dresses",
    "https://www.jjshouse.com/collection/girls-dresses",
    "https://www.jjshouse.com/shop/accessories-shoes",
])
ITEM_SEEDS = "\n".join([
    "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
    "https://www.jjshouse.com/product/jjshouse-odetta-g293497",
])


class TestListingSeedIntakeWarning(TestCase):
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
        data = {
            "url": HOME,
            "nav_method": "list",
            "list_urls": LISTING_SEEDS,
        }
        data.update(over)
        with patch("scraper.tasks.run_scrape_task", MagicMock()):
            return self.client.post(
                reverse("intake_create_job"), data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_all_listing_seeds_stamped(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "url_list")
        self.assertIn("[LISTING-SEEDS]", job.notes or "")
        self.assertIn("listing_seeds", resp.json())

    def test_item_seeds_not_stamped(self):
        resp = self._post(list_urls=ITEM_SEEDS)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        job = ScrapeJob.objects.latest("id")
        self.assertNotIn("[LISTING-SEEDS]", job.notes or "")

    def test_mode_is_never_flipped(self):
        resp = self._post()
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "url_list")
        self.assertTrue(resp.json().get("listing_seeds", {}).get("warning"))
