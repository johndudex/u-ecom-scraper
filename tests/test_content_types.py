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
    # NOTE: the LLM field-mapper seam in normalize_fields
    # (_build_mapping_prompt / _core_fields_present / _call_llm_for_mapping)
    # was removed when normalize_fields went deterministic — job content types
    # map via the src.job_fields resolver (see tests/test_job_fields.py for
    # its coverage), other types keep the analyzer's own field map. The
    # prompt-builder tests below were removed with it; registry-level prompt
    # fields are still covered by TestContentTypeRegistry.test_mapping_prompt_fields.
    # The surviving "are the core fields present" check is validate_coverage's
    # _extract_covered_fields, tested here in its place.

    def test_extract_covered_fields_core(self):
        from agents.nodes.validate_coverage import _extract_covered_fields
        from src.content_types import get_content_type

        config = get_content_type("product")
        core = list(config.core_field_names)
        fields = {
            f: {"method": "resolver", "selector": f"json.{f}"} for f in core[:3]
        }
        covered = _extract_covered_fields({"fields": fields})
        assert set(core[:3]) <= covered
        # an entry with neither method nor selector is NOT covered
        fields[core[0]] = {"method": "", "selector": ""}
        covered = _extract_covered_fields({"fields": fields})
        assert core[0] not in covered

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
        # _item_label moved from pre_execution_approval to field_confirmation
        # (Wave 2 Cut 2 gate merge: pre_execution_approval is no longer a
        # graph node; field_confirmation inherited the item-count estimate +
        # label it used in its interrupt message).
        from agents.nodes.field_confirmation import _item_label
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
    # NOTE: navigation_agent / navigation_explore / navigation_synthesize LLM
    # agents have been replaced by a single deterministic ``browser_traverse``
    # node. The LLM-agent prompt/message-builder tests have been removed; the
    # integration test (webapp/tests/test_browser_traverse_integration.py)
    # covers the new node. Tests below exercise preserved surface area:
    # _build_initial_state routing flags, the code_writer message builder and
    # the deterministic scraper_analyzer (_derive_strategy) — both of which
    # still consume ``navigation_analysis`` state — and the runtime
    # navigation scraper template.

    def test_route_after_site_analyzer_navigation(self):
        from agents.graph import _route_after_site_analyzer

        state_url_list = {"input_mode": "url_list"}
        state_navigation = {"input_mode": "navigation"}
        state_list_page = {"input_mode": "list_page"}
        state_search = {"input_mode": "search_term"}

        assert _route_after_site_analyzer(state_url_list) == "update_tracker_analysis"
        assert _route_after_site_analyzer(state_navigation) == "browser_traverse"
        assert _route_after_site_analyzer(state_list_page) == "browser_traverse"
        # search_term MUST route through navigation (the documented
        # docs/scraper_agents.md bug: routing it to update_tracker_analysis
        # bypassed navigation entirely for search jobs).
        assert _route_after_site_analyzer(state_search) == "browser_traverse"

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
        # skip_content_analysis was removed with the LLM-mapper cut: nav jobs
        # no longer skip content analysis (browser_traverse feeds
        # product_analyzer, which maps fields on discovered items). Fresh jobs
        # start with every skip flag off; routing skips site_analyzer instead
        # (set by _accessibility_goto at runtime, not here).
        assert state["skip_site_analysis"] is False
        assert state["skip_product_analysis"] is False

    def test_build_initial_state_navigation_mode_resolves_from_page_type(self):
        """Regression: jobs created without input_mode set (e.g. via legacy
        views or scheduler) must still route through navigation because
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
        assert state["skip_site_analysis"] is False

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
        assert state["skip_site_analysis"] is False

    def test_build_initial_state_list_page_mode(self):
        from scraper.tasks import _build_initial_state

        job = ScrapeJob(
            url="https://test.com",
            page_type="product_list",
            input_mode="list_page",
        )
        state = _build_initial_state(job)
        assert state["input_mode"] == "list_page"
        assert state["skip_site_analysis"] is False

    def test_build_initial_state_url_list_no_skip(self):
        from scraper.tasks import _build_initial_state

        job = ScrapeJob(
            url="https://test.com",
            page_type="product",
            input_mode="url_list",
        )
        state = _build_initial_state(job)
        assert state["skip_site_analysis"] is False
        assert state["skip_product_analysis"] is False
        assert state["skip_code_generation"] is False

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
        # build_scraper_analyzer_message is gone: scraper_analyzer is the
        # deterministic _decide_strategy/_derive_strategy pair (no LLM, no
        # prompt). The navigation-awareness this test guarded — the analyzer
        # must consume navigation_analysis — is exercised on _derive_strategy,
        # which reads it to pick the strategy + carry the API/discovery
        # signals code_writer needs for the two-phase scraper.
        from agents.graph import _derive_strategy

        nav_analysis = {
            "discovery_method": "browser_traverse",
            "rendering_verified": "csr",
            "data_source": "api",
            "api_endpoint": {
                "url": "https://test.com/api/search",
                "items_per_page": 20,
                "count": 26955,
            },
            "pagination": {
                "type": "page_param",
                "page_param_name": "page",
                "items_per_page": 20,
            },
        }
        state = {
            "url": "https://test.com",
            "site_slug": "test-com",
            "probe_result": {"connectivity": {"method_that_worked": "direct_http"}},
            "navigation_analysis": nav_analysis,
            "input_mode": "navigation",
        }
        analysis = _derive_strategy(state)
        # captured API beats the probe method -> internal_api two-phase scraper
        assert analysis["strategy"] == "internal_api"
        assert analysis["api_endpoint"]["url"] == "https://test.com/api/search"
        # navigator's pagination detection is propagated for the template
        assert analysis["discovery_config"]["type"] == "page_param"
        assert analysis["discovery_config"]["page_param_name"] == "page"
        assert analysis["strategy_justification"].startswith("Deterministic:")

    def test_pipeline_phases_includes_navigation(self):
        from scraper.tasks import PIPELINE_PHASES

        # 3 old nodes (navigation_explore / navigation_agent /
        # navigation_synthesize) collapsed into a single browser_traverse node.
        assert "browser_traverse" in PIPELINE_PHASES

    def test_graph_phases_include_navigation(self):
        from agents.graph import PHASE_MAP

        assert "browser_traverse" in PHASE_MAP

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
        # The 3 old navigation nodes (navigate_explore / navigate_agent /
        # navigate_synthesize) are archived — replaced by a single
        # ``browser_traverse`` node. Existence is verified at the graph level
        # (see webapp/tests/test_browser_traverse_integration.py).
        import pytest

        pytest.skip(
            "navigate_explore node archived — replaced by browser_traverse "
            "(see test_browser_traverse_integration.py)"
        )

    def test_navigation_synthesize_node_exists(self):
        # See test_navigation_explore_node_exists note.
        import pytest

        pytest.skip(
            "navigate_synthesize node archived — replaced by browser_traverse "
            "(see test_browser_traverse_integration.py)"
        )
