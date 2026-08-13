"""Unit tests for src/pagination_patterns.py — the single source of truth for
pagination selectors + regexes shared by Layer A (traversal.py JS probe) and
Layer C (discovery.py clicker).

The load-bearing tests are ``test_*_imports_from_canonical_module``: they prove
the drift between the two layers is dead by asserting ``src.discovery`` re-exports
the SAME objects ``src.pagination_patterns`` owns.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from src import pagination_patterns as pp
from src.pagination_patterns import (
    DEFAULT_LOAD_MORE_SELECTORS,
    DEFAULT_NEXT_BUTTON_SELECTORS,
    PATTERNS,
    PAGE_PARAM_REGEX,
    REL_NEXT_SELECTOR,
    _OFFSET_PARAMS,
)


class TestShape:
    def test_load_more_has_nine_selectors(self):
        assert len(PATTERNS.load_more_selectors) == 9

    def test_next_button_has_six_selectors(self):
        assert len(PATTERNS.next_button_selectors) == 6

    def test_offset_params_membership(self):
        assert set(_OFFSET_PARAMS) == {"offset", "start", "skip", "begin", "from"}

    def test_page_param_regex_and_rel_next(self):
        assert "page" in PAGE_PARAM_REGEX and "start" in PAGE_PARAM_REGEX
        assert REL_NEXT_SELECTOR == 'a[rel="next"]'


class TestFrozen:
    def test_is_frozen_dataclass(self):
        assert dataclasses.is_dataclass(PATTERNS)

    def test_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            PATTERNS.load_more_selectors = ()  # type: ignore[misc]


class TestBackwardCompatAliases:
    """src/discovery.py must keep working unchanged — the aliases ARE PATTERNS."""

    def test_load_more_alias_is_same_object(self):
        assert DEFAULT_LOAD_MORE_SELECTORS is PATTERNS.load_more_selectors

    def test_next_button_alias_is_same_object(self):
        assert DEFAULT_NEXT_BUTTON_SELECTORS is PATTERNS.next_button_selectors

    def test_offset_params_alias_is_same_object(self):
        assert _OFFSET_PARAMS is PATTERNS.offset_params


class TestDiscoveryImportsCanonical:
    """The drift is dead: discovery re-exports pagination_patterns' objects."""

    def test_load_more(self):
        import src.discovery as d

        assert d.DEFAULT_LOAD_MORE_SELECTORS is pp.DEFAULT_LOAD_MORE_SELECTORS

    def test_next_button(self):
        import src.discovery as d

        assert d.DEFAULT_NEXT_BUTTON_SELECTORS is pp.DEFAULT_NEXT_BUTTON_SELECTORS

    def test_offset_params(self):
        import src.discovery as d

        assert d._OFFSET_PARAMS is pp._OFFSET_PARAMS


class TestLoadMoreSupersetOfLegacyLayerA:
    """The 3 original Layer A selectors must still match (no regression)."""

    @pytest.mark.parametrize(
        "legacy",
        [
            "[class*='load-more' i]",
            "[class*='show-more' i]",
            "[class*='loadMore' i]",
        ],
    )
    def test_legacy_selector_present(self, legacy):
        assert legacy in PATTERNS.load_more_selectors


class TestPresenceSelectors:
    """Layer A CLASSIFICATION uses a conservative subset (the data-driven fix).

    The broad aria-label/magicbox selectors stay in load_more_selectors for
    Layer C CLICKING only — they false-positive on non-pagination buttons whose
    label/class merely contains "more"/"next" (e.g. Coveo facet checkboxes for
    "Morehead State University" / "Skidmore College"). Verified on lw.com.
    """

    def test_presence_has_six_class_based_selectors(self):
        assert len(PATTERNS.load_more_presence_selectors) == 6

    def test_presence_is_subset_of_click_list(self):
        for s in PATTERNS.load_more_presence_selectors:
            assert s in PATTERNS.load_more_selectors

    @pytest.mark.parametrize(
        "broad",
        [
            "button[aria-label*='more' i]",
            "a[aria-label*='next' i]",
            ".coveo-magicbox-load-more",
        ],
    )
    def test_broad_selectors_excluded_from_presence(self, broad):
        # Present for Layer C clicking...
        assert broad in PATTERNS.load_more_selectors
        # ...but NOT for Layer A classification.
        assert broad not in PATTERNS.load_more_presence_selectors


class TestCssList:
    def test_is_valid_js_string_literal(self):
        s = PATTERNS.load_more_css_list
        # json.dumps wraps in double quotes; CSS single quotes preserved inside
        assert s.startswith('"') and s.endswith('"')
        # round-trips to the comma-joined list
        assert json.loads(s) == ",".join(PATTERNS.load_more_selectors)
        # no escaping that would break document.querySelectorAll(...)
        assert "\\" not in s and '\\"' not in s

    def test_click_list_has_nine_comma_joined(self):
        # Layer C click list: 9 selectors → 8 commas
        inner = json.loads(PATTERNS.load_more_css_list)
        assert inner.count(",") == 8

    def test_presence_list_has_six_comma_joined(self):
        # Layer A presence list: 6 selectors → 5 commas
        inner = json.loads(PATTERNS.load_more_presence_css_list)
        assert inner.count(",") == 5


class TestPageStateJsInjection:
    """The Layer A probe injects the CONSERVATIVE presence list (not the full 9)."""

    def test_no_sentinel_leak(self):
        from experimental.nav_traversal.traversal import _PAGE_STATE_JS

        assert "/*__LOAD_MORE_SELECTORS__*/" not in _PAGE_STATE_JS

    def test_presence_list_injected_once(self):
        from experimental.nav_traversal.traversal import _PAGE_STATE_JS

        # The CONSERVATIVE presence list (6) is what Layer A classifies on.
        injected = PATTERNS.load_more_presence_css_list
        assert _PAGE_STATE_JS.count(injected) == 1

    def test_broad_selectors_not_in_probe(self):
        from experimental.nav_traversal.traversal import _PAGE_STATE_JS

        # The 3 broad selectors must NOT appear in the JS probe — they're the
        # false-positive vector (school names, "read more", facet buttons).
        for broad in ("aria-label*='more'", "aria-label*='next'", "coveo-magicbox"):
            assert broad not in _PAGE_STATE_JS

    def test_visibility_gate_present(self):
        from experimental.nav_traversal.traversal import _PAGE_STATE_JS

        # The gate that keeps the probe honest with Layer C's click contract
        assert "offsetParent" in _PAGE_STATE_JS
        assert "getClientRects" in _PAGE_STATE_JS
        assert "aria-hidden" in _PAGE_STATE_JS
