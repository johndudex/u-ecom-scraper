"""Dagster conversion is OPT-IN ([dagster-opt-in]).

The dagster_converter phase used to run unconditionally on every successful
job — burning LLM calls on a module nobody asked for (job 302: 34 calls /
7m05s of wall time). Now the job must explicitly opt in (intake checkbox /
partner-API flag / re-run carry-over); opted-out jobs skip the phase AND never
get a permanently-pending Step row for it in the pipeline UI.
"""

from unittest.mock import MagicMock, patch

from django.test import Client, TestCase
from django.urls import reverse
from model_bakery import baker

from scraper.models import ScrapeJob, Step
from scraper.tasks import PIPELINE_PHASES, _seed_pipeline_steps


class TestStepSeeding(TestCase):
    def test_opted_out_job_has_no_dagster_step(self):
        job = baker.make(ScrapeJob, dagster_enabled=False)
        _seed_pipeline_steps(job)
        phases = set(job.steps.values_list("phase", flat=True))
        self.assertNotIn("dagster_converter", phases)
        # sanity: the rest of the pipeline still seeds
        self.assertIn("execution", phases)

    def test_opted_in_job_seeds_dagster_step(self):
        job = baker.make(ScrapeJob, dagster_enabled=True)
        _seed_pipeline_steps(job)
        self.assertTrue(job.steps.filter(phase="dagster_converter").exists())

    def test_canonical_phase_list_keeps_dagster(self):
        # Only per-job SEEDING is filtered — the canonical list keeps the phase
        # (views._ordered_steps + integration tests read it for ordering).
        self.assertIn("dagster_converter", PIPELINE_PHASES)


class TestIntakeCreateJobOptIn(TestCase):
    """POST /intake/create-job/ — the checkbox posts '1' only when ticked."""

    URL = reverse("intake_create_job")

    def setUp(self):
        self.client = Client()

    def _post(self, **extra):
        data = {
            "url": "https://books.example.com/catalogue/a-light-in-the-attic",
            "nav_method": "list",
            "list_urls": "https://books.example.com/catalogue/a-light-in-the-attic",
        }
        data.update(extra)
        dispatch = MagicMock()
        dispatch.delay.return_value.id = "test-task-id"
        with patch("scraper.tasks.run_scrape_task", dispatch):
            return self.client.post(
                self.URL, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )

    def test_unticked_defaults_off(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        job = ScrapeJob.objects.get(id=resp.json()["job_id"])
        self.assertFalse(job.dagster_enabled)

    def test_ticked_opts_in(self):
        resp = self._post(dagster_enabled="1")
        self.assertEqual(resp.status_code, 200)
        job = ScrapeJob.objects.get(id=resp.json()["job_id"])
        self.assertTrue(job.dagster_enabled)


class TestRestartCarriesOptIn(TestCase):
    """Re-run carry-over: explicit POST value wins; bare re-run copies."""

    def setUp(self):
        self.client = Client()

    def _completed_job(self, **kw):
        d = dict(
            url="https://books.example.com/catalogue/a-light-in-the-attic",
            status=ScrapeJob.STATUS_COMPLETED,
            input_mode="url_list",
        )
        d.update(kw)
        return baker.make(ScrapeJob, **d)

    def _restart(self, job, **extra):
        dispatch = MagicMock()
        dispatch.delay.return_value.id = "test-task-id"
        with patch("scraper.tasks.run_scrape_task", dispatch):
            return self.client.post(
                reverse("job_restart", kwargs={"job_id": job.id}), extra
            )

    def test_bare_rerun_copies_opt_in(self):
        job = self._completed_job(dagster_enabled=True)
        resp = self._restart(job)
        self.assertEqual(resp.status_code, 302)
        new_job = ScrapeJob.objects.exclude(id=job.id).get()
        self.assertTrue(new_job.dagster_enabled)

    def test_bare_rerun_copies_opt_out(self):
        job = self._completed_job(dagster_enabled=False)
        resp = self._restart(job)
        self.assertEqual(resp.status_code, 302)
        new_job = ScrapeJob.objects.exclude(id=job.id).get()
        self.assertFalse(new_job.dagster_enabled)

    def test_explicit_post_value_wins(self):
        job = self._completed_job(dagster_enabled=True)
        resp = self._restart(job, dagster_enabled="0")
        self.assertEqual(resp.status_code, 302)
        new_job = ScrapeJob.objects.exclude(id=job.id).get()
        self.assertFalse(new_job.dagster_enabled)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
