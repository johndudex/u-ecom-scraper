"""[wave-19 T1.8] Scope the agent's shell tool to its own workspace
(323/D2-twin).

Job 323's tester ran free-form bash against the PROJECT ROOT and globbed 7
OTHER sites' workspaces — cross-contaminating its analysis with foreign
artifacts (the D2 twin: product_analyzer timeout left a hole, and the tester
happily filled it from `workspace/nastygal/…`). The filesystem tools already
honor ``workspace_scope``; ``run_bash`` — the widest read path — did not.

Contract:
- a pure classifier returns the path tokens that reach OTHER slugs'
  ``workspace/`` directories (other slug, ``workspace/*`` glob, ``..``);
- the job's OWN workspace is always readable;
- with ``workspace_scope`` set, ``run_bash`` refuses cross-workspace commands
  with a directive instead of executing them;
- no scope set → behavior unchanged (ops/admin callers).
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

from agents.tools.shell_tools import (  # noqa: E402
    cross_workspace_paths,
    get_shell_tools,
)

SLUG = "theiconic-com-au"


class TestCrossWorkspaceClassifier:
    def test_other_slug_flagged(self):
        bad = cross_workspace_paths("grep -r title workspace/nastygal-com/", SLUG)
        assert bad == ["nastygal-com"]

    def test_absolute_other_workspace_flagged(self):
        bad = cross_workspace_paths("cat /app/workspace/crocs-com-au/output.json", SLUG)
        assert bad == ["crocs-com-au"]

    def test_own_workspace_clean(self):
        assert cross_workspace_paths(f"ls workspace/{SLUG}/", SLUG) == []
        assert cross_workspace_paths(f"cat workspace/{SLUG}/site_analysis.json", SLUG) == []

    def test_workspace_glob_flagged(self):
        bad = cross_workspace_paths("grep -rl scraper_draft workspace/*/", SLUG)
        assert bad, "workspace/* must be flagged (it touches unknown slugs)"

    def test_dotdot_escape_flagged(self):
        bad = cross_workspace_paths("cat workspace/../secrets.txt", SLUG)
        assert bad == [".."]

    def test_no_workspace_reference_clean(self):
        assert cross_workspace_paths("python -c 'print(1)'", SLUG) == []
        assert cross_workspace_paths("ls templates/", SLUG) == []

    def test_no_scope_means_nothing_flagged(self):
        assert cross_workspace_paths("grep -r x workspace/nastygal-com/", "") == []


class TestRunBashScoping:
    def _tools(self, tmp_path, scope):
        tools = get_shell_tools(project_root=str(tmp_path), workspace_scope=scope)
        by_name = {t.name: t for t in tools}
        return by_name["run_bash"]

    def test_cross_workspace_command_refused(self, tmp_path):
        (tmp_path / "workspace" / "nastygal-com").mkdir(parents=True)
        (tmp_path / "workspace" / SLUG).mkdir(parents=True)
        bash = self._tools(tmp_path, SLUG)
        out = bash.invoke({"command": "grep -r title workspace/nastygal-com/"})
        assert "own workspace" in out or "workspace/" in out
        assert "title-not-there" not in out

    def test_own_workspace_command_runs(self, tmp_path):
        ws = tmp_path / "workspace" / SLUG
        ws.mkdir(parents=True)
        (ws / "site_analysis.json").write_text('{"platform": "sfcc"}')
        bash = self._tools(tmp_path, SLUG)
        out = bash.invoke({"command": f"cat workspace/{SLUG}/site_analysis.json"})
        assert "sfcc" in out

    def test_no_scope_keeps_root_access(self, tmp_path):
        (tmp_path / "workspace" / "nastygal-com").mkdir(parents=True)
        (tmp_path / "workspace" / "nastygal-com" / "x.txt").write_text("legacy")
        bash = self._tools(tmp_path, "")
        out = bash.invoke({"command": "cat workspace/nastygal-com/x.txt"})
        assert "legacy" in out


class TestFactoryWiring:
    def test_subagents_passes_workspace_scope_to_shell_factory(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "subagents.py")).read()
        assert "workspace_scope=workspace_scope or None" in src
        # and it reaches get_shell_tools, not just the filesystem factory
        i_gst = src.index("get_shell_tools as _gst")
        i_pass = src.index("workspace_scope=workspace_scope or None", i_gst)
        assert i_pass > i_gst




class TestFindingsMergeSettingsImport:
    """Incidental F821 found during wave-19: subagents.py:3327 referenced
    `settings.PROJECT_ROOT` with NO import — NameError, swallowed by the
    bare `except: pass`, so the navigation_findings→nav_analysis URL merge
    (the D2 load-bearing path when product_analyzer times out) silently
    never ran."""

    def test_settings_is_imported_at_the_merge_site(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "subagents.py")).read()
        i_use = src.index("settings.PROJECT_ROOT, \"workspace\", _slug")
        # (needle updated 2026-09-05: ruff I001 split the compound
        # `import json as _json_nf, os as _os_nf` into two lines — the
        # boundary anchor is the last of them)
        i_block = src.rindex("import os as _os_nf", 0, i_use)
        block = src[i_block:i_use]
        assert "from django.conf import settings" in block, (
            "the findings-merge block must import settings before using it"
        )

if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
