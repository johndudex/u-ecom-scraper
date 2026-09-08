"""[wave-26 W26-1] The tester prompt obeys the CLI contract per input_mode.

Prod 420/406: the nav-mode ``nav_validation`` block prescribed
``--sample --query "<search_criteria>"`` while wave-24's pre-computed
``_tester_phase1_instruction`` renders the args the draft ACTUALLY declares
(``--query`` only for search_term; ``--listing-url`` when present). The two
instructions fought: the tester followed the hardcoded ``--query`` line, the
draft rejected it (argparse exit 2), and the cycle was mislabelled a scraper
bug — a working draft destroyed by its own harness. Also: the workflow's
Phase-2 bullet prescribed a seed-file-only ``--input input_urls.json`` run
for ALL modes, but a nav scraper's shipped contract is discovery-driven —
the phase1-untested routing gate (route_after_testing) rejects exactly such
a PASS after the fact, wasting the cycle the prompt caused.
"""
from __future__ import annotations

import os
import textwrap
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys_path = None
import sys  # noqa: E402

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

NAV_DRAFT = '''
    import argparse, os
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--listing-url", type=str)
        parser.add_argument("--fresh-discovery", action="store_true")
        parser.add_argument("--discover-only", action="store_true")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--query", type=str)
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--input", type=str)
        args = parser.parse_args()
        print("ran")
    if __name__ == "__main__":
        main()
'''


def _write_draft(tmp_path, slug, src):
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "scraper_draft.py").write_text(
        textwrap.dedent(src).lstrip(), encoding="utf-8"
    )


def _build(state, tmp_path):
    from agents import subagents as sub

    _write_draft(tmp_path, state["site_slug"], NAV_DRAFT)
    with mock.patch.dict(os.environ, {"PROJECT_ROOT": str(tmp_path)}):
        msgs = sub.build_code_tester_message(state)
    return str(msgs[0].content)


NAV_STATE = {
    "site_slug": "navsite",
    "url": "https://x.example/p",
    "input_mode": "list_page",
    "search_criteria": "https://x.example/listing",
    "navigation_analysis": {
        "discovery": {"listing_url": "https://x.example/listing"}
    },
    "target_fields": ["title"],
    "page_type": "product",
}


class TestNavPromptDefersToContract:
    def test_no_hardcoded_sample_query_prescription(self, tmp_path):
        content = _build(NAV_STATE, tmp_path)
        assert "--sample --query" not in content, (
            "nav_validation must not prescribe args the draft may not "
            "declare — _tester_phase1_instruction already renders the "
            "declared flags (prod 420/406 argparse exit-2 mislabels)"
        )

    def test_url_shaped_criteria_never_in_query_slot(self, tmp_path):
        content = _build(NAV_STATE, tmp_path)
        assert '--query "https://' not in content, (
            "a URL-shaped search_criteria on a non-search_term job must "
            "never be placed in the --query slot"
        )

    def test_defers_to_precomputed_phase1_args(self, tmp_path):
        content = _build(NAV_STATE, tmp_path)
        assert "EXACT args prescribed" in content, (
            "nav_validation must point at the pre-computed Phase 1 args "
            "instead of inventing its own"
        )
        # the pre-computed instruction itself is still rendered
        assert "'--listing-url'" in content

    def test_search_term_mode_still_gets_query(self, tmp_path):
        state = {
            **NAV_STATE,
            "site_slug": "searchsite",
            "input_mode": "search_term",
            "search_criteria": "blue sneakers",
        }
        content = _build(state, tmp_path)
        assert "'--query'" in content, (
            "search_term jobs must still see the prescribed --query arg"
        )


class TestPhase2DiscoveryDriven:
    def test_nav_phase2_rides_discovery_invocation(self, tmp_path):
        content = _build(NAV_STATE, tmp_path)
        assert "SAME discovery-driven invocation" in content, (
            "nav-mode Phase 2 must be validated through the discovery-driven "
            "invocation — a seed-file-only run does not exercise the shipped "
            "contract (the phase1-untested gate rejects such a PASS after "
            "the fact)"
        )

    def test_url_list_phase2_keeps_seed_fast_path(self, tmp_path):
        state = {**NAV_STATE, "site_slug": "urlsite", "input_mode": "url_list"}
        content = _build(state, tmp_path)
        assert "'--sample','--input','input_urls.json'" in content
        assert "SAME discovery-driven invocation" not in content
