"""Per-site code_writer memory — File Master ``scrapers/{slug}/analysis/``.

Why this module exists (wave-29, docs/plans/wave29-skills-memory-plan.md)
    code_writer has zero cross-job memory. Re-drive a failed site → fresh
    probe, fresh analyzers, wiped workspace (setup_workspace PRESERVE_FILES
    =∅). Failed sites never reach skill_learner/nav_skill_review (both run
    SUCCESS-only), and the only cross-job writer fact was _prior_count_line
    (completed jobs). The cost is real: re-drives re-derive known fixes
    (job 524 burned 2×1800s writer turns re-researching transport).

Design principles (plan v2)
    - Deterministic injection > voluntary tool calls. The store is written
      by graph nodes (cleanup failure path, skill_learner success path) and
      read by deterministic prompt builders — no agent discretion involved.
    - Measured beats remembered — with teeth. Memory is advisory; B5 reads
      apply a mechanical stale-guard (probe disagreement drops strategy
      words) rather than trusting stale entries.
    - Fixes/lessons are memory; URL-shape facts are contamination-adjacent.
      sanitize_note() bans URLs mechanically (H3: no cross-run URL seeds).
    - Patterns, not the package (deepagents scoped-memory inspiration; no
      dependency).

Concurrency
    The running-sibling guard filters ``url=job.url``, NOT slug
    (scraper/tasks.py) — two URLs of one site CAN run concurrently, and the
    FM write is a bare PUT. The RMW below therefore holds a per-slug flock
    (same idiom as src/skills_store.py:_skills_lock — works across prefork
    children on one machine; accepts the same residual django-vs-celery
    window skills_store accepted).

Pure-python like src/skills_store.py (no Django import) so the worker,
django, and tests can all use it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 12
_MAX_PER_FAILURE_CLASS = 3
_NOTE_MAX = 160
_DIGEST_MAX = 500

_HTTP_BAN_RE = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
_FENCE_RE = re.compile(r"```+")

# Canonical FM key for one site's memory.
MEMORY_KEY = "scrapers/{slug}/analysis/writer_memory.json"


def memory_key(slug: str) -> str:
    import src.artifacts as artifacts

    return artifacts.scrapers_key(slug, "analysis", "writer_memory.json")


# ─── sanitization ─────────────────────────────────────────────────────────────


def sanitize_note(text: str) -> str:
    """Make a note safe for reuse: URL-ban (truncate at the first URL), no
    code fences, collapsed whitespace, hard-capped. Everything downstream of
    the first ``http``/``www.`` is dropped — a URL-leak variant was
    near-certain without this, and scraped-content payloads have no business
    crossing jobs anyway."""
    if not isinstance(text, str):
        return ""
    text = _FENCE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    m = _HTTP_BAN_RE.search(text)
    if m:
        text = text[: m.start()].rstrip(" ,;:—-(")
    return text[:_NOTE_MAX].strip()


# ─── failure classification (whitelisted, deterministic) ─────────────────────


def derive_failure_class(report: dict | None) -> str:
    """One whitelisted class per failed run — never free LLM text.

    Notes in memory entries are composed ONLY from deterministic report
    fields (class + remediation target); this is the injection-channel bound.
    """
    if not isinstance(report, dict):
        return "other"
    if str(report.get("crash_error") or "").strip():
        return "crash"
    types = {
        str(i.get("issue_type") or "").strip().upper()
        for i in (report.get("issues") or [])
        if isinstance(i, dict)
    }
    if any("TIMEOUT" in t for t in types):
        return "timeout"
    if any(t.startswith("HTTP") or "STATUS" in t or t in {"BLOCKED", "CAPTCHA"} for t in types):
        return "http_status"
    if types & {"MISSING_FIELD", "WRONG_TYPE", "EMPTY_FIELD", "EMPTY"}:
        return "missing_fields"
    return "other"


# ─── structural remediation fingerprint (mirror of wave-22 B3) ───────────────

_TRACEBACK_RE = re.compile(r"\b\w*(?:Error|Exception|Warning)\s*:", re.IGNORECASE)


def fingerprint_parts(report: dict | None) -> dict:
    """Structural identity of a report's remediation ask: target + field +
    sorted issue-type set + exception class. Mirrors
    webapp/agents/nodes/route_after_testing.py:_remediation_fingerprint's
    keying EXACTLY (hex must match — keep the two in lockstep, see the
    wave-32 C3 ``fields`` fallback in both)."""
    if not isinstance(report, dict):
        return {}
    rem = report.get("remediation")
    if not isinstance(rem, dict):
        return {}
    target = str(rem.get("target") or "").strip()
    # [wave-32 C3] Mirror of route_after_testing's fields fallback — hex
    # parity for multi-field verdicts is what lets B5 retry lines match.
    field = (
        str(rem.get("field") or "").strip()
        or ",".join(sorted(str(f) for f in (rem.get("fields") or [])))
    )
    types = sorted(
        {
            str(i.get("issue_type") or "").strip().upper()
            for i in (report.get("issues") or [])
            if isinstance(i, dict)
        }
        - {""}
    )
    crash = str(report.get("crash_error") or "")
    _m = _TRACEBACK_RE.search(crash)
    exc = _m.group(0).split(":")[0].strip() if _m else ""
    return {"target": target, "field": field, "issue_types": types, "exception": exc}


def structural_fingerprint(report: dict | None) -> str:
    import hashlib

    parts = fingerprint_parts(report)
    if not any(parts.values()):
        return ""
    payload = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ─── store (FM-backed, flocked RMW) ──────────────────────────────────────────

_LOCK_TIMEOUT_S = 15.0


class _slug_lock:
    """flock per slug — two URLs of one site can run concurrently (the
    running-sibling guard filters url, not slug). Same degrade-open policy as
    skills_store: an unavailable lock proceeds WITHOUT it (best-effort
    memory must never fail a job)."""

    def __init__(self, slug: str) -> None:
        self._slug = slug
        self._fd = None

    def __enter__(self):
        import fcntl

        safe = re.sub(r"[^a-z0-9_-]", "", (self._slug or "").lower()) or "default"
        lock_path = os.environ.get("WRITER_MEMORY_LOCK_DIR", "/tmp") + f"/writer_memory_{safe}.lock"
        try:
            self._fd = open(lock_path, "a+")
            deadline = time.time() + _LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError(f"writer_memory lock timeout ({self._slug})")
                    time.sleep(0.2)
        except Exception as exc:
            logger.warning("writer_memory: lock unavailable (%s) — proceeding WITHOUT lock", exc)
            if self._fd:
                try:
                    self._fd.close()
                except OSError:
                    pass
                self._fd = None
            return self

    def __exit__(self, *exc):
        if self._fd is not None:
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
        return False


def load_memory(slug: str) -> dict | None:
    """Read one site's memory. None when missing OR corrupt (advisory data:
    a bad file must never break a job — the caller just sees no memory)."""
    if not slug:
        return None
    try:
        import src.artifacts as artifacts

        mem = artifacts.read_json(MEMORY_KEY.format(slug=slug))
        if isinstance(mem, dict) and isinstance(mem.get("entries"), list):
            return mem
        return None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("writer_memory: load failed for '%s': %s", slug, exc)
        return None


def _build_digest(entries: list[dict]) -> str:
    parts = [e.get("note") for e in entries if e.get("note")]
    digest = "; ".join(parts)
    return digest[:_DIGEST_MAX].rstrip(" ;")


def upsert_memory(
    slug: str,
    entry: dict | None = None,
    probe_fingerprint: dict | None = None,
) -> dict:
    """Flocked read-modify-write of one site's memory file.

    ``entry``: {job_id, outcome, strategy, item_count, failure_class,
    remediation_fp, fp_parts, probe_method, note} — note is sanitized here.
    ``probe_fingerprint``: {method, platform} of this job's probe (top-level
    latest-wins). Entries also carry their own probe_method so B5's
    stale-guard can judge each line individually.

    Invariants applied inside the lock: same-fp-different-outcome supersedes
    the older entry; per-failure-class cap 3 (newest kept); ring buffer 12;
    lessons_digest rebuilt newest-first, ≤500 chars.
    """
    import src.artifacts as artifacts

    with _slug_lock(slug):
        mem = load_memory(slug)
        if mem is None:
            mem = {
                "site": slug,
                "updated": int(time.time()),
                "probe_fingerprint": {},
                "entries": [],
                "lessons_digest": "",
            }
        if isinstance(probe_fingerprint, dict) and probe_fingerprint:
            mem["probe_fingerprint"] = {
                k: str(v) for k, v in probe_fingerprint.items() if v
            }

        if entry:
            e = dict(entry)
            e["job_id"] = int(e.get("job_id") or 0)
            e["ts"] = int(e.get("ts") or time.time())
            e["outcome"] = str(e.get("outcome") or "failure")
            e["note"] = sanitize_note(str(e.get("note") or ""))
            e["failure_class"] = e.get("failure_class") or None
            # supersede: same structural fingerprint, different outcome
            fp = str(e.get("remediation_fp") or "")
            if fp:
                for old in mem["entries"]:
                    if (
                        str(old.get("remediation_fp") or "") == fp
                        and old.get("outcome") != e["outcome"]
                        and "superseded_by" not in old
                    ):
                        old["superseded_by"] = e["job_id"]
            mem["entries"].append(e)

        # per-failure-class cap (newest kept); success/None-class exempt
        by_class: dict[str, list[int]] = {}
        for idx, e in enumerate(mem["entries"]):
            cls = e.get("failure_class")
            if e.get("outcome") == "failure" and cls:
                by_class.setdefault(str(cls), []).append(idx)
        drop: set[int] = set()
        for cls, idxs in by_class.items():
            for idx in idxs[:-_MAX_PER_FAILURE_CLASS]:
                drop.add(idx)
        mem["entries"] = [e for i, e in enumerate(mem["entries"]) if i not in drop]

        # ring buffer (entries are appended chronologically)
        mem["entries"] = mem["entries"][-_MAX_ENTRIES:]

        mem["updated"] = int(time.time())
        mem["lessons_digest"] = _build_digest(mem["entries"])
        artifacts.write_json(MEMORY_KEY.format(slug=slug), mem)
        return mem


# ─── read path renderers (deterministic injection surfaces) ──────────────────


def render_first_attempt_block(
    mem: dict | None, max_chars: int = 800, fresh_probe_method: str | None = None
) -> str:
    """B4: slim block for the writer's FIRST attempt (beside
    _prior_count_line). Value on first attempts is the one-line
    outcome/strategy + open lessons, nothing more — the 800-char cap is
    load-bearing (every added char rides all writer turns; 25KB ballooning
    precedent). Same stale-guard as B5: probe disagreement drops strategy
    words (plan: strategy-flavored words are dropped from ALL reads)."""
    if not mem or not mem.get("entries"):
        return ""
    lines = ["SITE MEMORY (from prior jobs on this site):"]
    for e in mem["entries"][-3:]:
        outcome = e.get("outcome") or "?"
        line = f"- job {e.get('job_id')}:"
        stale = bool(
            fresh_probe_method
            and e.get("probe_method")
            and e["probe_method"] != fresh_probe_method
        )
        if not stale and e.get("strategy"):
            line += f" strategy {e['strategy']}"
        line += f" → {outcome}"
        if e.get("failure_class"):
            line += f" (class: {e['failure_class']})"
        if e.get("superseded_by"):
            line += f" [superseded by job {e['superseded_by']}]"
        if e.get("note"):
            line += f"; {e['note']}"
        lines.append(line)
    if mem.get("lessons_digest"):
        lines.append(f"open lessons: {mem['lessons_digest']}")
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[:max_chars].rstrip() + "\n…(truncated)"
    return block


def render_retry_fingerprint_lines(
    report: dict | None,
    mem: dict | None,
    fresh_probe_method: str | None = None,
    cap: int = 2,
) -> list[str]:
    """B5 (the load-bearing surface): when the CURRENT failure's structural
    fingerprint matches a prior entry, render "this signature occurred
    before" lines for the wave-20 retry slot — the one injection point with
    proven behavioral effect (its comment documents the exact pathology:
    regenerated drafts repeating a defect because retry context never stated
    the rule).

    Match tiers: exact (target+field+issue-set+exception), then degraded
    (target+issue-set, no field). Cap 2. Stale-guard: when the stored
    entry's probe method disagrees with the fresh probe, strategy words are
    DROPPED from the line (measured beats remembered)."""
    parts = fingerprint_parts(report)
    if not mem or not mem.get("entries") or not any(parts.values()):
        return []
    entries = mem["entries"]
    target = parts.get("target") or ""
    field = parts.get("field") or ""
    types = set(parts.get("issue_types") or [])
    exception = parts.get("exception") or ""

    def matches(e: dict, need_field: bool) -> bool:
        ep = e.get("fp_parts") or {}
        if (ep.get("target") or "") != target or not target:
            return False
        if need_field and (ep.get("field") or "") != field:
            return False
        if set(ep.get("issue_types") or []) != types or not types:
            return False
        return True

    exact = [e for e in entries if matches(e, need_field=True) and (ep_exc(e) == exception)]
    seen = {id(e) for e in exact}
    degraded = [e for e in entries if id(e) not in seen and matches(e, need_field=False)]

    lines: list[str] = []
    for tier, group in (("exact", exact), ("degraded", degraded)):
        for e in group:
            if len(lines) >= cap:
                return lines
            stale = bool(
                fresh_probe_method
                and e.get("probe_method")
                and e["probe_method"] != fresh_probe_method
            )
            seg = f"This failure signature ({tier} match) occurred in job {e.get('job_id')}"
            if not stale and e.get("strategy"):
                seg += f" (strategy {e['strategy']})"
            seg += f": outcome {e.get('outcome') or '?'}"
            if e.get("failure_class"):
                seg += f" (class: {e['failure_class']})"
            if e.get("superseded_by"):
                seg += f" [superseded by job {e['superseded_by']}]"
            if e.get("note"):
                seg += f"; {e['note']}"
            lines.append(seg)
    return lines


def ep_exc(e: dict) -> str:
    return str((e.get("fp_parts") or {}).get("exception") or "")
