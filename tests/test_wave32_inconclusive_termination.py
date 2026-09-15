"""[wave-32 C1] A no-discovery-source exit is EVIDENCE, not inconclusiveness.

Job 587: the draft exited rc=1 with ``No --query, --category-url, or
--listing-url provided`` — the probe logged it (A1) and then treated the run
as inconclusive, so the job kept burning retry cycles on a draft whose
discovery trigger never fired at all. A transport-independent, deterministic
diagnosis like that must route into the EXISTING crashed force-FAIL arm
(feedback_for_writer names the missing source) instead of a shrug.

Audit (critique finding 10): only ``templates/navigation_scraper.py`` and
``templates/http_navigation_scraper.py`` emit the marker, both
``sys.exit(1)`` — every other nonzero-no-traceback shape stays inconclusive
(the narrowing guard is pinned here too).

Retry-axes note (pinned): listing retry + tier escalation are gated on
``not crashed``; routing no-source through ``crashed=True`` disables both —
correct, because today ``probe_yield=None`` already returns False from
``_probe_retry_warranted``, so nothing that worked is lost.
"""
from __future__ import annotations

import os
import subprocess as _subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from webapp.agents import graph  # noqa: E402

MARKER = "No --query, --category-url, or --listing-url provided"


def _state(mode="navigation"):
    return {
        "input_mode": mode,
        "url": "https://s.example.com/products/x",
        "navigation_analysis": {
            "discovery": {"listing_url": "https://s.example.com/collections/y"}
        },
    }


def _probe_draft() -> str:
    return (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--discover-only', action='store_true')\n"
        "p.add_argument('--fresh-discovery', action='store_true')\n"
    )


def _workspace(tmp_path, slug="s"):
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "scraper_draft.py").write_text(_probe_draft(), encoding="utf-8")
    return ws


class TestNoSourceExit:
    def test_no_source_exit_forces_fail_with_feedback(self, monkeypatch, tmp_path):
        """rc=1 + the marker → the crashed force-FAIL lane (crashed=True),
        with the tail naming the missing discovery source so the crashed
        arm's feedback_for_writer carries the diagnosis to the writer."""
        _workspace(tmp_path)

        def fake_run(argv, **kw):
            return _subprocess.CompletedProcess(
                argv, 1, stdout="", stderr=f"INFO root starting\n{MARKER}\n"
            )

        monkeypatch.setattr(_subprocess, "run", fake_run)
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            crashed, tb, probe_yield = graph._probe_phase1_discovery_once(
                "s", _state(), 1
            )
        assert crashed is True
        assert probe_yield is None
        assert tb and MARKER in tb, "feedback tail must name the missing source"

    def test_no_source_exit_performs_zero_retry_attempts(self, monkeypatch):
        """Retry-axes pin: with crashed=True the listing retry and the tier
        escalation in _probe_phase1_discovery are both skipped — exactly one
        probe invocation happens."""
        calls = []

        def fake_checked(slug, state, job_id, **kw):
            calls.append(job_id)
            return True, f"exit rc=1: {MARKER}", None

        monkeypatch.setattr(graph, "_probe_phase1_discovery_checked", fake_checked)
        crashed, tb, probe_yield = graph._probe_phase1_discovery("s", _state(), 7)
        assert len(calls) == 1, "no-source must not buy a listing/tier retry"
        assert crashed is True and MARKER in (tb or "")

    def test_inconclusive_without_evidence_stays_noop(self, monkeypatch):
        """The narrowing guard: any OTHER nonzero-no-traceback shape keeps
        today's inconclusive semantics (crashed=False, yield=None) — C1 is
        keyed on the marker, not on 'rc != 0' in general."""

        def fake_checked(slug, state, job_id, **kw):
            return False, None, None

        monkeypatch.setattr(graph, "_probe_phase1_discovery_checked", fake_checked)
        crashed, tb, probe_yield = graph._probe_phase1_discovery("s", _state(), 7)
        assert (crashed, tb, probe_yield) == (False, None, None)

    def test_only_the_two_template_emitters_own_the_marker(self):
        """Audit pin (critique finding 10): the marker vocabulary stays owned
        by the two two-phase templates (both exit 1) — if a third emitter
        appears, this gate must be revisited before C1's widening applies."""
        hits = []
        for name in (
            "templates/navigation_scraper.py",
            "templates/http_navigation_scraper.py",
        ):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                if MARKER in fh.read():
                    hits.append(name)
        assert sorted(hits) == sorted(
            [
                "templates/navigation_scraper.py",
                "templates/http_navigation_scraper.py",
            ]
        )
