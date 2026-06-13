from model_bakery import baker
from django.test import TestCase, Client
from django.urls import reverse

from scraper.models import Approval, ScrapeJob


class TestHomeView(TestCase):
    def setUp(self):
        self.client = Client()

    def test_home_get(self):
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "New Scrape Job")

    def test_home_post_creates_job(self):
        resp = self.client.post(reverse("home"), {
            "url": "https://example.com",
        })
        self.assertEqual(ScrapeJob.objects.count(), 1)
        job = ScrapeJob.objects.first()
        self.assertEqual(job.url, "https://example.com")

    def test_home_post_missing_url(self):
        resp = self.client.post(reverse("home"), {})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "URL is required")

    def test_home_shows_recent_jobs(self):
        baker.make(ScrapeJob, url="https://first.com")
        baker.make(ScrapeJob, url="https://second.com")
        resp = self.client.get(reverse("home"))
        self.assertContains(resp, "first.com")
        self.assertContains(resp, "second.com")


class TestJobListView(TestCase):
    def setUp(self):
        self.client = Client()

    def test_list_empty(self):
        resp = self.client.get(reverse("job_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No jobs")

    def test_list_with_jobs(self):
        baker.make(ScrapeJob, url="https://example.com")
        resp = self.client.get(reverse("job_list"))
        self.assertContains(resp, "example.com")


class TestJobDetailView(TestCase):
    def setUp(self):
        self.client = Client()
        self.job = baker.make(ScrapeJob, url="https://example.com")

    def test_detail(self):
        resp = self.client.get(reverse("job_detail", kwargs={"job_id": self.job.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "example.com")

    def test_detail_404(self):
        resp = self.client.get(reverse("job_detail", kwargs={"job_id": 99999}))
        self.assertEqual(resp.status_code, 404)


class TestJobCancelView(TestCase):
    def setUp(self):
        self.client = Client()
        self.job = baker.make(ScrapeJob, url="https://example.com", status="running")

    def test_cancel_running(self):
        resp = self.client.post(reverse("job_cancel", kwargs={"job_id": self.job.id}))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ScrapeJob.STATUS_CANCELLED)

    def test_cancel_completed_noop(self):
        self.job.status = ScrapeJob.STATUS_COMPLETED
        self.job.save()
        resp = self.client.post(reverse("job_cancel", kwargs={"job_id": self.job.id}))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ScrapeJob.STATUS_COMPLETED)


class TestJobRestartView(TestCase):
    def setUp(self):
        self.client = Client()
        self.job = baker.make(
            ScrapeJob, url="https://example.com", status="completed"
        )

    def test_restart_completed(self):
        count_before = ScrapeJob.objects.count()
        resp = self.client.post(reverse("job_restart", kwargs={"job_id": self.job.id}))
        self.assertEqual(ScrapeJob.objects.count(), count_before + 1)

    def test_restart_running_noop(self):
        self.job.status = ScrapeJob.STATUS_RUNNING
        self.job.save()
        count_before = ScrapeJob.objects.count()
        resp = self.client.post(reverse("job_restart", kwargs={"job_id": self.job.id}))
        self.assertEqual(ScrapeJob.objects.count(), count_before)


class TestApprovalListView(TestCase):
    def setUp(self):
        self.client = Client()

    def test_empty_queue(self):
        resp = self.client.get(reverse("approval_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "All clear")

    def test_with_pending(self):
        job = baker.make(ScrapeJob)
        baker.make(Approval, job=job, approval_type="field_confirm", question="Approve fields?")
        resp = self.client.get(reverse("approval_list"))
        self.assertContains(resp, "Approve fields?")


class TestApprovalDetailView(TestCase):
    def setUp(self):
        self.client = Client()
        self.job = baker.make(ScrapeJob)
        self.approval = baker.make(
            Approval,
            job=self.job,
            approval_type="field_confirm",
            question="Approve these fields?",
        )

    def test_detail_get(self):
        resp = self.client.get(
            reverse("approval_detail", kwargs={"approval_id": self.approval.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Approve these fields?")

    def test_approve(self):
        resp = self.client.post(
            reverse("approval_detail", kwargs={"approval_id": self.approval.id}),
            {"action": "approve"},
        )
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.STATUS_APPROVED)

    def test_reject(self):
        resp = self.client.post(
            reverse("approval_detail", kwargs={"approval_id": self.approval.id}),
            {"action": "reject"},
        )
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, Approval.STATUS_REJECTED)


class TestJobAPIView(TestCase):
    def setUp(self):
        self.client = Client()
        self.job = baker.make(ScrapeJob, url="https://example.com")

    def test_api_json(self):
        resp = self.client.get(reverse("job_api", kwargs={"job_id": self.job.id}))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["url"], "https://example.com")
        self.assertIn("steps", data)
        self.assertIn("approvals", data)
