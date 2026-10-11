"""Wave-59 C2: the embed diet activates for FILE templates — and the pointer tells the truth.

R2 RCA: fresh writers carried the FULL 92,550-char http_navigation template in
their system prompt every turn (52-71% of the 180k input budget), collapsing
post-trim history to ~62k chars. The diet machinery (classify_embed +
render_writer_embed bounded mode) was built and PROVEN (the one bounded
invocation — 625's finisher — did the batch's only sustained repair), but
dormant for fresh writes: ``classify_embed(eow_active=_eow_active)`` returned
"full" whenever no edit-over-write base was live.

C2 wires fresh writes to the size-only golden (``eow_active=None``) and fixes
the W24-6 lie: templates/ IS present in the writer container (Dockerfile bake
+ compose bind, project-root-wide reads), so a bounded FILE embed must point
elided-region reads at the real template file — not at a workspace draft that
does not exist until the first write.

Run from repo root:  python3 -m pytest tests/test_wave59_embed_file_diet.py -v
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

from agents.draft_context import (  # noqa: E402
    classify_embed,
    render_writer_embed,
)

_REAL_TEMPLATE = os.path.join(
    ROOT, "templates", "http_navigation_scraper.py"
)


def _real_code() -> str:
    with open(_REAL_TEMPLATE, encoding="utf-8") as f:
        return f.read()


class TestClassifyOnSize:
    def test_big_file_template_classifies_bounded(self):
        """The real http_navigation template (92,550 chars) is bounded under
        the size-only golden — the exact R2 dormancy case."""
        assert classify_embed(_real_code(), eow_active=None) == "bounded"

    def test_small_code_stays_full(self):
        assert classify_embed("x = 1\n", eow_active=None) == "full"

    def test_eow_true_golden_unchanged(self):
        """EOW fix cycles keep the True golden: big + active → bounded,
        big + inactive → full (byte-identical legacy)."""
        code = _real_code()
        assert classify_embed(code, eow_active=True) == "bounded"
        assert classify_embed(code, eow_active=False) == "full"

    def test_threshold_is_40k(self):
        assert classify_embed("x" * 39_999, eow_active=None) == "full"
        assert classify_embed("x" * 40_000, eow_active=None) == "bounded"


class TestBoundedFileEmbedHonesty:
    def test_bounded_file_embed_points_at_real_template(self):
        """A dieted FILE embed must point elided-region reads at the REAL
        templates/ path (W24-6's 'does not exist' claim was false) — not at
        a workspace draft that doesn't exist until the first write."""
        out = render_writer_embed(
            "SYSTEM", _real_code(), mode="bounded",
            slug="jjshouse-com", embed_kind="file",
            template_file="http_navigation_scraper.py",
        )
        assert "templates/http_navigation_scraper.py" in out
        assert "does not exist in this container" not in out
        # the elision happened (embed is far smaller than the full template)
        assert len(out) < len(_real_code()) + 10_000

    def test_bounded_file_embed_carries_map_line_spans(self):
        """The map must index the elided machinery by symbol + line span."""
        out = render_writer_embed(
            "SYSTEM", _real_code(), mode="bounded",
            slug="jjshouse-com", embed_kind="file",
            template_file="http_navigation_scraper.py",
        )
        assert "Draft map" in out
        # real discovery machinery symbols survive as map rows (bodies elided)
        assert "_discover_urls_via_search" in out
        assert "_discover_urls_via_algolia" in out

    def test_bounded_file_embed_keeps_head_import(self):
        """`from src.algolia_discovery import` (line 135) survives in the
        head slice — the import the writer must KEEP is visible verbatim."""
        out = render_writer_embed(
            "SYSTEM", _real_code(), mode="bounded",
            slug="jjshouse-com", embed_kind="file",
            template_file="http_navigation_scraper.py",
        )
        assert "from src.algolia_discovery import" in out

    def test_bounded_draft_embed_keeps_legacy_pointer(self):
        """DRAFT-kind bounded embeds keep the legacy workspace pointer (the
        draft DOES exist on disk — 587's original contract)."""
        big = "def f():\n    pass\n" * 4_000  # ~68k chars, parses
        out = render_writer_embed(
            "SYSTEM", big, mode="bounded",
            slug="wonderbly-com", embed_kind="draft",
        )
        assert "workspace/wonderbly-com/scraper_draft.py" in out
        assert "templates/" not in out.split("NOTE:")[-1]

    def test_small_file_embed_is_full_legacy(self):
        """Below head+tail there is nothing to diet: full embed, byte-legacy,
        no file-kind notes."""
        small = "x = 1\n" * 100
        out = render_writer_embed(
            "SYSTEM", small, mode="bounded",
            slug="s", embed_kind="file", template_file="requests_scraper.py",
        )
        assert "read_file(path, line=" not in out
        assert "x = 1" in out


class TestW24L6NoteTruth:
    def test_full_embed_note_no_longer_lies(self):
        """The full-mode note dropped the false 'path does not exist' claim."""
        from agents.subagents import _embed_template

        out = _embed_template("SYSTEM", "x = 1\n")
        assert "does not exist in this container" not in out
        assert "COMPLETE file" in out
