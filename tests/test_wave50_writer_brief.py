"""[wave-50 T2] The writer brief must PRESCRIBE Algolia catalog discovery for
client-rendered listings (job 1116: the brief FORBADE discover mode, so the
writer — despite extracting the creds — shipped SSR nav anchors as "product
URLs"), and the HTTP navigation template must carry the discovery pre-step
backed by ``src.algolia_discovery``.
"""

import importlib.util
import os
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])

_ALGOLIA_STATE = {
    "detected": True,
    "application_id": "TESTAPPID",
    "api_key": "test-search-key",
    "index_name": "prod_us",
}


class TestWriterBrief:
    def _build(self, state):
        from agents import subagents as sub

        with mock.patch.dict(os.environ, {"PROJECT_ROOT": ROOT}):
            msgs = sub.build_code_writer_message(state)
        return str(msgs[0].content)

    def test_client_rendered_prescribes_catalog_discovery(self):
        # probe_result.listing_connectivity is the CANONICAL key (graph.py
        # writes the advisory listing probe there; the brief originally read
        # a dead "listing" key and never fired — job 485 e2e finding).
        content = self._build({
            "site_slug": "t", "url": "https://x.example/collections/all",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "http_navigation"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {"algolia": dict(_ALGOLIA_STATE)},
            "probe_result": {
                "listing_connectivity": {"listing_client_rendered": True}},
        })
        assert "catalog discovery" in content.lower()
        assert "algolia_discovery" in content
        assert "solidusSlug" in content   # the wide-key fallback is named
        assert "Do NOT implement discover mode" not in content
        assert "not Algolia discovery" not in content

    def test_wonderbly_485_shape_gets_check_first_prescription(self):
        """The e2e-revealed class: site_analysis is EMPTY (the LLM never
        wrote an algolia key) and the anchor counter LIED (55 nav anchors on
        a client-rendered shell → lcr False). A discovery-mode job whose
        listing is HTTP-reachable must still get the cheap deterministic
        check-first prescription: extract creds, prefer the catalog, fall
        back to anchors."""
        content = self._build({
            "site_slug": "t", "url": "https://x.example/collections/all",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "http_requests"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {},
            "probe_result": {"listing_connectivity": {
                "http_ok": True, "listing_client_rendered": False,
                "item_link_count": 55}},
        })
        assert "extract_algolia_creds" in content
        assert "discover_catalog_urls" in content
        assert "CHECK FIRST" in content
        # the forbid that caused job 1116 must not ride this branch
        assert "Do NOT implement discover mode" not in content

    def test_cloak_only_probe_gets_no_check_first_section(self):
        """[job-1107 class] The listing won on cloak_none — a BROWSER rung —
        so probe http_ok is False by construction (_is_http_method admits
        direct_http*/fingerprint_* only). A cloak-only site must NOT be told
        to hunt for Algolia creds it may not have: on jcrew the retry budget
        was the thing that ran out."""
        content = self._build({
            "site_slug": "t", "url": "https://x.example/collections/all",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "playwright"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {},
            "probe_result": {"listing_connectivity": {
                "http_ok": False, "browser_ok": True, "needs_browser": True,
                "listing_client_rendered": False, "item_link_count": 60}},
        })
        assert "CHECK FIRST" not in content

    def test_url_list_never_gets_the_check_first_section(self):
        """Phase-2-only jobs have no discovery to fix — no section at all."""
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "url_list",
            "scraper_analysis": {"strategy": "http_requests"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {},
            "probe_result": {"listing_connectivity": {
                "http_ok": True, "listing_client_rendered": False,
                "item_link_count": 55}},
        })
        assert "### Algolia" not in content
        assert "CHECK FIRST" not in content

    def test_algolia_without_client_rendered_keeps_lookup_only(self):
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "url_list",
            "scraper_analysis": {"strategy": "http_requests"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {"algolia": dict(_ALGOLIA_STATE)},
        })
        assert "per-product lookup" in content
        assert "Do NOT implement discover mode" in content
        assert "catalog discovery" not in content

    def test_no_algolia_no_section(self):
        content = self._build({
            "site_slug": "t", "url": "https://x.example/p",
            "input_mode": "url_list",
            "scraper_analysis": {"strategy": "http_requests"},
            "target_fields": ["title"],
            "page_type": "product",
        })
        # skill descriptions may mention Algolia; the SECTION must not exist
        assert "### Algolia" not in content


class TestTemplateWiring:
    """Static AST assertions — the house convention (templates are not
    importable: their {PLACEHOLDER} config is substituted by the writer at
    generation time, mirroring tests/test_discovery_ladder.py)."""

    def _tpl_source(self):
        return (Path(ROOT) / "templates" / "http_navigation_scraper.py"
                ).read_text(encoding="utf-8")

    def _tree(self):
        import ast

        return ast.parse(self._tpl_source())

    def _funcs(self):
        import ast

        return {n.name: n for n in ast.walk(self._tree())
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _calls_in(self, func_node):
        import ast

        names = set()
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    names.add(f.attr)
        return names

    def test_template_compiles_and_imports_the_helper(self):
        source = self._tpl_source()
        compile(source, "http_navigation_scraper.py", "exec")
        assert "from src.algolia_discovery import" in source
        assert "discover_catalog_urls" in source
        assert "extract_algolia_creds" in source

    def test_prestep_extracts_creds_and_converts(self):
        """The pre-step must: fetch the listing, extract creds, and call the
        catalog discoverer — the job-1116 chain, wired at generation time."""
        helper = self._funcs()["_discover_urls_via_algolia"]
        calls = self._calls_in(helper)
        assert "_http_get" in calls
        assert "extract_algolia_creds" in calls
        assert "discover_catalog_urls" in calls
        # failure modes degrade to the agreed sentinel, never raise past it
        assert "algolia_unavailable" in self._tpl_source()

    def test_dispatch_tries_algolia_before_html_discovery(self):
        """Both listing and category branches call the pre-step and fall back
        to `_discover_urls_via_category` only when it yields nothing."""
        main_fn = self._funcs()["main"]
        src = self._tpl_source()
        assert "_discover_urls_via_algolia" in self._calls_in(main_fn)
        for branch_anchor in ("args.listing_url", "args.category_url"):
            idx = src.find(f"elif {branch_anchor}:")
            assert idx > 0, branch_anchor
            window = src[idx:idx + 900]
            algolia_at = window.find("_discover_urls_via_algolia(")
            html_at = window.find("_discover_urls_via_category(")
            assert 0 < algolia_at < html_at, (
                f"{branch_anchor}: algolia pre-step must precede the HTML fallback")


class TestRequestsTemplateWiring:
    """The requests template ALSO carries the pre-step: job 485's strategy
    was `http_requests` (fingerprint-proven PDP → wave-28 rule), so the
    http_navigation template — and its pre-step — never entered play."""

    def _tpl_source(self):
        return (Path(ROOT) / "templates" / "requests_scraper.py"
                ).read_text(encoding="utf-8")

    def _funcs(self):
        import ast

        tree = ast.parse(self._tpl_source())
        return {n.name: n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _calls_in(self, func_node):
        import ast

        names = set()
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    names.add(f.attr)
        return names

    def test_template_compiles_and_imports_the_helper(self):
        source = self._tpl_source()
        compile(source, "requests_scraper.py", "exec")
        assert "from src.algolia_discovery import" in source
        assert "discover_catalog_urls" in source
        assert "extract_algolia_creds" in source
        assert "algolia_unavailable" in source

    def test_prestep_extracts_creds_and_converts(self):
        helper = self._funcs()["_discover_urls_via_algolia"]
        calls = self._calls_in(helper)
        assert "extract_algolia_creds" in calls
        assert "discover_catalog_urls" in calls
        assert "extract_taxon_query" in calls
        assert "build_taxon_filter" in calls

    def test_main_discovery_goes_through_the_algolia_first_gate(self):
        """main() must not call the anchor discoverer directly — both
        discovery arms route through the single algolia-first gate."""
        main_fn = self._funcs()["main"]
        assert "_discover_phase1" in self._calls_in(main_fn)
        assert "discover_listing_urls_with_retry" not in self._calls_in(main_fn)
        gate = self._funcs()["_discover_phase1"]
        src = self._tpl_source()
        import ast

        gate_src = ast.get_source_segment(src, gate)
        assert gate_src is not None
        algolia_at = gate_src.find("_discover_urls_via_algolia(")
        retry_at = gate_src.find("discover_listing_urls_with_retry(")
        assert 0 < algolia_at < retry_at
