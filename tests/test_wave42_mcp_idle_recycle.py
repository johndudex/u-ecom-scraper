"""[wave-42 T2] Idle MCP-Chrome recycle — decision matrix + wiring pins.

The one-shot SSE discovery: _mcp_client_connected() is only true WHILE a tool
call is in flight — between a walk's steps (150s+ LLM turns) it reads false.
So the recycle decision needs an explicit walk-window claim (POST
/mcp/walk-claim) on top of the live-client check. All trigger arithmetic
lives in browser_service/recycle_policy.py (stdlib-only home; server.py
imports fastapi, absent in this image — the module loads BY PATH, same
idiom as test_wave37_proactive_recycle).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave42_mcp_idle_recycle.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

POLICY_PATH = os.path.join(ROOT, "browser_service", "recycle_policy.py")
SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")
GRAPH_PATH = os.path.join(ROOT, "webapp", "agents", "graph.py")


def _load(monkeypatch):
    """Fresh policy module with the wave-42 envs unset (import-time reads)."""
    for name in ("MCP_IDLE_RECYCLE", "MCP_IDLE_RECYCLE_COOLDOWN_S"):
        monkeypatch.delenv(name, raising=False)
    name = f"_recycle_policy_w42_{id(monkeypatch)}"
    spec = importlib.util.spec_from_file_location(name, POLICY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = 10_000.0


class TestMcpRecycleDue:
    def test_flag_off_never_fires(self, monkeypatch):
        rp = _load(monkeypatch)
        assert rp.mcp_recycle_due(
            flag=False, claim_until=0.0, now=NOW,
            last_recycle=0.0, cooldown_s=0.0, client_connected=False,
        ) is False

    def test_live_claim_blocks(self, monkeypatch):
        rp = _load(monkeypatch)
        assert rp.mcp_recycle_due(
            flag=True, claim_until=NOW + 1.0, now=NOW,
            last_recycle=0.0, cooldown_s=0.0, client_connected=False,
        ) is False

    def test_expired_claim_fires(self, monkeypatch):
        rp = _load(monkeypatch)
        assert rp.mcp_recycle_due(
            flag=True, claim_until=NOW - 1.0, now=NOW,
            last_recycle=0.0, cooldown_s=0.0, client_connected=False,
        ) is True

    def test_zero_claim_never_blocks(self, monkeypatch):
        rp = _load(monkeypatch)
        assert rp.mcp_recycle_due(
            flag=True, claim_until=0.0, now=NOW,
            last_recycle=0.0, cooldown_s=0.0, client_connected=False,
        ) is True

    def test_client_connected_blocks(self, monkeypatch):
        rp = _load(monkeypatch)
        assert rp.mcp_recycle_due(
            flag=True, claim_until=0.0, now=NOW,
            last_recycle=0.0, cooldown_s=0.0, client_connected=True,
        ) is False

    def test_cooldown_blocks_until_elapsed(self, monkeypatch):
        rp = _load(monkeypatch)
        assert rp.mcp_recycle_due(
            flag=True, claim_until=0.0, now=NOW,
            last_recycle=NOW - 3599.0, cooldown_s=3600.0, client_connected=False,
        ) is False
        assert rp.mcp_recycle_due(
            flag=True, claim_until=0.0, now=NOW,
            last_recycle=NOW - 3600.0, cooldown_s=3600.0, client_connected=False,
        ) is True

    def test_each_guard_independent(self, monkeypatch):
        """Flag + claim-clear + client-absent + cooldown must ALL hold —
        any single one failing keeps the Chrome up."""
        rp = _load(monkeypatch)
        base = dict(flag=True, claim_until=0.0, now=NOW,
                    last_recycle=0.0, cooldown_s=0.0, client_connected=False)
        assert rp.mcp_recycle_due(**base) is True
        for override in (dict(flag=False), dict(client_connected=True),
                         dict(claim_until=NOW + 100.0),
                         dict(last_recycle=NOW - 10.0, cooldown_s=600.0)):
            kw = {**base, **override}
            assert rp.mcp_recycle_due(**kw) is False, override


class TestEnvWiring:
    def test_defaults_flag_off_cooldown_6h(self, monkeypatch):
        rp = _load(monkeypatch)
        assert rp.MCP_IDLE_RECYCLE is False, (
            "flag-off is the ship state — prod behavior identical until the "
            "tradeoff is consciously taken"
        )
        assert rp.MCP_IDLE_RECYCLE_COOLDOWN_S == 21600.0


class TestServerWiring:
    """server.py can't import here (fastapi) — pin the wiring by source."""

    def _src(self):
        return open(SERVER_PATH, encoding="utf-8").read()

    def test_maintenance_hook_dispatches_recycle(self):
        src = self._src()
        assert "await _maybe_recycle_mcp_chrome()" in src
        # and the hook sits inside the cleanup cycle, exception-guarded
        m = re.search(
            r"Periodic Scraper Chrome recycle failed.*?"
            r"await _maybe_recycle_mcp_chrome\(\)", src, re.S)
        assert m, "MCP recycle leg must follow the scraper recycle leg"

    def test_recycle_uses_proven_restart_sequence(self):
        src = self._src()
        m = re.search(
            r"async def _maybe_recycle_mcp_chrome.*?"
            r"run_in_executor\(RESTART_EXECUTOR, browser_pool\.restart_chrome, \"mcp\"\)",
            src, re.S)
        assert m, "restart must run on RESTART_EXECUTOR like the liveness path"
        assert "await _start_mcp_process()" in src[m.end():m.end() + 600]

    def test_decision_gates_in_order(self):
        src = self._src()
        m = re.search(
            r"lambda: mcp_recycle_due\((.*?)\)\)", src, re.S)
        assert m, "the decision must be the guarded lambda"
        args = m.group(1)
        assert "MCP_IDLE_RECYCLE" in args
        assert "_MCP_WALK_CLAIMED_UNTIL[0]" in args
        assert "_mcp_client_connected()" in args
        assert "_MCP_LAST_RECYCLE[0]" in args
        assert "MCP_IDLE_RECYCLE_COOLDOWN_S" in args

    def test_walk_claim_endpoint_stamps_window(self):
        src = self._src()
        assert '"/mcp/walk-claim"' in src
        m = re.search(
            r"@app\.post\(\"/mcp/walk-claim\"\).*?"
            r"_MCP_WALK_CLAIMED_UNTIL\[0\] = time\.monotonic\(\) \+ ttl",
            src, re.S)
        assert m, "endpoint must stamp the claim window"
        # TTL clamp so a bogus caller can't block recycles forever
        assert "MCP_WALK_CLAIM_MAX_TTL_S" in src


class TestGraphClaimWiring:
    """The walk node must claim the Chrome for its whole budget."""

    def _src(self):
        return open(GRAPH_PATH, encoding="utf-8").read()

    def test_node_claims_before_the_walk(self):
        src = self._src()
        m = re.search(
            r'_notify_phase\(job_id, "browser_traverse", "running"\).*?'
            r"_claim_mcp_walk\(job_id\).*?"
            r"with _mcp_browser_lock\(job_id\)", src, re.S)
        assert m, "claim must fire after the phase notify, before the lock/walk"

    def test_claim_swallows_and_uses_walk_budget_ttl(self):
        src = self._src()
        m = re.search(
            r"def _claim_mcp_walk\(job_id: int\) -> None:(.*?)\ndef ", src, re.S)
        assert m
        body = m.group(1)
        assert "NAV_TRAVERSE_MAX_TIMEOUT" in body
        assert "+ 600" in body
        assert "except Exception" in body


class TestWalkClaimSender:
    """The sender's runtime behavior (posted shape, never raises)."""

    def test_posts_ttl_and_swallows_errors(self, monkeypatch):
        from webapp.agents import graph

        posted = {}

        def fake_post(url, json=None, timeout=None):
            posted["url"], posted["json"], posted["timeout"] = url, json, timeout
            class _R:
                def raise_for_status(self):
                    pass
            return _R()

        monkeypatch.setenv("NAV_TRAVERSE_MAX_TIMEOUT", "3600")
        monkeypatch.setattr(graph.requests, "post", fake_post)
        graph._claim_mcp_walk(4242)
        assert posted["timeout"] == 5
        assert posted["json"]["job_id"] == 4242
        assert posted["json"]["ttl_s"] == 3600 + 600  # walk ceiling + grace
        assert posted["url"].endswith("/mcp/walk-claim")

    def test_sender_never_raises(self, monkeypatch):
        from webapp.agents import graph

        def boom(*a, **kw):
            raise RuntimeError("browser-service down")

        monkeypatch.setattr(graph.requests, "post", boom)
        graph._claim_mcp_walk(1)  # must not raise
