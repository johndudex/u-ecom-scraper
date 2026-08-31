"""[job-77 adoreme] Execution must not run a proven draft into a guaranteed
zero, and a zero-yield run must not leave destruction behind it.

Job 77 (proven RCA): the draft PASSED testing 0.95 — discovery on the
navigator-verified ``/bras`` yields 107 URLs in 10 s — but execution
unconditionally injected the list_page JOB URL (the PDP
``/kaia-black-2-1``) as the discovery listing, discovered 0 URLs, escalated
twice on the soft-block ladder, and finalized FAILED. The navigator's
promotion (and even ``search_criteria``) carried the real listing the whole
time. Secondary damage: the 0-URL run overwrote the site's canonical
``input_urls.json`` with ``{"urls": []}``, and the 0-item SUCCESS promoted
the dead draft to the production ``scraper.py``.

Fixes pinned here (generic, bounded, evidence-driven — mirrors F6):
- RC1: ``_maybe_retry_execution_listing`` — on a CLEAN zero (0 discovered
  URLs / ``empty_first_page``, not a crash) with a DISTINCT same-domain
  navigator listing, re-dispatch ONCE with the promotion and adopt its
  result when it yields items. Both listings dead → the honest zero stands.
- RC4: the zero-yield probe gate's ``_find_newest_output`` call must carry
  ``mtime_floor=_probe_started - 5`` — without it the F16 count-ranking
  picks the tester's older NON-EMPTY output over the probe's fresh 0-item
  one and the gate is structurally blind in exactly its target case.
- promotion gate: SUCCESS with an explicit 0-item count must NOT overwrite
  the production ``scraper.py``.
- seed guard: ``save_urls_to_file`` refuses to overwrite an existing seed
  file with an empty URL list.
- finalize diagnosis: the no-execution catch-all distinguishes a PASS-
  validated draft that never executed (job-74 thenile cycle-1 class) from
  an actually-exhausted cascade (job-73 class).
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest
from django.test.utils import override_settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

JOB_URL = "https://www.adoreme.com/kaia-black-2-1"
BRAS = "https://www.adoreme.com/bras"


def _state(nav_listing=BRAS, url=JOB_URL):
    nav = {"discovery": {"listing_url": nav_listing}} if nav_listing else {}
    return {"url": url, "navigation_analysis": nav}


def _zero_result(tmp_path, stop_reason="empty_first_page", discovered=0):
    out = tmp_path / "output_zero.json"
    out.write_text(json.dumps({
        "products": [],
        "metadata": {"discovery_coverage": {
            "discovered_urls": discovered, "stop_reason": stop_reason,
        }},
    }))
    return {"execution_status": "SUCCESS", "output_file": str(out),
            "product_count": 0}


# ─── RC1: candidate discipline ───────────────────────────────────────────────


class TestDistinctSameDomainListing:
    def test_same_domain_promotion_is_returned(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        assert _distinct_same_domain_listing(_state(), JOB_URL) == BRAS

    def test_primary_already_the_promotion_means_no_retry(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        assert _distinct_same_domain_listing(_state(), BRAS) == ""

    def test_cross_domain_promotion_rejected(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        st = _state(nav_listing="https://other-shop.org/bras")
        assert _distinct_same_domain_listing(st, JOB_URL) == ""

    def test_missing_navigation_means_no_retry(self):
        from webapp.agents.nodes.run_execution import _distinct_same_domain_listing

        assert _distinct_same_domain_listing(_state(nav_listing=""), JOB_URL) == ""
        assert _distinct_same_domain_listing({}, JOB_URL) == ""


# ─── RC1: clean-zero detection (crashes never retry) ─────────────────────────


class TestExecutionZeroDiscovery:
    def test_zero_discovered_urls_is_clean_zero(self, tmp_path):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(_zero_result(tmp_path)) is True

    def test_empty_first_page_is_clean_zero(self, tmp_path):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(
            _zero_result(tmp_path, stop_reason="empty_first_page")
        ) is True

    def test_nonzero_discovery_never_retries(self, tmp_path):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(
            _zero_result(tmp_path, discovered=107, stop_reason="short_page")
        ) is False

    def test_crashed_run_never_retries(self):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(
            {"execution_status": "FAILED", "error_message": "boom"}
        ) is False

    def test_missing_output_file_never_retries(self):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(
            {"execution_status": "SUCCESS", "output_file": "/nope/missing.json"}
        ) is False


# ─── RC1: the bounded wrapper ────────────────────────────────────────────────


class TestMaybeRetryExecutionListing:
    def test_zero_primary_retries_once_and_adopts_fallback(self, tmp_path):
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        calls = []

        def redispatch(alt):
            calls.append(alt)
            r = _zero_result(tmp_path)
            r["product_count"] = 40
            return r

        result = _maybe_retry_execution_listing(
            _zero_result(tmp_path), _state(), JOB_URL, redispatch
        )
        assert calls == [BRAS]
        assert result["product_count"] == 40
        assert result["listing_fallback"]["adopted"] is True
        assert result["listing_fallback"]["fallback_listing"] == BRAS

    def test_nonzero_primary_never_retries(self, tmp_path):
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        calls = []
        r = _zero_result(tmp_path, discovered=107, stop_reason="short_page")
        r["product_count"] = 5
        result = _maybe_retry_execution_listing(
            r, _state(), JOB_URL, lambda alt: calls.append(alt)
        )
        assert calls == []
        assert result["product_count"] == 5

    def test_failed_primary_never_retries(self, tmp_path):
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        calls = []
        result = _maybe_retry_execution_listing(
            {"execution_status": "FAILED", "error_message": "boom"},
            _state(), JOB_URL, lambda alt: calls.append(alt),
        )
        assert calls == []
        assert result["execution_status"] == "FAILED"

    def test_both_listings_dead_keeps_the_honest_zero(self, tmp_path):
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        calls = []
        primary = _zero_result(tmp_path)
        result = _maybe_retry_execution_listing(
            primary, _state(), JOB_URL,
            lambda alt: (calls.append(alt), _zero_result(tmp_path))[1],
        )
        assert calls == [BRAS]
        assert result is primary

    def test_no_distinct_alt_means_single_attempt(self, tmp_path):
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        calls = []
        _maybe_retry_execution_listing(
            _zero_result(tmp_path), _state(nav_listing=""), JOB_URL,
            lambda alt: calls.append(alt),
        )
        assert calls == []


class TestArgsWithListingUrl:
    def test_swaps_existing_flag_value(self):
        from webapp.agents.nodes.run_execution import _args_with_listing_url

        out = _args_with_listing_url(
            ["--listing-url", JOB_URL, "--fresh-discovery"], BRAS
        )
        assert out == ["--listing-url", BRAS, "--fresh-discovery"]

    def test_appends_when_flag_absent(self):
        from webapp.agents.nodes.run_execution import _args_with_listing_url

        assert _args_with_listing_url(["--fresh-discovery"], BRAS) == [
            "--fresh-discovery", "--listing-url", BRAS,
        ]


# ─── RC4: the probe gate's freshness floor is load-bearing ───────────────────


class TestProbeGateFloor:
    def test_probe_output_read_carries_mtime_floor(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        # Close on the call's OWN closing paren (start-of-line paren), not the
        # first ')' — the arg list nests os.path.join(...) calls.
        m = re.search(
            r"_probe_out = _find_newest_output\((.*?)\n\s*\)", src, re.S
        )
        assert m, "probe output read not found"
        assert "mtime_floor=_probe_started - 5" in m.group(1), (
            "the zero-yield gate MUST floor the output read to this probe's "
            "start — without it the F16 count-ranking returns the tester's "
            "older non-empty output, the freshness check then discards it, "
            "and the gate never fires (job-77 RC4)"
        )


# ─── promotion gate: a 0-item SUCCESS must not become production ─────────────


@pytest.fixture()
def fm(monkeypatch):
    import src.artifacts as artifacts

    written = {}
    monkeypatch.setattr(
        artifacts, "write", lambda key, data: written.__setitem__(key, data)
    )
    monkeypatch.setattr(artifacts, "read", lambda key: b"# draft\n")
    monkeypatch.setattr(artifacts, "exists", lambda key: False)
    return written


class TestPromotionGate:
    def _promote(self, product_count):
        from webapp.agents.graph import _promote_scraper

        return _promote_scraper(
            "acme-com", 77, "SUCCESS", None, product_count=product_count
        )

    def test_zero_item_success_does_not_promote(self, tmp_path, fm):
        ws = tmp_path / "workspace" / "acme-com"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("# draft\n")
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            self._promote(0)
        assert not any(k.endswith("/scraper.py") for k in fm), (
            "a 0-item SUCCESS must not overwrite the production scraper.py "
            "(job-77: the dead adoreme draft became the site's production "
            "scraper)"
        )

    def test_nonzero_success_promotes(self, tmp_path, fm):
        ws = tmp_path / "workspace" / "acme-com"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("# draft\n")
        with override_settings(PROJECT_ROOT=str(tmp_path)):
            self._promote(5)
        assert any(k.endswith("/scraper.py") for k in fm)

    def test_unknown_count_keeps_legacy_promotion(self, tmp_path, fm):
        ws = tmp_path / "workspace" / "acme-com"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("# draft\n")
        with override_settings(PROJECT_ROOT=str(tmp_path)):
            self._promote(None)
        assert any(k.endswith("/scraper.py") for k in fm)


# ─── seed guard: 0-URL discovery must not destroy input_urls.json ────────────


class TestSeedFileGuard:
    def _load_save(self, tmp_path):
        """Extract save_urls_to_file from the requests template and exec it."""
        src = open(os.path.join(ROOT, "templates", "requests_scraper.py")).read()
        m = re.search(r"^def save_urls_to_file\(.*?(?=^def |\Z)", src, re.M | re.S)
        assert m, "save_urls_to_file not found"
        import logging

        ns = {
            "json": json, "os": os, "logging": logging,
            "logger": logging.getLogger("t.j77"),
        }
        exec(m.group(0), ns)
        return ns["save_urls_to_file"]

    def test_refuses_empty_overwrite_of_existing_seed(self, tmp_path):
        save = self._load_save(tmp_path)
        seed = tmp_path / "input_urls.json"
        seed.write_text(json.dumps({"urls": ["https://x.com/p/1"]}))
        save(str(seed), [])
        assert json.loads(seed.read_text())["urls"] == ["https://x.com/p/1"]

    def test_nonempty_refresh_still_writes(self, tmp_path):
        save = self._load_save(tmp_path)
        seed = tmp_path / "input_urls.json"
        seed.write_text(json.dumps({"urls": ["https://x.com/p/1"]}))
        save(str(seed), ["https://x.com/p/2", "https://x.com/p/3"])
        assert json.loads(seed.read_text())["urls"] == [
            "https://x.com/p/2", "https://x.com/p/3",
        ]

    def test_empty_write_to_fresh_path_still_writes(self, tmp_path):
        save = self._load_save(tmp_path)
        seed = tmp_path / "fresh.json"
        save(str(seed), [])
        assert json.loads(seed.read_text())["urls"] == []

    def test_guard_present_in_all_seed_writer_templates(self):
        for name in ("shopify_scraper.py", "playwright_scraper.py",
                     "api_scraper.py", "undetected_chromedriver_scraper.py"):
            src = open(os.path.join(ROOT, "templates", name)).read()
            assert "Refusing to overwrite" in src, (
                f"{name} save_urls_to_file lacks the empty-overwrite guard"
            )


# ─── finalize diagnosis: PASS-but-never-executed is not "cascade exhausted" ──


class TestDiagnoseNoExecution:
    def _call(self, tmp_path, monkeypatch, archive_report=None, slug="acme-com"):
        import src.artifacts as artifacts

        monkeypatch.setattr(
            artifacts, "scrapers_key",
            lambda s, *p: "scrapers/" + s + "/" + "/".join(p),
        )
        reports = {}
        if archive_report is not None:
            reports["scrapers/acme-com/analysis/test_report-74.json"] = archive_report
        monkeypatch.setattr(artifacts, "exists", lambda k: k in reports)
        monkeypatch.setattr(artifacts, "read_json", lambda k: reports[k])
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        # App-label import (matches the other root tests) — importing
        # webapp.scraper.* directly re-registers the models and raises
        # ConflictingModels.
        from scraper.tasks import _diagnose_no_execution

        return _diagnose_no_execution(slug, 74)

    def test_pass_report_reports_the_resume_gap(self, tmp_path, monkeypatch):
        msg = self._call(
            tmp_path, monkeypatch,
            {"overall_assessment": "PASS", "confidence_score": 0.85},
        )
        assert "PASS" in msg and "never ran" in msg
        assert "cascade exhausted" not in msg

    def test_fail_report_keeps_the_cascade_message(self, tmp_path, monkeypatch):
        msg = self._call(
            tmp_path, monkeypatch,
            {"overall_assessment": "FAIL", "confidence_score": 0.3},
        )
        assert "testing cascade exhausted" in msg

    def test_no_report_keeps_the_cascade_message(self, tmp_path, monkeypatch):
        msg = self._call(tmp_path, monkeypatch, archive_report=None)
        assert "testing cascade exhausted" in msg


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
