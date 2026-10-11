"""[wave-58 class 2] Writer convergence: recursion floor, two-strike
window, and per-turn size.

Batch 613-616 writer evidence:

- revolve 614 inv2 and wonderbly 616 inv2 died mid-edit on langgraph's
  ``Recursion limit of 152 reached`` (51 turns × ~3 super-steps = 153 > 152)
  with ZERO draft bytes written. 152 is computed: ``_WRITER_ROUNDS (50) * 3
  + 2`` (graph.py AGENT_RECURSION_MAP). The wall clock (1800s base / 2700s
  ceiling) was meant to be the real backstop; the step counter must never
  bind below it.
- wave-56b F7 collapses the extension ceiling to the BASE window (1800)
  after ONE wall-clock death — so wonderbly 616 inv3 died at base with no
  draft while the operator's configured ceiling (WRITER_MAX_TIMEOUT=2700)
  was never even tried once. One death retires the GROWTH past the
  configured max, not the max itself; two deaths restore the fail-fast.
- writer turn inputs measured at 112k-435k chars: the pre-model hook caps
  tool RESULTS (24k) and chatter (8k) but never trims ``tool_calls`` args —
  write_file/edit_file args carry the whole draft on every retained turn,
  and the post-trim model input still reaches the ~45k-token prefill scale
  the wave-52 canary proved kills provider windows.
"""



from agents.graph import (
    _WRITER_ROUNDS,
    AGENT_RECURSION_MAP,
    _writer_extension_ceiling,
    _writer_invoke_timeout,
    _writer_max_timeout,
    _writer_window_death,
)


def _far_deadline(seconds):
    """A job deadline far enough out that the clamp never binds."""
    from datetime import timedelta

    from django.utils import timezone

    return timezone.now() + timedelta(seconds=seconds)

# ── 2a. the step counter never binds below the wall clock ────────────────


class TestRecursionFloor:
    def test_writer_limit_derives_from_rounds(self):
        """The wave-32 formula pin, restated: the limit tracks rounds."""
        assert AGENT_RECURSION_MAP["code_writer"] == _WRITER_ROUNDS * 3 + 2

    def test_writer_limit_clears_the_observed_death(self):
        """51 turns = 153 super-steps killed 614/616; wonderbly 625 then died
        on RecursionError BELOW its wall even at limit 452 — the R2 window
        restoral (1800/2700) buys ~120 more turns than the 1200s pin did.
        [wave-59 C5] 200 rounds (limit 602 ≈ 3070s at the measured 15 s/turn
        floor) keeps GraphRecursion past the 2700s max wall for any operator
        window: the wall clock binds first, the recursion floor stays
        theoretical. (Fast-ceiling deaths stay recursion-class — the
        reclassification was dropped in review, so the T12 wall-only grant
        below is unchanged.)"""
        assert AGENT_RECURSION_MAP["code_writer"] >= 600

    def test_other_agents_keep_their_limits(self):
        """The raise is writer-only — no other phase's budget moves."""
        assert AGENT_RECURSION_MAP["code_tester"] == 120
        assert AGENT_RECURSION_MAP["cleanup"] == 80


# ── 2c. the window: two-strike ceiling + recursion deaths count ─────────


class TestTwoStrikeCeiling:
    def test_healthy_writer_keeps_the_full_ceiling(self):
        assert _writer_extension_ceiling({}) == _writer_max_timeout()

    def test_first_death_keeps_the_configured_ceiling(self):
        """616 inv3's regression: after ONE death the next window must still
        be the operator's configured max, not the base clock — a window the
        writer never got is not slack, it is the working set."""
        assert _writer_extension_ceiling(
            {"writer_wall_clock_timeouts": 1}
        ) == _writer_max_timeout()

    def test_second_death_restores_the_fail_fast(self):
        """F7's original intent (job 580's zombie burn) lives at strike 2."""
        assert _writer_extension_ceiling(
            {"writer_wall_clock_timeouts": 2}
        ) == min(_writer_max_timeout(), _writer_invoke_timeout())
        assert _writer_extension_ceiling(
            {"writer_wall_clock_timeouts": 5}
        ) == min(_writer_max_timeout(), _writer_invoke_timeout())

    def test_low_env_ceiling_is_honored(self, monkeypatch):
        monkeypatch.setenv("WRITER_MAX_TIMEOUT", "1200")
        assert _writer_extension_ceiling({}) == 1200
        assert _writer_extension_ceiling({"writer_wall_clock_timeouts": 1}) == 1200

    def test_none_state_is_healthy(self):
        assert _writer_extension_ceiling(None) == _writer_max_timeout()


class TestRecursionDeathCountsAsAWindowDeath:
    def test_wall_clock_still_counts(self):
        assert _writer_window_death(
            "wall-clock timeout after 1800s base (thread stopped at 1800s)",
            "WallClockTimeout",
        )

    def test_recursion_limit_counts(self):
        """The new death mode: GraphRecursionError burns the window exactly
        like a timeout — F7's ledger must see it."""
        assert _writer_window_death(
            "Recursion limit of 152 reached.", "GraphRecursionError"
        )

    def test_healthy_error_does_not_count(self):
        assert not _writer_window_death(
            "Error code: 429 - rate limited", "RateLimitError"
        )
        assert not _writer_window_death("", "")

    def test_recursion_death_reaches_strike_two_like_a_wall_death(self, monkeypatch):
        """Behavior: the strike total arming the abort is the SUM of both
        ledgers (guardian change 2). A recursion death costs a full window
        with zero draft bytes (614/616 inv2), so it must reach strike 2
        exactly like a wall-clock death — through the ceiling arithmetic the
        abort shares."""
        from agents import graph

        monkeypatch.delenv("WRITER_MAX_TIMEOUT", raising=False)
        base, capped = graph._writer_invoke_timeout(), graph._writer_max_timeout()
        # one recursion strike keeps the configured max ...
        assert graph._writer_extension_ceiling(
            {"writer_recursion_deaths": 1}
        ) == capped
        # ... a wall strike PLUS a recursion strike is strike 2 → the base
        # window (fail-fast restored), identical to two wall strikes
        assert graph._writer_extension_ceiling(
            {"writer_wall_clock_timeouts": 1, "writer_recursion_deaths": 1}
        ) == min(base, capped)
        assert graph._writer_extension_ceiling(
            {"writer_wall_clock_timeouts": 2}
        ) == min(base, capped)

    def test_recursion_deaths_do_not_buy_the_3600s_grant(self, monkeypatch):
        """[critique S3 / guardian change 2] the T12 progress-escalation grant
        (2 strikes → a doubled writer window, 2×1800s = 3600s) must require
        WALL-CLOCK strikes only: a writer that recursion-died cannot converge
        in more turns either, and 3600s there is the job-580 burn class.
        Asserted on the grant helper itself — the single authority for
        eligibility."""
        from agents import graph

        grants = {
            "job_id": 0,
            "last_tested_draft_fp": "fp-abc123",
            "draft_mutated_during_test": False,
            "noop_fix_cycles": 0,
            "writer_escalation_used": False,
        }
        far = _far_deadline(600)
        monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
        # wall-clock strikes ≥ 2 qualify ...
        assert graph._writer_escalation_window(
            {**grants, "writer_wall_clock_timeouts": 2}, far
        )
        # ... recursion strikes NEVER do — not at 2, not mixed with wall
        assert graph._writer_escalation_window(
            {**grants, "writer_recursion_deaths": 2}, far
        ) is None
        assert graph._writer_escalation_window(
            {**grants, "writer_wall_clock_timeouts": 1,
             "writer_recursion_deaths": 1}, far
        ) is None
        # and below the two-strike threshold there is no grant either way
        assert graph._writer_escalation_window(
            {**grants, "writer_wall_clock_timeouts": 1}, far
        ) is None


# ── 2b. old write_file/edit_file args stop riding every turn ─────────────


def _msg(i, *, big_arg=None, content="ok", tool_id=None):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

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
        return ToolMessage(content=content, tool_call_id=tool_id)
    if i == -1:
        return HumanMessage(content=content)
    return AIMessage(content=content)


class TestToolCallArgStubbing:
    def _build_hook(self):
        from agents.subagents import _make_pre_model_hook

        return _make_pre_model_hook(0)

    def _history(self):
        """Seed + 8 old write_file turns (each carrying a full draft) + the
        6 most recent messages (last one carries the CURRENT draft)."""
        seed = _msg(-1, content="TASK: build the scraper " + "x" * 500)
        msgs = [seed]
        for i in range(8):
            msgs.append(_msg(i, big_arg=f"DRAFT_v{i} = " + "d" * 5000))
            msgs.append(_msg(i, tool_id=f"call-{i}"))
        # recent window (last 6 non-system): 3 more turns, the last with the
        # live draft the writer must still see in full
        for i in (8, 9):
            msgs.append(_msg(i, content="thinking about selectors"))
        msgs.append(_msg(10, big_arg="DRAFT_LIVE = " + "L" * 5000))
        return msgs

    def test_old_draft_args_are_stubbed(self):
        hook = self._build_hook()
        out = hook({"messages": self._history()})["llm_input_messages"]
        old_calls = [
            tc
            for m in out
            for tc in (getattr(m, "tool_calls", None) or [])
            if (tc.get("id") or "") in {f"call-{i}" for i in range(8)}
        ]
        assert old_calls, "expected the old write_file calls to survive"
        for tc in old_calls:
            assert len(str(tc)) < 1000, "old draft bytes must be elided"

    def test_the_live_draft_args_survive(self):
        """The most recent write must keep its full args — the writer reads
        its own latest edit from history, not from disk."""
        hook = self._build_hook()
        out = hook({"messages": self._history()})["llm_input_messages"]
        live = [
            tc
            for m in out
            for tc in (getattr(m, "tool_calls", None) or [])
            if (tc.get("id") or "") == "call-10"
        ]
        assert live, "expected the live write_file call to survive"
        assert "DRAFT_LIVE" in str(live[0])

    def test_tool_result_pairing_survives(self):
        """Stubbing args must never break provider pairing: every surviving
        ToolMessage keeps a backing tool_call id, and the ids are unique."""
        hook = self._build_hook()
        out = hook({"messages": self._history()})["llm_input_messages"]
        opened = set()
        for m in out:
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                assert tcid in opened, f"orphaned ToolMessage {tcid}"
            else:
                for tc in getattr(m, "tool_calls", None) or []:
                    opened.add(tc.get("id"))
        ids = [
            tc.get("id")
            for m in out
            for tc in (getattr(m, "tool_calls", None) or [])
        ]
        assert len(ids) == len(set(ids))

    def test_small_tool_args_are_untouched(self):
        """A read_file(path) call is 30 chars — stubbing must not fire on
        it (only the byte-heavy write/edit args get elided)."""
        from langchain_core.messages import AIMessage

        hook = self._build_hook()
        msgs = [
            _msg(-1, content="task"),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "read_file",
                    "args": {"path": "workspace/s/scraper_draft.py"},
                    "id": "call-r0",
                    "type": "tool_call",
                }],
            ),
            _msg(0, tool_id="call-r0"),
        ]
        for i in range(6):
            msgs.append(_msg(i, content="filler"))
        out = hook({"messages": msgs})["llm_input_messages"]
        small = [
            tc
            for m in out
            for tc in (getattr(m, "tool_calls", None) or [])
            if (tc.get("id") or "") == "call-r0"
        ]
        assert small and small[0]["args"] == {
            "path": "workspace/s/scraper_draft.py"
        }

    def test_stubbed_turn_keeps_message_metadata(self):
        """[critique S6] The reconstruction must not silently drop
        additional_kwargs / response_metadata — GLM's usage/token blocks and
        reasoning traces ride there, and losing them degrades the very
        history accounting wave-25e measures."""

        hook = self._build_hook()
        big = _msg(
            0, big_arg="OLD = %s" % ("d" * 5000))
        big.additional_kwargs = {"reasoning": "choose selectors", "usage": {"tokens": 12}}
        big.response_metadata = {"model": "glm-4.7", "finish_reason": "tool_calls"}
        msgs = [
            _msg(-1, content="task"),
            big,
            _msg(0, tool_id="call-0"),
        ]
        for i in range(6):
            msgs.append(_msg(i, content="filler"))
        out = hook({"messages": msgs})["llm_input_messages"]
        stubbed = [
            m for m in out
            if any(
                (tc.get("id") or "") == "call-0"
                for tc in (getattr(m, "tool_calls", None) or [])
            )
        ]
        assert len(stubbed) == 1
        m = stubbed[0]
        assert m.additional_kwargs.get("usage") == {"tokens": 12}
        assert m.response_metadata.get("model") == "glm-4.7"

    def test_history_shrinks_for_a_draft_editing_writer(self):
        """The point of the fix: an old-turn-heavy history loses its draft
        byte weight (the pre-trim 435k shape collapses)."""
        hook = self._build_hook()

        def _total(msgs):
            return sum(
                len(str(getattr(m, "content", "")))
                + len(str(getattr(m, "tool_calls", None) or ""))
                for m in msgs
            )

        before = _total(self._history())
        after = _total(hook({"messages": self._history()})["llm_input_messages"])
        assert after < before * 0.5
