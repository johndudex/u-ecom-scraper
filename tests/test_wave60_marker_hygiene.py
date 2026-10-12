"""[wave-60 N2] Uncopyable markers — stop feeding the writer its own bait.

Job 632: the writer copied the SYSTEM's own annotations from its history
into the draft as code — the tool-call-arg stubs (``[elided N chars — …]``)
became F821 'elided' literals, 18 gate-0 rejections, then a lift that let a
placeholder clobber the draft. N2 makes every system marker self-identify
("SYSTEM MARKER — never write this text into any file") and teaches the
writer the rule in its own prompt; N1's sidecar is the backstop when it
copies anyway.

Scope truth (plan §N2): ``[REDACTED N]`` has NO emitter in this repo — it
is provider-side (GLM/ZAI) redaction. Nothing here can cover it.
"""
from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

subagents = importlib.import_module("agents.subagents")  # noqa: E402
draft_context = importlib.import_module("agents.draft_context")  # noqa: E402

MARKER = "SYSTEM MARKER — never write this text into any file"


class TestStubPrefix:
    def test_old_args_carry_the_marker_prefix(self):
        """The stub a writer sees in its own history must announce itself as
        a system annotation BEFORE the bait text — the 632 writer quoted the
        bare `[elided …]` string into the draft as a literal."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        def _msg(i, *, big_arg=None, tool_id=None):
            if big_arg is not None:
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "write_file",
                        "args": {"path": "workspace/s/scraper_draft.py",
                                 "content": big_arg},
                        "id": f"call-{i}",
                        "type": "tool_call",
                    }],
                )
            if tool_id is not None:
                return ToolMessage(content="ok", tool_call_id=tool_id)
            return HumanMessage(content="TASK: build the scraper " + "x" * 500)

        msgs = [_msg(-1)]
        for i in range(8):
            msgs.append(_msg(i, big_arg="DRAFT_v%d = " % i + "d" * 5000))
            msgs.append(_msg(i, tool_id=f"call-{i}"))

        hook = subagents._make_pre_model_hook(0)
        out = hook({"messages": msgs})["llm_input_messages"]
        stubbed = [
            tc["args"]["content"]
            for m in out
            for tc in (getattr(m, "tool_calls", None) or [])
            if (tc.get("id") or "").startswith("call-")
            and MARKER[:20] in str(tc.get("args", {}).get("content", ""))
        ]
        assert stubbed, "expected stubbed old write_file args"
        for content in stubbed:
            assert content.startswith(MARKER), (
                f"stub must self-identify first: {content[:120]!r}"
            )
            assert "elided " in content and " chars" in content, (
                "the elided-bytes accounting must survive the prefix"
            )
            assert "read_file" in content, "the on-disk pointer must survive"
            assert len(content) < 1000, "stub must stay byte-light"

    def test_small_args_never_get_stubbed(self):
        """The marker rides ONLY oversized stubs — short args pass through
        unmarked, so the annotation stays rare and load-bearing."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        msgs = [
            HumanMessage(content="TASK: build the scraper " + "x" * 500),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "write_file",
                    "args": {"path": "workspace/s/scraper_draft.py",
                             "content": "X = 1\n"},
                    "id": "call-0",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(content="ok", tool_call_id="call-0"),
        ]
        hook = subagents._make_pre_model_hook(0)
        out = hook({"messages": msgs})["llm_input_messages"]
        for m in out:
            for tc in (getattr(m, "tool_calls", None) or []):
                assert MARKER not in str(tc), "short arg was stubbed"


class TestElisionMarker:
    def test_marker_is_comment_form_inside_the_fence(self):
        """_Elision renders INSIDE the embed's ```python fence — a non-
        comment marker here would manufacture the exact copied-marker
        SyntaxError class N1 kills (critique-2 blocker)."""
        elision = draft_context._Elision
        assert MARKER in elision
        for ln in elision.splitlines():
            stripped = ln.strip()
            assert (not stripped) or stripped.startswith("#"), (
                f"elision line is not comment-form: {ln!r}"
            )

    def test_middle_elided_literal_survives(self):
        """test_wave32_eow_diet pins/splits on the literal `middle elided`
        (asserts + .split anchor) — the prefix must not rename it."""
        assert "middle elided" in draft_context._Elision
        assert "[middle elided" in draft_context._Elision


class TestWriterPromptGuidance:
    def test_code_writer_prompt_carries_the_marker_rule(self):
        """Guardian cond-5: the standing marker guidance rides the code_writer
        system prompt (.opencode/agents/code-writer.md) — not the per-job
        feedback_for_writer channel."""
        path = os.path.join(ROOT, ".opencode", "agents", "code-writer.md")
        src = open(path, encoding="utf-8").read()
        assert MARKER in src, "prompt must name the marker text itself"
        assert "elided" in src, "prompt must cover the arg-stub form"
        assert "middle elided" in src, "prompt must cover the embed-elision form"
        assert "read_file" in src, (
            "the remedy is re-reading the real section from disk"
        )
        assert "job 632" in src


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
