"""Wave-23 W23-3: tool-result messages get a bigger context cap than chatter.

RC-2 of docs/plans/wave23-writer-convergence-plan.md: LLM_TRUNCATION_PER_MSG_CAP
re-truncates EVERY non-seed message to 8K chars in what the model sees on later
turns — so a 50K read_file page (the tool's own page size) collapses to 8K
in-context. The writer then believes "reads are being truncated to 8K chars
each" (prod 374's own words) and pages through shared modules at ~8K strides:
22 reads of src/http_fetch.py + src/listing_discovery.py in one failed attempt,
including one past EOF and a wrap-around re-read from offset 0. The successful
contrast (job 341) read each shared module exactly once.

Fix: ToolMessage content gets its own, larger cap (LLM_TRUNCATION_TOOL_MSG_CAP,
default 24000) — http_fetch.py at 21K now fits a single in-context view. The
overall LLM_TRUNCATION_MAX_CHARS budget still bounds the whole prompt, so this
cannot re-inflate context past the balloon guard. Non-tool messages keep the
tight 8K cap.

Run from repo root:  python3 -m pytest tests/test_tool_message_truncation.py -v
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

from agents.subagents import _truncate_messages  # noqa: E402
from django.test import override_settings  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    ToolMessage,
)


def _run(messages):
    return _truncate_messages({"messages": messages})["llm_input_messages"]


class TestToolMessageCap:
    @override_settings(LLM_TRUNCATION_PER_MSG_CAP=8000)
    def test_tool_result_within_tool_cap_passes_untrimmed(self):
        seed = HumanMessage(content="TASK extract products")
        big_tool = ToolMessage(content="x" * 20000, tool_call_id="call_1")
        out = _run([seed, AIMessage(content="ok"), big_tool])
        assert out[-1].content == "x" * 20000, (
            "a 21K-char shared module must survive intact in-context "
            "(per-msg 8K cap must not eat tool results)"
        )

    @override_settings(LLM_TRUNCATION_PER_MSG_CAP=8000)
    def test_tool_result_over_tool_cap_trims_at_tool_cap(self):
        seed = HumanMessage(content="TASK extract products")
        huge_tool = ToolMessage(content="y" * 30000, tool_call_id="call_2")
        out = _run([seed, huge_tool])
        content = out[-1].content
        assert "deterministic-truncated" in content
        assert len(content) < 30000 + 200, "trim must land near the tool cap"

    @override_settings(
        LLM_TRUNCATION_PER_MSG_CAP=8000, LLM_TRUNCATION_TOOL_MSG_CAP=5000
    )
    def test_tool_cap_is_configurable(self):
        seed = HumanMessage(content="TASK")
        tool = ToolMessage(content="z" * 9000, tool_call_id="call_3")
        out = _run([seed, tool])
        assert "deterministic-truncated" in out[-1].content
        assert len(out[-1].content) < 9000

    @override_settings(LLM_TRUNCATION_PER_MSG_CAP=8000)
    def test_non_tool_messages_keep_tight_cap(self):
        seed = HumanMessage(content="TASK extract products")
        rambling_ai = AIMessage(content="a" * 20000)
        out = _run([seed, rambling_ai])
        assert "deterministic-truncated" in out[-1].content, (
            "assistant chatter must stay at the tight per-msg cap"
        )
        assert len(out[-1].content) < 9000

    @override_settings(LLM_TRUNCATION_PER_MSG_CAP=8000)
    def test_seed_never_trimmed(self):
        seed = HumanMessage(content="TASK " + "s" * 20000)
        out = _run([seed])
        assert out[0].content == "TASK " + "s" * 20000

    @override_settings(LLM_TRUNCATION_PER_MSG_CAP=8000)
    def test_default_tool_cap_is_24000(self):
        # exactly at the default cap boundary: 24000 chars passes untrimmed
        seed = HumanMessage(content="TASK")
        tool = ToolMessage(content="w" * 24000, tool_call_id="call_4")
        out = _run([seed, tool])
        assert out[-1].content == "w" * 24000


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
