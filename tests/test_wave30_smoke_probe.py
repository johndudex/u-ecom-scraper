"""[wave-30 W30-5] The pre-execution smoke run: the Phase-1 probe IS the
smoke — this wave pins its parity with execution and closes the url_list gap.

Prod proof (job 570): a url_list draft that promoted ``--fresh-discovery``
to a Phase-1 trigger PASSED testing (url_list skips Phase 1 there) and
crashed at execution. Two layers already own the protection this plan item
wanted — the deterministic ``_probe_phase1_discovery`` smoke (nav modes,
execution listing + identity + flags, crash → forced FAIL) and the tester's
own ``--sample`` seed-path run (url_list). What was MISSING is parity of the
flag decision: the probe appended ``--fresh-discovery`` unconditionally while
run_execution now gates it behind ``_wants_fresh_discovery`` (W30-4). One
decision, two consumers.

Contract under test:
1. url_list gets NO smoke run at all (no Phase-1 leg to test; the seed path
   is the tester sample's job) — the mode gate fires before any subprocess.
2. nav-family modes smoke WITH the execution flag set (--discover-only +
   --fresh-discovery) and read the yield from the output the run wrote.
3. A draft that crashes in discovery (e.g. the 570-shape TypeError) fails
   the smoke with the traceback tail — the forced-FAIL contract.
4. The probe's flag decision literally shares run_execution's predicate —
   not a second, drift-prone copy of the mode list.
"""
from __future__ import annotations

import json
import os
import subprocess as _subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def _state(mode):
    return {
        "input_mode": mode,
        "url": "https://s.example.com/products/x",
        "navigation_analysis": {"discovery": {"listing_url": "https://s.example.com/collections/y"}},
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


class TestSmokeParity:
    def test_url_list_gets_no_smoke_run(self, monkeypatch, tmp_path):
        """The 570 guard at the probe layer: no Phase-1 leg → no subprocess,
        inconclusive verdict. The seed path is the tester sample's job."""
        import webapp.agents.graph as graph

        _workspace(tmp_path)

        def no_run(*a, **k):
            raise AssertionError("url_list must not spawn a probe subprocess")

        monkeypatch.setattr(_subprocess, "run", no_run)
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            crashed, tb, probe_yield = graph._probe_phase1_discovery_once(
                "s", _state("url_list"), 1
            )
        assert (crashed, tb, probe_yield) == (False, None, None)

    def test_phase1_modes_smoke_with_the_execution_flag_set(self, monkeypatch, tmp_path):
        import webapp.agents.graph as graph

        ws = _workspace(tmp_path)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            out = ws / "output_probe_0.json"
            out.write_text(json.dumps({
                "products": [],
                "metadata": {"phase": "discovery", "discovery_coverage": {
                    "discovered_urls": 7, "stop_reason": "",
                }},
            }))
            return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            crashed, tb, probe_yield = graph._probe_phase1_discovery_once(
                "s", _state("search_term"), 1
            )
        assert crashed is False
        assert "--discover-only" in seen["argv"]
        assert "--fresh-discovery" in seen["argv"], (
            "the smoke must run under execution's flag set (W30-4: nav modes "
            "carry --fresh-discovery)"
        )
        assert probe_yield is not None and probe_yield["discovered_urls"] == 7

    def test_broken_discovery_callable_fails_the_smoke(self, monkeypatch, tmp_path):
        """The 570 crash shape: Phase-1 TypeError → crashed=True + traceback
        tail, which the tester node turns into a forced FAIL."""
        import webapp.agents.graph as graph

        _workspace(tmp_path)

        def fake_run(argv, **kwargs):
            return _subprocess.CompletedProcess(
                argv, 1, stdout="",
                stderr="Traceback (most recent call last):\n"
                       '  File "scraper_draft.py", line 231, in fetch_page\n'
                       "TypeError: fetch_page() got an unexpected keyword argument 'min_tier'",
            )

        monkeypatch.setattr(_subprocess, "run", fake_run)
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            crashed, tb, probe_yield = graph._probe_phase1_discovery_once(
                "s", _state("list_page"), 1
            )
        assert crashed is True
        assert "TypeError" in (tb or "")
        assert probe_yield is None

    def test_flag_gate_shares_run_executions_decision(self):
        """The probe must not carry its own copy of the Phase-1 mode list —
        drift between the two is exactly how 570 slipped through."""
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            src = fh.read()
        i_args = src.index("probe_args = ")
        window = src[max(0, i_args - 600) : i_args + 300]
        assert "_wants_fresh_discovery" in window, (
            "probe --fresh-discovery must be gated by _wants_fresh_discovery "
            "(the W30-4 predicate), not appended unconditionally"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
