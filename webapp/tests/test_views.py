from model_bakery import baker
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.urls import reverse

from scraper.models import Approval, ScrapeJob

User = get_user_model()


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


class TestAdminJobVisibility(TestCase):
    """Admins (superusers) can see EVERY job and who created it; regular users
    only see their own. Covers the /intake/?job=N deep-link case where the admin
    was previously 404-blocked from a job another user created."""

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw", email="owner@example.com")
        self.other = User.objects.create_user(username="other", password="pw")
        self.admin = User.objects.create_superuser(username="admin", password="pw", email="admin@example.com")
        # Job created by `owner` — the one an admin deep-links into.
        self.job = baker.make(ScrapeJob, url="https://example.com", user=self.owner)
        # A job belonging to `other`, to confirm cross-user isolation.
        self.other_job = baker.make(ScrapeJob, url="https://other.com", user=self.other)

    def test_regular_user_blocked_from_others_job(self):
        c = Client()
        c.force_login(self.other)
        resp = c.get(reverse("job_detail", kwargs={"job_id": self.job.id}))
        self.assertEqual(resp.status_code, 404)

    def test_regular_user_sees_own_job(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.get(reverse("job_detail", kwargs={"job_id": self.job.id}))
        self.assertEqual(resp.status_code, 200)

    def test_admin_sees_others_job(self):
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse("job_detail", kwargs={"job_id": self.job.id}))
        self.assertEqual(resp.status_code, 200)
        # Owner info is surfaced to the admin viewer.
        self.assertContains(resp, "Admin view")
        self.assertContains(resp, "Created by owner")

    def test_admin_api_returns_owner(self):
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse("job_api", kwargs={"job_id": self.job.id}))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["owner_username"], "owner")
        self.assertEqual(data["owner_email"], "owner@example.com")

    def test_regular_api_hides_owner(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.get(reverse("job_api", kwargs={"job_id": self.job.id}))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # A regular user viewing their own job gets no owner fields.
        self.assertIsNone(data["owner_username"])

    def test_admin_intake_jobs_lists_all(self):
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse("intake_jobs"))
        self.assertEqual(resp.status_code, 200)
        ids = {j["id"] for j in resp.json()["jobs"]}
        self.assertIn(self.job.id, ids)
        self.assertIn(self.other_job.id, ids)

    def test_regular_intake_jobs_lists_own_only(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.get(reverse("intake_jobs"))
        self.assertEqual(resp.status_code, 200)
        ids = {j["id"] for j in resp.json()["jobs"]}
        self.assertIn(self.job.id, ids)
        self.assertNotIn(self.other_job.id, ids)


class TestIntakeValidateSchemaView(TestCase):
    """POST /intake/validate-schema/ — validates a pasted/uploaded JSON schema.

    Auth is covered by conftest's autouse superuser + DebugAutoLogin; the
    POST+AJAX guard is the real gate, so every call passes
    HTTP_X_REQUESTED_WITH="XMLHttpRequest".
    """

    URL = reverse("intake_validate_schema")

    def setUp(self):
        self.client = Client()

    def _post_text(self, body, **extra):
        return self.client.post(
            self.URL, {"schema_text": body},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest", **extra,
        )

    def _post_file(self, body, filename="schema.json"):
        return self.client.post(
            self.URL, {"schema_file": SimpleUploadedFile(filename, body, content_type="application/json")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_valid_schema_returns_derived_fields(self):
        resp = self._post_text('{"type":"object","properties":{"title":{"type":"string"},"price":{"type":"number"}}}')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["derived_fields"], ["title", "price"])
        self.assertEqual(data["detected_content_type"], "product")

    def test_valid_schema_via_file_upload(self):
        resp = self._post_file(b'{"fields":[{"name":"title"},{"name":"sku"}]}')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["derived_fields"], ["title", "sku"])

    def test_file_overrides_text(self):
        # When both are present, the file wins.
        resp = self.client.post(self.URL, {
            "schema_text": "garbage",
            "schema_file": SimpleUploadedFile("s.json", b'["title"]', content_type="application/json"),
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["valid"])

    def test_invalid_json_returns_200_with_issue(self):
        resp = self._post_text('{not valid')
        self.assertEqual(resp.status_code, 200)  # content errors are 200+valid:false
        data = resp.json()
        self.assertFalse(data["valid"])
        self.assertTrue(any(i["code"] == "INVALID_JSON" for i in data["issues"]))

    def test_non_object_returns_invalid(self):
        resp = self._post_text('"a string"')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["valid"])

    def test_get_rejected(self):
        resp = self.client.get(self.URL, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 400)

    def test_non_ajax_rejected(self):
        resp = self.client.post(self.URL, {"schema_text": "[]"})
        self.assertEqual(resp.status_code, 400)

    def test_oversize_rejected_by_validator(self):
        # Over our 256 KiB cap but under Django's 2.5 MB transport cap → 200 + TOO_LARGE.
        blob = "{" + ", ".join(f'"f{i}":"x"' for i in range(262144 // 6)) + "}"
        self.assertGreater(len(blob), 262144)
        resp = self._post_text(blob)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["valid"])
        self.assertTrue(any(i["code"] == "TOO_LARGE" for i in data["issues"]))


class TestIntakePageRendersSchemaUI(TestCase):
    """The /intake page must render with the new JSON-schema input UI."""

    def test_intake_page_has_schema_ui(self):
        resp = Client().get(reverse("intake"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="schema-mode"')
        self.assertContains(resp, 'id="schema-json-input"')
        self.assertContains(resp, 'id="schema-file"')
        self.assertContains(resp, "validateSchemaUrl")

