"""[wave-26 hotfix] browser_traverse must have exactly ONE destination.

Found live in local e2e job 348 (karenmillen, 2026-09-08): the sample URL
redirected to the brand-protection domain (karenmillen-engb.backend.verbolia
.com) twice → the wave-19 cross-domain arm returned
``Command(goto="cleanup")`` — but the static edge
``browser_traverse → product_analyzer`` (graph.py) is UNIONED with any
Command goto (the D6 lesson, documented twice in graph.py for F13
site_analyzer and run_execution). Result: cleanup AND product_analyzer both
executed in the same super-step — cleanup wrote an "empty run" report and
demoted the scraper while product_analyzer was still analyzing. One node,
one destination.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

GRAPH = os.path.join(ROOT, "webapp", "agents", "graph.py")


def _node_src() -> str:
    src = open(GRAPH).read()
    start = src.index("def _invoke_navigation_traverse(")
    end = src.index("def _invoke_nav_skill_review(")
    return src[start:end]


class TestSingleDestination:
    def test_no_static_edge_from_browser_traverse(self):
        src = open(GRAPH).read()
        assert (
            'workflow.add_edge("browser_traverse", "product_analyzer")'
            not in src
        ), (
            "static edge unions with the cross-domain Command(goto=cleanup) — "
            "BOTH destinations run (the D6 shadow branch; live job 348). The "
            "F13/run_execution precedent: Command-routed nodes get NO static "
            "out-edge."
        )

    def test_happy_paths_are_commands_to_product_analyzer(self):
        """With the static edge gone, the node's normal returns must carry
        their own routing — Command(goto="product_analyzer")."""
        node = _node_src()
        # strip the ARCHIVED block (commented code) so pins see live code only
        live = "\n".join(
            ln for ln in node.splitlines()
            if not ln.lstrip().startswith("#")
        )
        goto_pa = live.count('goto="product_analyzer"')
        assert goto_pa >= 2, (
            f"expected the happy-path returns to be Command(goto="
            f"'product_analyzer'); found {goto_pa} — with the static edge "
            "removed, dict returns would DEAD-END the graph"
        )

    def test_cross_domain_cleanup_command_still_present(self):
        node = _node_src()
        assert 'goto="cleanup"' in node, (
            "the cross-domain honest-fail exit must keep its Command"
        )

    def test_graph_still_compiles(self):
        import os as _os

        _os.environ.setdefault("PROJECT_ROOT", "/app")
        from agents.graph import build_scrape_graph

        g = build_scrape_graph()
        nodes = g.get_graph().nodes
        assert "browser_traverse" in nodes and "product_analyzer" in nodes
