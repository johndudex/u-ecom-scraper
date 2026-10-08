"""[wave-55 e2e follow-up] Honest intake when a url_list job carries no urls
of its own.

Job 564 (2026-10-08): a url_list submission whose seeds were posted under the
wrong field name sailed through intake (no 422 — ``missing_url_list_reason``
passes when the production input_urls.json exists), dispatch fell back to
that file's 10 STALE September urls, and the job shipped 10 rows the user
never submitted — COMPLETED, no visible indication. The C2 scope gate cannot
catch this: it is fed the same file-derived seeds, so stale matches stale.

Fix: intake KNOWS at creation time that this job will run on stored seeds
(list_urls empty after coercion + a seed source exists). Stamp
``[SEEDS-FROM-FILE]`` on job.notes with the actual source — the note travels
into state["user_notes"] (the pipeline sees it) and job_detail.html renders
job.notes (previously "a column nothing renders", round-10 GAP-4).
"""

import sys
from contextlib import ExitStack
from types import ModuleType
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from model_bakery import baker
from scraper.models import ScrapeJob, Site

PDP = "https://www.example.com/products/tee-1"
STALE = [f"https://www.example.com/products/old-{i}" for i in range(10)]


def _fake_fm_client():
    fake = ModuleType("src.artifacts")
    fake.scrapers_key = lambda slug, *parts: "/".join(["scrapers", slug, *parts])
    fake.exists = MagicMock(return_value=False)
    fake.read_json = MagicMock(return_value=None)
    fake.write_json = MagicMock()
    return fake


def _bind_fm_client(fake):
    import src as src_pkg

    stack = ExitStack()
    stack.enter_context(patch.dict(sys.modules, {"src.artifacts": fake}))
    stack.enter_context(patch.object(src_pkg, "artifacts", fake, create=True))
    return stack


class TestSeedsFromFileNote(TestCase):
    """A url_list intake whose submission holds no urls stamps the stored-seed
    provenance on the job row; a submission that carries its own urls does
    not."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)
        self.fm = patch("scraper.views._fm_exists", MagicMock(return_value=False))
        self.mock_fm_exists = self.fm.start()
        self.addCleanup(self.fm.stop)
        self.fake_fm = _fake_fm_client()
        self.addCleanup(_bind_fm_client(self.fake_fm).close)

    def _post(self, **over):
        data = {"url": over.pop("url", PDP), "list_urls": over.pop("list_urls", "")}
        data.update(over)
        with patch("scraper.tasks.run_scrape_task", MagicMock()):
            return self.client.post(
                reverse("intake_create_job"), data,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def _job(self):
        return ScrapeJob.objects.latest("id")

    def test_empty_list_with_fm_file_stamps_note(self):
        """The job-564 shape: no submitted urls, production file exists. The
        job must carry [SEEDS-FROM-FILE] saying it will run on stored
        seeds."""
        self.mock_fm_exists.return_value = True
        resp = self._post(nav_method="list")
        self.assertEqual(resp.status_code, 200)
        job = self._job()
        self.assertEqual(job.input_mode, "url_list")
        self.assertIn("[SEEDS-FROM-FILE]", job.notes)
        self.assertIn("input_urls.json", job.notes)

    def test_empty_list_with_site_row_seeds_stamps_note(self):
        """Same honesty for the other seed source: Site.input_urls holds the
        list, no FM file."""
        Site.objects.create(
            url=PDP, name="example", slug="example-com", input_urls=list(STALE),
        )
        resp = self._post(nav_method="list")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("[SEEDS-FROM-FILE]", self._job().notes)

    def test_submitted_list_does_not_stamp(self):
        """A pasted list is written verbatim for THIS job — no stored-seed
        caveat on a clean submission."""
        self.fake_fm.exists.return_value = True
        resp = self._post(
            nav_method="list", list_urls="https://www.example.com/p/a\n")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("[SEEDS-FROM-FILE]", self._job().notes)

    def test_coerced_pdp_that_seeds_itself_does_not_stamp(self):
        """A bare PDP with NO stored list gets a 1-URL repair seed — the
        submission IS the seed, nothing stale to disclose."""
        resp = self._post()  # PDP coerces to url_list, fm file absent
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("[SEEDS-FROM-FILE]", self._job().notes)

    def test_coerced_pdp_over_stored_list_stamps(self):
        """Bare PDP resubmission of an established site runs on the STORED
        list, not the submitted PDP — exactly what the note exists to
        disclose."""
        Site.objects.create(
            url=PDP, name="example", slug="example-com", input_urls=list(STALE),
        )
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("[SEEDS-FROM-FILE]", self._job().notes)

    def test_note_reaches_dispatch_user_notes(self):
        """The stamp is not DB decoration: _build_initial_state lines job.notes
        into state['user_notes'] so the pipeline itself sees the provenance."""
        from scraper.tasks import _build_initial_state

        self.mock_fm_exists.return_value = True
        self._post(nav_method="list")
        state = _build_initial_state(self._job())
        # user_notes is a single string (tasks.py joins job.notes lines).
        self.assertIn("[SEEDS-FROM-FILE]", state.get("user_notes") or "")


class TestNotesRenderedOnJobDetail(TestCase):
    """Round-10 GAP-4: 'notes is a column nothing renders.' The stamp only
    achieves user-visible honesty if the detail page shows job.notes."""

    def test_notes_panel_renders(self):
        self.client = Client()
        self.user = User.objects.create_user("staff2", password="pw")
        self.client.force_login(self.user)
        # _get_job scopes non-superusers to their own rows.
        job = baker.make(
            ScrapeJob, user=self.user, notes="[SEEDS-FROM-FILE] runs on stored seeds")
        resp = self.client.get(reverse("job_detail", kwargs={"job_id": job.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "[SEEDS-FROM-FILE]")

    def test_no_notes_no_panel_marker(self):
        self.client = Client()
        self.user = User.objects.create_user("staff3", password="pw")
        self.client.force_login(self.user)
        job = baker.make(ScrapeJob, user=self.user, notes="")
        resp = self.client.get(reverse("job_detail", kwargs={"job_id": job.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "[SEEDS-FROM-FILE]")
