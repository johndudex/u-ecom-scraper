"""[wave-55 C1] the local tester lane must pin ``src`` to the project root.

Prod evidence (audit 2026-10-05, C1): ``run_scraper``'s LOCAL branch
(``http_requests`` / ``internal_api`` drafts) inherited raw ``os.environ`` —
no ``PYTHONPATH``, no ``PYTHONSAFEPATH``. The browser lane staged both
(browser_service/scraper_runner.py, the job-317 lesson), so the two lanes
resolved ``import src.X`` differently for the SAME draft: the local run died
on the workspace shadow while the browser run passed, and 1215 delivered from
the writer's side run after 3 tester crashes.

Contract: the env dict handed to the local subprocess mirrors the browser
lane exactly — ``PYTHONSAFEPATH=1`` (kills the script-dir auto-prepend) plus
``PYTHONPATH`` = PROJECT_ROOT, then the run dir, then any inherited entries.
``env_overrides`` still win last, as they do on the browser lane.
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

from agents.tools.shell_tools import get_shell_tools  # noqa: E402

SLUG = "underarmour-com"


class _FakeResult:
    returncode = 0
    stdout = "ok\n"
    stderr = ""


@pytest.fixture()
def local_draft(tmp_path):
    slug_dir = tmp_path / "workspace" / SLUG
    slug_dir.mkdir(parents=True)
    draft = slug_dir / "scraper_draft.py"
    # No browser imports → run_scraper takes the LOCAL branch.
    draft.write_text("print('hi')\n")
    return draft


def _capture_local_run(monkeypatch):
    import agents.tools.shell_tools as shell_tools_mod

    captured = {}

    def _fake_run(cmd, capture_output, text, timeout, cwd, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeResult()

    monkeypatch.setattr(shell_tools_mod.subprocess, "run", _fake_run)
    return captured


def _invoke(draft, tmp_path):
    tools = {
        t.name: t
        for t in get_shell_tools(project_root=str(tmp_path), workspace_scope=SLUG)
    }
    out = tools["run_scraper"].invoke({"scraper_path": str(draft)})
    assert "ok" in out, out
    return out


class TestLocalLaneEnv:
    def test_safepath_is_set(self, tmp_path, local_draft, monkeypatch):
        captured = _capture_local_run(monkeypatch)
        _invoke(local_draft, tmp_path)
        assert captured["env"]["PYTHONSAFEPATH"] == "1"

    def test_project_root_leads_pythonpath(self, tmp_path, local_draft, monkeypatch):
        monkeypatch.delenv("PYTHONPATH", raising=False)
        captured = _capture_local_run(monkeypatch)
        _invoke(local_draft, tmp_path)
        parts = captured["env"]["PYTHONPATH"].split(os.pathsep)
        assert parts[0] == str(tmp_path), (
            "PROJECT_ROOT must be first — /app/src must outrank anything in "
            "the run dir (the workspace/src shadow class)"
        )
        assert parts[1] == str(local_draft.parent)

    def test_inherited_pythonpath_rides_along(self, tmp_path, local_draft, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "/pre/existing")
        captured = _capture_local_run(monkeypatch)
        _invoke(local_draft, tmp_path)
        parts = captured["env"]["PYTHONPATH"].split(os.pathsep)
        assert parts[:2] == [str(tmp_path), str(local_draft.parent)]
        assert "/pre/existing" in parts[2:], "inherited entries must survive"

    def test_project_root_outranks_a_hostile_run_dir_entry(
        self, tmp_path, local_draft, monkeypatch
    ):
        """The actual failure mode: a ``workspace/src`` shadow next to the
        draft must lose to the pipeline package."""
        monkeypatch.delenv("PYTHONPATH", raising=False)
        shadow = local_draft.parent / "src"
        shadow.mkdir()
        (shadow / "http_fetch.py").write_text("# site-local clone\n")
        captured = _capture_local_run(monkeypatch)
        _invoke(local_draft, tmp_path)
        parts = captured["env"]["PYTHONPATH"].split(os.pathsep)
        assert parts.index(str(tmp_path)) < parts.index(str(local_draft.parent))

    def test_env_overrides_still_win(self, tmp_path, local_draft, monkeypatch):
        """env_overrides are computed by the tool and applied last — the same
        precedence the browser lane keeps."""
        monkeypatch.setenv("PYTHONPATH", "/pre/existing")
        captured = _capture_local_run(monkeypatch)
        _invoke(local_draft, tmp_path)
        env = captured["env"]
        assert env["PYTHONSAFEPATH"] == "1"
        assert env["PYTHONPATH"].startswith(str(tmp_path))
