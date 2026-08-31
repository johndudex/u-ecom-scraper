"""[job-85 supercheapauto] Nonzero-junk-yield blindness — one shared
predicate for "this listing cannot be crawled", wired into every gate that
used to key on ``discovered_urls == 0``.

What happened (proven RCA): job 85 was a ``list_page`` job whose job URL was
a PRODUCT DETAIL page. The navigator reached the real listing
(``/brands/hardkorr``, 46 cards) — but the job-310 contract gives
``state["url"]`` top priority, so execution crawled the PDP. Discovery on a
PDP yields exactly 1 junk link (itself / related products):
``discovered_urls: 1, found: 0, stop_reason: "short_page"``. Every zero-keyed
gate — RC1's ``_execution_zero_discovery``, ``_probe_retry_warranted``, the
probe's zero-yield verdict — equated ">= 1 link" with "discovery works" and
stayed silent. The run shipped 0 items as SUCCESS; the real listing sat in
``search_criteria`` unused.

Fixes pinned here (generic, no caps):
- ``src.listing_discovery.listing_yield_failure`` — one predicate: raw zero,
  ``empty_first_page``, zero usable yield (``found``) with an
  exhaustion-flavored stop reason, or a tiny raw yield (<=2 links). NEVER
  ``max_pages_hit`` (genuine catalog end) or hard errors (strategy ladder's
  domain). Missing data stays a no-op.
- RC1 consumes the predicate (a junk-zero now arms the listing fallback).
- ``_distinct_same_domain_listing`` / ``_probe_listing_candidates`` gain the
  URL-shaped ``search_criteria`` as a candidate behind the navigator's
  promotion (job 85's real listing lived ONLY there).
- ``_run_category_sources`` no longer hard-returns when
  ``navigation_findings.json`` is absent, and adds zero-rescue candidates
  when the primary extracted nothing.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

JOB_URL = "https://www.supercheapauto.com.au/p/hardkorr-hyperion-9-led-driving-lights/719931.html"
LISTING = "https://www.supercheapauto.com.au/brands/hardkorr"

# The exact execution metadata job 85 shipped (0 items as SUCCESS).
JOB85_COVERAGE = {
    "stop_reason": "short_page", "found": 0,
    "discovered_urls": 1, "max_pages_hit": False,
    "ran_phase1": True, "skipped_reason": None,
    "dimensions_total": 0,
}


# ─── the shared predicate ────────────────────────────────────────────────────


class TestListingYieldFailure:
    def test_job85_junk_yield_is_a_failure(self):
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure(dict(JOB85_COVERAGE)) is True

    def test_raw_zero_is_a_failure_any_non_hard_reason(self):
        from src.listing_discovery import listing_yield_failure

        for sr in ("short_page", "no_next_link", "no_new_items", "", "weird"):
            assert listing_yield_failure({"discovered_urls": 0, "stop_reason": sr}) is True, sr

    def test_empty_first_page_is_a_failure(self):
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure({"discovered_urls": 0, "stop_reason": "empty_first_page"}) is True

    def test_healthy_yield_is_not_a_failure(self):
        from src.listing_discovery import listing_yield_failure

        cov = {"discovered_urls": 107, "found": 50, "stop_reason": "max_pages_hit"}
        assert listing_yield_failure(cov) is False

    def test_real_yield_survives_exhaustion_flavored_stop(self):
        """found > 0 with short_page: a small-but-real catalog ran dry — not
        this gate's problem."""
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure(
            {"discovered_urls": 107, "found": 12, "stop_reason": "short_page"}
        ) is False

    def test_max_pages_hit_is_never_a_failure(self):
        """Genuine catalog end — even with a zero extracted count (that class
        belongs to the extraction-quality gate, not the listing choice)."""
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure(
            {"discovered_urls": 900, "found": 0, "stop_reason": "max_pages_hit"}
        ) is False

    def test_hard_errors_are_never_a_failure(self):
        """navigate_error/navigate_throttled are access problems — the
        strategy ladder owns them, the listing fallback must not fire."""
        from src.listing_discovery import listing_yield_failure

        for sr in ("navigate_error", "navigate_throttled"):
            assert listing_yield_failure({"discovered_urls": 0, "stop_reason": sr}) is False, sr

    def test_zero_found_with_exhaustion_is_a_failure(self):
        from src.listing_discovery import listing_yield_failure

        for sr in ("short_page", "no_next_link", "no_new_items"):
            assert listing_yield_failure(
                {"discovered_urls": 46, "found": 0, "stop_reason": sr}
            ) is True, sr

    def test_tiny_raw_yield_without_found_is_a_failure(self):
        """Probe path: no ``found`` reported — the raw count IS the yield."""
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure({"discovered_urls": 1, "stop_reason": "short_page"}) is True
        assert listing_yield_failure({"discovered_urls": 2, "stop_reason": "no_next_link"}) is True
        assert listing_yield_failure({"discovered_urls": 3, "stop_reason": "short_page"}) is False
        assert listing_yield_failure({"discovered_urls": 50, "stop_reason": "short_page"}) is False

    def test_nested_coverage_found_is_read(self):
        """Probe-yield shape: the coverage dict rides along nested."""
        from src.listing_discovery import listing_yield_failure

        y = {"discovered_urls": 1, "stop_reason": "short_page",
             "coverage": {"found": 0, "stop_reason": "short_page"}}
        assert listing_yield_failure(y) is True
        y["coverage"]["found"] = 9
        assert listing_yield_failure(y) is False

    def test_missing_data_stays_a_noop(self):
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure(None) is False
        assert listing_yield_failure("nope") is False
        assert listing_yield_failure({"stop_reason": "short_page"}) is True  # raw zero by absence


# ─── RC1 consumes the predicate ──────────────────────────────────────────────


class _RC1Base:
    def _result(self, tmp_path, cov):
        out = tmp_path / "output_j85.json"
        out.write_text(json.dumps({
            "products": [],
            "metadata": {"discovery_coverage": cov},
        }))
        return {"execution_status": "SUCCESS", "output_file": str(out),
                "product_count": 0}


class TestRC1ReadsYield(_RC1Base):
    def test_junk_yield_arms_the_fallback(self, tmp_path):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(
            self._result(tmp_path, dict(JOB85_COVERAGE))
        ) is True

    def test_genuine_catalog_end_still_does_not(self, tmp_path):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        cov = dict(JOB85_COVERAGE, discovered_urls=900, stop_reason="max_pages_hit")
        assert _execution_zero_discovery(self._result(tmp_path, cov)) is False

    def test_healthy_yield_still_does_not(self, tmp_path):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        cov = dict(JOB85_COVERAGE, discovered_urls=107, found=50)
        assert _execution_zero_discovery(self._result(tmp_path, cov)) is False


class TestCriteriaCandidate(_RC1Base):
    def _state(self, nav_listing=None, criteria=LISTING):
        nav = {"discovery": {"listing_url": nav_listing}} if nav_listing else {}
        return {"url": JOB_URL, "navigation_analysis": nav,
                "search_criteria": criteria}

    def test_criteria_used_when_navigator_promoted_nothing(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        assert _distinct_same_domain_listing(self._state(), JOB_URL) == LISTING

    def test_navigator_promotion_outranks_criteria(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        other = "https://www.supercheapauto.com.au/brands/other-brand"
        assert _distinct_same_domain_listing(
            self._state(nav_listing=other), JOB_URL
        ) == other

    def test_criteria_must_be_url_shaped(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        st = self._state(criteria="hardkorr lighting")
        assert _distinct_same_domain_listing(st, JOB_URL) == ""

    def test_criteria_cross_domain_rejected(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        st = self._state(criteria="https://other-shop.org/brands/hardkorr")
        assert _distinct_same_domain_listing(st, JOB_URL) == ""

    def test_criteria_identical_to_primary_means_no_retry(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        assert _distinct_same_domain_listing(self._state(), LISTING) == ""

    def test_no_criteria_no_nav_means_no_retry(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        assert _distinct_same_domain_listing(self._state(criteria=""), JOB_URL) == ""


class TestRC1EndToEndJob85(_RC1Base):
    def test_junk_zero_retries_once_on_the_user_listing(self, tmp_path):
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        state = {"url": JOB_URL, "navigation_analysis": {},
                 "search_criteria": LISTING}
        calls = []

        def redispatch(alt):
            calls.append(alt)
            r = self._result(tmp_path, {"discovered_urls": 46, "found": 46,
                                        "stop_reason": "max_pages_hit"})
            r["product_count"] = 46
            return r

        result = _maybe_retry_execution_listing(
            self._result(tmp_path, dict(JOB85_COVERAGE)),
            state, JOB_URL, redispatch,
        )
        assert calls == [LISTING]
        assert result["product_count"] == 46
        assert result["listing_fallback"]["adopted"] is True


# ─── probe gates consume the predicate ───────────────────────────────────────


class TestProbeGates(_RC1Base):
    def test_probe_yield_dead_junk(self):
        from webapp.agents.graph import _probe_yield_dead

        assert _probe_yield_dead({"discovered_urls": 1, "stop_reason": "short_page"}) is True
        assert _probe_yield_dead({"discovered_urls": 0, "stop_reason": ""}) is True

    def test_probe_yield_alive(self):
        from webapp.agents.graph import _probe_yield_dead

        assert _probe_yield_dead({"discovered_urls": 50, "stop_reason": "max_pages_hit"}) is False
        assert _probe_yield_dead({"discovered_urls": 0, "stop_reason": "navigate_error"}) is False

    def test_listing_candidates_fall_back_to_criteria(self):
        """[rag-bone job 72] the criteria listing is now the PRIMARY (it is
        the user's own listing assertion) — pre-wave-10 the PDP was primary
        and criteria only the retry. Job url still wins when criteria is not
        URL-shaped (job-310 shape, see TestJob72CriteriaFirstChain)."""
        from webapp.agents.graph import _probe_listing_candidates

        state = {"input_mode": "list_page", "url": JOB_URL,
                 "search_criteria": LISTING}
        primary, alt = _probe_listing_candidates(state)
        assert primary == LISTING
        assert alt == ""

    def test_retry_warranted_on_junk_yield(self):
        from webapp.agents.graph import _probe_retry_warranted

        # Junk on the primary (criteria) listing with a DISTINCT navigator
        # promotion → one retry is warranted.
        other = "https://www.supercheapauto.com.au/brands/other-brand"
        state = {"input_mode": "list_page", "url": JOB_URL,
                 "navigation_analysis": {"discovery": {"listing_url": LISTING}},
                 "search_criteria": other}
        junk = {"discovered_urls": 1, "stop_reason": "short_page",
                "coverage": {"found": 0}}
        assert _probe_retry_warranted(state, junk) is True

    def test_job85_exact_shape_converges_without_a_retry(self):
        """The job-85 state (criteria = the real listing) now probes the REAL
        listing as primary — the retry path is simply never needed. Protection
        was not lost; the probe starts one step further ahead."""
        from webapp.agents.graph import _probe_listing_candidates, _probe_retry_warranted

        state = {"input_mode": "list_page", "url": JOB_URL,
                 "navigation_analysis": {}, "search_criteria": LISTING}
        junk = {"discovered_urls": 1, "stop_reason": "short_page",
                "coverage": {"found": 0}}
        primary, alt = _probe_listing_candidates(state)
        assert primary == LISTING and alt == ""
        assert _probe_retry_warranted(state, junk) is False

    def test_retry_not_warranted_on_healthy_yield(self):
        from webapp.agents.graph import _probe_retry_warranted

        state = {"input_mode": "list_page", "url": JOB_URL,
                 "navigation_analysis": {"discovery": {"listing_url": LISTING}}}
        healthy = {"discovered_urls": 46, "stop_reason": "max_pages_hit"}
        assert _probe_retry_warranted(state, healthy) is False

    def test_zero_yield_verdict_uses_the_predicate(self):
        """The tester's zero-yield verdict must key on the shared predicate —
        a bare ``discovered_urls == 0`` comparison would bless a PDP's junk
        link (the exact job-85 miss)."""
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        assert "probe_yield[\"discovered_urls\"] == 0" not in src
        assert "_probe_yield_dead(probe_yield)" in src


# ─── multisource: no more blind hard-return; zero-rescue candidates ──────────


class TestMultisourceWiring:
    def test_navigation_findings_absence_is_not_a_hard_return(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "nodes", "run_execution.py"
        )).read()
        assert "if not _os.path.isfile(nf_path):\n            return primary_result" not in src

    def test_zero_rescue_candidates_are_wired(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "nodes", "run_execution.py"
        )).read()
        assert "zero-rescue" in src
        assert "_url_shaped_criteria(state)" in src
        # [rag-bone job 72] The rescue arms on UNDER-delivery (< the scope
        # target), not just an empty primary — firstn/10 delivered 1 and
        # every zero-keyed gate read it as success. Candidates still skip
        # the listing the primary run already used (no pointless top-up).
        assert "if len(primary_products) < _scope_target(state):" in src
        assert "if not primary_products:" not in src

    def test_url_shaped_criteria_predicate(self):
        from webapp.agents.nodes.run_execution import _url_shaped_criteria

        assert _url_shaped_criteria({"search_criteria": LISTING}) == LISTING
        assert _url_shaped_criteria({"search_criteria": "hardkorr lights"}) == ""
        assert _url_shaped_criteria({}) == ""
        assert _url_shaped_criteria({"search_criteria": "  "}) == ""


# ─── [rag-bone job 72] criteria-first listing chain + scope-aware rescue ────


class TestJob72CriteriaFirstChain:
    """Job 72 (user-reported as madewell; the record is rag & bone):
    ``list_page`` job whose ``url`` is a sample PDP and whose real listing
    lived in ``search_criteria`` (…/womens/?gc=IN). The job-310 contract made
    the PDP the listing — the tester proved 25 URLs through the criteria
    listing while execution discovered 2 / extracted 1 and blessed it
    COMPLETED under scope=firstn/10."""

    PDP = "https://www.rag-bone.com/p/josie-relaxed-flare-jean-RJ7D26F1RFL.html"
    CRITERIA = "https://www.rag-bone.com/womens/?gc=IN"

    def _job72_state(self, **over):
        st = {
            "url": self.PDP,
            "search_criteria": self.CRITERIA,
            "input_mode": "list_page",
            "navigation_analysis": {"discovery": {"listing_url": self.CRITERIA}},
            "scope": "firstn",
            "scope_value": "10",
        }
        st.update(over)
        return st

    # ── the scope target ──

    def test_scope_target_firstn(self):
        from webapp.agents.nodes.run_execution import _scope_target

        assert _scope_target(self._job72_state()) == 10

    def test_scope_target_defaults_are_conservative(self):
        from webapp.agents.nodes.run_execution import _scope_target

        assert _scope_target({}) == 1
        assert _scope_target({"scope": "full"}) == 1
        assert _scope_target({"scope": "firstn", "scope_value": "abc"}) == 1
        assert _scope_target({"scope": "firstn", "scope_value": "0"}) == 1
        assert _scope_target({"scope": "firstn", "scope_value": ""}) == 1

    def test_one_item_under_firstn10_arms_rescue_zero_did_not(self):
        """The exact blind spot: 1 delivered vs 10 asked — the widened gate
        fires where the old empty-primary check stayed silent."""
        assert 1 < _target_of(self._job72_state())

    # ── probe candidates mirror the execution chain ──

    def test_probe_primary_is_the_criteria_listing(self):
        from webapp.agents.graph import _probe_listing_candidates

        primary, alt = _probe_listing_candidates(self._job72_state())
        assert primary == self.CRITERIA

    def test_probe_retry_uses_promotion_when_distinct(self):
        from webapp.agents.graph import _probe_listing_candidates

        other = "https://www.rag-bone.com/womens/denim"
        primary, alt = _probe_listing_candidates(
            self._job72_state(navigation_analysis={"discovery": {"listing_url": other}})
        )
        assert primary == self.CRITERIA
        assert alt == other

    def test_probe_no_pointless_retry_when_promotion_equals_criteria(self):
        from webapp.agents.graph import _probe_listing_candidates

        primary, alt = _probe_listing_candidates(self._job72_state())
        assert alt == "", "criteria == promotion → a retry would re-run the same URL"

    def test_job310_shape_still_selects_the_job_url(self):
        """Regression lock: pillowtalk's contract — no URL-shaped criteria →
        the job URL remains the list_page listing."""
        from webapp.agents.graph import _probe_listing_candidates

        st = {
            "url": LISTING,
            "search_criteria": "",
            "input_mode": "list_page",
            "navigation_analysis": {},
        }
        primary, alt = _probe_listing_candidates(st)
        assert primary == LISTING
        assert alt == ""

    def test_navigation_mode_primary_unchanged(self):
        from webapp.agents.graph import _probe_listing_candidates

        st = self._job72_state(input_mode="navigation")
        primary, alt = _probe_listing_candidates(st)
        assert primary == ""
        assert alt == self.CRITERIA

    # ── execution chain wiring (source pins — the chain is inline) ──

    def test_execution_chains_put_criteria_first(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "nodes", "run_execution.py"
        )).read()
        assert (
            "_candidates = [\n                _url_shaped_criteria(state),\n                _job_listing,"
        ) in src
        assert "_env_candidate = _url_shaped_criteria(state) or _job_listing or \"\"" in src

    def test_tester_env_knows_the_criteria_listing_too(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "tools", "shell_tools.py"
        )).read()
        assert "if not _listing_ts:" in src
        assert '_sc_ts.startswith(("http://", "https://"))' in src

    def test_execution_primary_is_criteria_end_to_end(self):
        """Decision mirror of the env chain's precedence with the real state:
        criteria → job URL → navigator promotion → traversal working URL."""
        st = self._job72_state()
        nav = st.get("navigation_analysis") or {}
        disc = (nav.get("discovery") if isinstance(nav, dict) else None) or {}
        job_listing = ""
        if st.get("input_mode") == "list_page":
            jl = str(st.get("url") or "").strip()
            if jl.startswith(("http://", "https://")):
                job_listing = jl
        criteria = ""
        sc = str(st.get("search_criteria") or "").strip()
        if sc.startswith(("http://", "https://")):
            criteria = sc
        chosen = criteria or job_listing or (disc.get("listing_url") or "")
        assert chosen == self.CRITERIA


def _target_of(state):
    from webapp.agents.nodes.run_execution import _scope_target

    return _scope_target(state)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
