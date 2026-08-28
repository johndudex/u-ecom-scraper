"""Integration test for the ``browser_traverse`` node.

The three legacy navigation nodes (``navigation_explore`` →
``navigation_agent`` → ``navigation_synthesize``) have been collapsed into a
single deterministic ``browser_traverse`` node. This test verifies the graph
wiring without invoking the real Playwright MCP browser — the underlying
traversal function is mocked so the test is hermetic and network-free.

Covers:
* The compiled graph registers a ``browser_traverse`` node.
* ``_route_after_site_analyzer`` routes the three discovery modes
  (``navigation`` / ``list_page`` / ``search_term``) to ``browser_traverse``
  and only ``url_list`` straight to ``update_tracker_analysis``.
  ``search_term`` is deliberately a navigation mode — omitting it bypasses
  discovery entirely (see CLAUDE.md "Critical bug" note and
  tests/test_content_types.py::test_route_after_site_analyzer_navigation).
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


def _build_hermetic_graph():
    """Build the compiled scrape graph with both Playwright MCP factories stubbed.

    ``build_scrape_graph`` is imported lazily so module import alone (which
    runs at collection time) doesn't pay the cost of wiring the full graph.
    Both the async and sync Playwright MCP factories are patched so that, if
    any wiring step ever did reach for a browser, it would hit a stub instead
    of a real MCP connection.
    """
    with (
        patch("agents.tools.playwright_tools.create_playwright_tools") as mock_async,
        patch(
            "agents.tools.playwright_tools.create_playwright_tools_sync"
        ) as mock_sync,
    ):
        mock_async.return_value = []  # no browser tools either way
        mock_sync.return_value = []
        from agents.graph import build_scrape_graph

        graph = build_scrape_graph(checkpointer=None)
    return graph, mock_async, mock_sync


def _graph_node_names():
    """Registered node names of the hermetically built scrape graph."""
    graph, _mock_async, _mock_sync = _build_hermetic_graph()
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
    """navigation + list_page + search_term all route to browser_traverse."""
    from agents.graph import _route_after_site_analyzer

    assert (
        _route_after_site_analyzer({"input_mode": "navigation"}) == "browser_traverse"
    )
    assert _route_after_site_analyzer({"input_mode": "list_page"}) == "browser_traverse"
    # search_term is a DISCOVERY mode: item URLs must be found before they can
    # be scraped, so it follows the browser traversal. Dropping it from this
    # tuple silently bypasses navigation (the known "critical bug" in
    # CLAUDE.md) and lands search_term jobs in the url_list flow with no item
    # URLs — asserted here so that regression cannot come back.
    assert (
        _route_after_site_analyzer({"input_mode": "search_term"}) == "browser_traverse"
    )


def test_route_after_site_analyzer_url_list_bypasses_navigation():
    """url_list (and the default) skip browser_traverse entirely.

    The user already supplied the item URLs, so there is nothing to discover —
    the flow goes straight to tracker/content analysis.
    """
    from agents.graph import _route_after_site_analyzer

    assert _route_after_site_analyzer({"input_mode": "url_list"}) == (
        "update_tracker_analysis"
    )
    # Missing input_mode defaults to url_list semantics (no discovery).
    assert _route_after_site_analyzer({}) == "update_tracker_analysis"


# ── Phase registries ─────────────────────────────────────────────────────────


def test_phase_map_includes_browser_traverse():
    from agents.graph import PHASE_MAP

    assert "browser_traverse" in PHASE_MAP


def test_pipeline_phases_includes_browser_traverse():
    from scraper.tasks import PIPELINE_PHASES

    assert "browser_traverse" in PIPELINE_PHASES


# ── Hermeticity: graph can be built with the browser MCP stubbed out ────────


def test_graph_builds_without_live_browser():
    """Graph wiring must never touch the Playwright MCP boundary.

    Since the 3 legacy navigation nodes collapsed into the deterministic
    ``browser_traverse`` node, the browser is reached only at RUN time —
    ``create_playwright_tools_sync()`` is called inside the traversal
    (agents/nodes/navigate_explore.py) and by ``get_tools_for_agent`` when an
    agent subgraph is first invoked. Agent factories are handed to nodes as
    ``agent_factory=...`` callables and constructed lazily, so building the
    graph is pure wiring: with both Playwright factories stubbed, a build must
    succeed and must not call either of them.
    """
    graph, mock_async, mock_sync = _build_hermetic_graph()
    assert graph is not None
    assert mock_async.call_count == 0, (
        "build_scrape_graph called create_playwright_tools at wiring time — "
        "graph construction reached for a browser"
    )
    assert mock_sync.call_count == 0, (
        "build_scrape_graph called create_playwright_tools_sync at wiring time — "
        "graph construction reached for a browser"
    )


def test_site_analyzer_has_no_registered_out_edges():
    """site_analyzer routes via Command only (F13) — no out-edges at all.

    LangGraph executes BOTH a Command ``goto`` and any registered out-edges,
    which is the D6 shadow-branch bug: a paused job ran the ghost
    update_tracker_analysis → validate_analysis chain in parallel with a
    human_approval interrupt. Wiring a static/conditional edge from
    site_analyzer back in would resurrect it.
    """
    graph, _mock_async, _mock_sync = _build_hermetic_graph()
    out_edges = [
        e
        for e in graph.get_graph().edges
        if getattr(e, "source", None) == "site_analyzer"
    ]
    assert not out_edges, (
        f"site_analyzer must have no registered out-edges (F13 Command routing), "
        f"found: {out_edges}"
    )
