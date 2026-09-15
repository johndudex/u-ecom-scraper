"""[wave-32 C2] Evidence-based remap denial — provenance-guarded.

A mapping failure whose field the live render PROVED dead (field_verification
wrote ``tested="empty"`` under ``render_provenance="full"``) cannot be fixed
by re-mapping: product_analyzer re-maps a source that demonstrably produced
nothing, burning a remap cycle (and a full writer window after it) on a field
no selector can fill (587's remap loop). The arm is denied to
``field_confirmation`` — the existing coverage-gap interrupt, which
auto-acknowledges under skip_approvals and carries the same approval UX in
prod — with the honest framing that re-mapping cannot create a source.

Denial is NARROW (critique finding 9): it requires BOTH the ``empty`` verdict
AND full-render provenance on the field entry. A stale verdict (provenance
not full) or an untested mapping keeps today's remap behavior.
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

# NOTE: `from webapp.agents.nodes import route_after_testing` binds the
# FUNCTION (package re-export) — import the module for the router + helpers.
rat = importlib.import_module("webapp.agents.nodes.route_after_testing")


def _state(fields: dict) -> dict:
    return {
        "job_id": 0,
        "site_slug": "no-such-workspace-slug",
        "input_mode": "url_list",
        "test_retry_count": 0,
        "remap_count": 0,
        "test_report": {
            "overall_assessment": "FAIL",
            "confidence_score": 0.6,
            "successful_extractions": 5,
            "issues": [],
            "remediation": {"target": "mapping", "fields": ["price"]},
        },
        "content_analysis": {"fields": fields},
    }


class TestRemapDenial:
    def test_dead_source_full_provenance_denies_remap(self):
        """tested=empty + render_provenance=full → the source is PROVEN dead;
        route to field_confirmation, not product_analyzer."""
        st = _state({
            "price": {"selector": ".price", "tested": "empty",
                      "render_provenance": "full"},
        })
        assert rat.route_after_testing(st) == "field_confirmation"

    def test_empty_without_full_provenance_still_remaps(self):
        """The stale-verdict guard: an 'empty' not proven on a full render
        keeps today's remap behavior."""
        st = _state({
            "price": {"selector": ".price", "tested": "empty",
                      "render_provenance": "truncated"},
        })
        assert rat.route_after_testing(st) == "product_analyzer"

    def test_untested_mapping_still_remaps(self):
        st = _state({"price": {"selector": ".price"}})
        assert rat.route_after_testing(st) == "product_analyzer"

    def test_partially_dead_mapping_still_remaps(self):
        """Deny only when EVERY named failing field is proven dead — one
        live field in the list means a remap can still fix something."""
        st = _state({
            "price": {"selector": ".price", "tested": "empty",
                      "render_provenance": "full"},
            "title": {"selector": "h1"},
        })
        st["test_report"]["remediation"]["fields"] = ["price", "title"]
        assert rat.route_after_testing(st) == "product_analyzer"
