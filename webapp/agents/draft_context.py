"""Writer embed rendering — full vs bounded (the 587 embed diet).

[wave-32 D5] The edit-over-write base (``_template_code``) can be the entire
existing draft — 119KB in 587's case — re-embedded into the writer's system
prompt every fix cycle. ``render_writer_embed`` is the single dispatch:

- **full**: today's ``subagents._embed_template`` output, byte-for-byte
  (lazy import — this module must stay Django-free and cycle-free);
- **bounded**: the same frame + do-not-read note, but the code body is the
  head + tail slices with the middle elided, plus a pointer to the on-disk
  file. If the slices would cover the whole file there is nothing to diet
  and full is returned. If the draft does not PARSE (587's exact state) the
  slices degrade to raw character cuts — never crash the embed.

Dormancy: the kill-switch (``CODE_WRITER_EMBED_MODE``, resolved in
``subagents``) defaults to ``full``, so bounded is dormant until wave-25e
E2a flips it. No AST map in this wave — wave-25e E1 extends this module.
"""

from __future__ import annotations

FULL_MAX_CHARS = 40_000
HEAD_CHARS = 20_000
TAIL_CHARS = 20_000

_Elision = "\n# ... [middle elided — see the pointer below] ...\n"

_FRAME_HEADER = (
    "\n\n### Template (use VERBATIM — do not rewrite discovery/pagination)\n"
    "The template below is the scraper skeleton. Fill in the site-specific "
    "parts (EXTRACT_PRODUCT_URLS_JS selectors, field extraction). KEEP the "
    "`from src.discovery import ...` line, the `discover_item_urls(...)` call, "
    "the argparse, and the env-var gate UNCHANGED. Do NOT define "
    "`_click_load_more`, `_get_next_page_url`, or any pagination loop inline.\n\n"
    "```python\n"
)

_DO_NOT_READ_NOTE = (
    "NOTE: the template above is embedded in this prompt — do NOT "
    "read_file templates/*.py; that path does not exist in this container.\n"
)


def _pointer_note(slug: str) -> str:
    _path = f"workspace/{slug or '<site_slug>'}/scraper_draft.py"
    return (
        "NOTE: only the HEAD and TAIL of the draft are embedded above — the "
        f"middle elided — full file at {_path}; use search_content / "
        "read_file(line=) to inspect any region before editing it.\n"
    )


def _slice_bounded(code: str, head: int, tail: int) -> str:
    """Head+tail slices; line-snapped when the draft parses, raw otherwise."""
    try:
        compile(code, "<draft>", "exec")
    except Exception:
        return code[:head] + _Elision + code[-tail:]
    h = code[:head]
    nl = h.rfind("\n")
    if nl != -1:
        h = h[: nl + 1]
    t = code[-tail:]
    nl = t.find("\n")
    if nl != -1:
        t = t[nl + 1 :]
    return h + _Elision + t


def render_writer_embed(
    system_prompt: str,
    code: str,
    *,
    mode: str = "full",
    slug: str = "",
    head: int = HEAD_CHARS,
    tail: int = TAIL_CHARS,
) -> str:
    """Embed ``code`` into the writer's system prompt.

    ``mode="full"`` is byte-identical to ``subagents._embed_template``;
    ``mode="bounded"`` elides the middle of a draft larger than
    ``head + tail`` (below that there is nothing to elide and full is
    returned — a small draft keeps the exact legacy embed).
    """
    if not code:
        return system_prompt
    if mode != "bounded" or len(code) <= head + tail:
        from .subagents import _embed_template  # lazy — avoids import cycle

        return _embed_template(system_prompt, code)
    return (
        system_prompt
        + _FRAME_HEADER
        + _slice_bounded(code, head, tail)
        + "\n```\n"
        + _DO_NOT_READ_NOTE
        + _pointer_note(slug)
    )
