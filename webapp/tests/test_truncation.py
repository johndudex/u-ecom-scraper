"""Unit tests for _truncate_messages (webapp/agents/subagents.py) — the real fix.

Covers: returns llm_input_messages (NOT messages); tool_calls counted in budget;
seed (first HumanMessage) always retained AND never capped; pair-safe drop
(no orphaned ToolMessage); kill-switch.
"""

from django.test import TestCase, override_settings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.subagents import _truncate_messages


def _run(messages):
    """Return the list the LLM will actually see (llm_input_messages)."""
    return _truncate_messages({"messages": messages})["llm_input_messages"]


def _big_tool_call(size, call_id="call_1", name="write_file"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {"content": "x" * size}, "id": call_id}],
    )


def _assert_pair_safe(kept):
    """Every ToolMessage must be backed by a preceding AIMessage's tool_call."""
    opened = set()
    for m in kept:
        tcid = getattr(m, "tool_call_id", None)
        if tcid:
            assert tcid in opened, f"orphaned ToolMessage {tcid} in kept view"
        for tc in (getattr(m, "tool_calls", None) or []):
            if isinstance(tc, dict) and tc.get("id"):
                opened.add(tc["id"])


class TestTruncation(TestCase):
    # ── the actual fix: llm_input_messages, not messages ─────────────────────
    def test_returns_llm_input_messages_key(self):
        result = _truncate_messages({"messages": [HumanMessage(content="hi")]})
        self.assertIn("llm_input_messages", result)
        self.assertNotIn("messages", result)

    def test_under_budget_keeps_everything(self):
        msgs = [HumanMessage(content="seed"), AIMessage(content="t"), ToolMessage(content="r", tool_call_id="c1")]
        kept = _run(msgs)
        self.assertEqual(len(kept), len(msgs))

    # ── (A) tool_calls counted in budget ─────────────────────────────────────
    def test_tool_calls_counted_so_step2_fires(self):
        msgs = [HumanMessage(content="seed"), _big_tool_call(200_000), ToolMessage(content="ok", tool_call_id="call_1")]
        kept = _run(msgs)
        self.assertIn(HumanMessage, [type(m) for m in kept])  # seed retained
        self.assertLess(len(kept), len(msgs))                  # Step 2 fired
        _assert_pair_safe(kept)

    # ── seed retained AND never capped ───────────────────────────────────────
    def test_seed_always_retained_when_over_budget(self):
        msgs = [HumanMessage(content="the original task seed")]
        for i in range(8):
            cid = f"c{i}"
            msgs.append(AIMessage(content="", tool_calls=[{"name": "edit_file", "args": {"s": "y" * 30_000}, "id": cid}]))
            msgs.append(ToolMessage(content="ok", tool_call_id=cid))
        kept = _run(msgs)
        self.assertTrue(any(isinstance(m, HumanMessage) and m.content == "the original task seed" for m in kept))

    def test_seed_never_capped_even_when_huge(self):
        # A 20k seed (larger than per_msg_cap=8000) must pass through FULL.
        seed = HumanMessage(content="S" * 20_000)
        kept = _run([seed, AIMessage(content="ok")])
        seeds = [m for m in kept if isinstance(m, HumanMessage)]
        self.assertEqual(len(seeds), 1)
        self.assertEqual(len(seeds[0].content), 20_000)  # NOT capped to 8000

    def test_non_seed_message_IS_capped(self):
        big = ToolMessage(content="B" * 20_000, tool_call_id="c1")
        msgs = [HumanMessage(content="seed"), AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]), big]
        kept = _run(msgs)
        tools = [m for m in kept if isinstance(m, ToolMessage)]
        self.assertTrue(any(len(m.content) < 20_000 for m in tools))  # capped to ~8000

    def test_seed_not_duplicated(self):
        msgs = [HumanMessage(content="seed"), _big_tool_call(200_000, call_id="c1"), ToolMessage(content="ok", tool_call_id="c1")]
        kept = _run(msgs)
        self.assertEqual(len([m for m in kept if isinstance(m, HumanMessage) and m.content == "seed"]), 1)

    # ── pair-safe drop ───────────────────────────────────────────────────────
    def test_orphaned_toolmessage_dropped(self):
        # Over budget; the fill loop would naturally keep Tool(big1) after dropping
        # its AI(big1). The pair-safe pass must remove the orphan.
        msgs = [
            HumanMessage(content="seed"),
            _big_tool_call(200_000, call_id="big1"),
            ToolMessage(content="ok-big1", tool_call_id="big1"),
            AIMessage(content="", tool_calls=[{"name": "edit_file", "args": {"x": 1}, "id": "keep"}]),
            ToolMessage(content="ok-keep", tool_call_id="keep"),
        ]
        kept = _run(msgs)
        _assert_pair_safe(kept)  # no orphaned ToolMessage reaches the model
        ids = {tc.get("id") for m in kept if isinstance(m, AIMessage) for tc in (m.tool_calls or [])}
        tool_ids = {m.tool_call_id for m in kept if isinstance(m, ToolMessage)}
        self.assertTrue(tool_ids.issubset(ids | set()))  # every kept tool has its AI

    def test_recent_pair_survives(self):
        msgs = [HumanMessage(content="seed"), _big_tool_call(180_000, call_id="filler"), ToolMessage(content="ok", tool_call_id="filler")]
        msgs.append(AIMessage(content="", tool_calls=[{"name": "edit_file", "args": {"x": 1}, "id": "keep"}]))
        msgs.append(ToolMessage(content="done", tool_call_id="keep"))
        kept = _run(msgs)
        tool_ids = {m.tool_call_id for m in kept if isinstance(m, ToolMessage)}
        self.assertIn("keep", tool_ids)

    # ── budget + kill-switch ─────────────────────────────────────────────────
    def test_kept_total_respects_budget(self):
        def _honest(m):
            n = len(str(getattr(m, "content", "")))
            tc = getattr(m, "tool_calls", None)
            return n + (len(str(tc)) if tc else 0)

        msgs = [HumanMessage(content="seed")]
        for i in range(10):
            cid = f"c{i}"
            msgs.append(AIMessage(content="", tool_calls=[{"name": "edit_file", "args": {"s": "z" * 25_000}, "id": cid}]))
            msgs.append(ToolMessage(content="ok", tool_call_id=cid))
        kept = _run(msgs)
        self.assertLessEqual(sum(_honest(m) for m in kept), 180_000)

    @override_settings(LLM_TRUNCATION_MODE="off")
    def test_mode_off_is_exact_rollback(self):
        msgs = [HumanMessage(content="seed"), _big_tool_call(200_000, call_id="c1"), ToolMessage(content="ok", tool_call_id="c1")]
        result = _truncate_messages({"messages": msgs})
        self.assertNotIn("llm_input_messages", result)  # kill-switch → no shaping
