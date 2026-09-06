"""[wave-22 C1] The zero-yield probe must bind to ITS OWN output by file
identity — mtime floors guess, and the guesses condemned healthy listings.

Prod shape (the zero-yield FAIL class): the probe wrapper runs up to 3
subprocess attempts plus concurrent tester writes in the same workspace, and
"which output file did THIS probe write?" was answered by newest-mtime-within-
a-window — a window the tester's own 5-item sample or a sibling attempt's file
can win. One stolen identity = a healthy listing condemned as dead (or a dead
one blessed). And the browser-branch probe never persisted its output at all:
scraper_runner returns ``output_content`` ("caller persists it") and the caller
didn't — every browser draft probed inconclusive.

Contract:
- pre-probe snapshot (path → (mtime_ns, size)); the probe's file is one that
  is NEW or CHANGED since the snapshot; >1 such output ⇒ inconclusive, never
  a guess;
- ``_read_probe_discovered_urls`` gets the same binding (URL hygiene must not
  read the tester's old seed list);
- the browser branch persists ``output_content`` to a probe_-PREFIXED file —
  an ``output_*.json`` name would be re-selected by run_execution's
  ``_find_newest_output`` and leak a probe artifact into a real run's
  verdict;
- blank coverage ⇒ an INCONCLUSIVE stamp (``inconclusive: True``, no count),
  never ``discovered_urls=0`` — blank evidence cannot arm the zero-yield
  FAIL;
- ``listing_yield_failure``: empty coverage and bare ``"skipped"`` are NOT
  dead (unknowable ≠ dead); wave-20 T2's ``phase1_skipped`` code-bug verdict
  STAYS dead.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

graph = importlib.import_module("agents.graph")  # noqa: E402
rex = importlib.import_module("agents.nodes.run_execution")  # noqa: E402


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class TestIdentitySnapshot:
    def test_snapshot_captures_output_and_seed_files(self, tmp_path):
        _write(str(tmp_path / "output_20260905.json"), "{}")
        _write(str(tmp_path / "input_urls.json"), "[]")
        snap = graph._identity_snapshot(str(tmp_path))
        names = {os.path.basename(p) for p in snap}
        assert names == {"output_20260905.json", "input_urls.json"}
        mtime_ns, size = snap[os.path.abspath(str(tmp_path / "input_urls.json"))]
        assert size == 2 and mtime_ns > 0

    def test_new_and_changed_files_are_owned(self, tmp_path):
        _write(str(tmp_path / "output_old.json"), "old")
        _write(str(tmp_path / "input_urls.json"), "[]")
        snap = graph._identity_snapshot(str(tmp_path))
        time.sleep(0.01)
        # sibling attempt's brand-new output + same-size-touch rewrite of the seed
        _write(str(tmp_path / "output_new.json"), "new")
        _write(str(tmp_path / "probe_output_1.json"), "{}")
        _write(str(tmp_path / "input_urls.json"), '["https://x/p1"]')
        owned = graph._probe_owned_files(snap, str(tmp_path))
        base = {os.path.basename(p) for p in owned}
        assert base == {"output_new.json", "probe_output_1.json", "input_urls.json"}

    def test_untouched_files_are_not_owned(self, tmp_path):
        _write(str(tmp_path / "output_old.json"), "old")
        snap = graph._identity_snapshot(str(tmp_path))
        assert graph._probe_owned_files(snap, str(tmp_path)) == []


class TestProbeOutputGlobSafety:
    def test_run_execution_never_selects_probe_prefixed_files(self, tmp_path):
        _write(str(tmp_path / "probe_output_1757000000.json"), "{}")
        assert rex._find_newest_output(str(tmp_path)) in ("", None), (
            "a probe_-prefixed artifact must be invisible to run_execution's "
            "output_*.json selector"
        )

    def test_real_outputs_still_selected(self, tmp_path):
        _write(str(tmp_path / "output_20260905.json"), "{}")
        assert rex._find_newest_output(str(tmp_path)).endswith(
            "output_20260905.json"
        )


class TestReadProbeDiscoveredUrlsIdentity:
    def test_unchanged_seed_file_is_not_the_probes(self, tmp_path):
        p = str(tmp_path / "input_urls.json")
        _write(p, '["https://x/p1"]')
        snap = graph._identity_snapshot(str(tmp_path))
        assert graph._read_probe_discovered_urls(str(tmp_path), snap) == []

    def test_rewritten_seed_file_is_the_probes(self, tmp_path):
        p = str(tmp_path / "input_urls.json")
        _write(p, "[]")
        snap = graph._identity_snapshot(str(tmp_path))
        time.sleep(0.01)
        _write(p, '["https://x/p1", "https://x/p2"]')
        assert graph._read_probe_discovered_urls(str(tmp_path), snap) == [
            "https://x/p1",
            "https://x/p2",
        ]


class TestInconclusiveSemantics:
    def test_inconclusive_stamp_is_not_dead(self):
        stamp = {
            "discovered_urls": None,
            "stop_reason": "inconclusive_blank_coverage",
            "coverage": {},
            "inconclusive": True,
        }
        assert graph._probe_yield_dead(stamp) is False, (
            "blank evidence must not arm the zero-yield FAIL"
        )

    def test_real_zero_is_still_dead(self):
        dead = {"discovered_urls": 0, "stop_reason": "short_page", "coverage": {}}
        assert graph._probe_yield_dead(dead) is True

    def test_real_yield_is_alive(self):
        alive = {
            "discovered_urls": 9,
            "stop_reason": "max_pages_hit",
            "coverage": {"discovered_urls": 9, "stop_reason": "max_pages_hit"},
        }
        assert graph._probe_yield_dead(alive) is False

    def test_wrapper_binds_by_identity_and_stamps_blank(self):
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            src = fh.read()
        i = src.index("def _probe_phase1_discovery_once")
        j = src.index("\ndef ", i + 10)
        body = src[i:j]
        assert "_identity_snapshot(" in body, (
            "the probe never snapshots the workspace — output selection is "
            "still mtime guessing"
        )
        assert "_probe_owned_files(" in body
        assert "inconclusive_blank_coverage" in body, (
            "blank coverage still lands as discovered_urls=0 — the zero-yield "
            "FAIL fires on no evidence"
        )
        assert "output_content" in body and "probe_output_" in body, (
            "the browser branch still discards the run's output_content — "
            "every browser draft probes inconclusive"
        )


class TestListingYieldFailureEdges:
    def test_empty_coverage_is_not_dead(self):
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure({}) is False, (
            "empty coverage = nothing observed — unknowable must not be dead"
        )

    def test_none_is_not_dead(self):
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure(None) is False

    def test_bare_skipped_is_not_dead(self):
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure({"stop_reason": "skipped"}) is False
        assert listing_yield_failure(
            {"stop_reason": "skipped", "discovered_urls": 0}
        ) is False

    def test_phase1_skipped_code_bug_stays_dead(self):
        from src.listing_discovery import listing_yield_failure

        assert listing_yield_failure(
            {"stop_reason": "phase1_skipped", "discovered_urls": 0}
        ) is True


class TestWrapperFixtureShape:
    """The real coverage reader must accept a persisted probe_output file."""

    def test_coverage_read_from_probe_file(self, tmp_path):
        out = {
            "items": [],
            "metadata": {
                "discovery_coverage": {
                    "discovered_urls": 0,
                    "stop_reason": "empty_first_page",
                }
            },
        }
        p = str(tmp_path / "probe_output_1757000000.json")
        _write(p, json.dumps(out))
        cov = rex._read_discovery_coverage(p)
        assert cov and cov["stop_reason"] == "empty_first_page"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
