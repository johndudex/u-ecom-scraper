"""[wave-41 T5] intake_discover_fields rides the shared partner probe core
(scraper.api.discovery.probe_and_discover — the same one POST
/api/v1/discover-fields serves). Pins the UI envelope contract the modal
JS depends on: `error` truthy = shown verbatim; `error: ""` + `message` =
informational path (blocked pages, honest zero); success = chips."""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from scraper.api import errors as api_errors

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _api_error(status, code, message, details=None):
    return api_errors.ApiError(status, code, message, details)


class TestIntakeDiscoverFields(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)

    def _post(self, url="https://www.example.com/p/tee-1"):
        return self.client.post("/intake/discover-fields/", {"url": url}, **AJAX)

    def test_requires_ajax(self):
        resp = self.client.post("/intake/discover-fields/", {"url": "https://x.example/p/1"})
        assert resp.status_code == 400

    def test_homepage_guidance_400(self):
        resp = self._post("https://www.example.com/")
        assert resp.status_code == 400
        assert "sample item page" in resp.json()["error"]

    def test_success_envelope(self):
        with patch("scraper.api.discovery.probe_and_discover",
                   MagicMock(return_value={
                       "fields": ["title", "price"], "json_schema": {"type": "object"},
                       "source": "llm", "content_type": "product"})):
            resp = self._post()
        body = resp.json()
        assert resp.status_code == 200
        assert body["fields"] == ["title", "price"]
        assert body["error"] == "" and body["message"] == ""
        assert body["source"] == "llm" and body["content_type"] == "product"

    def test_honest_zero_uses_message_path(self):
        with patch("scraper.api.discovery.probe_and_discover",
                   MagicMock(return_value={"fields": [], "json_schema": None,
                                           "source": "none", "content_type": ""})):
            resp = self._post()
        body = resp.json()
        assert resp.status_code == 200
        assert body["fields"] == [] and body["error"] == ""
        assert "Add them manually" in body["message"]

    def test_blocked_page_stays_on_message_path(self):
        # Old contract: blocked is informational (error ""), never an error.
        with patch("scraper.api.discovery.probe_and_discover",
                   MagicMock(side_effect=_api_error(502, "site_blocked",
                                                    "The page blocked automated access (cloudflare)."))):
            resp = self._post()
        body = resp.json()
        assert resp.status_code == 200
        assert body["error"] == ""
        assert "cloudflare" in body["message"]

    def test_infrastructure_failure_rides_error_sentence(self):
        with patch("scraper.api.discovery.probe_and_discover",
                   MagicMock(side_effect=_api_error(503, "discovery_unavailable",
                                                    "Browser service unreachable."))):
            resp = self._post()
        body = resp.json()
        assert resp.status_code == 200
        assert "Browser service unreachable" in body["error"]
        assert "Add fields manually" in body["error"]

    def test_discovery_crash_is_discovery_failed(self):
        with patch("scraper.api.discovery.probe_and_discover",
                   MagicMock(side_effect=RuntimeError("boom"))):
            resp = self._post()
        body = resp.json()
        assert resp.status_code == 200
        assert body["error"] == "discovery_failed"
        assert "boom" in body["message"]
