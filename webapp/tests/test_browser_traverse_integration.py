"""Integration test for the ``browser_traverse`` node.

The three legacy navigation nodes (``navigation_explore`` →
``navigation_agent`` → ``navigation_synthesize``) have been collapsed into a
single deterministic ``browser_traverse`` node. This test verifies the graph
wiring without invoking the real Playwright MCP browser — the underlying
traversal function is mocked so the test is hermetic and network-free.

Covers:
* The compiled graph registers a ``browser_traverse`` node.
* ``_route_after_site_analyzer`` routes navigation / list_page modes to
  ``browser_traverse`` (and url_list / search_term elsewhere).
* ``PHASE_MAP`` (agents.graph) and ``PIPELINE_PHASES`` (scraper.tasks) both
  advertise the new node.

Run inside the Django container::

    docker compose exec django pytest webapp/tests/test_browser_traverse_integration.py -q
"""

import os
import sys
from unittest.mock import patch

# Make `agents`, `scraper`, `config` importable (webapp/ on sys.path) and ensure
# Django is configured before importing modules that read django.conf.settings.
_WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WEBAPP not in sys.path:
    sys.path.insert(0, _WEBAPP)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

if not getattr(django, "_setup_done_", False):
    django.setup()


def _graph_node_names():
    """Build the compiled scrape graph and return its registered node names.

    ``build_scrape_graph`` is imported lazily so module import alone (which
    runs at collection time) doesn't pay the cost of wiring the full graph.
    The Playwright MCP client is stubbed so node construction never reaches
    out to a real browser service.
    """
    with patch("agents.tools.playwright_tools.create_playwright_tools") as mock_pw:
        mock_pw.return_value = []  # no browser tools — node wiring still succeeds
        from agents.graph import build_scrape_graph

        graph = build_scrape_graph(checkpointer=None)
    # CompiledStateGraph.nodes is a dict mapping node name -> Node spec.
    return set(getattr(graph, "nodes", {}).keys())


# ── Graph registration ─────────────────────────────────────────────────────


def test_graph_includes_browser_traverse_node():
    """The compiled graph must register the new ``browser_traverse`` node."""
    assert "browser_traverse" in _graph_node_names(), (
        "browser_traverse node not registered in the compiled scrape graph"
    )


def test_graph_no_longer_registers_legacy_navigation_nodes():
    """The 3 legacy navigation nodes should be removed from the graph.

    They may still exist as archived functions (so other code that imports
    them continues to parse), but they must not be wired into the graph.
    """
    nodes = _graph_node_names()
    legacy = {"navigation_explore", "navigation_agent", "navigation_synthesize"}
    present = nodes & legacy
    assert not present, (
        f"Legacy navigation nodes still registered as graph nodes: {present}"
    )


# ── Routing ─────────────────────────────────────────────────────────────────


def test_route_after_site_analyzer_navigation_modes():
    """navigation + list_page modes route to browser_traverse."""
    from agents.graph import _route_after_site_analyzer

    assert _route_after_site_analyzer({"input_mode": "navigation"}) == "browser_traverse"
    assert _route_after_site_analyzer({"input_mode": "list_page"}) == "browser_traverse"


def test_route_after_site_analyzer_non_navigation_modes():
    """url_list + search_term modes do NOT route to browser_traverse."""
    from agents.graph import _route_after_site_analyzer

    assert _route_after_site_analyzer({"input_mode": "url_list"}) != "browser_traverse"
    assert _route_after_site_analyzer({"input_mode": "search_term"}) != "browser_traverse"


# ── Phase registries ─────────────────────────────────────────────────────────


def test_phase_map_includes_browser_traverse():
    from agents.graph import PHASE_MAP

    assert "browser_traverse" in PHASE_MAP


def test_pipeline_phases_includes_browser_traverse():
    from scraper.tasks import PIPELINE_PHASES

    assert "browser_traverse" in PIPELINE_PHASES


# ── Hermeticity: graph can be built with the browser MCP stubbed out ────────


def test_graph_builds_without_live_browser():
    """The integration test must not require a real Playwright MCP connection.

    Patching ``create_playwright_tools`` (the network boundary used by
    ``get_tools_for_agent``) is sufficient to build the graph. This proves
    the browser traversal is mockable end-to-end — no real browser is
    instantiated when wiring nodes.
    """
    with patch("agents.tools.playwright_tools.create_playwright_tools") as mock_pw:
        mock_pw.return_value = []
        from agents.graph import build_scrape_graph

        graph = build_scrape_graph(checkpointer=None)
    assert graph is not None
    mock_pw.assert_called()
