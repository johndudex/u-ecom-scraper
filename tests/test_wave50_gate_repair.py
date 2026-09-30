"""[wave-50 T5d] The writer GUTTED the algolia-first gate in job 488's draft:
``_discover_phase1`` kept its name but lost ``_discover_urls_via_algolia``
and the ``src.algolia_discovery`` import entirely — the anchor path shipped 5
nav/collection links as products and the e2e went red with the CHECK-FIRST
brief verifiably live in the writer context. LLM compliance is the third
layer of the job-1116 disease (after the dead brief key and the template
bypass); the answer is deterministic, like wave-37's ladder repair: detect
the gutted gate at tester entry and splice it back from the template.

Also pinned here: the CHECK-FIRST prescription predicate, shared by the
writer brief and the tester-entry guard so the two cannot drift.
"""

import ast
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_REQ = ROOT / "templates" / "requests_scraper.py"
_NAV = ROOT / "templates" / "http_navigation_scraper.py"


# ── the prescription predicate (shared: brief + tester guard) ────────────────


class TestCheckFirstPredicate:
    def _prescribed(self, state):
        from agents.draft_safety import algolia_gate_prescribed

        return algolia_gate_prescribed(state)

    def test_discovery_mode_http_ok_is_prescribed(self):
        assert self._prescribed({
            "input_mode": "list_page",
            "probe_result": {"listing_connectivity": {"http_ok": True}},
        }) is True

    def test_cloak_only_probe_is_not_prescribed(self):
        """Job-1107 class: the listing won on cloak_none — a BROWSER rung.
        No HTTP tier, no creds hunt."""
        assert self._prescribed({
            "input_mode": "list_page",
            "probe_result": {"listing_connectivity": {
                "http_ok": False, "browser_ok": True}},
        }) is False

    def test_url_list_is_never_prescribed(self):
        assert self._prescribed({
            "input_mode": "url_list",
            "probe_result": {"listing_connectivity": {"http_ok": True}},
        }) is False

    def test_missing_probe_is_not_prescribed(self):
        assert self._prescribed({"input_mode": "navigation"}) is False

    def test_navigation_mode_with_lcr_is_prescribed(self):
        assert self._prescribed({
            "input_mode": "navigation",
            "probe_result": {"listing_connectivity": {
                "http_ok": False, "listing_client_rendered": True}},
        }) is True


# ── the repair itself ─────────────────────────────────────────────────────────


def _gutted_draft_src() -> str:
    """The job-488 shape: the gate survives in NAME only — its body routes
    straight to the anchor path, no algolia import, no pre-step helper."""
    return (
        "import re\n"
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "from src.listing_discovery import discover_listing_urls_with_retry\n"
        "PRODUCT_LISTING_URLS = [\"https://x.example/list\"]\n"
        "fetch_page = object()\n"
        "fetch_text = object()\n"
        "def _extract_listing_links(soup):\n"
        "    return []\n"
        "\n"
        "def _discover_phase1(discovery_cfg: dict, limit=None):\n"
        "    return discover_listing_urls_with_retry(\n"
        "        fetch_page, PRODUCT_LISTING_URLS, _extract_listing_links,\n"
        "        **discovery_cfg,\n"
        "    )\n"
        "\n"
        "def main():\n"
        "    product_urls, discovery_meta = _discover_phase1({}, None)\n"
    )


class TestRepairAlgoliaGate:
    def _repair(self, draft_src, template_path):
        from agents.draft_safety import repair_algolia_gate_violation

        return repair_algolia_gate_violation(
            draft_src, template_path.read_text(encoding="utf-8")
        )

    def test_gutted_gate_is_spliced_back(self):
        repaired = self._repair(_gutted_draft_src(), _REQ)
        assert repaired is not None
        ast.parse(repaired)  # must still parse
        assert "from src.algolia_discovery import" in repaired
        assert "def _discover_urls_via_algolia(" in repaired
        # the gate now references the pre-step AND keeps the anchor fallback
        import re as _re

        gate = _re.search(
            r"def _discover_phase1\(.*?(?=\ndef |\nclass |\Z)", repaired, _re.S
        ).group(0)
        assert "_discover_urls_via_algolia(" in gate
        assert "discover_listing_urls_with_retry(" in gate
        algolia_at = gate.find("_discover_urls_via_algolia(")
        retry_at = gate.find("discover_listing_urls_with_retry(")
        assert 0 < algolia_at < retry_at  # algolia FIRST

    def test_already_wired_draft_is_none(self):
        """Idempotence + honesty: a compliant draft is untouched."""
        from agents.draft_safety import repair_algolia_gate_violation

        src = _REQ.read_text(encoding="utf-8")
        assert repair_algolia_gate_violation(src, src) is None

    def test_unparseable_draft_is_none(self):
        assert self._repair("def broken(:\n", _REQ) is None

    def test_template_missing_the_helper_is_none(self):
        assert self._repair(_gutted_draft_src(), ROOT / "src" / "content_types.py") is None

    def test_requests_template_carries_the_spliceable_pieces(self):
        """The ONLY repairable shape: the requests template defines both
        functions and the (parenthesized, multi-line) import."""
        import re as _re

        src = _REQ.read_text(encoding="utf-8")
        assert "def _discover_urls_via_algolia(" in src
        assert "def _discover_phase1(" in src
        assert _re.search(
            r"^from src\.algolia_discovery import \([^)]*\)", src, _re.M
        ), "parenthesized multi-line import must be spliceable"

    def test_nav_template_has_prestep_but_no_gate_fn(self):
        """The http_navigation template wires the pre-step per-branch inside
        main() — no `_discover_phase1` gate function, so the repair honestly
        declines that shape (None) instead of splicing into main()."""
        src = _NAV.read_text(encoding="utf-8")
        assert "def _discover_urls_via_algolia(" in src
        assert "def _discover_phase1(" not in src


# ── the tester-entry wiring ───────────────────────────────────────────────────


class TestTesterEntryWiring:
    def test_invoke_code_tester_calls_the_repair(self):
        """The guard must sit in the tester entry, budget-gated, beside the
        ladder repair — the writer cannot be trusted to keep the gate."""
        src = (ROOT / "webapp" / "agents" / "graph.py").read_text(encoding="utf-8")
        assert "repair_algolia_gate_violation" in src
        assert "algolia_gate_prescribed" in src
        # budget-gated like ladder_repairs
        assert "gate_repairs" in src

    def test_brief_uses_the_shared_predicate(self):
        """The writer brief and the tester guard must not drift: the brief's
        CHECK-FIRST decision delegates to the same helper."""
        src = (ROOT / "webapp" / "agents" / "subagents.py").read_text(encoding="utf-8")
        assert "algolia_gate_prescribed" in src
