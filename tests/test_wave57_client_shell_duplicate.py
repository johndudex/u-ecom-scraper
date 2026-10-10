"""[wave-57 class 3] Client-rendered listing starvation gets its own verdict.

wonderbly #1082/#1116: Astro shell + Algolia InstantSearch. The non-JS
listing serves ZERO grid anchors and ``?page=2`` returns the SAME shell —
pagination is client-side. The discovery loop read that as
``no_new_items`` (exhaustion vocabulary), the coverage gate no-op'd, and
the 2-link FAQ salvage shipped COMPLETED with 2 items against a
full-catalog source the traverse had already measured.

Fix chain (wave-48 T3's telemetry promoted to a verdict):
- ``src/listing_discovery.py``: DOM-identical consecutive shells on the
  ``no_new_items`` branch reclassify to ``stop_reason=client_shell_duplicate``
  (+ ``client_rendered_shell`` stamp).
- ``route_after_testing``: the reason joins ``_COVERAGE_FAIL_STOP_REASONS``
  (a field-PASS with a starved discovery falls through to the strategy
  cascade — the transport flip to the browser lane) and
  ``_KYG_BLOCKED_STOP_REASONS`` (no double-fire with the yield-gap gate).
- ``draft_safety.algolia_gate_prescribed``: the coverage stamp alone arms
  the wave-50 algolia-first gate for the flipped cycle's next draft (the
  resume path never measured the listing probe, so the LCR stamp is
  missing exactly on the jobs that need this).
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

from bs4 import BeautifulSoup  # noqa: E402

SHELL = (
    "<html><head><title>wonderbly</title></head><body>"
    '<div class="astro-shell">grid mounts client-side</div>'
    "</body></html>"
)

# The wonderbly shell's items live in a script-body payload (Astro/Next
# hydration JSON) — BeautifulSoup parses ZERO <a> anchors out of it, which
# is exactly the discriminator the client_shell_duplicate rule keys on.
SCRIPT_SHELL = (
    "<html><head><title>wonderbly</title></head><body>"
    '<div class="astro-shell">grid mounts client-side</div>'
    '<script type="application/json">'
    '{"hits":[{"url":"https://site.test/p/a"},'
    '{"url":"https://site.test/p/b"}]}'
    "</script>"
    "</body></html>"
)


def _shell_fetch(calls_box: list, page_plans: list):
    """fetch_page serving page_plans[i] (list of hrefs) for call i. The
    SHELL body is constant — the client-rendered site never re-renders."""
    def fetch_page(url, min_tier=0):
        calls_box.append(url)
        i = len(calls_box) - 1
        if i >= len(page_plans):
            return BeautifulSoup(
                "<html><body></body></html>", "html.parser"
            ), 200
        links = "".join(
            f'<a href="{h}">i</a>' for h in page_plans[i]
        )
        return BeautifulSoup(
            f"<html><body>{links}</body></html>", "html.parser"
        ), 200

    fetch_page.min_tier_floor = 0
    fetch_page.tiers_total = 1
    return fetch_page


def _extract_all(soup):
    return [a["href"] for a in soup.find_all("a")]


def _const_shell_fetch(calls_box: list, body: str):
    """fetch_page serving the SAME body for every ?page=N — the client-
    rendered site never re-renders."""
    def fetch_page(url, min_tier=0):
        calls_box.append(url)
        return BeautifulSoup(body, "html.parser"), 200

    fetch_page.min_tier_floor = 0
    fetch_page.tiers_total = 1
    return fetch_page


def _script_extract(soup):
    """Salvage the hydration payload out of script bodies — the only place
    the shell carries items (BS4 sees zero anchors in that document)."""
    import re

    return re.findall(r"https://site\.test/p/[a-z]+", str(soup))


class TestClientShellDuplicateStopReason:
    def test_identical_shell_reclassified(self):
        """The wonderbly signature: page 2 serves the SAME script-body
        salvage out of the SAME DOM, with ZERO parseable anchors — the site
        never served a second page and no browser parse could have."""
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _const_shell_fetch(calls, SCRIPT_SHELL)
        urls, meta = discover_listing_urls(
            fetch, ["https://site.test/c/x"], _script_extract
        )
        assert meta["stop_reason"] == "client_shell_duplicate"
        assert meta["client_rendered_shell"] is True
        assert meta["identical_last_page"] is True
        assert urls == ["https://site.test/p/a", "https://site.test/p/b"]

    def test_real_anchor_echo_stays_no_new_items(self):
        """Precision pin: a server-rendered one-page catalog whose ?page=2
        echoes page 1 byte-for-byte still serves REAL item anchors — DOM
        identity alone cannot tell it from the client shell, so the
        discriminator must keep the exhaustion verdict (and the ladder)."""
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _shell_fetch(calls, [
            ["https://site.test/p/a", "https://site.test/p/b"],
            ["https://site.test/p/a", "https://site.test/p/b"],
        ])
        _, meta = discover_listing_urls(
            fetch, ["https://site.test/c/x"], _extract_all
        )
        assert meta["stop_reason"] == "no_new_items"
        assert not meta.get("client_rendered_shell")

    def test_overlap_with_different_dom_stays_no_new_items(self):
        """Genuine pagination overlap: page 2's DOM differs (server rendered
        a real second page whose items were all seen). Exhaustion verdict
        stands."""
        from src.listing_discovery import discover_listing_urls

        calls: list = []

        def fetch(url, min_tier=0):
            calls.append(url)
            i = len(calls) - 1
            marker = "page-two-marker" if i == 1 else "page-one"
            plan = (["https://site.test/p/b", "https://site.test/p/a"]
                    if i == 1 else
                    ["https://site.test/p/a", "https://site.test/p/b"])
            links = "".join(f'<a href="{h}">i</a>' for h in plan)
            return BeautifulSoup(
                f"<html><body><span>{marker}</span>{links}</body></html>",
                "html.parser",
            ), 200

        fetch.min_tier_floor = 0
        fetch.tiers_total = 1
        _, meta = discover_listing_urls(
            fetch, ["https://site.test/c/x"], _extract_all
        )
        assert meta["stop_reason"] == "no_new_items"
        assert not meta.get("client_rendered_shell")

    def test_shrinking_page_stays_no_new_items(self):
        """A subset link set is not link-set equality — wave-48's third pin
        keeps its verdict under the stricter duplicate-shell rule."""
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _shell_fetch(calls, [
            ["https://site.test/p/a", "https://site.test/p/b"],
            ["https://site.test/p/a"],
        ])
        _, meta = discover_listing_urls(
            fetch, ["https://site.test/c/x"], _extract_all
        )
        assert meta["stop_reason"] == "no_new_items"

    def test_short_page_unaffected(self):
        from src.listing_discovery import discover_listing_urls

        calls: list = []
        fetch = _shell_fetch(calls, [["https://site.test/p/a"]])
        _, meta = discover_listing_urls(
            fetch, ["https://site.test/c/x"], _extract_all
        )
        assert meta["stop_reason"] == "short_page"


class TestRouterWiring:
    def _state(self, **over):
        report = {
            "overall_assessment": "PASS",
            "confidence_score": 0.9,
            "issues": [],
            "successful_extractions": 2,
            "phases_tested": {"phase1_discovery": True},
            "discovery_coverage": {
                "ran_phase1": True,
                "stop_reason": "client_shell_duplicate",
                "found": 2,
            },
        }
        state = {
            "job_id": 1082,
            "input_mode": "list_page",
            "site_slug": "wonderbly-com",
            "test_report": report,
            "test_retry_count": 0,
            "scope": "firstn",
            "scope_value": "10",
        }
        state.update(over)
        return state

    def test_client_shell_duplicate_in_coverage_fail_set(self):
        from agents.nodes.route_after_testing import (
            _COVERAGE_FAIL_STOP_REASONS,
        )

        assert "client_shell_duplicate" in _COVERAGE_FAIL_STOP_REASONS

    def test_client_shell_duplicate_in_kyg_blocked_set(self):
        from agents.nodes.route_after_testing import (
            _KYG_BLOCKED_STOP_REASONS,
        )

        assert "client_shell_duplicate" in _KYG_BLOCKED_STOP_REASONS

    def test_two_item_pass_with_starved_discovery_cascades(self):
        """The #1082 shipping shape must NOT route to field_confirmation —
        the strategy cascade owns the transport flip."""
        from agents.nodes.route_after_testing import route_after_testing

        assert route_after_testing(self._state()) == "scraper_analyzer"

    def test_exhausted_discovery_still_passes(self):
        """Control: the same run with a genuine-exhaustion reason keeps the
        field-PASS (the flip must not fire on healthy discovery)."""
        from agents.nodes.route_after_testing import route_after_testing

        state = self._state()
        state["test_report"]["discovery_coverage"]["stop_reason"] = (
            "no_next_link"
        )
        assert route_after_testing(state) == "field_confirmation"


class TestAlgoliaPrescription:
    def test_coverage_stamp_alone_arms_the_gate(self):
        """The resume-path shape: NO probe listing block at all (the stamp
        the LCR flip needs was never measured), but the run's own coverage
        proved the client shell. The next draft must carry algolia-first."""
        from agents.draft_safety import algolia_gate_prescribed

        state = {
            "input_mode": "list_page",
            "test_report": {
                "discovery_coverage": {
                    "ran_phase1": True,
                    "stop_reason": "client_shell_duplicate",
                    "found": 2,
                    "client_rendered_shell": True,
                },
            },
        }
        assert algolia_gate_prescribed(state) is True

    def test_pdp_url_list_job_not_prescribed(self):
        from agents.draft_safety import algolia_gate_prescribed

        state = {
            "input_mode": "url_list",
            "test_report": {
                "discovery_coverage": {
                    "ran_phase1": True,
                    "stop_reason": "client_shell_duplicate",
                },
            },
        }
        assert algolia_gate_prescribed(state) is False

    def test_plain_healthy_report_not_prescribed(self):
        from agents.draft_safety import algolia_gate_prescribed

        state = {
            "input_mode": "list_page",
            "test_report": {
                "discovery_coverage": {
                    "ran_phase1": True,
                    "stop_reason": "no_next_link",
                },
            },
        }
        assert algolia_gate_prescribed(state) is False


def test_template_priority_map_carries_the_fail_rank():
    """The template-side merge down-weights unknown tokens to 0 — the new
    FAIL reason must be ranked so a browser-lane cycle cannot bury it."""
    src = open(
        os.path.join(ROOT, "templates", "http_navigation_scraper.py")
    ).read()
    assert '"client_shell_duplicate": 5' in src
