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

# [wave-59 C3] Gate 0 streak state. Consecutive syntax rejections by the
# same process; a clean gate-0 pass resets. Bounded at 3 (the block lifts on
# the 4th bad write) so a writer in a syntax loop is never wedged — the
# phase-boundary syntax fixer owns the file then. The counter counts
# REJECTIONS (pre-write content) — never "current file state", which after a
# rejection is stale bytes.
_SYNTAX_REJECT_MAX = 3
_syntax_reject_streak = {"n": 0}


def _syntax_gate_outcome(content: str) -> tuple[str, bool]:
    """[wave-59 C3] Gate 0 disposition as ``(rejection, lift)``.

    ``lift`` is True ONLY from the explicit streak-lift branch — the one
    place the block is intentionally relaxed so the payload lands. Kill
    switch, internal-error fall-open and clean passes all report lift=False:
    "the gate did not judge" must never be confused with "the gate chose to
    allow a bad payload through".

    [wave-60 N1] The public ``_syntax_gate_rejection`` keeps its string
    contract (pinned across wave-22/25/40/59 tests); callers that need to
    know a write proceeded ONLY because the streak lifted read the tuple.
    """
    if os.getenv("SCRAPER_DRAFT_SYNTAX_GATE", "1") == "0":
        return "", False
    try:
        compile(content, "<draft>", "exec")
    except SyntaxError as exc:
        _syntax_reject_streak["n"] += 1
        if _syntax_reject_streak["n"] > _SYNTAX_REJECT_MAX:
            return "", True  # bounded: lift the block, the backstop owns the file
        where = f"line {exc.lineno or 0} col {exc.offset or 0}"
        if _syntax_reject_streak["n"] == _SYNTAX_REJECT_MAX:
            return (
                f"REJECTED — NOT applied; file unchanged. SYNTAX ERROR "
                f"{where}: {exc.msg}. {_SYNTAX_REJECT_MAX} consecutive "
                f"syntax rejections — stop editing this region; REWRITE the "
                f"file fresh with write_file containing the full corrected "
                f"content. {_F821_REPAIR_CONTRACT}",
                False,
            )
        return (
            f"REJECTED — NOT applied; file unchanged. SYNTAX ERROR "
            f"{where}: {exc.msg}. {_F821_REPAIR_CONTRACT}",
            False,
        )
    except Exception:
        return "", False  # judge nothing on an internal error — fall open
    _syntax_reject_streak["n"] = 0
    return "", False


def _syntax_gate_rejection(content: str) -> str:
    """[wave-59 C3] Gate 0: parseability. "" (open) or a rejection."""
    return _syntax_gate_outcome(content)[0]


def reset_syntax_reject_streak() -> None:
    """[wave-60 N1] Reset gate 0's streak — writer-window start hygiene.

    ``create_code_writer`` calls this at every writer construction, which
    covers all three invocation seams (main window, progress escalation,
    strike-2 finisher): a terminal streak left by one seam must never lift
    the block on the next seam's FIRST bad write.
    """
    _syntax_reject_streak["n"] = 0


# [wave-60 N1] Sidecar constants. The floor mirrors the repair arm's
# substance floor (graph.py draft-finisher): below it there is nothing worth
# protecting, so a lifted payload lands as before and the lift contract
# (C3: a wedged writer is never blocked) stays intact.
_SUBSTANTIAL_DRAFT_LINES = 200
_DIVERT_SIDECAR_NAME = "scraper_draft-rejected.py"


def _divert_lifted_write(safe: str, payload: str) -> tuple[str, str | None]:
    """[wave-60 N1] Route a streak-lifted payload away from a real draft.

    The 632 class: the writer destroyed its own ~1,800-line draft at streak
    strike 4 — the lift let a placeholder OVERWRITE the draft of record, and
    the repair arm then had nothing ≥200 lines to salvage. Contract: when a
    substantial draft IS on disk, the lifted payload diverts to a rolling
    sidecar ``scraper_draft-rejected.py`` (same dir, OVERWRITTEN per divert)
    and the tool result says so explicitly; when no substantial draft exists
    (absent, or under the floor) the payload lands exactly as before — C3's
    lift contract is untouched.

    Rotation-pivot safety (guardian-verified): ``_archive_dead_strategy_draft``
    removes the draft via ``os.replace`` before a strategy pivot, so an
    ABSENT draft reports no-divert and the lifted pivot draft lands. The
    helper inherits the caller's already-resolved ``safe`` path — it must
    not re-derive slug scope. Fail-open: any internal error reports
    no-divert so a bug here can never wedge the lift.

    Returns ``(target_path, note)``: ``note`` is None → caller proceeds with
    the normal write to *safe*; ``note`` set → the payload is ALREADY on the
    sidecar and the caller must return *note* to the model INSTEAD of
    writing *safe*.
    """
    try:
        if os.path.basename(safe) != "scraper_draft.py":
            return safe, None
        with open(safe, encoding="utf-8") as f:
            existing = f.read()
        if len(existing.splitlines()) < _SUBSTANTIAL_DRAFT_LINES:
            return safe, None
        sidecar = os.path.join(os.path.dirname(safe), _DIVERT_SIDECAR_NAME)
        with open(sidecar, "w", encoding="utf-8") as f:
            f.write(payload)
        return sidecar, (
            "SYNTAX GATE lift: payload written to "
            f"{_DIVERT_SIDECAR_NAME}; scraper_draft.py UNCHANGED (or still "
            "absent). Fix the syntax error and write the draft again."
        )
    except Exception:
        # No draft to read → land as today; anything stranger → land too.
        logger.warning(
            "write guard: lift divert check failed — lifted write lands",
            exc_info=True,
        )
        return safe, None


def _f821_outcome(path: str, content: str) -> tuple[str, bool]:
    """Draft-gate stack disposition as ``(rejection, lift)``. [wave-60 N1]

    Gate 0's streak-lift propagates ONLY through a fully-open result — a
    downstream gate-1 (ruff F821) or gate-2 (call-signature) rejection keeps
    the write BLOCKED and reports lift=False, so a lifted payload is never
    diverted on a write that is in fact still rejected.
    """
    rejection, lift = _syntax_gate_outcome(content)
    if rejection:
        return rejection, False
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
        return "", lift
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
                return _call, False
        return "", lift
    shown = findings[:_F821_MAX_FINDINGS]
    more = len(findings) - len(shown)
    return (
        "REJECTED — NOT applied; file unchanged. Undefined name(s): "
        + "; ".join(shown)
        + (f" (and {more} more)" if more > 0 else "")
        + f". {_F821_NON_FIX_NOTE}. {_F821_REPAIR_CONTRACT}",
        False,
    )


def _f821_rejections(path: str, content: str) -> str:
    """Run the draft write gates over content; "" (gate open) or a rejection.

    Three gates, one seam (every code_writer write_file/edit_file call site
    goes through here):

    0. [wave-59 C3] parseability — ``compile()`` the content pre-write.
    1. [wave-22 B1] ruff F821 — undefined names.
    2. [wave-40 T7] ``draft_call_violation`` — helper CALL signatures.

    Falls OPEN on any checker malfunction (binary missing, timeout, crash):
    a broken linter must never block a write — ``_fix_scraper_syntax`` at the
    phase boundary stays the authoritative backstop. Gate 0 is BOUNDED
    (critic-2's wedging concern): after ``_SYNTAX_REJECT_MAX`` consecutive
    syntax rejections the streak counter lifts the block. Kill switches:
    ``SCRAPER_DRAFT_CALL_GATE=0`` disarms gate 2 only;
    ``SCRAPER_DRAFT_SYNTAX_GATE=0`` disarms gate 0 only.

    Public string contract — pinned across the wave-22/25/40/59 tests; the
    call-scoped ``(rejection, lift)`` disposition (wave-60 N1) lives in
    ``_f821_outcome``.
    """
    return _f821_outcome(path, content)[0]


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


def _enforce_slug_scope(path: str, root: str, slug: str | None) -> str:
    """Write-guard: an agent writes only inside its OWN job's workspace.

    [wave-55 C1] Job 1156's code_writer issued four ``write_file`` calls into
    ``workspace/src/`` — a directory no pipeline node creates — minting a
    site-local, proxy-free clone of the pipeline's ``src`` package. The
    templates' ``sys.path`` insert then resolved ``src`` to that shadow for
    EVERY draft on the volume: 30/79 jobs crashed at import and writers that
    merely read the shadow built drafts against the wrong API surface.
    ``_enforce_root`` only bounds the project root, so nothing stopped a
    writer from minting a sibling directory. Scoped by ``workspace_scope``
    (the job's slug, the same value run_bash already scopes reads with), so
    callers that pass no scope are unchanged.

    Site-local helper modules belong in ``workspace/{slug}/_vendor/`` — inside
    the slug dir, so the same guard admits them.
    """
    resolved = _enforce_root(path, root)
    if not slug:
        return resolved
    scope_dir = os.path.join(os.path.abspath(root), "workspace", slug)
    if resolved == scope_dir or resolved.startswith(scope_dir + os.sep):
        return resolved
    raise ValueError(
        f"REJECTED — '{path}' resolves to '{resolved}', outside this job's "
        f"workspace. Writes are scoped to workspace/{slug}/ (use "
        f"workspace/{slug}/_vendor/ for site-local helper modules). NEVER "
        "create workspace/src/ or another job's directory — a workspace-local "
        "src/ package shadows the pipeline's src/ for every draft on this "
        "volume."
    )


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
            to only the ``workspace/{workspace_scope}/`` subdirectory and
            write/edit to files under ``workspace/{workspace_scope}/``
            (reads stay project-root-wide). Callers that pass no scope keep
            the unscoped behavior.
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
            safe = _enforce_slug_scope(_enforce_not_skills(path, root), root, workspace_scope)
        except ValueError as e:
            return str(e)
        # [wave-22 B1] F821 gate: draft-shaped .py writes must define every
        # name they use. Runs BEFORE any bytes hit disk — a rejection leaves
        # nothing behind. [wave-60 N1] a streak-LIFTED payload whose target
        # is a substantial draft on disk diverts to the sidecar instead —
        # the 632 class (placeholder overwrites the real draft at strike 4).
        if syntax_gate and safe.endswith(".py") and f"{os.sep}workspace{os.sep}" in safe:
            rejection, lift = _f821_outcome(safe, content)
            if rejection:
                logger.warning(
                    "write guard: F821 gate rejected write to %s", safe
                )
                return rejection
            if lift:
                divert_target, divert_note = _divert_lifted_write(safe, content)
                if divert_note:
                    logger.warning(
                        "write guard: syntax-gate lift diverted to %s",
                        divert_target,
                    )
                    return divert_note
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
            # [wave-55 C1] Same admission rule as write_file: an edit cannot
            # mint a file, but with a scope set it can mutate any existing
            # file under the project root (incl. src/*.py) — the write guard
            # refuses the mint, so the edit path must refuse the mutation.
            safe = _enforce_slug_scope(_enforce_not_skills(path, root), root, workspace_scope)
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
        # rejection means the file on disk is untouched. [wave-60 N1] a
        # streak-LIFTED edit diverts the full post-edit hypothetical
        # (``updated``) to the sidecar when a substantial draft exists —
        # never the bare ``new_string``.
        if syntax_gate and safe.endswith(".py") and f"{os.sep}workspace{os.sep}" in safe:
            rejection, lift = _f821_outcome(safe, updated)
            if rejection:
                logger.warning(
                    "write guard: F821 gate rejected edit to %s", safe
                )
                return rejection
            if lift:
                divert_target, divert_note = _divert_lifted_write(safe, updated)
                if divert_note:
                    logger.warning(
                        "write guard: syntax-gate lift diverted to %s",
                        divert_target,
                    )
                    return divert_note
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
