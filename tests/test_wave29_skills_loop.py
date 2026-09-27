"""wave-29: close the learn→reuse loop.

A1 — the assembled runtime toolset must honor AGENT_TOOL_MAP's write-tool
requests. nav_skill_review is the sole skill writer and its prompt instructs
it to call ``learn_skill``, but ``_get_tools_sync`` never wired
``get_skill_write_tools`` (the tools existed only in the production-dead
async ``get_tools_for_agent``). Audit evidence: ``shared-data/skills/
_audit.jsonl`` shows zero production appends since the 2026-08-19 FM
migration. The prompt/tool mismatch made every nav_skill_review run a
no-op write-side.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))


class TestSkillWriteWiring:
    """A1: _get_tools_sync must wire write tools when the map requests them."""

    def test_nav_skill_review_gets_write_tools(self):
        from agents.subagents import _get_tools_sync

        names = [t.name for t in _get_tools_sync("nav_skill_review")]
        assert "learn_skill" in names, f"learn_skill missing from {names}"
        assert "create_new_skill" in names, f"create_new_skill missing from {names}"

    def test_nav_skill_review_keeps_read_tools(self):
        from agents.subagents import _get_tools_sync

        names = [t.name for t in _get_tools_sync("nav_skill_review")]
        assert "load_skill" in names
        assert "list_skills" in names

    def test_write_tools_not_leaked_to_other_agents(self):
        """The write path is nav_skill_review ONLY — no other agent may
        receive learn_skill/create_new_skill just because the branch
        exists."""
        from agents.subagents import _get_tools_sync

        for agent in ("code_writer", "site_analyzer", "skill_learner"):
            names = [t.name for t in _get_tools_sync(agent)]
            assert "learn_skill" not in names, f"{agent} leaked learn_skill"
            assert "create_new_skill" not in names, f"{agent} leaked create_new_skill"


class TestLoadSkillTelemetry:
    """A2: ToolCallLog.args_summary must carry the skill name.

    The load_skill tool signature is ``load_skill(skill_name: str)`` but
    ``_summarize_tool_args`` read ``args.get('name')`` — every persisted
    load_skill row rendered "Load skill: " blank, which made the S0.1
    baseline query (voluntary load_skill usage per agent) impossible to
    evaluate and killed the after-metric for reuse.
    """

    def test_load_skill_summary_covers_skill_name_kwarg(self):
        from agents.graph import _summarize_tool_args

        summary = _summarize_tool_args(
            "load_skill", {"skill_name": "jsonld-extraction"}
        )
        assert "jsonld-extraction" in summary, summary

    def test_load_skill_summary_still_accepts_name_kwarg(self):
        """Legacy/alternate kwarg must not regress the summary either."""
        from agents.graph import _summarize_tool_args

        summary = _summarize_tool_args("load_skill", {"name": "shopify-detection"})
        assert "shopify-detection" in summary, summary


class TestSkillBlurbGating:
    """A3: the "Available Skills" blurb must only reach agents whose actual
    toolset contains load_skill. Today it is appended unconditionally in
    _build_agent BEFORE tools are assembled, so code_tester / cleanup /
    dagster_converter are advertised a tool they don't have — pure prompt
    noise that invites doomed load_skill attempts.
    """

    def test_blurb_omitted_when_toolset_lacks_load_skill(self):
        from agents.subagents import _append_skill_descriptions

        out = _append_skill_descriptions(
            "BASE PROMPT", tool_names={"read_file", "run_scraper"}
        )
        assert out == "BASE PROMPT"

    def test_blurb_present_when_toolset_has_load_skill(self):
        from agents.subagents import _append_skill_descriptions

        out = _append_skill_descriptions(
            "BASE PROMPT", tool_names={"load_skill", "list_skills"}
        )
        assert "Available Skills" in out
        assert out.startswith("BASE PROMPT")

    def test_legacy_call_appends_untracked(self):
        """No-toolset callers (if any remain) keep old behavior rather than
        silently dropping the blurb everywhere."""
        from agents.subagents import _append_skill_descriptions

        out = _append_skill_descriptions("BASE PROMPT")
        assert "Available Skills" in out

    def test_build_agent_gates_blurb_by_tool_map(self, monkeypatch):
        """_build_agent must assemble tools BEFORE appending the blurb so the
        gate sees the real toolset. code_tester has no load_skill in
        AGENT_TOOL_MAP → its prompt must not carry the blurb."""
        import agents.subagents as sa
        from agents.tools import AGENT_TOOL_MAP

        def fake_tools(agent_name, workspace_scope=None):
            return [
                type("T", (), {"name": n})() for n in AGENT_TOOL_MAP.get(agent_name, [])
            ]

        def fake_react(*a, **k):
            # production calls create_react_agent(llm, tools=..., prompt=...)
            return {"prompt": k.get("prompt"), "tools": k.get("tools")}

        monkeypatch.setattr(sa, "load_agent_prompt", lambda stem: "BASE " + stem)
        monkeypatch.setattr(sa, "_get_tools_sync", fake_tools)
        monkeypatch.setattr(sa, "_strip_v_prefix_from_tools", lambda t: t)
        monkeypatch.setattr(sa, "_install_invocation_cancellation", lambda: None)
        monkeypatch.setattr(sa, "get_main_llm", lambda temperature: object())
        monkeypatch.setattr(sa, "create_react_agent", fake_react)

        tester_prompt = sa._build_agent("code_tester")["prompt"]
        analyzer_prompt = sa._build_agent("site_analyzer")["prompt"]
        assert "Available Skills" not in tester_prompt, (
            "code_tester advertised load_skill it does not have"
        )
        assert "Available Skills" in analyzer_prompt

    def test_site_analyzer_prompt_no_longer_advertises_load_skill(self):
        """A3 reconcile: build_site_analyzer_message prohibits skill loading
        ('detect from page content only') — the .md system prompt must not
        contradict it by advertising optional load_skill calls."""
        import re
        from pathlib import Path

        md = (
            Path(__file__).resolve().parents[1]
            / ".opencode"
            / "agents"
            / "site-analyzer.md"
        )
        text = md.read_text(encoding="utf-8")
        assert not re.search(r"load_skill", text), (
            "site-analyzer.md still advertises load_skill while "
            "build_site_analyzer_message prohibits it — reconcile the two"
        )


class TestLearnedSectionSurfacing:
    """C1: platform-skill '## Learned:' sections must reach the writer via
    _platform_distillation. Today ~65.7K chars of real learned content is
    unread: the read path is frontmatter-only (skills_store.py:122-126) and
    the one reuse consumer was deleted with deterministic navigation."""

    def test_render_returns_newest_two_whole_sections(self):
        import src.skills_store as store

        text = (
            "---\nname: x\ndescription: d\n---\n\nbaseline body\n"
            "## Learned: first lesson\n**Source:** job 1\n\nbody one\n"
            "## Learned: second lesson\n**Source:** job 2\n\nbody two\n"
            "## Learned: third lesson\n**Source:** job 3\n\nbody three\n"
        )
        out = store.render_learned_sections("x", _text=text)
        assert "## Learned: third lesson" in out
        assert "## Learned: second lesson" in out
        assert "first lesson" not in out  # only 2 newest
        assert "body three" in out and "body two" in out
        # whole sections: header + body travel together
        assert out.index("second") < out.index("third")

    def test_never_split_mid_section(self):
        """If the newest section alone busts the cap, drop it (never emit a
        half-section); an older section that fits may still surface."""
        import src.skills_store as store

        big = "x" * 2000
        text = (
            "---\nname: x\ndescription: d\n---\n\n"
            "## Learned: small one\nsmall body\n"
            f"## Learned: huge one\n{big}\n"
        )
        out = store.render_learned_sections("x", cap=1500, _text=text)
        assert "huge one" not in out
        assert "## Learned: small one" in out
        assert len(out) <= 1500

    def test_cap_against_real_jsonld_file(self):
        """The real 33KB jsonld-extraction skill (15 learned sections) must
        collapse to ≤1500 chars of whole sections. Reads the image seed copy
        directly — deterministic, no FM dependency."""
        from pathlib import Path

        import src.skills_store as store

        p = (
            Path(__file__).resolve().parents[1]
            / ".opencode"
            / "skills"
            / "jsonld-extraction"
            / "SKILL.md"
        )
        text = p.read_text(encoding="utf-8")
        assert len(text) > 20_000, "fixture drifted — expected the real 33KB file"
        out = store.render_learned_sections("jsonld-extraction", cap=1500, _text=text)
        assert out, "no learned sections rendered from real file"
        assert len(out) <= 1500
        assert "## Learned:" in out

    def test_no_learned_sections_returns_empty(self):
        import src.skills_store as store

        out = store.render_learned_sections(
            "x", _text="---\nname: x\ndescription: d\n---\n\nno learnings\n"
        )
        assert out == ""

    def test_platform_distillation_includes_learned_sections(self, monkeypatch):
        from agents.subagents import _platform_distillation

        import src.skills_store as store

        monkeypatch.setattr(
            store,
            "read_skill",
            lambda name: (
                "---\nname: x\ndescription: d\n---\n\n"
                "## Learned: shopify pagination\nuse cursor pagination\n"
            ),
        )
        state = {
            "site_analysis": {"platform": "shopify"},
            "scraper_analysis": {"strategy": "http_requests"},
        }
        out = _platform_distillation(state)
        assert "LEARNED SKILL NOTES" in out
        assert "use cursor pagination" in out

    def test_platform_without_skill_gets_no_platform_skill(self, monkeypatch):
        from agents.subagents import _platform_distillation

        # [wave-45b] The env gate is gone; silence is only guaranteed when
        # the store renders nothing — so pin the store, not the environment.
        import src.skills_store as store

        monkeypatch.setattr(store, "read_skill", lambda name: None)
        state = {
            "site_analysis": {"platform": "custom-cms"},
            "scraper_analysis": {"strategy": "http_requests"},
        }
        out = _platform_distillation(state)
        assert "LEARNED SKILL NOTES" not in out

    def test_unmatched_platform_still_gets_schema_skill(self, monkeypatch):
        """[wave-43] Cross-cutting output-schema-integrity injects even on
        platforms with no matching skill — store-pinned so FM contents can't
        flip the verdict."""
        from agents.subagents import _platform_distillation

        import src.skills_store as store

        monkeypatch.setattr(
            store,
            "read_skill",
            lambda name: (
                "---\nname: output-schema-integrity\ndescription: d\n---\n\n"
                "## Learned: honest empty\nempty means empty\n"
                if name == "output-schema-integrity"
                else None
            ),
        )
        state = {
            "site_analysis": {"platform": "custom-cms"},
            "scraper_analysis": {"strategy": "http_requests"},
        }
        out = _platform_distillation(state)
        assert "output-schema-integrity" in out
        assert "empty means empty" in out
        assert "shopify-detection" not in out
        assert "magento-detection" not in out
