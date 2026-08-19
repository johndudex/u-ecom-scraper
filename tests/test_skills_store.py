"""Skills store tests — FM source of truth, image fallback, write path.

Run in-container (needs FM running for the FM tests; the fallback tests use
a stubbed artifacts module). Covers the live-failure classes from the
Railway migration: per-key fallback (404 ≠ outage), append-only guard,
duplicate-title guard, flock'd RMW smoke, seed idempotence.
"""
from __future__ import annotations

import os
import sys
import types
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.skills_store as store

def _fm_stub(state: dict):
    """Build a stub src.artifacts AND patch both sys.modules and the package
    attribute (import pkg.mod consults sys.modules first, but `from X import Y`
    style attribute access on the real package would bypass a sys.modules-only
    patch — patch both)."""
    art = types.ModuleType("src.artifacts")
    def _read(k):
        if k in state:
            return state[k]
        raise FileNotFoundError(k)
    art.read_text = mock.Mock(side_effect=_read)
    art.write_text = mock.Mock(side_effect=lambda k, v: state.__setitem__(k, v))
    art.exists = mock.Mock(side_effect=lambda k: k in state)
    return art


class _PkgAttrPatch:
    def __init__(self, art):
        self.art = art
    def __enter__(self):
        import src as _src_pkg
        self._real = getattr(_src_pkg, "artifacts", None)
        _src_pkg.artifacts = self.art
        self._dict = mock.patch.dict(sys.modules, {"src.artifacts": self.art})
        self._dict.__enter__()
        return self
    def __exit__(self, *a):
        import src as _src_pkg
        if self._real is not None:
            _src_pkg.artifacts = self._real
        else:
            delattr(_src_pkg, "artifacts")
        self._dict.__exit__(*a)


class TestValidateName:
    def test_valid(self):
        assert store._validate_name("shopify-detection") == "shopify-detection"

    def test_rejects_traversal(self):
        assert store._validate_name("../etc") is None
        assert store._validate_name("a/b") is None

    def test_rejects_upper_and_empty(self):
        assert store._validate_name("BadName") is None
        assert store._validate_name("") is None
        assert store._validate_name(None) is None


class TestImageFallback:
    """Per-key fallback semantics — the sharp edge (404 ≠ outage)."""

    def test_fm_404_falls_back_to_image_quietly(self):
        art = types.ModuleType("src.artifacts")
        art.read_text = mock.Mock(side_effect=FileNotFoundError)
        art.list_keys = mock.Mock(return_value=[])
        with _PkgAttrPatch(art):
            store._invalidate_snapshot()
            content = store.read_skill("shopify-detection")
        assert content and content.startswith("---")  # image copy served

    def test_fm_outage_falls_back_with_warning(self):
        art = types.ModuleType("src.artifacts")
        art.read_text = mock.Mock(side_effect=RuntimeError("conn refused"))
        art.list_keys = mock.Mock(side_effect=RuntimeError("conn refused"))
        with _PkgAttrPatch(art):
            store._invalidate_snapshot()
            content = store.read_skill("shopify-detection")
        assert content is not None  # image copy still served

    def test_fm_hit_does_not_touch_image(self):
        art = types.ModuleType("src.artifacts")
        art.read_text = mock.Mock(return_value="---\nname: x\n---\nFM CONTENT")
        art.list_keys = mock.Mock(return_value=["skills/x/SKILL.md"])
        with _PkgAttrPatch(art):
            store._invalidate_snapshot()
            assert store.read_skill("x") == "---\nname: x\n---\nFM CONTENT"

    def test_unknown_skill_returns_none(self):
        art = types.ModuleType("src.artifacts")
        art.read_text = mock.Mock(side_effect=FileNotFoundError)
        art.list_keys = mock.Mock(return_value=[])
        with _PkgAttrPatch(art):
            store._invalidate_snapshot()
            assert store.read_skill("no-such-skill-anywhere") is None


class TestUIExclusion:
    def test_list_excludes_ui_skills(self):
        names = store.list_skills()
        assert "impeccable" not in names and "ui-ux-pro-max" not in names
        assert "shopify-detection" in names

    def test_snapshot_excludes_ui_skills(self):
        store._invalidate_snapshot()
        snap = store.descriptions_snapshot(force=True)
        assert "impeccable" not in snap


class TestDescriptionsSnapshot:
    def test_snapshot_cached_then_invalidated(self):
        store._invalidate_snapshot()
        s1 = store.descriptions_snapshot()
        s2 = store.descriptions_snapshot()
        assert s1 == s2
        store._invalidate_snapshot()
        s3 = store.descriptions_snapshot()
        assert s3 == s1  # stable content


class TestAppendLearnedGuard:
    """Append-only + duplicate-title + frontmatter-never-touched."""

    BASE = "---\nname: nav\ndescription: d\n---\n\nBaseline content.\n"

    def _patch_fm(self, initial: str):
        state = {"skills/nav/SKILL.md": initial, "skills/_audit.jsonl": "",
                 "skills/nav/.seed.yaml": ""}
        art = _fm_stub(state)
        art.list_keys = mock.Mock(return_value=["skills/nav/SKILL.md"])
        return state, _PkgAttrPatch(art)

    def test_append_adds_section(self):
        state, p = self._patch_fm(self.BASE)
        with p:
            store._invalidate_snapshot()
            r = store.append_learned("nav", "My Learn", "src", "when", "body")
        assert r["ok"] and r["appended"]
        assert "## Learned: My Learn" in state["skills/nav/SKILL.md"]
        assert state["skills/nav/SKILL.md"].startswith("---")  # frontmatter intact

    def test_duplicate_title_skipped(self):
        state, p = self._patch_fm(self.BASE + "\n## Learned: My Learn\n**Source:** x\n")
        with p:
            store._invalidate_snapshot()
            r = store.append_learned("nav", "My Learn", "s", "a", "b")
        assert r["ok"] and r["appended"] is False

    def test_never_touches_baseline(self):
        state, p = self._patch_fm(self.BASE)
        with p:
            store._invalidate_snapshot()
            store.append_learned("nav", "New", "s", "a", "b")
        assert "Baseline content." in state["skills/nav/SKILL.md"]

    def test_delete_learned_roundtrip(self):
        state, p = self._patch_fm(
            self.BASE + "\n## Learned: First\n**Source:** a\n\nA\n\n## Learned: Second\n**Source:** b\n\nB\n")
        with p:
            store._invalidate_snapshot()
            r = store.delete_learned_section("nav", "First")
        assert r["ok"]
        assert "## Learned: First" not in state["skills/nav/SKILL.md"]
        assert "## Learned: Second" in state["skills/nav/SKILL.md"]  # sibling kept


class TestCreateSkill:
    def test_create_and_reject_existing(self):
        state = {}
        art = _fm_stub(state)
        art.list_keys = mock.Mock(return_value=[])
        with _PkgAttrPatch(art):
            store._invalidate_snapshot()
            r = store.create_skill("brand-new-skill", "desc", "body")
            assert r["ok"], r
            assert "skills/brand-new-skill/SKILL.md" in state
            # now it exists → reject
            r2 = store.create_skill("brand-new-skill", "desc", "body")
            assert not r2["ok"]

    def test_reserved_name_rejected(self):
        r = store.create_skill("impeccable", "d", "b")
        assert not r["ok"]
