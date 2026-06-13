from unittest.mock import MagicMock, patch

from django.test import TestCase
from model_bakery import baker

from scraper.models import ScrapeJob
from scraper.tasks import _generate_slug, _graph_is_interrupted


class TestGenerateSlug(TestCase):
    def test_simple_url(self):
        self.assertEqual(_generate_slug("https://www.example.com"), "example-com")

    def test_subdomain(self):
        self.assertEqual(_generate_slug("https://shop.example.com"), "shop-example-com")

    def test_with_port(self):
        self.assertEqual(_generate_slug("https://example.com:8080"), "example-com")

    def test_path_stripped(self):
        self.assertEqual(_generate_slug("https://www.example.com/shop/products"), "example-com")

    def test_special_chars(self):
        self.assertEqual(_generate_slug("https://www.my_site.com"), "my-site-com")


class TestBuildInitialState(TestCase):
    @patch("scraper.tasks.LangGraphService")
    def test_initial_state_keys(self, mock_service_cls):
        from scraper.tasks import _build_initial_state

        job = baker.make(ScrapeJob, url="https://example.com", product_url="", currency="USD", full_extraction=False)
        state = _build_initial_state(job)
        self.assertEqual(state["url"], "https://example.com")
        self.assertEqual(state["currency"], "USD")
        self.assertTrue(state["sample_only"])
        self.assertEqual(state["messages"], [])
        self.assertEqual(state["agent_logs"], [])
        self.assertEqual(state["job_id"], job.id)


class TestGraphIsInterrupted(TestCase):
    def test_no_interrupt(self):
        mock_graph = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.tasks = []
        mock_graph.get_state.return_value = mock_snapshot
        self.assertFalse(_graph_is_interrupted(mock_graph, {"configurable": {"thread_id": "t-1"}}))

    def test_has_interrupt(self):
        mock_graph = MagicMock()
        mock_snapshot = MagicMock()
        mock_task = MagicMock()
        mock_task.interrupts = [{"reason": "field_confirmation"}]
        mock_snapshot.tasks = [mock_task]
        mock_graph.get_state.return_value = mock_snapshot
        self.assertTrue(_graph_is_interrupted(mock_graph, {"configurable": {"thread_id": "t-1"}}))
