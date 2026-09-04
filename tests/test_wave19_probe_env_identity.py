"""[wave-19 T0.2] The zero-yield Phase-1 probe must test EXECUTION's identity.

Job 324 RCA: the tester node's deterministic ``--discover-only`` gate killed
the job with a zero yield — but ``_probe_env`` was built from bare
``os.environ`` (plus the page cap + listing URL), staging NONE of the access
recipe's proxy tiers. The gate therefore tested a strictly weaker identity
than the one execution would have used (``_stealth_env`` stages
``SCRAPER_PROXY_TIER`` / ``SCRAPER_DISCOVERY_PROXY_TIER`` for the real run),
so a draft that would have succeeded under its measured recipe was condemned
by a probe running direct-only.

Contract pinned here:
- Local (in-process) probe path: ``_probe_env`` carries ``_stealth_env(state)``.
- Browser-dispatch probe path: the ``/scrape`` ``env_overrides`` carries it too.
- No recipe / same-tier recipe ⇒ nothing staged (byte-identical legacy env).
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents import graph  # noqa: E402
import importlib  # noqa: E402

# agents/nodes/__init__ re-exports the run_execution NODE FUNCTION under the
# same name as the module — importlib gets the module (sys.modules wins).
run_exec_mod = importlib.import_module("agents.nodes.run_execution")


JOB_URL = "https://www.myhouse.com.au/products/arcosteel-popcorn-maker-black"
LISTING = "https://myhouse.com.au/collections/sale-clearance"


def _state(recipe: dict | None) -> dict:
    st = {
        "job_id": 0,
        "site_slug": "myhouse-com-au",
        "url": JOB_URL,
        "input_mode": "list_page",
        "search_criteria": "",
    }
    if recipe is not None:
        # _scraper_proxy_tier/_discovery_proxy_tier read the recipe off
        # scraper_analysis.access_recipe — that's where _derive_strategy puts it.
        st["scraper_analysis"] = {"access_recipe": recipe}
    return st


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    """Run _probe_phase1_discovery_once with the local dispatch faked; return env."""
    slug = "myhouse-com-au"
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft\n", encoding="utf-8")
    monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        run_exec_mod, "_accepted_cli_flags", lambda path: ["discover-only", "fresh-discovery"]
    )
    from agents.tools import shell_tools

    monkeypatch.setattr(shell_tools, "_scraper_needs_browser", lambda path: False)

    captured: dict = {}

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, env=None):
        captured["env"] = env
        proc = subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return proc

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return captured


class TestProbeEnvIdentity:
    def test_local_probe_carries_recipe_tiers(self, probe_env):
        st = _state({"proxy_tier": "datacenter", "discovery_proxy_tier": "residential"})
        crashed, tail, _ = graph._probe_phase1_discovery_once(
            "myhouse-com-au", st, 0, listing_override=LISTING
        )
        assert crashed is False
        env = probe_env["env"]
        assert env["SCRAPER_PROXY_TIER"] == "datacenter"
        # S17 rule: discovery tier staged only when it differs from the PDP tier.
        assert env["SCRAPER_DISCOVERY_PROXY_TIER"] == "residential"
        # the original probe keys are untouched
        assert "SCRAPER_DISCOVERY_MAX_PAGES" in env
        assert env["SCRAPER_LISTING_URL"] == LISTING

    def test_local_probe_same_tier_stages_nothing(self, probe_env):
        st = _state({"proxy_tier": "none", "discovery_proxy_tier": "none"})
        graph._probe_phase1_discovery_once(
            "myhouse-com-au", st, 0, listing_override=LISTING
        )
        env = probe_env["env"]
        assert "SCRAPER_PROXY_TIER" not in env
        assert "SCRAPER_DISCOVERY_PROXY_TIER" not in env

    def test_local_probe_no_recipe_stages_nothing(self, probe_env):
        st = _state(None)
        graph._probe_phase1_discovery_once(
            "myhouse-com-au", st, 0, listing_override=LISTING
        )
        env = probe_env["env"]
        assert "SCRAPER_PROXY_TIER" not in env

    def test_browser_dispatch_env_overrides_carry_recipe_tiers(self, tmp_path, monkeypatch):
        slug = "myhouse-com-au"
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("# draft\n", encoding="utf-8")
        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(
            run_exec_mod, "_accepted_cli_flags", lambda path: ["discover-only", "fresh-discovery"]
        )
        from agents.tools import shell_tools

        monkeypatch.setattr(shell_tools, "_scraper_needs_browser", lambda path: True)

        captured: dict = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"returncode": 0, "stdout": "", "stderr": ""}

        def _fake_post(url, json=None, timeout=None):
            captured["body"] = json
            return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "post", _fake_post)

        st = _state({"proxy_tier": "residential", "discovery_proxy_tier": "datacenter"})
        graph._probe_phase1_discovery_once(
            "myhouse-com-au", st, 0, listing_override=LISTING
        )
        ov = captured["body"]["env_overrides"]
        assert ov["SCRAPER_PROXY_TIER"] == "residential"
        assert ov["SCRAPER_DISCOVERY_PROXY_TIER"] == "datacenter"
        assert ov["SCRAPER_LISTING_URL"] == LISTING
