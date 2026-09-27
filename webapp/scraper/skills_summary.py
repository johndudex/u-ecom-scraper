"""Per-job skill provenance, derived read-only from existing log rows.

[wave-44] Backs the intake deep-link "Skills" panel: which skills a job
USED (injected into the writer's prompt by the deterministic distillation,
or loaded via load_skill) and which it WROTE (actual learn_skill /
create_new_skill tool calls) or PROPOSED to write (skill_learner's
analysis summary).

All parsers are pure functions over row-like objects (duck-typed
.agent/.content/.tool_name/.args_summary); ``summarize_job_skills`` is the
thin DB wrapper surfaced in job_api. No rows are ever created here.
"""

from __future__ import annotations

import re

# Header emitted by webapp.agents.subagents._learned_notes_block — the only
# honest marker that a skill's learned notes reached a prompt.
_INJECTED_RE = re.compile(r"LEARNED SKILL NOTES \(from `([a-z0-9_-]+)`")
_LOADED_RE = re.compile(r"Load skill:\s*([a-z0-9_-]+)")
# skill_learner's summary lines: "Skills modified: [a (x), b]" / "Skills created: []"
_MODIFIED_RE = re.compile(r"Skills modified:\s*\[([^\]]*)\]")
_CREATED_RE = re.compile(r"Skills created:\s*\[([^\]]*)\]")
# Actual writes: the [TOOL] row format used by graph tool logging, e.g.
# "[TOOL] learn_skill: {'skill_name': 'x', ...}". Both kwarg spellings are
# accepted because learn_skill/create_new_skill were observed with either.
_LEARN_ROW_RE = re.compile(
    r"\[TOOL\] (?:learn_skill|create_new_skill): .*?'(?:skill_name|name)':\s*'([a-z0-9_-]+)'",
    re.S,
)
_ENTRY_SPLIT_RE = re.compile(r"\s*,\s*")


def parse_injected(rows) -> list[str]:
    """Skill names whose LEARNED SKILL NOTES block reached a prompt.

    Sorted + deduped. Platform mentions without the block header don't
    count (wave-43's negation guard means platform words appear in
    verdicts that injected nothing).
    """
    found: set[str] = set()
    for row in rows:
        found.update(_INJECTED_RE.findall(getattr(row, "content", "") or ""))
    return sorted(found)


def parse_loaded(rows) -> list[dict]:
    """load_skill tool calls as [{'skill', 'agent'}], deduped in order."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if getattr(row, "tool_name", "") != "load_skill":
            continue
        m = _LOADED_RE.search(getattr(row, "args_summary", "") or "")
        if not m:
            continue
        key = (m.group(1), getattr(row, "agent", "") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"skill": key[0], "agent": key[1]})
    return out


def _split_entries(raw: str) -> list[str]:
    names = []
    for entry in _ENTRY_SPLIT_RE.split(raw.strip()):
        entry = entry.strip()
        if not entry:
            continue
        names.append(entry.split(" (", 1)[0].strip())
    return names


def parse_written(rows) -> tuple[list[str], list[str]]:
    """Returns (actual, proposed) skill names.

    actual — skill names from real learn_skill/create_new_skill [TOOL]
    rows (nav_skill_review is the only agent wired with write tools).
    proposed — skill names from skill_learner's "Skills modified/created"
    summary lines. These are analysis PROPOSALS: skill_learner has no
    write tools (wave-29 A1), so they must never be presented as writes.
    """
    actual: list[str] = []
    proposed: list[str] = []
    for row in rows:
        content = getattr(row, "content", "") or ""
        actual.extend(_LEARN_ROW_RE.findall(content))
        for m in _MODIFIED_RE.findall(content) + _CREATED_RE.findall(content):
            proposed.extend(_split_entries(m))
    # dedupe, preserve order
    actual = list(dict.fromkeys(actual))
    proposed = [p for p in dict.fromkeys(proposed) if p not in actual]
    return actual, proposed


def summarize_job_skills(job_id: int) -> dict:
    """DB wrapper: read this job's log rows and classify skill provenance."""
    from scraper.models import SessionLog, ToolCallLog

    injected_rows = SessionLog.objects.filter(
        job_id=job_id, content__contains="LEARNED SKILL NOTES"
    ).only("content")
    loaded_rows = ToolCallLog.objects.filter(
        job_id=job_id, tool_name="load_skill"
    ).only("agent", "args_summary")
    write_rows = SessionLog.objects.filter(job_id=job_id).filter(
        content__contains="Skills modified"
    ) | SessionLog.objects.filter(job_id=job_id).filter(
        content__contains="[TOOL] learn_skill"
    )
    create_rows = SessionLog.objects.filter(
        job_id=job_id, content__contains="[TOOL] create_new_skill"
    ).only("content")
    actual, proposed = parse_written(list(write_rows) + list(create_rows))
    return {
        "injected": parse_injected(injected_rows),
        "loaded": parse_loaded(loaded_rows),
        "written": actual,
        "proposed": proposed,
    }
