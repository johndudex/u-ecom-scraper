"""[wave-43] Skill-seeding executor (docs/plans/wave43-skill-seeding-plan.md §3).

The seeder writes the 13 approved seed bodies into the File Master skill
files through the SAME append path the agents use (src.skills_store). Tests
run against the real seed files plus an in-memory FM stand-in (only the two
FM primitives are patched — the real append/create/render logic runs).
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SEEDS_DIR = _REPO / "_harvest" / "seeds"
_TOOL = _REPO / "_harvest" / "tools" / "seed_skills.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("wave43_seed_skills", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The plan §3.5 mapping — seed id → target skill, in execution order.
PLAN_TARGETS = {
    "s01": "sfcc-detection",
    "s02": "shopify-detection",
    "s03": "amazon-detection",
    "s14": "kibo-detection",
    "s05": "navigation-patterns",
    "s06": "jsonld-extraction",
    "s07": "anti-bot-handling",
    "s08": "proxy-config",
    "s09": "playwright-navigation",
    "s10": "akamai-detection",
    "s11": "magento-detection",
    "s12": "output-schema-integrity",
    "s15": "netsuite-detection",
}
PLAN_TIERS = {sid: 1 for sid in ("s01", "s02", "s03", "s14")}
PLAN_TIERS.update({sid: 2 for sid in ("s05", "s06", "s07", "s08", "s09", "s10")})
PLAN_TIERS.update({sid: 3 for sid in ("s11", "s12", "s15")})


# ─── parsing the seed files ───────────────────────────────────────────────────


class TestSeedParsing:
    def test_s01_parses_target_title_body(self):
        mod = _load_tool()
        p = mod.parse_seed("s01", seeds_dir=str(_SEEDS_DIR))
        assert p["target"] == "sfcc-detection"
        assert p["title"] == "SFCC runtime sub-patterns (3 shapes + Mobify island)"
        assert p["body"].startswith("SFCC ships THREE shapes")
        assert 0 < len(p["body"]) <= 1200
        assert p["applicability"].startswith("Any Salesforce")

    def test_all_active_seeds_parse_from_real_files(self):
        mod = _load_tool()
        for sid, target in PLAN_TARGETS.items():
            p = mod.parse_seed(sid, seeds_dir=str(_SEEDS_DIR))
            assert p["target"] == target, sid
            assert p["title"], sid
            assert p["source"], sid
            assert p["applicability"], sid
            assert 0 < len(p["body"]) <= 1200, (sid, len(p["body"]))
            assert "## Learned:" not in p["body"], sid
            assert "## Honest gaps" not in p["body"], sid

    def test_no_two_active_seeds_share_target(self):
        targets = list(PLAN_TARGETS.values())
        assert len(targets) == len(set(targets))

    def test_tool_target_map_matches_plan(self):
        mod = _load_tool()
        assert mod.ACTIVE_SEEDS == PLAN_TARGETS
        assert mod.TIERS == PLAN_TIERS

    def test_dropped_seeds_not_active(self):
        mod = _load_tool()
        assert "s04" not in mod.ACTIVE_SEEDS
        assert "s13" not in mod.ACTIVE_SEEDS

    def test_tier3_baselines_carry_distilled_lessons(self):
        mod = _load_tool()
        p11 = mod.parse_seed("s11", seeds_dir=str(_SEEDS_DIR))
        assert "data-ui-id" in p11["baseline"]
        p12 = mod.parse_seed("s12", seeds_dir=str(_SEEDS_DIR))
        assert p12["baseline"]
        p15 = mod.parse_seed("s15", seeds_dir=str(_SEEDS_DIR))
        assert "/api/items" in p15["baseline"]

    def test_tier3_new_skills_have_descriptions(self):
        mod = _load_tool()
        for sid in ("s11", "s12", "s15"):
            desc = mod.NEW_SKILL_DESCRIPTIONS[sid]
            assert desc and "\n" not in desc


# ─── FM stand-in: real skills_store logic, fake storage ──────────────────────


class FakeFM:
    """In-memory stand-in for the two FM primitives skills_store writes use."""

    def __init__(self, initial: dict[str, str]):
        self.files: dict[str, str] = dict(initial)
        self.created: list[str] = []

    def install(self, monkeypatch):
        import src.skills_store as ss

        monkeypatch.setattr(ss, "_fm_read_or_fail", self._read)
        monkeypatch.setattr(ss, "_fm_write", self._write)
        # Hermetic: kill the image-copy fallback — the real repo checkout
        # would satisfy skill_exists/_read_image_skill for tier-1 skills.
        monkeypatch.setattr(
            ss, "_image_skills_dir", lambda: Path("/nonexistent-wave43-image-skills")
        )
        import src.artifacts as artifacts

        monkeypatch.setattr(artifacts, "exists", lambda key: key in self.files)
        monkeypatch.setattr(artifacts, "write_text", self._key_write)
        monkeypatch.setattr(artifacts, "read_text", self._key_read)

    def _read(self, name):
        return self.files.get(f"skills/{name}/SKILL.md")

    def _write(self, name, text):
        key = f"skills/{name}/SKILL.md"
        if key not in self.files:
            self.created.append(name)  # first write to an absent key = create
        self.files[key] = text

    def _key_write(self, key, text, timeout=None):
        self.files[key] = text
        return len(text)

    def _key_read(self, key, timeout=None):
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def seed_image_skill(self, name, frontmatter_body: str):
        self.files[f"skills/{name}/SKILL.md"] = frontmatter_body


def _skill_text(name: str, learned_sections: list[str]) -> str:
    parts = [
        f"---\nname: {name}\ndescription: {name} baseline\n---\n\nBaseline for {name}."
    ]
    for i, sec in enumerate(learned_sections):
        parts.append(
            f"\n## Learned: {name} prior {i}\n**Source:** old\n"
            f"**Applicability:** old\n\n{sec}\n"
        )
    return "\n".join(parts)


EXISTING = {
    f"skills/{n}/SKILL.md": _skill_text(n, secs)
    for n, secs in {
        "sfcc-detection": ["x" * 2467],  # over cap — renders nothing today
        "shopify-detection": [],
        "amazon-detection": [],
        "kibo-detection": [],
        "navigation-patterns": ["y" * 5955],
        "jsonld-extraction": ["z" * 1429],
        "anti-bot-handling": ["w" * 1370, "v" * 2000],
        "proxy-config": [],
        "playwright-navigation": ["u" * 1934, "t" * 2723],
        "akamai-detection": [],
    }.items()
}


@pytest.fixture()
def fm(monkeypatch):
    fake = FakeFM(dict(EXISTING))
    fake.install(monkeypatch)
    return fake


# ─── render simulation ───────────────────────────────────────────────────────


class TestSimulateRender:
    def test_simulation_matches_real_append(self, fm, monkeypatch):
        """simulate(new_text) == render of the text after a REAL append_learned."""
        from src.skills_store import append_learned, render_learned_sections

        mod = _load_tool()
        p = mod.parse_seed("s08", seeds_dir=str(_SEEDS_DIR))
        before = fm._read("proxy-config")
        block = mod.build_block(p["title"], p["source"], p["applicability"], p["body"])
        predicted = mod.simulate_render(before, block)
        r = append_learned(
            "proxy-config", p["title"], p["source"], p["applicability"], p["body"],
            actor="test",
        )
        assert r["ok"] and r["appended"]
        actual = render_learned_sections("proxy-config", cap=1500)
        assert actual == predicted
        assert p["title"] in actual

    def test_sfcc_oversized_section_strict_improvement(self, fm):
        """sfcc's only section is over the cap → renders nothing today, the
        seed alone after. Encodes the plan §3.4 strict-improvement claim."""
        from src.skills_store import render_learned_sections

        mod = _load_tool()
        p = mod.parse_seed("s01", seeds_dir=str(_SEEDS_DIR))
        before = render_learned_sections(
            "sfcc-detection", _text=fm._read("sfcc-detection")
        )
        assert before == ""
        block = mod.build_block(p["title"], p["source"], p["applicability"], p["body"])
        after = mod.simulate_render(fm._read("sfcc-detection"), block)
        assert p["title"] in after

    def test_anti_bot_append_displaces_current_render(self, fm):
        """anti-bot's newest section (1370) renders today; the seed displaces
        it (the honest trade the plan §3.4 flags for Tier 2)."""
        from src.skills_store import render_learned_sections

        mod = _load_tool()
        current = fm._read("anti-bot-handling")
        today = render_learned_sections("anti-bot-handling", _text=current)
        assert today != ""
        p = mod.parse_seed("s07", seeds_dir=str(_SEEDS_DIR))
        block = mod.build_block(p["title"], p["source"], p["applicability"], p["body"])
        after = mod.simulate_render(current, block)
        assert p["title"] in after
        assert after != today


# ─── execution ───────────────────────────────────────────────────────────────


class TestSeedAll:
    def test_tier1_appends_all_four(self, fm):
        mod = _load_tool()
        rows = mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False,
                            actor="test")
        by_target = {r["target"]: r for r in rows}
        for sid in ("s01", "s02", "s03", "s14"):
            r = by_target[PLAN_TARGETS[sid]]
            assert r["ok"] and r["appended"], r
        text = fm._read("sfcc-detection")
        assert text.count("## Learned: SFCC runtime sub-patterns") == 1

    def test_idempotent_second_run_skips(self, fm):
        mod = _load_tool()
        mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False, actor="t1")
        n_before = fm._read("sfcc-detection").count("## Learned:")
        rows = mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False,
                            actor="t2")
        assert fm._read("sfcc-detection").count("## Learned:") == n_before
        by_target = {r["target"]: r for r in rows}
        r = by_target["sfcc-detection"]
        assert r["ok"] and not r["appended"] and "already" in r.get("note", "")

    def test_tier3_creates_then_appends_when_missing(self, fm):
        mod = _load_tool()
        rows = mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False,
                            actor="test")
        by_target = {r["target"]: r for r in rows}
        r = by_target["magento-detection"]
        assert r["ok"], r
        assert "magento-detection" in fm.created
        text = fm._read("magento-detection")
        assert text.startswith("---\nname: magento-detection")
        assert "## Learned:" in text
        assert "data-ui-id" in text  # baseline carries the distilled lessons

    def test_tier3_existing_skill_appends_without_create(self, fm):
        fm.seed_image_skill(
            "magento-detection",
            "---\nname: magento-detection\ndescription: partial\n---\n\nPartial body.",
        )
        mod = _load_tool()
        rows = mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False,
                            actor="test")
        by_target = {r["target"]: r for r in rows}
        r = by_target["magento-detection"]
        assert r["ok"], r
        assert "magento-detection" not in fm.created
        assert "## Learned:" in fm._read("magento-detection")

    def test_tier1_target_missing_is_error_not_create(self, fm, monkeypatch):
        del fm.files["skills/shopify-detection/SKILL.md"]
        mod = _load_tool()
        rows = mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False,
                            actor="test")
        by_target = {r["target"]: r for r in rows}
        r = by_target["shopify-detection"]
        assert not r["ok"] and "not found" in r["error"]
        assert "shopify-detection" not in fm.created
        # the other 12 still land
        assert sum(1 for r in rows if r["ok"]) == 12

    def test_dry_run_writes_nothing(self, fm):
        mod = _load_tool()
        snapshot = dict(fm.files)
        rows = mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=True,
                            actor="test")
        assert fm.files == snapshot
        by_target = {r["target"]: r for r in rows}
        r = by_target["sfcc-detection"]
        assert r["dry_run"] and "SFCC runtime sub-patterns" in r["after"]
        assert r["before"] == ""

    def test_all_thirteen_rows_reported(self, fm):
        mod = _load_tool()
        rows = mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False,
                            actor="test")
        assert len(rows) == 13
        assert {r["seed"] for r in rows} == set(PLAN_TARGETS)
        assert all(r.get("tier") == PLAN_TIERS[r["seed"]] for r in rows)

    def test_manifest_written(self, fm, tmp_path, monkeypatch):
        mod = _load_tool()
        out = tmp_path / "seed_manifest.json"
        mod.seed_all(store=None, seeds_dir=str(_SEEDS_DIR), dry_run=False,
                     actor="test", manifest_path=str(out))
        data = json.loads(out.read_text())
        assert len(data["rows"]) == 13
        assert data["actor"] == "test"
