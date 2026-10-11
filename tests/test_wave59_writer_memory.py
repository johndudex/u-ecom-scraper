"""Wave-59 C1: the truncation budget walks the WHOLE history, not a 6-message slice.

R2 RCA (jobs 623/625, HEAD d4399cb): the step-2 hook did ``other[-6:]`` BEFORE the
char-budget walk, so any over-budget invocation got a 6-message working memory
regardless of a 62k-130k budget — observed as ``Truncated messages: 280 → 7`` /
``295 → 7`` / ``28 → 1``. Every 24k draft read was evicted within 1-3 turns, so
623-inv2 spent 88% of its turns re-reading its own draft (114 reads, 0 edits).

Fix: the newest-first walk covers all step-1-trimmed history; ``_MIN_KEEP_RECENT``
stays as a HARD floor (newest 6 kept even over budget — byte-identical to the old
unconditional slice, never shipping more than today).

Run from repo root:  python3 -m pytest tests/test_wave59_writer_memory.py -v
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

BUDGET_60K = dict(
    LLM_TRUNCATION_MODE="on",
    LLM_TRUNCATION_MAX_CHARS=60000,
    LLM_TRUNCATION_PER_MSG_CAP=8000,
    LLM_TRUNCATION_TOOL_MSG_CAP=24000,
)
BUDGET_120K = dict(BUDGET_60K, LLM_TRUNCATION_MAX_CHARS=120000)
BUDGET_TIGHT = dict(
    LLM_TRUNCATION_MODE="on",
    LLM_TRUNCATION_MAX_CHARS=20000,
    LLM_TRUNCATION_PER_MSG_CAP=8000,
    LLM_TRUNCATION_TOOL_MSG_CAP=24000,
)


def _spiral_transcript(n_pairs=20, tool_chars=7900):
    """A 623-shaped transcript: seed + N (AI chatter + big tool result) pairs."""
    msgs = [HumanMessage(content="TASK extract products from listing seeds")]
    for i in range(n_pairs):
        msgs.append(AIMessage(content=f"thinking step {i}", tool_calls=[
            {"name": "read_file", "args": {"path": f"f{i}.py"}, "id": f"c{i}",
             "type": "tool_call"},
        ]))
        msgs.append(ToolMessage(content="x" * tool_chars, tool_call_id=f"c{i}"))
    return msgs


def _run(messages):
    out = _truncate_messages({"messages": messages})
    return out.get("llm_input_messages", out.get("messages"))


class TestBudgetWalksWholeHistory:
    @override_settings(**BUDGET_60K)
    def test_long_history_keeps_more_than_six(self):
        """20×(0.4k AI + 7.9k tool) ≈ 166k chars against a 60k budget: the walk
        keeps ~7 pairs (the old code kept exactly 6 messages = 3 pairs)."""
        out = _run(_spiral_transcript())
        non_seed = [m for m in out if getattr(m, "type", "") != "human"]
        assert len(non_seed) > 6, (
            f"budget walk must keep more than the old 6-message slice "
            f"(kept {len(non_seed)})"
        )

    @override_settings(**BUDGET_60K)
    def test_kept_count_grows_with_budget(self):
        """The same transcript under a doubled budget keeps strictly more
        history — the old pre-slice made kept-count budget-invariant."""
        small = _run(_spiral_transcript())
        small_n = len([m for m in small if getattr(m, "type", "") != "human"])
        with override_settings(**BUDGET_120K):
            big = _run(_spiral_transcript())
        big_n = len([m for m in big if getattr(m, "type", "") != "human"])
        assert big_n > small_n, (
            f"kept-count must grow with budget (60k→{small_n}, 120k→{big_n})"
        )

    @override_settings(**BUDGET_60K)
    def test_under_budget_history_untouched(self):
        """A transcript that fits is returned whole — step 2 never engages."""
        msgs = _spiral_transcript(n_pairs=4)
        out = _run(msgs)
        assert len(out) == len(msgs)


class TestHardFloor:
    @override_settings(**BUDGET_TIGHT)
    def test_floor_holds_when_every_message_oversized(self):
        """Every message ~24k against a 20k budget: the walk selects nothing,
        the hard floor still ships the newest 6 (may exceed budget — explicit)."""
        out = _run(_spiral_transcript(n_pairs=10, tool_chars=24000))
        non_seed = [m for m in out if getattr(m, "type", "") != "human"]
        assert len(non_seed) >= 6, (
            f"hard floor must keep ≥6 messages when all are oversized "
            f"(kept {len(non_seed)})"
        )

    @override_settings(**BUDGET_TIGHT)
    def test_floor_never_ships_more_than_old_code(self):
        """The floor is byte-identical to the old unconditional `other[-6:]`:
        never more than 6 non-seed messages."""
        out = _run(_spiral_transcript(n_pairs=10, tool_chars=24000))
        non_seed = [m for m in out if getattr(m, "type", "") != "human"]
        assert len(non_seed) <= 6

    @override_settings(**BUDGET_TIGHT)
    def test_floor_is_chronological(self):
        """Floor output stays in chronological order (newest last)."""
        out = _run(_spiral_transcript(n_pairs=10, tool_chars=24000))
        non_seed = [m for m in out if getattr(m, "type", "") != "human"]
        ids = []
        for m in non_seed:
            tc = getattr(m, "tool_calls", None)
            if tc:
                ids.append(tc[0]["id"])
            elif getattr(m, "tool_call_id", None):
                ids.append(m.tool_call_id)
        # pair order: cN's AIMessage before cN's ToolMessage, pairs ascending
        assert ids == sorted(ids, key=lambda x: int(x[1:])), (
            f"floor output must be chronological, got {ids}"
        )


class TestPairSafetyAndSeed:
    @override_settings(**BUDGET_60K)
    def test_no_orphan_tool_results(self):
        out = _run(_spiral_transcript())
        opened = set()
        for m in out:
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                assert tcid in opened, f"orphan ToolMessage {tcid} shipped"
            else:
                for tc in getattr(m, "tool_calls", None) or []:
                    opened.add(tc["id"])

    @override_settings(**BUDGET_60K)
    def test_seed_always_retained(self):
        msgs = _spiral_transcript()
        out = _run(msgs)
        assert any(
            getattr(m, "type", "") == "human" and "TASK extract" in str(m.content)
            for m in out
        ), "seed (task spec) must never be trimmed away"

    @override_settings(LLM_TRUNCATION_MODE="off")
    def test_off_mode_noop(self):
        msgs = _spiral_transcript(n_pairs=3)
        out = _run(msgs)
        assert len(out) == len(msgs)
