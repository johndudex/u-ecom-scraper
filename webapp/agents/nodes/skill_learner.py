"""Deterministic skill auto-apply — the write arm wave-55 gave skill_learner.

Audit H6 (2026-10-05): skill learning was structurally dead. ``skill_learner``
has no write tools (wave-29 A1) — it only *reports* proposals into
``learning_report.json`` — and it defers to ``nav_skill_review``, which runs
BEFORE it in the graph (right after ``navigation_synthesize``) and
early-returns on re-scrapes and non-navigation jobs. Net effect: every
proposal died in an ephemeral workspace file and the File Master's
``skills/_audit.jsonl`` went untouched after 2026-09-27, while
``Step navigation_skill_review`` measured a 17 ms median (an early return).

This is the smaller of the audit's two options: instead of re-running an LLM
reviewer at the tail of the job, ``_invoke_skill_learner`` hands the report
the learner already wrote to :func:`apply_learning_proposals`, which replays
its high-confidence ``status: "applied"`` entries through the SAME write path
the admin UI uses (``src.skills_store.append_learned`` / ``create_skill`` —
flock-guarded, append-only, audit-trailed), actor-tagged so the trail
distinguishes machine applies from ``ui:<username>`` ones.

Non-blocking by contract: an unreadable report, an unreachable File Master
or a refused write is logged and SKIPPED — this runs after a job finished
successfully, so it must never turn a SUCCESS red.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

# Reusability floor: only the learner's own "high" verdict may touch the
# shared skill DB without a human. The report is LLM-authored, so the rank is
# re-checked here rather than trusted (the learner's prompt gates harder).
MIN_PROPOSAL_CONFIDENCE = "high"
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Per-job ceiling — the learner's own "1-2 quality applications beat 10
# marginal ones". The report order is the priority order.
MAX_AUTO_APPLY_PER_JOB = 2

# Audit-trail actor — how these differ from the UI's ``ui:<username>`` rows.
ACTOR = "skill_learner:auto"


def apply_learning_proposals(state: dict) -> dict[str, Any]:
    """Replay ``learning_report.json``'s applied proposals into the skills.

    Returns ``{"applied": [...], "skipped": [...]}`` (``{}`` when there is
    nothing to do). The caller logs the summary — it is deliberately NOT
    merged into graph state.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    if not slug:
        return {}

    root = getattr(settings, "PROJECT_ROOT", os.getcwd())
    report_path = os.path.join(root, "workspace", slug, "learning_report.json")
    if not os.path.isfile(report_path):
        return {}
    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "skill_learner.auto: unreadable learning_report.json (%s) — "
            "no skills applied (job %s)", exc, job_id,
        )
        return {}

    entries = report.get("potential_learnings")
    if not isinstance(entries, list):
        return {}

    url = state.get("url", "") or report.get("site_url", "")
    applied: list[dict] = []
    skipped: list[dict] = []
    for entry in entries:
        title = (entry.get("title") or "untitled")[:120]
        verdict = _entry_is_eligible(entry)
        if verdict is None and len(applied) >= MAX_AUTO_APPLY_PER_JOB:
            # At the cap: record the remainder rather than silently ignoring
            # it (the learner's own rule — every candidate stays traceable).
            skipped.append({
                "title": title,
                "reason": f"cap of {MAX_AUTO_APPLY_PER_JOB} applies per job reached",
            })
            continue
        ok, detail = _apply_entry(entry, url) if verdict is None else verdict
        if ok:
            applied.append({"skill": detail, "title": title})
            logger.info(
                "skill_learner.auto: applied '%s' → %s (job %s)", title, detail, job_id
            )
        else:
            skipped.append({"title": title, "reason": detail})
            logger.info(
                "skill_learner.auto: skipped '%s' — %s (job %s)", title, detail, job_id
            )

    logger.info(
        "skill_learner.auto: %d applied, %d skipped (cap %d, job %s)",
        len(applied), len(skipped), MAX_AUTO_APPLY_PER_JOB, job_id,
    )
    return {"applied": applied, "skipped": skipped}


def _entry_is_eligible(entry: dict) -> tuple[bool, str] | None:
    """The confidence + status gate. ``None`` = eligible, decide the payload."""
    if (entry.get("status") or "") != "applied":
        return False, f"status is {entry.get('status') or 'unset'!r}, not 'applied'"
    confidence = str(entry.get("reusability") or "").lower()
    if _CONFIDENCE_RANK.get(confidence, -1) < _CONFIDENCE_RANK[MIN_PROPOSAL_CONFIDENCE]:
        return False, f"reusability {confidence or 'unset'!r} < {MIN_PROPOSAL_CONFIDENCE}"
    return None


def _apply_entry(entry: dict, url: str) -> tuple[bool, str]:
    """One report entry → one ``skills_store`` write. Never raises."""
    import src.skills_store as store

    proposed_append = entry.get("proposed_append")
    proposed_new = entry.get("proposed_new_skill")
    existing = (entry.get("existing_skill") or "").strip()
    description = (entry.get("description") or "").strip()

    try:
        # An explicit append payload wins (the learner built it for learn_skill).
        if isinstance(proposed_append, dict) and proposed_append.get("skill_name"):
            return _append(
                store, proposed_append.get("skill_name"),
                proposed_append.get("title") or entry.get("title"),
                proposed_append.get("source") or url,
                proposed_append.get("applicability") or entry.get("reason") or "",
                proposed_append.get("body") or description,
            )
        # A genuinely new skill (rare) — only when the learner said so.
        if isinstance(proposed_new, dict) and proposed_new.get("name"):
            return _create(
                store, proposed_new.get("name"),
                proposed_new.get("description") or description,
                proposed_new.get("body") or description,
            )
        if existing:
            # No structured payload: assemble the section from the report's
            # own fields rather than dropping a gated, high-confidence entry.
            body = description
            evidence = entry.get("evidence")
            if isinstance(evidence, list) and evidence:
                body += "\n\nEvidence:\n" + "\n".join(
                    f"- {e}" for e in evidence if isinstance(e, str)
                )
            return _append(
                store, existing, entry.get("title"), url,
                entry.get("reason") or entry.get("category") or "", body,
            )
        return False, "no actionable payload (no skill named, no proposed_append/new_skill)"
    except Exception as exc:  # FM outage, lock timeout, anything — never raise.
        logger.warning("skill_learner.auto: apply failed: %s", exc)
        return False, f"write failed: {exc}"


def _append(store, skill_name, title, source, applicability, body) -> tuple[bool, str]:
    if not (body or "").strip():
        return False, "empty body"
    result = store.append_learned(
        skill_name, title or "", source or "", applicability or "", body, actor=ACTOR
    )
    if not result.get("ok"):
        return False, result.get("error") or result.get("note") or "append refused"
    if not result.get("appended"):
        return False, result.get("note") or "duplicate title"
    return True, result.get("skill") or str(skill_name)


def _create(store, name, description, body) -> tuple[bool, str]:
    if not (body or "").strip():
        return False, "empty body"
    result = store.create_skill(name or "", description or "", body, actor=ACTOR)
    if not result.get("ok"):
        return False, result.get("error") or "create refused"
    return True, result.get("skill") or str(name)
