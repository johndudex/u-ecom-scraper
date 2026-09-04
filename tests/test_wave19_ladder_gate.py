"""[wave-19 T1.1] Structural ladder-preservation gate — the wall that stops
the 324 draft class from existing.

Job 324 (myhouse): the writer shipped an api-family draft whose custom
``_http_get`` checked ``proxy_config.is_banned()`` but carried NO ``proxies=``
kwarg, NO ``src.http_fetch`` import, and defined ``DIRECT_ONLY_TIERS`` it
never consumed — a structurally unproxied draft. The tester PASSED it (the
probe URL answered direct that hour), execution zeroed under the throttle,
and the zero-yield gate killed the job.

The gate mirrors ``cli_contract_violation``: satisfied by ANY of
  L1  ``import src.http_fetch`` / ``from src.http_fetch import ...``
  L2  any HTTP call carrying a ``proxies=`` keyword
  L3  a shared-ladder call: ``create_fetch_*`` / ``get_escalation_tier`` /
      ``get_proxy_dict``
Absent all three → violation. ``DIRECT_ONLY_TIERS`` is deliberately NOT a
satisfying marker (dead token — defined, never consumed). Browser-only
strategies are exempt: their fetching rides a real browser, not the HTTP
ladder.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents.draft_safety import ladder_preservation_violation  # noqa: E402

# The exact 324 shape: custom _http_get with a banned-check, no proxy wiring.
DRAFT_324 = '''
import requests
from src.proxy import ProxyConfig

DIRECT_ONLY_TIERS = ["none"]

proxy_config = ProxyConfig.get_instance()

def _http_get(url):
    resp = requests.get(url, headers={"User-Agent": "x"}, timeout=15)
    if proxy_config.is_banned(resp.status_code, resp.text):
        raise RuntimeError("banned")
    return resp.text
'''

DRAFT_WITH_IMPORT = '''
from src.http_fetch import create_fetch_json
def main():
    fetch = create_fetch_json()
'''

DRAFT_WITH_PROXIES = '''
import requests
def fetch(url, proxies):
    return requests.get(url, proxies=proxies, timeout=10)
'''

DRAFT_WITH_LADDER_CALL = '''
def _get_fetch_json():
    from src.http_fetch import create_fetch_json
    return create_fetch_json()

def main():
    data, status = _get_fetch_json()("https://x/api")
'''

DRAFT_GET_PROXY_DICT = '''
import requests
def fetch(url):
    tier = proxy_config.get_escalation_tier()
    return requests.get(url)
'''


def _draft(tmp_path, body: str) -> str:
    p = tmp_path / "scraper_draft.py"
    p.write_text(body, encoding="utf-8")
    return str(p)


class TestLadderGate:
    def test_324_shaped_draft_is_a_violation(self, tmp_path):
        violation = ladder_preservation_violation(
            _draft(tmp_path, DRAFT_324), "list_page", "internal_api"
        )
        assert violation is not None
        assert "proxy" in violation.lower() or "ladder" in violation.lower()

    def test_direct_only_tiers_token_does_not_satisfy(self, tmp_path):
        """The dead token MUST NOT count as proxy wiring (job 324's trap)."""
        violation = ladder_preservation_violation(
            _draft(tmp_path, DRAFT_324), "list_page", "internal_api"
        )
        assert violation is not None

    def test_http_fetch_import_satisfies(self, tmp_path):
        assert ladder_preservation_violation(
            _draft(tmp_path, DRAFT_WITH_IMPORT), "list_page", "internal_api"
        ) is None

    def test_proxies_kwarg_satisfies(self, tmp_path):
        assert ladder_preservation_violation(
            _draft(tmp_path, DRAFT_WITH_PROXIES), "navigation", "http_requests"
        ) is None

    def test_shared_ladder_call_satisfies(self, tmp_path):
        assert ladder_preservation_violation(
            _draft(tmp_path, DRAFT_WITH_LADDER_CALL), "list_page", "internal_api"
        ) is None

    def test_get_proxy_dict_satisfies(self, tmp_path):
        assert ladder_preservation_violation(
            _draft(tmp_path, DRAFT_GET_PROXY_DICT), "list_page", "api"
        ) is None

    def test_browser_strategies_exempt(self, tmp_path):
        for strategy in ("playwright", "stealth_browser", "seleniumbase_uc",
                         "undetected_chromedriver"):
            assert ladder_preservation_violation(
                _draft(tmp_path, DRAFT_324), "list_page", strategy
            ) is None, strategy

    def test_url_list_exempt(self, tmp_path):
        assert ladder_preservation_violation(
            _draft(tmp_path, DRAFT_324), "url_list", "internal_api"
        ) is None

    def test_unparseable_draft_never_blocks(self, tmp_path):
        assert ladder_preservation_violation(
            _draft(tmp_path, "def broken(:\n"), "list_page", "internal_api"
        ) is None

    def test_missing_file_never_blocks(self, tmp_path):
        assert ladder_preservation_violation(
            str(tmp_path / "absent.py"), "list_page", "internal_api"
        ) is None


class TestRunExecutionRefusal:
    @pytest.fixture
    def exec_env(self, tmp_path, monkeypatch):
        """run_execution with the workspace rooted at tmp_path."""
        import importlib

        run_exec_mod = importlib.import_module("agents.nodes.run_execution")
        slug = "myhouse-com-au"
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True)
        monkeypatch.setattr(run_exec_mod, "_get_project_root", lambda: str(tmp_path))
        return run_exec_mod, ws

    def test_unwired_draft_refused_before_launch(self, exec_env):
        run_exec_mod, ws = exec_env
        (ws / "scraper_draft.py").write_text(DRAFT_324, encoding="utf-8")
        state = {
            "job_id": 0,
            "site_slug": "myhouse-com-au",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "internal_api"},
        }
        result = run_exec_mod.run_execution(state)
        assert result["execution_status"] == "FAILED"
        assert "proxy" in (result.get("error_message") or "").lower() or \
            "ladder" in (result.get("error_message") or "").lower()

    def test_wired_draft_proceeds_past_the_gate(self, exec_env, monkeypatch):
        """A ladder-carrying draft must NOT be refused by THIS gate (it will
        fail later on missing machinery — here we detect the gate passing by
        the error message NOT being the ladder refusal)."""
        run_exec_mod, ws = exec_env
        (ws / "scraper_draft.py").write_text(DRAFT_WITH_LADDER_CALL, encoding="utf-8")
        state = {
            "job_id": 0,
            "site_slug": "myhouse-com-au",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "internal_api"},
        }
        result = run_exec_mod.run_execution(state)
        msg = result.get("error_message") or ""
        assert "no proxy-aware fetch path" not in msg
