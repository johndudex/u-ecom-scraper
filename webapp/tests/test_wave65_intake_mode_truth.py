"""[wave-65] Intake mode-truth — surface what the system changes, guard
what it can't honor.

Face A (prod 1417): wave-37 coerced a listing submission to PDP mode and
only told the job notes — the create response now carries the note as
``warning`` (the wave-55 M4 surface).
Face B (prod 1418): an amazon /s? search URL sailed through exact-list
mode as a product-URL seed and burned 2.5h to an honest pc=0 — search-
shaped seeds now 422 with an explicit-override escape. Category/collection
seeds are NOT touched (wave-57's deliberate 200+stamp contract, job 566's
913 rows).
Face C: runBuild's silent ``|| 'list'`` default is replaced by a loud
block (template pin).
"""

import os
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from scraper.models import ScrapeJob

from src.intake_coerce import looks_like_search_seed

# __file__-anchored (the full-suite run order is not CWD-safe — a chdir
# leaks upstream, which the first draft's relative Path() read tripped over)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PDP_URL = "https://www.replacements.com/crystal-lismore-set/p/133938771"
AMAZON_SEARCH = (
    "https://www.amazon.in/s?rh=n%3A1389401031%2Cp_123%3A110955"
    "&dc&qid=1791964550&rnid=91049095031&ref=sr_nr_p_123_0"
)
NORDSTROM_ITEM = "https://www.nordstrom.com/s/nordstrom-bleach-safe-flip-flops/3954158"
CATEGORY_SEEDS = "\n".join([
    "https://www.jjshouse.com/all/bridesmaid-dresses",
    "https://www.jjshouse.com/collection/girls-dresses",
    "https://www.jjshouse.com/shop/accessories-shoes",
])


class _IntakeHarness(TestCase):
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
            "url": "https://books.example.com/",
            "nav_method": "list",
            "list_urls": "https://books.example.com/products/the-book",
        }
        data.update(over)
        with patch("scraper.tasks.run_scrape_task", MagicMock()):
            return self.client.post(
                reverse("intake_create_job"), data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest")


class TestF1CoercionSurfaced(_IntakeHarness):
    def test_coerced_submission_returns_warning(self):
        resp = self._post(url=PDP_URL, nav_method="listing",
                          listing_urls="")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        j = resp.json()
        self.assertIn("warning", j)
        w = j["warning"]
        self.assertIn("coerced", w)
        self.assertIn("item page", w)
        # wave-65 guidance appended to the note
        self.assertIn("listing/catalog", w)
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "url_list")

    def test_clean_listing_submission_has_no_warning(self):
        resp = self._post(nav_method="listing",
                          listing_urls="https://books.example.com/catalogue/travel")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertNotIn("warning", resp.json())

    def test_fragment_warning_still_flows(self):
        # wave-55 M4 arm must keep working through the merged spread
        resp = self._post(
            nav_method="listing",
            listing_urls="https://books.example.com/catalogue/travel#page-2")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertIn("fragment", resp.json().get("warning", "").lower())


class TestF2UnitLooksLikeSearchSeed(TestCase):
    def test_search_shaped_urls(self):
        self.assertTrue(looks_like_search_seed(AMAZON_SEARCH))
        self.assertTrue(looks_like_search_seed("https://x.example.com/search?q=bags"))
        self.assertTrue(looks_like_search_seed("https://x.example.com/s?k=bags"))

    def test_item_and_category_urls_not_flagged(self):
        self.assertFalse(looks_like_search_seed(NORDSTROM_ITEM))
        self.assertFalse(looks_like_search_seed("https://x.example.com/products/widget"))
        self.assertFalse(looks_like_search_seed("https://x.example.com/collections/all"))
        self.assertFalse(looks_like_search_seed("https://x.example.com/c/94020"))
        self.assertFalse(looks_like_search_seed("https://x.example.com/"))
        self.assertFalse(looks_like_search_seed(""))


class TestF2SearchLookalikeGuard(_IntakeHarness):
    def test_amazon_search_seed_422_no_job(self):
        resp = self._post(url="https://www.amazon.in/", nav_method="list",
                          list_urls=AMAZON_SEARCH)
        self.assertEqual(resp.status_code, 422)
        j = resp.json()
        self.assertTrue(j.get("listing_lookalike"))
        self.assertIn("error", j)
        self.assertEqual(ScrapeJob.objects.count(), 0)

    def test_override_creates_job(self):
        resp = self._post(url="https://www.amazon.in/", nav_method="list",
                          list_urls=AMAZON_SEARCH, exact_list_override="1")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "url_list")

    def test_nordstrom_s_item_seed_not_flagged(self):
        # /s/<slug>/<digits> are real PDPs (nordstrom prod seeds)
        resp = self._post(url="https://www.nordstrom.com/",
                          nav_method="list", list_urls=NORDSTROM_ITEM)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertNotIn("listing_lookalike", resp.json())

    def test_category_seeds_still_200_and_stamped(self):
        # wave-57 contract intact: warn-stamp, never 422
        resp = self._post(list_urls=CATEGORY_SEEDS)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertIn("listing_seeds", resp.json())

    def test_listing_mode_with_search_url_untouched(self):
        resp = self._post(url=AMAZON_SEARCH, nav_method="listing",
                          listing_urls=AMAZON_SEARCH)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertNotIn("listing_lookalike", resp.json())
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "list_page")


class TestF3TemplatePins(TestCase):
    def test_intake_template_pins(self):
        template_path = os.path.join(
            ROOT, "webapp", "scraper", "templates", "scraper", "intake.html")
        with open(template_path, encoding="utf-8") as fh:
            html = fh.read()
        # F3: loud block instead of the silent 'list' default
        self.assertIn("Choose how we should find the pages", html)
        # F2: the lookalike branch precedes the generic error branch
        self.assertIn("listing_lookalike", html)
        # advisory 6: the warning banner is cleared on every submit
        self.assertIn("classList.add('hidden')", html)
