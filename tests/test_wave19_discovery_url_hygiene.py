"""[wave-19 T1.9] Discovered-URL hygiene wall (local e2e job 332, myhouse).

The myhouse draft harvested listing nodes carrying PROTOCOL-RELATIVE paths
(``path = '//products/arcosteel-popcorn-maker-black'``) and urljoin'd them:
``urljoin(SITE, '//products/x')`` -> ``https://products/x`` — the path's first
segment becomes the netloc. Discovery reported **80 URLs**; execution burned
all 10 item fetches on a nonexistent host; the extraction quality gate
honestly failed the run 0-good ~25 minutes later. Nothing in the harness
validated discovered-URL *shape*: the Phase-1 probe counts URLs, the tester
extracts seed URLs only.

Wall (T1.9):
  1. a pure hygiene classifier — URLs that are not absolute http(s) on the
     job's own registrable domain;
  2. ``_discovery_yield_hygiene`` — a majority-malformed probe yield DEADENS
     the yield (discovered_urls -> 0, stop_reason 'malformed_discovery_urls')
     so the deterministic zero-yield FAIL arm owns it with precise writer
     feedback and the T1.2 tier escalation still applies;
  3. the probe wiring reads the draft's fresh ``workspace/<slug>/input_urls.json``
     (mtime-floored, same discipline as the probe-output floor);
  4. run_execution's quality-gate diagnosis NAMES malformed discovery URLs —
     a 25-minute burn is never silent again;
  5. the templates ship ``_absolute_site_url`` (protocol-relative -> site path)
     and the writer prompt carries the gotcha.

Run: docker compose exec -T django pytest /app/tests/test_wave19_discovery_url_hygiene.py -q
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

JOB_URL = "https://myhouse.com.au/products/arcosteel-popcorn-maker-black"


class TestDiscoveredUrlIssues:
    def test_importable_from_graph(self):
        from agents.graph import _discovered_url_issues

        assert callable(_discovered_url_issues)

    def test_absolute_same_domain_is_clean(self):
        from agents.graph import _discovered_url_issues

        urls = [
            "https://myhouse.com.au/products/a",
            "https://www.myhouse.com.au/products/b",
        ]
        assert _discovered_url_issues(urls, JOB_URL) == []

    def test_protocol_relative_value_yields_malformed_host(self):
        # the myhouse shape: urljoin(SITE, '//products/x') -> https://products/x
        from agents.graph import _discovered_url_issues

        bad = _discovered_url_issues(["https://products/arcosteel-x"], JOB_URL)
        assert bad == ["https://products/arcosteel-x"]

    def test_off_domain_flagged(self):
        from agents.graph import _discovered_url_issues

        bad = _discovered_url_issues(
            ["https://evil.example.com/products/a"], JOB_URL
        )
        assert len(bad) == 1

    def test_relative_path_flagged_not_fetchable(self):
        # the discovered set goes STRAIGHT into fetch — a bare path is not
        # fetchable, so it fails hygiene even though urljoin could absolutize
        from agents.graph import _discovered_url_issues

        assert _discovered_url_issues(["/products/a"], JOB_URL) == ["/products/a"]

    def test_garbage_and_empty_flagged(self):
        from agents.graph import _discovered_url_issues

        bad = _discovered_url_issues(["", "   ", "not a url", None], JOB_URL)
        assert len(bad) == 4

    def test_no_job_url_no_ownership_no_flags(self):
        from agents.graph import _discovered_url_issues

        assert _discovered_url_issues(["https://products/x"], "") == []


class TestDiscoveryYieldHygiene:
    def test_healthy_yield_untouched(self):
        from agents.graph import _discovery_yield_hygiene

        y = {"discovered_urls": 80, "stop_reason": "no_next_link", "coverage": {}}
        urls = [f"https://myhouse.com.au/products/p{i}" for i in range(80)]
        out = _discovery_yield_hygiene(y, urls, JOB_URL)
        assert out is y or out == y
        assert out["discovered_urls"] == 80

    def test_majority_malformed_deads_the_yield(self):
        from agents.graph import _discovery_yield_hygiene

        y = {"discovered_urls": 80, "stop_reason": "no_next_link", "coverage": {}}
        urls = [f"https://products/p{i}" for i in range(80)]
        out = _discovery_yield_hygiene(y, urls, JOB_URL)
        assert out["discovered_urls"] == 0
        assert out["stop_reason"] == "malformed_discovery_urls"
        hy = out["coverage"]["hygiene"]
        assert hy["malformed"] == 80 and hy["total"] == 80
        assert "https://products/p0" in hy["examples"][0]

    def test_minority_malformed_passes_through(self):
        from agents.graph import _discovery_yield_hygiene

        y = {"discovered_urls": 10, "stop_reason": "no_next_link", "coverage": {}}
        urls = [f"https://products/p{i}" for i in range(4)] + [
            f"https://myhouse.com.au/products/p{i}" for i in range(6)
        ]
        out = _discovery_yield_hygiene(y, urls, JOB_URL)
        assert out["discovered_urls"] == 10

    def test_no_url_list_gate_noops(self):
        from agents.graph import _discovery_yield_hygiene

        y = {"discovered_urls": 80, "stop_reason": "no_next_link"}
        assert _discovery_yield_hygiene(y, [], JOB_URL) is y

    def test_zero_yield_untouched(self):
        from agents.graph import _discovery_yield_hygiene

        y = {"discovered_urls": 0, "stop_reason": "empty_first_page"}
        assert _discovery_yield_hygiene(y, ["https://products/x"], JOB_URL) is y


class TestProbeWiring:
    """The probe must read the draft's fresh input_urls.json and run the
    hygiene gate over the yield it reports."""

    def _src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def test_once_function_applies_hygiene(self):
        src = self._src()
        once = re.search(
            r"^def _probe_phase1_discovery_once\(.*?(?=^def )", src, re.M | re.S
        ).group(0)
        assert "_discovery_yield_hygiene(" in once

    def test_once_function_reads_input_urls(self):
        src = self._src()
        once = re.search(
            r"^def _probe_phase1_discovery_once\(.*?(?=^def )", src, re.M | re.S
        ).group(0)
        assert "input_urls.json" in once

    def test_helpers_defined_before_use(self):
        src = self._src()
        assert src.index("def _discovered_url_issues(") < src.index(
            "def _probe_phase1_discovery_once("
        )
        assert src.index("def _discovery_yield_hygiene(") < src.index(
            "def _probe_phase1_discovery_once("
        )


class TestRunExecutionDiagnosis:
    def test_quality_gate_names_malformed_discovery(self):
        with open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")
        ) as fh:
            src = fh.read()
        # the wiring sits at the CALL SITE, immediately before the gate fires
        gate_at = src.index("Extraction quality gate")
        window = src[max(0, gate_at - 4000) : gate_at + 500]
        assert "_discovered_url_issues(" in window


class TestTemplateMakeAbsolute:
    """The template's own href resolver must not mint 'https://products/x'
    from a malformed '//products/x' path value (the myhouse listing-payload
    shape). Protocol-relative is honored only for plausible netlocs."""

    def _nav_src(self) -> str:
        with open(
            os.path.join(ROOT, "templates", "http_navigation_scraper.py")
        ) as fh:
            return fh.read()

    def _helper(self):
        src = self._nav_src()
        m = re.search(r"^def _make_absolute\(.*?(?=^def |\Z)", src, re.M | re.S)
        ns: dict = {"SITE_URL": "https://myhouse.com.au"}
        exec(compile(m.group(0), "<_make_absolute>", "exec"), ns)
        return ns["_make_absolute"]

    def test_malformed_protocol_relative_becomes_site_path(self):
        helper = self._helper()
        out = helper("//products/arcosteel-popcorn-maker-black")
        assert out == "https://myhouse.com.au/products/arcosteel-popcorn-maker-black"

    def test_true_protocol_relative_with_netloc_preserved(self):
        helper = self._helper()
        assert (
            helper("//cdn.shopify.com/assets/x.js")
            == "https://cdn.shopify.com/assets/x.js"
        )

    def test_root_relative_joined(self):
        helper = self._helper()
        assert helper("/products/x") == "https://myhouse.com.au/products/x"

    def test_absolute_passthrough(self):
        helper = self._helper()
        assert (
            helper("https://myhouse.com.au/products/y")
            == "https://myhouse.com.au/products/y"
        )

    def test_harvest_route_goes_through_make_absolute(self):
        harvest = re.search(
            r"^def _extract_item_links\(.*?(?=^def )", self._nav_src(), re.M | re.S
        )
        assert harvest and "_make_absolute(" in harvest.group(0)


class TestWriterPromptGotcha:
    def test_prompt_carries_protocol_relative_gotcha(self):
        with open(os.path.join(ROOT, ".opencode", "agents", "code-writer.md")) as fh:
            src = fh.read()
        assert "protocol-relative" in src
        assert "https://products/x" in src  # the concrete trap example


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
