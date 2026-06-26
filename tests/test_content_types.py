"""Tests for multi-content-type support: registry, models, state, nodes."""

import json
import os
import tempfile

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webapp"))
if not os.path.exists("manage.py"):
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp"))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from src.content_types import (  # noqa: E402
    CONTENT_TYPES,
    PAGE_TYPE_MAP,
    all_page_type_choices,
    get_content_type,
    resolve_page_type,
)
from scraper.models import ScrapeJob, Site  # noqa: E402


class TestContentTypeRegistry:
    def test_registry_has_six_content_types(self):
        assert len(CONTENT_TYPES) == 6
        expected = {
            "product",
            "article",
            "job_posting",
            "forum_thread",
            "serp",
            "page_content",
        }
        assert set(CONTENT_TYPES.keys()) == expected

    def test_page_type_map_has_eleven_entries(self):
        assert len(PAGE_TYPE_MAP) == 11

    def test_resolve_page_type(self):
        ct, mode = resolve_page_type("product")
        assert ct == "product" and mode == "url_list"
        ct, mode = resolve_page_type("product_list")
        assert ct == "product" and mode == "list_page"
        ct, mode = resolve_page_type("product_navigation")
        assert ct == "product" and mode == "navigation"
        ct, mode = resolve_page_type("article")
        assert ct == "article" and mode == "url_list"
        ct, mode = resolve_page_type("job_navigation")
        assert ct == "job_posting" and mode == "navigation"
        ct, mode = resolve_page_type("serp")
        assert ct == "serp" and mode == "search_term"
        ct, mode = resolve_page_type("page_content")
        assert ct == "page_content" and mode == "url_list"
        ct, mode = resolve_page_type("unknown_type")
        assert ct == "unknown_type" and mode == "url_list"

    def test_get_content_type(self):
        for name in CONTENT_TYPES:
            config = get_content_type(name)
            assert config is not None
            assert config.name == name

    def test_output_keys(self):
        expected_keys = {
            "product": "products",
            "article": "articles",
            "job_posting": "jobs",
            "forum_thread": "threads",
            "serp": "results",
            "page_content": "pages",
        }
        for name, key in expected_keys.items():
            config = get_content_type(name)
            assert config.output_key == key

    def test_core_fields_subset_of_all_fields(self):
        for name, config in CONTENT_TYPES.items():
            all_field_names = [f.name for f in config.all_fields]
            for cf in config.core_field_names:
                assert cf in all_field_names, f"{cf} not in {name} all_fields"

    def test_output_schema_structure(self):
        for name, config in CONTENT_TYPES.items():
            schema = config.output_schema
            assert schema["output_key"] == config.output_key
            assert schema["content_type"] == config.name
            assert len(schema["fields"]) > 0
            for field in schema["fields"]:
                assert "name" in field
                assert "label" in field
                assert "type" in field

    def test_all_page_type_choices(self):
        choices = all_page_type_choices()
        assert len(choices) == 11
        values = [v for v, _ in choices]
        assert set(values) == set(PAGE_TYPE_MAP.keys())

    def test_direct_fields_in_non_serp_types(self):
        for name, config in CONTENT_TYPES.items():
            if name == "serp":
                continue
            all_field_names = [f.name for f in config.all_fields]
            assert "url" in all_field_names
            assert "status_code" in all_field_names
            assert "scraped_at" in all_field_names

    def test_mapping_prompt_fields(self):
        for name, config in CONTENT_TYPES.items():
            prompt = config.mapping_prompt_fields()
            assert len(prompt) > 0
            for cf in config.core_field_names:
                assert cf in prompt

    def test_to_agent_context(self):
        for name, config in CONTENT_TYPES.items():
            ctx = config.to_agent_context()
            assert config.label in ctx
            assert config.output_key in ctx
            assert "Core fields:" in ctx


class TestModelDefaults:
    def test_scrape_job_defaults(self):
        job = ScrapeJob()
        assert job.page_type == "product"
        assert job.input_mode == "url_list"
        assert job.search_criteria == ""

    def test_site_defaults(self):
        site = Site()
        assert site.site_type == "shopping"
        assert site.output_schema == {}


@pytest.mark.django_db
class TestBuildInitialState:
    def _make_job(self, **kwargs):
        job = ScrapeJob(**kwargs)
        job.save()
        return job

    def test_product_defaults(self):
        job = self._make_job(url="https://example.com")
        from scraper.tasks import _build_initial_state

        state = _build_initial_state(job)
        assert state["page_type"] == "product"
        assert state["input_mode"] == "url_list"
        assert state["site_type"] == "shopping"
        ct_config = state["content_type_config"]
        assert ct_config["output_key"] == "products"
        assert ct_config["content_type"] == "product"
        assert state["sample_url"] == state["product_url"]

    @pytest.mark.parametrize(
        "page_type,expected_output_key,expected_site_type",
        [
            ("article", "articles", "articles"),
            ("job_posting", "jobs", "jobs"),
            ("forum_thread", "threads", "forum"),
            ("page_content", "pages", "general"),
        ],
    )
    def test_content_type_state(
        self, page_type, expected_output_key, expected_site_type
    ):
        job = self._make_job(url="https://example.com", page_type=page_type)
        from scraper.tasks import _build_initial_state

        state = _build_initial_state(job)
        assert state["page_type"] == page_type
        ct_config = state["content_type_config"]
        assert ct_config["output_key"] == expected_output_key
        assert state["site_type"] == expected_site_type

    @pytest.mark.parametrize(
        "page_type,expected_mode,expected_criteria",
        [
            ("product_navigation", "navigation", ""),
            ("serp", "search_term", ""),
            ("job_navigation", "navigation", "python developer"),
        ],
    )
    def test_input_mode_from_page_type(
        self, page_type, expected_mode, expected_criteria
    ):
        _, mode_from_map = resolve_page_type(page_type)
        job = self._make_job(
            url="https://example.com",
            page_type=page_type,
            input_mode=mode_from_map,
            search_criteria=expected_criteria,
        )
        from scraper.tasks import _build_initial_state

        state = _build_initial_state(job)
        assert state["input_mode"] == expected_mode
        assert state["search_criteria"] == expected_criteria


class TestNodeFunctions:
    def test_build_mapping_prompt_product(self):
        from agents.nodes.normalize_fields import _build_mapping_prompt
        from src.content_types import get_content_type

        config = get_content_type("product")
        prompt = _build_mapping_prompt(config.output_schema)
        assert "price" in prompt.lower()
        assert "title" in prompt.lower()

    def test_build_mapping_prompt_article(self):
        from agents.nodes.normalize_fields import _build_mapping_prompt
        from src.content_types import get_content_type

        config = get_content_type("article")
        prompt = _build_mapping_prompt(config.output_schema)
        assert "author" in prompt.lower()
        assert "publish_date" in prompt.lower()

    def test_build_mapping_prompt_job(self):
        from agents.nodes.normalize_fields import _build_mapping_prompt
        from src.content_types import get_content_type

        config = get_content_type("job_posting")
        prompt = _build_mapping_prompt(config.output_schema)
        assert "company" in prompt.lower()
        assert "location" in prompt.lower()

    def test_core_fields_present(self):
        from agents.nodes.normalize_fields import _core_fields_present
        from src.content_types import get_content_type

        config = get_content_type("product")
        core = list(config.core_field_names)
        fields = {f: f"val_{i}" for i, f in enumerate(core[:3])}
        result = _core_fields_present(fields, core)
        assert result is not None

    def test_format_output_products_all_keys(self):
        from agents.nodes.field_confirmation import _format_output_products

        samples = {
            "products": {
                "products": [
                    {
                        "id": 1,
                        "title": "Shoe",
                        "price": "99",
                        "url": "http://x",
                        "src_url": "http://x",
                        "status_code": 200,
                        "scraped_at": "2026-01-01",
                        "remarks": "",
                        "location": "",
                        "availability": "",
                        "original_price": "",
                        "currency": "",
                    }
                ]
            },
            "articles": {
                "articles": [
                    {
                        "id": 1,
                        "title": "News",
                        "author": "Bob",
                        "publish_date": "2026-01-01",
                        "content": "Body",
                        "url": "http://x",
                        "src_url": "http://x",
                        "status_code": 200,
                        "scraped_at": "2026-01-01",
                        "remarks": "",
                        "location": "",
                        "images": [],
                        "tags": [],
                        "category": "",
                    }
                ]
            },
            "jobs": {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Engineer",
                        "company": "Acme",
                        "location": "NYC",
                        "description": "Code",
                        "url": "http://x",
                        "src_url": "http://x",
                        "status_code": 200,
                        "scraped_at": "2026-01-01",
                        "remarks": "",
                        "salary": "",
                        "requirements": "",
                        "job_type": "",
                        "apply_url": "",
                    }
                ]
            },
            "threads": {
                "threads": [
                    {
                        "id": 1,
                        "title": "Help!",
                        "author": "Jane",
                        "posts": [],
                        "url": "http://x",
                        "src_url": "http://x",
                        "status_code": 200,
                        "scraped_at": "2026-01-01",
                        "remarks": "",
                        "location": "",
                        "views": 0,
                        "replies": 0,
                        "last_activity": "",
                    }
                ]
            },
            "pages": {
                "pages": [
                    {
                        "id": 1,
                        "title": "About",
                        "content": "Welcome",
                        "url": "http://x",
                        "src_url": "http://x",
                        "status_code": 200,
                        "scraped_at": "2026-01-01",
                        "remarks": "",
                        "location": "",
                        "images": [],
                        "metadata": {},
                    }
                ]
            },
        }
        for key, data in samples.items():
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as f:
                json.dump(data, f)
                tmp_path = f.name
            try:
                text = _format_output_products(tmp_path, output_key=key)
                assert data[key][0]["title"] in text
            finally:
                os.unlink(tmp_path)

    def test_item_label(self):
        from agents.nodes.pre_execution_approval import _item_label
        from src.content_types import get_content_type

        expected = {
            "product": "products",
            "article": "article",
            "job_posting": "job",
            "forum_thread": "thread",
            "serp": "result",
            "page_content": "page",
        }
        for ct_name, label in expected.items():
            config = get_content_type(ct_name)
            assert _item_label({"content_type_config": config.output_schema}) == label
        assert _item_label({}) == "items"

    @pytest.mark.django_db
    def test_check_tracker_site_type(self):
        from agents.nodes.check_tracker import _handle_new_site
        from scraper.models import Site

        Site.objects.filter(url="https://test-articles.com").delete()
        _handle_new_site(
            "https://test-articles.com", "test-articles", site_type="articles"
        )
        site = Site.objects.get(url="https://test-articles.com")
        assert site.site_type == "articles"
        site.delete()

    def test_build_content_type_context(self):
        from agents.subagents import _build_content_type_context
        from src.content_types import get_content_type

        for ct_name in CONTENT_TYPES:
            config = get_content_type(ct_name)
            ctx = _build_content_type_context(
                {"content_type_config": config.output_schema}
            )
            assert config.output_schema["output_key"] in ctx
            assert ct_name in ctx

        empty_ctx = _build_content_type_context({})
        assert empty_ctx == ""

    def test_build_site_analyzer_message_with_ct(self):
        from agents.subagents import build_site_analyzer_message
        from src.content_types import get_content_type

        config = get_content_type("article")
        state = {
            "url": "https://test.com",
            "content_type_config": config.output_schema,
            "sample_url": "https://test.com/page",
            "slug": "test-com",
        }
        msg = build_site_analyzer_message(state)
        assert len(msg) == 1
        assert "article" in msg[0].content.lower()

    def test_build_site_analyzer_message_backward_compat(self):
        from agents.subagents import build_site_analyzer_message

        state = {
            "url": "https://test.com",
            "sample_url": "https://test.com/page",
            "slug": "test-com",
        }
        msg = build_site_analyzer_message(state)
        assert len(msg) == 1

    def test_build_product_analyzer_message_with_ct(self):
        from agents.subagents import build_product_analyzer_message
        from src.content_types import get_content_type

        for ct_name in [
            "product",
            "article",
            "job_posting",
            "forum_thread",
            "page_content",
        ]:
            config = get_content_type(ct_name)
            state = {
                "url": "https://test.com",
                "content_type_config": config.output_schema,
                "sample_url": "https://test.com/page",
                "slug": "test-com",
            }
            msg = build_product_analyzer_message(state)
            assert len(msg) == 1

    def test_build_code_writer_message_with_ct(self):
        from agents.subagents import build_code_writer_message
        from src.content_types import get_content_type

        for ct_name in [
            "product",
            "article",
            "job_posting",
            "forum_thread",
            "page_content",
        ]:
            config = get_content_type(ct_name)
            state = {
                "url": "https://test.com",
                "content_type_config": config.output_schema,
                "slug": "test-com",
                "sample_url": "https://test.com/page",
                "product_url": "https://test.com/page",
            }
            msg = build_code_writer_message(state)
            assert len(msg) == 1


class TestNavigationAgent:
    def test_navigation_agent_prompt_exists(self):
        import agents.subagents as sub_mod

        assert "navigation_agent" in sub_mod.AGENT_PROMPT_MAP
        assert sub_mod.AGENT_PROMPT_MAP["navigation_agent"] == "navigation-agent"
        assert "navigation-agent" in sub_mod.AGENT_TEMPERATURES
        assert "navigation_agent" in sub_mod.AGENT_MAX_ITERATIONS

    def test_build_navigation_agent_message(self):
        from agents.subagents import build_navigation_agent_message

        state = {
            "url": "https://test.com",
            "site_slug": "test-com",
            "input_mode": "navigation",
            "search_criteria": "running shoes",
            "content_type_config": {
                "content_type": "product",
                "output_key": "products",
            },
        }
        msg = build_navigation_agent_message(state)
        assert len(msg) == 1
        content = msg[0].content
        assert "navigation" in content.lower()
        assert "running shoes" in content
        assert "navigation_analysis.json" in content

    def test_build_navigation_agent_message_list_page(self):
        from agents.subagents import build_navigation_agent_message

        state = {
            "url": "https://test.com",
            "site_slug": "test-com",
            "input_mode": "list_page",
            "sample_url": "https://test.com/shop",
            "content_type_config": {
                "content_type": "article",
                "output_key": "articles",
            },
        }
        msg = build_navigation_agent_message(state)
        content = msg[0].content
        assert "list page" in content.lower()
        assert "20 tool calls" in content

    def test_route_after_site_analyzer_navigation(self):
        from agents.graph import _route_after_site_analyzer

        state_url_list = {"input_mode": "url_list"}
        state_navigation = {"input_mode": "navigation"}
        state_list_page = {"input_mode": "list_page"}
        state_search = {"input_mode": "search_term"}

        assert _route_after_site_analyzer(state_url_list) == "update_tracker_analysis"
        assert _route_after_site_analyzer(state_navigation) == "navigation_explore"
        assert _route_after_site_analyzer(state_list_page) == "navigation_explore"
        assert _route_after_site_analyzer(state_search) == "update_tracker_analysis"

    def test_build_initial_state_navigation_mode(self):
        from scraper.tasks import _build_initial_state

        job = ScrapeJob(
            url="https://test.com",
            page_type="product_navigation",
            input_mode="navigation",
            search_criteria="sneakers",
        )
        state = _build_initial_state(job)
        assert state["input_mode"] == "navigation"
        assert state["search_criteria"] == "sneakers"
        assert state["skip_content_analysis"] is True

    def test_build_initial_state_navigation_mode_resolves_from_page_type(self):
        """Regression: jobs created without input_mode set (e.g. via legacy
        views or scheduler) must still route through navigation_agent because
        page_type carries the canonical routing intent."""
        from scraper.tasks import _build_initial_state

        # input_mode left at default 'url_list' — this used to misroute
        # navigation jobs through the standard product pipeline.
        job = ScrapeJob(
            url="https://test.com",
            page_type="product_navigation",
            input_mode="",  # empty: the bug condition
            search_criteria="sneakers",
        )
        state = _build_initial_state(job)
        assert state["input_mode"] == "navigation", (
            "page_type=product_navigation must resolve to input_mode=navigation even when job.input_mode is empty"
        )
        assert state["skip_content_analysis"] is True

    def test_build_initial_state_list_page_mode_resolves_from_page_type(self):
        """Regression: same as above for list_page mode."""
        from scraper.tasks import _build_initial_state

        job = ScrapeJob(
            url="https://test.com",
            page_type="product_list",
            input_mode="",
        )
        state = _build_initial_state(job)
        assert state["input_mode"] == "list_page"
        assert state["skip_content_analysis"] is True

    def test_build_initial_state_list_page_mode(self):
        from scraper.tasks import _build_initial_state

        job = ScrapeJob(
            url="https://test.com",
            page_type="product_list",
            input_mode="list_page",
        )
        state = _build_initial_state(job)
        assert state["input_mode"] == "list_page"
        assert state["skip_content_analysis"] is True

    def test_build_initial_state_url_list_no_skip(self):
        from scraper.tasks import _build_initial_state

        job = ScrapeJob(
            url="https://test.com",
            page_type="product",
            input_mode="url_list",
        )
        state = _build_initial_state(job)
        assert state["skip_content_analysis"] is False

    def test_code_writer_message_with_navigation(self):
        from agents.subagents import build_code_writer_message

        nav_analysis = {
            "discovery_method": "search",
            "search": {
                "has_search": True,
                "url_pattern": "/search?q={query}",
                "input_selector": "input.search",
            },
            "pagination": {
                "type": "next_button",
                "next_button_selector": "a.next",
            },
            "item_links": {
                "container_selector": ".grid",
                "link_selector": "a.item",
                "url_pattern": "/item/{slug}",
            },
        }
        state = {
            "url": "https://test.com",
            "site_slug": "test-com",
            "input_mode": "navigation",
            "search_criteria": "sneakers",
            "navigation_analysis": nav_analysis,
            "scraper_analysis": {"strategy": "playwright", "proxy_tier": "none"},
            "site_analysis": {"platform": "custom"},
        }
        msg = build_code_writer_message(state)
        content = msg[0].content
        assert "TWO-PHASE" in content
        assert "navigation_analysis.json" in content
        assert "Phase 1" in content
        assert "Phase 2" in content

    def test_code_writer_message_without_navigation(self):
        from agents.subagents import build_code_writer_message

        state = {
            "url": "https://test.com",
            "site_slug": "test-com",
            "input_mode": "url_list",
            "scraper_analysis": {"strategy": "playwright", "proxy_tier": "none"},
            "site_analysis": {"platform": "custom"},
        }
        msg = build_code_writer_message(state)
        content = msg[0].content
        assert "TWO-PHASE" not in content
        assert "input_urls.json" in content

    def test_scraper_analyzer_message_with_navigation(self):
        from agents.subagents import build_scraper_analyzer_message

        nav_analysis = {
            "discovery_method": "category",
            "pagination": {"type": "page_param"},
        }
        state = {
            "url": "https://test.com",
            "site_slug": "test-com",
            "navigation_analysis": nav_analysis,
            "input_mode": "navigation",
        }
        msg = build_scraper_analyzer_message(state)
        content = msg[0].content
        assert "navigation" in content.lower()
        assert "two-phase" in content.lower()

    def test_pipeline_phases_includes_navigation(self):
        from scraper.tasks import PIPELINE_PHASES

        assert "navigation_explore" in PIPELINE_PHASES
        assert "navigation_synthesize" in PIPELINE_PHASES

    def test_agent_tool_map_has_navigation(self):
        from agents.tools import AGENT_TOOL_MAP

        assert "navigation_agent" in AGENT_TOOL_MAP
        assert "playwright" in AGENT_TOOL_MAP["navigation_agent"]
        assert "probe" not in AGENT_TOOL_MAP["navigation_agent"]

    def test_allowed_playwright_tools_has_navigation(self):
        from agents.tools import ALLOWED_PLAYWRIGHT_TOOLS

        assert "navigation_agent" in ALLOWED_PLAYWRIGHT_TOOLS
        assert (
            "playwright_browser_navigate"
            in ALLOWED_PLAYWRIGHT_TOOLS["navigation_agent"]
        )

    def test_graph_phases_include_navigation(self):
        from agents.graph import PHASE_MAP

        assert "navigation_explore" in PHASE_MAP
        assert "navigation_synthesize" in PHASE_MAP

    def test_navigation_template_exists(self):
        template_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "templates",
            "navigation_scraper.py",
        )
        assert os.path.isfile(template_path), (
            f"Navigation template not found at {template_path}"
        )

    def test_navigation_explore_node_exists(self):
        from agents.nodes.navigate_explore import navigate_explore

        assert callable(navigate_explore)

    def test_navigation_synthesize_node_exists(self):
        from agents.nodes.navigate_synthesize import navigate_synthesize

        assert callable(navigate_synthesize)

    def test_navigation_synthesize_has_no_browser_tools(self):
        from agents.tools import AGENT_TOOL_MAP

        tools = AGENT_TOOL_MAP.get("navigation_synthesize", [])
        assert "playwright" not in tools
        assert "web" not in tools
        assert "read_file" in tools
        assert "write_file" in tools

    def test_build_navigation_synthesize_message(self):
        from agents.subagents import build_navigation_synthesize_message

        state = {
            "site_slug": "test-com",
            "url": "https://www.test.com",
            "search_criteria": "shoes",
            "input_mode": "navigation",
        }
        messages = build_navigation_synthesize_message(state)
        assert len(messages) == 1
        content = messages[0].content
        assert "navigation_findings.json" in content
        assert "navigation_analysis.json" in content
        assert "write_file" in content
