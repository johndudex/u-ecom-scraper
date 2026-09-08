"""[wave-26 W26-3 narrowed] Terminal-cleanup invariant.

Prod 410 (karenmillen): the tester returned PASS 0.82 with 1 real product
and 48 discovered URLs; the freshness-floor bug (W26-10) made every
``_scraper_has_real_items`` rescue return False, so the exhausted-cascade
skip_approvals arm returned ``cleanup`` SILENTLY — a job with a working
scraper on disk ended as an honest failure with zero output.

W26-10 fixes the rescue predicate. This invariant is the belt behind it:
NO exhausted arm may return ``cleanup`` while the six ground-truth vetoes
are clean AND the workspace holds at least one real item. When that holds,
the job goes to execution — the same decision the ground-truth override
(route_after_testing's GROUND-TRUTH PASS arm) would have made earlier.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import importlib  # noqa: E402
import pytest  # noqa: E402

# agents.nodes re-exports the route_after_testing FUNCTION, so a plain
# `from agents.nodes import route_after_testing` binds the function, not
# the module — go through importlib for the module itself.
rat = importlib.import_module("agents.nodes.route_after_testing")

ROUTER_SRC = os.path.join(
    ROOT, "webapp", "agents", "nodes", "route_after_testing.py"
)
GRAPH_SRC = os.path.join(ROOT, "webapp", "agents", "graph.py")

CLEAN_VETOES = dict(
    cov_reason=None,
    missing_core=None,
    contract_bad=False,
    count_regression=None,
    volume_reason=None,
    det_blockers=[],
)


def _state(**over):
    base = {
        "site_slug": "karenmillen",
        "input_mode": "list_page",
        "skip_approvals": True,
        "test_report": {
            "overall_assessment": "PASS",
            "confidence_score": 0.82,
            "sample_products": [
                {"title": "Knit Dress", "price": "£89.00", "url": "https://x/p/1"},
            ],
        },
    }
    base.update(over)
    return base


def _guard(state, dest, **veto_over):
    kwargs = {**CLEAN_VETOES, **veto_over}
    return rat._exhausted_cleanup_redirect(state, dest, **kwargs)


class TestGuardBehavior:
    def test_clean_vetoes_plus_real_item_redirects_cleanup(self):
        """410's terminal state: exhausted arm, cleanup destination, one real
        item, every veto clean → must NOT end the job."""
        assert _guard(_state(), "cleanup") == "run_execution"

    def test_human_approval_passes_through(self):
        """The invariant targets the silent kill (cleanup). human_approval in
        a supervised run is a legitimate user decision point."""
        assert _guard(_state(), "human_approval") == "human_approval"

    def test_high_severity_does_not_block_redirect(self):
        """Same stance as the GROUND-TRUTH PASS arm: real items override the
        tester's severity flags (they're variance-prone). The six VETOES are
        cov/missing-core/contract/count-regression/volume/det-blockers."""
        assert _guard(_state(), "cleanup") == "run_execution"

    def test_contract_bad_blocks_redirect(self):
        assert _guard(_state(), "cleanup", contract_bad=True) == "cleanup"

    def test_coverage_reason_blocks_redirect(self):
        assert (
            _guard(_state(), "cleanup", cov_reason="ratio <threshold")
            == "cleanup"
        )

    def test_det_blockers_block_redirect(self):
        assert (
            _guard(_state(), "cleanup", det_blockers=[{"field": "price"}])
            == "cleanup"
        )

    def test_no_real_items_blocks_redirect(self):
        """No sample_products and no workspace output → the honest-failure
        cleanup stands (nothing proven to work)."""
        state = _state()
        state["test_report"] = {
            "overall_assessment": "FAIL",
            "confidence_score": 0.3,
        }
        assert _guard(state, "cleanup") == "cleanup"


class TestWiring:
    def test_all_three_exhausted_arms_wrap_their_cleanup(self):
        src = open(ROUTER_SRC).read()
        for arm in (
            "contract-exhausted",
            "final-attempt-cleanup",
            "cascade-exhausted-cleanup",
        ):
            i = src.find(f'"{arm}"')
            assert i != -1, f"arm row missing: {arm}"
            window = src[i: i + 900]
            assert "_exhausted_cleanup_redirect(" in window, (
                f"the {arm} arm must route its terminal cleanup through the "
                "terminal-cleanup invariant (prod 410)"
            )

    def test_edge_map_carries_run_execution(self):
        """The guard returns ``run_execution`` — the code_tester conditional
        edge map must accept that destination."""
        src = open(GRAPH_SRC).read()
        i = src.find('"code_tester": "code_tester"')
        assert i != -1, "router edge map moved"
        window = src[i: i + 300]
        assert '"run_execution": "run_execution"' in window, (
            "add run_execution to route_after_testing's edge map — the "
            "invariant's redirect destination must be a wired node"
        )

    def test_guard_mentions_invariant_and_min_count_one(self):
        src = open(ROUTER_SRC).read()
        i = src.find("def _exhausted_cleanup_redirect(")
        body = src[i: i + 2600]
        assert "min_count=1" in body
        assert "W26-3" in body or "prod-410" in body
