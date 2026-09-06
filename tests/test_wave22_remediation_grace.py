"""[wave-22 B3] A NEVER-attempted remediation earns one grace fix cycle —
structural identity, not prose.

Prod shape (359/360): the tester isolated the defect (remediation.target with
a concrete field) but the retry ladder was already exhausted, so the cascade
terminated with the fix sitting in the report, never attempted. The naive fix
— "if the report carries a remediation, keep going" — re-opens the writer
ballooning loop: the writer regenerates against the SAME diagnosis forever.

Contract:
- the remediation's identity is STRUCTURAL: target + field + sorted set of
  issue types + exception class. NEVER the free-text ``fix`` — writers
  paraphrase the same defect a dozen ways and a text hash would call every
  cycle "new";
- grace fires only when this exact fingerprint was never attempted
  (``remediation_fps_seen`` — appended by the writer node on entry), the
  per-job allowance is unspent (``remediation_grace_used``, consumed by the
  writer when it enters on the grace signature), and the ladder is NOT on
  FINAL_RETRY_SENTINEL (a human-approved final retry has its own contract);
- the router may only route — bookkeeping lives in the writer node;
- precedence: a never-attempted remediation outranks B2's forced re-test.
"""
from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

rat = importlib.import_module("agents.nodes.route_after_testing")  # noqa: E402


def _report(fix="parse JSON-LD before returning products", **over):
    rep = {
        "overall_assessment": "FAIL",
        "confidence_score": 0.7,
        "items_extracted": 0,
        "issues": [{"issue_type": "MISSING", "severity": "high"}],
        "remediation": {
            "target": "scraper",
            "field": "products",
            "fix": fix,
        },
        "crash_error": "NameError: name 'jsonld' is not defined",
    }
    rep.update(over)
    return rep


def _state(ws_dir="/app", **over):
    base = {
        "job_id": 0,
        "site_slug": "example-com",
        "test_report": _report(),
        "test_retry_count": rat.MAX_TEST_RETRIES,
        "skip_approvals": True,
        "tester_wall_clock_timeouts": 0,
        "forced_retest_count": 0,
        "remediation_fps_seen": [],
        "remediation_grace_used": 0,
    }
    base.update(over)
    return base


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    slug_dir = tmp_path / "workspace" / "example-com"
    slug_dir.mkdir(parents=True)
    return slug_dir


class TestRemediationFingerprint:
    def test_paraphrased_fix_text_is_the_same_remediation(self):
        a = rat._remediation_fingerprint(
            _report(fix="parse JSON-LD before returning products")
        )
        b = rat._remediation_fingerprint(
            _report(fix="You should really parse the JSON-LD block first!!")
        )
        assert a and a == b, (
            "prose hashing would call every writer paraphrase a NEW "
            "remediation and re-open the ballooning loop"
        )

    def test_field_change_is_a_new_remediation(self):
        a = rat._remediation_fingerprint(_report())
        b = rat._remediation_fingerprint(_report(remediation={
            "target": "scraper", "field": "price", "fix": "x",
        }))
        assert a != b

    def test_issue_type_set_change_is_a_new_remediation(self):
        a = rat._remediation_fingerprint(_report())
        b = rat._remediation_fingerprint(_report(issues=[
            {"issue_type": "WRONG_VALUE", "severity": "high"},
        ]))
        assert a != b

    def test_exception_class_change_is_a_new_remediation(self):
        a = rat._remediation_fingerprint(_report())
        b = rat._remediation_fingerprint(
            _report(crash_error="KeyError: 'price'")
        )
        assert a != b

    def test_missing_or_bare_remediation_has_no_fingerprint(self):
        assert rat._remediation_fingerprint(None) == ""
        assert rat._remediation_fingerprint({}) == ""
        assert rat._remediation_fingerprint(
            _report(remediation={}, issues=[], crash_error="")
        ) == ""


class TestGraceFunnel:
    def test_unseen_remediation_earns_one_grace_cycle(self):
        assert (
            rat._terminal_after_grace_check(_state(), "cleanup") == "code_writer"
        )

    def test_seen_remediation_goes_terminal(self):
        fp = rat._remediation_fingerprint(_state()["test_report"])
        state = _state(remediation_fps_seen=[fp])
        assert rat._terminal_after_grace_check(state, "cleanup") == "cleanup"

    def test_cap_one_no_second_grace(self):
        state = _state(remediation_grace_used=1)
        assert rat._terminal_after_grace_check(state, "cleanup") == "cleanup"

    def test_never_on_final_retry_sentinel(self):
        state = _state(test_retry_count=rat.FINAL_RETRY_SENTINEL)
        assert rat._terminal_after_grace_check(state, "cleanup") == "cleanup"

    def test_non_terminal_dest_untouched(self):
        assert (
            rat._terminal_after_grace_check(_state(), "field_confirmation")
            == "field_confirmation"
        )


class TestRouterIntegration:
    def test_exhausted_terminal_with_new_remediation_routes_to_writer(self, ws):
        assert rat.route_after_testing(_state()) == "code_writer"

    def test_exhausted_terminal_after_grace_is_terminal(self, ws):
        fp = rat._remediation_fingerprint(_state()["test_report"])
        state = _state(remediation_fps_seen=[fp], remediation_grace_used=1)
        assert rat.route_after_testing(state) == "cleanup"

    def test_grace_outranks_forced_retest(self, ws):
        # B2 mismatch present AND new remediation present → the writer gets
        # the cycle (fixing the defect re-tests the draft anyway).
        (ws / "scraper_draft.py").write_text("# newer than last judged\n")
        import hashlib

        state = _state(
            last_tested_draft_fp=hashlib.sha1(b"old draft").hexdigest(),
        )
        assert rat.route_after_testing(state) == "code_writer"


class TestWriterBookkeeping:
    def test_writer_appends_seen_fps_and_consumes_the_allowance(self):
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            src = fh.read()
        i = src.index("def _invoke_code_writer")
        j = src.index("\ndef ", i + 10)
        body = src[i:j]
        assert "remediation_fps_seen" in body, (
            "the writer never records attempted remediations — the router's "
            "newness check would see every repeat as new and grace-cycle "
            "forever"
        )
        assert "remediation_grace_used" in body, (
            "the writer never consumes the grace allowance — the hard "
            "per-job cap could never engage"
        )


class TestStateDeclarations:
    def test_counters_declared_in_scrape_state(self):
        with open(os.path.join(ROOT, "webapp", "agents", "state.py")) as fh:
            src = fh.read()
        for field in (
            "forced_retest_count",
            "remediation_fps_seen",
            "remediation_grace_used",
        ):
            assert field in src, f"ScrapeState is missing {field}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
