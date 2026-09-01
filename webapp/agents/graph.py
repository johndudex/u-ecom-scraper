"""Main LangGraph assembly for the Universal Ecommerce Scraper.

Builds a ``StateGraph[ScrapeState]`` that orchestrates the full scraping
pipeline: command parsing → tracker check → workspace setup → site analysis →
product analysis → code generation → testing → execution → cleanup → skill
learning.

Each LLM-powered phase (site_analyzer, product_analyzer, code_writer,
code_tester, cleanup, skill_learner) is a ``create_react_agent`` subgraph
produced by the factories in ``subagents.py``.  Deterministic nodes come from
``nodes/`` and handle routing, validation, approval, and artifact management.

Human-in-the-loop is handled via ``langgraph.types.interrupt()`` inside
specific nodes (check_tracker, validate_analysis, validate_coverage,
field_confirmation, human_approval).  The graph pauses at these points and
resumes when the user provides input.

Note: ``pre_execution_approval`` was a second consecutive gate after
``field_confirmation`` (Wave 2 Cut 2 merged it into field_confirmation — the
item-count estimate now appears in field_confirmation's interrupt and an approve
there routes straight to run_execution). The node is no longer registered here.

The compiled graph is stateful — checkpointed to PostgreSQL via
``checkpointer.py`` — so jobs can be resumed after interrupts.

Usage::

    from webapp.agents.graph import build_scrape_graph

    graph = build_scrape_graph()
    result = graph.invoke({
        "url": "https://www.nike.com",
        "sample_only": True,
    })
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import functools
from typing import Any, Optional
from django.utils import timezone

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from langchain_core.runnables import RunnableConfig

from .constants import (
    FINAL_RETRY_SENTINEL,
    MAX_TEST_RETRIES,
    STEALTH_METHOD_PREFIXES,
)
from .decisions import options_to_decisions
from .nodes import (
    check_tracker,
    field_confirmation,
    human_approval,
    normalize_fields,
    parse_command,
    route_after_cleanup,
    route_after_testing,
    run_execution,
    setup_workspace,
    update_tracker_analysis,
    validate_analysis,
    validate_coverage,
)
from .nodes.run_execution import (
    _find_newest_output,
    _read_discovery_coverage,
    _substantive_item_count,
)
from .state import ScrapeState
from .subagents import (
    build_cleanup_message,
    build_code_tester_message,
    build_code_writer_message,
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # build_navigation_agent_message,
    # ═══ END ARCHIVED ═══
    build_product_analyzer_message,
    build_site_analyzer_message,
    build_dagster_converter_message,
    create_cleanup_agent,
    create_code_tester,
    create_code_writer,
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # create_navigation_agent,
    # ═══ END ARCHIVED ═══
    create_product_analyzer,
    create_site_analyzer,
    create_skill_learner,
    build_skill_learner_message,
    create_dagster_converter,
)
from .tools.context import (
    set_tool_context,
    clear_tool_context,
    set_tool_deadline,
    get_tool_deadline,
)

logger = logging.getLogger(__name__)

# Default per-agent recursion limit (langgraph counts each model+tool step).
# Raised from 100 -> 150: complex sites (e.g. AMN job-field mapping) legitimately
# exceed 100 steps.  Map entries above override per agent.  If an agent STILL
# exceeds its limit, GraphRecursionError is caught in services.py/tasks.py and
# converted to a human_approval (graceful pause) rather than failing the job.
AGENT_RECURSION_LIMIT = 150
API_MAX_RETRIES = 3
API_RETRY_DELAYS = [5, 15, 30]

# Debug toggle: when False, the post-generation scraper patches AND the
# analysis-level strategy overrides are SKIPPED, so `run_node --no-patches`
# shows the raw LLM output. Lets us prove a source-level fix makes a patch
# redundant before deleting the patch. Production default is True.
_PATCHES_ENABLED = True


def _scan_json_prefix(content: str, upto: int) -> tuple[list[str], bool]:
    """Track JSON structure over ``content[:upto]``.

    Returns ``(closers, in_string)`` where ``closers`` is the stack of still
    required closers (innermost first) and ``in_string`` says whether the scan
    ended inside an unterminated string literal.
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for c in content[:upto]:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "{[":
                stack.append("}" if c == "{" else "]")
            elif c in "}]":
                if stack:
                    stack.pop()
    return stack, in_str


def _trim_partial_json(head: str) -> str:
    """Trim a raw JSON prefix back to the end of its last COMPLETE element.

    Walks backward past any trailing whitespace/comma, then drops a dangling
    fragment — ``"key"`` with no value, ``"key":`` with no value, or an
    unterminated/invalid value (``"key": 25 offices``, ``"key": "unesc``) —
    repeatedly, so what remains is either empty or ends on a complete value.
    """
    while True:
        h = head.rstrip()
        if not h:
            return h
        if h.endswith(","):
            head = h[:-1]
            continue
        # find the character that ends the last syntactic unit
        m = len(h) - 1
        while m >= 0 and h[m] in " \t\r\n":
            m -= 1
        if m < 0:
            return ""
        # Case A: the last complete thing is a VALUE (ends with a non-separator)
        if h[m] not in ",:":
            return h
        # Case B: "key": with nothing after it — drop the whole pair
        if h[m] == ":":
            # walk back over the key's closing quote
            k = m - 1
            while k >= 0 and h[k] in " \t\r\n":
                k -= 1
            if k < 0:
                return ""
            if h[k] != '"':
                # not a simple string key (numeric/keyed var) — bail out safe
                return h[: m].rstrip().rstrip(",")
            # find the key's opening quote
            j = k - 1
            while j >= 0 and h[j] != '"':
                j -= 1
            head = h[:j]
            continue
        # h[m] == "," handled above; unreachable
        return h


def _balanced_close(content: str, err: json.JSONDecodeError) -> str:
    """Repair by closing the unclosed structure at the strict-parse error.

    Re-parse from the TOP, keeping the largest prefix that is structurally
    coherent, then append exactly the closers the bracket/quote stack demands.
    When the error lands inside a malformed string value (the sidley/job-10
    class: an unquoted scalar or an unescaped quote), the cut is rewound past
    the enclosing ``"key":`` pair so no half-written value survives.
    """
    cut = min(err.pos, len(content))
    stack, in_str = _scan_json_prefix(content, cut)
    if in_str:
        # Rewind to the opening quote of the malformed string...
        j = cut - 1
        while j >= 0 and content[j] != '"':
            j -= 1
        # ...and if that string is a VALUE (preceded by ':'), drop its key too.
        m = j - 1 if j > 0 else -1
        while m >= 0 and content[m] in " \t\r\n":
            m -= 1
        if m >= 0 and content[m] == ":":
            m2 = m - 1
            while m2 >= 0 and content[m2] in " \t\r\n":
                m2 -= 1
            if m2 >= 0 and content[m2] == '"':
                j = m2
        cut = j
        stack, _ = _scan_json_prefix(content, cut)
    head = _trim_partial_json(content[:cut])
    # re-derive the stack against the trimmed head (trimming may have removed
    # structure tokens, e.g. an opening quote of a dropped key)
    stack, _ = _scan_json_prefix(head, len(head))
    candidate = head + "".join(reversed(stack))
    # A trailing bare scalar (unquoted value — the sidley shape) parses but is
    # NOT the data the model wrote. Drop that pair and retry once so only
    # complete, correctly-quoted elements survive the salvage.
    if len(stack) == 1 and stack[0] == "}":
        # Does the final element end with a bare token that only parses
        # accidentally (``"bad": 25 offices with counts`` → parses as 25)?
        m = len(head) - 1
        while m >= 0 and head[m] in " \t\r\n":
            m -= 1
        # find the last key colon at the ROOT depth (inside the outermost
        # object only — deeper elements belong to their own closed objects)
        depth = 0
        in_s = False
        esc2 = False
        last_sep = -1
        for i2, c2 in enumerate(head[: m + 1]):
            if in_s:
                if esc2:
                    esc2 = False
                elif c2 == "\\":
                    esc2 = True
                elif c2 == '"':
                    in_s = False
            else:
                if c2 == '"':
                    in_s = True
                elif c2 in "{[":
                    depth += 1
                elif c2 in "}]":
                    depth -= 1
                elif c2 in ",:" and depth == 1:
                    last_sep = i2
        if last_sep >= 0 and head[last_sep] == ":":
            k2 = last_sep - 1
            while k2 >= 0 and head[k2] in " \t\r\n":
                k2 -= 1
            if k2 >= 0 and head[k2] == '"':
                j = k2 - 1
                while j >= 0 and head[j] != '"':
                    j -= 1
                head2 = _trim_partial_json(head[:j])
                stack2, _ = _scan_json_prefix(head2, len(head2))
                cand2 = head2 + "".join(reversed(stack2))
                try:
                    probe2 = json.loads(cand2)
                    if isinstance(probe2, dict) and probe2:
                        return cand2
                except Exception:
                    pass
    return candidate


def repair_json_text(content: str) -> tuple[Optional[str], str]:
    """In-memory artifact repair (pure function — no filesystem access).

    Runs the pass ladder described on ``_fix_json_artifact`` and returns
    ``(repaired_text, note)``; ``(None, "")`` when nothing repairs. Used both
    by the phase-exit repair and by the M4 copy-path guards, which must repair
    bytes in memory rather than mutating a workspace file.
    """
    # pass 0: already valid?
    try:
        json.loads(content)
        return content, ""
    except json.JSONDecodeError as e:
        err: Optional[json.JSONDecodeError] = e
    # pass 0b: C1 — literal control chars, repaired losslessly
    try:
        parsed = json.loads(content, strict=False)
        return (
            json.dumps(parsed, indent=1, ensure_ascii=False),
            "pass 0b: repaired control characters (strict=False)",
        )
    except json.JSONDecodeError:
        pass
    # pass 1: bad escapes (C4)
    try:
        fixed = re.sub(r'(?<=[^\\])\\(?!["\\/bfnrtu])', r"\\\\", content)
        json.loads(fixed)
        return fixed, "pass 1: fixed bad escapes"
    except Exception:
        pass
    # pass 2: salvage the largest parseable leading object (C3 prefix)
    try:
        dec = json.JSONDecoder()
        obj, _end = dec.raw_decode(content)
        if isinstance(obj, dict) and obj:
            return (
                json.dumps(obj, indent=1, ensure_ascii=False),
                "pass 2: salvaged truncated object (valid prefix kept)",
            )
    except Exception:
        pass
    # pass 2b: balanced-closer salvage bounded by the error position (C2)
    if err is not None:
        try:
            candidate = _balanced_close(content, err)
            obj = json.loads(candidate)
            if isinstance(obj, dict) and obj:
                return (
                    json.dumps(obj, indent=1, ensure_ascii=False),
                    f"pass 2b: balanced-closer salvage (recovered {len(obj)} "
                    f"top-level keys; dropped content after char {err.pos})",
                )
        except Exception:
            pass
    # pass 3: coarse truncation sweep (last resort)
    try:
        step = max(1, len(content) // 200)
        for cut in range(len(content) - 1, 0, -step):
            head = content[:cut].rstrip().rstrip(",")
            for closer in ("}", "]}"):
                try:
                    obj = json.loads(head + closer)
                    if isinstance(obj, dict) and obj:
                        return (
                            json.dumps(obj, indent=1, ensure_ascii=False),
                            "pass 3: salvaged by truncation sweep",
                        )
                except Exception:
                    continue
    except Exception:
        pass
    return None, ""


def _fix_json_artifact(slug: str, filename: str) -> None:
    """Repair-on-write guard (job-10 lesson): a corrupt artifact silently
    became {} downstream and the writer lost the field map AND the verify
    note. Still-failing files are RENAMED *.corrupt so downstream sees
    missing (-> rerun path), never silently-empty.

    Passes (each logs which pass succeeded):
      0  valid as-is (strict)
      0b strict=False — C1 literal control chars, repaired losslessly
      1  bad-escape rewrite — C4
      2  raw_decode salvage of a leading object — C3 prefix
      2b balanced-closer bounded by the error position — C2 (sidley/job-10).
          The old pass 3 stepped from the END by len//200 trying only two
          hard-coded closers and recovered NOTHING on the real sidley file;
          the balanced closer recovers 9/10 top-level keys + all 36 field
          mappings there.
      3  coarse truncation sweep — last resort, unchanged
    """
    if not slug:
        return
    try:
        root = _get_project_root()
    except Exception:
        return
    path = os.path.join(root, "workspace", slug, filename)
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return

    try:
        json.loads(content)
        return  # valid as-is
    except json.JSONDecodeError:
        pass

    repaired, note = repair_json_text(content)
    if repaired is not None:
        # validate BEFORE writing — never persist a guess that doesn't parse
        json.loads(repaired)
        with open(path, "w", encoding="utf-8") as f:
            f.write(repaired)
        logger.warning("_fix_json_artifact: %s (%s)", note, path)
        return
    # unrepairable: rename so downstream treats as MISSING (rerun path)
    try:
        os.replace(path, path + ".corrupt")
        logger.error(
            "_fix_json_artifact: %s unrepairable — renamed .corrupt "
            "(downstream treats as missing)", path,
        )
    except OSError:
        pass


def _enforce_anti_bot_strategy(analysis: dict, slug: str, filename: str) -> dict:
    """For anti-bot sites, force the strategy fields to ``http_navigation`` (cloak).

    Bot protection (Akamai/Cloudflare/PerimeterX) guards **API endpoints too**,
    not just HTML pages — a discovered ``internal_api``/``http_requests`` strategy
    403/400s exactly like direct HTTP does (verified: calvklein's b2c-api returns
    400). So the only reliable strategy for an anti-bot site is a browser-backed
    one. ``http_navigation`` is the preferred anti-bot strategy because the
    browser_service ``/navigate`` endpoint supports ``stealth: "cloak"`` per call
    (CloakBrowser's C++ fingerprint patches defeat Akamai). The legacy
    ``playwright`` strategy is NOT in ``_bad`` — it stays for back-compat.

    KEPT after verify-then-delete: `run_node code_writer` showed code_writer picks
    ``internal_api`` for anti-bot sites even with the strengthened prompt, producing
    a non-working scraper. Generic — driven by the anti_bot signal, no site names.
    """
    if not isinstance(analysis, dict) or not slug:
        return analysis
    conn = analysis.get("connectivity") or {}
    anti_bot = analysis.get("anti_bot") or {}
    method = (conn.get("method_that_worked") if isinstance(conn, dict) else "") or ""
    detected = bool(
        (isinstance(anti_bot, dict) and anti_bot.get("detected"))
        or str(method).startswith(STEALTH_METHOD_PREFIXES)
    )
    if not detected:
        return analysis
    # Strategies that won't work behind bot protection → http_navigation (cloak).
    # NOTE: ``playwright`` is deliberately NOT in ``_bad`` — it is retained as a
    # legacy browser strategy. Only the explicitly-bad tokens (UC, HTTP/API) are
    # rewritten to ``http_navigation``, the new preferred anti-bot strategy
    # (the /navigate endpoint applies cloak server-side via ``stealth: "cloak"``).
    _bad = ("seleniumbase", "undetected", "stealth_browser", "uc_chrome",
            "internal_api", "http_requests", "requests", "api")
    _keys = ("scraping_mechanism", "scraping_method", "strategy",
             "recommended_strategy", "mechanism")
    changed = False

    def _rewrite(d: dict) -> None:
        nonlocal changed
        for k, v in list(d.items()):
            if isinstance(v, str) and any(t in v.lower() for t in _bad):
                d[k] = "http_navigation"
                changed = True

    _rewrite(analysis)
    mr = analysis.get("mechanism_reassessment")
    if isinstance(mr, dict):
        _rewrite(mr)
    if not changed:
        return analysis
    try:
        path = os.path.join(_get_project_root(), "workspace", slug, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        logger.info("_enforce_anti_bot_strategy: anti-bot → http_navigation in %s/%s", slug, filename)
    except Exception as exc:
        logger.warning("_enforce_anti_bot_strategy: %s", exc)
    return analysis


def _patch_scraper_output_filter(
    slug: str, content_type: str = "", target_fields: list | None = None
) -> None:
    """Insert a content-type-aware output filter in scraper_draft.py.

    Discovery can capture non-item pages (nav/category roots, soft-404s). This
    filter drops them before the output is written: keep items that have a
    ``title`` AND at least one of the content type's core fields. For ``product``
    that's price/availability (unchanged from the old price filter); for
    ``job_posting`` it's company/location; for ``article`` author/publish_date;
    unknown types keep every item with a title. GENERIC — field set comes from
    ``src.content_types.output_filter_fields``, no per-type hardcoding here.
    """
    if not slug:
        return
    try:
        root = _get_project_root()
    except Exception:
        return
    scraper_path = os.path.join(root, "workspace", slug, "scraper_draft.py")
    if not os.path.isfile(scraper_path):
        return
    try:
        with open(scraper_path, "r", encoding="utf-8") as f:
            code = f.read()
        if "_OUTPUT_FILTER_APPLIED" in code or "_OUTPUT_PRICE_FILTER_APPLIED" in code:
            return
        if target_fields:
            # Custom schema: keep items with ANY of the user's requested fields.
            # Don't require title/price (product-specific) — that would strip
            # every record on non-product sites (profiles, jobs, articles).
            checks = " or ".join(f"p.get({f!r})" for f in target_fields)
            cond = checks
            label = f"any of {','.join(target_fields)}"
            fields = list(target_fields)
        else:
            from src.content_types import output_filter_fields

            fields = [f for f in output_filter_fields(content_type) if isinstance(f, str)]
            if fields:
                checks = " or ".join(f"p.get({f!r})" for f in fields)
                cond = f"p.get('title') and ({checks})"
                label = f"title+{','.join(fields)}"
            else:
                cond = "p.get('title')"
                label = "title"
        # The injected block resolves the output key ITSELF: 5 of 9 template
        # families never define OUTPUT_KEY, and the old injected reference
        # NameError'd into the bare except → the filter silently no-oped
        # (job 10's exact blank-row mechanism). Resolution order: the
        # template's OUTPUT_KEY if defined, else the first list-of-dicts
        # value in `output` (the actual item array, whatever it's called).
        filter_code = (
            "# _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)\n"
            f"_FILTER_FIELDS = {fields!r}\n"
            "try:\n"
            "    _OUTPUT_KEY = OUTPUT_KEY if 'OUTPUT_KEY' in dir() else next(\n"
            "        (k for k, v in output.items() if isinstance(v, list)\n"
            "         and v and isinstance(v[0], dict)), None)\n"
            "    if _OUTPUT_KEY:\n"
            "        _before = len(output[_OUTPUT_KEY])\n"
            f"        output[_OUTPUT_KEY] = [p for p in output[_OUTPUT_KEY] if {cond}]\n"
            "        _after = len(output[_OUTPUT_KEY])\n"
            "        if _before != _after:\n"
            f"            logger.info('output filter: %d → %d items (removed %d without {label})',\n"
            "                         _before, _after, _before - _after)\n"
            "except Exception:\n"
            "    pass\n"
            "\n"
        )
        # Insert before `json.dump(output` (fallback: json.dump( / output_filename)
        marker = "json.dump(output"
        idx = code.find(marker)
        if idx < 0:
            marker = "json.dump("
            idx = code.find(marker)
        if idx < 0:
            marker = "output_filename"
            idx = code.find(marker)
        if idx > 0:
            line_start = code.rfind("\n", 0, idx) + 1
            indent = code[line_start:idx]
            indented_filter = "\n".join(
                indent + line if line else line for line in filter_code.split("\n")
            )
            code = code[:line_start] + indented_filter + "\n" + code[line_start:]
            with open(scraper_path, "w", encoding="utf-8") as f:
                f.write(code)
            logger.info(
                "_patch_scraper_output_filter: inserted filter (%s) for content_type=%s",
                label, content_type or "(unknown)",
            )
        else:
            logger.warning("_patch_scraper_output_filter: could not find output write location")
    except Exception as exc:
        logger.warning("_patch_scraper_output_filter: %s", exc)


def _enforce_discovery_import(slug: str) -> None:
    """Post-generation enforcement: ensure the generated scraper imports src.discovery.

    Modeled on _patch_scraper_output_filter (string-marker injection + idempotency
    sentinel). code_writer frequently drops the `from src.discovery import` line
    and hand-rolls inline pagination (verified across lw.com scraper-177/179/181).
    Prompt rules mandate keeping it but cannot reliably constrain codegen — this
    deterministic backstop catches the drift after generation and injects the
    import if missing.
    """
    if not slug:
        return
    try:
        draft_path = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
        if not os.path.isfile(draft_path):
            return
        with open(draft_path, "r", encoding="utf-8") as f:
            code = f.read()

        # Already compliant?
        if "from src.discovery import" in code and "discover_item_urls(" in code:
            return

        # Hand-rolled pagination detected (inline _click_load_more / _get_next_page_url)?
        has_inline_pagination = any(
            marker in code
            for marker in ("def _click_load_more", "def _get_next_page_url", "_click_load_more(page)")
        )

        if has_inline_pagination:
            logger.warning(
                "_enforce_discovery_import: %s has INLINE pagination (_click_load_more/"
                "_get_next_page_url defined) instead of src.discovery import — "
                "code_writer drifted from the template. Injecting the import as a "
                "backstop, but the inline code may still break.",
                slug,
            )

        # Inject the import after the last `from src.` or `from playwright` line
        # (top of file, column 0 — no indent needed).
        import_line = (
            "from src.discovery import discover_item_urls, config_for_load_more  "
            "# _DISCOVERY_IMPORT_APPLIED (enforced — do not remove)"
        )
        if "from src.discovery import" not in code:
            # Find insertion point: after last top-level import
            last_import = max(
                code.rfind("\nfrom src."),
                code.rfind("\nfrom playwright"),
                code.rfind("\nimport "),
            )
            if last_import > 0:
                # Insert after the import line (find the newline after it)
                line_end = code.find("\n", last_import + 1)
                if line_end > 0:
                    code = code[:line_end + 1] + import_line + "\n" + code[line_end + 1:]
                else:
                    code = import_line + "\n" + code
            else:
                code = import_line + "\n" + code
            logger.info("_enforce_discovery_import: injected src.discovery import into %s", slug)

        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as exc:
        logger.warning("_enforce_discovery_import: %s", exc)


def _enforce_env_discovery_gate(slug: str) -> None:
    """Post-generation enforcement: ensure the SCRAPER_LISTING_URL env-var gate
    has its initializer lines.

    code_writer drops the ``_env_listing = os.environ.get("SCRAPER_LISTING_URL"…)``
    and ``_env_force = …`` assignments but KEEPS the consumer
    (``if/elif _env_listing or _env_force or args.fresh_discovery…``). Any
    ``--listing-url`` / ``--fresh-discovery`` / ``SCRAPER_LISTING_URL`` invocation
    then raises ``NameError`` before discovery runs (verified: lw.com scraper-187
    crashed at run_execution, exit code 1 in 0s). Invisible to code_tester
    because ``--sample`` takes a different branch — the classic scratch-vs-exec
    blind-spot. Modeled on _enforce_discovery_import.
    """
    if not slug:
        return
    try:
        draft_path = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
        if not os.path.isfile(draft_path):
            return
        with open(draft_path, "r", encoding="utf-8") as f:
            code = f.read()

        # Already compliant?
        if "_ENV_GATE_APPLIED" in code or '_env_listing = os.environ.get("SCRAPER_LISTING_URL"' in code:
            return

        # Find the consumer: the if/elif that references _env_listing / _env_force.
        consumer_re = re.compile(r"(?m)^(\s*)(elif|if)\s+_env_listing\b.*:\s*$")
        m = consumer_re.search(code)
        if not m:
            return  # no env-gate consumer → nothing to enforce
        indent, kw = m.group(1), m.group(2)

        # If it's an `elif`, the initializers must be defined before the WHOLE
        # if/elif chain evaluates, so insert before the chain's leading `if`.
        insert_pos = m.start()
        if kw == "elif":
            chain_re = re.compile(r"(?m)^" + re.escape(indent) + r"if\b.*:\s*$")
            chain_matches = list(chain_re.finditer(code, 0, m.start()))
            if chain_matches:
                insert_pos = chain_matches[-1].start()

        init_lines = (
            f'{indent}_env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()'
            "  # _ENV_GATE_APPLIED (enforced — do not remove)\n"
            f'{indent}_env_force = os.environ.get("SCRAPER_FORCE_DISCOVERY", "").strip().lower() in ("1", "true", "yes")\n'
        )
        code = code[:insert_pos] + init_lines + code[insert_pos:]
        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(code)
        logger.info("_enforce_env_discovery_gate: injected env-var initializers into %s", slug)
    except Exception as exc:
        logger.warning("_enforce_env_discovery_gate: %s", exc)


def _warn_unaddressed_critical_fix(slug: str, scraper_analysis: dict) -> None:
    """Deterministic backstop for the critical_fix loop.

    After code_writer regenerates ``scraper_draft.py``, check whether any
    selector documented as non-existent in ``scraper_analysis.critical_fix``
    still appears in the generated code. If it does, log a prominent warning —
    the documented defect was not addressed and the next code_tester run will
    almost certainly crash the same way.

    This does NOT mutate the scraper (selector choice is the LLM's call); it
    surfaces the regression loudly so it is visible in logs and the code_review
    loop can act on it. GENERIC — runs for any site whose analyzer wrote a
    ``critical_fix`` block.
    """
    if not slug or not isinstance(scraper_analysis, dict):
        return
    critical_fix = scraper_analysis.get("critical_fix") or {}
    if not isinstance(critical_fix, dict) or not critical_fix:
        return
    try:
        root = _get_project_root()
    except Exception:
        return
    scraper_path = os.path.join(root, "workspace", slug, "scraper_draft.py")
    if not os.path.isfile(scraper_path):
        return
    try:
        with open(scraper_path, "r", encoding="utf-8") as f:
            code = f.read()
        # Extract the FAILED selector from the crash message in `issue` — the
        # pattern is `selector 'X'` / `selector "X"` (Playwright/Selenium
        # wording). That selector is definitively broken on the page.
        import re as _re

        blob = " ".join(
            str(critical_fix.get(k, ""))
            for k in ("issue", "root_cause", "fix")
            if critical_fix.get(k)
        )
        # Mark selectors flagged as non-existent; the issue text usually reads
        # "DOES NOT EXIST" / "does not exist" near the broken selector.
        forbidden: list[str] = []
        for m in _re.finditer(r"selector\s*['\"]([^'\"]+)['\"]", blob, flags=_re.IGNORECASE):
            forbidden.append(m.group(1))
        # Dedupe while preserving order.
        seen: set[str] = set()
        forbidden = [s for s in forbidden if not (s in seen or seen.add(s))]
        if not forbidden:
            return
        offenders = [s for s in forbidden if s in code]
        if offenders:
            logger.warning(
                "_warn_unaddressed_critical_fix: %s STILL CONTAINS documented-"
                "non-existent selector(s) %s despite critical_fix — code_tester "
                "will likely crash again (slug=%s)",
                os.path.basename(scraper_path), offenders, slug,
            )
        else:
            logger.info(
                "_warn_unaddressed_critical_fix: OK — documented-non-existent "
                "selector(s) %s absent from regenerated scraper (slug=%s)",
                forbidden, slug,
            )
    except Exception as exc:
        logger.warning("_warn_unaddressed_critical_fix: %s", exc)




def _load_test_report(slug: str, min_mtime: float | None = None) -> dict | None:
    """Load the test report JSON from the agent's workspace folder.

    ``min_mtime`` (epoch seconds): when given, a report whose file mtime
    PREDATES it is rejected as stale — [job-81] a dead code_tester invocation
    must never adopt the PREVIOUS cycle's verdict (its "cascade exhausted"
    routing fired on a report written 70 minutes earlier while a fresh draft
    sat unjudged). The report for THIS attempt must have been written during
    THIS attempt.
    """
    if not slug:
        return None
    report_path = os.path.join("workspace", slug, "test_report.json")
    if not os.path.isfile(report_path):
        try:
            from django.conf import settings

            report_path = os.path.join(
                settings.PROJECT_ROOT, "workspace", slug, "test_report.json"
            )
        except Exception:
            pass
    if not os.path.isfile(report_path):
        return None
    if min_mtime is not None:
        try:
            _mtime = os.path.getmtime(report_path)
            if _mtime < min_mtime:
                logger.warning(
                    "_load_test_report: report mtime %.0f predates the current "
                    "test attempt (%.0f) — rejecting as stale",
                    _mtime, min_mtime,
                )
                return None
        except OSError:
            pass
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("_load_test_report: failed to parse %s: %s", report_path, exc)
    return None


def _attach_discovery_coverage(report: dict, slug: str) -> dict:
    """Deterministically attach the scraper's ``discovery_coverage`` to the test report.

    code_tester's LLM-written ``test_report.json`` does not reliably carry the
    ``discovery_coverage`` block the scraper emits in its output metadata. Read it
    from the test scrape's output file and inject it so the coverage-aware
    classifier (``route_after_testing._discovery_coverage_failure``) can see it.
    No-op when there is no output file or no block (url_list scrapers, Phase 1 not
    run) — keeps the gate dormant rather than erroring.
    """
    if not isinstance(report, dict) or not slug:
        return report
    try:
        from django.conf import settings

        root = settings.PROJECT_ROOT
    except Exception:
        root = "."
    workspace_dir = os.path.join(root, "workspace", slug)
    site_dir = os.path.join(root, "scrapers", slug)
    try:
        output_file = _find_newest_output(workspace_dir, site_dir, slug=slug)
    except Exception as exc:
        logger.debug("_attach_discovery_coverage: newest-output lookup failed: %s", exc)
        output_file = None
    if not output_file:
        return report
    try:
        cov = _read_discovery_coverage(output_file)
        # Guard: the newest output may be a --discover-only PROBE artifact, not
        # the real run's output. Pre-P0 (playwright closed-page bug) those
        # probes always wrote {found: 0, stop_reason: navigate_error}; picking
        # that up here downgraded healthy jobs via _COVERAGE_FAIL_STOP_REASONS.
        # A probe artifact is identifiable by empty products + a coverage block.
        if (
            isinstance(cov, dict)
            and int(cov.get("found") or 0) == 0
            and str(cov.get("stop_reason") or "") == "navigate_error"
        ):
            try:
                with open(output_file, "r", errors="ignore") as _pf:
                    _pdata = json.load(_pf)
                if not (_pdata.get("products") or []):
                    logger.info(
                        "_attach_discovery_coverage: skipping empty discover-only "
                        "probe artifact %s (navigate_error/0 — pre-P0 probe shape)",
                        os.path.basename(output_file),
                    )
                    cov = None
            except Exception:
                pass
        if isinstance(cov, dict):
            report["discovery_coverage"] = cov
            logger.info(
                "_attach_discovery_coverage: attached (stop_reason=%s, found=%s, "
                "dims=%s/%s) from %s",
                cov.get("stop_reason"),
                cov.get("found"),
                cov.get("dimensions_iterated"),
                cov.get("dimensions_total"),
                os.path.basename(output_file),
            )
    except Exception as exc:
        logger.warning("_attach_discovery_coverage: failed to read %s: %s", output_file, exc)
    return report


def _attach_transient_render_evidence(report: dict, slug: str) -> dict:
    """Job-311: prove (or refute) a TRANSIENT site-side render block.

    The testing phase runs the draft several times. When the NEWEST output's
    discovery stopped with ``empty_render`` while an EARLIER output in the
    same phase carried real items, the draft+strategy demonstrably work and
    the empty run is a site-side soft-block window — not a wrong strategy.
    Attach that evidence so ``classify_test_failure`` re-tests the same draft
    instead of burning the strategy rung (job 311: by the time its strategy
    switch ran, the window had already passed — the post-mortem probe found
    12 URLs). No-op unless the newest output is a genuine empty_render.
    """
    if not isinstance(report, dict) or not slug or report.get("discovery_transient"):
        return report
    try:
        from django.conf import settings as _settings

        root = str(getattr(_settings, "PROJECT_ROOT", "."))
        ws_dir = os.path.join(root, "workspace", slug)
        outs = []
        for name in os.listdir(ws_dir) if os.path.isdir(ws_dir) else []:
            if name.startswith("output_") and name.endswith(".json"):
                p = os.path.join(ws_dir, name)
                try:
                    outs.append((os.path.getmtime(p), p))
                except OSError:
                    continue
        if not outs:
            return report
        outs.sort()
        _newest_path = outs[-1][1]
        newest_cov = {}
        try:
            from .nodes.run_execution import _read_discovery_coverage

            newest_cov = _read_discovery_coverage(_newest_path) or {}
        except Exception:
            newest_cov = {}
        if str(newest_cov.get("stop_reason") or "") != "empty_render":
            return report
        best_items = 0
        try:
            from .nodes.run_execution import _substantive_item_count

            for _mt, p in outs:
                best_items = max(best_items, _substantive_item_count(p))
        except Exception:
            best_items = 0
        if best_items <= 0:
            return report  # nothing ever worked — not transient evidence
        report["discovery_transient"] = {
            "suspected": True,
            "latest_stop_reason": "empty_render",
            "best_items": best_items,
            "outputs_seen": len(outs),
        }
        logger.warning(
            "_attach_transient_render_evidence: empty_render on newest output "
            "but an earlier output this phase carried %d items — transient "
            "render block suspected (job draft is NOT condemned)",
            best_items,
        )
    except Exception as exc:
        logger.debug("_attach_transient_render_evidence: skipped: %s", exc)
    return report


def _preserve_test_report(slug: str) -> None:
    """Copy test_report.json from LOCAL workspace to the File Master (scrapers analysis/)."""
    if not slug:
        return
    try:
        import src.artifacts as artifacts

        root = _get_project_root()
        src = os.path.join(root, "workspace", slug, "test_report.json")  # LOCAL
        if not os.path.isfile(src):
            return
        with open(src, "rb") as _f:
            _bytes = _f.read()
        dst_key = artifacts.scrapers_key(slug, "analysis", "test_report.json")
        artifacts.write(dst_key, _bytes)
        logger.info("_preserve_test_report: copied to %s", dst_key)
    except Exception as exc:
        logger.warning("_preserve_test_report: failed: %s", exc)


AGENT_RECURSION_MAP: dict[str, int] = {
    "site_analyzer": 250,
    "product_analyzer": 200,
    # ARCHIVED: "navigation_agent": 200,
    "nav_skill_review": 60,
    "scraper_analyzer": 160,
    "code_writer": 120,  # recursion limit — high enough to finish (read+write+test+fix
                         # needs ~25 steps). The wall-clock cap (_invoke_agent_with_timeout
                         # at 900s) is the real backstop; this just prevents the react loop
                         # from iterating past GraphRecursionError.
    "code_tester": 120,
    # T1.7: dagster_converter was absent → ran at the default AGENT_RECURSION_LIMIT
    # (150) — which is how job 302 burned 34 LLM calls. Capped BELOW default like
    # its peers; the T0.2 wall is the real backstop.
    "dagster_converter": 120,
    "cleanup": 80,
    "skill_learner": 80,
}


class _ToolCallLogger(BaseCallbackHandler):
    """Write a SessionLog per tool call, in real time.

    Why this exists: agent tool calls are batch-persisted only AFTER
    ``agent.invoke()`` returns (``_persist_agent_logs``). During a long run
    (code_writer ~15 min) the only SessionLog entries are content-free heartbeats
    — the job *looks* idle/hung while actively working, which caused a healthy
    run to be misdiagnosed as a hang and cancelled. This callback writes a
    SessionLog on every ``on_tool_start`` so monitoring sees real progress
    (which tool, which args) as it happens, and can distinguish slow-but-working
    from a genuinely stuck LLM call. Generic — attached centrally in
    ``_agent_config`` so every agent benefits.
    """

    def __init__(self, job_id: int, agent_name: str) -> None:
        self.job_id = job_id
        self.agent_name = agent_name

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:  # type: ignore[override]
        try:
            name = ""
            if isinstance(serialized, dict):
                name = serialized.get("name") or ""
            from scraper.models import SessionLog

            seq = SessionLog.objects.filter(job_id=self.job_id).count()
            SessionLog.objects.create(
                job_id=self.job_id,
                # P0-18: tool-call traces are NOT assistant messages. Writing
                # them as ROLE_ASSISTANT polluted the agent-summary view (tool
                # noise masqueraded as the agent's reasoning text). Use
                # ROLE_SYSTEM so "assistant" means real LLM output only.
                role=SessionLog.ROLE_SYSTEM,
                agent=self.agent_name,
                content=f"[TOOL] {name}: {(input_str or '')[:140]}",
                seq=seq,
            )
        except Exception:
            # A logging callback must NEVER crash the agent it observes.
            pass


def _agent_config(config: RunnableConfig, agent_name: str = "") -> RunnableConfig:
    """Create a config copy with a higher recursion limit for react agents.

    React agents make many tool-call rounds (each round = 1 recursion step).
    The default limit of 25 is too low for browsing-heavy agents like
    site_analyzer.  Per-agent limits are set in AGENT_RECURSION_MAP.

    Also attaches ``_ToolCallLogger`` (real-time tool-call SessionLog entries)
    when ``job_id`` is present in the config metadata, so long agent runs don't
    look idle. [progress-visibility fix]
    """
    limit = AGENT_RECURSION_MAP.get(agent_name, AGENT_RECURSION_LIMIT)
    agent_cfg = {**config}
    agent_cfg["recursion_limit"] = limit
    # Real-time tool-call logging: job_id is placed in config metadata by
    # LangGraphService.get_config, so it propagates to every node. Attaching
    # here (not per-invoke-site) covers all agents in one place.
    job_id = (config.get("metadata") or {}).get("job_id") if isinstance(config, dict) else None
    if job_id:
        # P0-18: Canonicalize the agent name to the hyphenated display form
        # (matching _persist_agent_logs). Without this, _ToolCallLogger writes
        # underscore names (code_writer) while _persist_agent_logs writes
        # hyphen names (code-writer) — splitting every agent into two DB
        # buckets. AGENT_PROMPT_MAP maps underscore → hyphen stem.
        from .subagents import AGENT_PROMPT_MAP
        _display_name = AGENT_PROMPT_MAP.get(agent_name, agent_name)
        cb = _ToolCallLogger(int(job_id), _display_name)
        # Circuit-breaker observation: records per-LLM-call success/failure so
        # a stalling model trips the breaker (llm_breaker) and traffic routes
        # to ZAI_FALLBACK_MODEL. Attached here so every agent's LLM calls feed it.
        from .llm_breaker import CircuitBreakerCallback

        cb_breaker = CircuitBreakerCallback()
        existing = agent_cfg.get("callbacks")
        # config["callbacks"] can be: None, a list of handlers, OR a
        # BaseCallbackManager (langgraph passes one; it's NOT iterable — calling
        # list() on it raises TypeError). Normalise to a flat handler list so
        # langgraph's run-tracking handlers are preserved alongside ours.
        if existing is None:
            agent_cfg["callbacks"] = [cb, cb_breaker]
        elif isinstance(existing, list):
            agent_cfg["callbacks"] = [*existing, cb, cb_breaker]
        elif isinstance(existing, BaseCallbackManager):
            agent_cfg["callbacks"] = [*existing.handlers, cb, cb_breaker]
        else:
            agent_cfg["callbacks"] = [existing, cb, cb_breaker]
    return agent_cfg


# ═══════════════════════════════════════════════════════════════════════════
# Agent wrapper nodes — bridge between deterministic graph and react agents
# ═══════════════════════════════════════════════════════════════════════════

PHASE_MAP: dict[str, str] = {
    "site_analyzer": "site_analysis",
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # "navigation_explore": "navigation_explore",
    # "navigation_agent": "navigation_agent",
    # "navigation_synthesize": "navigation_synthesize",
    # ═══ END ARCHIVED ═══
    # Must stay the ENUM TOKEN (Step.phase choices + sync API Phase schema) —
    # the display string "Browser Navigation" (pre-0035) wrote schema-invalid
    # values to Step.phase and forked a duplicate row from the seeded one.
    "browser_traverse": "browser_traverse",
    "nav_skill_review": "navigation_skill_review",
    "product_analyzer": "product_analysis",
    "scraper_analyzer": "scraper_analysis",
    "code_writer": "code_generation",
    "code_tester": "testing",
    "cleanup": "cleanup",
    "skill_learner": "skill_learning",
    "dagster_converter": "dagster_converter",
    "store_job_listings": "store_job_listings",
}


import threading


class _HeartbeatHandle:
    """Per-invocation heartbeat state.

    Holds a stop flag + the live timers so ``_stop_heartbeat`` can cancel
    EVERY rescheduled timer (not just the initial one) AND signal ``_beat`` to
    stop rescheduling. Per-invocation (not a module-global list) so the
    concurrency=2 worker's two in-flight jobs don't clobber each other's timers.
    """

    __slots__ = ("stop", "timers", "beats")

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.timers: list = []
        self.beats = 0


# M4: hard cap on the self-rescheduling chain. Even with the F5 try/finally
# at every call site, a future copy-pasted site (the pattern has already been
# pasted five times) or a worker-level kill could leave the chain immortal —
# job 333's leaked timer wrote DB rows every 5 minutes for days. 60 beats at
# the default 300s interval ≈ 5h; at the execution interval (240s) ≈ 4h.
_HEARTBEAT_MAX_BEATS = 60


def _start_heartbeat(
    job_id: int, agent_name: str, interval: int = 300,
    prefix: str = "[HEARTBEAT]",
    beat_budget: int | None = None,
) -> _HeartbeatHandle:
    """Start a background heartbeat that writes a SessionLog entry every
    ``interval`` seconds during long agent executions.

    The watchdog kills jobs with no SessionLog activity for 15+ minutes.
    LLM agents (code_writer, site_analyzer, etc.) are blocking calls that
    can run 15+ minutes without producing SessionLog entries. This heartbeat
    keeps the watchdog informed.

    ``prefix`` selects the watchdog treatment: ``[HEARTBEAT]`` rows are
    EXCLUDED from the stuck-job activity check (a leaked timer chain must
    not mask a dead agent — see cleanup_stuck_jobs), while run_execution
    passes ``[EXEC-ALIVE]`` so its rows COUNT: execution liveness is
    independently bounded (EXECUTION_STALL_TIMEOUT/EXECUTION_TIMEOUT and
    the /scrape timeouts), so an [EXEC-ALIVE] row can only rescue a
    genuinely-live 30+ min scrape — never mask a hang. Without this, a
    healthy long scrape whose only signal is heartbeats gets SIGKILLed
    as "crashed" the moment the watchdog is revived.

    ``beat_budget`` [wave-14 job-133] — the phase's own deadline in seconds
    (e.g. run_scraper's floored browser-dispatch bound). When given, the
    interval shrinks to ``max(30, beat_budget // 3)`` (never LONGER than the
    caller's ``interval``) so at least two beats land INSIDE the bounded
    window. A death mid-dispatch is then provable from the row sequence
    ("started" row present, later beats absent) instead of being
    indistinguishable from "the first beat wasn't due yet".

    The FIRST beat fires synchronously at t=0 and writes a "started" row:
    it stamps the dispatch moment, so postmortems can tell "dispatch began
    and the process died inside the run" from "the run never started".

    Returns a ``_HeartbeatHandle`` that must be passed to ``_stop_heartbeat``
    when the agent finishes. The handle's stop flag is what actually ends the
    chain — ``_beat`` checks it before rescheduling, so cancellation is reliable
    even if a beat fires mid-cancel.

    M4 belt-and-braces: the chain self-terminates after _HEARTBEAT_MAX_BEATS
    beats OR when the job reaches a terminal status — so no leak path can
    run forever even if _stop_heartbeat is never called.
    """
    handle = _HeartbeatHandle()

    if beat_budget is not None and beat_budget > 0:
        interval = min(interval, max(30, beat_budget // 3))

    def _beat() -> None:
        if handle.stop.is_set():
            return
        # M4: self-cap — an immortal chain is worse than a missing heartbeat.
        handle.beats += 1
        if handle.beats > _HEARTBEAT_MAX_BEATS:
            logger.warning(
                "heartbeat for job %s (%s) exceeded %d beats — self-terminating",
                job_id, agent_name, _HEARTBEAT_MAX_BEATS,
            )
            return
        _first = handle.beats == 1
        _job_terminal = False
        try:
            from scraper.models import ScrapeJob, SessionLog

            _job_terminal = job_id in () or ScrapeJob.objects.filter(
                pk=job_id, status__in=(
                    ScrapeJob.STATUS_COMPLETED, ScrapeJob.STATUS_FAILED,
                    ScrapeJob.STATUS_CANCELLED, ScrapeJob.STATUS_CAPTCHA_BLOCKED,
                    ScrapeJob.STATUS_AKAMAI_BLOCKED,
                ),
            ).exists()
            if not _job_terminal:
                seq = SessionLog.objects.filter(job_id=job_id).count()
                _content = (
                    f"{prefix} Agent {agent_name} started (beat every {interval}s)"
                    if _first
                    else f"{prefix} Agent {agent_name} still running..."
                )
                SessionLog.objects.create(
                    job_id=job_id,
                    role=SessionLog.ROLE_SYSTEM,
                    agent=agent_name,
                    content=_content,
                    seq=seq,
                )
        except Exception as exc:
            # [wave-14 job-133] A silently-swallowed beat failure made the
            # heartbeat's OWN health unobservable — "beat didn't fire" and
            # "beat's DB write failed" were indistinguishable in postmortems.
            logger.warning(
                "heartbeat for job %s (%s) beat %d failed: %s",
                job_id, agent_name, handle.beats, exc,
            )
        if handle.stop.is_set() or _job_terminal:
            return
        # Re-check before rescheduling — _stop_heartbeat may have fired while
        # the SessionLog write was in flight.
        if handle.stop.is_set():
            return
        timer = threading.Timer(interval, _beat)
        timer.daemon = True
        handle.timers.append(timer)
        timer.start()

    _beat()  # t=0 beat — stamps the start moment synchronously
    return handle


def _stop_heartbeat(handle: _HeartbeatHandle | None) -> None:
    """Stop the heartbeat: set the stop flag (any in-flight ``_beat`` won't
    reschedule) and cancel every live timer.

    The old version cancelled only the INITIAL timer and then ``clear()``-ed a
    shared module-global list — which (a) left the self-rescheduled timers
    firing forever (an immortal chain that masked agent hangs from the watchdog
    AND was ~30% of all SessionLog writes), and (b) under concurrency=2 let one
    job's stop clear another job's timers. The per-handle stop flag fixes both.
    """
    if handle is None:
        return
    handle.stop.set()
    for t in handle.timers:
        try:
            t.cancel()
        except Exception:
            pass
    handle.timers.clear()


def _notify_phase(job_id: int, node_name: str, status: str) -> None:
    phase = PHASE_MAP.get(node_name, node_name)
    try:
        from django.utils import timezone
        from scraper.models import ScrapeJob, Step

        job = ScrapeJob.objects.get(pk=job_id)
        step, _ = Step.objects.get_or_create(job=job, phase=phase)
        step.status = status
        # Latest-attempt span: a "running" (re)start opens a fresh span, so a
        # retried phase shows its LAST run's duration instead of the union of
        # every attempt (the job-46 double-booking bug — "118m" that was
        # really two attempts summed). done/failed/skipped close the span;
        # a running with no prior start still gets one (backstop).
        if status == "running":
            step.started_at = timezone.now()
            step.completed_at = None
        elif not step.started_at:
            step.started_at = timezone.now()
        if status in ("done", "failed", "skipped"):
            step.completed_at = timezone.now()
        step.save()
    except Exception as exc:
        logger.warning("_notify_phase(%s, %s): %s", node_name, status, exc)

    try:
        from scraper.services import LangGraphService

        LangGraphService._publish_redis(
            job_id, {"type": "step", "phase": phase, "status": status}
        )
    except Exception:
        pass
    # Partner event (async_api.yaml JobPhaseUpdated) — the single phase
    # choke point, so every phase transition fans out. Best-effort: emit's
    # own gate skips non-partner jobs. (job is re-fetched: the Step block
    # above may have failed before assigning it.)
    try:
        from scraper.events import emit as _emit
        from scraper.models import ScrapeJob

        pjob = ScrapeJob.objects.filter(pk=job_id).first()
        if pjob is not None:
            _emit(pjob, "job.phase.updated",
                  {"phase": phase, "phase_status": status})
    except Exception:
        pass


def _log_event_row(job_id: int, agent: str, content: str) -> None:
    """One SessionLog row for a deterministic phase event (no agent loop).

    [jobs 83/88 RCA] browser_traverse — whose result IS the discovery
    contract every downstream phase leans on (working_url, listing_url,
    rendering_verified, items_per_page) — used to emit ZERO log rows, so
    neither RCA could be read from the job log at all.
    """
    try:
        from scraper.models import SessionLog

        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_SYSTEM,
            agent=agent,
            content=content[:4000],
            seq=seq,
        )
    except Exception:
        pass


def _budget_setting(name: str, default: int) -> int:
    """Budget/timeout constant, env-overridable via Django settings.

    Lets the Phase-1 gate tune per-agent budgets from measured per-step latency
    WITHOUT a code change. The structural mismatch to fix: PRODUCT_ANALYSIS_BUDGET
    (recursion steps) × ~per-step latency can exceed _AGENT_INVOKE_TIMEOUT (the
    wall-clock cap), so the wall-clock abandons mid-budget → empty result →
    budget-escalation cascade. Tuning lowers the budget to fit, NOT raises the
    wall-clock (which would widen the leaked-thread window).
    """
    try:
        from django.conf import settings

        return int(getattr(settings, name, default))
    except Exception:
        return default


SITE_ANALYSIS_BUDGET = _budget_setting("SITE_ANALYSIS_BUDGET", 10)
SITE_ANALYSIS_BUDGET_EXTENDED = _budget_setting("SITE_ANALYSIS_BUDGET_EXTENDED", 20)
SITE_ANALYSIS_MAX_BUDGET = _budget_setting("SITE_ANALYSIS_MAX_BUDGET", 50)
PRODUCT_ANALYSIS_BUDGET = _budget_setting("PRODUCT_ANALYSIS_BUDGET", 50)
PRODUCT_ANALYSIS_BUDGET_EXTENDED = _budget_setting("PRODUCT_ANALYSIS_BUDGET_EXTENDED", 70)
PRODUCT_ANALYSIS_MAX_BUDGET = _budget_setting("PRODUCT_ANALYSIS_MAX_BUDGET", 70)
MAX_OUTER_RETRIES = _budget_setting("MAX_OUTER_RETRIES", 2)

MAX_RETRY_SUMMARY_CHARS = 8000


def _read_json_artifact(root: str, slug: str, filename: str) -> dict[str, Any]:
    path = os.path.join(root, "workspace", slug, filename)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _archive_existing_scraper(slug: str) -> str | None:
    """Archive the current scraper.py before cleanup overwrites it.

    Returns the archive KEY (or None if there was nothing to archive) so the
    caller can restore the prior good scraper on a failed run. Artifacts live in
    the File Master (cross-service); keys are logical ``scrapers/{slug}/...``.
    """
    if not slug:
        return None
    try:
        import src.artifacts as artifacts
        from datetime import datetime, timezone as dt_timezone

        prod_key = artifacts.scrapers_key(slug, "scraper.py")
        if not artifacts.exists(prod_key):
            return None
        ts = datetime.now(dt_timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        archive_name = f"scraper-{slug}-{ts}.py"
        archive_key = artifacts.scrapers_key(slug, archive_name)
        artifacts.write(archive_key, artifacts.read(prod_key))
        logger.info("_archive_existing_scraper: archived → %s", archive_name)
        return archive_key
    except Exception as exc:
        logger.warning("_archive_existing_scraper: failed: %s", exc)
        return None


def _archive_failure_evidence(slug: str, job_id: int, execution_status: str) -> None:
    """[pillowtalk gap → jobs 71/76 RCA] Keep a FAILED job's test report.

    On non-SUCCESS, copy ``workspace/{slug}/test_report.json`` to the File
    Master as ``scrapers/{slug}/analysis/test_report-{job_id}.json`` — the
    workspace is wiped by the next job's setup, and both job-71/job-76
    post-mortems had to reconstruct the cascade from truncated session logs
    because the report was gone. Corrupt-guarded like every FM publish.
    SUCCESS skips (the tracker flow already publishes the report).
    """
    if not slug or execution_status == "SUCCESS":
        return
    try:
        import src.artifacts as artifacts

        from .tools.filesystem_tools import guard_json_bytes

        root = _get_project_root()
        report = os.path.join(root, "workspace", slug, "test_report.json")
        if not os.path.isfile(report):
            return
        with open(report, "rb") as _f:
            _bytes = _f.read()
        _guarded, _note = guard_json_bytes(_bytes)
        if _guarded is None:
            logger.error(
                "_archive_failure_evidence: test_report.json is corrupt and "
                "unrepairable (%s) — SKIPPED (job %s)", _note, job_id,
            )
            return
        if _note:
            logger.warning(
                "_archive_failure_evidence: test_report.json was corrupt — "
                "archiving REPAIRED version (%s, job %s)", _note, job_id,
            )
        artifacts.write(
            artifacts.scrapers_key(slug, "analysis", f"test_report-{job_id}.json"),
            _guarded,
        )
        logger.info(
            "_archive_failure_evidence: test_report.json → scrapers/%s/analysis/ "
            "(job %s, execution_status=%s)", slug, job_id, execution_status,
        )
    except Exception as exc:
        logger.warning("_archive_failure_evidence: failed: %s", exc)


def _promote_scraper(
    slug: str, job_id: int, execution_status: str, archive_key: str | None,
    product_count: int | None = None,
) -> str | None:
    """Deterministic, failure-safe scraper finalization (replaces the LLM cp).

    1. Always copy this job's LOCAL ``workspace/{slug}/scraper_draft.py`` to a
       per-job File Master key ``scrapers/{slug}/jobs/scraper-{job_id}.py``
       (attributed, survives later jobs).
    2. Promote to production ``scrapers/{slug}/scraper.py`` ONLY on SUCCESS. On
       non-success, leave production untouched and restore the prior good scraper
       from the archive key (defends against any stray clobber).
       [job-77] SUCCESS with an explicit 0-item count does NOT promote either —
       execution ran, extracted nothing, and the dead draft still became the
       site's production scraper.py (verified byte-identical in prod FM). A
       scraper that extracts nothing under execution conditions must not stand
       in for a working one. Unknown count (None) keeps the legacy behavior.

    Returns the per-job scraper KEY (or None if no draft was produced). The draft
    read is local (workspace stays on the worker); all scrapers/ writes go to FM.
    """
    if not slug:
        return None
    try:
        import src.artifacts as artifacts

        root = _get_project_root()
        draft = os.path.join(root, "workspace", slug, "scraper_draft.py")  # LOCAL
        per_job_key = artifacts.scrapers_key(slug, "jobs", f"scraper-{job_id}.py")
        prod_key = artifacts.scrapers_key(slug, "scraper.py")

        promoted = None
        if os.path.isfile(draft):
            with open(draft, "rb") as _f:
                _draft_bytes = _f.read()
            artifacts.write(per_job_key, _draft_bytes)
            promoted = per_job_key
            logger.info("_promote_scraper: per-job copy → jobs/scraper-%s.py", job_id)

        if execution_status == "SUCCESS" and product_count == 0:
            logger.warning(
                "_promote_scraper: SUCCESS but 0 items extracted — NOT promoting "
                "a zero-yield draft to production scraper.py (job %s) [job-77]",
                job_id,
            )
        elif execution_status == "SUCCESS":
            if promoted:
                artifacts.write(prod_key, artifacts.read(per_job_key))
                logger.info(
                    "_promote_scraper: SUCCESS → promoted to scraper.py (job %s)", job_id
                )
        else:
            # Non-success: do NOT promote the (possibly broken) draft. Restore
            # the prior good production scraper if anything clobbered it.
            if archive_key and artifacts.exists(archive_key) and artifacts.exists(prod_key):
                artifacts.write(prod_key, artifacts.read(archive_key))
                logger.info(
                    "_promote_scraper: non-SUCCESS → restored scraper.py from archive (job %s)",
                    job_id,
                )
            logger.info(
                "_promote_scraper: non-SUCCESS (execution_status=%s) → production "
                "scraper.py left as-is (job %s)",
                execution_status, job_id,
            )
        # Partner events (async_api.yaml): scraper_ready fires IN-GRAPH at
        # promotion on SUCCESS (not reconciler-late — the spec's artifact
        # ordering places it between sample and output). Best-effort;
        # emit's gate skips non-partner jobs; dedupe keeps retries once.
        if promoted and execution_status == "SUCCESS":
            try:
                from scraper.events import emit as _emit
                from scraper.models import ScrapeJob

                pjob = ScrapeJob.objects.filter(pk=job_id).first()
                if pjob is not None:
                    _emit(pjob, "job.scraper_ready", {},
                          dedupe_key="scraper_ready")
                    _emit(pjob, "job.artifact.available",
                          {"kind": "scraper_code",
                           "url": f"/api/v1/jobs/{job_id}/scraper-code",
                           "key": promoted},
                          dedupe_key=f"artifact:scraper_code:{job_id}")
            except Exception as exc:
                logger.warning("_promote_scraper: emit: %s", exc)
        return promoted
    except Exception as exc:
        logger.warning("_promote_scraper: failed: %s", exc)
        return None


def _extract_previous_findings(
    result: dict, max_chars: int = MAX_RETRY_SUMMARY_CHARS
) -> str:
    messages = result.get("messages", [])
    parts: list[str] = []
    total_len = 0

    for msg in messages:
        content = ""
        prefix = ""

        if isinstance(msg, AIMessage):
            text = getattr(msg, "content", "")
            if text and isinstance(text, str) and len(text.strip()) > 20:
                prefix = "[Agent]"
                content = text.strip()
        elif isinstance(msg, ToolMessage):
            text = str(getattr(msg, "content", ""))
            if any(
                marker in text
                for marker in [
                    '"jsonlds"',
                    '"platformMarkers"',
                    '"algolia"',
                    '"appId"',
                    '"@type"',
                    '"jsonld_extraction"',
                ]
            ):
                prefix = "[Data]"
                content = text.strip()

        if not content or not prefix:
            continue

        chunk = f"{prefix}: {content[:2000]}"
        if total_len + len(chunk) > max_chars:
            remaining = max_chars - total_len
            if remaining > 100:
                parts.append(chunk[:remaining] + "\n[...truncated]")
            break
        parts.append(chunk)
        total_len += len(chunk)

    return "\n\n".join(parts) if parts else "(no findings extracted from previous run)"


_PLAYWRIGHT_RESULT_HEADERS = [
    "### Ran Playwright code",
    "### Page State",
    "### Result",
    "### Clicked element",
    "### Navigated to",
    "### Browser console",
]


def _summarize_tool_args(tool_name: str, args: dict) -> str:
    if "navigate" in tool_name:
        return f"Navigate to {str(args.get('url', ''))[:80]}"
    if "snapshot" in tool_name:
        return "Accessibility snapshot"
    if "evaluate" in tool_name:
        script = str(args.get("script", args.get("expression", "")))
        return f"Evaluate: {script[:120]}" if script else "Evaluate JS"
    if "click" in tool_name:
        return f"Click {str(args.get('element', args.get('selector', '')))[:80]}"
    if "type" in tool_name and "browser" in tool_name:
        return f"Type into {str(args.get('element', ''))[:60]}"
    if "wait_for" in tool_name:
        return f"Wait for {str(args.get('selector', args.get('time', '')))[:60]}"
    if tool_name == "learn_skill":
        return f"Learn skill {args.get('skill_name', '')}: {str(args.get('title', ''))[:60]}"
    if tool_name == "create_new_skill":
        return f"Create skill {args.get('name', '')}: {str(args.get('description', ''))[:60]}"
    if tool_name == "write_file":
        path = str(args.get("path", ""))
        content = str(args.get("content", ""))
        return f"Write {path} ({len(content)} chars)"
    if tool_name == "read_file":
        return f"Read {str(args.get('path', ''))}"
    if tool_name == "edit_file":
        return f"Edit {str(args.get('path', ''))}"
    if tool_name == "search_files":
        return f"Search files: {str(args.get('pattern', ''))[:60]}"
    if tool_name == "search_content":
        return f"Search content: {str(args.get('pattern', ''))[:60]}"
    if "load_skill" in tool_name:
        return f"Load skill: {str(args.get('name', ''))}"
    if "list_skills" in tool_name:
        return "List available skills"
    if "web_fetch" in tool_name:
        return f"Fetch {str(args.get('url', ''))[:80]}"
    if "run_bash" in tool_name:
        cmd = str(args.get("command", ""))
        return f"Run: {cmd[:120]}"
    if "network_request" in tool_name:
        return f"Network request {str(args.get('requestId', ''))[:30]}"
    if "network_requests" in tool_name:
        return "List network requests"
    if "tabs" in tool_name:
        return "List browser tabs"
    json_args = json.dumps(args, default=str)
    return json_args[:150] if json_args != "{}" else tool_name


def _clean_result_summary(raw: str, max_len: int = 300) -> str:
    text = raw
    for header in _PLAYWRIGHT_RESULT_HEADERS:
        text = text.replace(header, "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = " ".join(lines)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "..."
    return cleaned


def _accessibility_goto(state: ScrapeState, update: dict[str, Any] | None = None) -> Command:
    """Pick the next node after check_accessibility based on input_mode.

    For navigation/list_page/search_term jobs, route directly to browser_traverse
    and set ``skip_site_analysis``. site_analyzer's output is never consumed by
    the browser path (browser_traverse reads url/page_type/search_criteria only),
    so running it wastes ~5 minutes. url_list jobs still go through site_analyzer,
    where site_analysis informs product/content analysis.
    """
    input_mode = state.get("input_mode", "url_list")
    if input_mode in ("navigation", "list_page", "search_term"):
        logger.info(
            "check_accessibility: input_mode=%s → browser_traverse "
            "(skipping site_analyzer)",
            input_mode,
        )
        upd: dict[str, Any] = dict(update or {})
        upd["skip_site_analysis"] = True
        return Command(update=upd, goto="browser_traverse")
    if update:
        return Command(update=update, goto="site_analyzer")
    return Command(goto="site_analyzer")


def check_accessibility(state: ScrapeState, config: RunnableConfig) -> Command:
    """Probe the target URL with LLM-based captcha verification.

    On fresh start: runs probe + LLM captcha check on each escalation method.
    If all methods hit captcha, ends the job immediately.
    If captcha-free method found, saves probe data and routes to site_analyzer.

    On resume (skip flags set): skips probe and routes to the appropriate node.
    """
    job_id = state.get("job_id", 0)

    if state.get("skip_site_analysis"):
        if state.get("skip_product_analysis"):
            if state.get("skip_code_generation"):
                return Command(goto="code_tester")
            if state.get("scraper_analysis"):
                return Command(goto="code_writer")
            return Command(goto="scraper_analyzer")
        return Command(goto="validate_analysis")

    url = state.get("product_url", "") or state.get("url", "")
    _notify_phase(job_id, "accessibility_check", "running")

    logger.info("check_accessibility: probing %s (job %s)", url[:100], job_id)

    try:
        from .tools.probe_tools import run_probe_with_captcha_check

        data = run_probe_with_captcha_check(url, render_js=True, job_id=job_id)
    except Exception as exc:
        logger.warning("check_accessibility: probe failed, continuing: %s", exc)
        _notify_phase(job_id, "accessibility_check", "done")
        return _accessibility_goto(state)

    if data.get("captcha_detected"):
        methods = data.get("methods_tried", [])
        captcha_type = data.get("captcha_type", "unknown")
        reasoning = data.get("captcha_reasoning", "")
        is_akamai = data.get("akamai_detected", False)
        error_msg = (
            f"Akamai Bot Manager protection detected on {url}. "
            f"All {len(methods)} probe methods across all proxy tiers "
            f"were blocked by Akamai. Site skipped."
            if is_akamai
            else f"Captcha detected: {captcha_type}. "
            f"All {len(methods)} probe methods returned captcha pages. "
            f"Methods tried: {', '.join(methods)}. "
            f"{reasoning}"
        )
        status_label = "akamai_blocked" if is_akamai else "captcha_blocked"
        logger.warning(
            "check_accessibility: %s for job %s — %s",
            status_label,
            job_id,
            error_msg[:200],
        )

        try:
            from scraper.models import ScrapeJob

            job_status = (
                ScrapeJob.STATUS_AKAMAI_BLOCKED
                if is_akamai
                else ScrapeJob.STATUS_CAPTCHA_BLOCKED
            )
            ScrapeJob.objects.filter(pk=job_id).update(
                status=job_status,
                error_message=error_msg[:2000],
                completed_at=timezone.now(),
            )
        except Exception as exc:
            logger.warning("check_accessibility: failed to update job status: %s", exc)

        _notify_phase(job_id, "accessibility_check", "done")
        # T3.7: tag WHICH url the probe actually hit — downstream analyzers
        # otherwise assume the job URL and build on a false anchor when the
        # probe resolved elsewhere (redirects, listing candidates).
        data.setdefault("probed_url", url)
        return Command(
            update={
                "error_message": error_msg,
                "probe_result": data,
                "probe_url": url,
            },
            goto=END,
        )

    _notify_phase(job_id, "accessibility_check", "done")

    method = data.get("method", "unknown")
    proxy_tier = data.get("proxy_tier", "none")

    agent_probe_result: dict[str, Any] = {
        # T3.7: the analyzer message reads this so it can state which URL the
        # connectivity evidence actually came from.
        "probed_url": url,
        "connectivity": {
            "method_that_worked": method,
            "http_method": data.get("http_method"),
            "browser_method": data.get("browser_method"),
            "proxy_tier": proxy_tier,
            "js_rendering_needed": data.get("needs_browser", True),
            "anti_bot_detected": bool(data.get("blocked", False)),
            "spa_detected": bool(data.get("spa_detected", False)),
            "spa_framework": data.get("spa_framework", ""),
        },
        "platform": "unknown",
        "captcha_verified": True,
    }

    _persist_probe_summary(job_id, url, agent_probe_result, data)

    probe_state: dict[str, Any] = {
        "probe_result": agent_probe_result,
        "probe_url": url,
    }

    from .tools.context import update_probe_result

    update_probe_result(data)

    return _accessibility_goto(state, probe_state)


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back on absence/garbage."""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# T0.2: the wall sat INSIDE the healthy range for the one agent that needs it
# (glm-5-turbo code_writer ~700-900s) and was un-configurable; the value is now
# env-tunable so a measured p95 per agent can drive it without a code change.
_AGENT_INVOKE_TIMEOUT = _env_int("AGENT_INVOKE_TIMEOUT", 900)  # seconds — hard
    # wall-clock cap per agent.invoke(). glm-5-turbo needs ~700-900s for
    # code_writer (read template + generate ~500 lines + self-test + fix).


def _tester_invoke_timeout() -> int:
    """[job-81 N-A] The code_tester's wall clock, derived from its own contract.

    The tester's prompt MANDATES up to two blocking browser runs (phase-1
    discovery + phase-2 sample), and run_scraper floors every browser run at
    BROWSER_RUN_TIMEOUT_FLOOR (+60s httpx margin). The flat 900s
    AGENT_INVOKE_TIMEOUT cannot contain that mandated work: job 81's two ~510s
    runs left 370s of window mid-cascade — abandonment was structural, not a
    slow-site anomaly. Derive the window from the tool budget the prompt tells
    the tester to spend: 2 × floored run + LLM/analysis margin. Healthy runs
    that finish early are untouched; AGENT_INVOKE_TIMEOUT stays the floor, so
    raising it via env still wins. NOT a cap on the work — a backstop sized to
    the work.
    """
    from .tools.shell_tools import BROWSER_RUN_TIMEOUT_FLOOR

    return max(
        _AGENT_INVOKE_TIMEOUT,
        2 * (BROWSER_RUN_TIMEOUT_FLOOR + 60) + 300,
    )


# [wave-15 PR-2b/W15-C] Per-phase async allowlist. DEFAULT EMPTY — no phase
# runs ainvoke-under-loop until the canary earns it: set
# AGENT_ASYNC_PHASES="code_writer" (comma-separated) to opt a phase in, and
# flip the default only after a local e2e shows a full code_writer phase
# under async. LLM_ASYNC_EXECUTION remains the explicit ALL-phases override
# (rollback lever); docker-compose no longer passes it, so only an operator
# env can turn it on.
_ASYNC_PHASES: frozenset = frozenset()


def _async_phase_allowlist() -> set:
    """Phases opted into the async invoke path: the shipped default
    (``_ASYNC_PHASES``, empty) union the ``AGENT_ASYNC_PHASES`` entries."""
    raw = (os.environ.get("AGENT_ASYNC_PHASES") or "").strip().lower()
    env_phases = {p.strip() for p in raw.split(",") if p.strip()}
    return set(_ASYNC_PHASES) | env_phases


def _async_execution_enabled(phase: str = "") -> bool:
    """Per-phase kill-switch for async cancellation (Per-Phase Execution Contract).

    True (for THIS phase) → ``_invoke_agent_with_timeout`` runs
    ``agent.ainvoke`` under ``asyncio.wait_for`` in a manually managed loop: on
    timeout, ``CancelledError`` propagates into the react loop and the async
    httpx client CLOSES the in-flight z.ai socket — the work actually stops
    (vs the sync path's daemon-thread abandon-then-leak that held the socket +
    ~180K-char context until the Celery time_limit SIGKILLed the worker). This
    is the contract's real cancellation.

    Resolution order: ``LLM_ASYNC_EXECUTION`` (explicit all-phases override,
    default False) wins; otherwise THIS ``phase`` must be named in
    ``AGENT_ASYNC_PHASES``. With neither set the answer is False for every
    phase — the allowlist ships empty.

    Cancellation bounds: a timeout during the LLM call cancels cleanly (async
    httpx closes the socket); a timeout during a SYNC tool (run_in_executor)
    cancels the awaiting task immediately, but that tool's executor thread
    keeps running to completion and is only reaped at process exit (bounded by
    the per-tool guards). The loop is closed WITHOUT
    ``shutdown_default_executor`` so the thread is NOT joined — the phase
    still ends on its deadline (see ``_shutdown_loop_no_executor_join``).
    """
    try:
        from django.conf import settings

        if bool(getattr(settings, "LLM_ASYNC_EXECUTION", False)):
            return True
    except Exception:
        pass
    return bool(phase) and phase in _async_phase_allowlist()


def _shutdown_loop_no_executor_join(loop) -> None:
    """Close a loop the way ``asyncio.Runner`` does — MINUS the executor join.

    Cancels leftover tasks, drains them, shuts down async generators (the
    streaming path's ``_astream``), then closes. Deliberately skips
    ``shutdown_default_executor``: joining it is exactly what made the async
    wall clock soft (see ``_invoke_agent_async``).
    """
    import asyncio

    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        loop.close()


def _invoke_agent_async(agent, messages, agent_cfg, phase, job_id, timeout):
    """Run ``agent.ainvoke`` under ``asyncio.wait_for`` in a fresh event loop.

    The graph runs synchronously today (graph.invoke from the Celery task), so
    there is no running event loop when this node executes — a fresh loop is
    safe. On timeout the ``wait_for`` cancels the ainvoke await; for an
    in-flight LLM call the CancelledError reaches the async httpx client which
    closes the z.ai socket (verified cancellable). Returns ``{"messages": []}``
    on timeout (callers treat as budget-exhausted), ``{"_error": ...}`` on
    other errors — matching the sync path's contract.
    """
    import asyncio
    import time as _time

    t0 = _time.monotonic()

    async def _run():
        return await asyncio.wait_for(
            agent.ainvoke({"messages": messages}, agent_cfg), timeout=timeout
        )

    # [wave-15 PR-2b/W15-B] Manual loop management — NOT ``asyncio.run``:
    # asyncio.run closes the loop with ``shutdown_default_executor()``, which
    # JOINS the default executor's threads. A sync tool still in flight (a
    # 600s run_scraper, a slow pre_model_hook) would hold the phase past its
    # deadline — measured: 2s deadline + 6s tool → 6.01s wall (sync path:
    # 2.00s). The old gate comment called that "the same shape as today's
    # thread abandon" — it was not: the wall clock was soft. Skipping the
    # executor join keeps the deadline honest; the abandoned tool's thread
    # finishes in the background and is reaped at process exit, the same
    # eventual bound as the sync path's leak.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_run())
    except asyncio.TimeoutError:
        logger.error(
            "_invoke_agent_with_timeout[%s]: ainvoke exceeded %ds wall-clock "
            "— cancelled (socket closed), returning empty (job %s)",
            phase, timeout, job_id,
        )
        # [wave-14 job-133] The container's stdout log is not the job log —
        # postmortems read SessionLog. Record the invoke death as activity
        # (it IS activity: the phase's window ended).
        _log_event_row(
            job_id, phase,
            f"[INVOKE-TIMEOUT] {phase} exceeded {timeout}s wall-clock — "
            f"invocation cancelled, phase did not complete",
        )
        # T0.3: the dead invocation must be DISTINGUISHABLE from a healthy
        # budget-exhausted return — both paths used to be bare {"messages": []}
        # and `_error` was read by nobody, so a wall-clock death was invisible
        # downstream (25% of code_writer's wall was these silent deaths).
        return {"messages": [], "_error": f"wall-clock timeout after {timeout}s",
                "_error_class": "WallClockTimeout"}
    except Exception as exc:
        import traceback

        logger.error(
            "_invoke_agent_with_timeout[%s]: ainvoke raised after %.0fms: %s: %s (job %s)",
            phase, (_time.monotonic() - t0) * 1000,
            type(exc).__name__, str(exc)[:300], job_id,
        )
        logger.error(
            "_invoke_agent_with_timeout[%s]: traceback:\n%s",
            phase, traceback.format_exc(limit=8),
        )
        return {"_error": str(exc)[:200], "_error_class": type(exc).__name__}
    finally:
        # Every path above (success, timeout, error): reap leftover tasks and
        # async generators, then close the loop — never joining executor
        # threads (that join was the soft wall clock).
        try:
            _shutdown_loop_no_executor_join(loop)
        except Exception:
            pass
    return result


def _invoke_agent_with_timeout(agent, messages, agent_cfg, phase: str, job_id, timeout: int = _AGENT_INVOKE_TIMEOUT):
    """Run the agent with a wall-clock timeout.

    Two modes (per-phase gate, see ``_async_execution_enabled`` — default
    every-phase-sync until ``AGENT_ASYNC_PHASES`` or the ``LLM_ASYNC_EXECUTION``
    override says otherwise):

    - **async**: ``agent.ainvoke`` under ``asyncio.wait_for`` in a manually
      managed loop — on timeout the in-flight z.ai call is genuinely CANCELLED
      (httpx closes the socket) and the deadline holds even with a sync tool
      in flight.
    - **sync** (default): raw daemon thread + ``thread.join``; on timeout the
      thread is abandoned (leaks until the Celery ``time_limit`` reclaims the
      worker). Pre-Phase-4 behavior.

    Both return ``{"messages": []}`` on timeout (callers treat as
    budget-exhausted).
    """
    # [job-81 N-B] Publish the deadline so blocking tools (run_scraper's
    # browser dispatch) can refuse work that cannot finish before this fires.
    try:
        set_tool_deadline(time.time() + timeout)
    except Exception:
        pass

    if _async_execution_enabled(phase):
        return _invoke_agent_async(agent, messages, agent_cfg, phase, job_id, timeout)

    import threading

    result_box = [None]
    def _run():
        try:
            result_box[0] = agent.invoke({"messages": messages}, agent_cfg)
        except Exception as exc:
            # Preserve the exception CLASS alongside the message: str(exc)
            # alone laundered provider outages (429 code 1302) into
            # "agent made no progress" (job-12).
            result_box[0] = {"_error": str(exc)[:200], "_error_class": type(exc).__name__}

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        logger.error(
            "_invoke_agent_with_timeout[%s]: agent.invoke exceeded %ds wall-clock "
            "— abandoning thread, returning empty (job %s)",
            phase, timeout, job_id,
        )
        # [wave-14 job-133] SessionLog twin of the async-path row (postmortems
        # read the job log, not container stdout).
        _log_event_row(
            job_id, phase,
            f"[INVOKE-TIMEOUT] {phase} exceeded {timeout}s wall-clock — "
            f"thread abandoned (leaks until the task time limit), phase did "
            f"not complete",
        )
        # T0.3 (sync twin of the async-path marker): surface the dead invocation.
        return {"messages": [], "_error": f"wall-clock timeout after {timeout}s",
                "_error_class": "WallClockTimeout"}
    return result_box[0] or {"messages": []}


def _run_budgeted_agent(
    state: ScrapeState,
    config: RunnableConfig,
    *,
    phase: str,
    display_name: str,
    agent_factory,
    message_builder,
    artifact_name: str,
    state_key: str,
    budget: int,
    budget_extended: int,
    budget_max: int,
    budget_exhausted_reason: str,
    budget_exhausted_options: list[str],
    budget_exhausted_message: str,
    missing_artifact_reason: Optional[str] = None,
    missing_retries_state_key: Optional[str] = None,
    missing_redo_label: str = "",
    missing_skip_label: str = "",
    missing_message: str = "",
    auto_extend_min_tool_calls: int = 5,
    artifact_fix_fn=None,
    on_success=None,
) -> dict[str, Any] | Command:
    """Shared control flow for the three budgeted analysis agents
    (site_analyzer, product_analyzer, navigation_agent).

    Encapsulates the previously triplicated pattern:
      budget-retry count math → optional budget-extension prompt → invoke →
      (on missing artifact) auto-extend-if-≥N-tool-calls → re-invoke →
      budget_exhausted_* interrupt → [missing_artifact_* interrupt].

    Per-phase variation is passed via kwargs. The ``on_success`` hook (if given)
    is applied in BOTH the primary and auto-extend success paths, normalising a
    prior asymmetry where the auto-extend path skipped phase-specific
    post-processing (e.g. product's anti-bot strategy enforcement, site's
    probe_result surfacing) — both are idempotent/additive, so applying them
    consistently is strictly safer.

    Resume contract: the budget interrupts here only SET ``interrupt_reason``
    and ``goto="human_approval"``; the actual ``interrupt()`` call site lives in
    the ``human_approval`` node, so LangGraph's interrupt_id is assigned there
    and is unaffected by this centralisation. Each phase's reason string
    (budget_exhausted_site / _product / _navigation, missing_artifact_site /
    _product) is preserved verbatim, so services.py INTERRUPT_TO_APPROVAL_TYPE
    and route_from_human_approval resume routing keep working unchanged.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    is_budget_retry = state.get("interrupt_reason") == budget_exhausted_reason
    is_missing_artifact = (
        missing_artifact_reason is not None
        and state.get("interrupt_reason") == missing_artifact_reason
    )
    # Reset on phase entry: only carry the count forward when THIS is a
    # budget-retry re-entry for THIS phase. A stale count from a prior phase
    # (e.g. site exhaustion → product reads count=1) corrupts the downstream
    # budget + skips the escalation interrupt. P0-8.
    _prior = (
        state.get("budget_retry_count", 0)
        if (is_budget_retry or is_missing_artifact)
        else 0
    )
    budget_retries = (
        _prior
        + (1 if is_budget_retry else 0)
        + (1 if is_missing_artifact else 0)
    )
    recursion_limit = budget_extended if budget_retries > 0 else budget
    _notify_phase(job_id, phase, "running")
    set_tool_context(dict(state), agent_name=phase)
    try:
        logger.info(
            "_run_budgeted_agent[%s]: starting (job %s, budget=%d, retry=%d)",
            phase,
            job_id,
            recursion_limit,
            budget_retries,
        )
        messages = message_builder(state)

        if budget_retries > 0:
            previous_summary = state.get("budget_retry_summary", "")
            augmented = (
                "## BUDGET EXTENSION\n"
                f"Previous analysis ran out of the call budget. "
                f"You now have {recursion_limit} calls.\n\n"
                "### CRITICAL INSTRUCTION\n"
                f"You MUST write {artifact_name} before running out of calls. "
                "Write the file as soon as you have enough data — do NOT explore further.\n\n"
                f"### Previous Findings\n"
                f"Use these findings to skip re-discovery. Fill any gaps and write the output file.\n\n"
                f"{previous_summary}\n\n"
                f"---\n\n"
            )
            original_content = messages[0].content
            messages = [HumanMessage(content=augmented + original_content)]

        _log_agent_context(state, display_name, messages)
        agent = agent_factory(site_slug=slug)
        agent_cfg = _agent_config(config, phase)
        hb = _start_heartbeat(job_id, display_name)
        # F5: any raise between start and stop (DB outage, agent factory
        # failure) previously leaked the self-rescheduling timer chain.
        try:
            result = _invoke_agent_with_timeout(agent, messages, agent_cfg, phase, job_id)
        finally:
            _stop_heartbeat(hb)
        _persist_agent_logs(state, result, display_name, config)

        if artifact_fix_fn is not None:
            artifact_fix_fn(slug)

        root = _get_project_root()
        output_exists = os.path.isfile(
            os.path.join(root, "workspace", slug, artifact_name)
        )

        if output_exists:
            _notify_phase(job_id, phase, "done")
            analysis = _read_json_artifact(root, slug, artifact_name)
            if on_success is not None:
                _ret = on_success(analysis, state)
                if _ret is not None:
                    return _ret
            return {"messages": [], state_key: analysis}

        tool_call_count = sum(
            1
            for m in (result.get("messages") or [])
            if m.__class__.__name__ == "ToolMessage"
        )
        summary = _extract_previous_findings(result)

        # Auto-extend: if the agent ran out of calls BUT made real progress
        # (≥ N tool calls), give it +10 calls once and re-invoke with a
        # "write the file NOW" instruction. This catches the common case where
        # the agent was one step away from writing the artifact.
        if recursion_limit < budget_max and tool_call_count >= auto_extend_min_tool_calls:
            extended_limit = min(recursion_limit + 10, budget_max)
            logger.info(
                "_run_budgeted_agent[%s]: auto-extending budget %d -> %d for job %s (made %d tool calls)",
                phase,
                recursion_limit,
                extended_limit,
                job_id,
                tool_call_count,
            )
            augmented = (
                "## BUDGET AUTO-EXTENSION\n"
                f"You ran out of calls but made {tool_call_count} tool calls (progress detected).\n"
                f"You now have {extended_limit} calls total.\n\n"
                "### CRITICAL INSTRUCTION\n"
                f"You MUST write {artifact_name} NOW. You have all the data you need. "
                "Do NOT explore further — write the output file immediately.\n\n"
                f"### Previous Findings\n{summary}\n\n---\n\n"
            )
            original_content = message_builder(state)[0].content
            retry_messages = [HumanMessage(content=augmented + original_content)]
            agent_cfg2 = _agent_config(config, phase)
            result = _invoke_agent_with_timeout(agent, retry_messages, agent_cfg2, phase, job_id)
            _persist_agent_logs(state, result, display_name, config)

            if artifact_fix_fn is not None:
                artifact_fix_fn(slug)

            output_exists = os.path.isfile(
                os.path.join(root, "workspace", slug, artifact_name)
            )
            if output_exists:
                _notify_phase(job_id, phase, "done")
                analysis = _read_json_artifact(root, slug, artifact_name)
                if on_success is not None:
                    _ret = on_success(analysis, state)
                    if _ret is not None:
                        return _ret
                return {"messages": [], state_key: analysis}
            summary = _extract_previous_findings(result)

        # No artifact after invoke (+ optional auto-extend). First time around,
        # surface a budget-escalation interrupt; on retry, fall through to the
        # missing-artifact gate (or proceed) below.
        if budget_retries < 1:
            logger.warning(
                "_run_budgeted_agent[%s]: %s missing after run (job %s). "
                "Routing to human_approval for budget escalation.",
                phase,
                artifact_name,
                job_id,
            )
            return Command(
                update={
                    "messages": [],
                    "interrupt_reason": budget_exhausted_reason,
                    "interrupt_message": budget_exhausted_message,
                    "interrupt_options": budget_exhausted_options,
                    "interrupt_decisions": options_to_decisions(budget_exhausted_options),
                    "budget_retry_count": budget_retries,
                    "budget_retry_summary": summary,
                },
                goto="human_approval",
            )

        if missing_artifact_reason is not None:
            retries_now = int(state.get(missing_retries_state_key, 0)) + 1
            if retries_now < MAX_OUTER_RETRIES:
                logger.warning(
                    "_run_budgeted_agent[%s]: still no output (job %s, retries=%d). Offering redo.",
                    phase,
                    job_id,
                    retries_now,
                )
                options = [
                    missing_redo_label,
                    missing_skip_label,
                    "Cancel entire job",
                ]
                return Command(
                    update={
                        "messages": [],
                        "interrupt_reason": missing_artifact_reason,
                        "interrupt_message": missing_message.format(summary=summary[:500]),
                        "interrupt_options": options,
                        "interrupt_decisions": options_to_decisions(options),
                        "budget_retry_count": budget_retries,
                        "budget_retry_summary": summary,
                        missing_retries_state_key: retries_now,
                    },
                    goto="human_approval",
                )

            logger.warning(
                "_run_budgeted_agent[%s]: still no output after %d retries (job %s). Proceeding.",
                phase,
                retries_now,
                job_id,
            )
            return {
                "messages": [],
                missing_retries_state_key: retries_now,
            }

        # Navigation has no missing-artifact gate (Wave 1 removed
        # missing_artifact_navigation) — proceed with whatever we have.
        logger.warning(
            "_run_budgeted_agent[%s]: still no output after retries (job %s). Proceeding.",
            phase,
            job_id,
        )
        return {"messages": []}
    except Exception:
        _notify_phase(job_id, phase, "failed")
        raise
    finally:
        clear_tool_context()


NAVIGATION_ANALYSIS_BUDGET = 40
NAVIGATION_ANALYSIS_BUDGET_EXTENDED = 60
NAVIGATION_ANALYSIS_MAX_BUDGET = 60


def _invoke_site_analyzer(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    def _on_success(analysis: dict, st: ScrapeState):
        update: dict[str, Any] = {"messages": [], "site_analysis": analysis}
        connectivity = analysis.get("connectivity", {})
        if connectivity:
            product_url = st.get("product_url") or ""
            update["probe_result"] = {
                "url": product_url,
                "connectivity": connectivity,
                "platform": analysis.get("platform", ""),
                "anti_bot_detected": analysis.get("anti_bot_detected", False),
            }
            update["probe_url"] = product_url
        # F13: fold the old conditional-edge routing (_route_after_site_analyzer)
        # into the Command so budget/missing-artifact interrupts are NOT unioned
        # with it (LangGraph executes both a Command goto AND any registered
        # out-edges — the D6 shadow-branch bug: a paused job ran the ghost
        # update_tracker_analysis → validate_analysis chain in parallel with
        # the human_approval interrupt). This node must have NO out-edges.
        input_mode = st.get("input_mode", "url_list")
        if input_mode in ("navigation", "list_page", "search_term"):
            logger.info(
                "site_analyzer: input_mode=%s → browser_traverse (F13 Command route)",
                input_mode,
            )
            return Command(goto="browser_traverse", update=update)
        logger.info(
            "site_analyzer: input_mode=%s → update_tracker_analysis (F13 Command route)",
            input_mode,
        )
        return Command(goto="update_tracker_analysis", update=update)

    return _run_budgeted_agent(
        state,
        config,
        phase="site_analyzer",
        display_name="site-analyzer",
        agent_factory=create_site_analyzer,
        message_builder=build_site_analyzer_message,
        artifact_name="site_analysis.json",
        state_key="site_analysis",
        budget=SITE_ANALYSIS_BUDGET,
        budget_extended=SITE_ANALYSIS_BUDGET_EXTENDED,
        budget_max=SITE_ANALYSIS_MAX_BUDGET,
        budget_exhausted_reason="budget_exhausted_site",
        budget_exhausted_options=[
            "Retry with higher budget (50 calls)",
            "Continue anyway",
            "Cancel",
        ],
        budget_exhausted_message=(
            f"Site analysis did not complete — the agent used its call budget "
            f"({SITE_ANALYSIS_BUDGET} calls) without writing site_analysis.json. "
            f"This site may be complex. Choose how to proceed."
        ),
        missing_artifact_reason="missing_artifact_site",
        missing_retries_state_key="site_analysis_retries",
        missing_redo_label="Redo site analysis",
        missing_skip_label="Continue without site analysis",
        missing_message=(
            "Site analysis could not produce site_analysis.json after extended attempts. "
            "The agent explored the site but didn't write the output file.\n\n"
            "Previous findings summary:\n{summary}\n\n"
            "Choose how to proceed."
        ),
        auto_extend_min_tool_calls=5,
        artifact_fix_fn=lambda slug: _fix_json_artifact(slug, "site_analysis.json"),
        on_success=_on_success,
    )


def _invoke_product_analyzer(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    # Re-map mode: route_after_testing sent us here because code_tester flagged a
    # MAPPING failure (test_report.remediation.target == "mapping"). After the
    # agent re-maps the failed fields, route straight to code_writer (skipping
    # normalize/validate) so the scraper regenerates against the corrected mapping.
    _pa_test_report = state.get("test_report") or {}
    _pa_remediation = (
        _pa_test_report.get("remediation") if isinstance(_pa_test_report, dict) else None
    )
    is_remap = isinstance(_pa_remediation, dict) and _pa_remediation.get("target") == "mapping"
    if is_remap:
        logger.info(
            "_invoke_product_analyzer: RE-MAP mode (job %s) — fields %s",
            state.get("job_id", 0),
            _pa_remediation.get("fields"),
        )

    def _on_success(analysis: dict, st: ScrapeState):
        # Anti-bot ⇒ playwright (cloak). KEPT: code_writer otherwise picks the
        # discovered API (which Akamai also guards → 400). Gated by _PATCHES_ENABLED.
        # Applied in both the primary and auto-extend success paths (normalised
        # by _run_budgeted_agent): previously the auto-extend path skipped this,
        # leaking an un-enforced strategy when the second invoke produced output.
        if _PATCHES_ENABLED:
            _enforce_anti_bot_strategy(
                analysis, st.get("site_slug", ""), "product_analysis.json"
            )
        if is_remap:
            remap_count = int(st.get("remap_count", 0) or 0) + 1
            logger.info(
                "_invoke_product_analyzer: re-mapped failed fields → code_writer "
                "(remap %d, job %s)",
                remap_count,
                st.get("job_id", 0),
            )
            return Command(
                goto="code_writer",
                update={
                    "messages": [],
                    "product_analysis": analysis,
                    "remap_count": remap_count,
                },
            )
        # F13: happy path routes via Command (the static product_analyzer →
        # normalize_fields edge was deleted — it unioned with the budget/
        # remap Commands; the D6 shadow branch). normalize_fields keeps its
        # own static out-edge to validate_coverage.
        return Command(
            goto="normalize_fields",
            update={"messages": [], "product_analysis": analysis},
        )

    return _run_budgeted_agent(
        state,
        config,
        phase="product_analyzer",
        display_name="product-analyzer",
        agent_factory=create_product_analyzer,
        message_builder=build_product_analyzer_message,
        artifact_name="product_analysis.json",
        state_key="product_analysis",
        budget=PRODUCT_ANALYSIS_BUDGET,
        budget_extended=PRODUCT_ANALYSIS_BUDGET_EXTENDED,
        budget_max=PRODUCT_ANALYSIS_MAX_BUDGET,
        budget_exhausted_reason="budget_exhausted_product",
        budget_exhausted_options=[
            "Retry with higher budget (70 calls)",
            "Continue anyway",
            "Cancel",
        ],
        budget_exhausted_message=(
            f"Product analysis did not complete — the agent used its call budget "
            f"({PRODUCT_ANALYSIS_BUDGET} calls) without writing product_analysis.json. "
            f"This product page may be complex. Choose how to proceed."
        ),
        missing_artifact_reason="missing_artifact_product",
        missing_retries_state_key="product_analysis_retries",
        missing_redo_label="Redo product analysis",
        missing_skip_label="Continue without product analysis",
        missing_message=(
            "Product analysis could not produce product_analysis.json after extended attempts. "
            "The agent explored the page but didn't write the output file.\n\n"
            "Previous findings summary:\n{summary}\n\n"
            "Choose how to proceed."
        ),
        auto_extend_min_tool_calls=5,
        artifact_fix_fn=lambda slug: _fix_json_artifact(slug, "product_analysis.json"),
        on_success=_on_success,
    )


def _sanitize_nav_domains(analysis: dict, job_url: str) -> dict:
    """F17: blank cross-registrable-domain navigation artifacts (blank + warn).

    Prod 331 (prettylittlething.us): browser_traverse followed a footer locale
    link, so search.working_url / discovery.listing_url / every url_example
    ended up prettylittlething.com.au under a .us job — 80/80 rows shipped
    wrong-domain. No domain check existed anywhere in the chain. BLANKING
    (not rejecting) is safe because the site-root fallback upstream re-fills
    discovery.listing_url on the CORRECT domain when the artifact is empty;
    if nothing valid is found, F9 fails the job loudly instead of shipping
    contaminated data.
    """
    try:
        from experimental.nav_traversal.traversal import _registrable

        job_reg = _registrable(job_url)
        if not job_reg:
            return analysis
        blanks: list[str] = []

        def _check(value, field):
            if isinstance(value, str) and value.startswith("http") and _registrable(value) not in ("", job_reg):
                blanks.append(f"{field}={value[:60]}")
                return True
            return False

        search = analysis.get("search") or {}
        if isinstance(search, dict):
            for k in ("working_url", "listing_url_used"):
                if _check(search.get(k), f"search.{k}"):
                    search[k] = ""
        disc = analysis.get("discovery") or {}
        if isinstance(disc, dict):
            if _check(disc.get("listing_url"), "discovery.listing_url"):
                disc["listing_url"] = ""
        il = analysis.get("item_links") or {}
        if isinstance(il, dict):
            examples = il.get("url_examples")
            if isinstance(examples, list):
                kept = [
                    u for u in examples
                    if not (isinstance(u, str) and u.startswith("http")
                            and _registrable(u) not in ("", job_reg))
                ]
                if len(kept) != len(examples):
                    blanks.append(f"item_links.url_examples ({len(examples) - len(kept)} dropped)")
                    il["url_examples"] = kept
        if blanks:
            logger.warning(
                "browser_traverse: F17 domain guard blanked cross-domain nav "
                "artifacts for job URL %s (registrable %s): %s",
                job_url[:60], job_reg, "; ".join(blanks),
            )
        return analysis
    except Exception as exc:
        logger.warning("F17 domain guard error (passing through): %s", exc)
        return analysis


def _project_api_endpoint(api: Any) -> dict:
    """Project verify_api's descriptor for navigation_analysis.api_endpoint.

    Uses the "url" key (what subagents.py:2208 checks), not "api_url", and
    RETAINS the measured evidence the fetch already produced: count,
    items_per_page, sample_keys, content_type. The old projection kept only
    {url, count, items_per_page}, discarding sample_keys — which is what
    left the strategy gate with nothing but a URL to judge (job-12).
    """
    if not api or not isinstance(api, dict) or not api.get("url"):
        return api or {}
    return {
        "url": api["url"],
        "count": api.get("count"),
        "items_per_page": api.get("items_per_page"),
        "sample_keys": api.get("sample_keys"),
        "content_type": api.get("content_type"),
    }


def _invoke_navigation_traverse(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    """Graph wrapper for the browser-driven navigation traversal node.

    Replaces the 3-node navigation_explore → navigation_agent → navigation_synthesize
    pipeline with a single browser_traverse call (LLM-driven MCP browser walk from
    the homepage to a listing page). The TraversalResult is converted to the same
    navigation_analysis dict shape that downstream nodes (product_analyzer, etc.)
    already consume. Falls back to the archived navigate_explore + navigate_synthesize
    when the MCP browser is unavailable.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    url = state.get("url", "")
    content_type = state.get("page_type", "product")
    query = state.get("search_criteria", "") or ""

    _notify_phase(job_id, "browser_traverse", "running")
    try:
        from experimental.nav_traversal.traversal import browser_traverse, traverse

        _input_mode = state.get("input_mode") or ""
        result = browser_traverse(
            url, content_type, query,
            trust_start_as_listing=_input_mode in ("list_page", "search_term"),
        )

        # MCP unavailable → fall back to the archived deterministic explorer +
        # synthesizer (imported lazily here so the fallback path is self-contained).
        if "MCP" in (result.notes or ""):
            logger.info(
                "browser_traverse: MCP unavailable (%s) — falling back to "
                "navigate_explore + navigate_synthesize (job %s)",
                result.notes, job_id,
            )
            from .nodes.navigate_explore import navigate_explore
            from .nodes.navigate_synthesize import navigate_synthesize

            explore_result = navigate_explore(dict(state), config)
            if isinstance(explore_result, dict):
                state.update(explore_result)
            synth_result = navigate_synthesize(dict(state), config)
            # F17: apply the same domain guard on the fallback path's analysis
            if isinstance(synth_result, dict):
                _na = synth_result.get("navigation_analysis")
                if isinstance(_na, dict):
                    synth_result["navigation_analysis"] = _sanitize_nav_domains(_na, url)
            _notify_phase(job_id, "browser_traverse", "done")
            return synth_result if isinstance(synth_result, dict) else {"messages": []}

        # browser_traverse didn't reach the goal (but MCP was available) —
        # fall back to the HTTP-first traverse() which handles form-driven
        # sites (locumtenens QuickSearch) and API discovery (aya) that the
        # LLM-driven browser approach couldn't complete within its budget.
        if not result.reached:
            logger.info(
                "browser_traverse: didn't reach goal (%s) — falling back to "
                "HTTP traverse() (job %s)",
                result.notes, job_id,
            )
            fb_result = traverse(url, content_type, query)
            if fb_result.reached:
                result = fb_result  # use the fallback's result
                logger.info(
                    "HTTP traverse fallback reached goal: %s (job %s)",
                    result.goal_url, job_id,
                )
            else:
                logger.info(
                    "HTTP traverse also didn't reach goal — using best partial (job %s)",
                    job_id,
                )

        # Extract item URL examples so product_analyzer + code_tester have
        # ready-made sample URLs (avoids ~6.5 min of auto-discovery). Prefer the
        # real item hrefs browser_traverse captured from the RENDERED goal page
        # (correct for CSR/JS-rendered listings — Coveo/React/Vue). Fall back to a
        # plain HTTP fetch + link parse ONLY when no browser item links exist
        # (SSR pages where the raw HTML already contains the item anchors).
        url_examples: list[str] = list(getattr(result, "item_links", []) or [])[:20]
        if not url_examples and result.goal_url:
            try:
                from experimental.nav_traversal.traversal import _default_fetch, extract_links

                page_resp = _default_fetch(result.goal_url)
                if page_resp.get("ok"):
                    links = extract_links(page_resp.get("text", ""), result.goal_url)
                    url_examples = [l["href"] for l in links[:20] if l.get("href")]
            except Exception as exc:
                logger.info(
                    "browser_traverse: url_examples extraction failed (%s)", exc
                )
            if url_examples:
                logger.info(
                    "browser_traverse: extracted %d url_examples from goal page (job %s)",
                    len(url_examples), job_id,
                )
        elif url_examples:
            logger.info(
                "browser_traverse: using %d browser-captured item links as url_examples (job %s)",
                len(url_examples), job_id,
            )

        # ── Listing-reachability fallback (uindex class) ────────────────────
        # browser_traverse can exhaust its budget without judging any page a
        # listing (Cloudflare wall, JS gate, slow render) → discovery comes back
        # {listing_url: null, listing_reached: false}. That cascades to no
        # discovery_config, a detail-page sample, and 0 items — even when the
        # site ROOT is a perfectly good listing (uindex homepage: 120 torrents,
        # HTTP 200). When the navigator didn't reach a listing but the site root
        # is already proven reachable (probe_result.connectivity.method_that_worked,
        # populated upstream by check_accessibility — ZERO new network calls),
        # fall back to the root as the listing. Fixes the value at the source so
        # every downstream consumer (run_execution, _derive_strategy, code_writer)
        # reads the corrected URL. url_list mode is exempt — it has no listing.
        _disc_fb = dict(getattr(result, "discovery", {}) or {})
        _input_mode = (state.get("input_mode") or "").strip()
        _probe = state.get("probe_result") or {}
        _conn = _probe.get("connectivity") if isinstance(_probe, dict) else None
        _root_method = ""
        if isinstance(_conn, dict):
            _root_method = str(_conn.get("method_that_worked") or "").strip()
        # Site ORIGIN (scheme+host), NOT state.url verbatim — the input URL can be
        # a detail/sample page (uindex job 184 submitted a details.php?id=... URL).
        # Falling back to that would point discovery at a single detail page.
        _site_root = ""
        try:
            from urllib.parse import urlparse as _urlparse
            _p = _urlparse((state.get("url") or "").strip())
            if _p.scheme in ("http", "https") and _p.netloc:
                _site_root = f"{_p.scheme}://{_p.netloc}/"
        except Exception:
            _site_root = ""
        if (
            _input_mode in ("navigation", "list_page", "search_term")
            and _site_root
            and _root_method
            and not _disc_fb.get("listing_reached")
        ):
            # list_page: the user EXPLICITLY provided the listing URL
            # (search_criteria) — prefer it over the site root. The traverse
            # probe failing to reach it (empty page-state / JS gate / anti-bot
            # wall) says nothing about the EXECUTION browser (cloak=True full
            # stealth via browser_service), which is far more capable than the
            # probe. Substituting the site root silently redirects discovery to
            # homepage featured links (rmwilliams job 227: 12 sweatshirts +
            # belts/boot-polish/mug instead of the requested sweatshirts
            # listing). navigation/search_term keep the root fallback — their
            # "listing" is genuinely discovered, not user-specified.
            _fallback_listing = _site_root
            if _input_mode == "list_page":
                # Job 309 (pillowtalk e2e): search_criteria is how INTAKE carries
                # the listing URL, but a list_page job's `url` field is ALSO the
                # user-provided listing by definition — jobs submitted without
                # search_criteria (quick form / API / shell) fell through to the
                # site root, silently redirecting discovery to homepage featured
                # links (the exact rmwilliams job-227 failure this branch exists
                # to prevent). Candidate order: search_criteria → state.url → root.
                _crit = (state.get("search_criteria") or "").strip()
                try:
                    _cp = _urlparse(_crit)
                    if _cp.scheme in ("http", "https") and _cp.netloc == _p.netloc:
                        _fallback_listing = _crit
                except Exception:
                    pass
                if _fallback_listing is _site_root:
                    try:
                        _up = _urlparse((state.get("url") or "").strip())
                        if _up.scheme in ("http", "https") and _up.netloc == _p.netloc:
                            _fallback_listing = _up.geturl()
                    except Exception:
                        pass
            _disc_fb = {
                "listing_url": _fallback_listing,
                "listing_reached": True,
                "pagination": _disc_fb.get("pagination") or {"type": "load_more"},
            }
            logger.warning(
                "browser_traverse: listing not reached — falling back to %s (via %s, job %s)",
                _fallback_listing, _root_method, job_id,
            )

        # [jobs 83/88 RCA] The traversal result IS the discovery contract the
        # whole pipeline leans on — surface it in the job log. This phase
        # previously emitted zero SessionLog rows, so neither RCA could be
        # reconstructed from the UI.
        _log_event_row(
            job_id,
            "navigator",
            "[NAV-SUMMARY] reached=%s working_url=%s listing_url=%s "
            "listing_reached=%s mechanism=%s form_method=%s item_links=%d notes=%s"
            % (
                getattr(result, "reached", "?"),
                str(getattr(result, "goal_url", "") or "")[:120],
                str(_disc_fb.get("listing_url") or "")[:120],
                _disc_fb.get("listing_reached"),
                getattr(result, "mechanism", "") or "?",
                getattr(result, "goal_method", "GET") or "GET",
                len(url_examples or []),
                str(getattr(result, "notes", "") or "")[:200],
            ),
        )

        analysis = {
            "discovery_method": "browser_traverse" if result.reached else "fallback",
            "search": {
                "working_url": result.goal_url,
                "has_search": True,
                # propagate form-POST replay info so code_writer can replay the search
                "form_method": result.goal_method if hasattr(result, "goal_method") else "GET",
                "form_data": dict(result.goal_data) if hasattr(result, "goal_data") and result.goal_data else {},
                "form_action": result.goal_request_url if hasattr(result, "goal_request_url") else "",
            },
            "item_links": {
                "url_examples": url_examples,
                # propagate signals so code_writer has SOMETHING to work with
                "signals": result.signals if hasattr(result, "signals") else {},
            },
            "data_source": getattr(result, "mechanism", "") or "unknown",
            # Bug fix: use "url" key (what subagents.py:2208 checks), not "api_url".
            # Preserve count/items_per_page so _derive_strategy can gate the
            # internal_api override on the API having DEMONSTRABLY returned records
            # (items_per_page>0) — a bare URL with 0 results (Coveo /coveo/rest/search
            # returns totalCount=0 without the browser's filter) must NOT trigger
            # internal_api, or the job diverts from playwright to a doomed strategy.
            "api_endpoint": _project_api_endpoint(result.api),
            "rendering_verified": "browser",
            # propagate the full path so downstream can see how we got here
            "traversal_path": result.path[:8] if hasattr(result, "path") else [],
            # Phase 1 (JS-listing+pagination class fix): carry the discovery
            # contract (listing_reached, listing_url, pagination type) from the
            # navigator through state to run_execution + code_writer. The
            # navigator ALREADY detects these; the graph must not drop them.
            "discovery": _disc_fb,
            "pagination": _disc_fb.get("pagination") or {},
        }

        # F17: blank cross-registrable-domain artifacts before persisting.
        analysis = _sanitize_nav_domains(analysis, url)

        # Persist to workspace/{slug}/navigation_analysis.json
        root = _get_project_root()
        na_path = os.path.join(root, "workspace", slug, "navigation_analysis.json")
        try:
            os.makedirs(os.path.dirname(na_path), exist_ok=True)
            with open(na_path, "w", encoding="utf-8") as f:
                json.dump(analysis, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning(
                "browser_traverse: failed to write navigation_analysis.json: %s", exc
            )

        _notify_phase(job_id, "browser_traverse", "done")
        return {"navigation_analysis": analysis, "messages": []}
    except Exception as exc:
        logger.exception("_invoke_navigation_traverse failed (job %s): %s", job_id, exc)
        _notify_phase(job_id, "browser_traverse", "failed")
        return {"messages": []}


# ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
# ARCHIVED @_with_api_retry
# ARCHIVED def _invoke_navigation_agent(
# ARCHIVED     state: ScrapeState, config: RunnableConfig
# ARCHIVED ) -> dict[str, Any] | Command:
# ARCHIVED     return _run_budgeted_agent(
# ARCHIVED         state,
# ARCHIVED         config,
# ARCHIVED         phase="navigation_agent",
# ARCHIVED         display_name="navigation-agent",
# ARCHIVED         agent_factory=create_navigation_agent,
# ARCHIVED         message_builder=build_navigation_agent_message,
# ARCHIVED         artifact_name="navigation_analysis.json",
# ARCHIVED         state_key="navigation_analysis",
# ARCHIVED         budget=NAVIGATION_ANALYSIS_BUDGET,
# ARCHIVED         budget_extended=NAVIGATION_ANALYSIS_BUDGET_EXTENDED,
# ARCHIVED         budget_max=NAVIGATION_ANALYSIS_MAX_BUDGET,
# ARCHIVED         budget_exhausted_reason="budget_exhausted_navigation",
# ARCHIVED         budget_exhausted_options=[
# ARCHIVED             "Retry with higher budget",
# ARCHIVED             "Continue anyway",
# ARCHIVED             "Cancel",
# ARCHIVED         ],
# ARCHIVED         budget_exhausted_message=(
# ARCHIVED             f"Navigation analysis did not complete — the agent used its call budget "
# ARCHIVED             f"({NAVIGATION_ANALYSIS_BUDGET} calls) without writing navigation_analysis.json. "
# ARCHIVED             f"This site may have complex navigation. Choose how to proceed."
# ARCHIVED         ),
# ARCHIVED         missing_artifact_reason=None,
# ARCHIVED         auto_extend_min_tool_calls=3,
# ARCHIVED     )
# ARCHIVED
# ARCHIVED
# ARCHIVED def _explore_findings_solid(findings: dict) -> bool:
# ARCHIVED     """True when the deterministic explorer already found a real listing + data.
# ARCHIVED
# ARCHIVED     Used to decide whether to SKIP the heavy navigation_agent. "Solid" = a
# ARCHIVED     working listing URL AND a real data signal: embedded-JSON blob, a captured
# ARCHIVED     backend API endpoint, or >=10 real detail links. (Classic-search forms are
# ARCHIVED     handled separately by ``_explore_has_classic_form`` — they still need the
# ARCHIVED     agent to drive the form even when findings look solid.)
# ARCHIVED     """
# ARCHIVED     if not isinstance(findings, dict):
# ARCHIVED         return False
# ARCHIVED     lp = findings.get("listing_page") or {}
# ARCHIVED     working_url = bool((lp.get("url") or "").strip())
# ARCHIVED     ds = lp.get("data_source")
# ARCHIVED     real_links = len(lp.get("product_links") or [])
# ARCHIVED     has_api = bool(findings.get("api_endpoints") or lp.get("api_endpoints"))
# ARCHIVED     solid_data = (
# ARCHIVED         ds == "embedded_json"
# ARCHIVED         or has_api
# ARCHIVED         or (ds == "detail_links" and real_links >= 10)
# ARCHIVED     )
# ARCHIVED     return working_url and bool(solid_data)
# ARCHIVED
# ARCHIVED
# ARCHIVED def _explore_has_classic_form(findings: dict) -> bool:
# ARCHIVED     """True when explore detected a classic multi-select POST search form.
# ARCHIVED
# ARCHIVED     Those forms (e.g. locumtenens QuickSearch) need the agent's browser tools to
# ARCHIVED     drive, so they trigger a navigation_agent handoff even when explore is solid.
# ARCHIVED     """
# ARCHIVED     if not isinstance(findings, dict):
# ARCHIVED         return False
# ARCHIVED     lp = findings.get("listing_page") or {}
# ARCHIVED     hn = findings.get("homepage_nav") or {}
# ARCHIVED     return bool(
# ARCHIVED         findings.get("classic_search")
# ARCHIVED         or (hn.get("classic_search") if isinstance(hn, dict) else None)
# ARCHIVED         or (lp.get("classic_search") if isinstance(lp, dict) else None)
# ARCHIVED     )
# ARCHIVED
# ARCHIVED
# ARCHIVED def _navigation_handoff_decision(findings: dict, anti_bot: bool) -> tuple[bool, str | None]:
# ARCHIVED     """Decide whether to hand off to the LLM navigation_agent after explore.
# ARCHIVED
# ARCHIVED     Returns ``(handoff, reason)``. ``handoff=False`` means SKIP the agent (explore
# ARCHIVED     succeeded) and let ``navigation_synthesize`` build the analysis from findings.
# ARCHIVED     Rules: never hand off for anti-bot (agent's MCP browser isn't cloak-enabled);
# ARCHIVED     hand off when explore is NOT solid OR a classic POST form was detected.
# ARCHIVED     """
# ARCHIVED     if anti_bot:
# ARCHIVED         return False, None
# ARCHIVED     solid = _explore_findings_solid(findings)
# ARCHIVED     form = _explore_has_classic_form(findings)
# ARCHIVED     if (not solid) or form:
# ARCHIVED         return True, ("form_driving_needed" if form else "explore_insufficient")
# ARCHIVED     return False, None
# ARCHIVED
# ARCHIVED
# ARCHIVED def _invoke_navigation_explore(
# ARCHIVED     state: ScrapeState, config: RunnableConfig
# ARCHIVED ) -> dict[str, Any] | Command:
# ARCHIVED     """Graph wrapper for the deterministic navigation exploration node."""
# ARCHIVED     from .nodes.navigate_explore import navigate_explore as _explore
# ARCHIVED
# ARCHIVED     job_id = state.get("job_id", 0)
# ARCHIVED     _notify_phase(job_id, "navigation_explore", "running")
# ARCHIVED     try:
# ARCHIVED         result = _explore(dict(state), config)
# ARCHIVED         _notify_phase(job_id, "navigation_explore", "done")
# ARCHIVED
# ARCHIVED         if isinstance(result, dict) and result.get("playwright_unavailable"):
# ARCHIVED             logger.info(
# ARCHIVED                 "_invoke_navigation_explore: Playwright unavailable, "
# ARCHIVED                 "interrupting for user decision (job %s)",
# ARCHIVED                 job_id,
# ARCHIVED             )
# ARCHIVED             options = ["Use probe_html (no interaction)", "Retry Playwright", "Cancel"]
# ARCHIVED             return Command(
# ARCHIVED                 update={
# ARCHIVED                     "navigation_findings": result.get("navigation_findings"),
# ARCHIVED                     "interrupt_reason": "playwright_unavailable",
# ARCHIVED                     "interrupt_message": (
# ARCHIVED                         "Playwright MCP is unavailable but the site is NOT Akamai-protected. "
# ARCHIVED                         "The explore fell back to HTTP but may have missed JS-rendered content.\n\n"
# ARCHIVED                         "Options:\n"
# ARCHIVED                         "- **Use probe_html**: Proceed with single-page fetch (no clicking/scrolling)\n"
# ARCHIVED                         "- **Retry Playwright**: Retry — check that the browser_service container is running\n"
# ARCHIVED                         "- **Cancel**: Abort this job"
# ARCHIVED                     ),
# ARCHIVED                     "interrupt_options": options,
# ARCHIVED                     "interrupt_decisions": options_to_decisions(options),
# ARCHIVED                 },
# ARCHIVED                 goto="human_approval",
# ARCHIVED             )
# ARCHIVED
# ARCHIVED         # Handoff to the LLM navigation_agent when the deterministic explorer
# ARCHIVED         # detected a search form (classic_search) but couldn't get many real item
# ARCHIVED         # links from it — e.g. a JS/validation-gated POST form (locumtenens
# ARCHIVED         # QuickSearch: required-specialty + decorative-vs-real submit button). The
# ARCHIVED         # agent drives the form with browser tools + the navigation-patterns skill.
# ARCHIVED         # Threshold is generous (< 30) because listing_page.product_links can be
# ARCHIVED         # inflated by category/nav noise; classic_search detection (a multi-select
# ARCHIVED         # form was found) is the real signal of a form-driven job board.
# ARCHIVED         if isinstance(result, dict):
# ARCHIVED             # navigate_explore has inconsistent return shapes — some paths return
# ARCHIVED             # {"navigation_findings": findings, ...}, others return the bare
# ARCHIVED             # findings dict. Handle both.
# ARCHIVED             _f = result.get("navigation_findings") or result
# ARCHIVED             _lp = _f.get("listing_page") or {}
# ARCHIVED             _pl = len(_lp.get("product_links") or [])
# ARCHIVED             # Anti-bot guard: don't hand off to navigation_agent for anti-bot sites —
# ARCHIVED             # its MCP browser isn't cloak-enabled, so Akamai would block it. Anti-bot
# ARCHIVED             # sites (e.g. calvklein) find few links at analysis time (truncated
# ARCHIVED             # /render) but the RUNTIME scraper (cloak) gets the products, so a low
# ARCHIVED             # analysis-time count is expected + not a failure there.
# ARCHIVED             _probe = state.get("probe_result") or {}
# ARCHIVED             _ab = _probe.get("anti_bot") if isinstance(_probe, dict) else None
# ARCHIVED             _conn = _probe.get("connectivity") if isinstance(_probe, dict) else None
# ARCHIVED             _meth = (
# ARCHIVED                 (_probe.get("method") if isinstance(_probe, dict) else "")
# ARCHIVED                 or (_conn.get("method_that_worked") if isinstance(_conn, dict) else "")
# ARCHIVED                 or ""
# ARCHIVED             )
# ARCHIVED             _anti_bot = bool(
# ARCHIVED                 state.get("anti_bot_detected")
# ARCHIVED                 or (isinstance(_ab, dict) and _ab.get("detected"))
# ARCHIVED                 or str(_meth).startswith(("uc_chrome", "cloak"))
# ARCHIVED             )
# ARCHIVED             # Decide whether the heavy LLM navigation_agent is needed. The
# ARCHIVED             # deterministic explorer is now reliable (LLM URL selector + embedded-
# ARCHIVED             # JSON detector), so SKIP the agent (~10-26 min) when explore already
# ARCHIVED             # found a real listing with data — UNLESS a classic POST search form
# ARCHIVED             # was detected (the agent drives those forms) or the site is anti-bot
# ARCHIVED             # (the agent's MCP browser isn't cloak-enabled → never hand off there).
# ARCHIVED             _handoff, _reason = _navigation_handoff_decision(_f, _anti_bot)
# ARCHIVED             if _handoff:
# ARCHIVED                 logger.info(
# ARCHIVED                     "_invoke_navigation_explore: handing off to navigation_agent "
# ARCHIVED                     "(reason=%s, anti_bot=%s, %d links) (job %s)",
# ARCHIVED                     _reason, _anti_bot, _pl, job_id,
# ARCHIVED                 )
# ARCHIVED                 return Command(
# ARCHIVED                     update={
# ARCHIVED                         "navigation_findings": _f,
# ARCHIVED                         "handoff_reason": _reason,
# ARCHIVED                     },
# ARCHIVED                     goto="navigation_agent",
# ARCHIVED                 )
# ARCHIVED             logger.info(
# ARCHIVED                 "_invoke_navigation_explore: explore solid — SKIPPING "
# ARCHIVED                 "navigation_agent → synthesize (anti_bot=%s, %d links) (job %s)",
# ARCHIVED                 _anti_bot, _pl, job_id,
# ARCHIVED             )
# ARCHIVED
# ARCHIVED         return result
# ARCHIVED     except Exception as exc:
# ARCHIVED         logger.exception("_invoke_navigation_explore failed (job %s): %s", job_id, exc)
# ARCHIVED         _notify_phase(job_id, "navigation_explore", "failed")
# ARCHIVED         return {}
# ARCHIVED
# ARCHIVED
# ARCHIVED def _merge_explore_findings_into_analysis(analysis: dict, root: str, slug: str) -> dict:
# ARCHIVED     """Fill gaps in the navigation_agent's analysis from the explorer's findings.
# ARCHIVED
# ARCHIVED     The agent re-discovers and can produce a sparse/wrong analysis (e.g. aya: it
# ARCHIVED     overwrote a correct embedded-JSON finding with an empty analysis). Critical
# ARCHIVED     fields the explorer reliably found — working URL, data_source, embedded_json,
# ARCHIVED     item links, category links, api_endpoint — are merged in ONLY when the agent
# ARCHIVED     left them missing/empty. Never overwrites a field the agent populated.
# ARCHIVED     """
# ARCHIVED     if not isinstance(analysis, dict):
# ARCHIVED         return analysis
# ARCHIVED     try:
# ARCHIVED         nf_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
# ARCHIVED         if not os.path.isfile(nf_path):
# ARCHIVED             return analysis
# ARCHIVED         with open(nf_path, "r", encoding="utf-8") as f:
# ARCHIVED             findings = json.load(f)
# ARCHIVED     except Exception:
# ARCHIVED         return analysis
# ARCHIVED     lp = findings.get("listing_page") or {}
# ARCHIVED     hn = findings.get("homepage_nav") or {}
# ARCHIVED
# ARCHIVED     # Top-level data-model signals from the listing page
# ARCHIVED     for k in ("data_source", "embedded_json", "rendering_verified", "data_richness"):
# ARCHIVED         v = lp.get(k)
# ARCHIVED         if v not in (None, "", [], {}) and not analysis.get(k):
# ARCHIVED             analysis[k] = v
# ARCHIVED
# ARCHIVED     # search.working_url / listing_url_used
# ARCHIVED     search = analysis.get("search")
# ARCHIVED     if not isinstance(search, dict):
# ARCHIVED         search = {}
# ARCHIVED     wurl = (lp.get("url") or "").strip()
# ARCHIVED     if wurl and not (search.get("working_url") or search.get("listing_url_used")):
# ARCHIVED         search["working_url"] = wurl
# ARCHIVED         search["listing_url_used"] = wurl
# ARCHIVED         analysis["search"] = search
# ARCHIVED
# ARCHIVED     # item_links.url_examples / urls
# ARCHIVED     il = analysis.get("item_links")
# ARCHIVED     if not isinstance(il, dict):
# ARCHIVED         il = {}
# ARCHIVED     if not (il.get("urls") or il.get("url_examples")):
# ARCHIVED         hrefs = []
# ARCHIVED         for p in (lp.get("product_links") or []):
# ARCHIVED             h = p.get("href") if isinstance(p, dict) else p
# ARCHIVED             if isinstance(h, str) and h:
# ARCHIVED                 hrefs.append(h)
# ARCHIVED         if hrefs:
# ARCHIVED             il.setdefault("url_pattern", "")
# ARCHIVED             il["url_examples"] = hrefs[:10]
# ARCHIVED             il["urls"] = hrefs
# ARCHIVED             analysis["item_links"] = il
# ARCHIVED
# ARCHIVED     # categories.category_links
# ARCHIVED     cats = analysis.get("categories")
# ARCHIVED     if not (isinstance(cats, dict) and (cats.get("category_links") or [])):
# ARCHIVED         cat_links = [
# ARCHIVED             c.get("href") for c in (hn.get("category_links") or [])
# ARCHIVED             if isinstance(c, dict) and c.get("href")
# ARCHIVED         ]
# ARCHIVED         if cat_links:
# ARCHIVED             cats = cats if isinstance(cats, dict) else {}
# ARCHIVED             cats["category_links"] = cat_links[:20]
# ARCHIVED             analysis["categories"] = cats
# ARCHIVED
# ARCHIVED     # api_endpoint
# ARCHIVED     if not (isinstance(analysis.get("api_endpoint"), dict) and analysis["api_endpoint"].get("url")):
# ARCHIVED         try:
# ARCHIVED             from .nodes.navigate_synthesize import _best_api_endpoint
# ARCHIVED
# ARCHIVED             best = _best_api_endpoint(findings)
# ARCHIVED             if isinstance(best, dict) and best.get("url"):
# ARCHIVED                 analysis["api_endpoint"] = best
# ARCHIVED         except Exception:
# ARCHIVED             pass
# ARCHIVED
# ARCHIVED     return analysis
# ARCHIVED
# ARCHIVED
# ARCHIVED def _invoke_navigation_synthesize(
# ARCHIVED     state: ScrapeState, config: RunnableConfig
# ARCHIVED ) -> dict[str, Any] | Command:
# ARCHIVED     """Graph wrapper for the navigation synthesis node."""
# ARCHIVED     from .nodes.navigate_synthesize import navigate_synthesize as _synthesize
# ARCHIVED
# ARCHIVED     job_id = state.get("job_id", 0)
# ARCHIVED
# ARCHIVED     # If the LLM navigation_agent already wrote navigation_analysis.json (it runs
# ARCHIVED     # on the form-driven handoff path), skip re-synthesizing from raw findings —
# ARCHIVED     # the agent's structured output IS the analysis. Synthesize would otherwise
# ARCHIVED     # overwrite the agent's work with a re-reading of the (sparse) raw findings.
# ARCHIVED     try:
# ARCHIVED         slug = state.get("site_slug", "")
# ARCHIVED         na_path = os.path.join(_get_project_root(), "workspace", slug, "navigation_analysis.json")
# ARCHIVED         if state.get("handoff_reason") and os.path.isfile(na_path):
# ARCHIVED             root = _get_project_root()
# ARCHIVED             analysis = _read_json_artifact(root, slug, "navigation_analysis.json")
# ARCHIVED             if analysis:
# ARCHIVED                 # Merge guard: never let a sparse agent run discard the explorer's
# ARCHIVED                 # reliable findings — fill missing fields from navigation_findings.
# ARCHIVED                 analysis = _merge_explore_findings_into_analysis(analysis, root, slug)
# ARCHIVED                 try:
# ARCHIVED                     with open(na_path, "w", encoding="utf-8") as f:
# ARCHIVED                         json.dump(analysis, f, indent=2, ensure_ascii=False)
# ARCHIVED                 except Exception:
# ARCHIVED                     pass
# ARCHIVED                 logger.info(
# ARCHIVED                     "_invoke_navigation_synthesize: navigation_analysis.json from "
# ARCHIVED                     "navigation_agent (handoff) — merged with explore findings (job %s)",
# ARCHIVED                     job_id,
# ARCHIVED                 )
# ARCHIVED                 _notify_phase(job_id, "navigation_synthesize", "done")
# ARCHIVED                 return {"messages": [], "navigation_analysis": analysis}
# ARCHIVED     except Exception as exc:
# ARCHIVED         logger.warning("_invoke_navigation_synthesize: skip-check failed: %s", exc)
# ARCHIVED
# ARCHIVED     _notify_phase(job_id, "navigation_synthesize", "running")
# ARCHIVED     set_tool_context(dict(state), agent_name="navigation_synthesize")
# ARCHIVED     try:
# ARCHIVED         result = _synthesize(dict(state), config)
# ARCHIVED         _notify_phase(job_id, "navigation_synthesize", "done")
# ARCHIVED
# ARCHIVED         # SOURCE FIX: navigation_synthesize (LLM) sometimes drops the product
# ARCHIVED         # URLs discovered by navigation_explore. The product links are in
# ARCHIVED         # navigation_findings.json > listing_page.product_links — merge them into
# ARCHIVED         # navigation_analysis.json > item_links.urls if missing. This ensures
# ARCHIVED         # code_writer has the correct URLs to build the scraper around, instead
# ARCHIVED         # of generating broken discovery logic. [fix data flow at the source]
# ARCHIVED         try:
# ARCHIVED             slug = state.get("site_slug", "")
# ARCHIVED             root = _get_project_root()
# ARCHIVED             nf_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
# ARCHIVED             na_path = os.path.join(root, "workspace", slug, "navigation_analysis.json")
# ARCHIVED             if os.path.isfile(nf_path) and os.path.isfile(na_path):
# ARCHIVED                 import json as _json
# ARCHIVED                 nf = _json.load(open(nf_path))
# ARCHIVED                 na = _json.load(open(na_path))
# ARCHIVED                 # product URLs are nested in listing_page.product_links (list of dicts with 'href')
# ARCHIVED                 lp = nf.get("listing_page") or {}
# ARCHIVED                 _raw_links = lp.get("product_links") or []
# ARCHIVED                 product_urls = []
# ARCHIVED                 for _rl in _raw_links:
# ARCHIVED                     if isinstance(_rl, str):
# ARCHIVED                         product_urls.append(_rl)
# ARCHIVED                     elif isinstance(_rl, dict) and _rl.get("href"):
# ARCHIVED                         product_urls.append(_rl["href"])
# ARCHIVED                 if product_urls:
# ARCHIVED                     il = na.get("item_links")
# ARCHIVED                     if not isinstance(il, dict):
# ARCHIVED                         il = {}
# ARCHIVED                     existing = il.get("urls") or []
# ARCHIVED                     # Filter to strings only (some items may be dicts)
# ARCHIVED                     existing_str = [u for u in existing if isinstance(u, str)]
# ARCHIVED                     product_str = [u for u in product_urls if isinstance(u, str)]
# ARCHIVED                     if len(existing_str) < len(product_str):
# ARCHIVED                         il["urls"] = list(dict.fromkeys(existing_str + product_str))
# ARCHIVED                         na["item_links"] = il
# ARCHIVED                         with open(na_path, "w") as f:
# ARCHIVED                             _json.dump(na, f, indent=2, ensure_ascii=False)
# ARCHIVED                         logger.info(
# ARCHIVED                             "navigation_synthesize: merged %d product URLs from "
# ARCHIVED                             "findings into analysis.item_links.urls (had %d)",
# ARCHIVED                             len(product_urls), len(existing),
# ARCHIVED                         )
# ARCHIVED                         # ALSO update the state return value (result) so downstream
# ARCHIVED                         # nodes (code_writer, etc.) see the URLs without needing to
# ARCHIVED                         # re-read the file. This is the root-cause fix for the
# ARCHIVED                         # state-loses-URLs bug that required the input_urls.json
# ARCHIVED                         # workaround in _invoke_code_writer.
# ARCHIVED                         if isinstance(result, dict):
# ARCHIVED                             result["navigation_analysis"] = na
# ARCHIVED         except Exception as exc_merge:
# ARCHIVED             logger.warning("navigation_synthesize: URL merge failed: %s", exc_merge)
# ARCHIVED
# ARCHIVED         return result
# ARCHIVED     except Exception as exc:
# ARCHIVED         logger.exception(
# ARCHIVED             "_invoke_navigation_synthesize failed (job %s): %s", job_id, exc
# ARCHIVED         )
# ARCHIVED         _notify_phase(job_id, "navigation_synthesize", "failed")
# ARCHIVED         return {}
# ARCHIVED     finally:
# ARCHIVED         clear_tool_context()
# ═══ END ARCHIVED ═══


def _invoke_nav_skill_review(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    """Graph wrapper for the navigation skill review node.

    Non-blocking: any failure is logged and an empty dict returned so the
    graph proceeds to scraper_analyzer without skill updates.
    """
    from .nodes.navigate_skill_review import navigate_skill_review as _review

    job_id = state.get("job_id", 0)
    # Skip on non-SUCCESS (see _invoke_skill_learner guard for rationale).
    if state.get("execution_status", "FAILED") != "SUCCESS":
        logger.info(
            "_invoke_nav_skill_review: skipping (execution_status=%s, job %s)",
            state.get("execution_status"), job_id,
        )
        _notify_phase(job_id, "nav_skill_review", "skipped")
        return {"messages": []}
    _notify_phase(job_id, "nav_skill_review", "running")
    set_tool_context(dict(state), agent_name="nav_skill_review")
    try:
        result = _review(dict(state), config)
        _notify_phase(job_id, "nav_skill_review", "done")
        return result
    except Exception as exc:
        logger.exception(
            "_invoke_nav_skill_review failed (job %s): %s — non-blocking, "
            "continuing pipeline",
            job_id,
            exc,
        )
        _notify_phase(job_id, "nav_skill_review", "failed")
        return {}
    finally:
        clear_tool_context()


def _decide_strategy(state: ScrapeState) -> dict[str, Any]:
    """Deterministic strategy selection (replaces the LLM scraper_analyzer).

    Derives the scraping strategy from ``probe_result.connectivity.method_that_worked``
    (mirroring the old prompt's method -> strategy mapping), copies the proxy tier
    from the probe, and carries a ``critical_fix`` synthesized from the prior test
    crash on retry. ``_enforce_anti_bot_strategy`` remains the sole strategy
    authority (rewrites bad tokens to http_navigation for anti-bot sites).
    """
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "scraper_analyzer", "running")
    slug = state.get("site_slug", "")
    try:
        # ── Strategy cascade: record a failed prior strategy so it isn't re-picked.
        tried = list(state.get("strategies_tried") or [])
        _prior_strategy = (state.get("scraper_analysis") or {}).get("strategy", "")
        _prior_report = state.get("test_report") or {}
        _new_tried: list = []
        # A field-PASS can be downgraded by route_after_testing for insufficient
        # discovery coverage. Record the strategy in that case too, so it isn't
        # re-picked — otherwise the cascade loops on the same failed strategy.
        _cov_bad = False
        if isinstance(_prior_report, dict):
            try:
                from .nodes.route_after_testing import _discovery_coverage_failure
                _cov_bad = bool(_discovery_coverage_failure(_prior_report))
            except Exception as _e:
                logger.debug("_decide_strategy: coverage check skipped: %s", _e)
        if _prior_strategy and isinstance(_prior_report, dict) and (
            _prior_report.get("overall_assessment") not in (None, "PASS") or _cov_bad
        ):
            try:
                from .nodes.route_after_testing import classify_test_failure
                _action, _reason = classify_test_failure(_prior_report, _prior_strategy)
                if _action == "strategy" and not any(
                    (t.get("strategy") if isinstance(t, dict) else t) == _prior_strategy
                    for t in tried
                ):
                    _new_tried = [{"strategy": _prior_strategy, "reason": _reason}]
                    logger.info(
                        "_decide_strategy: strategy '%s' failed (%s) — recording (job %s)",
                        _prior_strategy, _reason, job_id,
                    )
            except Exception as _e:
                logger.warning("_decide_strategy: failure classify failed: %s", _e)

        analysis = _derive_strategy(state)
        # Anti-bot ⇒ http_navigation (cloak). Sole strategy authority.
        if _PATCHES_ENABLED:
            analysis = _enforce_anti_bot_strategy(analysis, slug, "scraper_analysis.json")
        # Escalation: _derive_strategy is a pure function of the probe method, so
        # without this it re-picks the SAME failing strategy every retry (the old
        # LLM analyzer read strategies_tried; the deterministic one must too). If
        # the chosen strategy was already tried+failed, escalate to a more capable
        # one (http_requests -> http_navigation -> playwright; internal_api only
        # via the count gate, never via escalation). When the ladder is exhausted,
        # route to the exhausted path instead of re-picking (job-12 cycle 3).
        _all_tried = {
            (_t.get("strategy") if isinstance(_t, dict) else _t)
            for _t in (tried + _new_tried)
        }
        analysis, _exhausted_goto = _escalate_strategy(
            analysis, _all_tried, skip_approvals=bool(state.get("skip_approvals"))
        )
        # [T2.1/wave-13] Before routing out on an exhausted strategy ladder,
        # spend the SECOND axis: escalate the proxy tier and re-run the
        # derived strategy at it. Bounded (2 extra cycles max) and honest —
        # a different IP class is a new experiment, recorded as such.
        if _exhausted_goto and _two_dim_ladder_enabled():
            _tiered = _escalate_tier_axis(analysis)
            if _tiered is not None:
                analysis = _tiered
                _exhausted_goto = None
                _new_tried = _new_tried + [{
                    "strategy": analysis.get("strategy"),
                    "reason": "proxy-tier escalation after strategy-ladder exhaustion",
                    "tier": analysis.get("proxy_tier"),
                }]
                logger.warning(
                    "_decide_strategy: strategy ladder exhausted — escalating "
                    "proxy tier to %s and re-running %s (job %s)",
                    analysis.get("proxy_tier"), analysis.get("strategy"), job_id,
                )
        if _exhausted_goto:
            logger.error(
                "_decide_strategy: all strategies tried+failed — routing to %s "
                "instead of re-picking the same one (job %s)",
                _exhausted_goto, job_id,
            )
        # Persist so downstream nodes/code_writer read the artifact from disk.
        try:
            root = _get_project_root()
            with open(os.path.join(root, "workspace", slug, "scraper_analysis.json"),
                      "w", encoding="utf-8") as f:
                json.dump(analysis, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("_decide_strategy: could not write scraper_analysis.json: %s", exc)

        update: dict[str, Any] = {"messages": [], "scraper_analysis": analysis}
        if _new_tried:
            update["strategies_tried"] = _new_tried  # append (Annotated[list, operator.add])
        _notify_phase(job_id, "scraper_analyzer", "done")
        if _exhausted_goto:
            # Command routing (bypasses conditional edges — the node decides):
            # mirror route_after_testing's exhausted arms — skip_approvals
            # intake jobs go to cleanup (honest failure, artifacts preserved);
            # jobs with approvals get the human "final retry feedback" gate.
            return Command(goto=_exhausted_goto, update=update)
        # [job-82 D6] Command routing, like the happy path above the exhausted
        # arm — a registered static scraper_analyzer → code_writer edge
        # shadowed the exhausted arm's goto with a ghost code_writer re-entry,
        # silently bypassing the never-retry-a-dead-strategy honesty rule
        # (job-12).
        return Command(goto="code_writer", update=update)
    except Exception:
        _notify_phase(job_id, "scraper_analyzer", "failed")
        raise


_STRATEGY_ESCALATION = ["http_requests", "http_navigation", "playwright", "internal_api"]

# [T2.1/wave-13] Second escalation axis: PROXY TIER. When the strategy ladder
# is exhausted, everything tried so far failed AT ONE NETWORK IDENTITY — a
# datacenter/residential IP is a genuinely different experiment, not the
# doomed re-run the exhaustion rule exists to stop. Tiers escalate sideways
# (none → datacenter → residential) BEFORE routing out; the derived strategy
# is re-run at the higher tier (code_writer reads scraper_analysis.proxy_tier
# and bakes it into the draft). Env kill-switch: SCRAPER_TWO_DIM_LADDER=0.
_PROXY_TIER_LADDER = ("none", "datacenter", "residential")


def _two_dim_ladder_enabled() -> bool:
    try:
        return os.environ.get("SCRAPER_TWO_DIM_LADDER", "1").strip().lower() not in (
            "0", "false", "no",
        )
    except Exception:
        return False


def _tier_configured(tier: str) -> bool:
    """Is a real proxy configured for this tier? An unconfigured tier would
    silently run unproxied — manufacturing the same experiment twice."""
    if tier == "none":
        return True
    try:
        from src.proxy import build_proxy_url

        return bool(build_proxy_url(tier))
    except Exception:
        return False


def _escalate_tier_axis(analysis: dict[str, Any]) -> dict[str, Any] | None:
    """One rung up the proxy-tier axis.

    Returns the UPDATED analysis (strategy unchanged, tier raised), or None
    when the tier ladder is also exhausted / the next tier has no configured
    proxy. Durability across cycles is handled by _derive_strategy, which
    honors a stronger prior tier.
    """
    current = str(analysis.get("proxy_tier") or "none") or "none"
    idx = _PROXY_TIER_LADDER.index(current) if current in _PROXY_TIER_LADDER else 0
    for nxt in _PROXY_TIER_LADDER[idx + 1:]:
        if not _tier_configured(nxt):
            continue
        analysis["proxy_tier"] = nxt
        analysis["no_proxy_flag"] = False
        analysis["strategy_justification"] = (
            f"Deterministic tier escalation: strategy ladder exhausted — "
            f"re-running {analysis.get('strategy')} at proxy tier {nxt} "
            f"(was {current})"
        )
        return analysis
    return None


def _escalate_strategy(
    analysis: dict[str, Any],
    all_tried: set,
    *,
    skip_approvals: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Escalate a tried+failed strategy, honestly.

    Returns ``(analysis, goto)``. ``goto`` is None on the normal path, or the
    node to route to when NO untried strategy remains: "cleanup" under
    skip_approvals (intake jobs — human_approval would auto-approve and loop),
    else "human_approval".

    Two job-12 honesty rules on top of the old ladder:
    - Never escalate INTO ``internal_api``. Evidence-backed internal_api is
      CHOSEN directly by _derive_strategy's count gate; the old code let a
      failed playwright escalate into it with no evidence check
      (_ESCALATION[3:] hole) — manufacturing the doomed strategy the gate
      just declined.
    - Never re-pick the tried+failed strategy when the ladder is exhausted.
      The old code fell through and returned the SAME strategy (job-12
      cycle 3: 10 min / 49 tool calls / zero writes). Downward rungs are
      also not manufactured: re-running internal_api as http_requests
      against a CSR page is the exact "doomed strategy" failure.
    """
    chosen = analysis.get("strategy")
    if chosen not in all_tried:
        return analysis, None
    idx = _STRATEGY_ESCALATION.index(chosen) if chosen in _STRATEGY_ESCALATION else -1
    for nxt in _STRATEGY_ESCALATION[idx + 1:]:
        if nxt == "internal_api":
            continue
        if nxt not in all_tried:
            for k in ("strategy", "scraping_mechanism", "scraping_method", "recommended_strategy"):
                analysis[k] = nxt
            analysis["strategy_justification"] = (
                f"Deterministic escalation: {chosen} tried+failed -> {nxt}"
            )
            return analysis, None
    analysis["strategy_justification"] = (
        f"Deterministic: strategy ladder exhausted — all strategies tried+failed "
        f"(tried: {sorted(all_tried)}), no untried strategy remains; "
        f"routing out instead of re-picking {chosen}"
    )
    return analysis, ("cleanup" if skip_approvals else "human_approval")


# [job-65 phase 3a] How many times a zero-item execution may recycle through
# the strategy ladder. 1: the first zero-item execution gets a strategy switch;
# a second one finalizes honestly instead of looping.
_EXECUTION_RECYCLE_MAX = 1


def _route_after_execution(state: ScrapeState):
    """Execution-phase strategy recycle on zero-item discovery failure.

    The testing ladder verifies a draft works — then the EXECUTION run can
    still see zero items. Job-65 citybeach: the tester discovered 1,317 URLs,
    the execution fetched the same listing minutes later and got
    200-but-zero-links; nothing in the graph tried a different strategy and
    the job finalized 0 items. When the execution output shows a FAIL-class
    discovery coverage stop_reason, the draft's OWN in-run recovery (45s
    retry, proxy-ladder escalation, JSON-LD ItemList fallback) has already run
    and failed — the strategy cannot see this site's items. This node recycles
    ONCE through scraper_analyzer with the failed strategy recorded, so
    ``_escalate_strategy`` picks the next rung and a fresh
    code_writer → code_tester pass runs it.

    Everything else goes to cleanup as before: real items (the normal path),
    plain crashes (code bugs the strategy ladder cannot fix — the finalize
    gate reports them), url_list jobs (no discovery phase to blame), a
    coverage stop_reason outside the FAIL class (exhaustion-flavored
    signatures are Phase 2's test-time gate, not an execution verdict), and
    the second zero-item execution (recycle budget spent — finalize honestly
    with execution_status FAILED so cleanup does not promote a 0-item scraper).
    [T2.10] One widening: a FAILED execution whose failure IS the
    zero-discovery verdict (template DISCOVERY_ZERO rc=3, tagged by
    run_execution) recycles like the SUCCESS zero — the ladder, not cleanup,
    owns an access failure.
    """
    job_id = state.get("job_id", 0)
    status = state.get("execution_status", "")
    try:
        items = int(state.get("product_count") or 0)
    except (TypeError, ValueError):
        items = 0
    cov = state.get("discovery_coverage") if isinstance(
        state.get("discovery_coverage"), dict
    ) else {}
    stop_reason = str(cov.get("stop_reason") or "")

    if status == "SUCCESS" and items > 0:
        return Command(goto="cleanup")
    # [T2.10/wave-13] A FAILED execution is not automatically a code problem:
    # the http_navigation template exits 3 with DISCOVERY_ZERO when Phase 1
    # ran cleanly and saw no item URLs — run_execution tags EXACTLY that shape
    # (rc=3 / stderr marker) with ``no_fresh_output``. That is the SAME
    # strategy verdict as the SUCCESS zero — a clean-zero execution that
    # rc!=0'd used to fly straight past this recycle into cleanup (job-85's
    # trap). Admit ONLY runs carrying the run_execution tag: a raw FAIL-class
    # stop_reason is NOT enough, or a rc=1 traceback whose run happened to
    # record empty_first_page coverage would be misread as an access problem
    # and recycled (tracebacks are code problems; the ladder cannot fix them).
    from .nodes.route_after_testing import _COVERAGE_FAIL_STOP_REASONS

    _zero_discovery_failed = (
        status != "SUCCESS"
        and items == 0
        and bool(state.get("no_fresh_output"))
    )
    if (status != "SUCCESS" or items != 0) and not _zero_discovery_failed:
        # Crashed / stalled / no-output executions are code problems, not
        # strategy problems — the ladder cannot fix a traceback.
        return Command(goto="cleanup")
    if (state.get("input_mode") or "") not in ("list_page", "navigation", "search_term"):
        return Command(goto="cleanup")  # url_list: no discovery phase to recycle

    if stop_reason not in _COVERAGE_FAIL_STOP_REASONS:
        logger.warning(
            "_route_after_execution: 0 items but stop_reason=%r is not a "
            "FAIL-class coverage reason — cleanup (job %s)", stop_reason, job_id,
        )
        return Command(goto="cleanup")
    if int(state.get("execution_recycle_count") or 0) >= _EXECUTION_RECYCLE_MAX:
        update: dict[str, Any] = {
            "execution_status": "FAILED",
            "error_message": (
                f"Execution produced 0 items again after a strategy recycle "
                f"(stop_reason={stop_reason}); the strategy ladder cannot see "
                f"this site's items."
            )[:2000],
        }
        logger.error(
            "_route_after_execution: zero-item execution AFTER recycle → "
            "honest cleanup (job %s, stop_reason=%s)", job_id, stop_reason,
        )
        return Command(goto="cleanup", update=update)

    prior_strategy = (state.get("scraper_analysis") or {}).get("strategy", "")
    if not prior_strategy:
        logger.error(
            "_route_after_execution: zero-item execution but no prior strategy "
            "on state — cannot escalate, cleanup (job %s)", job_id,
        )
        return Command(goto="cleanup")

    _reason = f"execution zero-items: stop_reason={stop_reason}"
    report = state.get("test_report")
    recycled_report = dict(report) if isinstance(report, dict) else {}
    recycled_report.update({
        "overall_assessment": "FAIL",
        "ready_for_execution": False,
        # The writer retry must know the draft EXECUTED and still saw nothing
        # — otherwise a passing tester report reads as "nothing to fix".
        "feedback_for_writer": (
            "EXECUTION RECYCLE — the draft passed testing but its Phase-1 "
            f"discovery found 0 URLs AT EXECUTION (stop_reason={stop_reason}). "
            "The strategy is being escalated; rebuild the discovery path for "
            "the new strategy. Keep the shared discovery module wiring intact "
            "and re-derive the listing URLs/selectors for it."
        ),
    })
    update = {
        "execution_recycle_count": int(
            state.get("execution_recycle_count") or 0
        ) + 1,
        # Annotated add-channel: appends, and _decide_strategy's dupe guard
        # sees it so _escalate_strategy moves up the ladder.
        "strategies_tried": [{"strategy": prior_strategy, "reason": _reason}],
        "test_report": recycled_report,
    }
    _notify_phase(job_id, "scraper_analyzer", "running")
    logger.warning(
        "_route_after_execution: zero-item execution (stop_reason=%s) — "
        "recycling strategy '%s' through the ladder (job %s, recycle %d/%d)",
        stop_reason, prior_strategy, job_id,
        update["execution_recycle_count"], _EXECUTION_RECYCLE_MAX,
    )
    return Command(goto="scraper_analyzer", update=update)


def _derive_strategy(state: ScrapeState) -> dict[str, Any]:
    """Map probe_result.connectivity.method_that_worked to a scraping strategy.

    Mirrors the mapping the old LLM scraper_analyzer was prompted with:
      - direct_http  -> http_requests (proxy none), unless discovery is form-POST-only
        (the requests template can't POST forms) -> http_navigation
      - browser_none -> http_navigation (proxy none)
      - uc_chrome_* / cloak_* -> http_navigation, proxy from the method suffix
    """
    probe = state.get("probe_result") or {}
    if not isinstance(probe, dict):
        probe = {}
    conn = probe.get("connectivity") or {}
    method = (conn.get("method_that_worked") if isinstance(conn, dict) else "") or ""
    # Fallback to site_analysis connectivity (probe may be sparse on resume).
    if not method:
        _sa = state.get("site_analysis") or {}
        _sa_conn = (_sa.get("connectivity") if isinstance(_sa, dict) else {}) or {}
        method = (
            (_sa_conn.get("method_that_worked") if isinstance(_sa_conn, dict) else "")
            or ""
        )
    method = method or ""

    # Anti-bot signal: explicit flag OR only-working-method is a stealth browser.
    _ab = probe.get("anti_bot") or {}
    anti_bot = isinstance(_ab, dict) and bool(_ab.get("detected"))
    if not anti_bot and method.startswith(STEALTH_METHOD_PREFIXES):
        anti_bot = True

    # Proxy tier from the method suffix (mirrors the prompt mapping).
    if "residential" in method:
        proxy_tier = "residential"
    elif "datacenter" in method:
        proxy_tier = "datacenter"
    else:
        proxy_tier = "none"
    # [T2.1/wave-13] Tier durability: a previously-escalated tier must survive
    # re-derivation — the probe method doesn't change between cycles, so a
    # pure re-derive would silently drop back to the probe's tier and repeat
    # the exact experiment that exhausted the ladder. Honor the STRONGER of
    # the two (probe evidence vs. escalation); a downgrade never happens.
    try:
        _prior_tier = str(
            (state.get("scraper_analysis") or {}).get("proxy_tier") or ""
        )
    except Exception:
        _prior_tier = ""
    if (
        _prior_tier in _PROXY_TIER_LADDER
        and proxy_tier in _PROXY_TIER_LADDER
        and _PROXY_TIER_LADDER.index(_prior_tier)
        > _PROXY_TIER_LADDER.index(proxy_tier)
    ):
        proxy_tier = _prior_tier

    meth = method.lower()
    # Listing-page JS-rendering signal (navigate_explore._verify_rendering, propagated
    # via navigate_synthesize). "csr" = item links only appear after JS rendering, so
    # http_requests can't reach them → pick a browser strategy upfront. Fixes the
    # ayahealthcare class: homepage reachable via direct_http but listings JS-rendered.
    _nav = state.get("navigation_analysis") or {}
    _rendering = (_nav.get("rendering_verified") if isinstance(_nav, dict) else None) or "unknown"
    # Embedded-JSON data-model signal (navigate_explore detector → navigate_synthesize).
    # Surfaced on scraper_analysis so code_writer sees it in one place and it survives
    # retries. "embedded_json" = items live in a <script> JSON blob in the listing page,
    # NOT detail pages — a third data model. The strategy itself still comes from the
    # rendering cascade above (ssr→http_requests / csr→http_navigation); this only tags
    # the model so code_writer extracts records from the listing JSON (no per-detail Phase 2).
    _data_source = (_nav.get("data_source") if isinstance(_nav, dict) else None) or "none"
    _embedded_json = (_nav.get("embedded_json") if isinstance(_nav, dict) else None) or None
    # JS-rendered listing → browser-backed strategy, REGARDLESS of how the probe
    # reached the page (method_that_worked). The listing's render need is
    # independent of probe access: a site whose homepage is reachable via
    # cloak_none can still have a Coveo/React listing that http_navigation's
    # /navigate (2s render wait) can't surface. The previous gate nested this
    # inside `if meth == "direct_http"`, so a cloak_none probe bypassed it and
    # picked http_navigation for a Coveo site → 0 discovered.
    if _rendering == "browser":
        # browser_traverse ran (rendering=browser), but that ALONE doesn't mean the
        # listing is JS-rendered — a form-POST→SSR site (locumtenens) is HTTP-
        # reachable via POST replay and must stay on http_requests. Only a GET-
        # navigated listing that needed the browser to render (Coveo/React) needs
        # playwright. Distinguish by the discovery form method the traverser recorded:
        # POST → SSR results → http_requests; GET → JS-rendered listing → playwright.
        _form_method = ""
        _search = _nav.get("search") if isinstance(_nav, dict) else None
        if isinstance(_search, dict):
            _form_method = (_search.get("form_method") or "").upper()
        if _form_method == "POST":
            strategy = "http_requests"
        elif method.startswith(STEALTH_METHOD_PREFIXES):
            # [job-83 woolworths] The probe's escalation ladder tries every
            # playwright tier BEFORE it reaches a working uc_chrome/cloak
            # method — so a stealth method_that_worked means every playwright
            # tier that ran was BLOCKED (woolworths: playwright_none/
            # datacenter/residential all failed; uc_chrome_none + uc_chrome_
            # residential succeeded). Bare playwright cannot render here, and
            # _enforce_anti_bot_strategy deliberately leaves it unrewritten.
            # Derive the probe-proven browser flavor instead: http_navigation's
            # /navigate applies the cloak fingerprint server-side.
            strategy = "http_navigation"
        else:
            strategy = "playwright"
    elif _rendering == "csr":
        # navigate_explore._verify_rendering: lighter CSR where the server-side
        # /navigate render surfaces the links.
        strategy = "http_navigation"
    elif meth == "direct_http" and not _is_form_only_discovery(state, state.get("url", "")):
        strategy = "http_requests"
    else:
        # browser_none, uc_chrome_*, cloak_*, or form-only direct_http → browser-backed.
        strategy = "http_navigation"

    # API-strategy override: when browser_traverse captured a backend JSON data API
    # (data_source == "api" + an api_endpoint), the items come from that API — use
    # internal_api (HTTP + JSON paginated loop), NOT http_requests/http_navigation.
    # This aligns scraper_analysis.strategy with the api_section + api_scraper.py
    # template hint build_code_writer_message already emits; without it, code_writer
    # follows the http_requests strategy field and builds a listing-paginating scraper
    # that hangs on CSR pages with no paginated listing (aya).
    _nav_api = (_nav.get("api_endpoint") if isinstance(_nav, dict) else None) or {}
    _nav_api = _nav_api if isinstance(_nav_api, dict) else {}
    # Gate on the API being a REAL data source for this query — POSITIVE COUNT
    # EVIDENCE required:
    #   - items_per_page > 0, AND
    #   - count is a positive int (> 0).
    # items_per_page>0 is nearly vacuous as a signal: verify_api returns no
    # descriptor at all unless it found a non-empty dict-array, then sets
    # items_per_page = len(items) >= 1 — so every descriptor ever emitted
    # satisfies it. The ketchcdn consent-config poison yielded
    # items_per_page=5 from a 5-element array inside the CMP blob. count>0 is
    # the gate's one real discriminator:
    #   - count > 0   (aya: 26955)                              -> pass
    #   - count null  (ketchcdn consent config, useinsider personalization,
    #                  sidley taxonomy, zquiet heatmap)          -> rejected
    #   - count == 0  (Coveo explicit zero, lw.com)              -> rejected
    # The explicit-zero check predates this gate (lw.com regression:
    # totalCount=0 yet ~15 sample items -> internal_api -> 1 item vs
    # playwright's 20); the null rejection is the job-12 fix.
    # Honest scope: this is a NO-COUNT filter, not a full poison filter — an
    # endpoint reporting a real-looking total while returning non-item records
    # (review widgets, totalResults:N) still passes; catching that class needs
    # content-evidence verification (sample_keys/content_type, now retained by
    # _project_api_endpoint), not a weaker count rule. Legit no-total APIs
    # (Shopify /products.json feeds, cursor-paginated APIs) are downgraded to
    # the next strategy BY DESIGN; code_writer's catalog guidance can still
    # reach the feed directly.
    _api_items = _nav_api.get("items_per_page")
    _api_count = _nav_api.get("count")
    if (
        _data_source == "api"
        and (_nav_api.get("url") or _nav_api.get("api_url"))
        and isinstance(_api_items, int)
        and not isinstance(_api_items, bool)
        and _api_items > 0
        and isinstance(_api_count, int)
        and not isinstance(_api_count, bool)
        and _api_count > 0
    ):
        strategy = "internal_api"

    # Evidence precedence (job-12 fix S3): when the descriptor carries NO
    # positive count evidence (the gate above declined internal_api), an
    # explicit mechanism verdict produced by the content/product analysis
    # outranks this function's heuristic cascade. Priceline shipped
    # mechanism_reassessment.recommended="playwright" with OCC-interception
    # instructions while the consent-config descriptor dragged the strategy
    # elsewhere — the measured-page verdict was already on disk and must win.
    # Browser/HTTP strategy names ONLY: a recommendation can never re-arm
    # internal_api past the count gate (measured evidence outranks opinion,
    # and poison descriptors report opinions too).
    _rec_recommended = ""
    _rec_key = ""
    for _pa_key in ("content_analysis", "product_analysis"):
        _pa = state.get(_pa_key)
        if isinstance(_pa, dict):
            _mr = _pa.get("mechanism_reassessment")
            if isinstance(_mr, dict):
                # T1.5: the key-alias version read ONLY `.recommended` — the
                # next synonym the analyzer model coins is silently dropped
                # again (the exact drift class S3 was written to close). Two
                # halves, in order:
                #   1. verdict keys (known verdict-bearing names, fixed order);
                #   2. a bounded VALUE scan over the remaining keys.
                # The bare value-match a critic proposed is WORSE than either:
                # origin-marking keys (original_recommendation,
                # site_analyzer_said) carry the OLD verdict as their VALUE, so
                # an unfiltered scan flips strategy to the very thing the
                # reassessment argued against. Excluded here. Exact-token +
                # enum-only stays (a contaminated artifact must not become
                # self-confirming); ambiguous (≥2 distinct candidates) is
                # ignored, not guessed.
                _MR_VERDICT_KEYS = (
                    "recommended", "reassessed_mechanism", "recommended_mechanism",
                )
                _MR_ORIGIN_TOKENS = ("original", "said", "previous", "prior", "old", "was")
                _MR_ENUM = ("http_requests", "http_navigation", "playwright")
                for _k in _MR_VERDICT_KEYS:
                    _v = str(_mr.get(_k) or "").strip().lower()
                    if _v in _MR_ENUM:
                        _rec_recommended = _v
                        _rec_key = _k
                        break
                if not _rec_recommended:
                    _candidates: list[str] = []
                    for _mk, _mv in _mr.items():
                        if any(_tok in str(_mk).lower() for _tok in _MR_ORIGIN_TOKENS):
                            continue
                        _mv_s = str(_mv or "").strip().lower()
                        if _mv_s in _MR_ENUM and _mv_s not in _candidates:
                            _candidates.append(_mv_s)
                    if len(_candidates) == 1:
                        _rec_recommended = _candidates[0]
                        _rec_key = "value-scan"
                    elif len(_candidates) > 1:
                        logger.warning(
                            "_derive_strategy: ambiguous mechanism_reassessment "
                            "values %s — ignored (job-level state key %s)",
                            _candidates, _pa_key,
                        )
                if _rec_recommended:
                    break
    # [job-114 citychic / job-115 sephora.cz] Probe-proven stealth outranks the
    # reassessment OPINION. The probe ladder tries every playwright tier AND
    # every direct-http tier BEFORE it reaches a working uc_chrome/cloak method
    # — so a stealth method_that_worked MEASURES that bare playwright and plain
    # requests are blocked. The product-analyzer's prose verdict ("recommend
    # playwright") is an opinion about that same measurement and cannot
    # un-disprove it (both jobs shipped doomed playwright drafts this way —
    # 114's override literally read: mechanism_reassessment[recommended]='
    # playwright' outranks count=None descriptor). On a stealth-proven site the
    # override is therefore SUPPRESSED — never applied, not even capped: it
    # must not trample the cascade's POST-form http_requests pick either
    # (POST replay is not probe-disproven; locumtenens contract). Non-stealth
    # sites keep the job-12 priceline semantics untouched — there the
    # reassessment carries a genuinely measured page verdict this heuristic
    # cascade never ran.
    _override_suppressed = bool(method.startswith(STEALTH_METHOD_PREFIXES))
    _strategy_source = ""
    if (
        _rec_recommended in ("http_requests", "http_navigation", "playwright")
        and not (
            isinstance(_api_count, int)
            and not isinstance(_api_count, bool)
            and _api_count > 0
        )
        and strategy != _rec_recommended
    ):
        if _override_suppressed:
            # [T1.10/wave-13] This string must NOT contain the
            # "mechanism_reassessment" token: subagents._suppress_mechanism_
            # reassessment reads that token as "the verdict was APPLIED" and
            # renders the contradicting block into the writer seed — the
            # suppression branch was defeating its own suppression (jobs
            # 114/115 shipped doomed playwright drafts even after N13 because
            # the writer still read "recommend playwright" here). "ignored" +
            # "stealth-proven" stay (pinned by test_job83_job88_classes).
            _strategy_source = (
                f"; product-analyzer verdict[{_rec_key}]={_rec_recommended!r} "
                f"ignored — stealth-proven probe disproved playwright and "
                f"http_requests at every probe tier"
            )
        else:
            strategy = _rec_recommended
            _strategy_source = (
                f"; mechanism_reassessment[{_rec_key}]={_rec_recommended!r} "
                f"outranks count={_api_count!r} descriptor"
            )

    # Discovery config: propagate the navigator's pagination detection so the
    # template uses the RIGHT config_for_* preset (load_more vs page_param vs
    # next_button) — deterministic, not code_writer's guess.
    _disc_pag = (_nav.get("discovery") or {}).get("pagination") if isinstance(_nav, dict) else None
    if not _disc_pag:
        _disc_pag = _nav.get("pagination") if isinstance(_nav, dict) else None
    _discovery_config = None
    if isinstance(_disc_pag, dict) and _disc_pag.get("type"):
        # The navigator (navigate_explore.py) emits `page_param`/`page_size` for
        # offset_param (?start=0&sz=24); other paths emit canonical
        # `page_param_name`/`items_per_page`. Accept BOTH so offset_param values
        # reach config_for_page_param via discovery_config.json instead of being
        # silently dropped (graph.py field-name pipeline bug).
        _discovery_config = {
            "type": _disc_pag.get("type"),
            "page_param_name": _disc_pag.get("page_param_name") or _disc_pag.get("page_param"),
            "items_per_page": _disc_pag.get("items_per_page") or _disc_pag.get("page_size"),
            "next_button_selector": _disc_pag.get("next_button_selector"),
            "max_pages": _disc_pag.get("max_pages"),
        }

    # [T3.4-restricted/wave-13] The probe method's tier suffix describes the
    # BROWSER experiment that worked. A pure-HTTP strategy on a NON-stealth
    # method must not inherit it — the writer would otherwise bake a paid
    # proxy tier into a requests/API draft whose access path (POST replay,
    # backend API) the probe never disproved. Stealth-proven methods KEEP
    # their tier: the probe measured that every direct tier was blocked, and
    # an http draft faces the same wall.
    _browser_shaped = strategy in (
        "playwright", "http_navigation", "stealth_browser",
        "seleniumbase_uc", "undetected_chromedriver",
    )
    if not _browser_shaped and not method.startswith(STEALTH_METHOD_PREFIXES):
        proxy_tier = "none"

    analysis: dict[str, Any] = {
        "strategy": strategy,
        "scraping_mechanism": strategy,
        "scraping_method": strategy,
        "recommended_strategy": strategy,
        "proxy_tier": proxy_tier,
        "connectivity": {"method_that_worked": method},
        "anti_bot": {"detected": anti_bot},
        "confidence_score": 0.9,
        "data_source": _data_source,
        "embedded_json": _embedded_json,
        "api_endpoint": _nav_api,
        "discovery_config": _discovery_config,
        "strategy_justification": (
            f"Deterministic: method_that_worked={method or 'unknown'} -> {strategy} "
            f"(proxy={proxy_tier}, anti_bot={anti_bot}, rendering={_rendering}, "
            f"data_source={_data_source}){_strategy_source}"
        ),
    }

    # On retry, carry a critical_fix synthesized from the prior crash so code_writer
    # makes a targeted fix (the read-only analyzer that authored critical_fix is gone).
    _tr = state.get("test_report") or {}
    _crash = (_tr.get("crash_error") or "") if isinstance(_tr, dict) else ""
    # code_tester nests crash info at script_checks.crash_error (not top-level).
    if not _crash and isinstance(_tr, dict):
        _sc = _tr.get("script_checks")
        if isinstance(_sc, dict):
            _crash = _sc.get("crash_error") or _sc.get("error_message") or ""
    if _crash:
        analysis["critical_fix"] = {
            "issue": f"Previous scraper crashed: {str(_crash)[:300]}",
            "root_cause": "See crash above — the scraper hit this error during testing.",
            "fix": "Make a MINIMAL, targeted fix for THIS error; do NOT rewrite from scratch.",
        }
    return analysis


def _is_form_only_discovery(state: dict, url: str) -> bool:
    """True when discovery requires POSTing a form the requests template can't do.

    Generic — keys on structural signals in navigation_analysis (no category_links
    + POST/CSRF search or form-method filters), excluding sites with a same-domain
    JSON API (those use internal_api). Mirrors the old prompt override.
    """
    nav = state.get("navigation_analysis") or {}
    if not isinstance(nav, dict):
        return False
    categories = nav.get("categories") or {}
    category_links = (
        categories.get("category_links") if isinstance(categories, dict) else None
    ) or []
    search = nav.get("search") or {}
    filters = nav.get("filters") or {}
    form_only = (
        (not category_links)
        and (
            (isinstance(search, dict) and search.get("classic_search_method") == "post")
            or (isinstance(search, dict) and bool(search.get("classic_search_requires_csrf")))
            or (isinstance(filters, dict) and filters.get("method") == "form")
        )
    )
    if not form_only:
        return False
    from urllib.parse import urlparse as _urlparse

    api = nav.get("api_endpoint") or {}
    api_url = (api.get("url") or "") if isinstance(api, dict) else ""
    if api_url:
        api_host = _urlparse(api_url).hostname or ""
        site_host = _urlparse(url).hostname or ""
        if api_host and site_host and api_host == site_host:
            return False
    return True



def _fix_scraper_syntax(
    agent, state: ScrapeState, config: RunnableConfig, job_id: int, slug: str,
    max_tries: int = 3,
) -> None:
    """Re-invoke code_writer to fix syntax errors in scraper_draft.py.

    code_writer has no shell tool to self-validate parseability, so the node
    does it: ast.parse the scraper, and on SyntaxError feed the exact error
    (line + message) back to code_writer for an immediate fix. This keeps
    syntax errors out of code_tester's path — code_tester should test
    FUNCTIONALITY, not parseability. Best-effort: if still unparseable after
    max_tries, return and let code_tester catch it (the prior behavior).
    """
    import ast
    from langchain_core.messages import HumanMessage

    scraper_path = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
    for attempt in range(max_tries):
        if not os.path.isfile(scraper_path):
            return
        try:
            with open(scraper_path, "r", errors="ignore") as fh:
                ast.parse(fh.read())
            if attempt > 0:
                logger.info(
                    "_invoke_code_writer: syntax fixed after %d attempt(s) (job %s)",
                    attempt, job_id,
                )
            return  # parses clean
        except SyntaxError as exc:
            logger.warning(
                "_invoke_code_writer: syntax error (attempt %d/%d) in scraper_draft.py line %s: %s",
                attempt + 1, max_tries, exc.lineno, exc.msg,
            )
            line_ctx = f"  { (exc.text or '').strip() }" if exc.text else ""
            fix_msg = [HumanMessage(content=(
                f"Your `workspace/{slug}/scraper_draft.py` has a Python syntax error and will not run:\n"
                f"  **Line {exc.lineno}: {exc.msg}**\n{line_ctx}\n\n"
                f"Read `workspace/{slug}/scraper_draft.py`, locate the error near line {exc.lineno}, "
                f"and use `edit_file` to fix ONLY the parse error — do NOT rewrite the whole scraper. "
                f"Common causes: unclosed bracket/parenthesis/quote, bad indentation, a broken "
                f"f-string, or a stray character. Fix it now."
            ))]
            hb = _start_heartbeat(job_id, "code-writer")
            try:
                result = _invoke_agent_with_timeout(
                    agent, fix_msg, _agent_config(config, "code_writer"),
                    "code_writer", job_id,
                )
                _persist_agent_logs(state, result, "code-writer", config)
            finally:
                _stop_heartbeat(hb)
    logger.error(
        "_invoke_code_writer: syntax errors persist after %d attempts (job %s) — letting code_tester catch it",
        max_tries, job_id,
    )


def _contract_fix_message(
    slug: str, input_mode: str, violation: str, template_path: str
) -> str:
    """L1 fix instruction. Renders the env-gate snippet FROM THE SELECTED
    TEMPLATE (two canonical gate shapes are live — playwright's
    `global PRODUCT_LISTING_URL` vs the http_navigation family's
    `args.listing_url = _env_listing; args.fresh_discovery = True` — and a
    wrong-shape instruction produces a Frankenstein gate; critique v1 vector 2).
    """
    gate_hint = (
        "re-add the env-var gate in the shape YOUR template's main() uses "
        "(the full template is in your system prompt) — do NOT invent a "
        "different shape"
    )
    if template_path and os.path.isfile(template_path):
        try:
            with open(template_path, "r", errors="ignore") as _tf:
                _tsrc = _tf.read()
            if "global PRODUCT_LISTING_URL" in _tsrc:
                gate_hint = (
                    "re-add the gate exactly as your template (playwright "
                    "family) does: `_env_listing = os.environ.get("
                    '"SCRAPER_LISTING_URL", "").strip()` then '
                    "`if _env_listing or args.fresh_discovery or "
                    "args.listing_url:` with the `global PRODUCT_LISTING_URL` "
                    "assignment inside"
                )
            elif "_env_listing" in _tsrc:
                gate_hint = (
                    "re-add the gate exactly as your template (http_navigation "
                    "family) does: `_env_listing = os.environ.get("
                    '"SCRAPER_LISTING_URL", "").strip()` then '
                    "`if _env_listing: args.listing_url = _env_listing; "
                    "args.fresh_discovery = True`"
                )
        except OSError:
            pass
    return (
        f"Your `workspace/{slug}/scraper_draft.py` VIOLATES the execution CLI "
        f"contract for this {input_mode} job.\n\n"
        f"{violation}\n\n"
        "How to fix — a SMALL targeted edit with `edit_file` (do NOT rewrite "
        "the scraper):\n"
        "1. In main()'s argparse, add the missing flag declarations VERBATIM "
        "from the template in your system prompt (e.g. "
        '`parser.add_argument("--listing-url", type=str, default=None, ...)` '
        'and `parser.add_argument("--fresh-discovery", action="store_true", '
        "...)`).\n"
        "2. Wire discovery: " + gate_hint + ".\n"
        "3. The flags must be CONSUMED (feed the discovery branch), not just "
        "declared.\n"
        "Change nothing else. Keep the file syntactically valid."
    )


def _enforce_cli_contract(
    agent, state: ScrapeState, config: RunnableConfig, job_id: int, slug: str,
    max_tries: int = 2,
) -> None:
    """L1: bounce CLI-contract violations back into this agent loop.

    Same bounded single-HumanMessage pattern as _fix_scraper_syntax (fresh
    invoke — no context accumulation; NOT the reverted rescrape-routing class).
    On persistent violation, fall through — L2 (code_tester force-FAIL) is the
    load-bearing gate and will catch it deterministically.
    """
    from langchain_core.messages import HumanMessage

    try:
        from .nodes.run_execution import cli_contract_violation
    except Exception as exc:
        logger.warning("_enforce_cli_contract: import failed: %s", exc)
        return

    input_mode = (state.get("input_mode") or "").strip()
    if input_mode not in ("navigation", "list_page", "search_term"):
        return  # url_list: no discovery contract
    strategy = ""
    _sa = state.get("scraper_analysis")
    if isinstance(_sa, dict):
        strategy = (_sa.get("strategy") or "").strip()

    scraper_path = os.path.join(
        _get_project_root(), "workspace", slug, "scraper_draft.py"
    )
    for attempt in range(max_tries):
        violation = cli_contract_violation(scraper_path, input_mode, strategy)
        if violation is None:
            if attempt > 0:
                logger.info(
                    "_invoke_code_writer: CLI contract fixed after %d attempt(s) (job %s)",
                    attempt, job_id,
                )
            return
        logger.warning(
            "_invoke_code_writer: CLI contract violation (attempt %d/%d, job %s): %s",
            attempt + 1, max_tries, job_id, violation[:300],
        )
        template_path = os.path.join(
            _get_project_root(), _select_template_file(state)
        )
        fix_msg = [HumanMessage(content=_contract_fix_message(
            slug, input_mode, violation, template_path
        ))]
        hb = _start_heartbeat(job_id, "code-writer")
        try:
            result = _invoke_agent_with_timeout(
                agent, fix_msg, _agent_config(config, "code_writer"),
                "code_writer", job_id,
            )
            _persist_agent_logs(state, result, "code-writer", config)
        finally:
            _stop_heartbeat(hb)
        # re-check at loop top; a syntax break from the edit is handled by the
        # NEXT cycle's _fix_scraper_syntax (checker returns None on unparseable).
    logger.error(
        "_invoke_code_writer: CLI contract still violated after %d attempts "
        "(job %s) — code_tester L2 gate will force-FAIL",
        max_tries, job_id,
    )




def _select_template_file(state: ScrapeState) -> str:
    """Thin alias → :mod:`agents.template_selector` (T3.2 single authority).

    The mapping MOVED OUT of graph.py so ``build_code_writer_message``'s
    template-hint can call the SAME function instead of its own
    mechanism-first re-derivation (the two authorities used to disagree for
    api/ssr_div_list/requests strategies).
    """
    from .template_selector import select_template_file

    return select_template_file(state)


def _noop_should_escalate(noop_cycles: int, test_retry_count: int) -> bool:
    """[A2/job-73 RC2] Should the no-op-fix gate escalate right now?

    A byte-identical draft escalates on the SECOND consecutive no-op. But
    with the main retry budget exhausted (``test_retry_count >=
    MAX_TEST_RETRIES``) there is no later round to absorb the waste — the
    first no-op escalates too, so the final round is never spent re-testing
    identical code (job 73: a read-only writer round burned the last test;
    the verdict could not have changed, the round was simply lost).
    """
    if noop_cycles <= 0:
        return False
    if noop_cycles >= 2:
        return True
    return test_retry_count >= MAX_TEST_RETRIES


def _invoke_code_writer(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "code_writer", "running")
    set_tool_context(dict(state), agent_name="code_writer")
    try:
        logger.info("_invoke_code_writer: starting (job %s)", job_id)
        update = {}
        # [job-83 woolworths] Belt for the parse_command null: a truthy
        # state test_report whose file is GONE is a stale report from a
        # previous run riding the graph checkpoint (resume paths can outrank
        # the update). Counting it burned a whole test cycle before the first
        # test ran.
        if state.get("test_report") and not os.path.isfile(
            os.path.join(
                _get_project_root(), "workspace",
                state.get("site_slug") or "", "test_report.json",
            )
        ):
            logger.warning(
                "_invoke_code_writer: test_report in state but no file on disk — "
                "stale report from a previous run, ignoring it (job %s)", job_id,
            )
            state = {**state, "test_report": None}
        # Count a test-retry whenever re-entering from route_after_testing with a
        # prior test_report (a real test failure). The test_retry_count budget
        # caps the regenerate-test loop (MAX_TEST_RETRIES).
        if state.get("test_report"):
            current_count = state.get("test_retry_count", 0)
            if current_count != FINAL_RETRY_SENTINEL:
                update["test_retry_count"] = current_count + 1
                logger.info(
                    "_invoke_code_writer: retry cycle %d (job %s)",
                    update["test_retry_count"],
                    job_id,
                )
                assert update["test_retry_count"] <= FINAL_RETRY_SENTINEL - 1, (
                    f"test_retry_count {update['test_retry_count']} exceeds "
                    f"MAX_TEST_RETRIES ({FINAL_RETRY_SENTINEL - 1})"
                )
            else:
                logger.info(
                    "_invoke_code_writer: FINAL retry cycle (job %s)",
                    job_id,
                )
        slug = state.get("site_slug", "")
        # Write sample URLs from nav_analysis to input_urls.json so the
        # scraper can use them in --sample mode (skip slow discovery).
        # Reads from STATE (navigation_synthesize merges URLs into state
        # via the result update — see _invoke_navigation_synthesize).
        try:
            import json as _json
            na = state.get("navigation_analysis") or {}
            na = na if isinstance(na, dict) else {}
            sample_urls: list = []
            if na.get("data_source") == "embedded_json":
                # Embedded-JSON model: the LISTING/category pages carry the data
                # (not detail pages). Seed listing + category URLs so --input/--sample
                # tests fetch listing pages and extract the embedded JSON — the correct
                # test for this model. [plan: embedded-json model]
                search = na.get("search") or {}
                search = search if isinstance(search, dict) else {}
                for k in ("working_url", "listing_url_used", "url_pattern", "search_url_pattern"):
                    v = search.get(k)
                    if isinstance(v, str) and v and not v.startswith(("javascript", "#")):
                        sample_urls.append(v)
                cats = na.get("categories") or {}
                for c in (cats.get("category_links") or []) if isinstance(cats, dict) else []:
                    if isinstance(c, str) and c:
                        sample_urls.append(c)
                sample_urls = list(dict.fromkeys(sample_urls))
                logger.info(
                    "_invoke_code_writer: embedded_json model — seeding %d listing/category URLs",
                    len(sample_urls),
                )
            elif na.get("data_source") == "ssr_div_list":
                # Seed the LISTING URL (not per-item URLs) — the ssr_div_list
                # scraper fetches the listing page + extracts records from the
                # DOM directly (no per-item detail pages).
                search = na.get("search") or {}
                sample_urls = [v for v in (search.get("working_url"), search.get("listing_url_used")) if v]
            else:
                il = na.get("item_links") or {}
                sample_urls = il.get("urls") or il.get("url_examples") or []
            if sample_urls:
                # [wave-14 job-133] The writer's seed comes from navigation
                # output — historically it has grabbed nav links from OTHER
                # hosts (the lw.com class). Same full-host filter as intake,
                # so the writer can never seed a poison URL that run_scraper's
                # hygiene belt would just have to strip back out.
                try:
                    from src.seed_urls import dropped_summary, seed_report

                    sample_urls, _sdrops = seed_report(
                        sample_urls, state.get("url") or ""
                    )
                    if _sdrops:
                        logger.warning(
                            "_invoke_code_writer: filtered writer seed — dropped %s",
                            dropped_summary(_sdrops),
                        )
                except Exception:
                    pass
            if sample_urls:
                iu_path = os.path.join(_get_project_root(), "workspace", slug, "input_urls.json")
                # Bug 1 fix: don't overwrite input_urls.json if it already has MORE
                # URLs than the seed set. code_tester's discovery may have saved
                # hundreds/thousands of URLs; overwriting with 5-20 seeds destroys
                # them before run_execution can use them. Only overwrite when the
                # seed set is richer (first run or navigation found more URLs).
                try:
                    if os.path.isfile(iu_path):
                        with open(iu_path, "r") as _ef:
                            _loaded = _json.load(_ef)
                        # [job-88 selfridges] the writer sometimes seeds a BARE
                        # JSON array — `.get` on a list raised AttributeError
                        # and the preserve check silently degraded to an
                        # overwrite that destroyed the discovered URL set.
                        _existing = (
                            _loaded.get("urls", [])
                            if isinstance(_loaded, dict)
                            else _loaded if isinstance(_loaded, list) else []
                        )
                        if len(_existing) > len(sample_urls):
                            logger.info(
                                "_invoke_code_writer: preserving existing input_urls.json "
                                "(%d URLs > %d seeds) — not overwriting",
                                len(_existing), len(sample_urls),
                            )
                            sample_urls = []  # skip the write
                except Exception:
                    pass
                if sample_urls:
                    with open(iu_path, "w") as _f:
                        _json.dump({"urls": sample_urls}, _f, indent=2)
                    logger.info("_invoke_code_writer: wrote %d sample URLs to input_urls.json", len(sample_urls))
        except Exception as _exc:
            logger.warning("_invoke_code_writer: failed to write input_urls.json: %s", _exc)

        # Write discovery_config.json (from scraper_analysis) so the template's
        # discover_product_urls can select the RIGHT config_for_* preset
        # deterministically (load_more vs page_param vs next_button) — driven by
        # the navigator's observation, not code_writer's guess.
        try:
            _sa = state.get("scraper_analysis") or {}
            _dc = _sa.get("discovery_config") if isinstance(_sa, dict) else None
            if _dc and isinstance(_dc, dict) and _dc.get("type"):
                _dc_path = os.path.join(_get_project_root(), "workspace", slug, "discovery_config.json")
                with open(_dc_path, "w") as _df:
                    _json.dump(_dc, _df, indent=2)
                logger.info("_invoke_code_writer: wrote discovery_config.json (type=%s)", _dc.get("type"))
        except Exception as _exc:
            logger.warning("_invoke_code_writer: failed to write discovery_config.json: %s", _exc)

        messages = build_code_writer_message(state)
        _log_agent_context(state, "code-writer", messages)

        # Read the selected template file + inject into the system prompt (so the
        # template code is NEVER summarized by SummarizationMiddleware — the system
        # prompt is always present in full, only the conversation history is
        # summarized). This also saves a read_file round-trip (the LLM already has
        # the template; no need to read_file it).
        _template_file = _select_template_file(state)
        _template_code = ""
        try:
            _tp = os.path.join(_get_project_root(), "templates", _template_file)
            with open(_tp) as _tf:
                _template_code = _tf.read()
            logger.info("_invoke_code_writer: template %s (%d lines) injected into system prompt",
                        _template_file, _template_code.count("\n"))
        except Exception as _exc:
            logger.warning("_invoke_code_writer: could not read template %s: %s", _template_file, _exc)

        # [T2.5/wave-13] Edit-over-write: when the SAME template was the base
        # last cycle AND a parseable draft from that cycle is still on disk,
        # hand the writer the DRAFT in place of the pristine template. The
        # writer then refines a known-good base instead of regenerating from
        # scratch (job 46 class: ~25 min of from-scratch loops when the draft
        # was one targeted edit from passing). A template CHANGE (strategy
        # switch) skips this — regeneration is then genuinely required.
        _eow_active = False
        try:
            from .draft_safety import draft_parses

            if (
                _template_code
                and str(state.get("last_writer_template") or "") == _template_file
            ):
                _eow_draft = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
                if draft_parses(_eow_draft):
                    with open(_eow_draft, "r", encoding="utf-8", errors="replace") as _ef:
                        _template_code = _ef.read()
                    _eow_active = True
                    logger.info(
                        "_invoke_code_writer: edit-over-write — same template %s with a "
                        "parseable draft; the existing DRAFT is the base (no regen)",
                        _template_file,
                    )
        except Exception as _eow_exc:
            logger.warning("_invoke_code_writer: edit-over-write check failed: %s", _eow_exc)

        agent = create_code_writer(site_slug=slug, template_code=_template_code)
        hb = _start_heartbeat(job_id, "code-writer")
        # F5: try/finally — an exception here previously leaked the timer chain.
        _cw_cfg = _agent_config(config, "code_writer")
        try:
            result = _invoke_agent_with_timeout(agent, messages, _cw_cfg, "code_writer", job_id)
        finally:
            _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "code-writer", config)

        # [B1.3/wave-13] Fence recovery: a writer that put the COMPLETE scraper
        # inside a ```python fence in its reply but never called write_file
        # still did the work — the draft is in the transcript. Recover it here,
        # before the failure counters and the FM snapshot run: write the
        # largest parseable fenced block to the draft path and let the normal
        # post-invocation checks see a healthy draft. No code_writer_error_count
        # bump — this is delivery-format recovery, not a failure.
        try:
            from .draft_safety import draft_parses, extract_fenced_python

            _cw_pre = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
            if not draft_parses(_cw_pre):
                _cw_msgs = (result.get("messages") or []) if isinstance(result, dict) else []
                _cw_text = ""
                for _m in reversed(_cw_msgs):
                    _c = getattr(_m, "content", None)
                    if isinstance(_c, str) and _c.strip():
                        _cw_text = _c
                        break
                if _cw_text:
                    _fenced = extract_fenced_python(_cw_text)
                    if _fenced:
                        os.makedirs(os.path.dirname(_cw_pre), exist_ok=True)
                        with open(_cw_pre, "w", encoding="utf-8") as _ff:
                            _ff.write(_fenced)
                        logger.warning(
                            "_invoke_code_writer: no draft on disk — recovered %d-char "
                            "scraper from the response's python fence (job %s)",
                            len(_fenced), job_id,
                        )
        except Exception as _fence_exc:
            logger.warning(
                "_invoke_code_writer: fence recovery failed (job %s): %s", job_id, _fence_exc
            )

        # [jobs-79/80] Snapshot EVERY completed draft to THIS job's FM key
        # immediately — promotion to production only happens at cleanup, which
        # a wedged/killed run never reaches. setup_workspace restores from this
        # key when the local draft is missing (watchdog re-drive after an
        # ephemeral-volume recycle), so the job resumes with the draft its
        # writer actually produced instead of regenerating from nothing.
        try:
            _cw_draft = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
            if os.path.isfile(_cw_draft):
                import src.artifacts as _art

                _art.write(
                    _art.scrapers_key(slug, "jobs", f"scraper-draft-{job_id}.py"),
                    open(_cw_draft, "rb").read(),
                )
        except Exception as _snap_exc:
            logger.warning(
                "_invoke_code_writer: draft FM snapshot failed (job %s): %s", job_id, _snap_exc
            )

        # T0.3/T0.4: a dead invocation (wall-clock timeout, provider exception)
        # used to be indistinguishable from a healthy return — the flow then
        # paid code_tester's full (un-walled) invoke against a draft that was
        # never written and the retry got relabelled "budget". Detect it, count
        # it on a DEDICATED counter (test_retry_count carries
        # FINAL_RETRY_SENTINEL semantics and must not be corrupted), and route
        # past code_tester: one bounce to scraper_analyzer (re-derive strategy
        # + regenerate), then honest escalation to a human.
        _cw_result = result if isinstance(result, dict) else {}
        _cw_err = str(_cw_result.get("_error") or "")
        _cw_dead = bool(_cw_err) or not _cw_result.get("messages")
        _draft_path = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
        # [jobs 83/88 RCA] A killed invocation that ALREADY wrote its draft used
        # to sail through this detector — including a HALF-WRITTEN draft that
        # only fails at test time (both prod jobs' cycle 1 burned on it).
        # Deterministic floor: a dead invocation's draft must at least parse,
        # else it counts as absent (the LLM self-check loops it skipped are
        # gone with the invocation).
        _draft_ok = os.path.isfile(_draft_path)
        _draft_note = ""
        if _draft_ok and _cw_dead:
            try:
                import ast as _ast

                with open(_draft_path, "r", encoding="utf-8", errors="replace") as _df:
                    _ast.parse(_df.read(), filename=_draft_path)
            except Exception as _c_exc:
                _draft_ok = False
                _draft_note = f"dead invocation left an uncompilable draft: {_c_exc}"
                logger.error("_invoke_code_writer: %s (job %s)", _draft_note, job_id)
        # [jobs-79/80] An ALIVE invocation that produced no draft is the same
        # failure as a dead one: the writer replied text-only (no tool calls)
        # — the agent loop ended "successfully" with nothing on disk, the
        # tester then burned its whole cascade CRASHing on the missing file
        # (3 cycles, both prod jobs). The writer's one job is the draft; treat
        # no-draft as failure regardless of how chatty the invocation was.
        if not _draft_ok:
            _err_note = _cw_err or _draft_note or (
                "invocation returned no draft"
                if _cw_result.get("messages")
                else "invocation returned no messages and wrote no draft"
            )
            _err_count = int(state.get("code_writer_error_count") or 0)
            logger.error(
                "_invoke_code_writer: %s invocation — %s (error_count=%d, job %s)",
                "dead" if _cw_dead else "no-op", _err_note, _err_count + 1, job_id,
            )
            _notify_phase(job_id, "code_writer", "failed")
            update["code_writer_error"] = _err_note
            update["code_writer_error_count"] = _err_count + 1
            update["messages"] = []
            if _err_count + 1 >= 2:
                # Second consecutive no-draft — stop burning wall-clock.
                # [jobs-79/80] skip_approvals jobs must CLEANUP here, not
                # interrupt: human_approval auto-approves ("Retry code
                # generation") and the writer no-ops again — an unbounded
                # auto-approve loop. Mirror the S-4 wall-clock arm.
                if state.get("skip_approvals", False):
                    logger.error(
                        "_invoke_code_writer: repeated no-draft writer failures + "
                        "skip_approvals → cleanup (honest failure, job %s)", job_id,
                    )
                    return Command(
                        goto="cleanup",
                        update={
                            **update,
                            "messages": [],
                            "error_message": (
                                f"code_writer failed twice without producing a draft "
                                f"({_err_note})"
                            ),
                        },
                    )
                return Command(
                    goto="human_approval",
                    update={
                        **update,
                        "interrupt_reason": "code_writer_failed",
                        "interrupt_message": (
                            f"code_writer invocation failed twice without producing a draft "
                            f"({_err_note}). Inspect the model/provider logs, then retry or cancel."
                        ),
                        "interrupt_options": ["Retry code generation", "Cancel"],
                        "interrupt_decisions": [
                            {"type": "approve", "label": "Retry code generation",
                             "allow_feedback": True},
                            {"type": "reject", "label": "Cancel", "allow_feedback": False},
                        ],
                    },
                )
            return Command(goto="scraper_analyzer", update=update)
        if _cw_dead:
            # Dead invocation but a draft WAS written (timeout hit after the
            # write) — the draft is usable; log loudly and keep testing.
            logger.warning(
                "_invoke_code_writer: invocation died (%s) but draft exists — proceeding to test (job %s)",
                _cw_err or "no messages", job_id,
            )
            # [S-4 cheap half] count consecutive wall-clock deaths on a usable
            # draft. One death → still worth testing the draft that DID get
            # written. Two in a row → the loop would keep re-running the full
            # writer window against a draft it cannot improve (the most
            # expensive pathological cycle there is) — escalate instead.
            if "wall-clock timeout" in _cw_err:
                _wc = int(state.get("writer_wall_clock_timeouts") or 0) + 1
                update["writer_wall_clock_timeouts"] = _wc
                logger.warning(
                    "_invoke_code_writer: wall-clock timeout #%d with a usable "
                    "draft on disk (job %s)", _wc, job_id,
                )
                if _wc >= 2:
                    _wc_note = (
                        "code_writer hit its wall-clock timeout twice in a row "
                        f"({_cw_err or 'no detail'}) while a draft already "
                        "existed — the generation loop is not making progress."
                    )
                    _notify_phase(job_id, "code_writer", "failed")
                    if state.get("skip_approvals", False):
                        logger.error(
                            "_invoke_code_writer: repeated writer wall-clock "
                            "deaths + skip_approvals → cleanup (honest failure, "
                            "job %s)", job_id,
                        )
                        return Command(
                            goto="cleanup",
                            update={
                                **update,
                                "messages": [],
                                "error_message": _wc_note,
                            },
                        )
                    return Command(
                        goto="human_approval",
                        update={
                            **update,
                            "messages": [],
                            "interrupt_reason": "code_writer_wall_clock",
                            "interrupt_message": _wc_note + (
                                " The existing draft can still be tested or the "
                                "strategy adjusted — retry or cancel."
                            ),
                            "interrupt_options": ["Retry code generation", "Cancel"],
                            "interrupt_decisions": [
                                {"type": "approve", "label": "Retry code generation",
                                 "allow_feedback": True},
                                {"type": "reject", "label": "Cancel", "allow_feedback": False},
                            ],
                        },
                    )

        if not _cw_dead:
            # Healthy run — reset the consecutive wall-clock-death counter.
            update["writer_wall_clock_timeouts"] = 0
        # [T2.5] Record the template this successful draft was built on, so the
        # NEXT cycle can hand the writer its own draft back (edit-over-write)
        # whenever the base template is unchanged. Set only on the usable-draft
        # path — a failed cycle must not arm the next one.
        update["last_writer_template"] = _template_file
        _notify_phase(job_id, "code_writer", "done")
        if _PATCHES_ENABLED:
            # Strategy-drift patches REMOVED (verify-then-delete via run_node --no-patches):
            # _patch_scraper_waits, _patch_scraper_to_playwright — code_writer now emits
            # sleep(8)+domcontentloaded and pure Playwright unaided (Phase 2 prompts).
            # _patch_scraper_xvfb / _discovery / _multisource / _write_discovered_urls_to_input
            # — see deletion notes in the commit/plan.
            _ct = (state.get("content_type_config") or {}).get("content_type") or ""
            if not _ct:
                try:
                    from src.content_types import get_content_type
                    _cfg = get_content_type(state.get("page_type", "product"))
                    _ct = _cfg.name if _cfg else ""
                except Exception:
                    _ct = ""
            _patch_scraper_output_filter(slug, _ct, state.get("target_fields") or [])
            _enforce_discovery_import(slug)
            _enforce_env_discovery_gate(slug)

        # Deterministic backstop: if scraper_analysis documented a non-existent
        # selector in critical_fix, warn loudly if the regenerated scraper still
        # uses it (catches the regression the prompt-level fix in subagents.py
        # is designed to prevent).
        _warn_unaddressed_critical_fix(slug, state.get("scraper_analysis") or {})

        # Syntax guard: code_writer has no shell tool to self-validate, so the
        # node parses the scraper and feeds any SyntaxError back for an
        # immediate fix (keeps syntax errors out of code_tester's path).
        _fix_scraper_syntax(agent, state, config, job_id, slug)

        # CLI-contract guard L1 (docs/cli-contract-plan.md): a draft with no
        # wired discovery trigger would pass --sample testing (seed mode) and
        # ship 1 seed item at execution. Bounce back into THIS agent loop with
        # a targeted fix instruction before code_tester burns its budget.
        _enforce_cli_contract(agent, state, config, job_id, slug)

        # [A2] No-op-fix gate: a draft byte-identical to the one the tester
        # ALREADY tested means this cycle changed nothing — the previous
        # cycle's FAIL verdict still stands, and routing back through
        # scraper_analyzer would re-pick the same strategy and regenerate the
        # same code (job 46: ~25 min of regenerate loops on a draft one edit
        # away from passing). Count consecutive no-op cycles on a dedicated
        # counter; the second one escalates instead of re-testing identical
        # code a third time.
        try:
            import hashlib as _hashlib

            _new_fp = ""
            if os.path.isfile(_draft_path):
                with open(_draft_path, "rb") as _fp_fh:
                    _new_fp = _hashlib.sha1(_fp_fh.read()).hexdigest()
            _tested_fp = str(state.get("last_tested_draft_fp") or "")
            if _new_fp and _tested_fp and _new_fp == _tested_fp:
                _noop = int(state.get("noop_fix_cycles") or 0) + 1
                update["noop_fix_cycles"] = _noop
                logger.warning(
                    "_invoke_code_writer: draft is UNCHANGED from the last "
                    "tested version (no-op fix cycle %d, job %s)", _noop, job_id,
                )
                if _noop_should_escalate(
                    _noop, int(state.get("test_retry_count", 0) or 0)
                ):
                    _noop_note = (
                        "code_writer produced an identical draft twice after "
                        "failed tests — the fix loop is no longer making "
                        "progress on this strategy."
                    )
                    _notify_phase(job_id, "code_writer", "failed")
                    if state.get("skip_approvals", False):
                        logger.error(
                            "_invoke_code_writer: no-op fix loop + skip_approvals "
                            "→ cleanup (honest failure, job %s)", job_id,
                        )
                        return Command(
                            goto="cleanup",
                            update={
                                **update,
                                "messages": [],
                                "error_message": _noop_note,
                            },
                        )
                    return Command(
                        goto="human_approval",
                        update={
                            **update,
                            "messages": [],
                            "interrupt_reason": "noop_fix_loop",
                            "interrupt_message": _noop_note + (
                                " Edit the scraper or change the strategy/fields,"
                                " then retry."
                            ),
                            "interrupt_options": ["Retry code generation", "Cancel"],
                            "interrupt_decisions": [
                                {"type": "approve", "label": "Retry code generation",
                                 "allow_feedback": True},
                                {"type": "reject", "label": "Cancel", "allow_feedback": False},
                            ],
                        },
                    )
            else:
                # Draft changed (or nothing to compare yet) — reset the streak.
                update["noop_fix_cycles"] = 0
        except Exception as _noop_exc:
            logger.debug("_invoke_code_writer: no-op gate skipped: %s", _noop_exc)

        update["messages"] = []
        scraper_analysis = state.get("scraper_analysis") or {}
        strategy = scraper_analysis.get("strategy", "")
        if strategy:
            update["scraping_method"] = strategy
        # [job-82 D6] Route via Command — this node carries NO static out-edge
        # any more. With a registered static code_writer → code_tester edge,
        # LangGraph ran BOTH this destination and every failure Command's
        # destination in the same superstep, so the writer's dead-invocation
        # escalation ladder (scraper_analyzer bounce, human_approval, cleanup)
        # executed only as ghost siblings racing the doomed tester cycle.
        # Same contract as _route_after_execution.
        return Command(goto="code_tester", update=update)
    except Exception:
        _notify_phase(job_id, "code_writer", "failed")
        raise
    finally:
        clear_tool_context()




_PROBE_EXHAUSTION_STOP_REASONS = ("short_page", "no_next_link", "no_new_items")
# [job-316 citybeach] Page cap the discovery probe hands every --discover-only
# run (local env and browser_service env_overrides). The probe's verdict is
# "does this listing yield item URLs" — 3 pages answers that in ~1 min where a
# full 29-page walk blew the probe's 180s bound and left the Phase-2 gate
# blind on deep catalogues. src.listing_discovery honors it only when the
# caller passes no explicit max_pages.
_PROBE_DISCOVERY_PAGE_CAP = "3"


def _normalize_probe_stop_reason(stop_reason: str) -> str:
    """Graph-level mirror of the draft-side reclassification
    (src/listing_discovery): an exhaustion-flavored stop_reason on a ZERO-URL
    probe is the blocked signature, not a genuine catalog end — a real
    catalog-end requires having seen items. Reclassify to ``empty_first_page``
    so ``_discovery_coverage_failure`` arms and ``classify_test_failure`` lands
    on "strategy" (access problem → strategy ladder), never "refine".
    Hard reasons (navigate_error) and unknown reasons pass through untouched.
    """
    sr = str(stop_reason or "")
    if sr in _PROBE_EXHAUSTION_STOP_REASONS:
        return "empty_first_page"
    return sr


def _probe_listing_candidates(state: dict) -> tuple[str, str]:
    """(primary, alternate) ``SCRAPER_LISTING_URL`` candidates for the Phase-1
    probe, mirroring run_execution's chain. [rag-bone job 72] a URL-shaped
    search_criteria is the USER'S OWN listing assertion — the intake UI puts
    the sample PDP in ``url`` and the real listing in search_criteria — so it
    outranks the list_page job URL (which stays ahead of every navigator
    candidate, job-310 contract). The alternate is what the single retry uses
    when the primary yields zero (job-76: the list_page job URL was an ITEM
    page — as a listing it can only ever yield 0). [job-85] the navigator's
    ``discovery.listing_url`` is the retry candidate when it was not already
    the primary — job 85's real listing lived only in search_criteria.
    """
    _sc = str(state.get("search_criteria") or "").strip()
    criteria = _sc if _sc.startswith(("http://", "https://")) else ""
    primary = ""
    if state.get("input_mode") == "list_page":
        _jl = criteria or (state.get("url") or "").strip()
        if _jl.startswith(("http://", "https://")):
            primary = _jl
    _nav = state.get("navigation_analysis") or {}
    _disc = (_nav.get("discovery") if isinstance(_nav, dict) else None) or {}
    _alt = (_disc.get("listing_url") if isinstance(_disc, dict) else "") or ""
    if not _alt:
        # criteria is only a useful retry when the primary is something else.
        _alt = "" if criteria == primary else criteria
    if _alt == primary:
        _alt = ""
    return primary, _alt


def _probe_yield_dead(probe_yield: dict) -> bool:
    """[job-85 supercheapauto] Is this probe yield a DEAD listing?

    The shared predicate (``src.listing_discovery.listing_yield_failure``)
    treats a junk-only yield — a PDP-as-listing's 1 self link, raw count 1-2
    with no usable yield — the same as a raw zero. The old
    ``discovered_urls == 0`` check armed nothing for that class, so the probe
    blessed a listing execution could never crawl.
    """
    try:
        from src.listing_discovery import listing_yield_failure

        # [85-gap-c/wave-13] A --discover-only probe's ``coverage.found`` is 0
        # BY CONSTRUCTION (Phase 2 never ran), not a filter verdict. Passed
        # through verbatim, the predicate's found==0 arm reduces to
        # "discovered>2 + exhaustion stop ⇒ dead" — which declares a healthy
        # SMALL catalogue (job-85's own 2-page listing ends ``no_next_link``
        # with a real yield) a dead listing. Nulling it selects the
        # predicate's "no post-filter signal" arm: judge by raw yield only,
        # which is the only signal a probe has.
        _cov_pf = dict(probe_yield.get("coverage") or {})
        _cov_pf["found"] = None
        return listing_yield_failure({**probe_yield, "coverage": _cov_pf})
    except Exception:
        return int(probe_yield.get("discovered_urls") or 0) == 0


def _probe_retry_warranted(state: dict, probe_yield: dict | None) -> bool:
    """Should the Phase-1 probe retry once on the navigator's listing?

    Only a CLEAN-EXIT DEAD YIELD on the primary candidate warrants it: a
    crash is a code bug (not a listing choice), a real yield means discovery
    works ([job-85] "real" means usable — a PDP's 1 junk link is dead, not
    working), and an inconclusive probe (timeout, dispatch failure) has no
    evidence either way. No distinct same-domain navigator listing → nothing
    better to try. F17 applies to the retry candidate too.
    """
    if not isinstance(probe_yield, dict) or not _probe_yield_dead(probe_yield):
        return False
    primary, alt = _probe_listing_candidates(state)
    if not primary or not alt or alt == primary:
        # primary is only set for list_page; other modes already ran on alt.
        return False
    try:
        from agents.nodes.run_execution import _registrable_of

        _job_reg = _registrable_of(primary)
        _alt_reg = _registrable_of(alt)
        if not (_job_reg and _alt_reg and _alt_reg == _job_reg):
            return False
    except Exception:
        return False
    return True


def _probe_phase1_discovery(
    slug: str, state: dict, job_id: int
) -> tuple[bool, str | None, dict | None]:
    """Probe the draft's Phase-1 discovery, retrying ONCE on the navigator's
    listing when the primary candidate yields zero.

    [job-76 myhouse] The list_page job URL is tried first (job-310 contract),
    but when that URL is an ITEM page the probe tests a listing that can only
    ever yield 0 — while the navigator's promoted listing works. One retry,
    only on a clean-exit zero, only with a distinct same-domain navigator
    listing (see ``_probe_retry_warranted``). Both candidates dead → the
    honest zero stands and the caller's zero-yield gate fires on real
    evidence.
    """
    crashed, tb, probe_yield = _probe_phase1_discovery_once(slug, state, job_id)
    if not crashed and _probe_retry_warranted(state, probe_yield):
        _primary, _alt = _probe_listing_candidates(state)
        logger.info(
            "_probe_phase1_discovery: primary listing yielded 0 (job %s) — "
            "retrying once with the navigator's listing %s",
            job_id, _alt[:80],
        )
        crashed, tb, probe_yield = _probe_phase1_discovery_once(
            slug, state, job_id, listing_override=_alt
        )
    return crashed, tb, probe_yield


def _probe_phase1_discovery_once(
    slug: str, state: dict, job_id: int, listing_override: str = ""
) -> tuple[bool, str | None, dict | None]:
    """One ``--discover-only`` probe run of the draft's Phase-1 discovery.

    ``listing_override`` (the retry path) replaces the candidate chain's
    primary — the F17 domain guard still applies. See
    ``_probe_phase1_discovery`` for the contract.

    Returns ``(crashed, traceback_tail, probe_yield)``. ``probe_yield`` is a
    dict (``discovered_urls``, ``stop_reason``, ``coverage``) ONLY when a fresh
    output file was written by THIS probe run (mtime floor — never a stale
    artifact); timeouts / unsupported flags / no fresh output are inconclusive
    (``crashed=False, probe_yield=None``) — we don't fail on slow or opaque
    discovery.
    """
    if not slug:
        return False, None, None
    # Only jobs with a discovery phase (nav modes); url_list has no Phase 1.
    if state.get("input_mode") not in ("search_term", "list_page", "navigation"):
        return False, None, None
    import subprocess

    try:
        root = _get_project_root()
        draft = os.path.join(root, "workspace", slug, "scraper_draft.py")
        if not os.path.isfile(draft):
            return False, None, None
        # Only probe if the draft actually supports --discover-only (static AST check).
        from agents.nodes.run_execution import _accepted_cli_flags

        accepted = _accepted_cli_flags(draft)
        if accepted is not None and "discover-only" not in accepted:
            return False, None, None
        # C2: mirror run_execution's SCRAPER_LISTING_URL candidate chain so the
        # probe tests the listing execution will actually use (job 310: for
        # list_page the JOB URL outranks the navigator's promotion; F17
        # domain-guards everything). The retry passes listing_override to test
        # the navigator's promotion after the primary yielded zero (job-76).
        _primary, _alt = _probe_listing_candidates(state)
        _probe_env_candidate = listing_override or _primary or _alt
        if _probe_env_candidate:
            try:
                from agents.nodes.run_execution import _registrable_of

                _job_reg = _registrable_of(state.get("url", ""))
                _cand_reg = _registrable_of(_probe_env_candidate)
                if _job_reg and _cand_reg and _cand_reg != _job_reg:
                    logger.warning(
                        "_probe_phase1_discovery: F17 dropped cross-domain listing "
                        "%s (job domain %s)", _probe_env_candidate[:70], _job_reg,
                    )
                    _probe_env_candidate = ""
            except Exception:
                pass
        _probe_env = {**os.environ, "SCRAPER_DISCOVERY_MAX_PAGES": _PROBE_DISCOVERY_PAGE_CAP}
        if _probe_env_candidate:
            _probe_env["SCRAPER_LISTING_URL"] = _probe_env_candidate
        logger.info(
            "_probe_phase1_discovery: running --discover-only (job %s, listing=%s)",
            job_id, (_probe_env_candidate or "<draft default>")[:80],
        )
        probe_args = ["--discover-only", "--fresh-discovery"]
        _probe_started = time.time()
        # Browser scrapers (Playwright/Selenium) can ONLY run in browser_service —
        # celery-worker has neither installed. Running the draft directly here
        # ModuleNotFoundError-crashes every browser draft, which route_after_testing
        # reads as "playwright failed (no items)" → wrong strategy switch. Mirror
        # run_scraper's dispatch: browser draft → browser_service /scrape; else local.
        from agents.tools.shell_tools import _scraper_needs_browser, _get_browser_service_url

        if _scraper_needs_browser(draft):
            import httpx

            try:
                # Stateless /scrape: read the local draft source, POST it.
                try:
                    with open(draft, "r", encoding="utf-8", errors="replace") as _pf:
                        _draft_source = _pf.read()
                except OSError:
                    _draft_source = ""
                # Read sibling files (discovery_config.json) for staging
                _probe_extra = {}
                for _sf in ("input_urls.json", "discovery_config.json"):
                    _sp = os.path.join(os.path.dirname(draft), _sf)
                    if os.path.isfile(_sp):
                        try:
                            with open(_sp, "r", encoding="utf-8", errors="replace") as _fh:
                                _probe_extra[_sf] = _fh.read()
                        except OSError:
                            pass
                resp = httpx.post(
                    f"{_get_browser_service_url()}/scrape",
                    json={
                        "scraper_source": _draft_source,
                        "scraper_name": os.path.basename(draft),
                        "extra_files": _probe_extra,
                        "args": probe_args,
                        "timeout": 180,
                        "max_retries": 1,
                        # C2: execution-conditions listing for browser drafts too.
                        # The page cap rides along so browser drafts probe fast
                        # for the same reason local ones do.
                        **({"env_overrides": {
                            **({"SCRAPER_LISTING_URL": _probe_env_candidate}
                               if _probe_env_candidate else {}),
                            "SCRAPER_DISCOVERY_MAX_PAGES": _PROBE_DISCOVERY_PAGE_CAP,
                        }}),
                    },
                    timeout=180 + 60,
                )
                resp.raise_for_status()
                result = resp.json()
                rc = result.get("returncode", 0)
                stderr = result.get("stderr") or ""
                # T2.1: stdout used to be discarded on both paths.
                _stdout = result.get("stdout") or ""
            except Exception as exc:
                logger.warning(
                    "_probe_phase1_discovery: browser_service dispatch failed (%s) — inconclusive",
                    exc,
                )
                return False, None, None
        else:
            proc = subprocess.run(
                ["python3", draft] + probe_args,
                cwd=os.path.join(root, "workspace", slug),
                capture_output=True, text=True, timeout=180,
                env=_probe_env,
            )
            rc = proc.returncode
            stderr = proc.stderr or ""
            _stdout = proc.stdout or ""
        if rc != 0 and "Traceback" in stderr:
            lines = stderr.strip().splitlines()
            tail = "\n".join(lines[-12:]) if lines else stderr[:800]
            logger.warning(
                "_probe_phase1_discovery: CRASHED (job %s, rc=%s):\n%s",
                job_id, rc, tail,
            )
            return True, tail, None
        # argparse exit(2) carries NO Traceback — without this hook the probe
        # silently passed a draft whose CLI rejects the execution flags
        # (CLI-contract plan v2 hand-off). Treat it as a crash with the
        # unrecognized-argument list in the tail.
        if rc == 2 and "unrecognized arguments" in stderr:
            tail = stderr.strip()[-800:]
            logger.warning(
                "_probe_phase1_discovery: ARGPARSE REJECTED execution flags "
                "(job %s): %s",
                job_id, tail,
            )
            return True, tail, None
        logger.info(
            "_probe_phase1_discovery: OK (job %s, rc=%s)", job_id, rc
        )
        # [job-65 citybeach] Read THIS probe's yield from the output file it
        # just wrote (mtime floor — a pre-probe artifact must never yield a
        # verdict). Every two-phase template emits
        # metadata.discovery_coverage.{discovered_urls, stop_reason}; on a
        # --discover-only run ``found`` is 0 by construction, so the yield is
        # ``discovered_urls`` (int; some templates emit a list).
        probe_yield: dict | None = None
        try:
            # [job-77 RC4] The floor is LOAD-BEARING here, not just a filter:
            # without it the F16 substantive-count ranking picks the tester's
            # older NON-EMPTY output over this probe's fresh 0-item one, the
            # getmtime check below then (correctly) rejects the stale file as
            # inconclusive — and the zero-yield gate is structurally blind in
            # exactly the case it exists for (probe 0 next to a 5-item testing
            # file). A floored call also bypasses the FM fallback and, with
            # only this probe's files eligible, the count ranking reduces to
            # max() over the wrapper's own attempts — correct for the
            # retry-once-then-last-attempt-verdict contract.
            _probe_out = _find_newest_output(
                os.path.join(root, "workspace", slug),
                os.path.join(root, "scrapers", slug),
                slug=slug,
                mtime_floor=_probe_started - 5,
            )
            if _probe_out:
                _cov = _read_discovery_coverage(_probe_out) or {}
                _disc = _cov.get("discovered_urls")
                if isinstance(_disc, list):
                    _disc = len(_disc)
                try:
                    _disc_n = int(_disc or 0)
                except (TypeError, ValueError):
                    _disc_n = 0
                _sr = str(_cov.get("stop_reason") or "")
                probe_yield = {
                    "discovered_urls": _disc_n,
                    "stop_reason": _sr,
                    "coverage": dict(_cov),
                }
                logger.info(
                    "_probe_phase1_discovery: discovered=%s stop_reason=%s "
                    "(probe output %s, stdout tail: %r)",
                    _disc_n, _sr or "?", os.path.basename(_probe_out),
                    _stdout.strip()[-200:],
                )
            else:
                logger.info(
                    "_probe_phase1_discovery: no fresh probe output — yield "
                    "inconclusive (job %s)", job_id,
                )
        except Exception as _pexc:
            logger.debug("_probe_phase1_discovery: coverage read skipped: %s", _pexc)
        return False, None, probe_yield
    except subprocess.TimeoutExpired:
        logger.info(
            "_probe_phase1_discovery: timed out (job %s) — inconclusive", job_id
        )
    except Exception as exc:
        logger.warning("_probe_phase1_discovery: errored (job %s): %s", job_id, exc)
    return False, None, None


def _invoke_code_tester(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    retry_count = state.get("test_retry_count", 0)
    _notify_phase(job_id, "code_tester", "running")
    if retry_count > 0:
        try:
            from scraper.models import Step

            note = "FINAL retry" if retry_count == FINAL_RETRY_SENTINEL else f"Retry cycle {retry_count}"
            Step.objects.filter(job_id=job_id, phase="testing").update(notes=note)
        except Exception:
            pass
    # _check_strategy_mismatch REMOVED (Fix B): with Phase 2 prompts, code_writer
    # emits the correct Playwright strategy unaided — verified via run_node
    # code_writer --no-patches (0 seleniumbase). The deterministic pre-test guard
    # no longer fires; strategy drift is handled by the normal test→retry loop.
    set_tool_context(dict(state), agent_name="code_tester")
    # [B1.2/wave-13] Draft-presence guard at tester entry. A missing or
    # unparseable draft turns the whole invocation into a pre-ordained
    # "No such file" absent-draft cycle — a window burned on a filesystem
    # race, not on the code. Restore the job's own FM-archived draft FIRST;
    # only count absence when the restore cannot save the run. The counter
    # feeds route_after_testing's absent arm (reset on a parseable draft,
    # incremented on absence) because routing functions cannot mutate state.
    _te_absent = 0
    try:
        from .draft_safety import draft_parses, restore_job_draft

        _te_slug = state.get("site_slug", "")
        _te_draft = (
            os.path.join(_get_project_root(), "workspace", _te_slug, "scraper_draft.py")
            if _te_slug else ""
        )
        if _te_draft and not draft_parses(_te_draft):
            _te_restored = restore_job_draft(
                _get_project_root(), _te_slug, state.get("job_id", 0)
            )
            if _te_restored:
                logger.warning(
                    "_invoke_code_tester: draft absent/unparseable at entry — "
                    "restored job archive copy %s", _te_restored,
                )
        if not (_te_draft and draft_parses(_te_draft)):
            _te_absent = int(state.get("draft_absent_count") or 0) + 1
            logger.error(
                "_invoke_code_tester: NO parseable draft at entry (absent %d) "
                "— the tester will run against a missing file (job %s)",
                _te_absent, job_id,
            )
    except Exception as _te_exc:
        logger.warning("_invoke_code_tester: draft entry guard failed: %s", _te_exc)
    try:
        logger.info("_invoke_code_tester: starting (job %s)", job_id)
        # [A6/job-73 RC1] Stamp the test START. _freshness_floor raises the
        # output floor to last_tested_at on a same-draft re-test — stamping at
        # node EXIT (after the draft wrote its outputs) excluded the current
        # attempt's own passing output from every gate: job 73's 20/20 run was
        # invisible to ground truth and the cascade "exhausted" on a working
        # scraper. Entry stamp keeps A6's protection (prior attempts' outputs
        # predate it) while this attempt's outputs stay visible.
        _test_started_at = time.time()
        messages = build_code_tester_message(state)
        _log_agent_context(state, "code-tester", messages)
        slug = state.get("site_slug", "")
        agent = create_code_tester(site_slug=slug)
        hb = _start_heartbeat(job_id, "code-tester")
        # F5 guard; T0.2: was a raw invoke with NO wall-clock cap.
        _ct_cfg = _agent_config(config, "code_tester")
        try:
            result = _invoke_agent_with_timeout(
                agent, messages, _ct_cfg, "code_tester", job_id,
                timeout=_tester_invoke_timeout(),
            )
        finally:
            _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "code-tester", config)
        update = {"messages": [], "draft_absent_count": _te_absent}
        # [job-81 N-C] A dead invocation invalidates whatever verdict is on
        # disk: the report was written by a PREVIOUS cycle about a PREVIOUS
        # draft (job 81: the "cascade exhausted" routing consumed cycle-2's
        # CRASH report, 70 min stale, while cycle-3's fresh draft sat
        # unjudged). The mtime floor on _load_test_report below enforces that.
        # Count consecutive wall-clock deaths on a DEDICATED counter (mirrors
        # the writer's arm): route_after_testing escalates at 2 — this node
        # must NOT Command-route, its conditional edge to route_after_testing
        # would union with the goto and run both destinations (D6).
        _ct_res = result if isinstance(result, dict) else {}
        _ct_err = str(_ct_res.get("_error") or "")
        _ct_dead = bool(_ct_err) or not _ct_res.get("messages")
        if _ct_dead:
            logger.error(
                "_invoke_code_tester: invocation DIED (%s) — on-disk reports "
                "predating this attempt will be rejected (job %s)",
                _ct_err or "no messages", job_id,
            )
        # [B2.6] The escalation counter itself is set BELOW, after the verdict
        # is resolved — "invocation alive but produced no verdict" burns the
        # same window as a wall-clock death and must count too (parity).
        _notify_phase(job_id, "code_tester", "done")
        # [A2/A1/A6] Fingerprint the draft THIS test just ran + track same-draft
        # re-tests. The fingerprint feeds route_after_testing's freshness floor
        # (A6) and the writer's no-op-fix gate (A2); the same-draft counter is
        # the retest cap's budget (A1/QW-3 — routing functions cannot mutate
        # state, so it is maintained here where the same-draft fact is directly
        # observable: same fingerprint twice in a row = re-test, changed draft
        # = reset).
        _draft_fp = ""
        try:
            import hashlib as _hashlib

            _draft_for_fp = (
                os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
                if slug else ""
            )
            if _draft_for_fp and os.path.isfile(_draft_for_fp):
                with open(_draft_for_fp, "rb") as _fp_fh:
                    _draft_fp = _hashlib.sha1(_fp_fh.read()).hexdigest()
        except Exception as _fp_exc:
            logger.debug("_invoke_code_tester: draft fingerprint failed: %s", _fp_exc)
        update["last_tested_draft_fp"] = _draft_fp
        update["last_tested_at"] = _test_started_at
        if _draft_fp:
            _prev_fp = str(state.get("last_tested_draft_fp") or "")
            _prev_retests = int(state.get("test_retest_count", 0) or 0)
            update["test_retest_count"] = (
                _prev_retests + 1 if _draft_fp == _prev_fp else 0
            )
        # [job-81 N-C] min_mtime: only a report written DURING this attempt
        # (after the entry stamp) is this attempt's verdict. A dead/no-op
        # invocation would otherwise adopt the previous cycle's report.
        report = _load_test_report(slug, min_mtime=_test_started_at)
        # only attempt the repair when the report FILE exists but would not
        # parse (a genuinely-missing file is the F19 no-report path, not a
        # corruption case — repairing it would be a no-op call).
        _report_path = os.path.join(
            _get_project_root(), "workspace", slug, "test_report.json"
        ) if slug else ""
        if (
            not report and slug and os.path.isfile(_report_path)
            # [job-81 N-C] Only repair a file THIS attempt wrote. A corrupt
            # report from a PREVIOUS cycle predates the stamp — repairing it
            # would bump its mtime past the floor and re-adopt the stale
            # verdict the floor just rejected.
            and os.path.getmtime(_report_path) >= _test_started_at
        ):
            # _run_budgeted_agent, so it never had an artifact_fix_fn. A corrupt
            # test_report.json (the priceline instance: literal control chars)
            # made _load_test_report return None and route_after_testing then
            # burned the retry loop on a phantom "no report" failure. Repair
            # once and reload BEFORE concluding the report is missing.
            try:
                _fix_json_artifact(slug, "test_report.json")
                # The repair rewrote the file — its mtime is fresh, so the
                # same floor applies cleanly.
                report = _load_test_report(slug, min_mtime=_test_started_at)
            except Exception as exc:
                logger.warning(
                    "_invoke_code_tester: test_report repair failed: %s", exc
                )
        if not report:
            # This attempt produced NO verdict — a previous cycle's report
            # must not ride along in LangGraph state (the key persists across
            # cycles unless overwritten; route_after_testing reads it from
            # state, so a dead/no-op attempt would silently route on the
            # LAST cycle's verdict even with the file-level mtime floor).
            update["test_report"] = None
        # [B2.6/wave-13] Parity: the escalation counter keys on "no verdict
        # for THIS attempt", not merely "the invocation object died". A
        # healthy invocation that wrote no on-disk report burned the same
        # window against the same wall — count it. healthy+report → 0;
        # dead+wall-clock → +1 (unchanged); alive+no report → +1;
        # dead-non-wall-clock (provider crash) stays untouched — different
        # failure class, and the retry ladder already owns it.
        if report:
            update["tester_wall_clock_timeouts"] = 0
        elif _ct_dead and "wall-clock timeout" in _ct_err:
            update["tester_wall_clock_timeouts"] = (
                int(state.get("tester_wall_clock_timeouts") or 0) + 1
            )
        elif not _ct_dead:
            update["tester_wall_clock_timeouts"] = (
                int(state.get("tester_wall_clock_timeouts") or 0) + 1
            )
        # [job-81 N-C] Two consecutive wall-clock deaths and STILL no verdict
        # for this attempt: stage the escalation — route_after_testing's
        # no-report arm sends counter>=2 to human_approval (or cleanup under
        # skip_approvals, where an auto-approved retry would just burn a third
        # full window against the same wall). The FRESH draft is never judged
        # by what happened here; the interrupt offers re-testing it, not
        # regenerating it.
        _ct_twc = int(
            update.get(
                "tester_wall_clock_timeouts",
                state.get("tester_wall_clock_timeouts") or 0,
            )
            or 0
        )
        if not report and _ct_twc >= 2:
            _ct_note = (
                "code_tester hit its wall-clock timeout twice in a row "
                f"({_ct_err or 'no detail'}) — the site is too slow for the "
                "testing window or the runs are wedging; testing is not "
                "making progress. A draft exists but no verdict was produced "
                "for it."
            )
            if state.get("skip_approvals", False):
                logger.error(
                    "_invoke_code_tester: repeated wall-clock deaths + "
                    "skip_approvals → honest failure (job %s)", job_id,
                )
                update["error_message"] = _ct_note
                update["execution_status"] = "FAILED"
            else:
                update["interrupt_reason"] = "code_tester_wall_clock"
                update["interrupt_message"] = _ct_note + (
                    " The existing draft can still be re-tested or executed "
                    "anyway — retry, execute, or cancel."
                )
                update["interrupt_options"] = [
                    "Retry testing", "Execute anyway", "Cancel",
                ]
                update["interrupt_decisions"] = [
                    {"type": "approve", "label": "Retry testing",
                     "allow_feedback": True},
                    {"type": "approve", "label": "Execute anyway",
                     "allow_feedback": False},
                    {"type": "reject", "label": "Cancel", "allow_feedback": False},
                ]
        if report:
            # Phase 4a: deterministically attach the scraper's discovery_coverage
            # so the coverage-aware classifier sees it (the LLM-written report
            # doesn't reliably carry it).
            report = _attach_discovery_coverage(report, slug)
            # Job-311: detect a transient site-side render block (newest
            # output empty_render, an earlier one carried items) BEFORE the
            # classifier turns it into a strategy verdict.
            report = _attach_transient_render_evidence(report, slug)
            # T2.2/T2.3: deterministically check the OUTPUT rows (double-host
            # URLs, inverted price pairs, mapped-but-empty fields) and merge
            # the findings into report.issues — the tester's LLM misses these
            # mechanical defects and routing arms on their suggested_fix shape.
            try:
                from .nodes.route_after_testing import deterministic_output_issues

                _det = deterministic_output_issues(slug, state)
                if _det:
                    _known = {
                        (str(i.get("field")), str(i.get("description")))
                        for i in (report.get("issues") or [])
                        if isinstance(i, dict)
                    }
                    _added = [i for i in _det if (str(i.get("field")), str(i.get("description"))) not in _known]
                    if _added:
                        report["issues"] = (report.get("issues") or []) + _added
                        logger.warning(
                            "_invoke_code_tester: %d deterministic output defect(s) "
                            "appended to test_report: %s",
                            len(_added),
                            "; ".join(str(i.get("description", ""))[:90] for i in _added),
                        )
            except Exception as _dexc:
                logger.debug("_invoke_code_tester: deterministic checks skipped: %s", _dexc)
            update["test_report"] = report
            logger.info(
                "_invoke_code_tester: loaded test_report from workspace/%s/", slug
            )
            _preserve_test_report(slug)
            # Partner sample (fold B1): persist the first PASS's records +
            # emit job.sample_ready — NOT at field_confirmation (dead code for
            # partner jobs; sample_only bounces before its sample block).
            # Idempotent across retry cycles (dedupe sample:{job_id}).
            try:
                if job_id:
                    from scraper.models import ScrapeJob as _SJ

                    _job = _SJ.objects.filter(pk=job_id).first()
                    if _job is not None:
                        from scraper.api.sample_persist import persist_partner_sample

                        persist_partner_sample(_job, slug=slug, report=report)
            except Exception as exc:  # never break the graph for the sample
                logger.warning("_invoke_code_tester: partner sample hook: %s", exc)
        else:
            logger.warning(
                "_invoke_code_tester: no test_report found at workspace/%s/", slug
            )
            # F19 (prod 352 pattern): retries exhausted (or final attempt) with
            # no test_report — route_after_testing sends this to cleanup WITHOUT
            # execution. Previously the finalize ladder saw no error_message and
            # no execution_status → blessed the job COMPLETED with 0 items
            # (D2-pattern in new clothes). Record the failure so it finalizes
            # FAILED honestly. (Not a rescue case — the rescue path requires
            # real output items, checked in route_after_testing.)
            # NB: is_final_attempt is NOT in scope here (that's
            # route_after_testing's local) — derive it the same way: the
            # sentinel marks the final attempt (route_after_testing.py:391).
            _is_last = (
                retry_count == FINAL_RETRY_SENTINEL
                or retry_count >= MAX_TEST_RETRIES
            )
            _has_real_out = False
            try:
                _ws = os.path.join(_get_project_root(), "workspace", slug)
                if os.path.isdir(_ws):
                    import glob as _glob

                    for _op in sorted(
                        _glob.glob(os.path.join(_ws, "output_*.json")),
                        key=os.path.getmtime,
                        reverse=True,
                    )[:3]:
                        try:
                            if _substantive_item_count(_op) > 0:
                                _has_real_out = True
                                break
                        except Exception:
                            pass
            except Exception:
                pass
            if _is_last and not _has_real_out:
                update["error_message"] = (
                    f"Testing exhausted {MAX_TEST_RETRIES} retries without a "
                    "test_report — code_writer failed to produce a working "
                    "scraper (repeated 900s timeouts). No output produced; "
                    "not executed."
                )
                update["execution_status"] = "FAILED"

        # Deterministic Phase-1 discovery probe: catches discovery-path crashes
        # that --sample testing skips (e.g. session.url phantom attributes). On a
        # real crash, force the test to FAIL so route_after_testing retries
        # code_writer with the traceback. [job-65] A clean-exit run that
        # discovered ZERO URLs fails the test too — under execution conditions
        # (same listing injection as run_execution), so a draft whose discovery
        # cannot see the site never reaches the 0-item execution.
        crashed, tb, probe_yield = _probe_phase1_discovery(slug, dict(state), job_id)
        if crashed:
            report = report or {}
            report["overall_assessment"] = "FAIL"
            report["confidence_score"] = 0.0
            report["ready_for_execution"] = False
            report.setdefault("issues", []).insert(
                0,
                # Issue-shape fix (P1): carry BOTH keys — _summarize_test_report
                # reads description/field, code-tester.md documents problem, the
                # marker relay reads message. One vocabulary going forward.
                {
                    "severity": "high",
                    "message": "Phase-1 discovery crashed: " + (tb or ""),
                    "description": "Phase-1 discovery crashed: " + (tb or ""),
                },
            )
            report["feedback_for_writer"] = (
                "PHASE-1 DISCOVERY CRASH — caught by the deterministic discovery "
                "probe (which --sample skips, since --sample uses pre-seeded URLs "
                "and never enters Phase 1):\n" + (tb or "")
                + "\nFix the discovery/pagination code. Do NOT re-signature the "
                "template's helpers or reference nonexistent attributes (e.g. "
                "`session.url` — capture the URL from the response instead)."
            )
            update["test_report"] = report
            logger.warning(
                "_invoke_code_tester: discovery probe FAILED the test (job %s) → retry", job_id
            )
        elif (
            probe_yield is not None
            and _probe_yield_dead(probe_yield)
            and isinstance(report, dict)
            and report.get("discovery_transient")
        ):
            # Job-311 lesson: a transient site-side block window must not burn
            # the strategy ladder — the draft demonstrably worked earlier in
            # this same testing phase.
            logger.info(
                "_invoke_code_tester: probe found no usable URLs but "
                "discovery_transient evidence is attached — suppressing the "
                "zero-yield verdict (job %s)",
                job_id,
            )
        elif probe_yield is not None and _probe_yield_dead(probe_yield):
            report = report or {}
            report["overall_assessment"] = "FAIL"
            report["confidence_score"] = 0.0
            report["ready_for_execution"] = False
            _zcov = dict(probe_yield.get("coverage") or {})
            _zsr = _normalize_probe_stop_reason(
                _zcov.get("stop_reason") or probe_yield.get("stop_reason") or ""
            )
            _zcov.update({
                "stop_reason": _zsr,
                "ran_phase1": True,
                "discovered_urls": 0,
                "probe_scope": "discover_only",
            })
            report["discovery_coverage"] = _zcov
            report.setdefault("phases_tested", {})["phase1_discovery"] = False
            _zmsg = (
                "Phase-1 discovery probe: clean exit but no usable item URLs "
                f"discovered under execution conditions (stop_reason={_zsr}). "
                "The draft's discovery cannot see this site's items with the "
                "current strategy."
            )
            report.setdefault("issues", []).insert(
                0,
                {"severity": "high", "message": _zmsg, "description": _zmsg},
            )
            report["feedback_for_writer"] = (
                "PHASE-1 DISCOVERY YIELDED 0 URLS — caught by the deterministic "
                "discovery probe (--discover-only, the listing execution will "
                f"use, SCRAPER_LISTING_URL injected):\nstop_reason={_zsr}\n"
                "Fix the discovery selectors/URL-building for this listing's "
                "markup. Do NOT remove the shared discovery module wiring."
            )
            update["test_report"] = report
            _retry_z = state.get("test_retry_count", 0)
            _is_last_z = (
                _retry_z == FINAL_RETRY_SENTINEL or _retry_z >= MAX_TEST_RETRIES
            )
            if _is_last_z:
                update["error_message"] = (
                    f"{_zmsg} Retries exhausted; refusing a guaranteed-0-item "
                    "execution."
                )
                update["execution_status"] = "FAILED"
            logger.warning(
                "_invoke_code_tester: discovery probe ZERO-YIELD FAILED the "
                "test (job %s, stop_reason=%s, retry_count=%s)",
                job_id, _zsr, _retry_z,
            )
        elif probe_yield is not None:
            # Probe succeeded with real yield: make the self-reported
            # phase1_discovery boolean deterministic and record that the
            # coverage verdict now reflects execution conditions. ``found``
            # stays the tester's own (the probe skips Phase 2 by design —
            # _volume_gap reads found/discovered and must see the real one).
            _pcov = dict(report.get("discovery_coverage") or {}) if isinstance(report, dict) else {}
            _pcov.update({
                "stop_reason": probe_yield.get("stop_reason") or _pcov.get("stop_reason"),
                "ran_phase1": True,
                "discovered_urls": probe_yield["discovered_urls"],
                "probe_scope": "discover_only",
            })
            report["discovery_coverage"] = _pcov
            report.setdefault("phases_tested", {})["phase1_discovery"] = (
                probe_yield["discovered_urls"] > 0
            )
            update["test_report"] = report
            logger.info(
                "_invoke_code_tester: probe yield %s URLs — phase1_discovery set "
                "deterministically (job %s)", probe_yield["discovered_urls"], job_id,
            )

        # L2 CLI-contract hard gate (docs/cli-contract-plan.md): the LOAD-BEARING
        # closure. A draft with no wired discovery trigger passes --sample
        # testing (seed mode) and would ship seed-only at execution. Force the
        # report FAIL so route_after_testing skips the PASS exit entirely (the
        # job-7 escape) and bounces to code_writer with the violation.
        _cli_violation = None
        try:
            from .nodes.run_execution import cli_contract_violation

            _draft = os.path.join(
                _get_project_root(), "workspace", slug, "scraper_draft.py"
            )
            _sa_t = state.get("scraper_analysis")
            _strategy_t = (
                (_sa_t.get("strategy") or "") if isinstance(_sa_t, dict) else ""
            )
            _cli_violation = cli_contract_violation(
                _draft, state.get("input_mode", ""), _strategy_t
            )
        except Exception as _exc:
            logger.warning(
                "_invoke_code_tester: CLI-contract check errored (job %s): %s",
                job_id, _exc,
            )
        if _cli_violation:
            report = report or {}
            report["overall_assessment"] = "FAIL"
            report["confidence_score"] = 0.0
            report["ready_for_execution"] = False
            report.setdefault("issues", []).insert(
                0,
                {
                    "severity": "high",
                    "message": _cli_violation,
                    "description": _cli_violation,
                },
            )
            report["feedback_for_writer"] = (
                "CLI CONTRACT VIOLATION (deterministic — testing cannot pass "
                "while discovery is unwired):\n" + _cli_violation + "\n"
                "Add the missing argparse declarations AND the "
                "SCRAPER_LISTING_URL env gate in main(), exactly as your "
                "template does. Use edit_file; do NOT rewrite the scraper."
            )
            update["test_report"] = report
            # F19-pattern honest failure when the retry budget is spent.
            _retry_now = state.get("test_retry_count", 0)
            _is_last_now = (
                _retry_now == FINAL_RETRY_SENTINEL
                or _retry_now >= MAX_TEST_RETRIES
            )
            if _is_last_now:
                update["error_message"] = (
                    _cli_violation
                    + " — retries exhausted; refusing seed-only execution."
                )
                update["execution_status"] = "FAILED"
            logger.warning(
                "_invoke_code_tester: CLI contract violation → test FAIL "
                "(job %s, retry_count=%s)", job_id, _retry_now,
            )
        return update
    except Exception:
        _notify_phase(job_id, "code_tester", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_cleanup(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "cleanup", "running")
    set_tool_context(dict(state), agent_name="cleanup")
    try:
        logger.info("_invoke_cleanup: starting (job %s)", job_id)

        slug = state.get("site_slug", "")
        # Archive the current production scraper BEFORE the agent runs, so we can
        # restore it on failure (the agent used to clobber it unconditionally).
        archive_path = _archive_existing_scraper(slug)

        messages = build_cleanup_message(state)
        _log_agent_context(state, "cleanup", messages)
        agent = create_cleanup_agent(site_slug=slug)
        # [wave-15 PR-2a] This was the last raw un-walled invoke on the happy
        # path (skill_learner got the same treatment in QW-1): a hung cleanup
        # LLM call produced no heartbeat and no SessionLog rows, so the job
        # looked silent to the watchdog while the thread sat in a socket
        # read. Bound like every other LLM phase — wall-clock cap, [INVOKE-
        # TIMEOUT] row on expiry, heartbeat beats while it waits.
        hb = _start_heartbeat(job_id, "cleanup")
        try:
            result = _invoke_agent_with_timeout(
                agent, messages, _agent_config(config, "cleanup"),
                "cleanup", job_id,
            )
        finally:
            _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "cleanup", config)

        # Deterministic, failure-safe scraper promotion (the agent no longer cp's
        # scraper.py — see build_cleanup_message). Per-job copy + success gate.
        scraper_path = _promote_scraper(
            slug, job_id, state.get("execution_status", ""), archive_path,
            product_count=state.get("product_count"),
        )
        # Keep the failed cycle's evidence (pillowtalk gap): the workspace is
        # wiped by the next job; a FAILED job must leave its test report.
        _archive_failure_evidence(slug, job_id, state.get("execution_status", ""))
        _notify_phase(job_id, "cleanup", "done")
        out: dict[str, Any] = {"messages": []}
        if scraper_path:
            out["scraper_path"] = scraper_path
        return out
    except Exception:
        _notify_phase(job_id, "cleanup", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_skill_learner(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    # Skip on non-SUCCESS: learning from a failed/incomplete scrape injects
    # garbage into the skill DB (skill_learner writes reusable skills +
    # copies learning_report.json into scrapers/<slug>/analysis/). Mirrors
    # _invoke_store_job_listings's guard. != SUCCESS is forward-compatible
    # with a future PARTIAL status.
    # [QW-1] Also skip when code generation was skipped (re-scrape reusing the
    # existing scraper): nothing new was learned about generation, and the
    # tail LLM call only re-derives what the first run already recorded.
    if (
        state.get("execution_status", "FAILED") != "SUCCESS"
        or state.get("skip_code_generation")
    ):
        logger.info(
            "_invoke_skill_learner: skipping (execution_status=%s, "
            "skip_code_generation=%s, job %s)",
            state.get("execution_status"), state.get("skip_code_generation"), job_id,
        )
        _notify_phase(job_id, "skill_learner", "skipped")
        return {"messages": []}
    _notify_phase(job_id, "skill_learner", "running")
    set_tool_context(dict(state), agent_name="skill_learner")
    try:
        logger.info("_invoke_skill_learner: starting (job %s)", job_id)
        messages = build_skill_learner_message(state)
        _log_agent_context(state, "skill-learner", messages)
        slug = state.get("site_slug", "")
        agent = create_skill_learner(site_slug=slug)
        # [QW-1] underscore key so AGENT_RECURSION_MAP["skill_learner"] applies,
        # wall-clock cap + heartbeat like every other LLM tail phase (this was
        # the last raw un-walled invoke on the happy path).
        hb = _start_heartbeat(job_id, "skill-learner")
        try:
            result = _invoke_agent_with_timeout(
                agent, messages, _agent_config(config, "skill_learner"),
                "skill_learner", job_id,
            )
        finally:
            _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "skill-learner", config)
        _notify_phase(job_id, "skill_learner", "done")

        if slug:
            try:
                import src.artifacts as artifacts
                from django.conf import settings

                ws = os.path.join(settings.PROJECT_ROOT, "workspace", slug)
                # Preserve learning + nav_learning reports to the File Master.
                # M4 copy-path guard: never publish corrupt bytes (they are
                # re-hydrated into later jobs by setup_workspace).
                from .tools.filesystem_tools import guard_json_bytes

                for _name in ("learning_report.json", "nav_learning_report.json"):
                    _src = os.path.join(ws, _name)
                    if os.path.isfile(_src):
                        with open(_src, "rb") as _f:
                            _bytes = _f.read()
                        _guarded, _note = guard_json_bytes(_bytes)
                        if _guarded is None:
                            logger.error(
                                "_invoke_skill_learner: %s is corrupt and "
                                "unrepairable (%s) — SKIPPED, not published to "
                                "the File Master", _name, _note,
                            )
                            continue
                        if _note:
                            logger.warning(
                                "_invoke_skill_learner: %s was corrupt — "
                                "publishing REPAIRED version (%s)", _name, _note,
                            )
                        _key = artifacts.scrapers_key(slug, "analysis", _name)
                        artifacts.write(_key, _guarded)
                        logger.info(
                            "_invoke_skill_learner: copied %s → scrapers/%s/analysis/",
                            _name, slug,
                        )
            except Exception as exc:
                logger.debug("skill_learner: failed to preserve reports: %s", exc)

        return {"messages": []}
    except Exception:
        _notify_phase(job_id, "skill_learner", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_dagster_converter(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any]:
    """Post-completion agent: convert the existing scraper into the client's
    BaseTlsScraper format. Non-blocking — failure is logged but doesn't affect
    the job status."""
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    # Dagster conversion is OPT-IN ([dagster-opt-in], intake checkbox /
    # partner-API flag): skip unless the job explicitly asked for it. state.get
    # (not state[...]) because tests drive this node with a bare dict.
    if not state.get("dagster_enabled", False):
        logger.info(
            "_invoke_dagster_converter: skipping (not opted in, job %s)", job_id
        )
        return {"messages": []}
    # P2 (Railway job-1 forensics): skip on non-SUCCESS — converting a
    # scraper for a failed job burned 6 minutes on prod. Same guard pattern
    # as skill_learner/store_job_listings.
    if state.get("execution_status", "FAILED") != "SUCCESS":
        logger.info(
            "_invoke_dagster_converter: skipping (execution_status=%s, job %s)",
            state.get("execution_status"), job_id,
        )
        return {"messages": []}
    _notify_phase(job_id, "dagster_converter", "running")

    # Only run if the scraper exists + job succeeded
    try:
        root = _get_project_root()
        scraper_exists = os.path.isfile(
            os.path.join(root, "scrapers", slug, "scraper.py")
        ) or os.path.isfile(
            os.path.join(root, "workspace", slug, "scraper_draft.py")
        )
        if not scraper_exists:
            logger.info("_invoke_dagster_converter: no scraper found for %s — skipping", slug)
            _notify_phase(job_id, "dagster_converter", "skipped")
            return {"messages": []}
    except Exception:
        _notify_phase(job_id, "dagster_converter", "skipped")
        return {"messages": []}

    set_tool_context(dict(state), agent_name="dagster_converter")
    try:
        logger.info("_invoke_dagster_converter: starting (job %s, slug %s)", job_id, slug)

        ws_dagster = os.path.join(root, "workspace", slug, f"{slug}_dagster.py")
        scrapers_dagster = os.path.join(root, "scrapers", slug, f"{slug}_dagster.py")

        # T3.1: deterministic renderer first — 0 LLM calls on the happy path.
        # (job 302: the LLM converter burned 34 calls / 7m05s of wall hand-copying
        # the draft's own parsing logic. The renderer does that mechanically off
        # the draft's AST and its output must pass the SAME acceptance gate below.)
        _rendered = ""
        try:
            from .dagster_renderer import describe_rejection, render_dagster_module

            _draft_path = ""
            for _cand in (
                os.path.join(root, "workspace", slug, "scraper_draft.py"),
                os.path.join(root, "scrapers", slug, "scraper.py"),
            ):
                if os.path.isfile(_cand):
                    _draft_path = _cand
                    break
            if _draft_path:
                _template = ""
                _tpl_path = os.path.join(root, "templates", "dagster_template.py")
                if os.path.isfile(_tpl_path):
                    with open(_tpl_path, "r", encoding="utf-8", errors="replace") as _tf:
                        _template = _tf.read()
                _report: dict[str, Any] = {}
                _rendered = render_dagster_module(
                    _draft_path, _template,
                    {
                        "site_slug": slug,
                        "site_url": state.get("url", ""),
                        "site_name": state.get("site_name", ""),
                        "input_mode": state.get("input_mode", ""),
                        "job_id": job_id,
                    },
                    _report,
                )
                if _rendered:
                    logger.info(
                        "_invoke_dagster_converter: deterministic render OK "
                        "(shape=%s) — LLM skipped (job %s)",
                        _report.get("shape"), job_id,
                    )
                    os.makedirs(os.path.dirname(ws_dagster), exist_ok=True)
                    with open(ws_dagster, "w", encoding="utf-8") as _f:
                        _f.write(_rendered)
                else:
                    logger.info(
                        "_invoke_dagster_converter: renderer declined (%s) — "
                        "LLM fallback (job %s)",
                        describe_rejection(_report), job_id,
                    )
        except Exception as _exc:
            _rendered = ""
            logger.warning(
                "_invoke_dagster_converter: renderer attempt failed — LLM "
                "fallback (job %s): %s", job_id, _exc,
            )

        if not _rendered:
            messages = build_dagster_converter_message(state)
            _log_agent_context(state, "dagster-converter", messages)
            agent = create_dagster_converter(site_slug=slug)
            # T0.2: wall-clock cap — this was the other raw unbounded invoke
            # (job 302: 34 LLM calls, 7m05s of wall with no backstop).
            result = _invoke_agent_with_timeout(
                agent, messages, _agent_config(config, "dagster_converter"),
                "dagster_converter", job_id,
            )
            _persist_agent_logs(state, result, "dagster-converter", config)

        # Acceptance gate (runs for BOTH the rendered and the LLM-written file)
        if os.path.isfile(ws_dagster):
            # Syntax + import-binding check (P0-5: ast.parse alone misses
            # commented-out imports and undefined base classes — the file
            # "syntax OK"s but NameErrors at import time).
            try:
                import ast
                with open(ws_dagster, "r") as f:
                    _src = f.read()
                _tree = ast.parse(_src)
                # Collect names bound by imports/classdefs/assignments at module scope.
                _bound = set()
                for _node in ast.iter_child_nodes(_tree):
                    if isinstance(_node, (ast.Import, ast.ImportFrom)):
                        for _alias in _node.names:
                            _bound.add(_alias.asname or _alias.name.split(".")[0])
                    elif isinstance(_node, ast.ClassDef):
                        _bound.add(_node.name)
                    elif isinstance(_node, ast.Assign):
                        for _t in _node.targets:
                            if isinstance(_t, ast.Name):
                                _bound.add(_t.id)
                # Check that every base class referenced in a ClassDef is bound
                # (not commented out). Catches `class X(BaseTlsScraper):` when
                # `# from dagster_scraper_base import BaseTlsScraper` is commented.
                _unresolved = []
                for _node in ast.walk(_tree):
                    if isinstance(_node, ast.ClassDef):
                        for _base in _node.bases:
                            if isinstance(_base, ast.Name) and _base.id not in _bound:
                                _unresolved.append(f"class {_node.name}: base '{_base.id}' not imported")
                if _unresolved:
                    logger.warning(
                        "_invoke_dagster_converter: %s_dagster.py has unresolved "
                        "names (won't import): %s — file NOT copied to scrapers/",
                        slug, "; ".join(_unresolved[:3]),
                    )
                else:
                    # Per-job copy to the File Master always; promote to production
                    # {slug}_dagster.py only on success (mirrors _promote_scraper —
                    # don't clobber a good dagster file with a failed job's output).
                    import src.artifacts as artifacts

                    with open(ws_dagster, "rb") as _f:
                        _dagster_bytes = _f.read()
                    per_job_key = artifacts.scrapers_key(slug, "jobs", f"dagster-{job_id}.py")
                    artifacts.write(per_job_key, _dagster_bytes)
                    if state.get("execution_status") == "SUCCESS":
                        prod_key = artifacts.scrapers_key(slug, f"{slug}_dagster.py")
                        artifacts.write(prod_key, artifacts.read(per_job_key))
                        logger.info(
                            "_invoke_dagster_converter: SUCCESS → promoted %s_dagster.py (job %s)",
                            slug, job_id,
                        )
                    else:
                        logger.info(
                            "_invoke_dagster_converter: non-SUCCESS → %s_dagster.py "
                            "left as-is, per-job copy at jobs/dagster-%s.py",
                            slug, job_id,
                        )
                    _notify_phase(job_id, "dagster_converter", "done")
                    return {"messages": [], "dagster_path": per_job_key}
            except SyntaxError as exc:
                logger.warning(
                    "_invoke_dagster_converter: %s_dagster.py has syntax error: %s",
                    slug, exc,
                )
        else:
            logger.warning(
                "_invoke_dagster_converter: agent did not write %s_dagster.py",
                slug,
            )
        _notify_phase(job_id, "dagster_converter", "failed")
        return {"messages": []}
    except Exception as exc:
        logger.warning("_invoke_dagster_converter: failed (non-blocking): %s", exc)
        _notify_phase(job_id, "dagster_converter", "failed")
        return {"messages": []}
    finally:
        clear_tool_context()


def _invoke_store_job_listings(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any]:
    """Post-completion: ingest job listings from the output JSON into the DB.

    Deterministic (not an LLM agent). Reads the output file, parses the jobs array,
    and inserts/updates JobListing rows. Only runs for job content types.
    Non-blocking — failure is logged but doesn't affect the job.
    """
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "store_job_listings", "running")
    page_type = state.get("page_type", "")
    exec_status = state.get("execution_status", "")
    output_file = state.get("output_file", "")
    slug = state.get("site_slug", "")
    job_id = state.get("job_id", 0)

    # Guard: only for job content types + successful execution + output exists
    if "job" not in page_type.lower():
        _notify_phase(job_id, "store_job_listings", "skipped")
        return {"messages": []}
    if exec_status != "SUCCESS":
        _notify_phase(job_id, "store_job_listings", "skipped")
        return {"messages": []}
    if not output_file:
        # Try to find the latest output file (newest mtime across workspace
        # and scrapers — see _find_newest_output for why mtime, not name-sorted).
        try:
            root = _get_project_root()
            site_folder = os.path.join(root, "scrapers", slug)
            workspace_folder = os.path.join(root, "workspace", slug)
            output_file = _find_newest_output(workspace_folder, site_folder, slug=slug)
        except Exception:
            output_file = ""
    if not output_file or not os.path.isfile(output_file):
        logger.info("_invoke_store_job_listings: no output file for %s — skipping", slug)
        _notify_phase(job_id, "store_job_listings", "skipped")
        return {"messages": []}

    try:
        import json as _json
        from datetime import datetime as _dt

        with open(output_file, "r", encoding="utf-8") as f:
            data = _json.load(f)

        # Get the output key (usually "jobs")
        ct_config = state.get("content_type_config") or {}
        output_key = ct_config.get("output_key", "jobs")
        items = data.get(output_key) or data.get("jobs") or data.get("products") or []
        if not items:
            logger.info("_invoke_store_job_listings: no items in %s — skipping", output_file)
            _notify_phase(job_id, "store_job_listings", "skipped")
            return {"messages": []}

        # Known fields → model columns; everything else → extra_data
        _KNOWN_FIELDS = {
            "title", "company", "location", "description", "salary",
            "job_type", "employment_type", "posted_date", "valid_through",
            "url", "job_id", "src_url", "remarks",
        }

        # Resolve the Site FK
        from scraper.models import JobListing, Site as SiteModel
        site_obj = None
        if slug:
            site_obj = SiteModel.objects.filter(slug=slug).first()

        site_name = state.get("site_name", "") or (site_obj.name if site_obj else "") or slug
        scrape_job_ref = None
        if job_id:
            from scraper.models import ScrapeJob
            scrape_job_ref = ScrapeJob.objects.filter(id=job_id).first()

        created_count = 0
        updated_count = 0
        for item in items:
            if not isinstance(item, dict):
                continue

            # Parse posted_date
            posted_raw = item.get("posted_date") or item.get("date_posted") or ""
            posted_date = None
            if posted_raw:
                for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%d/%m/%Y"):
                    try:
                        posted_date = _dt.strptime(str(posted_raw)[:19], fmt).date()
                        break
                    except ValueError:
                        continue

            valid_raw = item.get("valid_through") or ""
            valid_through = None
            if valid_raw:
                for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        valid_through = _dt.strptime(str(valid_raw)[:19], fmt).date()
                        break
                    except ValueError:
                        continue

            url = item.get("url", "")
            job_src_id = item.get("job_id") or item.get("job_number") or ""

            # Extra data: any field not in the known set
            extra = {k: v for k, v in item.items() if k not in _KNOWN_FIELDS}

            # Dedup key: (site_slug, url) — or (site_slug, job_source_id) if no url
            # P0-13: assess posted_date reliability. Sites that dynamically set
            # datePosted to "today" produce fabricated freshness. Don't overwrite
            # a prior reliable date with an unreliable one on update.
            _scrape_date = _dt.now().date()
            _date_str, _reliable, _reason = (None, True, "ok")
            if posted_raw:
                try:
                    from src.job_fields import assess_date_reliability
                    _date_str, _reliable, _reason = assess_date_reliability(str(posted_raw), _scrape_date)
                except Exception:
                    _reliable = True  # conservative: trust the date if assessment fails

            defaults = {
                "title": (item.get("title") or "")[:500],
                "company": (item.get("company") or "")[:300],
                "location": (item.get("location") or "")[:300],
                "description": item.get("description") or "",
                "salary": (item.get("salary") or "")[:300],
                "job_type": (item.get("job_type") or "")[:100],
                "employment_type": (item.get("employment_type") or item.get("employment_type") or "")[:100],
                "date_posted_reliable": _reliable,
                "valid_through": valid_through,
                "site_name": site_name,
                "site": site_obj,
                "scrape_job": scrape_job_ref,
                "extra_data": extra,
            }

            # Dedup: prefer url, fall back to job_source_id
            # P0-13: only set posted_date when reliable (avoids overwriting
            # with a fabricated "today" on every re-scrape). When unreliable,
            # leave posted_date as-is (NULL on first create) — the dashboard
            # uses scraped_at (first_seen_at) as the freshness signal instead.
            if _reliable and posted_date:
                defaults["posted_date"] = posted_date

            if url:
                defaults["url"] = url[:1000]
                defaults["job_source_id"] = str(job_src_id)[:200]
                obj, created = JobListing.objects.update_or_create(
                    site_slug=slug, url=url[:1000], defaults=defaults
                )
            elif job_src_id:
                defaults["job_source_id"] = str(job_src_id)[:200]
                obj, created = JobListing.objects.update_or_create(
                    site_slug=slug, job_source_id=str(job_src_id)[:200], defaults=defaults
                )
            else:
                # No natural dedup key — synthesize a DETERMINISTIC one from the
                # stable fields so re-scrapes (and acks_late redeliveries in
                # Phase 3) UPDATE instead of creating duplicates. The old bare
                # .create(url="", job_source_id="", ...) produced a fresh row per
                # item per run (the locumtenens-style dup explosion).
                import hashlib as _hashlib

                _key_src = "␟".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("company") or ""),
                        str(item.get("location") or ""),
                    ]
                )
                _synth_id = "synth:" + _hashlib.sha1(
                    _key_src.encode("utf-8")
                ).hexdigest()[:24]
                defaults["job_source_id"] = _synth_id
                obj, created = JobListing.objects.update_or_create(
                    site_slug=slug, job_source_id=_synth_id, defaults=defaults
                )

            if created:
                created_count += 1
            else:
                updated_count += 1

        logger.info(
            "_invoke_store_job_listings: %d created, %d updated from %s (job %s)",
            created_count, updated_count, output_file, job_id,
        )
        _notify_phase(job_id, "store_job_listings", "done")
        return {"messages": [], "listings_stored": created_count + updated_count}
    except Exception as exc:
        logger.warning("_invoke_store_job_listings: failed (non-blocking): %s", exc)
        _notify_phase(job_id, "store_job_listings", "failed")
        return {"messages": []}


def _log_agent_context(state: ScrapeState, agent_name: str, messages: list) -> None:
    """Write the agent's initial HumanMessage as a visible [CONTEXT] log entry.

    This makes the context/summary each agent receives from previous agents
    easily visible in the UI under the agent's own log section.
    """
    job_id = state.get("job_id")
    if not job_id or not messages:
        return
    context = ""
    for msg in messages:
        if hasattr(msg, "type") and msg.type == "human":
            context = getattr(msg, "content", "")
            break
    if not context:
        return
    try:
        from scraper.models import SessionLog

        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_SYSTEM,
            agent=agent_name,
            content=f"[CONTEXT] {context[:20000]}",
            seq=seq,
        )
    except Exception:
        pass


def _persist_agent_logs(
    state: ScrapeState, result: dict, agent_name: str, config: RunnableConfig
) -> None:
    """Extract messages from agent result and persist as SessionLog rows."""
    job_id = state.get("job_id")
    if not job_id:
        return

    # [QW-6] A wall-clock abandonment produced NO messages, so the early
    # return below hid it: the job's tool-call trail just went silent for
    # 900s with zero DB record of why. Write one ToolCallLog row so the
    # abandonment is visible (and countable) in the tool-calls view.
    _err = str(result.get("_error") or "") if isinstance(result, dict) else ""
    if "wall-clock timeout" in _err or (
        str(result.get("_error_class") or "") == "WallClockTimeout"
        if isinstance(result, dict)
        else False
    ):
        try:
            from scraper.models import ToolCallLog

            ToolCallLog.objects.create(
                job_id=job_id,
                agent=agent_name,
                tool_name="wall_clock_timeout",
                tool_call_id="",
                call_seq=ToolCallLog.objects.filter(job_id=job_id).count(),
                args_summary=_err[:500],
                result_summary="agent exceeded its wall clock; thread abandoned",
            )
        except Exception as _wc_exc:
            logger.warning(
                "_persist_agent_logs: wall-clock row failed for %s: %s",
                agent_name, _wc_exc,
            )

    messages = result.get("messages", [])
    if not messages:
        # [job-82] A dead invocation from a NON-wall-clock exception (provider
        # 5xx, unclassified httpx read error, decode/validation failure)
        # previously left NO trace in the job's DB rows — the SessionLog
        # showed a healthy-looking silence and the exception surfaced only in
        # a truncated celery log line. One ToolCallLog row makes every
        # invocation-ending exception visible (and classifiable) in the
        # tool-calls view, for every agent, not just code_writer.
        _di_class = str(result.get("_error_class") or "") if isinstance(result, dict) else ""
        if _err or _di_class:
            try:
                from scraper.models import ToolCallLog

                ToolCallLog.objects.create(
                    job_id=job_id,
                    agent=agent_name,
                    tool_name="dead_invocation",
                    tool_call_id="",
                    call_seq=ToolCallLog.objects.filter(job_id=job_id).count(),
                    args_summary=(_di_class or "unknown")[:200],
                    result_summary=_err[:500] or "invocation ended with an exception and no messages",
                )
            except Exception as _di_exc:
                logger.warning(
                    "_persist_agent_logs: dead-invocation row failed for %s: %s",
                    agent_name, _di_exc,
                )
        return

    # Observability (job 9-vs-10 lesson): persist the RESOLVED model per invoke
    # so "did two runs use the same model?" is answerable from the DB. One row.
    try:
        _model_used = getattr(result, "model_used", None) or ""
        if not _model_used:
            # langchain result metadata may carry it per-message
            for _m in messages:
                _meta = getattr(_m, "response_metadata", None) or {}
                _model_used = _meta.get("model_name") or _model_used
                if _model_used:
                    break
        if _model_used:
            from scraper.models import SessionLog as _SL

            _seq = _SL.objects.filter(job_id=job_id).count()
            _SL.objects.create(
                job_id=job_id,
                role=_SL.ROLE_SYSTEM,
                agent=agent_name,
                content=f"[MODEL] {_model_used}",
                seq=_seq,
            )
    except Exception:
        pass

    try:
        from scraper.models import SessionLog, ToolCallLog

        seq_start = SessionLog.objects.filter(job_id=job_id).count()
        for i, msg in enumerate(messages):
            if hasattr(msg, "type"):
                role = msg.type
                content = getattr(msg, "content", "")
                if not content:
                    continue

                if role == "ai":
                    log_role = SessionLog.ROLE_ASSISTANT
                elif role == "tool":
                    log_role = SessionLog.ROLE_TOOL
                else:
                    log_role = SessionLog.ROLE_USER

                SessionLog.objects.create(
                    job_id=job_id,
                    role=log_role,
                    agent=agent_name,
                    content=str(content)[:20000],
                    seq=seq_start + i,
                )
        logger.info(
            "_persist_agent_logs: %d messages for %s (job %s)",
            len(messages),
            agent_name,
            job_id,
        )
    except Exception as exc:
        logger.warning("Failed to persist logs for %s: %s", agent_name, exc)

    try:
        from scraper.models import ToolCallLog

        call_seq_start = ToolCallLog.objects.filter(job_id=job_id).count()
        pending_calls: dict[str, Any] = {}

        for msg in messages:
            if getattr(msg, "type", "") == "ai":
                tool_calls = getattr(msg, "tool_calls", None)
                if not tool_calls:
                    continue
                for tc in tool_calls:
                    tc_id = tc.get("id", "")
                    args_summary = _summarize_tool_args(
                        tc.get("name", ""), tc.get("args", {})
                    )
                    tcl = ToolCallLog.objects.create(
                        job_id=job_id,
                        agent=agent_name,
                        tool_name=tc.get("name", "unknown"),
                        tool_call_id=tc_id,
                        call_seq=call_seq_start,
                        args_summary=args_summary,
                    )
                    if tc_id:
                        pending_calls[tc_id] = tcl
                    call_seq_start += 1

        for msg in messages:
            if getattr(msg, "type", "") == "tool":
                tc_id = getattr(msg, "tool_call_id", "")
                if tc_id and tc_id in pending_calls:
                    result_text = str(getattr(msg, "content", ""))[:500]
                    result_summary = _clean_result_summary(result_text)
                    pending_calls[tc_id].result_summary = result_summary
                    pending_calls[tc_id].save(update_fields=["result_summary"])

        tool_count = len(pending_calls)
        if tool_count:
            logger.info(
                "_persist_agent_logs: %d tool calls for %s (job %s)",
                tool_count,
                agent_name,
                job_id,
            )
    except Exception as exc:
        logger.warning("Failed to persist tool calls for %s: %s", agent_name, exc)


def _persist_probe_summary(
    job_id: int, url: str, probe_result: dict, raw_data: dict
) -> None:
    """Persist check_accessibility probe result as a SessionLog entry."""
    if not job_id:
        return
    try:
        from scraper.models import SessionLog

        conn = probe_result.get("connectivity", {})
        status_code = raw_data.get("status_code", "?")
        method = conn.get("method_that_worked", "unknown")
        proxy_tier = conn.get("proxy_tier", "none")
        needs_browser = conn.get("js_rendering_needed", "?")
        anti_bot = conn.get("anti_bot_detected", False)
        http_method = conn.get("http_method")
        browser_method = conn.get("browser_method")

        summary_lines = [
            f"Probe result for {url[:80]}",
            f"  Method: {method} (proxy: {proxy_tier})",
            f"  HTTP method: {http_method or 'none'}",
            f"  Browser method: {browser_method or 'none'}",
            f"  Status code: {status_code}",
            f"  JS rendering needed: {needs_browser}",
            f"  Anti-bot detected: {anti_bot}",
        ]
        if raw_data.get("captcha_type"):
            summary_lines.append(f"  Captcha type: {raw_data['captcha_type']}")
        if raw_data.get("methods_tried"):
            summary_lines.append(
                f"  Methods tried: {', '.join(raw_data['methods_tried'])}"
            )

        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_SYSTEM,
            agent="check_accessibility",
            content="\n".join(summary_lines),
            seq=seq,
        )
    except Exception as exc:
        logger.warning("Failed to persist probe summary for job %s: %s", job_id, exc)


# ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
# ARCHIVED def _route_after_navigation_explore(state: ScrapeState) -> str:
# ARCHIVED     """Route after navigation_explore.
# ARCHIVED
# ARCHIVED     Normally proceeds to navigation_synthesize.  If navigate_explore
# ARCHIVED     flagged playwright_unavailable, the node already issued a
# ARCHIVED     Command(goto="human_approval") internally — this function only
# ARCHIVED     handles the case where the state carries the flag without a Command
# ARCHIVED     (defensive fallback).
# ARCHIVED     """
# ARCHIVED     if state.get("playwright_unavailable"):
# ARCHIVED         logger.info("route_after_navigate_explore: routing to human_approval")
# ARCHIVED         return "human_approval"
# ARCHIVED     return "navigation_synthesize"
# ═══ END ARCHIVED ═══


# ═══════════════════════════════════════════════════════════════════════════
# Conditional edge functions
# ═══════════════════════════════════════════════════════════════════════════


def route_from_human_approval(state: ScrapeState) -> str:
    """Route the graph after human_approval resolves.

    Handles both legacy ``{"choice": "Cancel"}`` and new
    ``{"decision": "reject", "feedback": "..."}`` format.
    """
    reason = state.get("interrupt_reason", "")
    response = state.get("human_response")

    if isinstance(response, dict):
        choice = response.get("decision", response.get("choice", ""))
        label = response.get("label", choice)
    else:
        choice = str(response) if response else ""
        label = choice

    cancel_values = {"Cancel", "Abort", "reject", "Cancel entire job"}
    if choice in cancel_values:
        logger.info("route_from_human_approval: user cancelled (%s)", reason)
        return "__end__"

    # Handle testing_exhausted BEFORE the approve_values override,
    # because "Provide feedback for final retry" has decision="approve"
    # and would get its label overwritten to "Continue anyway".
    if reason == "testing_exhausted":
        feedback = state.get("human_feedback", "")
        if label == "Provide feedback for final retry":
            if not feedback:
                logger.warning(
                    "route_from_human_approval: testing_exhausted -> final retry "
                    "requested but no feedback provided, proceeding to field_confirmation"
                )
                return "field_confirmation"
            logger.info(
                "route_from_human_approval: testing_exhausted -> scraper_analyzer "
                "(FINAL retry with user feedback: %s)",
                feedback[:200],
            )
            # F18: the sentinel (test_retry_count=FINAL_RETRY_SENTINEL) is set
            # by the human_approval node itself — a path fn may only return a
            # plain node name (Command(update=...) here raised TypeError).
            return "scraper_analyzer"
        logger.info(
            "route_from_human_approval: testing_exhausted -> field_confirmation"
        )
        return "field_confirmation"

    # low_coverage (validate_coverage gate): honor "Retry content analysis" BEFORE
    # the approve_values override clobbers its label to "Continue anyway" (the
    # retry option is decision-type approve, so without this it silently proceeds).
    # Retry -> product_analyzer (re-map fields); anything else -> proceed.
    if reason == "low_coverage":
        if "retry" in (label or "").lower() or "retry" in (choice or "").lower():
            logger.info("route_from_human_approval: low_coverage -> retry product_analyzer")
            return "product_analyzer"
        logger.info("route_from_human_approval: low_coverage -> proceed to scraper_analyzer")
        return "scraper_analyzer"

    # coverage_exhausted (validate_coverage gate, after MAX_COVERAGE_RETRIES):
    # "Continue anyway" -> proceed with partial coverage; "Abort"/cancel -> end.
    if reason == "coverage_exhausted":
        if choice in cancel_values:
            logger.info("route_from_human_approval: coverage_exhausted -> abort (END)")
            return "__end__"
        logger.info("route_from_human_approval: coverage_exhausted -> proceed to scraper_analyzer")
        return "scraper_analyzer"

    # code_tester_wall_clock (job-81 N-C): two consecutive DEAD code_tester
    # invocations left the draft unjudged. MUST be handled BEFORE the
    # approve_values clobber below (same reason as code_writer_failed).
    # "Retry testing" → re-run the test on the SAME draft (it was never
    # judged); "Execute anyway" → proceed as if the test passed (human's
    # call); anything else ends the job.
    if reason == "code_tester_wall_clock":
        if "execute" in (label or "").lower():
            logger.info(
                "route_from_human_approval: code_tester_wall_clock -> "
                "field_confirmation (execute unjudged draft on human's call)"
            )
            return "field_confirmation"
        if "retry" in (label or "").lower():
            logger.info(
                "route_from_human_approval: code_tester_wall_clock -> code_tester "
                "(re-test the unjudged draft)"
            )
            return "code_tester"
        logger.info(
            "route_from_human_approval: code_tester_wall_clock -> end "
            "(draft never judged)"
        )
        return "__end__"

    # code_writer_failed (T0.4): two consecutive DEAD code_writer invocations.
    # MUST be handled BEFORE the approve_values clobber below — the retry
    # option is decision-type approve, so its label would be overwritten to
    # "Continue anyway" and the retry would end the job instead.
    # Retry -> scraper_analyzer (re-derive strategy + regenerate); anything
    # non-retry ends the job (the default would have routed to cleanup and
    # "finalized" a job with no draft at all).
    if reason == "code_writer_failed":
        if "retry" in (label or "").lower():
            logger.info(
                "route_from_human_approval: code_writer_failed -> retry scraper_analyzer"
            )
            return "scraper_analyzer"
        logger.info("route_from_human_approval: code_writer_failed -> end (no draft)")
        return "__end__"

    approve_values = {"approve", "yes", "ok", "continue", "continue anyway", "proceed"}
    if choice.lower() in approve_values:
        choice = "continue"
        label = "Continue anyway"

    routing: dict[str, str] = {
        "re_scrape": "setup_workspace",
        "retry_failed": "setup_workspace",
        "choose_mechanism": "code_writer",
        "low_coverage": "code_writer",
        "validation_failed": "field_confirmation",
        "reanalyze_exhausted": "run_execution",
        # pre_execution node was removed (Wave 2 Cut 2); keep this entry as a
        # safety net so any in-flight job resuming a legacy pre_execution
        # interrupt routes straight to run_execution (the merged behaviour).
        "pre_execution": "run_execution",
        "field_confirmation": "run_execution",
        "playwright_unavailable": "browser_traverse",
        "review": "run_execution",
    }

    if reason == "low_confidence":
        if "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: low_confidence -> continue to product_analyzer"
            )
            return "product_analyzer"
        logger.info(
            "route_from_human_approval: low_confidence -> retry setup_workspace"
        )
        return "setup_workspace"

    if reason in (
        "budget_exhausted_site",
        "budget_exhausted_product",
        "budget_exhausted_navigation",
    ):
        if "retry" in (label or "").lower() or "higher budget" in (label or "").lower():
            target = (
                "site_analyzer"
                if "site" in reason
                else (
                    "browser_traverse"
                    if "navigation" in reason
                    else "product_analyzer"
                )
            )
            logger.info(
                "route_from_human_approval: %s -> retry %s with higher budget",
                reason,
                target,
            )
            return target
        if "continue" in (label or "").lower():
            logger.info("route_from_human_approval: %s -> continue anyway", reason)
            if reason in ("budget_exhausted_site", "budget_exhausted_navigation"):
                return "scraper_analyzer"
            return "normalize_fields"
        logger.info("route_from_human_approval: %s -> cancelled", reason)
        return "__end__"

    if reason == "missing_artifact_site":
        if "redo" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_site -> redo site_analyzer"
            )
            return "site_analyzer"
        if "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_site -> continue without"
            )
            return "update_tracker_analysis"
        logger.info("route_from_human_approval: missing_artifact_site -> cancelled")
        return "__end__"

    if reason == "missing_artifact_product":
        if "redo" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_product -> redo product_analyzer"
            )
            return "product_analyzer"
        if "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_product -> continue without"
            )
            return "scraper_analyzer"
        logger.info("route_from_human_approval: missing_artifact_product -> cancelled")
        return "__end__"

    if reason == "playwright_unavailable":
        if "retry" in (label or "").lower() or "playwright" in (label or "").lower():
            logger.info(
                "route_from_human_approval: playwright_unavailable -> retry browser_traverse"
            )
            return "browser_traverse"
        if "probe_html" in (label or "").lower() or "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: playwright_unavailable -> proceed with probe_html"
            )
            return "product_analyzer"
        logger.info("route_from_human_approval: playwright_unavailable -> cancelled")
        return "__end__"

    next_node = routing.get(reason, "cleanup")
    logger.info("route_from_human_approval: reason=%s -> %s", reason, next_node)
    return next_node


# ═══════════════════════════════════════════════════════════════════════════
# Graph builder
# ═══════════════════════════════════════════════════════════════════════════


def build_scrape_graph(
    checkpointer: Optional[Any] = None,
) -> CompiledStateGraph:
    """Build and compile the full scraping StateGraph.

    The graph is assembled with:

    * 6 LLM agent subgraphs (site_analyzer, product_analyzer, code_writer,
      code_tester, cleanup, skill_learner)
    * 12 deterministic nodes (parse_command, check_tracker, setup_workspace,
      update_tracker_analysis, validate_analysis, validate_coverage,
      field_confirmation, run_execution, route_after_testing,
      route_after_cleanup, human_approval)
    * 3 conditional edges (check_tracker → Command-based routing,
      route_after_testing, route_after_cleanup, route_from_human_approval)

    Args:
        checkpointer: Optional LangGraph checkpointer.  When ``None``,
            ``get_checkpointer()`` is used to obtain a PostgresSaver.

    Returns:
        A compiled ``StateGraph`` ready to invoke.
    """
    if checkpointer is None:
        try:
            from .checkpointer import get_checkpointer

            checkpointer = get_checkpointer()
        except Exception as exc:
            logger.warning(
                "Could not create Postgres checkpointer, running without persistence: %s",
                exc,
            )
            checkpointer = None

    workflow = StateGraph(ScrapeState)

    # ── Add nodes (in execution order so the Mermaid diagram reads top-to-bottom) ─
    # Setup
    workflow.add_node("parse_command", parse_command)
    workflow.add_node("check_tracker", check_tracker)
    workflow.add_node("setup_workspace", setup_workspace)
    workflow.add_node("check_accessibility", check_accessibility)
    # Analysis
    workflow.add_node("site_analyzer", _invoke_site_analyzer)
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # workflow.add_node("navigation_explore", _invoke_navigation_explore)
    # workflow.add_node("navigation_agent", _invoke_navigation_agent)
    # workflow.add_node("navigation_synthesize", _invoke_navigation_synthesize)
    # ═══ END ARCHIVED ═══
    workflow.add_node("browser_traverse", _invoke_navigation_traverse)
    workflow.add_node("product_analyzer", _invoke_product_analyzer)
    workflow.add_node("update_tracker_analysis", update_tracker_analysis)
    workflow.add_node("validate_analysis", validate_analysis)
    workflow.add_node("normalize_fields", normalize_fields)
    workflow.add_node("validate_coverage", validate_coverage)
    # Generation & testing
    workflow.add_node("scraper_analyzer", _decide_strategy)
    workflow.add_node("code_writer", _invoke_code_writer)
    workflow.add_node("code_tester", _invoke_code_tester)
    workflow.add_node("field_confirmation", field_confirmation)
    # (Wave 2 Cut 2) pre_execution_approval node removed — its gate was merged
    # into field_confirmation (item-count now shown there); field_confirmation
    # routes straight to run_execution on approve.
    # Execution & post-completion
    workflow.add_node("run_execution", run_execution)
    # [job-65 phase 3a] Zero-item executions recycle through the strategy
    # ladder once before cleanup (Command-routed — no static out-edge here).
    workflow.add_node("route_after_execution", _route_after_execution)
    workflow.add_node("cleanup", _invoke_cleanup)
    workflow.add_node("nav_skill_review", _invoke_nav_skill_review)
    workflow.add_node("skill_learner", _invoke_skill_learner)
    workflow.add_node("dagster_converter", _invoke_dagster_converter)
    workflow.add_node("store_job_listings", _invoke_store_job_listings)
    # Generic human-in-the-loop handler (reached from many gates)
    workflow.add_node("human_approval", human_approval)

    # ── Wire edges ──────────────────────────────────────────────────────

    # START → parse_command → check_tracker
    workflow.add_edge(START, "parse_command")
    workflow.add_edge("parse_command", "check_tracker")

    # check_tracker uses Command-based routing internally (no conditional
    # edge needed — the node itself decides goto).
    # From check_tracker, Command goto may be: setup_workspace, human_approval, __end__

    # setup_workspace → check_accessibility (probe + captcha check)
    workflow.add_edge("setup_workspace", "check_accessibility")

    # check_accessibility uses Command-based routing (skip flags on resume,
    # or probe result on first pass). goto may be: site_analyzer,
    # validate_analysis, scraper_analyzer, code_writer, code_tester, or END.

    # F13: site_analyzer routes via Command (folded into _on_success above) —
    # NO registered out-edges. The old conditional edge unioned with the
    # budget-exhaustion Command (both destinations ran — the D6 shadow branch).

    # browser_traverse → product_analyzer (replaces the 3-node navigation pipeline).
    workflow.add_edge("browser_traverse", "product_analyzer")
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # # navigation_explore → conditional (human_approval if Playwright down, else navigation_synthesize).
    # # navigate_explore may also return Command(goto="navigation_agent") when it detects a form-driven
    # # site it can't drive deterministically (low product links + form detected) — the LLM navigation_agent
    # # then drives the form with browser tools + skills, and flows into navigation_synthesize.
    # workflow.add_conditional_edges(
    #     "navigation_explore",
    #     _route_after_navigation_explore,
    #     {
    #         "navigation_synthesize": "navigation_synthesize",
    #         "human_approval": "human_approval",
    #     },
    # )
    # workflow.add_edge("navigation_agent", "navigation_synthesize")
    # workflow.add_edge("navigation_synthesize", "product_analyzer")
    # ═══ END ARCHIVED ═══

    # update_tracker_analysis → validate_analysis
    workflow.add_edge("update_tracker_analysis", "validate_analysis")

    # validate_analysis uses Command-based routing internally.
    # From validate_analysis, Command goto may be: product_analyzer,
    # human_approval, code_writer

    # F13: product_analyzer routes via Command (happy path → normalize_fields
    # via its _on_success; budget-exhaustion/missing-artifact/remap → their own
    # gotos). The static edge unioned with those Commands (the D6 shadow
    # branch: prod 272 needed TWO cancels and ended with a misleading error;
    # the remap path's intended skip of normalize/validate never happened).
    # normalize_fields keeps its own static edge (its source never returns
    # a Command).
    workflow.add_edge("normalize_fields", "validate_coverage")

    # validate_coverage uses Command-based routing internally.
    # From validate_coverage, Command goto may be: scraper_analyzer,
    # human_approval, code_tester

    # scraper_analyzer → code_writer and code_writer → code_tester are
    # COMMAND-ROUTED now (D6 shadow branch, prod job 82): both nodes return
    # Command on every exit — the happy paths target exactly what these static
    # edges pointed at, and the failure arms (dead writer → scraper_analyzer /
    # human_approval / cleanup; strategy ladder exhausted → cleanup /
    # human_approval) previously fired only as GHOST siblings running in
    # parallel with the doomed static-edge destination, because LangGraph
    # unions a Command goto with any registered static out-edge. Mirror of the
    # product_analyzer (F13) and run_execution (job-65) precedents above.
    # (The read-only code_review phase was removed; code_tester validates
    # functionality and route_after_testing handles retries.)

    # code_tester → route_after_testing (conditional)
    workflow.add_conditional_edges(
        "code_tester",
        route_after_testing,
        {
            "field_confirmation": "field_confirmation",
            "scraper_analyzer": "scraper_analyzer",
            "product_analyzer": "product_analyzer",
            "code_writer": "code_writer",
            # [QW-3/A1] same-draft re-test for unproven-coverage failures
            # (429 / throttle / transient render) — capped by
            # state.test_retest_count, maintained inside _invoke_code_tester.
            "code_tester": "code_tester",
            "human_approval": "human_approval",
            "cleanup": "cleanup",
        },
    )

    # field_confirmation uses Command-based routing internally (goto is either
    # run_execution on approve, or product_analyzer on reject for re-analysis).
    # No conditional edge needed — the Command decides.
    # (Wave 2 Cut 2: the old pre_execution_approval hop in between was removed.)

    # run_execution → route_after_execution (job-65 phase 3a: zero-item
    # executions recycle through the strategy ladder; everything else routes
    # to cleanup via the node's Command — NO static out-edge, the D6 lesson:
    # a static edge unioned with Command routing runs BOTH destinations).
    workflow.add_edge("run_execution", "route_after_execution")

    # cleanup → nav_skill_review (capture navigation learnings post-scrape)
    workflow.add_edge("cleanup", "nav_skill_review")

    # nav_skill_review → skill_learner (or END if skill_learner skipped)
    workflow.add_edge("nav_skill_review", "skill_learner")

    # skill_learner → dagster_converter → store_job_listings → END
    workflow.add_edge("skill_learner", "dagster_converter")
    workflow.add_edge("dagster_converter", "store_job_listings")
    workflow.add_edge("store_job_listings", END)

    # human_approval → conditional resume routing
    workflow.add_conditional_edges(
        "human_approval",
        route_from_human_approval,
        {
            "setup_workspace": "setup_workspace",
            "scraper_analyzer": "scraper_analyzer",
            "code_writer": "code_writer",
            "code_tester": "code_tester",
            "field_confirmation": "field_confirmation",
            "run_execution": "run_execution",
            "skill_learner": "skill_learner",
            "product_analyzer": "product_analyzer",
            "browser_traverse": "browser_traverse",
            "nav_skill_review": "nav_skill_review",
            "site_analyzer": "site_analyzer",
            "update_tracker_analysis": "update_tracker_analysis",
            "normalize_fields": "normalize_fields",
            "cleanup": "cleanup",
            "__end__": END,
        },
    )

    # ── Compile ─────────────────────────────────────────────────────────
    compiled = workflow.compile(checkpointer=checkpointer)

    logger.info("Scrape graph compiled with %d nodes", len(workflow.nodes))
    for node_name in workflow.nodes:
        logger.info("  node: %s", node_name)

    return compiled


# ═══════════════════════════════════════════════════════════════════════════
# Edge helper functions (not exposed as nodes)
# ═══════════════════════════════════════════════════════════════════════════


def _route_after_site_analyzer(state: ScrapeState) -> str:
    """Route after site_analyzer based on input_mode.

    - navigation/list_page/search_term → browser_traverse (browser-driven navigation)
    - url_list → update_tracker_analysis (existing product/content analysis flow)
    """
    input_mode = state.get("input_mode", "url_list")
    if input_mode in ("navigation", "list_page", "search_term"):
        logger.info(
            "_route_after_site_analyzer: input_mode=%s → browser_traverse",
            input_mode,
        )
        return "browser_traverse"
    logger.info(
        "_route_after_site_analyzer: input_mode=%s → update_tracker_analysis",
        input_mode,
    )
    return "update_tracker_analysis"


__all__ = ["build_scrape_graph"]
