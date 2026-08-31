"""Draft-safety helpers shared by the code_writer / code_tester boundaries.

[wave-13 B1] Three facts about ``scraper_draft.py`` motivated this module:

1. The draft can go missing between the writer's write and the tester's read
   (watchdog re-drive, ephemeral-volume recycle, a wipe racing a cycle) — the
   tester then burns its cascade crashing on an absent file (58% of the
   campaign's test verdicts were missing-draft cycles).
2. A writer invocation that died mid-reply often LEFT the scraper in its last
   assistant message as a fenced code block, with no ``write_file`` call —
   the bytes were delivered, only the write never happened.
3. Every draft boundary check (isfile + ast.parse) and every per-job FM
   restore was previously re-implemented inline in 2-3 places.
"""

import ast
import logging
import os

logger = logging.getLogger(__name__)

# Upper bound on a fence-extracted draft (pathological-payload guard).
MAX_EXTRACTED_DRAFT_BYTES = 400_000

DRAFT_FILENAME = "scraper_draft.py"


def draft_path_for(root: str, slug: str) -> str:
    return os.path.join(root, "workspace", slug, DRAFT_FILENAME)


def draft_parses(path: str) -> bool:
    """Is ``path`` an existing, parseable Python file? (the draft floor)"""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            ast.parse(fh.read(), filename=path)
        return True
    except Exception:
        return False


def extract_fenced_python(text: str) -> str | None:
    """Return the LARGEST ```python fenced block from ``text`` that parses.

    Deterministic (no LLM): a dead writer invocation frequently delivered the
    complete scraper as a prose fence without ever calling write_file — the
    fix is to read the bytes back out of the message it already sent.

    Only ``python``-tagged fences are candidates (a bare ``` fence is prose
    more often than code). Each candidate must ast.parse — a truncated fence
    (the other failure mode) is rejected rather than half-shipped. Returns
    None when nothing usable is found.
    """
    if not text:
        return None
    blocks: list[str] = []
    for marker in ("```python", "```Python", "```py"):
        start = 0
        while True:
            i = text.find(marker, start)
            if i < 0:
                break
            j = text.find("```", i + len(marker))
            if j < 0:
                # Unterminated fence — the invocation died mid-fence. Take
                # what's there; the ast.parse gate below rejects truncation.
                blocks.append(text[i + len(marker):])
                break
            blocks.append(text[i + len(marker):j])
            start = j + 3
    best: str | None = None
    for block in blocks:
        code = block.strip("\n")
        if not code or len(code) > MAX_EXTRACTED_DRAFT_BYTES:
            continue
        try:
            ast.parse(code)
        except Exception:
            continue
        if best is None or len(code) > len(best):
            best = code
    return best


def restore_job_draft(root: str, slug: str, job_id) -> str | None:
    """Restore THIS job's own draft archive into the workspace.

    code_writer snapshots every completed draft to
    ``scrapers/{slug}/jobs/scraper-draft-{job_id}.py`` in the File Master.
    The key is per-job, so a fresh user re-run (new job id) can never inherit
    a stale draft from it. Returns the restore path on success, else None.
    """
    if not slug or not job_id:
        return None
    target = draft_path_for(root, slug)
    if draft_parses(target):
        return None  # nothing to restore over
    try:
        import src.artifacts as artifacts

        per_job_key = artifacts.scrapers_key(slug, "jobs", f"scraper-draft-{job_id}.py")
        if not artifacts.exists(per_job_key):
            return None
        payload = artifacts.read(per_job_key)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(payload)
        logger.info(
            "draft_safety: restored scraper_draft.py from THIS job's FM draft "
            "archive (job %s)", job_id,
        )
        return target
    except Exception as exc:
        logger.warning("draft_safety: per-job draft restore failed (job %s): %s", job_id, exc)
        return None
