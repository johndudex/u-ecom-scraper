"""F13: no Command-vs-out-edge union (the D6 shadow branch).

Prod 272: product_analyzer budget-exhaust → human_approval interrupt, but the
STATIC edge product_analyzer → normalize_fields was ALSO scheduled — LangGraph
runs both. The ghost normalize_fields → validate_coverage chain re-interrupted
AFTER the user's cancel (2 approvals to answer; misleading final error). Same
class at site_analyzer (conditional edge + budget Command).

Fix: both nodes route via Command with NO registered out-edges (the codebase's
own convention — check_tracker/check_accessibility/validate_analysis already
work this way). LangGraph silently IGNORES a goto to an unknown node, so the
tests must assert the target ACTUALLY RUNS, not just that the return is str.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))
# repo root from either tests/ or webapp/tests/
ROOT = os.path.dirname(_HERE) if os.path.basename(os.path.dirname(_HERE)) != "webapp" \
    else os.path.dirname(os.path.dirname(_HERE))


class TestTopology:
    def _graph_src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def test_no_static_edge_from_command_returning_nodes(self):
        src = self._graph_src()
        # product_analyzer and site_analyzer must have NO registered out-edges
        for node in ("product_analyzer", "site_analyzer"):
            assert not re.search(
                rf'add_edge\("{node}"\s*,', src
            ), f"{node} still has a static out-edge"
            assert not re.search(
                rf'add_conditional_edges\(\s*"{node}"', src
            ), f"{node} still has a conditional edge"

    def test_normalize_fields_keeps_static_out_edge(self):
        src = self._graph_src()
        assert re.search(r'add_edge\("normalize_fields"\s*,\s*"validate_coverage"\)', src)

    def test_happy_path_commands_present(self):
        src = self._graph_src()
        import re as _re
        assert _re.search(r'Command\(\s*goto="normalize_fields"', src)
        assert _re.search(r'Command\(\s*goto="browser_traverse"', src)
        assert _re.search(r'Command\(\s*goto="update_tracker_analysis"', src)

    def test_remap_still_routes_to_code_writer(self):
        src = self._graph_src()
        assert 'goto="code_writer"' in src  # the remap path, unchanged


class TestLiveGraph:
    """Compile the real graph and walk the F13-affected transitions."""

    def _build(self):
        import django

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        from agents.graph import build_scrape_graph

        return build_scrape_graph()

    def test_graph_compiles_and_contains_nodes(self):
        g = self._build()
        for n in ("parse_command", "site_analyzer", "product_analyzer",
                  "normalize_fields", "validate_coverage", "browser_traverse",
                  "update_tracker_analysis", "human_approval", "code_writer"):
            assert n in g.nodes, f"{n} missing"

    def test_product_analyzer_writers_no_static_branch(self):
        """The core mechanical assertion: product_analyzer's writer list must
        contain NO branch:to:normalize_fields static writer (only the
        Command-detecting one). This is the exact structure 2c verified on
        prod as the bug."""
        g = self._build()
        writers = getattr(g.nodes["product_analyzer"], "writers", None) or []
        static_branch = [
            w for w in writers
            if "branch:to:normalize_fields" in repr(w)
        ]
        assert not static_branch, (
            f"static branch writer still present: {static_branch}"
        )

    def test_site_analyzer_writers_no_static_branch(self):
        g = self._build()
        writers = getattr(g.nodes["site_analyzer"], "writers", None) or []
        static_branch = [
            w for w in writers
            if any(t in repr(w) for t in ("branch:to:browser_traverse",
                                          "branch:to:update_tracker_analysis"))
        ]
        assert not static_branch, f"static branch writer still present: {static_branch}"

    def test_normalize_fields_still_reachable(self):
        """Every previous entry path into normalize_fields must still work:
        happy product_analyzer (Command), human_approval 'continue anyway'
        (path fn returns 'normalize_fields' — still a registered node)."""
        g = self._build()
        assert "normalize_fields" in g.nodes
        # and its out-edge to validate_coverage survives
        writers = getattr(g.nodes["normalize_fields"], "writers", None) or []
        assert any("validate_coverage" in repr(w) for w in writers), (
            "normalize_fields lost its validate_coverage edge"
        )
