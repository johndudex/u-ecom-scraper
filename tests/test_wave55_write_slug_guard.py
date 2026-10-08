"""[wave-55 C1] write_file must be scoped to the running job's own workspace.

Prod evidence (audit 2026-10-05, C1): job 1156's code_writer issued four
``write_file`` calls into ``workspace/src/`` — a directory NO pipeline node
creates — minting a site-local, proxy-free clone of the pipeline's ``src``
package. Every later draft's template ``sys.path`` insert put ``workspace/``
at ``sys.path[0]``, so that shadow won every ``import src.X`` on the volume:
30/79 jobs in the 4.5-day window crashed at import, and writers that merely
READ the shadow built drafts against the wrong API surface. ``_enforce_root``
only bounds the project root, so nothing stopped a writer from minting a
sibling directory.

Contract:
- with a workspace scope (the job's slug), writes outside
  ``workspace/{slug}/`` are refused with a directive;
- the job's own slug dir — including ``workspace/{slug}/_vendor/``, the
  sanctioned home for site-local helper modules — still writes;
- ``..`` and absolute-path tricks that land outside the slug are refused;
- no scope set (ops/admin surfaces) → behavior unchanged.
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

from webapp.agents.tools.filesystem_tools import get_filesystem_tools  # noqa: E402

SLUG = "underarmour-com"
OTHER = "dollartree-com"


def _tools(root, scope):
    return {
        t.name: t
        for t in get_filesystem_tools(project_root=str(root), workspace_scope=scope)
    }


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "workspace" / SLUG).mkdir(parents=True)
    (tmp_path / "workspace" / OTHER).mkdir(parents=True)
    return tmp_path


class TestScopedWriteRefusals:
    def test_workspace_src_is_refused(self, proj):
        """The exact prod shape: write_file('workspace/src/http_fetch.py')."""
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke({"path": "workspace/src/http_fetch.py", "content": "x = 1\n"})
        assert "REJECTED" in out
        assert not (proj / "workspace" / "src").exists(), (
            "a refused write must not create the directory"
        )

    def test_other_jobs_slug_is_refused(self, proj):
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke(
            {"path": f"workspace/{OTHER}/site_analysis.json", "content": "{}"}
        )
        assert "REJECTED" in out
        assert not (proj / "workspace" / OTHER / "site_analysis.json").exists()

    def test_dotdot_escape_is_refused(self, proj):
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke(
            {"path": f"workspace/{SLUG}/../src/http_fetch.py", "content": "x = 1\n"}
        )
        assert "REJECTED" in out
        assert not (proj / "workspace" / "src").exists()

    def test_absolute_path_outside_slug_is_refused(self, proj):
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke(
            {
                "path": str(proj / "workspace" / OTHER / "scraper_draft.py"),
                "content": "x = 1\n",
            }
        )
        assert "REJECTED" in out

    def test_workspace_root_file_is_refused(self, proj):
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke({"path": "workspace/probe_scratch.py", "content": "x = 1\n"})
        assert "REJECTED" in out

    def test_rejection_names_the_sanctioned_home(self, proj):
        """The rejection text is the one place the model is guaranteed to
        read (wave-25 W25-d) — it must carry the redirect, not just a no."""
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke({"path": "workspace/src/http_fetch.py", "content": "x = 1\n"})
        assert f"workspace/{SLUG}/" in out
        assert "_vendor" in out
        assert "workspace/src" in out

    def test_workspace_relative_without_slug_is_refused(self, proj):
        """A bare ``workspace/site_analysis.json`` (search_files already
        defaults there) is still OUTSIDE the job's slug dir — refused."""
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke({"path": "workspace/site_analysis.json", "content": "{}"})
        assert "REJECTED" in out


class TestScopedWriteAllowsOwnSlug:
    def test_own_slug_dir_write_succeeds(self, proj):
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke(
            {"path": f"workspace/{SLUG}/site_analysis.json", "content": '{"a": 1}'}
        )
        assert "Successfully wrote" in out
        assert (proj / "workspace" / SLUG / "site_analysis.json").exists()

    def test_own_slug_vendor_write_succeeds(self, proj):
        """``workspace/{slug}/_vendor/`` is the sanctioned home for site-local
        helper modules — inside the slug dir, so it must keep working."""
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke(
            {
                "path": f"workspace/{SLUG}/_vendor/http_fetch.py",
                "content": "def create_fetch_page():\n    return None\n",
            }
        )
        assert "Successfully wrote" in out
        assert (proj / "workspace" / SLUG / "_vendor" / "http_fetch.py").exists()

    def test_own_slug_nested_dir_is_created(self, proj):
        wf = _tools(proj, SLUG)["write_file"]
        out = wf.invoke(
            {"path": f"workspace/{SLUG}/analysis/deep/report.json", "content": "{}"}
        )
        assert "Successfully wrote" in out


class TestNoScopeUnchanged:
    def test_without_scope_the_project_root_still_bounds(self, tmp_path):
        """Callers that pass no scope (ops/admin surfaces) behave as before:
        any path under the project root writes."""
        tools = {
            t.name: t
            for t in get_filesystem_tools(project_root=str(tmp_path))
        }
        out = tools["write_file"].invoke(
            {"path": "workspace/src/http_fetch.py", "content": "x = 1\n"}
        )
        assert "Successfully wrote" in out
        assert (tmp_path / "workspace" / "src" / "http_fetch.py").exists()
