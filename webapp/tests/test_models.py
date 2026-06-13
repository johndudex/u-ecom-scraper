from model_bakery import baker
from django.test import TestCase

from scraper.models import Approval, ScrapeJob, Step


class TestScrapeJobModel(TestCase):
    def test_create_job_defaults(self):
        job = baker.make(ScrapeJob, url="https://example.com")
        self.assertEqual(job.status, ScrapeJob.STATUS_PENDING)
        self.assertEqual(job.product_count, 0)
        self.assertTrue(job.id)

    def test_duration_seconds_no_dates(self):
        job = baker.make(ScrapeJob)
        self.assertEqual(job.duration_seconds, 0)

    def test_duration_seconds_with_dates(self):
        job = baker.make(
            ScrapeJob,
            _fill_optional=["started_at", "completed_at"],
        )
        self.assertGreater(job.duration_seconds, 0)

    def test_str(self):
        job = baker.make(ScrapeJob, url="https://example.com", status="running")
        self.assertIn("example.com", str(job))
        self.assertIn("running", str(job))

    def test_status_choices(self):
        job = baker.make(ScrapeJob)
        for value, _ in ScrapeJob.STATUS_CHOICES:
            job.status = value
            job.save()
            job.refresh_from_db()
            self.assertEqual(job.status, value)

    def test_ordering(self):
        job1 = baker.make(ScrapeJob, url="https://first.com")
        job2 = baker.make(ScrapeJob, url="https://second.com")
        jobs = list(ScrapeJob.objects.all())
        self.assertEqual(jobs[0].id, job2.id)
        self.assertEqual(jobs[1].id, job1.id)


class TestStepModel(TestCase):
    def test_create_step(self):
        job = baker.make(ScrapeJob)
        step = baker.make(Step, job=job, phase="site_analysis")
        self.assertEqual(step.phase, "site_analysis")
        self.assertEqual(step.status, Step.STATUS_PENDING)

    def test_step_ordering(self):
        job = baker.make(ScrapeJob)
        step1 = baker.make(Step, job=job, phase="site_analysis")
        step2 = baker.make(Step, job=job, phase="product_analysis")
        steps = list(job.steps.all())
        self.assertEqual(steps[0].id, step1.id)
        self.assertEqual(steps[1].id, step2.id)

    def test_str(self):
        job = baker.make(ScrapeJob)
        step = baker.make(Step, job=job, phase="site_analysis", status="done")
        self.assertIn("site_analysis", str(step))


class TestApprovalModel(TestCase):
    def test_create_approval(self):
        job = baker.make(ScrapeJob)
        approval = baker.make(Approval, job=job, approval_type="field_confirm")
        self.assertEqual(approval.status, Approval.STATUS_PENDING)

    def test_approve_reject(self):
        job = baker.make(ScrapeJob)
        approval = baker.make(Approval, job=job)
        approval.status = Approval.STATUS_APPROVED
        approval.save()
        approval.refresh_from_db()
        self.assertEqual(approval.status, "approved")

    def test_str(self):
        job = baker.make(ScrapeJob)
        approval = baker.make(Approval, job=job, approval_type="execution", status="pending")
        self.assertIn("pending", str(approval))
        self.assertIn("Execution Approval", str(approval))

    def test_ordering(self):
        job = baker.make(ScrapeJob)
        a1 = baker.make(Approval, job=job)
        a2 = baker.make(Approval, job=job)
        approvals = list(Approval.objects.all())
        self.assertEqual(approvals[0].id, a2.id)
