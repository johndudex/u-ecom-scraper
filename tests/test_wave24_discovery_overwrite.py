"""[wave-24 W24-2] Discovery overwrite guards — evidence preservation.

Prod 394: Phase-1 discovery PROVEN functional (240 URLs), then a redundant
``--discover-only`` pass hit ``empty_render`` and the ZERO rode into the
verdict as a false HIGH. Two mechanisms (deep-agent overwrite map):

1. in-process: the template's own second discovery pass clobbers its result;
2. consumer-level: probe artifacts sail into ``_attach_discovery_coverage``
   (skip too narrow), the probe zeroes the checkpoint, and the tester's
   zero-yield arm overwrites the run's own stronger coverage block.

This module pins all the guards: the three consumer-belt hooks (1-3), the
persist-namespace hook (4 — zero-yield discovery artifacts land in the
``probe_output_*`` namespace so no ``output_*.json`` consumer reads them),
and the template in-process fixes (playwright's weaker-result guard,
http_navigation's job-77 checkpoint guard).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

import agents.graph as ag


def _zero_the_checkpoint(tmp_path):
    """Stand-in for a probe that discovered 0 and banked it."""
    ckpt = tmp_path / "discovered_urls_checkpoint.json"
    ckpt.write_text(json.dumps({"urls": [], "count": 0, "ts": 9.0}))


def _seed_output(tmp_path, slug, name, data):
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True, exist_ok=True)
    p = ws / name
    p.write_text(json.dumps(data))
    return p


class TestConsumerBeltSkipsProbeArtifacts:
    """Hook 1: _attach_discovery_coverage must never attach a probe
    artifact's zero-coverage to the run's verdict."""

    def _attach(self, tmp_path, monkeypatch, output_data, name="output_99.json"):
        monkeypatch.setattr("django.conf.settings.PROJECT_ROOT", str(tmp_path), raising=False)
        _seed_output(tmp_path, "crocs-com", name, output_data)
        return ag._attach_discovery_coverage({}, "crocs-com")

    def test_tagged_probe_artifact_skipped(self, tmp_path, monkeypatch):
        """metadata.phase=discovery → probe artifact (394's empty_render
        shape): its zero-coverage must not ride into the report."""
        report = self._attach(tmp_path, monkeypatch, {
            "metadata": {
                "phase": "discovery",
                "discovery_coverage": {
                    "found": 0, "discovered_urls": 0, "stop_reason": "empty_render",
                },
            },
            "products": [],
        })
        assert "discovery_coverage" not in report

    def test_untagged_navigate_error_probe_still_skipped(self, tmp_path, monkeypatch):
        """Pre-tagging outputs: the legacy navigate_error/0 shape guard stays."""
        report = self._attach(tmp_path, monkeypatch, {
            "metadata": {
                "discovery_coverage": {
                    "found": 0, "discovered_urls": 0, "stop_reason": "navigate_error",
                },
            },
            "products": [],
        })
        assert "discovery_coverage" not in report

    def test_real_run_coverage_still_attaches(self, tmp_path, monkeypatch):
        """The run's own output (untagged, real coverage) attaches as before —
        the guard is a skip for probes, not a muzzle for evidence."""
        report = self._attach(tmp_path, monkeypatch, {
            "metadata": {
                "discovery_coverage": {
                    "found": 240, "discovered_urls": 240, "stop_reason": "target_met",
                },
            },
            "products": [{"title": "x", "price": "$1"}],
        })
        assert report["discovery_coverage"]["found"] == 240


class TestZeroYieldArmPreservesStrongerEvidence:
    """Hook 3: the tester's zero-yield arm must not overwrite the run's own
    stronger discovery coverage with the probe's zero."""

    def test_strong_run_coverage_kept_live_probe_stashed(self):
        report = {"discovery_coverage": {"found": 240, "stop_reason": "target_met"}}
        probe_cov = {"found": 0, "stop_reason": "empty_render", "ran_phase1": True}
        live, note = ag._zero_yield_coverage_update(report, probe_cov)
        assert live is report["discovery_coverage"]
        assert live["found"] == 240
        assert "240" in note and "contradicts" in note

    def test_zero_or_missing_run_coverage_publishes_probe(self):
        probe_cov = {"found": 0, "stop_reason": "empty_first_page"}
        for report in ({}, {"discovery_coverage": {"found": 0, "stop_reason": "x"}}):
            live, note = ag._zero_yield_coverage_update(report, probe_cov)
            assert live is probe_cov
            assert note == ""

    def test_arm_publishes_live_block_and_stashes_probe(self):
        """Source contract: the arm calls the helper and stashes the probe
        block under probe_discovery_coverage when the run's evidence wins."""
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def _invoke_code_tester("):src.index("def _invoke_cleanup(")]
        assert "_zero_yield_coverage_update(report, _zcov)" in body
        assert 'report["probe_discovery_coverage"]' in body


class TestCheckpointGuard:
    """Hook 2: a probe's 0-URL checkpoint must never destroy the real run's
    banked discovery (job-77 guard shape, probe side)."""

    def test_identity_snapshot_tracks_checkpoint(self, tmp_path):
        ckpt = tmp_path / "discovered_urls_checkpoint.json"
        ckpt.write_text('{"urls": ["a"], "count": 1}')
        snap = ag._identity_snapshot(str(tmp_path))
        assert any(p.endswith("discovered_urls_checkpoint.json") for p in snap)

    def test_restore_zeroed_checkpoint(self, tmp_path):
        ckpt = tmp_path / "discovered_urls_checkpoint.json"
        good = {"urls": ["a", "b", "c"], "count": 3, "ts": 1.0}
        ckpt.write_text(json.dumps(good))
        # Probe zeroes it:
        ckpt.write_text(json.dumps({"urls": [], "count": 0, "ts": 2.0}))
        ag._restore_zeroed_checkpoint(str(tmp_path), good)
        assert json.loads(ckpt.read_text())["urls"] == ["a", "b", "c"]

    def test_restore_noop_when_current_has_urls(self, tmp_path):
        ckpt = tmp_path / "discovered_urls_checkpoint.json"
        good = {"urls": ["a"], "count": 1, "ts": 1.0}
        newer = {"urls": ["n1", "n2"], "count": 2, "ts": 2.0}
        ckpt.write_text(json.dumps(newer))
        ag._restore_zeroed_checkpoint(str(tmp_path), good)
        assert json.loads(ckpt.read_text())["count"] == 2

    def test_restore_noop_when_no_pre_checkpoint(self, tmp_path):
        ckpt = tmp_path / "discovered_urls_checkpoint.json"
        ckpt.write_text(json.dumps({"urls": [], "count": 0}))
        ag._restore_zeroed_checkpoint(str(tmp_path), None)  # must not raise
        assert json.loads(ckpt.read_text())["count"] == 0

    def test_probe_wrapper_restores_after_zeroing_probe(self, tmp_path, monkeypatch):
        """The guarded wrapper around _probe_phase1_discovery_once: whatever
        the probe does to the checkpoint, a banked >0-URL checkpoint survives."""
        monkeypatch.setattr(
            ag, "_probe_phase1_discovery_once",
            lambda *a, **k: (_zero_the_checkpoint(tmp_path) or (False, None, {"discovered_urls": 0})),
        )
        ws = tmp_path / "workspace" / "crocs-com"
        ws.mkdir(parents=True)
        ckpt = ws / "discovered_urls_checkpoint.json"
        ckpt.write_text(json.dumps({"urls": ["a", "b"], "count": 2, "ts": 1.0}))

        result = ag._probe_phase1_discovery_checked(
            "crocs-com", {"job_id": 0}, 0, root=str(tmp_path)
        )
        assert result[2]["discovered_urls"] == 0  # the probe's verdict stands
        assert json.loads(ckpt.read_text())["urls"] == ["a", "b"]  # evidence restored

    def test_template_checkpoint_writer_refuses_zero_overwrite(self):
        """Source contract: navigation_scraper.py's _write_checkpoint carries
        the job-77 guard — no 0-URL overwrite of a banked checkpoint."""
        tpl = os.path.join(ROOT, "templates", "navigation_scraper.py")
        with open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        fn = src[src.index("def _write_checkpoint("):src.index("def _load_checkpoint(")]
        assert "not urls" in fn or "if urls" in fn
        assert "return" in fn


def _discovery_content(urls: int) -> str:
    return json.dumps(
        {
            "products": [],
            "metadata": {
                "phase": "discovery",
                "discovery_coverage": {
                    "found": 0,
                    "discovered_urls": urls,
                    "stop_reason": "empty_render" if urls == 0 else "target_met",
                },
            },
        }
    )


class TestHook4ProbePersistNamespace:
    """Hook 4: run_scraper's browser_service persist path must not file a
    ZERO-yield --discover-only artifact under output_*.json — the namespace
    every downstream consumer globs (394: the probe's 0 became the verdict's
    HIGH evidence). The deterministic probe already uses probe_output_* for
    its own capture (wave-22 C1); this extends the same namespace to the
    agent-tool persist path."""

    def test_zero_yield_discover_only_artifact_renamed(self):
        from agents.tools.shell_tools import _probe_namespace_rename

        out = _probe_namespace_rename(
            ["--discover-only"], "output_1700_42.json", _discovery_content(0)
        )
        assert out == "probe_output_1700_42.json"

    def test_productive_discovery_artifact_keeps_output_name(self):
        from agents.tools.shell_tools import _probe_namespace_rename

        out = _probe_namespace_rename(
            ["--discover-only"], "output_1700_42.json", _discovery_content(240)
        )
        assert out == "output_1700_42.json"

    def test_extraction_artifact_untouched(self):
        from agents.tools.shell_tools import _probe_namespace_rename

        content = json.dumps(
            {"products": [{"title": "x"}], "metadata": {"phase": "extraction"}}
        )
        out = _probe_namespace_rename([], "output_1700_42.json", content)
        assert out == "output_1700_42.json"

    def test_no_discover_only_flag_untouched(self):
        from agents.tools.shell_tools import _probe_namespace_rename

        out = _probe_namespace_rename([], "output_1700_42.json", _discovery_content(0))
        assert out == "output_1700_42.json"

    def test_unparsable_content_untouched(self):
        from agents.tools.shell_tools import _probe_namespace_rename

        out = _probe_namespace_rename(
            ["--discover-only"], "output_1700_42.json", "<html>crash</html>"
        )
        assert out == "output_1700_42.json"

    def test_missing_coverage_block_untouched(self):
        """Without a measurable yield, don't rename — the consumer belt owns
        shape-based skipping; this hook only acts on a provable zero."""
        from agents.tools.shell_tools import _probe_namespace_rename

        content = json.dumps({"products": [], "metadata": {"phase": "discovery"}})
        out = _probe_namespace_rename(
            ["--discover-only"], "output_1700_42.json", content
        )
        assert out == "output_1700_42.json"

    def test_persist_site_calls_the_helper(self):
        """The rename must sit on the browser_service persist path, not just
        exist."""
        path = os.path.join(ROOT, "webapp", "agents", "tools", "shell_tools.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert "_probe_namespace_rename(" in src


class TestTemplateInProcessGuards:
    """The durable fix lives at the source: a template must never file a
    WEAKER discovery result than an earlier pass in the same process."""

    def test_playwright_discover_only_never_persists_weaker_result(self):
        tpl = os.path.join(ROOT, "templates", "playwright_scraper.py")
        with open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("if args.discover_only:"):src.index("if args.sample:")]
        # Guard reads the earlier pass's evidence...
        assert "LAST_DISCOVERY" in block or "product_urls" in block
        # ...and parks a weaker result in the probe namespace instead of OUTPUT_FILE
        assert "probe_output_" in block

    def test_http_navigation_checkpoint_writer_refuses_zero_overwrite(self):
        """Same job-77 hole navigation_scraper.py had: a probe that discovers
        0 must not zero the banked checkpoint a crash-retry would resume
        from."""
        tpl = os.path.join(ROOT, "templates", "http_navigation_scraper.py")
        with open(tpl, encoding="utf-8") as fh:
            src = fh.read()
        fn = src[src.index("def _write_checkpoint("):src.index("def _load_checkpoint(")]
        assert "not urls" in fn or "if urls" in fn
        assert "return" in fn
