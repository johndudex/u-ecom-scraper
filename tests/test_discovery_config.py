"""Unit tests for src/discovery.py:config_from_dict — pagination-type mapping.

Covers the offset_param regression: SFCC-style ``?start=0&sz=24`` is detected by
the navigator (which emits ``page_param``/``page_size``, NOT
``page_param_name``/``items_per_page``) but was UNMAPPED — it fell through to
``config_for_navigation_job`` with ``page_param_name=None``, making the
``page_param`` primitive dead. Now it maps to ``config_for_page_param`` which
reuses the existing primitive (``_OFFSET_PARAMS`` → offset arithmetic).
"""
from __future__ import annotations

from src.discovery import (
    DiscoveryConfig,
    build_page_param_url,
    config_from_dict,
)


class TestOffsetParam:
    def test_navigator_keys_start_sz(self):
        # Exact shape navigate_explore.py emits for SFCC ?start=0&sz=24
        cfg = config_from_dict({
            "type": "offset_param", "page_param": "start",
            "page_size_param": "sz", "page_size": 24,
            "url_pattern": "url_with_start_sz_params",
        })
        assert cfg.strategies == ("page_param",)
        assert cfg.page_param_name == "start"
        assert cfg.items_per_page == 24

    def test_canonical_keys(self):
        cfg = config_from_dict({
            "type": "offset_param",
            "page_param_name": "offset", "items_per_page": 20,
        })
        assert cfg.page_param_name == "offset"
        assert cfg.items_per_page == 20

    def test_defaults_when_missing(self):
        cfg = config_from_dict({"type": "offset_param"})
        assert cfg.page_param_name == "start"
        assert cfg.items_per_page is None

    def test_offset_alias(self):
        cfg = config_from_dict({"type": "offset", "page_param": "skip", "page_size": 50})
        assert cfg.page_param_name == "skip"
        assert cfg.items_per_page == 50

    def test_navigator_keys_do_not_crash_apply(self):
        # Regression: bare **overrides made _apply raise TypeError on the
        # navigator-only keys (page_size_param, url_pattern).
        cfg = config_from_dict({
            "type": "offset_param", "page_param": "start",
            "page_size_param": "sz", "page_size": 24, "url_pattern": "x",
        })
        assert isinstance(cfg, DiscoveryConfig)

    def test_max_pages_passthrough(self):
        cfg = config_from_dict({
            "type": "offset_param", "page_param": "start",
            "page_size": 24, "max_pages": 10,
        })
        assert cfg.max_pages == 10

    def test_build_url_uses_offset_math_for_start(self):
        # The "start" param flows through to offset arithmetic (page-1)*ipp.
        cfg = config_from_dict({"type": "offset_param", "page_param": "start", "page_size": 24})
        url = build_page_param_url(
            "https://x/jobs?start=0", cfg.page_param_name, 3, cfg.items_per_page
        )
        assert url == "https://x/jobs?start=48"  # (3-1)*24


class TestPageParamUnchanged:
    def test_page_param_default(self):
        cfg = config_from_dict({"type": "page_param"})
        assert cfg.page_param_name == "page"
        assert cfg.strategies == ("page_param",)

    def test_page_param_with_name(self):
        cfg = config_from_dict({"type": "page_param", "page_param_name": "p", "items_per_page": 12})
        assert cfg.page_param_name == "p"
        assert cfg.items_per_page == 12


class TestPageNumbersDeferred:
    def test_falls_through_to_navigation_job(self):
        # No primitive fits numbered-button SPA pagers; keep fallbacks rather
        # than locking to next_button. config_for_navigation_job tries all four.
        cfg = config_from_dict({"type": "page_numbers", "max_pages": 5})
        assert "load_more" in cfg.strategies
        assert "page_param" in cfg.strategies


class TestOtherTypesUnchanged:
    def test_load_more(self):
        cfg = config_from_dict({"type": "load_more"})
        assert "load_more" in cfg.strategies

    def test_infinite_scroll_tall(self):
        cfg = config_from_dict({"type": "infinite_scroll_tall"})
        assert cfg.strategies == ("infinite_scroll", "load_more")

    def test_unknown_falls_through(self):
        cfg = config_from_dict({"type": "cursor_pagination"})
        assert len(cfg.strategies) >= 2  # navigation_job preset
