"""Job-309 (pillowtalk e2e) regressions: the exhausted-retry rescue must not
read output files written by PREVIOUS test cycles' drafts, and a final FAIL
must not be auto-executed under skip_approvals.

What happened (job 309): the http_requests/http_navigation test cycles left
output_*.json files with a few real items; the third cycle escalated to
playwright (wrong for a server-rendered site), failed with 0 items, retries
hit the cap — and the rescue arm read the STALE files, routed to
field_confirmation (skip_approvals → run_execution), executed the broken
playwright draft, extracted 0, and finalize blessed the job COMPLETED.

Two fixes pinned here:
- freshness floor in `_scraper_has_real_items` (outputs must postdate the
  current scraper_draft.py — the F16 cross-artifact mtime-floor lesson);
- the is_final_attempt arm now treats skip_approvals like the exhausted
  cascade arm (honest cleanup / ground-truth rescue) instead of routing a
  FAIL to human_approval, which auto-approves and executes the failed draft.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_job309_rescue_freshness.py -q
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import importlib as _importlib

SLUG = "pillowtalktest"

# Contract-compliant draft (env gate) so the route reaches the cascade/rescue
# arms instead of bouncing on the CLI-contract check.
ENV_GATE_SHAPE = """
    import argparse, os
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--listing-url", type=str, default=None)
        parser.add_argument("--fresh-discovery", action="store_true")
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--input", type=str, default=None)
        args = parser.parse_args()
        _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
        if _env_listing or args.fresh_discovery or args.listing_url:
            discover(_env_listing or args.listing_url)
        elif args.input:
            load(args.input)
        elif os.path.exists(INPUT_FILE):
            load(INPUT_FILE)
"""


def _items(n):
    return [
        {
            "title": f"Pillow {i}",
            "price": f"${i}.99",
            "url": f"https://{SLUG}.example/p/{i}",
        }
        for i in range(1, n + 1)
    ]


def _route(tmp_path, state_updates=None, n_stale_outputs=0, n_fresh_outputs=0):
    """Build a workspace with `n_stale_outputs` output files written BEFORE the
    draft (the stale-cycle shape) and `n_fresh_outputs` written AFTER it (the
    current-cycle shape), then route the exhausted-cascade state."""
    ws = tmp_path / "workspace" / SLUG
    ws.mkdir(parents=True, exist_ok=True)
    out_paths = []
    for j in range(n_stale_outputs):
        p = ws / f"output_20260828_{j:04d}.json"
        p.write_text(json.dumps({"products": _items(3)}), encoding="utf-8")
        out_paths.append(p)
    (ws / "scraper_draft.py").write_text(
        textwrap.dedent(ENV_GATE_SHAPE), encoding="utf-8"
    )
    fresh_paths = []
    for j in range(n_fresh_outputs):
        p = ws / f"output_20260828_9{j:03d}.json"
        p.write_text(json.dumps({"products": _items(1)}), encoding="utf-8")
        fresh_paths.append(p)
    if out_paths:
        # All outputs predate the current draft → previous cycles' files.
        old = time.time() - 3600
        for p in out_paths:
            os.utime(str(p), (old, old))

    base = {
        "site_slug": SLUG,
        "input_mode": "list_page",
        "test_report": {
            "overall_assessment": "FAIL",
            "confidence_score": 0.3,
            "issues": [],
            "ready_for_execution": False,
        },
        "test_retry_count": 2,  # == MAX_TEST_RETRIES → exhausted-cascade arm
        "skip_approvals": True,  # intake default — human_approval auto-approves
        "content_type_config": {"content_type": "product", "output_key": "products"},
        "probe_result": {},
    }
    base.update(state_updates or {})
    rat = _importlib.import_module("agents.nodes.route_after_testing")
    with mock.patch.dict(os.environ, {"PROJECT_ROOT": str(tmp_path)}):
        return rat.route_after_testing(base)


class TestStaleOutputRescue:
    """The freshness floor: previous cycles' outputs are not ground truth."""

    def test_stale_outputs_cannot_rescue_exhausted_draft(self, tmp_path):
        """THE job-309 path: 3 stale 3-item outputs + exhausted FAIL draft →
        must be an honest cleanup, not a rescue into execution."""
        assert _route(tmp_path, n_stale_outputs=3) == "cleanup"

    def test_fresh_output_still_rescues(self, tmp_path):
        """Guard-rail: a CURRENT-draft output (1 real item, below the 3-item
        ground-truth override but above the list_page rescue floor) still
        rescues. Written after the draft → passes the freshness floor."""
        assert _route(tmp_path, n_fresh_outputs=1) == "field_confirmation"


class TestFinalAttemptSkipApprovals:
    """Fix A: a FINAL-attempt FAIL under skip_approvals must not be routed to
    human_approval (auto-approve → executes the draft the tester just
    failed). Same treatment as the exhausted-cascade arm."""

    def test_final_fail_no_items_goes_to_cleanup(self, tmp_path):
        assert (
            _route(tmp_path, {"test_retry_count": 99}) == "cleanup"
        )

    def test_final_fail_rescues_on_real_items_in_report(self, tmp_path):
        state = {
            "test_retry_count": 99,
            "test_report": {
                "overall_assessment": "FAIL",
                "confidence_score": 0.3,
                "issues": [],
                "sample_products": _items(1),
            },
        }
        assert _route(tmp_path, state) == "field_confirmation"

    def test_final_fail_with_human_still_goes_to_approval(self, tmp_path):
        """Guard-rail: with a human in the loop (skip_approvals=False) the
        final-attempt FAIL still goes to human_approval — Fix A only changes
        the unattended path."""
        assert (
            _route(tmp_path, {"test_retry_count": 99, "skip_approvals": False})
            == "human_approval"
        )


class TestGraphListPageFallbackPin:
    """Fix B (job 309): the listing-reachability fallback's list_page branch
    must consider the JOB URL itself, not only search_criteria. For list_page
    jobs the user-provided listing IS state["url"]; when intake's
    search_criteria is empty the old code fell back to the site root, which
    poisoned discovery.listing_url and the SCRAPER_LISTING_URL env gate."""

    def test_list_page_fallback_considers_job_url(self):
        graph_path = os.path.join(ROOT, "webapp", "agents", "graph.py")
        with open(graph_path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("_fallback_listing = _site_root")
        assert start != -1, "listing-reachability fallback block moved"
        window = src[start : start + 3000]
        assert 'state.get("search_criteria")' in window, (
            "fallback should still prefer search_criteria first"
        )
        assert 'state.get("url")' in window, (
            "list_page fallback must also consider the job URL (Fix B)"
        )


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
