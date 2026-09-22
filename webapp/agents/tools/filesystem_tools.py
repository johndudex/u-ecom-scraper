"""Filesystem tools for LangGraph agent nodes.

All tools enforce a project-root sandbox — paths that resolve outside
``project_root`` are rejected with a clear error message.

Tools use the ``@tool`` decorator from ``langchain_core.tools`` so they are
automatically converted into LangChain ``BaseTool`` instances with correct
schemas for the LLM.
"""

import fnmatch
import glob
import json
import logging
import os
import re
import subprocess

from langchain_core.tools import tool

from agents.draft_safety import draft_call_violation

logger = logging.getLogger(__name__)

# F2 (artifact-corruption): what write_file/edit_file append to the tool result
# when a .json target could not be parsed even leniently. The bytes are still
# written — the phase-exit repair pass (graph._fix_json_artifact) owns salvage
# — but the agent and the logs both see it. The WARNING log line is the
# measurement signal for whether a corrective-refusal layer is ever needed.
_JSON_WARN_NOTE = (
    "NOTE: content written but is not valid JSON (strict or lenient parse "
    "failed) — the repair pass will attempt salvage on read"
)


# [wave-22 B1] F821 draft gate. The writer's dominant failure mode (337 class)
# is a module-level NameError that only surfaces at tester py_compile — one
# full test cycle later. The gate runs REAL ruff (a vendored scope checker was
# critiqued and rejected: hand-rolled checkers false-flagged templates) with
# F821-ONLY selection: drafts legitimately carry unused imports (F401) and
# locals (F841), and flagging those would reject healthy work.
_F821_RUFF_TIMEOUT = 10  # seconds — a linter must never hang a tool call
_F821_MAX_FINDINGS = 5
# [wave-25 W25-a] Untargeted reads of large files return head+tail only —
# the SNIPPED notice directs the agent to search_content + targeted windows.
_LARGE_READ_HEAD = 20_000
_LARGE_READ_TAIL = 20_000
_F821_NON_FIX_NOTE = (
    "define the name before first use — a later def does not fix a "
    "module-level call, and `X if X in dir()` is not a fix"
)
# [wave-25 W25-d] Reject-turn economics: a rejected write leaves the file
# unchanged and costs the whole turn. Prod job 404 died mid-fix right after a
# rejection because the writer spent its remaining steps RE-READING the draft
# instead of repairing. The rejection text is the one place the model is
# guaranteed to read — put the repair contract there.
_F821_REPAIR_CONTRACT = (
    "After a rejection: re-issue the write immediately with the name "
    "defined — do NOT re-read the draft and do not re-verify with other "
    "tools; a rejected write costs the whole turn, and the steps you save "
    "are the fix."
)


def _f821_rejections(path: str, content: str) -> str:
    """Run the draft write gates over content; "" (gate open) or a rejection.

    Two gates, one seam (every code_writer write_file/edit_file call site goes
    through here):

    1. ruff F821 — an undefined name is the more basic defect and keeps
       priority: the call gate below is only reached once this one is clean.
    2. [wave-40 T7] ``draft_call_violation`` — helper CALL signatures. The F821
       check binds no arguments, so the prod 760/791/762 class (unexpected
       kwarg, multiple values for argument, non-compiling regex literal) sailed
       through both this gate and the compile gate and crashed at execution
       hours later.

    Falls OPEN on any checker malfunction (binary missing, timeout, crash):
    a broken linter must never block a write — ``_fix_scraper_syntax`` at the
    phase boundary stays the authoritative backstop. A dead F821 checker
    disarms both gates on that write (gate 2 rides gate 1's clean path);
    ``draft_call_violation`` itself falls open on anything it cannot judge, so
    a gate bug never blocks a draft here. ``SCRAPER_DRAFT_CALL_GATE=0``
    disarms gate 2 only.
    """
    try:
        proc = subprocess.run(
            [
                "ruff", "check", "--isolated", "--select", "F821",
                "--no-cache", "--stdin-filename", path, "-",
            ],
            input=content,
            capture_output=True,
            text=True,
            timeout=_F821_RUFF_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("F821 gate: checker unavailable (%s) — gate falls open", exc)
        return ""
    findings = [
        ln.strip() for ln in (proc.stdout or "").splitlines() if "F821" in ln
    ]
    if not findings:
        # [wave-40 T7] helper CALL-signature gate — same rejection shape the
        # F821 check returns (the tool result IS the rejection; nothing is
        # written), guarded by its own kill switch.
        if os.getenv("SCRAPER_DRAFT_CALL_GATE", "1") != "0":
            _call = draft_call_violation(content)
            if _call:
                return _call
        return ""
    shown = findings[:_F821_MAX_FINDINGS]
    more = len(findings) - len(shown)
    return (
        "REJECTED — NOT applied; file unchanged. Undefined name(s): "
        + "; ".join(shown)
        + (f" (and {more} more)" if more > 0 else "")
        + f". {_F821_NON_FIX_NOTE}. {_F821_REPAIR_CONTRACT}"
    )


def _strip_json_fence(content: str) -> str:
    """Strip a leading ```json fence (and trailing ```) if present.

    C5 (fences/prose around JSON) — the model sometimes wraps the artifact in a
    markdown fence, which makes the file unparseable on read. Stripping is only
    attempted when the text starts with a fence opener, so ordinary JSON that
    happens to contain backticks inside strings is never touched.
    """
    text = content.lstrip("﻿ \t\r\n")
    m = re.match(r"^```(?:json|JSON)?\s*\n?", text)
    if not m:
        return content
    text = text[m.end():]
    # drop the closing fence if it is the last non-whitespace content
    text = re.sub(r"\n?```\s*$", "", text)
    return text


def sanitize_json_content(content: str) -> tuple[str, bool, str | None]:
    """Validate/normalize *content* destined for a .json path.

    Returns ``(canonical_content, is_valid, error)``:

    * ``is_valid=True``  → ``canonical_content`` is a canonical re-dump
      (``indent=1``) of the parsed value. A leading ```json fence is stripped
      first, and a strict parse is tried before the lenient one, so the
      canonical output is always STRICTLY valid JSON regardless of which parse
      accepted the input.
    * ``is_valid=False`` → ``canonical_content`` is the input unchanged and
      ``error`` carries the strict-parse failure for logging. Callers write the
      raw bytes anyway (the repair pass owns salvage) and surface the warning.
    """
    text = _strip_json_fence(content)
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, indent=1, ensure_ascii=False), True, None
    except json.JSONDecodeError as strict_err:
        error = f"{strict_err.msg} at line {strict_err.lineno} column {strict_err.colno} (char {strict_err.pos})"
    try:
        # strict=False legalizes literal control characters inside strings
        # (the priceline class: real 0x0A bytes in a prose field). The re-dump
        # escapes them, so the output is never looser than strict JSON.
        parsed = json.loads(text, strict=False)
        logger.info(
            "write guard: canonicalized .json content (strict parse failed, "
            "lenient parse succeeded — control characters escaped): %s",
            error,
        )
        return json.dumps(parsed, indent=1, ensure_ascii=False), True, None
    except json.JSONDecodeError as lenient_err:
        error = (
            f"{lenient_err.msg} at line {lenient_err.lineno} column "
            f"{lenient_err.colno} (char {lenient_err.pos})"
        )
        return content, False, error


def _repair_json_text_in_memory(text: str) -> tuple[str | None, str]:
    """Run the deterministic artifact-repair ladder on *text* without touching
    disk. Delegates to ``agents.graph.repair_json_text`` (imported lazily —
    importing graph at module scope would be circular and drag Django in).
    """
    try:
        from agents.graph import repair_json_text
    except Exception as exc:  # pragma: no cover - import env problem
        logger.warning("guard_json_bytes: repair ladder unavailable: %s", exc)
        return None, ""
    try:
        return repair_json_text(text)
    except Exception as exc:
        logger.warning("guard_json_bytes: repair ladder errored: %s", exc)
        return None, ""


def guard_json_bytes(raw: bytes) -> tuple[bytes | None, str]:
    """M4 copy-path guard: validate/repair .json bytes crossing a byte-copy
    boundary (workspace → File Master, or FM → workspace on re-hydration).

    Returns ``(bytes_to_store, note)``:

    * valid strict JSON        → the ORIGINAL bytes, byte-identical (a valid
      artifact must never be reformatted by a copy path — stable diffs).
    * only control chars (C1)  → canonical redump built from the PARSED value.
    * otherwise unparseable    → the deterministic repair runs in-memory and
      the repaired bytes are returned.
    * unrepairable             → ``(None, note)`` so the caller can SKIP the
      copy instead of propagating corrupt bytes.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"not valid UTF-8 ({exc})"

    # strict parse first: a strictly-valid artifact passes through UNTOUCHED.
    try:
        json.loads(text)
        return raw, ""
    except json.JSONDecodeError as strict_err:
        err = strict_err

    # C1: control chars only — lenient parse, redump canonically from the
    # PARSED value (never a raw-byte copy of lenient-accepted bytes).
    try:
        parsed = json.loads(text, strict=False)
        return (
            json.dumps(parsed, indent=1, ensure_ascii=False).encode("utf-8"),
            f"canonicalized on copy ({err.msg} at char {err.pos})",
        )
    except json.JSONDecodeError:
        pass

    # unparseable even leniently → run the deterministic repair in-memory
    repaired, note = _repair_json_text_in_memory(text)
    if repaired is not None:
        return repaired.encode("utf-8"), note
    return None, f"unrepairable ({err.msg} at char {err.pos})"


def _resolve_project_root(project_root: str | None = None) -> str:
    """Return the effective project root directory."""
    if project_root:
        return os.path.abspath(project_root)
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _enforce_root(path: str, root: str) -> str:
    """Resolve *path* to absolute and verify it is inside *root*.

    Relative paths are resolved against *root*, not the current working
    directory.  Returns the resolved absolute path.

    Raises:
        ValueError: If the resolved path escapes the project root.
    """
    root_abs = os.path.abspath(root)
    if os.path.isabs(path):
        resolved = os.path.abspath(path)
    else:
        resolved = os.path.abspath(os.path.join(root_abs, path))
    if not resolved.startswith(root_abs + os.sep) and resolved != root_abs:
        raise ValueError(
            f"Path '{path}' resolves to '{resolved}' which is outside "
            f"the project root '{root_abs}'"
        )
    return resolved


def _enforce_not_skills(path: str, root: str) -> str:
    """Write-guard: .opencode/skills is File-Master territory.

    Skills live in the FM (skills/ namespace, typed learn_skill tool); a
    local write here would (a) land in the ephemeral container and be lost,
    or (b) dirty the git seed copy. Checked on the RESOLVED path so tricks
    like ``.//opencode/skills`` or ``subdir/../.opencode`` can't slip through.
    Reads remain allowed (load_skill itself reads the image fallback).
    """
    resolved = _enforce_root(path, root)
    skills_dir = os.path.join(os.path.abspath(root), ".opencode", "skills")
    if resolved == skills_dir or resolved.startswith(skills_dir + os.sep):
        raise ValueError(
            "Direct writes to .opencode/skills are disabled — skills persist in "
            "the File Master. Use the learn_skill / create_new_skill tools."
        )
    return resolved


def get_filesystem_tools(
    project_root: str | None = None,
    workspace_scope: str | None = None,
    syntax_gate: bool = False,
) -> list:
    """Return all filesystem tools with sandboxing configured.

    Args:
        project_root: Root directory to restrict file operations to.
            Falls back to ``settings.PROJECT_ROOT`` then ``os.getcwd()``.
        workspace_scope: If set, restrict search_files and search_content
            to only the ``workspace/{workspace_scope}/`` subdirectory.
            Read/write/edit still work on any path under project_root.
        syntax_gate: [wave-22 B1] If True, write_file/edit_file run the F821
            gate on .py files under workspace/ (code_writer only — 8 agents
            share these tools and none of the others draft code).

    Returns:
        List of LangChain BaseTool instances.
    """
    root = _resolve_project_root(project_root)

    if workspace_scope:
        ws = os.path.join(root, "workspace", workspace_scope)
        if not (os.path.isdir(ws) or os.path.isdir(os.path.dirname(ws))):
            logger.warning(
                "workspace_scope='%s' but %s does not exist — scoping to root",
                workspace_scope,
                ws,
            )

    @tool
    def read_file(path: str, offset: int = 0, line: int = 0, num_lines: int = 0) -> str:
        """Read the content of a file and return it as a string.

        Args:
            path: Absolute or relative path to the file within the project.
            offset: CHARACTER position to start reading from. Files larger
                than 50K chars return ONLY head+tail (first/last 20K) on an
                untargeted read — do NOT page through the whole file. Use
                search_content to locate the exact section, then pass
                offset=<position> for a targeted ~50K window around it.
            line: LINE number to start reading from (1-based) — this is the
                unit search_content reports its hits in, so prefer this for
                targeted reads of code files.
            num_lines: How many lines to return with line= (default 400).

        Returns:
            The file content as text, or an error message if the file
            cannot be read. Large files are returned head+tail with a
            SNIPPED notice; targeted offset/line reads return a bounded
            window.
        """
        try:
            safe = _enforce_root(path, root)
        except ValueError as e:
            return str(e)
        try:
            with open(safe, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return f"File not found: {path}"
        except IsADirectoryError:
            return f"Path is a directory, not a file: {path}"
        except UnicodeDecodeError:
            return f"Cannot read binary file as text: {path}"
        except Exception as e:
            return f"Error reading '{path}': {e}"

        MAX_READ_CHARS = 50_000

        # [W26-4/prod-418] Line-based targeted read — the SAME unit
        # search_content reports, so the writer can aim without converting.
        if line:
            if offset:
                return (
                    "Pass EITHER offset= (a CHARACTER position) OR line= "
                    "(a LINE number, the unit search_content reports) — "
                    "not both."
                )
            all_lines = content.split("\n")
            total = len(all_lines)
            if line < 1 or line > total:
                return (
                    f"line {line:,} out of range: file has {total:,} lines. "
                    "line= takes a LINE number (1-based, the unit "
                    "search_content hits report); offset= would be a "
                    "CHARACTER position."
                )
            try:
                count = int(num_lines) if num_lines else 400
            except (TypeError, ValueError):
                count = 400
            count = max(1, min(count, 1200))
            window = all_lines[line - 1: line - 1 + count]
            end_line = line - 1 + len(window)
            body = "\n".join(window)
            if len(body) > MAX_READ_CHARS:
                body = body[:MAX_READ_CHARS]
                body += (
                    f"\n\n... [window truncated at {MAX_READ_CHARS:,} chars — "
                    f"re-call with line={end_line + 1} to continue]"
                )
            header = (
                f"[read_file: requested line {line}, showing lines "
                f"{line:,}-{end_line:,} of {total:,} — line= is a LINE "
                "number; offset= is a CHARACTER position]\n"
            )
            return header + body

        if offset:
            if offset < 0 or offset >= len(content):
                return (
                    f"offset {offset:,} out of range: file is "
                    f"{len(content):,} chars. NOTE: offset is a CHARACTER "
                    "position, NOT a line number — search_content hits "
                    "report LINES, so call read_file(path, line=<that "
                    "line number>) instead."
                )
            content = content[offset:]
        if len(content) > MAX_READ_CHARS:
            if offset:
                # Targeted window (the caller named a position) — page as before.
                next_offset = offset + MAX_READ_CHARS
                return (
                    content[:MAX_READ_CHARS]
                    + f"\n\n... [TRUNCATED: file is {len(content):,} chars from "
                    f"offset {offset:,}, showing chars {offset:,}-"
                    f"{next_offset - 1:,}. Re-call read_file with "
                    f"offset={next_offset:,} for the next portion.]"
                )
            # [wave-25 W25-a] Untargeted read of a large file: head+tail only.
            # Sequential paging of an 80K+ draft/output burned ~100 read_file
            # calls and ~2MB of tool-result context in prod job 404 before
            # langgraph's step budget silently killed the invocation
            # ("Sorry, need more steps"). Point at targeted tools instead.
            snipped = len(content) - _LARGE_READ_HEAD - _LARGE_READ_TAIL
            return (
                content[:_LARGE_READ_HEAD]
                + (
                    f"\n\n... [SNIPPED {snipped:,} of {len(content):,} chars — "
                    "large file, head+tail only. Do NOT page through the "
                    "whole file: use search_content to locate the exact "
                    "section, then read_file(path, line=<that line number>) "
                    "for a targeted window (line= is a LINE number; "
                    "offset= is a CHARACTER position). This read showed "
                    f"chars 0-{_LARGE_READ_HEAD - 1:,} and "
                    f"{len(content) - _LARGE_READ_TAIL:,}-"
                    f"{len(content) - 1:,}.]\n"
                )
                + content[-_LARGE_READ_TAIL:]
            )
        return content

    @tool
    def write_file(path: str, content: str, full_rewrite_reason: str = "") -> str:
        """Write content to a file, creating parent directories if needed.

        Args:
            path: Absolute or relative path within the project.
            content: Text content to write.
            full_rewrite_reason: Only for scraper_draft.py rewrites during a
                fix cycle, when targeted edits genuinely cannot fix the issue:
                a short explanation of WHY a from-scratch rewrite is required.
                The harness's codefix write guard reads this to allow the write
                through; this function itself ignores it.

        Returns:
            Success message with the resolved path, or an error message.
        """
        try:
            safe = _enforce_not_skills(path, root)
        except ValueError as e:
            return str(e)
        # [wave-22 B1] F821 gate: draft-shaped .py writes must define every
        # name they use. Runs BEFORE any bytes hit disk — a rejection leaves
        # nothing behind.
        if syntax_gate and safe.endswith(".py") and f"{os.sep}workspace{os.sep}" in safe:
            rejection = _f821_rejections(safe, content)
            if rejection:
                logger.warning(
                    "write guard: F821 gate rejected write to %s", safe
                )
                return rejection
        note = ""
        if safe.endswith(".json"):
            # F2 sanitize-on-write: never let an unparseable artifact reach disk
            # unflagged. Canonicalize on success; on failure write the raw bytes
            # (the phase-exit repair pass owns salvage) but say so.
            content, valid, err = sanitize_json_content(content)
            if not valid:
                note = "\n" + _JSON_WARN_NOTE
                logger.warning(
                    "write guard: %s is not valid JSON (%s) — written raw; "
                    "repair pass will attempt salvage on read",
                    safe, err,
                )
        try:
            os.makedirs(os.path.dirname(safe) or ".", exist_ok=True)
            with open(safe, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote {len(content)} characters to {safe}{note}"
        except Exception as e:
            return f"Error writing '{path}': {e}"

    @tool
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace an exact string in a file with a new string.

        The replacement is literal — no regex.  If *old_string* is not found,
        or is found multiple times, the operation fails so the LLM can retry
        with a more specific match.

        Args:
            path: Absolute or relative path within the project.
            old_string: The exact text to find in the file.
            new_string: The replacement text.

        Returns:
            Success or failure message with details.
        """
        try:
            safe = _enforce_not_skills(path, root)
        except ValueError as e:
            return str(e)
        try:
            with open(safe, encoding="utf-8") as f:
                original = f.read()
        except FileNotFoundError:
            return f"File not found: {path}"
        except Exception as e:
            return f"Error reading '{path}' for editing: {e}"

        count = original.count(old_string)
        if count == 0:
            # [wave-25e E6a] stale-copy honesty: a bounded prompt embed only
            # carries head+tail of large files — the same vocabulary as the
            # embed's staleness contract (draft_context._STALENESS_NOTE).
            return (
                f"old_string not found in '{path}'. "
                "Provide a more specific match or check the file content. "
                "If this file was provided head+tail in your prompt, your "
                "copy may be stale — search_content, then "
                "read_file(path, line=N) the window, and retry the edit "
                "(do NOT rewrite the file)."
            )
        if count > 1:
            return (
                f"old_string found {count} times in '{path}'. "
                "Provide more surrounding context to make the match unique."
            )

        updated = original.replace(old_string, new_string, 1)
        # [wave-22 B1] F821 gate on the EDITED content, before the write — a
        # rejection means the file on disk is untouched.
        if syntax_gate and safe.endswith(".py") and f"{os.sep}workspace{os.sep}" in safe:
            rejection = _f821_rejections(safe, updated)
            if rejection:
                logger.warning(
                    "write guard: F821 gate rejected edit to %s", safe
                )
                return rejection
        note = ""
        if safe.endswith(".json"):
            # Same guard as write_file, applied at the edit's write point: an
            # edit that leaves the file invalid JSON is canonicalized when
            # possible, and flagged (result + log) when not.
            updated, valid, err = sanitize_json_content(updated)
            if not valid:
                note = "\n" + _JSON_WARN_NOTE
                logger.warning(
                    "write guard: edit_file left %s as non-JSON (%s) — written "
                    "raw; repair pass will attempt salvage on read",
                    safe, err,
                )
        try:
            with open(safe, "w", encoding="utf-8") as f:
                f.write(updated)
            return (
                f"Successfully replaced 1 occurrence in {safe} "
                f"({len(original)} → {len(updated)} chars){note}"
            )
        except Exception as e:
            return f"Error writing edited file '{path}': {e}"

    @tool
    def search_files(pattern: str, path: str = ".") -> str:
        """Find files matching a glob pattern within the project.

        Args:
            pattern: Glob pattern (e.g. ``**/*.py``, ``src/**/*.json``).
            path: Base directory to search in. Defaults to the agent's
                workspace subfolder if scoping is active, otherwise project root.

        Returns:
            Newline-separated list of matching file paths, or an error message.
        """
        if workspace_scope and path == ".":
            effective_path = os.path.join("workspace", workspace_scope)
        else:
            effective_path = path
        try:
            base = _enforce_root(effective_path, root)
        except ValueError as e:
            return str(e)
        try:
            matches = sorted(
                glob.glob(os.path.join(base, pattern), recursive=True)
            )
            if not matches:
                return f"No files match pattern '{pattern}' in '{effective_path}'"
            rel = [os.path.relpath(m, root) for m in matches]
            return "\n".join(rel)
        except Exception as e:
            return f"Error searching files: {e}"

    @tool
    def search_content(
        pattern: str,
        path: str = ".",
        include: str | None = None,
    ) -> str:
        """Search file contents with a regular expression.

        Args:
            pattern: Regex pattern to search for (e.g. ``class.*Scraper``).
            path: Base directory to search in. Defaults to the agent's
                workspace subfolder if scoping is active, otherwise project root.
            include: Optional file glob filter (e.g. ``*.py``).

        Returns:
            Matching files with line numbers and excerpt lines, or a message
            if nothing was found. ``path`` may be a FILE (that exact file is
            searched; ``include`` is ignored for an explicit file) or a
            directory (walked recursively, ``include`` filters filenames).
        """
        if workspace_scope and path == ".":
            effective_path = os.path.join("workspace", workspace_scope)
        else:
            effective_path = path
        try:
            base = _enforce_root(effective_path, root)
        except ValueError as e:
            return str(e)

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex pattern '{pattern}': {e}"

        if not os.path.exists(base):
            return f"Path not found: {effective_path}"

        results: list[str] = []
        try:
            # A FILE base means "search this exact file". os.walk on a file
            # iterates zero times, which used to report a false "No matches"
            # and sent code_writer into probe-script workarounds (job-71
            # popsockets: ~20 wasted calls on template paths).
            if os.path.isfile(base):
                candidates = [base]
            else:
                candidates = [
                    os.path.join(dirpath, fname)
                    for dirpath, _dirnames, filenames in os.walk(base)
                    for fname in filenames
                    if not include or fnmatch.fnmatch(fname, include)
                ]
            for fpath in candidates:
                rel = os.path.relpath(fpath, root)
                try:
                    with open(fpath, encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, 1):
                            if compiled.search(line):
                                excerpt = line.rstrip()[:200]
                                results.append(f"{rel}:{lineno}: {excerpt}")
                except Exception:
                    continue
        except Exception as e:
            return f"Error searching content: {e}"

        if not results:
            return f"No matches for pattern '{pattern}' in '{effective_path}'"
        return "\n".join(results)

    @tool
    def check_syntax(path: str) -> str:
        """Parse a Python file for syntax errors WITHOUT running it.

        Args:
            path: Absolute or relative path to a .py file within the project.

        Returns:
            "OK: <path> parses cleanly (compiles)" on success, or a line-precise
            error ("Line N: <message>" plus the offending source line) on
            failure. Use this after EVERY write_file/edit_file to a .py file —
            syntax errors must never reach testing.
        """
        import ast as _ast

        try:
            safe = _enforce_root(path, root)
        except ValueError as e:
            return str(e)
        try:
            with open(safe, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except FileNotFoundError:
            return f"File not found: {path}"
        except IsADirectoryError:
            return f"Path is a directory, not a file: {path}"
        except Exception as e:
            return f"Error reading '{path}': {e}"

        # ast.parse gives the line-precise error; compile() catches the rare
        # invalid-syntax cases ast accepts (e.g. stray bytes issues) — cheap.
        try:
            _ast.parse(src, filename=safe)
            compile(src, safe, "exec")
        except SyntaxError as e:
            lines = src.splitlines()
            offending = ""
            if e.lineno and 1 <= e.lineno <= len(lines):
                offending = lines[e.lineno - 1][:200]
            where = f"Line {e.lineno}" + (f", offset {e.offset}" if e.offset else "")
            return (
                f"SYNTAX ERROR in {os.path.relpath(safe, root)}: {where}: {e.msg}\n"
                f"{offending}\n"
                f"Fix THIS exact line and re-run check_syntax."
            )
        except ValueError as e:
            return f"SYNTAX ERROR in {os.path.relpath(safe, root)}: {e}"
        except Exception as e:
            return f"Error checking '{path}': {e}"
        return f"OK: {os.path.relpath(safe, root)} parses cleanly (compiles)"

    return [read_file, write_file, edit_file, search_files, search_content, check_syntax]
