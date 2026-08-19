"""Guard tests the e2e critique flagged as missing: the filesystem deny-list,
seed_from_image branches, and the tool wrappers."""
from __future__ import annotations

import os
import sys
import types
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.skills_store as store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDenyList:
    """filesystem_tools must refuse writes under .opencode/skills (resolved path)."""

    def _fs_tools(self):
        from agents.tools.filesystem_tools import get_filesystem_tools

        return {t.name: t for t in get_filesystem_tools(project_root=ROOT)}

    def test_write_file_denied_on_skills(self):
        tools = self._fs_tools()
        r = tools["write_file"].invoke(
            {"path": ".opencode/skills/navigation-patterns/SKILL.md", "content": "x"})
        assert "disabled" in r and "learn_skill" in r

    def test_edit_file_denied_on_skills(self):
        tools = self._fs_tools()
        r = tools["edit_file"].invoke(
            {"path": ".opencode/skills/navigation-patterns/SKILL.md",
             "old_string": "a", "new_string": "b"})
        assert "disabled" in r

    def test_backout_traversal_still_denied(self):
        tools = self._fs_tools()
        r = tools["write_file"].invoke(
            {"path": "workspace/../.opencode/skills/x/SKILL.md", "content": "x"})
        assert "disabled" in r
        r2 = tools["write_file"].invoke(
            {"path": ".//.opencode/skills/x/SKILL.md", "content": "x"})
        assert "disabled" in r2

    def test_normal_write_still_works(self):
        tools = self._fs_tools()
        r = tools["write_file"].invoke(
            {"path": "workspace/_deny_test/tmp.txt", "content": "ok"})
        assert "Successfully wrote" in r
        # cleanup
        try:
            os.remove(os.path.join(ROOT, "workspace", "_deny_test", "tmp.txt"))
            os.rmdir(os.path.join(ROOT, "workspace", "_deny_test"))
        except OSError:
            pass


class TestSeedBranches:
    """seed_from_image: first-boot, byte-equal, drifted."""

    def _patch(self, fm: dict, image: dict):
        art = types.ModuleType("src.artifacts")
        def _read(k):
            if k in fm:
                return fm[k]
            raise FileNotFoundError(k)
        art.read_text = mock.Mock(side_effect=_read)
        art.write_text = mock.Mock(side_effect=lambda k, v: fm.__setitem__(k, v))
        art.exists = mock.Mock(side_effect=lambda k: k in fm)
        art.list_keys = mock.Mock(return_value=[])
        img = types.SimpleNamespace(glob=lambda pat: image.get(pat, []))
        return art, img

    def test_seed_all_branches(self):
        # build a fake image dir listing
        seeded_bytes = "BASELINE"
        drifted_bytes = "BASELINE\n\n## Learned: X\n**Source:** s\n"
        class FakeSkill:
            def __init__(self, name, text):
                self._t = text
                self.parent = types.SimpleNamespace(name=name)
            def read_text(self, encoding="utf-8"):
                return self._t
            def __lt__(self, other):
                return self.parent.name < other.parent.name
        fm_state = {
            "skills/equal/SKILL.md": seeded_bytes,
            "skills/equal/.seed.yaml": '{"sha256": "%s"}' % store._sha256_text(seeded_bytes),
            "skills/drifted/SKILL.md": drifted_bytes,
            "skills/drifted/.seed.yaml": '{"sha256": "%s"}' % store._sha256_text("OLD"),
            # 'fresh' intentionally absent
        }
        writes = {}
        art = types.ModuleType("src.artifacts")
        def _read(k):
            if k in fm_state:
                return fm_state[k]
            raise FileNotFoundError(k)
        art.read_text = mock.Mock(side_effect=_read)
        art.write_text = mock.Mock(side_effect=lambda k, v: writes.__setitem__(k, v))
        art.exists = mock.Mock(side_effect=lambda k: k in fm_state)
        art.list_keys = mock.Mock(return_value=[])
        with mock.patch.dict(sys.modules, {"src.artifacts": art}), \
             mock.patch.object(store, "_image_skills_dir") as d:
            d.return_value = types.SimpleNamespace(
                is_dir=lambda: True,
                glob=lambda pat: [FakeSkill("fresh", seeded_bytes),
                                  FakeSkill("equal", seeded_bytes),
                                  FakeSkill("drifted", seeded_bytes)],
            )
            stats = store.seed_from_image(git_sha="t1")
        assert "fresh" in stats["seeded"]
        assert "equal" in stats["refreshed"]
        assert "drifted" in stats["kept_learned"]
        # drifted content NOT overwritten
        assert fm_state["skills/drifted/SKILL.md"] == drifted_bytes


class TestToolWrappers:
    def test_learn_skill_tool_name_and_refusal(self):
        from agents.tools.skill_tools import get_skill_write_tools

        tools = {t.name: t for t in get_skill_write_tools()}
        assert set(tools) == {"learn_skill", "create_new_skill"}
        r = tools["learn_skill"].invoke(
            {"skill_name": "../evil", "title": "t", "source": "s",
             "applicability": "a", "body": "b"})
        assert "failed" in r and "invalid skill name" in r


class TestReplaceLearnedSection:
    BASE = "---\nname: nav\n---\n\nBaseline.\n"

    def test_replace_keeps_concurrent_other_section(self):
        state = {
            "skills/nav/SKILL.md": self.BASE + "\n## Learned: First\n**Source:** a\n\nA\n",
            "skills/_audit.jsonl": "",
        }
        art = types.ModuleType("src.artifacts")
        def _read(k):
            if k in state:
                return state[k]
            raise FileNotFoundError(k)
        art.read_text = mock.Mock(side_effect=_read)
        art.write_text = mock.Mock(side_effect=lambda k, v: state.__setitem__(k, v))
        art.exists = mock.Mock(return_value=True)
        art.list_keys = mock.Mock(return_value=["skills/nav/SKILL.md"])
        with mock.patch.dict(sys.modules, {"src.artifacts": art}):
            store._invalidate_snapshot()
            r = store.replace_learned_section("nav", "First", "## Learned: First\n**Source:** a\n\nEDITED\n")
        assert r["ok"]
        txt = state["skills/nav/SKILL.md"]
        assert "EDITED" in txt
        assert txt.startswith("---")  # baseline intact

    def test_replace_missing_title_fails(self):
        state = {"skills/nav/SKILL.md": self.BASE, "skills/_audit.jsonl": ""}
        art = types.ModuleType("src.artifacts")
        def _read(k):
            if k in state:
                return state[k]
            raise FileNotFoundError(k)
        art.read_text = mock.Mock(side_effect=_read)
        art.write_text = mock.Mock(side_effect=lambda k, v: state.__setitem__(k, v))
        art.exists = mock.Mock(return_value=True)
        art.list_keys = mock.Mock(return_value=[])
        with mock.patch.dict(sys.modules, {"src.artifacts": art}):
            store._invalidate_snapshot()
            r = store.replace_learned_section("nav", "Nope", "x")
        assert not r["ok"]
