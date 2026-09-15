"""Writer embed rendering — full vs bounded (the 587 embed diet).

[wave-32 D5] The edit-over-write base (``_template_code``) can be the entire
existing draft — 119KB in 587's case — re-embedded into the writer's system
prompt every fix cycle. ``render_writer_embed`` is the single dispatch:

- **full**: today's ``subagents._embed_template`` output, byte-for-byte
  (lazy import — this module must stay Django-free and cycle-free);
- **bounded**: the same frame + do-not-read note, but the code body is the
  head + tail slices with the middle elided, plus an ast DRAFT MAP indexing
  what was elided (``build_draft_map``), a pointer to the on-disk file, and
  the staleness/navigation contract. If the slices would cover the whole
  file there is nothing to diet and full is returned. If the draft does not
  PARSE (587's exact state) the slices degrade to raw character cuts and
  the map is dropped — never crash the embed.

Dormancy: the kill-switch (``CODE_WRITER_EMBED_MODE``, resolved in
``subagents``) defaults to ``full``, so bounded is dormant until wave-25e
E2a flips it. [wave-25e E1] adds ``build_draft_map`` + ``classify_embed``;
E2a owns the flip.
"""

from __future__ import annotations

import ast

FULL_MAX_CHARS = 40_000
HEAD_CHARS = 20_000
TAIL_CHARS = 20_000
MAP_MAX_CHARS = 6_000

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

_MAP_HEADER = (
    "### Draft map (line spans index the FULL file — middle body not shown)\n"
)

_MAP_TRUNCATED = "\n… (map truncated at the char cap — ask for specific symbols instead)\n"

_MAP_UNAVAILABLE = (
    "NOTE: map unavailable (draft does not parse — fix the parse error "
    "first; the syntax fixer window will carry the exact line).\n"
)

# [wave-25e E1] The staleness + navigation contract (plan §3.1.5). Under a
# bounded embed the writer edits a file it has only partially seen; this is
# the vocabulary edit_file's not-found reply (E6a) mirrors.
_STALENESS_NOTE = (
    "CONTRACT: the middle of the file is NOT in your context. To inspect or "
    "edit it: `search_content` for the symbol, then "
    "`read_file(path, line=<hit line>)` for the exact window. If `edit_file` "
    "reports `old_string not found`, your copy is STALE — re-read the window "
    "and retry the edit; do NOT rewrite the file from memory.\n"
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


def _signature_args(node) -> str:
    """Plain arg-name signature for a map row (annotations omitted)."""
    a = node.args
    names = [x.arg for x in (a.posonlyargs + a.args) if x.arg != "self"]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return ", ".join(names)


def _doc_head(node, width: int = 80) -> str:
    doc = ast.get_docstring(node) or ""
    doc = " ".join(doc.split())
    return doc[:width]


def build_draft_map(code: str, max_chars: int = MAP_MAX_CHARS) -> str:
    """[wave-25e E1] ast index of a draft's top-level surface: every
    ``def``/``class``/ALL-CAPS assignment as ``start-end  name  — dochead``,
    in file order, capped at ``max_chars``.

    Returns ``""`` on SyntaxError (or any parse failure) — an unparseable
    draft is a supported input state, not a crash. Never raises.
    """
    if not code:
        return ""
    try:
        tree = ast.parse(code)
    except Exception:
        return ""
    rows: list[str] = []
    for node in tree.body:
        start = getattr(node, "lineno", 0) or 0
        end = getattr(node, "end_lineno", start) or start
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            row = f"{start}-{end}  def {node.name}({_signature_args(node)})"
            doc = _doc_head(node)
            if doc:
                row += f" — {doc}"
        elif isinstance(node, ast.ClassDef):
            row = f"{start}-{end}  class {node.name}"
            doc = _doc_head(node)
            if doc:
                row += f" — {doc}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.Assign):
                names = [
                    t.id for t in node.targets
                    if isinstance(t, ast.Name) and t.id.isupper()
                ]
            else:
                t = node.target
                names = [t.id] if isinstance(t, ast.Name) and t.id.isupper() else []
            if not names:
                continue
            try:
                value_head = ast.unparse(node.value)[:40]
            except Exception:
                value_head = "…"
            row = f"{start}-{end}  {names[0]} = {value_head}"
        else:
            continue
        rows.append(row)
    body = "\n".join(rows)
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + _MAP_TRUNCATED


def classify_embed(
    code: str,
    *,
    eow_active: bool | None = True,
    full_max_chars: int = FULL_MAX_CHARS,
) -> str:
    """[wave-25e E1] The threshold decision D5 inlined at its call sites.

    - ``eow_active=True/False`` — the main call site's golden:
      bounded iff the edit-over-write base is live AND the code exceeds
      ``full_max_chars``;
    - ``eow_active=None`` — the finisher's golden: size alone (that window
      is definitionally post-death, with no EOW state to consult).

    The kill-switch is applied separately (``subagents._resolve_embed_mode``)
    — this helper only classifies the REQUEST.
    """
    _big = len(code or "") >= full_max_chars
    if eow_active is None:
        return "bounded" if _big else "full"
    return "bounded" if (eow_active and _big) else "full"


def render_writer_embed(
    system_prompt: str,
    code: str,
    *,
    mode: str = "full",
    slug: str = "",
    head: int = HEAD_CHARS,
    tail: int = TAIL_CHARS,
    map_max_chars: int = MAP_MAX_CHARS,
) -> str:
    """Embed ``code`` into the writer's system prompt.

    ``mode="full"`` is byte-identical to ``subagents._embed_template``;
    ``mode="bounded"`` elides the middle of a draft larger than
    ``head + tail`` (below that there is nothing to elide and full is
    returned — a small draft keeps the exact legacy embed) and indexes the
    elided region with the ast map.
    """
    if not code:
        return system_prompt
    if mode != "bounded" or len(code) <= head + tail:
        from .subagents import _embed_template  # lazy — avoids import cycle

        return _embed_template(system_prompt, code)
    map_body = build_draft_map(code, max_chars=map_max_chars)
    if map_body:
        elided_to = max(len(code) - tail, head)
        map_block = (
            _MAP_HEADER
            + map_body
            + "\n"
            + f"NOTE: chars {head:,}-{elided_to:,} are not shown; the map "
            f"above indexes the full file ({len(code.splitlines())} lines).\n"
        )
    else:
        map_block = _MAP_UNAVAILABLE
    return (
        system_prompt
        + _FRAME_HEADER
        + _slice_bounded(code, head, tail)
        + "\n```\n"
        + map_block
        + _DO_NOT_READ_NOTE
        + _pointer_note(slug)
        + _STALENESS_NOTE
    )
