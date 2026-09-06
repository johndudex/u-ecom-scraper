"""[wave-22 B1] code_writer's draft writes must clear an F821 gate.

The writer's dominant failure mode (337 class) is a NameError it could have
caught in the same breath: a name used at module level before definition, or
deleted outright in a self-edit. The SyntaxError only surfaces at tester
py_compile — one full test cycle later, and the writer's own _fix_scraper_syntax
backstop then fights a draft that several edits have since mangled. The gate
catches it at the WRITE, when the writer still has the whole file in mind.

Contract:
- the checker is RUFF, not a vendored scope checker (two independent
  hand-rolled checkers both false-flagged templates);
- F821-ONLY selection is load-bearing: drafts legitimately carry unused
  imports (F401) and locals (F841) — flagging those would reject healthy
  drafts;
- the gate falls OPEN on any checker malfunction (binary missing, timeout,
  crash) — a broken linter must never block a write;
- the rejection names the offending `line:col: undefined name X` (capped at
  5) and states the file was NOT modified;
- the gate scopes to code_writer's DRAFT writes only: .py files under
  workspace/ — never templates, never other agents' writes, never non-.py
  artifacts;
- `X if X in dir()` and define-later tricks are called out as non-fixes in
  the rejection (the writer has papered over NameErrors this way).
"""
from __future__ import annotations

import importlib
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

fs = importlib.import_module("agents.tools.filesystem_tools")  # noqa: E402
subagents = importlib.import_module("agents.subagents")  # noqa: E402

BAD = "PRODUCT_LISTING_URL = 'x'\n\nprint(PRODUCT_LISTING_URL)\nprint(MISSING_NAME)\n"
CLEAN = "NAME = 'x'\n\n\ndef main():\n    return NAME\n"


def _tools(root, gate: bool) -> dict:
    tools = fs.get_filesystem_tools(project_root=str(root), syntax_gate=gate)
    return {t.name: t.func for t in tools}


class TestF821Checker:
    def test_flags_undefined_name(self):
        msg = fs._f821_rejections("scraper_draft.py", BAD)
        assert msg, "undefined name must be caught at write time"
        assert "MISSING_NAME" in msg
        assert "NOT applied" in msg and "file unchanged" in msg
        assert "define the name before first use" in msg

    def test_clean_code_passes(self):
        assert fs._f821_rejections("scraper_draft.py", CLEAN) == ""

    def test_f821_only_selection(self):
        # F401 (unused import) + F841 (unused local) are benign in drafts —
        # selecting them would reject healthy work.
        benign = "import os\n\n\ndef main():\n    unused_local = 1\n    return None\n"
        assert fs._f821_rejections("scraper_draft.py", benign) == ""

    def test_falls_open_when_checker_dies(self, monkeypatch):
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ruff", timeout=10)

        monkeypatch.setattr(fs.subprocess, "run", _boom)
        assert fs._f821_rejections("scraper_draft.py", BAD) == "", (
            "a checker malfunction must fall OPEN — a broken linter must "
            "never block a write"
        )

    def test_falls_open_when_ruff_missing(self, monkeypatch):
        def _boom(*a, **k):
            raise FileNotFoundError("ruff")

        monkeypatch.setattr(fs.subprocess, "run", _boom)
        assert fs._f821_rejections("scraper_draft.py", BAD) == ""

    def test_findings_capped_at_five(self):
        body = "\n".join(f"print(UNDEF_{i})" for i in range(8))
        msg = fs._f821_rejections("scraper_draft.py", body + "\n")
        assert msg
        assert msg.count("F821") <= 5, "rejection must stay bounded (≤5 findings)"


class TestWriteFileGate:
    def test_gated_writer_rejects_bad_draft(self, tmp_path):
        w = _tools(tmp_path, gate=True)
        out = w["write_file"](
            path="workspace/example-com/scraper_draft.py", content=BAD
        )
        assert "NOT applied" in out
        assert not (
            tmp_path / "workspace" / "example-com" / "scraper_draft.py"
        ).exists(), "rejected write must leave NO file behind"

    def test_gated_writer_accepts_clean_draft(self, tmp_path):
        w = _tools(tmp_path, gate=True)
        out = w["write_file"](
            path="workspace/example-com/scraper_draft.py", content=CLEAN
        )
        assert "Successfully wrote" in out

    def test_gate_covers_only_workspace_python(self, tmp_path):
        w = _tools(tmp_path, gate=True)
        # non-.py artifact with garbage → written
        out = w["write_file"](
            path="workspace/example-com/site_analysis.json", content="{bad json"
        )
        assert "Successfully wrote" in out
        # .py OUTSIDE workspace (templates/) → not gated
        out = w["write_file"](path="templates/scratch_draft.py", content=BAD)
        assert "Successfully wrote" in out

    def test_ungated_tools_write_bad_draft(self, tmp_path):
        # 8 agents share write_file — without the kwarg nothing changes.
        w = _tools(tmp_path, gate=False)
        out = w["write_file"](
            path="workspace/example-com/scraper_draft.py", content=BAD
        )
        assert "Successfully wrote" in out


class TestEditFileGate:
    def test_gated_edit_rejected_and_file_unchanged(self, tmp_path):
        w = _tools(tmp_path, gate=True)
        p = "workspace/example-com/scraper_draft.py"
        assert "Successfully wrote" in w["write_file"](path=p, content=CLEAN)
        out = w["edit_file"](
            path=p,
            old_string="    return NAME",
            new_string="    return NAME + EXTRA_THING",
        )
        assert "NOT applied" in out
        on_disk = (tmp_path / p).read_text()
        assert "EXTRA_THING" not in on_disk, "rejected edit must not touch the file"

    def test_gated_edit_accepted_when_clean(self, tmp_path):
        w = _tools(tmp_path, gate=True)
        p = "workspace/example-com/scraper_draft.py"
        w["write_file"](path=p, content=CLEAN)
        out = w["edit_file"](
            path=p, old_string="    return NAME", new_string="    return NAME.upper()"
        )
        assert "Successfully replaced" in out


class TestWiring:
    def test_gate_scoped_to_code_writer(self):
        with open(
            os.path.join(ROOT, "webapp", "agents", "subagents.py")
        ) as fh:
            src = fh.read()
        assert 'syntax_gate=(agent_name == "code_writer")' in src, (
            "the F821 gate must be kwarg-scoped to code_writer — 8 agents "
            "share get_filesystem_tools and none of the others draft code"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
