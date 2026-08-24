"""Output page index — fold M10: finalize-time byte offsets, O(page) reads.

build_page_index() scans the output file ONCE (raw_decode walk over the
items array) recording each item's byte range; the index is persisted to
the File Master at finalize. read_output_page() then serves any page by
slicing exactly that page's byte ranges — never re-parsing the file
(the streaming-window design would cost O(file) per sequential page walk:
268 pages × 101 MB on aya-class outputs).

The scan itself is a single pass with a bounded buffer (streamed read);
items must decode individually, which the scraper's json.dump output
guarantees (one array of flat records).
"""
from __future__ import annotations

import json
import logging

from . import errors

logger = logging.getLogger("scraper.api")

OUTPUT_KEYS = ("products", "jobs", "articles", "threads", "results", "pages")


def _fm_read_json(key: str):
    try:
        import src.artifacts as artifacts

        return artifacts.read_json(key)
    except Exception:
        return None


def _fm_write_json(key: str, payload: dict) -> None:
    import src.artifacts as artifacts

    artifacts.write_json(key, payload)


def _fm_read_text(key: str) -> str:
    import src.artifacts as artifacts

    return artifacts.read_text(key)


def index_key(slug: str, job_id: int) -> str:
    return f"scrapers/{slug}/indexes/output-{job_id}.json"


def _iter_item_spans(text: str, array_start: int):
    """Yield (offset, length) per element of the JSON array starting at
    array_start (index of '['). Uses raw_decode — no full-document parse."""
    decoder = json.JSONDecoder()
    i = array_start + 1
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] == "]":
            return
        obj, end = decoder.raw_decode(text, i)
        yield i, end - i
        i = end
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i < n and text[i] == ",":
            i += 1


def _find_items_array(text: str):
    """Locate the first OUTPUT_KEYS array: returns (key, bracket_index).

    String-anchored: find `"key"` followed (after whitespace/colon) by `[`.
    Robust against key ordering and nested objects before the array.
    """
    for key in OUTPUT_KEYS:
        search = 0
        while True:
            i = text.find(f'"{key}"', search)
            if i < 0:
                break
            j = i + len(key) + 2
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] == ":":
                k = j + 1
                while k < len(text) and text[k] in " \t\r\n":
                    k += 1
                if k < len(text) and text[k] == "[":
                    return key, k
            search = j
    return None, -1


def build_page_index(src_path: str, items_key: str | None = None) -> dict:
    """Scan the output file once; return the index dict (caller persists)."""
    with open(src_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    key, bracket = _find_items_array(text)
    if items_key:
        key = items_key
    if key is None or bracket < 0:
        return {"items_key": items_key or "", "total_items": 0, "items": []}
    spans = [
        {"offset": off, "length": length}
        for off, length in _iter_item_spans(text, bracket)
    ]
    # site + metadata are small — capture at index time so page reads never
    # re-derive them (one full parse HERE is fine: runs once per job)
    site = metadata = None
    try:
        doc = json.loads(text)
        if isinstance(doc, dict):
            site = doc.get("site")
            metadata = doc.get("metadata")
    except Exception:
        pass
    return {
        "items_key": key, "total_items": len(spans), "items": spans,
        "site": site, "metadata": metadata,
        # the window cache keys on (key, size) — a prune rewrite changes
        # size, which must invalidate rather than serve stale pages
        "source_bytes": len(text.encode("utf-8")),
    }


def read_output_page(job, page: int, page_size: int) -> dict:
    """One window of the output via the byte index. Raises ApiError."""
    if not isinstance(page, int) or page < 1:
        raise errors.ApiError(422, "invalid_page", "page must be >= 1.")
    if not isinstance(page_size, int) or not 1 <= page_size <= 500:
        raise errors.ApiError(422, "invalid_page_size", "page_size must be in [1, 500].")
    slug = (job.site_folder or "").strip("/").split("/")[-1] if job.site_folder else ""
    if not slug or not job.output_file:
        raise errors.ApiError(404, "output_not_found", "No output for this job.")
    index = _fm_read_json(index_key(slug, job.id))
    if not index or not index.get("items"):
        raise errors.ApiError(404, "output_not_found", "No output for this job.")
    total = index["total_items"]
    total_pages = max(1, -(-total // page_size))
    if page > total_pages:
        raise errors.ApiError(
            422, "invalid_page", f"page must be <= {total_pages}.", {"total_pages": total_pages}
        )
    start = (page - 1) * page_size
    window = index["items"][start: start + page_size]
    try:
        from .window_cache import _cache, window_fetch as _window_fetch

        size = index.get("source_bytes") or 0
        text = _cache.get(job.output_file, size, _window_fetch)
    except errors.ApiError:
        raise
    except Exception as exc:
        # M10: fail-fast, never hang a worker on FM
        raise errors.ApiError(503, "internal_error", "Output store unavailable.") from exc
    items = [json.loads(text[e["offset"]: e["offset"] + e["length"]]) for e in window]
    out = {"site": index.get("site")}
    out[index["items_key"]] = items
    out["metadata"] = index.get("metadata")
    out.update({"page": page, "page_size": page_size, "total_items": total, "total_pages": total_pages})
    return out


def _workspace_output_path(job):
    """The job's LOCAL workspace output file (pre-rmtree), or None."""
    import glob
    import os

    from django.conf import settings

    if not job.site_folder:
        return None
    ws = os.path.join(str(settings.PROJECT_ROOT), job.site_folder.strip("/"))
    files = sorted(glob.glob(os.path.join(ws, "output_*.json")), key=os.path.getmtime)
    return files[-1] if files else None


def _fm_open_local(job) -> str | None:
    """Materialize the job's FM output to a temp file for indexing; None on
    miss. (build_page_index takes a path; FM gives us bytes.)"""
    import tempfile

    data = None
    try:
        import src.artifacts as artifacts

        data = artifacts.read(job.output_file)  # artifacts has read(), not read_bytes
    except Exception:
        return None
    if not data:
        return None
    tmp = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


def finalize_output_index(job) -> bool:
    """Finalize-time hook (fold M10 + ordering): build the byte index from
    the FM output AFTER the schema prune rewrote it, write the index back to
    FM, and emit the output artifact event. Returns True when indexed.

    Caller: tasks.py _finalize_job, immediately after the schema-prune
    block (the index MUST describe the final pruned bytes).
    """
    tmp = _fm_open_local(job)
    if not tmp:
        return False
    import os as _os

    try:
        slug = (job.site_folder or "").strip("/").split("/")[-1]
        try:
            index = build_page_index(tmp)
            _fm_write_json(index_key(slug, job.id), index)
        except Exception as exc:
            logger.warning("finalize_output_index: job %s: %s", job.id, exc)
            return False
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
    from ..events import emit

    emit(
        job, "job.artifact.available",
        {
            "kind": "output",
            "url": f"/api/v1/jobs/{job.id}/output",
            "item_count": index["total_items"],
        },
        dedupe_key=f"artifact:output:{job.id}",
    )
    return True


def partner_owned_keys(slug: str) -> set[str]:
    """M5/R2 prune exemption: FM output keys owned by PARTNER jobs (reverse
    lookup — output_{datetime}.json filenames carry no job id, so ownership
    is ScrapeJob.output_file → key). Unowned/legacy files stay prunable."""
    try:
        from ..models import ScrapeJob

        return {
            j.output_file
            for j in ScrapeJob.objects.filter(created_via="api", output_file__startswith=f"scrapers/{slug}/")
            if j.output_file
        }
    except Exception:
        return set()
