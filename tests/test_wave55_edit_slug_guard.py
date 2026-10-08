"""[wave-55 C1 residual] edit_file must be scoped like write_file.

Rounds 1-18 wired ``_enforce_slug_scope`` into ``write_file`` only —
``edit_file`` still ran just ``_enforce_not_skills``. An edit cannot MINT a
file, but with a workspace scope set it can MUTATE any existing file under
the project root, including ``src/*.py`` — the same shadow-tree surface the
write guard closed (a writer that edits the pipeline's own discovery module
poisons every later draft on the volume, and the edit is invisible to the
template re-anchoring because no new file appears).

Contract (mirrors tests/test_wave55_write_slug_guard.py):
- with a workspace scope (the job's slug), edits outside
  ``workspace/{slug}/`` are refused with the same directive text;
- the job's own slug dir — including ``workspace/{slug}/_vendor/`` — still
  edits, and a refused edit leaves the bytes on disk untouched;
- ``..`` and absolute-path tricks that land outside the slug are refused;
- no scope set (ops/admin surfaces) → behavior unchanged;
- edit_file still cannot create a file (the slug guard adds no mint path).

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest ../tests/test_wave55_edit_slug_guard.py -q"
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

_EDIT = {"old_string": "VALUE = 1\n", "new_string": "VALUE = 2\n"}


def _tools(root, scope):
    return {
        t.name: t
        for t in get_filesystem_tools(project_root=str(root), workspace_scope=scope)
    }


def _seed(path, content="VALUE = 1\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return content


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "workspace" / SLUG).mkdir(parents=True)
    (tmp_path / "workspace" / OTHER).mkdir(parents=True)
    return tmp_path


class TestScopedEditRefusals:
    def test_pipeline_src_is_refused(self, proj):
        """The audit shape: an existing file under src/ must not be editable
        by a scoped writer (the write guard refuses minting it; the edit
        guard must refuse mutating it)."""
        target = proj / "src" / "discovery.py"
        _seed(target)
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke({"path": "src/discovery.py", **_EDIT})
        assert "REJECTED" in out
        assert target.read_text() == "VALUE = 1\n", (
            "a refused edit must leave the file untouched"
        )

    def test_other_jobs_slug_is_refused(self, proj):
        target = proj / "workspace" / OTHER / "scraper_draft.py"
        _seed(target)
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke({"path": f"workspace/{OTHER}/scraper_draft.py", **_EDIT})
        assert "REJECTED" in out
        assert target.read_text() == "VALUE = 1\n"

    def test_dotdot_escape_is_refused(self, proj):
        target = proj / "workspace" / "src" / "discovery.py"
        _seed(target)
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke(
            {"path": f"workspace/{SLUG}/../src/discovery.py", **_EDIT}
        )
        assert "REJECTED" in out
        assert target.read_text() == "VALUE = 1\n"

    def test_absolute_path_outside_slug_is_refused(self, proj):
        target = proj / "workspace" / OTHER / "scraper_draft.py"
        _seed(target)
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke(
            {"path": str(target), **_EDIT}
        )
        assert "REJECTED" in out
        assert target.read_text() == "VALUE = 1\n"

    def test_rejection_names_the_sanctioned_home(self, proj):
        """The rejection text is the one place the model is guaranteed to
        read (wave-25 W25-d) — same directive as write_file's."""
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke({"path": "src/discovery.py", **_EDIT})
        assert f"workspace/{SLUG}/" in out
        assert "_vendor" in out

    def test_skills_guard_still_runs_first(self, proj):
        """The .opencode/skills write ban is not relaxed by the slug scope."""
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke({"path": ".opencode/skills/shopify-detection/SKILL.md", **_EDIT})
        assert "disabled" in out


class TestScopedEditAllowsOwnSlug:
    def test_own_slug_dir_edit_succeeds(self, proj):
        target = proj / "workspace" / SLUG / "scraper_draft.py"
        _seed(target)
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke({"path": f"workspace/{SLUG}/scraper_draft.py", **_EDIT})
        assert "Successfully replaced" in out
        assert target.read_text() == "VALUE = 2\n"

    def test_own_slug_vendor_edit_succeeds(self, proj):
        """``workspace/{slug}/_vendor/`` is the sanctioned home for site-local
        helper modules — inside the slug dir, so it must keep working."""
        target = proj / "workspace" / SLUG / "_vendor" / "http_fetch.py"
        _seed(target)
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke({"path": f"workspace/{SLUG}/_vendor/http_fetch.py", **_EDIT})
        assert "Successfully replaced" in out
        assert target.read_text() == "VALUE = 2\n"

    def test_edit_still_cannot_mint_a_file(self, proj):
        """The slug guard widens no mint path: a missing file inside the slug
        is still 'File not found' (write_file is the creator)."""
        ef = _tools(proj, SLUG)["edit_file"]
        out = ef.invoke(
            {"path": f"workspace/{SLUG}/absent.py", **_EDIT}
        )
        assert "File not found" in out
        assert not (proj / "workspace" / SLUG / "absent.py").exists()


class TestNoScopeUnchanged:
    def test_without_scope_the_project_root_still_bounds(self, tmp_path):
        """Callers that pass no scope (ops/admin surfaces) behave as before:
        any existing file under the project root edits."""
        target = tmp_path / "src" / "discovery.py"
        _seed(target)
        tools = {
            t.name: t
            for t in get_filesystem_tools(project_root=str(tmp_path))
        }
        out = tools["edit_file"].invoke({"path": "src/discovery.py", **_EDIT})
        assert "Successfully replaced" in out
        assert target.read_text() == "VALUE = 2\n"
