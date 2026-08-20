"""Prompt-side tests for the CLI-contract enforcement (docs/cli-contract-plan.md v2).

Covers: constants ↔ templates anti-drift (per family), the code_writer message
contract section, tester message pre-computed args, .md staleness tripwires,
template headers, and the issue-relay normalization (P1).

Run: docker compose exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings \
     celery-worker bash -c "cd /app && python -m pytest tests/test_cli_contract_prompt.py -v"
"""

from __future__ import annotations

import ast
import os
import textwrap
import sys
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

from agents.constants import (  # noqa: E402
    API_STRATEGIES,
    NAV_FAMILY_FLAGS,
    SEARCH_QUERY_FAMILY_FLAGS,
    SEARCH_ENV_FAMILY_FLAGS,
    SSR_NAV_FLAGS,
    URL_LIST_FLAGS,
    required_cli_flags,
)


def _template_flags(path: str) -> set[str]:
    """The SAME ast walk run_execution._accepted_cli_flags uses, normalized to
    bare names (constants carry the -- form for prompt rendering)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    flags = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--"):
                    flags.add(arg.value[2:])
    return flags


def _norm(flag_tuple) -> set[str]:
    return {f.lstrip("-") for f in flag_tuple}


def _tpl(name: str) -> str:
    return os.path.join(ROOT, "templates", name)


class TestAntiDrift:
    """required_cli_flags must be ⊆ what each template family declares — the
    tripwire that keeps prompt vocabulary and the guard from drifting."""

    def test_playwright_family_declares_nav_set(self):
        # Discovery flags only — --input/--urls on http_navigation/navigation
        # are a documented pre-existing gap the writer ADDS (critique noted it;
        # making them hard requirements here would fail day one).
        for name in ("playwright_scraper.py", "http_navigation_scraper.py", "navigation_scraper.py"):
            declared = _template_flags(_tpl(name))
            need = _norm(NAV_FAMILY_FLAGS) - {"input", "urls", "sample", "limit"}
            assert need <= declared, f"{name} missing {need - declared}"

    def test_playwright_family_declares_search_set(self):
        # Only the http_navigation family declares --query
        for name in ("http_navigation_scraper.py", "navigation_scraper.py"):
            declared = _template_flags(_tpl(name))
            need = _norm(SEARCH_QUERY_FAMILY_FLAGS) - {"input", "urls", "sample", "limit"}
            assert need <= declared, f"{name} missing {need - declared}"

    def test_ssr_declares_its_reduced_set(self):
        declared = _template_flags(_tpl("ssr_div_list_scraper.py"))
        need = _norm(SSR_NAV_FLAGS) - {"input", "urls", "sample", "limit"}
        assert need <= declared
        # and does NOT declare the flags we must not advertise for it
        assert "fresh-discovery" not in declared and "discover-only" not in declared

    def test_api_declares_its_set(self):
        declared = _template_flags(_tpl("api_scraper.py"))
        need = _norm(required_cli_flags("navigation", "api")) - {"input", "urls", "sample", "limit"}
        assert need <= declared

    def test_url_list_discovery_flags_absent_where_not_declared(self):
        # UC/shopify declare no discovery flags — required_cli_flags must not
        # advertise any for them (they aren't strategy-routed today, but the
        # default nav set must never be claimed for them).
        assert required_cli_flags("url_list") == URL_LIST_FLAGS
        for name in ("undetected_chromedriver_scraper.py", "shopify_scraper.py"):
            declared = _template_flags(_tpl(name))
            assert not ({"fresh-discovery", "listing-url", "discover-only"} & declared), name

    def test_required_flags_mode_gating(self):
        assert required_cli_flags("url_list") == URL_LIST_FLAGS
        assert required_cli_flags("") == URL_LIST_FLAGS
        assert required_cli_flags("list_page") == NAV_FAMILY_FLAGS
        assert required_cli_flags("search_term") == SEARCH_QUERY_FAMILY_FLAGS
        assert required_cli_flags("search_term", "playwright") == SEARCH_ENV_FAMILY_FLAGS
        assert required_cli_flags("navigation", "internal_api") == URL_LIST_FLAGS + ("--fresh-discovery",)
        assert required_cli_flags("list_page", "ssr_div_list") == SSR_NAV_FLAGS


class _FakeSettings:
    def __init__(self):
        self.PROJECT_ROOT = ROOT


class TestWriterMessage:
    def _build(self, state):
        from agents import subagents as sub

        with mock.patch.dict(os.environ, {"PROJECT_ROOT": ROOT}):
            msgs = sub.build_code_writer_message(state)
        return str(msgs[0].content)

    def test_nav_message_carries_contract(self):
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "http_navigation"},
            "target_fields": ["title"],
            "page_type": "product",
        })
        assert "HARD CONTRACT" in content
        assert "--listing-url" in content and "--fresh-discovery" in content
        assert "SCRAPER_LISTING_URL" in content

    def test_search_term_uses_query_not_listing(self):
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "search_term",
            "scraper_analysis": {"strategy": "playwright"},
            "target_fields": ["title"],
            "page_type": "product",
        })
        assert "--query" in content

    def test_url_list_has_no_discovery_flags(self):
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "url_list",
            "target_fields": ["title"],
            "page_type": "product",
        })
        # Scope to the contract section: the generic "do NOT rename" example
        # elsewhere in the message legitimately names flags.
        assert "Discovery flags for this url_list job" not in content
        assert "HARD CONTRACT" in content  # base contract still present

    def test_api_strategy_omits_listing_url(self):
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "navigation",
            "scraper_analysis": {"strategy": "internal_api"},
            "target_fields": ["title"],
            "page_type": "product",
        })
        assert "Discovery flags for this navigation job" in content
        assert "--fresh-discovery" in content
        # The contract tail must not advertise --listing-url for the api family
        tail = content.split("Discovery flags for this navigation job")[1][:400]
        assert "--listing-url" not in tail

    def test_fidelity_block_mentions_contract(self):
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "url_list",
            "target_fields": ["title"],
            "page_type": "product",
        })
        assert "Template fidelity" in content
        assert "CONTRACT, not boilerplate" in content


class TestTesterMessage:
    def _build(self, state, draft_src: str, tmp_path):
        slug = state["site_slug"]
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "scraper_draft.py").write_text(
            textwrap.dedent(draft_src).lstrip(), encoding="utf-8"
        )
        from agents import subagents as sub

        with mock.patch.dict(os.environ, {"PROJECT_ROOT": str(tmp_path)}):
            msgs = sub.build_code_tester_message(state)
        return str(msgs[0].content)

    NAV_STATE = {
        "site_slug": "t", "url": "https://x.example/p",
        "input_mode": "list_page",
        "navigation_analysis": {"discovery": {"listing_url": "https://x.example/listing"}},
        "target_fields": ["title"],
        "page_type": "product",
    }

    def test_phase1_args_precomputed_from_draft(self, tmp_path):
        draft = '''
            import argparse, os
            def main():
                parser = argparse.ArgumentParser()
                parser.add_argument("--listing-url", type=str)
                parser.add_argument("--fresh-discovery", action="store_true")
                parser.add_argument("--discover-only", action="store_true")
                parser.add_argument("--limit", type=int)
                parser.add_argument("--sample", action="store_true")
                parser.add_argument("--input", type=str)
                args = parser.parse_args()
                _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
                if _env_listing or args.listing_url:
                    discover(args.listing_url or _env_listing)
                elif args.input:
                    load(args.input)
        '''
        content = self._build(dict(self.NAV_STATE), draft, tmp_path)
        assert "--listing-url" in content and "--fresh-discovery" in content
        assert "--discover-only" in content and "CLI CONTRACT VIOLATION" in content

    def test_no_discovery_flags_reported_as_violation(self, tmp_path):
        draft = '''
            import argparse
            def main():
                parser = argparse.ArgumentParser()
                parser.add_argument("--sample", action="store_true")
                parser.add_argument("--input", type=str)
                parser.add_argument("--limit", type=int)
                args = parser.parse_args()
                if args.input:
                    load(args.input)
        '''
        content = self._build(dict(self.NAV_STATE), draft, tmp_path)
        assert "NO discovery flags" in content
        assert "CLI CONTRACT VIOLATION" in content


class TestMdStaleness:
    def test_code_writer_md_names_the_surface(self):
        md = open(os.path.join(ROOT, ".opencode/agents/code-writer.md"), encoding="utf-8").read()
        for token in ("--listing-url", "--fresh-discovery", "--discover-only", "--query", "SCRAPER_LISTING_URL"):
            assert token in md, f"code-writer.md missing {token}"
        # rule 4 must not over-promise the patcher (phrase spans a line break)
        import re as _re

        assert _re.search(r"protected\s+by\s+nothing\s+but\s+you", md)

    def test_code_tester_md_documents_string_feedback(self):
        md = open(os.path.join(ROOT, ".opencode/agents/code-tester.md"), encoding="utf-8").read()
        assert "a STRING" in md or "STRING" in md


class TestTemplateHeaders:
    def test_headers_above_main(self):
        for name in (
            "playwright_scraper.py", "http_navigation_scraper.py",
            "navigation_scraper.py", "requests_scraper.py",
            "api_scraper.py", "ssr_div_list_scraper.py",
        ):
            src = open(_tpl(name), encoding="utf-8").read()
            idx_main = src.index("\ndef main(")
            window = src[max(0, idx_main - 1200): idx_main]
            assert "CLI CONTRACT" in window, f"{name} missing header above main()"
            assert "{" not in window.split("CLI CONTRACT")[1][:400], f"{name} header has braces"

    def test_uc_and_shopify_notes(self):
        for name in ("undetected_chromedriver_scraper.py", "shopify_scraper.py"):
            src = open(_tpl(name), encoding="utf-8").read()
            assert "run_execution passes --fresh-discovery" in src, name


class TestIssueRelay:
    def test_summarize_normalizes_three_keys(self):
        from agents import subagents as sub

        state = {
            "test_retry_count": 0,
            "test_report": {
                "overall_assessment": "FAIL",
                "confidence_score": 0.1,
                "issues": [
                    {"severity": "high", "message": "via message key"},
                    {"severity": "medium", "problem": "via problem key"},
                    {"severity": "high", "description": "CLI CONTRACT VIOLATION: via description key"},
                ],
            },
        }
        out = sub._summarize_test_report(state)
        assert "via message key" in out          # P1: was invisible before
        assert "via problem key" in out
        assert "CLI CONTRACT VIOLATION" in out   # marker relay
        assert "do NOT regenerate" in out


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
