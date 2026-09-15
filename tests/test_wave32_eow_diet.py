"""[wave-32 D5] Minimal edit-over-write embed diet — DORMANT by default.

A 119KB edit-over-write base re-embedded into the writer's system prompt
every fix cycle (587) dominates the context and pushes real history out of
the summarizer's reach. ``draft_context.render_writer_embed`` adds a
BOUNDED mode (same frame + head/tail slices + a pointer to the on-disk
file); the kill-switch ``CODE_WRITER_EMBED_MODE`` defaults to ``"full"``
so behavior is byte-identical today — W25-e E2a owns the flip.

Full mode MUST remain byte-equal to the legacy ``_embed_template`` output
(wave-24 pins stay green).
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
from django.test import override_settings  # noqa: E402

import webapp.agents.graph as g  # noqa: E402
import webapp.agents.subagents as sub  # noqa: E402
from webapp.agents import draft_context as dc  # noqa: E402

SP = "BASE SYSTEM PROMPT"

# A parseable draft bigger than head+tail (40K default).
LARGE_CODE = "\n".join(
    f"def _filler_{i}():\n    return {i}\n" for i in range(4000)
)
MIDDLE_MARK = "MIDDLE_SENTINEL_SHOULD_BE_ELIDED"
LARGE_CODE = (
    LARGE_CODE[: len(LARGE_CODE) // 2]
    + f"\nx = '{MIDDLE_MARK}'\n"
    + LARGE_CODE[len(LARGE_CODE) // 2 :]
)

BROKEN_LARGE = (
    "def broken(joined_marker:\n" + "# pad\n" * 12000 + "    return 1\n"
)

SMALL_CODE = "print('hi')\n"


class TestFullMode:
    def test_full_mode_output_matches_legacy_embed_template(self):
        code = "def a():\n    return 1\n"
        assert dc.render_writer_embed(SP, code, mode="full") == (
            sub._embed_template(SP, code)
        )

    def test_empty_code_returns_prompt_only(self):
        assert dc.render_writer_embed(SP, "", mode="full") == SP
        assert dc.render_writer_embed(SP, "", mode="bounded") == SP


class TestBoundedMode:
    def test_small_draft_keeps_full_embed(self):
        """Below the elision threshold there is nothing to diet — bounded
        must produce the exact legacy embed."""
        assert dc.render_writer_embed(
            SP, SMALL_CODE, mode="bounded"
        ) == sub._embed_template(SP, SMALL_CODE)

    def test_large_draft_gets_head_tail_with_pointer(self):
        out = dc.render_writer_embed(SP, LARGE_CODE, mode="bounded", slug="d5-com")
        assert SP in out
        assert "### Template" in out
        assert "do NOT" in out and "read_file templates/" in out
        # head and tail present, middle gone
        assert "def _filler_0()" in out
        assert "_filler_3999" in out
        assert MIDDLE_MARK not in out, "the middle must be elided"
        # pointer note
        assert "middle elided" in out
        assert "workspace/d5-com/scraper_draft.py" in out
        assert "search_content" in out and "read_file" in out
        assert len(out) < len(sub._embed_template(SP, LARGE_CODE))

    def test_unparseable_draft_degrades_to_raw_head_tail(self):
        """587's exact state — the draft does not parse. The embed must
        degrade to a raw head+tail slice, never crash."""
        out = dc.render_writer_embed(SP, BROKEN_LARGE, mode="bounded", slug="d5-com")
        assert "### Template" in out
        assert "middle elided" in out
        assert len(out) < len(SP) + len(BROKEN_LARGE)
        assert "def broken" in out  # head survived
        assert "return 1" in out  # tail survived

    def test_slices_are_line_snapped_when_parseable(self):
        out = dc.render_writer_embed(SP, LARGE_CODE, mode="bounded")
        head_section = out.split("middle elided")[0]
        raw_head = LARGE_CODE[: dc.HEAD_CHARS]
        partial_line = raw_head[raw_head.rfind("\n") + 1:]
        if partial_line.strip():
            assert partial_line + "\n" not in head_section or partial_line in (
                LARGE_CODE[: dc.HEAD_CHARS]
            ), "a parseable draft's head must end at a line boundary"
        # the partial line, if any, must not be the LAST line of the head
        last_line = [ln for ln in head_section.splitlines() if ln.strip()][-1]
        assert not last_line.startswith("_filler_") or last_line.endswith(":"), (
            "head must not end mid-function (raw character cut)"
        )


class TestKillSwitch:
    def test_embed_mode_kill_switch_lazy_read(self):
        """Mechanics only: the resolver reads settings AT CALL TIME and an
        explicit argument wins. The DEFAULT value is E2a's assertion to own
        — deliberately not pinned here."""
        with override_settings(CODE_WRITER_EMBED_MODE="bounded"):
            assert sub._resolve_embed_mode("") == "bounded"
        with override_settings(CODE_WRITER_EMBED_MODE="full"):
            assert sub._resolve_embed_mode("") == "full"
        with override_settings(CODE_WRITER_EMBED_MODE="bounded"):
            assert sub._resolve_embed_mode("full") == "full", (
                "an explicit mode from the call site wins over the switch"
            )

    def test_resolver_downgrades_bounded_request_when_switch_is_full(self):
        """Dormancy contract: the wiring may REQUEST bounded, but with the
        switch at "full" the effective mode is full — byte-identical."""
        with override_settings(CODE_WRITER_EMBED_MODE="full"):
            assert sub._resolve_embed_mode("bounded") == "full"


class TestWiring:
    def test_first_cycle_still_full_embed(self):
        """[EOW boundary, main call site] bounded is requested ONLY when
        the edit-over-write base is live AND the base exceeds the budget —
        a first cycle (no EOW) is always full."""
        import inspect

        body = inspect.getsource(g._invoke_code_writer)
        # the requested mode is the conditional: BOTH the EOW flag and the
        # size budget helper gate it, and it rides the create call
        assert "_embed_mode = (" in body
        assert "_eow_active" in body.split("_embed_mode = (", 1)[1].split(")", 1)[0]
        assert "_embed_full_max_chars()" in body
        assert "embed_mode=_embed_mode" in body

    def test_finisher_conditions_on_size_alone(self):
        """[finisher call site] no _eow_active in scope there — bounded is
        requested on draft SIZE alone."""
        import inspect

        body = inspect.getsource(g._run_draft_finisher)
        assert "embed_mode=" in body
        assert "_embed_full_max_chars()" in body

    def test_settings_declared(self):
        """The three D5 settings exist with the planned defaults."""
        from django.conf import settings

        assert getattr(settings, "CODE_WRITER_EMBED_FULL_MAX_CHARS", None) == 40_000
        assert getattr(settings, "CODE_WRITER_EMBED_HEAD", None) == 20_000
        assert getattr(settings, "CODE_WRITER_EMBED_TAIL", None) == 20_000

    def test_head_tail_settings_honored_by_resolver(self):
        with override_settings(CODE_WRITER_EMBED_HEAD=50, CODE_WRITER_EMBED_TAIL=50):
            out = dc.render_writer_embed(SP, LARGE_CODE, mode="bounded", slug="d5-com")
        # tiny head/tail → a very short embed that still carries both ends
        assert "def _filler_0" in out
        assert "_filler_3999" in out
        assert MIDDLE_MARK not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
