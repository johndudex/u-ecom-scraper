"""Wave-64: writer contract adherence — the system-borne exec budget must
actually reach writer-generated drafts.

Fix-loop entry for the wave-63 caveat (Guardian ledger row 10): job 663's
writer received the job-662 RCA and self-implemented a graceful budget — but
under its OWN env name (``SCRAPER_TIME_BUDGET_S``, default 540, a category-leg
value) instead of consuming the injected ``SCRAPER_EXEC_TIME_BUDGET=3000``.
The flagship passed on a writer-guessed fuse; one different writer roll from
landing 0 rows on a bigger catalogue, and run_execution still had no control
over the bound.

Contract (docs/plans/wave64-writer-contract-plan.md, critique-revised v2):

- **F1 alias belt**: ``_exec_budget_env()`` returns BOTH
  ``SCRAPER_EXEC_TIME_BUDGET`` AND ``SCRAPER_TIME_BUDGET_S`` (same str value);
  disabled ⇒ ``{}`` (no alias leakage). Templates must NOT read the alias —
  it is a DRAFT-side compatibility name, not a template contract.
- **F2 writer prompt contract**: ``.opencode/agents/code-writer.md`` (the
  HYPHENATED path — AGENT_PROMPT_MAP maps "code_writer" → "code-writer";
  critique-1 caught the plan targeting a dead file) carries the contract
  block: verbatim consumption, SECONDS units, never invent an env name, never
  hard-default a budget, bound recorded at
  ``metadata.discovery_coverage.time_budget_bounded``, resolved bound logged
  once at startup (stderr → job log carries the proof — the SessionLog does
  not record request envs).
- **F3 deterministic contract scan (position-safe, warning-only)**: pure
  function ``missing_contract_env_issue(source)`` in webapp/agents/
  draft_safety.py returns a tester-shaped issue dict
  (``issue_type: "missing_contract_env"``, severity ``medium``) when the
  contract env name is absent from the draft source; wired in graph.py's
  ``_invoke_code_tester`` deterministic-scan region APPENDED to the tail —
  NEVER position 0 (issues[0] is the remediation-message fallback) and never
  a forced FAIL (MEDIUM is non-veto by existing routing).

House style (test_wave63): static source contracts for templates/prompts and
wiring; behavioral exec-from-source for the pure helper; the run_execution
helper is pinned by source anchors + behavioral import (Django settings are
configured by the suite's conftest).
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RUN_EXECUTION = os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")
NAV_TEMPLATE = os.path.join(ROOT, "templates", "navigation_scraper.py")
HTTP_TEMPLATE = os.path.join(ROOT, "templates", "http_navigation_scraper.py")
WRITER_PROMPT = os.path.join(ROOT, ".opencode", "agents", "code-writer.md")
DRAFT_SAFETY = os.path.join(ROOT, "webapp", "agents", "draft_safety.py")
GRAPH = os.path.join(ROOT, "webapp", "agents", "graph.py")

CONTRACT_ENV = "SCRAPER_EXEC_TIME_BUDGET"
ALIAS_ENV = "SCRAPER_TIME_BUDGET_S"


def _src(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _grab_fn(source: str, name: str) -> str:
    """AST-exact grab of a top-level function's source (regex grabs overrun)."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


# ---------------------------------------------------------------------------
# F1 — alias belt
# ---------------------------------------------------------------------------
class TestF1AliasBelt:
    def test_helper_returns_both_keys_enabled(self):
        re_src = _src(RUN_EXECUTION)
        assert f'"{CONTRACT_ENV}"' in _grab_fn(re_src, "_exec_budget_env")
        assert f'"{ALIAS_ENV}"' in _grab_fn(re_src, "_exec_budget_env")

    def test_helper_behavioral_both_keys_equal_str(self):
        import importlib

        import django

        django.setup()
        # webapp.agents.nodes re-exports a FUNCTION named run_execution (the
        # node); importlib reaches the module itself.
        re_mod = importlib.import_module("webapp.agents.nodes.run_execution")

        env = re_mod._exec_budget_env(3000)
        assert env[CONTRACT_ENV] == "3000"
        assert env[ALIAS_ENV] == "3000"

    def test_helper_behavioral_disabled_is_empty(self):
        import importlib

        import django

        django.setup()
        re_mod = importlib.import_module("webapp.agents.nodes.run_execution")

        assert re_mod._exec_budget_env(0) == {}
        assert re_mod._exec_budget_env(-5) == {}

    def test_injection_sites_unchanged(self):
        body = _src(RUN_EXECUTION)
        assert body.count("_exec_budget_env(") >= 3

    def test_templates_do_not_read_the_alias(self):
        # The alias is a DRAFT-side compatibility name. If a template ever
        # starts reading it, the two-deadline reserve math is being bypassed.
        assert ALIAS_ENV not in _src(NAV_TEMPLATE)
        assert ALIAS_ENV not in _src(HTTP_TEMPLATE)


# ---------------------------------------------------------------------------
# F2 — writer prompt contract
# ---------------------------------------------------------------------------
class TestF2WriterPrompt:
    def test_prompt_file_is_the_hyphenated_path(self):
        # critique-1: the live prompt is code-writer.md (AGENT_PROMPT_MAP
        # "code_writer" -> "code-writer", subagents.py). A code_writer.md
        # file would be a dead artifact the live writer never sees.
        assert os.path.isfile(WRITER_PROMPT), (
            "live writer prompt .opencode/agents/code-writer.md missing — "
            "AGENT_PROMPT_MAP retargeted?"
        )

    def test_contract_block_anchors(self):
        src = _src(WRITER_PROMPT)
        assert CONTRACT_ENV in src
        assert ALIAS_ENV in src
        assert "SECONDS" in src  # units pinned loudly (units-misread gap)
        assert "never invent" in src.lower()
        assert "time_budget_bounded" in src

    def test_contract_block_has_startup_log_rule(self):
        # The SessionLog does not record request envs; the draft's own
        # startup log line is the only live proof the bound was consumed.
        src = _src(WRITER_PROMPT).lower()
        assert "log" in src and ("startup" in src or "start-up" in src)

    def test_subagents_prompt_map_still_targets_hyphenated_file(self):
        sub = _src(os.path.join(ROOT, "webapp", "agents", "subagents.py"))
        m = re.search(r'"code_writer":\s*"([^"]+)"', sub)
        assert m and m.group(1) == "code-writer"


# ---------------------------------------------------------------------------
# F3 — deterministic contract scan (position-safe, warning-only)
# ---------------------------------------------------------------------------
class TestF3ContractScan:
    def _func(self):
        from webapp.agents.draft_safety import missing_contract_env_issue

        return missing_contract_env_issue

    def test_absent_name_yields_medium_warning_issue(self):
        issue = self._func()("_RESULT = fetch_all()\nprint(_RESULT)\n")
        assert isinstance(issue, dict)
        assert issue["issue_type"] == "missing_contract_env"
        assert issue["severity"] == "medium"
        assert issue["field"] == "time_budget"
        assert issue.get("description")
        assert issue.get("suggested_fix")

    def test_present_name_yields_none(self):
        src = f'import os\n_B = os.environ.get("{CONTRACT_ENV}", "")\n'
        assert self._func()(src) is None

    def test_alias_alone_is_not_compliance(self):
        # A draft reading ONLY the alias predates the belt; the contract is
        # the primary name. (The alias still controls its value via F1 — this
        # is a warning about drift, not a hard fail.)
        src = f'import os\n_B = os.environ.get("{ALIAS_ENV}", "")\n'
        assert self._func()(src) is not None

    def test_empty_source_yields_issue(self):
        assert self._func()("") is not None

    def test_graph_wiring_is_position_safe_append(self):
        gsrc = _src(GRAPH)
        idx = gsrc.find("missing_contract_env_issue(")
        assert idx > 0, "F3 scan not wired into graph.py _invoke_code_tester"
        window = gsrc[idx : idx + 1200]
        assert "insert(0" not in window, (
            "F3 issue must never take position 0 (issues[0] is the "
            "remediation-message fallback)"
        )
        assert ('report["issues"] = (report.get("issues") or [])' in window) or (
            ".append(" in window
        ), "F3 issue must be APPENDED to the tail"

    def test_graph_wiring_is_exception_gagged(self):
        # Both neighbouring deterministic scans are try/except-gagged so a
        # scan bug can never kill the tester invocation — same belt required.
        gsrc = _src(GRAPH)
        idx = gsrc.find("missing_contract_env_issue(")
        before = gsrc[max(0, idx - 600) : idx]
        assert "try:" in before

    def test_graph_wiring_never_manufactures_a_report(self):
        # [job-81 regression guard, real RCA 2026-10-14] The dead-tester arm
        # ships test_report=None ON PURPOSE (stale-report rejection). The
        # first F3 wiring did `report = report or {}` and conjured a report
        # on that path — the job81 contract tests caught it. The scan must
        # gate on a LIVE dict report and must not contain the or-{} conjure.
        gsrc = _src(GRAPH)
        idx = gsrc.find("missing_contract_env_issue(")
        window = gsrc[max(0, idx - 700) : idx + 200]
        assert "isinstance(report, dict)" in window
        assert "report = report or {}" not in window
