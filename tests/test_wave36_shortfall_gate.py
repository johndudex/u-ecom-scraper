"""[wave-36 Fix 3] Scope-adjusted shortfall gate at execution routing.

docs/plans/wave36-field-mapping-plan.md §3 (amended per F16-F18). Job 658's
cousins: SUCCESS runs that extracted a sliver of what discovery found fell
through every zero-item arm into cleanup with no verdict. The gate extends
``_volume_gap`` semantics (route_after_testing.py) to execution routing:

- zero-item recycle arms keep precedence — shortfall fires ONLY when
  ``0 < extracted < 0.25 × expected`` (never stacked on a zero recycle);
- scope-NARROWED runs (firstn/filter/scope_value) never recycle — a
  scope-satisfied run is success-by-design (the 10-of-70 firstn case);
- discovery must have covered ≥2 pages (items_per_page denominator);
- remediation routes to the writer only on a code-fixable signature
  (high ``failed_products`` + ``soft_block_escalations`` recorded in the
  output metadata — Fix 2's ``json_no_items`` keeps that counter meaningful
  for JSON walls); otherwise honest SUCCESS + loud shortfall warning;
- keep-better: the prior artifact pair (``prior_output_file`` /
  ``prior_product_count``) wins when the remediation pass delivers less;
- ``shortfall_remediation_count`` is a DECLARED state key bound at 1 (round-2
  B3: undeclared keys are stripped → unbounded loop).
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

import pytest  # noqa: E402

from webapp.agents.graph import (  # noqa: E402
    _SHORTFALL_REMEDIATION_MAX,
    _execution_shortfall_reason,
)


def _state(**over):
    st = {
        "job_id": 1,
        "input_mode": "navigation",
        "scope": "",
        "scope_value": "",
        "discovery_coverage": {
            "ran_phase1": True,
            "found": 80,  # ≥2 pages of 36 → volume is judgeable
            "discovered_urls": ["u"] * 80,
            "items_per_page": 36,
        },
        "navigation_analysis": {"api_endpoint": {"items_per_page": 36}},
        "product_count": 3,
    }
    st.update(over)
    return st


class TestShortfallVerdict:
    def test_drastic_shortfall_fires(self):
        # 3 of 80 (< 25%) with ≥2 pages covered → verdict.
        reason = _execution_shortfall_reason(_state(), 3)
        assert reason and "3" in reason and "80" in reason

    def test_healthy_extraction_stays_silent(self):
        assert _execution_shortfall_reason(_state(), 40) is None

    def test_scope_narrowed_never_fires(self):
        # The 658 shape: firstn/10 over 80 discovered — success-by-design.
        st = _state(scope="firstn", scope_value="10", product_count=3)
        assert _execution_shortfall_reason(st, 3) is None
        st2 = _state(scope_value="10")
        assert _execution_shortfall_reason(st2, 3) is None
        st3 = _state(scope="filter")
        assert _execution_shortfall_reason(st3, 3) is None

    def test_single_page_coverage_stays_silent(self):
        st = _state(
            discovery_coverage={
                "ran_phase1": True, "found": 20,
                "discovered_urls": ["u"] * 20, "items_per_page": 36,
            },
            navigation_analysis={"api_endpoint": {"items_per_page": 36}},
        )
        assert _execution_shortfall_reason(st, 3) is None  # 20 < 2×36

    def test_no_page_denominator_stays_silent(self):
        st = _state(navigation_analysis={}, discovery_coverage={
            "ran_phase1": True, "found": 70,
            "discovered_urls": ["u"] * 70,
        })
        assert _execution_shortfall_reason(st, 3) is None

    def test_zero_items_belongs_to_zero_arms(self):
        assert _execution_shortfall_reason(_state(), 0) is None

    def test_no_phase1_stays_silent(self):
        st = _state(discovery_coverage={"ran_phase1": False, "found": 70,
                                        "items_per_page": 36})
        assert _execution_shortfall_reason(st, 3) is None

    def test_no_discovery_at_all_stays_silent(self):
        st = _state(discovery_coverage={}, navigation_analysis={})
        assert _execution_shortfall_reason(st, 3) is None

    def test_bound_is_one_and_declared(self):
        assert _SHORTFALL_REMEDIATION_MAX == 1
        from webapp.agents.state import ScrapeState

        for key in ("shortfall_remediation_count", "prior_output_file",
                    "prior_product_count"):
            assert key in ScrapeState.__annotations__, key


class TestCodeFixableSignature:
    def _out(self, tmp_path, failed, escalations):
        path = tmp_path / "out.json"
        path.write_text(json.dumps({
            "products": [{"title": "x"}] * 3,
            "metadata": {"failed_products": failed,
                         "soft_block_escalations": escalations},
        }))
        return str(path)

    def test_high_failures_with_escalations_is_code_fixable(self, tmp_path):
        from webapp.agents.graph import _execution_code_fixable

        assert _execution_code_fixable(
            self._out(tmp_path, failed=9, escalations=2), items=3
        )

    def test_no_escalations_is_not_code_fixable(self, tmp_path):
        from webapp.agents.graph import _execution_code_fixable

        assert not _execution_code_fixable(
            self._out(tmp_path, failed=9, escalations=0), items=3
        )

    def test_few_failures_is_not_code_fixable(self, tmp_path):
        from webapp.agents.graph import _execution_code_fixable

        assert not _execution_code_fixable(
            self._out(tmp_path, failed=1, escalations=2), items=3
        )

    def test_unreadable_output_is_not_code_fixable(self, tmp_path):
        from webapp.agents.graph import _execution_code_fixable

        assert not _execution_code_fixable(
            str(tmp_path / "missing.json"), items=3
        )


class TestRouteIntegration:
    """_route_after_execution precedence: keep-better, then zero arms, then
    shortfall — with the declared counter bound."""

    def _route(self, state, monkeypatch=None):
        from webapp.agents.graph import _route_after_execution

        return _route_after_execution(state)

    def test_zero_items_still_takes_zero_arm_first(self):
        # A FAIL-class zero must go to the strategy recycle, never the
        # shortfall arm (zero-arm precedence, round-2 M2).
        st = _state(product_count=0, execution_status="SUCCESS",
                    scraper_analysis={"strategy": "http_navigation"})
        st["discovery_coverage"]["stop_reason"] = "empty_first_page"
        cmd = self._route(st)
        assert cmd.goto == "scraper_analyzer"

    def test_scope_satisfied_run_goes_straight_to_cleanup(self):
        st = _state(scope="firstn", scope_value="10", product_count=3,
                    execution_status="SUCCESS")
        cmd = self._route(st)
        assert cmd.goto == "cleanup"
        assert cmd.update is None or "shortfall" not in json.dumps(
            cmd.update or {}
        )

    def test_shortfall_without_signature_honest_success_with_warning(self):
        st = _state(execution_status="SUCCESS")
        st["output_file"] = "/nonexistent/out.json"  # no metadata → no signature
        cmd = self._route(st)
        assert cmd.goto == "cleanup"
        cov = (cmd.update or {}).get("discovery_coverage") or {}
        assert "shortfall_warning" in cov

    def test_shortfall_with_signature_recycles_with_prior_stash(
        self, tmp_path
    ):
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "products": [{"title": "x"}] * 3,
            "metadata": {"failed_products": 12,
                         "soft_block_escalations": 3},
        }))
        st = _state(execution_status="SUCCESS",
                    output_file=str(out),
                    product_count=3)
        cmd = self._route(st)
        assert cmd.goto == "scraper_analyzer"
        upd = cmd.update or {}
        assert upd["shortfall_remediation_count"] == 1
        assert upd["prior_output_file"] == str(out)
        assert upd["prior_product_count"] == 3

    def test_second_shortfall_pass_is_bound(self):
        st = _state(execution_status="SUCCESS",
                    shortfall_remediation_count=1)
        st["output_file"] = "/nonexistent/out.json"
        cmd = self._route(st)
        assert cmd.goto == "cleanup"  # bound spent → no second recycle

    def test_keep_better_restores_prior_artifact(self, tmp_path):
        prior = tmp_path / "prior.json"
        prior.write_text(json.dumps({"products": [{"title": "p"}] * 3}))
        st = _state(execution_status="SUCCESS",
                    prior_output_file=str(prior),
                    prior_product_count=3,
                    product_count=1,
                    output_file=str(tmp_path / "worse.json"))
        cmd = self._route(st)
        assert cmd.goto == "cleanup"
        upd = cmd.update or {}
        assert upd["output_file"] == str(prior)
        assert upd["product_count"] == 3
        assert upd["execution_status"] == "SUCCESS"

    def test_worse_zero_after_remediation_also_restores(self, tmp_path):
        prior = tmp_path / "prior.json"
        prior.write_text(json.dumps({"products": [{"title": "p"}] * 3}))
        st = _state(execution_status="SUCCESS",
                    prior_output_file=str(prior),
                    prior_product_count=3,
                    product_count=0)
        st["discovery_coverage"]["stop_reason"] = "empty_first_page"
        cmd = self._route(st)
        assert cmd.goto == "cleanup"  # keep-better wins over a second recycle
        assert (cmd.update or {})["product_count"] == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
