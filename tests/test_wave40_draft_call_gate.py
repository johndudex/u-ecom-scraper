"""[wave-40 T6] AST pre-gate: helper CALL signatures in generated drafts.

Prod: 760 (unexpected kwarg 'post' on the create_fetch_json closure),
791 (multiple values for 'fetch_page'), 762 (re.error nothing to repeat) —
all crashed at execution after passing compile + F821."""

import json
import os
import time

import pytest

from agents.draft_safety import (
    DRAFT_CALL_VIOLATION_MARKER, _REGISTRY_MODULES, _registry_root,
    draft_call_violation)

FETCHER_BAD = (
    "from src.http_fetch import create_fetch_json\n"
    "fetch_json = create_fetch_json()\n"
    "rows = fetch_json('https://x.example/api', post={'q': 1})\n"
)  # job 760 shape: closure takes (url, params=None, min_tier=0)

DISCOVERY_BAD = (
    "from src.listing_discovery import (\n"
    "    discover_listing_urls_with_retry, create_fetch_page)\n"
    "fetch_page = create_fetch_page()\n"
    "urls = discover_listing_urls_with_retry(\n"
    "    'https://x.example', fetch_page, fetch_page=fetch_page)\n"
)  # job 791 shape: fetch_page positional AND keyword

REGEX_BAD = "import re\nPAT = re.compile('a{2,1}')\n"  # job 762 shape

FETCHER_GOOD = (
    "from src.http_fetch import create_fetch_json\n"
    "fetch_json = create_fetch_json()\n"
    "rows = fetch_json('https://x.example/api', params={'q': 1})\n"
)

DISCOVERY_GOOD = (
    "from src.listing_discovery import (\n"
    "    discover_listing_urls_with_retry, create_fetch_page)\n"
    "fetch_page = create_fetch_page()\n"
    "urls = discover_listing_urls_with_retry('https://x.example', fetch_page)\n"
)


def test_unexpected_kwarg_is_caught():
    out = draft_call_violation(FETCHER_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "post" in out


def test_multiple_values_for_kwarg_is_caught():
    out = draft_call_violation(DISCOVERY_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "fetch_page" in out


def test_bad_regex_literal_is_caught():
    out = draft_call_violation(REGEX_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)


def test_valid_registry_calls_pass():
    assert draft_call_violation(FETCHER_GOOD + "\nimport re\nre.findall(r'x+', html)\n") == ""
    assert draft_call_violation(DISCOVERY_GOOD) == ""


def test_draft_local_defs_shadow_the_registry():
    src = ("def fetch_json(url, post=None):\n"
           "    return {}\n"
           "fetch_json('https://x', post={'a': 1})\n")
    assert draft_call_violation(src) == ""


def test_starred_and_missing_args_stay_legal():
    src = (FETCHER_GOOD +
           "fetch_json(*parts)\n"
           "fetch_json()\n")
    assert draft_call_violation(src) == ""


def test_unknown_helpers_fall_open():
    assert draft_call_violation("my_invented_helper(url, post=1, bogus=2)\n") == ""


def test_findings_are_capped_at_five():
    calls = "\n".join(
        f"fetch_json('https://x', post={i})" for i in range(9))
    src = ("from src.http_fetch import create_fetch_json\n"
           "fetch_json = create_fetch_json()\n" + calls)
    out = draft_call_violation(src)
    assert out.count("\n") <= 5


def test_internal_error_falls_open(monkeypatch):
    from agents import draft_safety as ds
    monkeypatch.setattr(ds, "_helper_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert draft_call_violation("fetch_json('https://x', post=1)") == ""


# ── [T6 r1 review pins] The factory-own-signature path — regression pins for
# behavior that already shipped in the T6 commit, NOT RED-first discoveries.
# The registry maps a factory name to its CLOSURE signature (both names), but
# a factory is also INVOKED with its own kwargs (delay_s=/headers=), and 4 of
# the 7 active templates do exactly that — requests_scraper.py:105 at module
# level, api/shopify/http_navigation inside _get_fetch_*(). If the factory-sig
# path regresses (the `elif name in factory_sigs` branch or the
# factories.setdefault() line), every template-derived draft would be
# false-flagged at the writer's reject seam — the fail-closed direction.

def test_factory_invocation_with_its_own_kwargs_passes():
    out = draft_call_violation(
        "fetch_page = create_fetch_page(delay_s=2.0, headers={})\n"
        "fetch_page('u')\n")
    assert out == ""


def test_bad_factory_kwarg_is_caught():
    out = draft_call_violation(
        "from src.http_fetch import create_fetch_page\n"
        "create_fetch_page(bogus=1)\n")
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "bogus" in out


def test_aliased_closure_target_name_is_checked():
    # The alias target name exists ONLY in the draft (api/shopify template
    # shape) — without factory→closure aliasing this call would fall open.
    out = draft_call_violation(
        "from src.http_fetch import create_fetch_json\n"
        "_FETCH_JSON = create_fetch_json(delay_s=1.0, headers={})\n"
        "_FETCH_JSON('https://x.example/api', post={'q': 1})\n")
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "post" in out


@pytest.mark.parametrize("module_path", _REGISTRY_MODULES)
def test_registry_module_sources_pass_their_own_gate(module_path):
    # Locks the 7/7-template sweep: the gate must never flag the real helper
    # modules drafts are derived from. A missing registry module fails loudly
    # here (the registry build would otherwise skip it silently).
    path = os.path.join(_registry_root(), module_path)
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    assert draft_call_violation(source) == "", (
        f"{module_path} is flagged by its own call gate")


# ══ [wave-40 T7] The write-path seam + the L2 belt carry the call gate ═══════
#
# T6 built the judge; T7 wires it into the seams a violating draft crosses.
# The real tool surface (critique fix): get_filesystem_tools(project_root=...,
# syntax_gate=True) returns tool objects with .name/.func, and a rejection is
# the write_file RESULT with the file NOT created.

def _tools(tmp_path):
    from agents.tools import filesystem_tools as ft
    return {t.name: t.func
            for t in ft.get_filesystem_tools(project_root=str(tmp_path),
                                             syntax_gate=True)}


class TestCallGateWiring:
    """[wave-40 T7] the write-path gate runs draft_call_violation beside the
    F821 check, under SCRAPER_DRAFT_CALL_GATE (default on)."""

    def test_write_tool_rejects_a_bad_signature_draft(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCRAPER_DRAFT_CALL_GATE", "1")
        out = _tools(tmp_path)["write_file"](
            path="workspace/example-com/scraper_draft.py", content=FETCHER_BAD)
        assert "HELPER CALL SIGNATURE VIOLATION" in str(out)
        assert not (tmp_path / "workspace" / "example-com"
                    / "scraper_draft.py").exists()

    def test_kill_switch_disables_the_call_gate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCRAPER_DRAFT_CALL_GATE", "0")
        out = _tools(tmp_path)["write_file"](
            path="workspace/example-com/scraper_draft.py", content=FETCHER_BAD)
        assert "HELPER CALL SIGNATURE VIOLATION" not in str(out)
        assert (tmp_path / "workspace" / "example-com"
                / "scraper_draft.py").exists()

    def test_f821_rejections_carries_the_call_gate(self, monkeypatch):
        from agents.tools import filesystem_tools as ft
        monkeypatch.setenv("SCRAPER_DRAFT_CALL_GATE", "1")
        assert "HELPER CALL SIGNATURE VIOLATION" in ft._f821_rejections(
            "workspace/x/scraper_draft.py", FETCHER_BAD)
        assert ft._f821_rejections(
            "workspace/x/scraper_draft.py", FETCHER_GOOD) == ""

    def test_call_gate_defaults_on(self, tmp_path, monkeypatch):
        # No env var at all → the gate is ARMED (the prod default).
        monkeypatch.delenv("SCRAPER_DRAFT_CALL_GATE", raising=False)
        out = _tools(tmp_path)["write_file"](
            path="workspace/example-com/scraper_draft.py", content=FETCHER_BAD)
        assert "HELPER CALL SIGNATURE VIOLATION" in str(out)

    def test_f821_rejection_keeps_priority_over_the_call_gate(self, monkeypatch):
        # A draft with BOTH defects bounces on the undefined name (the more
        # basic defect, and the message the writer already knows how to fix);
        # the call gate is reached only once the F821 check is clean.
        from agents.tools import filesystem_tools as ft
        monkeypatch.setenv("SCRAPER_DRAFT_CALL_GATE", "1")
        both = FETCHER_BAD + "rows = TOTALLY_UNDEFINED_NAME\n"
        out = ft._f821_rejections("workspace/x/scraper_draft.py", both)
        assert "F821" in out and "TOTALLY_UNDEFINED_NAME" in out
        assert "HELPER CALL SIGNATURE VIOLATION" not in out

    def test_l2_report_carries_violation_kind_call(self, monkeypatch, tmp_path):
        # Copied L2 harness (test_job81's _run_node / test_cli_contract's
        # deterministic-gate shape): a stubbed tester writes a PASS verdict,
        # then the deterministic L2 chain in _invoke_code_tester re-judges the
        # on-disk draft. FETCHER_BAD rides every existing arm clean — no
        # undefined name (F821), input_mode "" exempts the CLI gate, and the
        # src.http_fetch import satisfies the ladder — so only the call gate
        # can force this FAIL.
        from langgraph.types import RunnableConfig

        import agents.graph as g

        slug = "example-com"
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text(FETCHER_BAD, encoding="utf-8")
        report = {
            "overall_assessment": "PASS",
            "confidence_score": 0.9,
            "ready_for_execution": True,
            "issues": [],
            "results": {"successful_extractions": 3},
        }
        (ws / "test_report.json").write_text(json.dumps(report), encoding="utf-8")
        # mtime floor: only a verdict written DURING the attempt is adopted.
        _future = time.time() + 120
        os.utime(ws / "test_report.json", (_future, _future))

        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "set_tool_context", lambda *a, **k: None)
        monkeypatch.setattr(g, "clear_tool_context", lambda: None)
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(g, "build_code_tester_message", lambda state: [])
        monkeypatch.setattr(g, "_log_agent_context", lambda *a, **k: None)
        monkeypatch.setattr(g, "_start_heartbeat", lambda *a, **k: 0)
        monkeypatch.setattr(g, "_stop_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(g, "_agent_config", lambda *a, **k: {})
        monkeypatch.setattr(g, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(g, "create_code_tester", lambda site_slug="", **_: object())
        monkeypatch.setattr(
            g, "_invoke_agent_with_timeout",
            lambda *a, **k: {"messages": ["verdict written"]},
        )
        monkeypatch.setattr(
            g, "_probe_phase1_discovery", lambda *a, **k: (False, None, None))
        # Hermetic: no live browser-service /health poll from a unit test.
        monkeypatch.setattr(
            "agents.tools.browser_http.wait_for_browser_service",
            lambda *a, **k: True)

        from django.conf import settings as dj_settings
        monkeypatch.setattr(dj_settings, "PROJECT_ROOT", str(tmp_path), raising=True)

        out = g._invoke_code_tester(
            {"job_id": 0, "site_slug": slug, "test_retry_count": 0},
            RunnableConfig(),
        )
        rep = out.get("test_report") or {}
        assert rep["overall_assessment"] == "FAIL"
        assert rep["deterministic_gate"]["violation_kind"] == "call"
        assert "HELPER CALL SIGNATURE VIOLATION" in str(
            rep["feedback_for_writer"])


class TestFenceSalvageFence:
    """[wave-40 T7] the writer's fence-recovery bypass writes straight to
    disk, skipping every write-path gate — it must not become a side door a
    signature-violating draft slips through into testing (source-inspection
    idiom: the writer node is too heavy to stub for one line of contract)."""

    def _fence_block(self) -> str:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "webapp", "agents", "graph.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        i = src.index("[B1.3/wave-13] Fence recovery")
        j = src.index("_invoke_code_writer: fence recovery failed", i)
        return src[i:j]

    def test_salvage_runs_the_call_gate(self):
        assert "draft_call_violation(" in self._fence_block(), (
            "fence recovery writes straight to disk — without the call gate a "
            "signature-violating fenced draft is salvaged into testing and "
            "crashes at execution (the 760/791/762 class)"
        )

    def test_fence_honours_the_kill_switch(self):
        assert "SCRAPER_DRAFT_CALL_GATE" in self._fence_block()

    def test_fence_guards_before_the_disk_write(self):
        block = self._fence_block()
        i_gate = block.index("draft_call_violation(")
        i_write = block.index('open(_cw_pre, "w"')
        assert i_gate < i_write, (
            "the gate must be consulted BEFORE the fenced bytes reach disk — "
            "a violation has to leave the draft absent, not present-then-"
            "judged"
        )
