"""Create the workspace and output directories required by downstream nodes.

The LOCAL ``workspace/{slug}/`` tree stays on the worker's own volume (internal
pipeline scratch). Published artifacts (``scrapers/``) live in the File Master —
so workspace↔scrapers moves are now local↔FM round-trips.
"""

import json
import logging
import os
import shutil
from typing import Any

from ..state import ScrapeState

logger = logging.getLogger(__name__)

PRESERVE_FILES: set[str] = set()


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _publish_leftover_outputs(workspace_dir: str, slug: str) -> int:
    """Publish any leftover workspace output_*.json to the File Master.

    Safety net for outputs left in workspace by a crashed prior run (the normal
    publish happens in _finalize_job). Reads local, writes FM key, removes local.
    """
    import src.artifacts as artifacts

    moved = 0
    if not os.path.isdir(workspace_dir):
        return moved
    for fname in os.listdir(workspace_dir):
        if fname.startswith("output_") and fname.endswith(".json"):
            src = os.path.join(workspace_dir, fname)
            if os.path.isfile(src):
                try:
                    with open(src, "rb") as _f:
                        artifacts.write(artifacts.scrapers_key(slug, fname), _f.read())
                    os.remove(src)
                    moved += 1
                except Exception as exc:
                    logger.warning("setup_workspace: failed to publish %s: %s", src, exc)
    return moved


def _clean_stale_artifacts(workspace_dir: str, preserve: set[str] | None = None) -> int:
    removed = 0
    if not os.path.isdir(workspace_dir):
        return removed
    skip = preserve if preserve is not None else PRESERVE_FILES
    for fname in os.listdir(workspace_dir):
        if fname not in skip:
            fpath = os.path.join(workspace_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    removed += 1
                elif os.path.isdir(fpath):
                    shutil.rmtree(fpath)
                    removed += 1
            except Exception as exc:
                logger.warning("setup_workspace: failed to remove %s: %s", fpath, exc)
    return removed


def _restore_from_archive(
    slug: str,
    workspace_dir: str,
    filename: str,
    status: dict[str, str] | None = None,
) -> bool:
    """Re-hydrate an artifact from the File Master (scrapers/{slug}/analysis/)
    into the LOCAL workspace, if the workspace copy is missing (selective re-run
    — the workspace was rmtree'd by _finalize_job).

    M4 copy-path guard: a corrupt FM copy is quarantined (``.corrupt``) rather
    than faithfully restored — re-hydrating corrupt bytes is how corruption
    became durable across jobs (job 10's author consumed a poisoned artifact
    this way). Repairable bytes land repaired; unrepairable bytes never land.

    S5 salvage honesty: repair passes 2/2b/3 are LOSSY (content after the
    parse-error position is dropped; every lossy note says "salvage"). A
    lossy-salvaged FM copy is REFUSED and ``status[filename]`` is set to
    ``"salvage-refused"`` so the caller can clear the skip flag — the phase
    must re-run fresh rather than trust a 1-of-N artifact (job-12 round 2:
    stale-artifact re-injection). Lossless repairs (0b control chars, 1 bad
    escapes) still re-hydrate repaired."""
    import src.artifacts as artifacts

    dst = os.path.join(workspace_dir, filename)
    if os.path.isfile(dst):
        return False
    key = artifacts.scrapers_key(slug, "analysis", filename)
    try:
        if artifacts.exists(key):
            _bytes = artifacts.read(key)
            if filename.endswith(".json"):
                try:
                    from ..tools.filesystem_tools import guard_json_bytes
                except Exception as exc:  # pragma: no cover
                    logger.warning(
                        "setup_workspace: guard unavailable for %s: %s", filename, exc
                    )
                    guard_json_bytes = None
                if guard_json_bytes is not None:
                    guarded, note = guard_json_bytes(_bytes)
                    if guarded is None:
                        logger.error(
                            "setup_workspace: FM copy of %s is corrupt and "
                            "unrepairable (%s) — NOT re-hydrated; downstream "
                            "will treat the artifact as missing", filename, note,
                        )
                        return False
                    if note and "salvage" in note:
                        if status is not None:
                            status[filename] = "salvage-refused"
                        logger.error(
                            "setup_workspace: FM copy of %s is a LOSSY salvage "
                            "(%s) — NOT re-hydrated; clearing the skip flag so "
                            "the phase re-runs fresh", filename, note,
                        )
                        return False
                    if note:
                        logger.warning(
                            "setup_workspace: FM copy of %s was corrupt — "
                            "re-hydrating REPAIRED version (%s)", filename, note,
                        )
                    _bytes = guarded
            os.makedirs(workspace_dir, exist_ok=True)
            with open(dst, "wb") as _f:
                _f.write(_bytes)
            logger.info("setup_workspace: re-hydrated %s from FM analysis/", filename)
            return True
    except Exception as exc:
        logger.warning("setup_workspace: failed to re-hydrate %s: %s", filename, exc)
    return False


def setup_workspace(state: ScrapeState) -> dict[str, Any]:
    """Ensure ``workspace/{slug}/`` and ``logs/`` exist (LOCAL), publish any
    leftover outputs to the File Master, and re-hydrate skipped analysis
    artifacts from FM for selective re-runs.

    All directory creation is idempotent (``exist_ok=True``). ``workspace/`` is
    the worker's own scratch volume; ``scrapers/`` lives in the File Master.
    """
    slug = state["site_slug"]
    root = _get_project_root()

    workspace_dir = os.path.join(root, "workspace", slug)

    for d in (workspace_dir, os.path.join(root, "logs")):
        os.makedirs(d, exist_ok=True)

    moved = _publish_leftover_outputs(workspace_dir, slug)
    if moved:
        logger.info("setup_workspace: published %d leftover output files to FM for %s", moved, slug)

    skip_files: set[str] = set(PRESERVE_FILES)
    if state.get("skip_site_analysis"):
        skip_files.add("site_analysis.json")
    if state.get("skip_product_analysis"):
        skip_files.add("product_analysis.json")
    if state.get("skip_code_generation"):
        skip_files.add("scraper_draft.py")

    removed = _clean_stale_artifacts(workspace_dir, skip_files)
    if removed:
        logger.info("setup_workspace: cleaned %d stale artifacts from %s", removed, slug)

    if state.get("skip_code_generation"):
        draft_in_ws = os.path.join(workspace_dir, "scraper_draft.py")
        if not os.path.isfile(draft_in_ws):
            import src.artifacts as artifacts
            try:
                _key = artifacts.scrapers_key(slug, "scraper.py")
                if artifacts.exists(_key):
                    with open(draft_in_ws, "wb") as _f:
                        _f.write(artifacts.read(_key))
                    logger.info(
                        "setup_workspace: restored scraper_draft.py from FM scraper.py (skip_code_generation)"
                    )
            except Exception as exc:
                logger.warning("setup_workspace: failed to restore scraper_draft.py: %s", exc)

    # Re-hydrate analysis artifacts from FM (scrapers/{slug}/analysis/) for the
    # selective re-run case (workspace was rmtree'd by _finalize_job last run).
    # S5: when the only FM copy is a LOSSY salvage, the restore refuses — clear
    # the governing skip flag so the producing phase re-runs instead of the job
    # consuming a 1-of-N artifact under a "skip" it no longer earned.
    update: dict[str, Any] = {}

    def _restore(filename: str, flag: str) -> None:
        _status: dict[str, str] = {}
        _restore_from_archive(slug, workspace_dir, filename, _status)
        if _status.get(filename) == "salvage-refused":
            update[flag] = False

    if state.get("skip_site_analysis"):
        _restore("site_analysis.json", "skip_site_analysis")
    if state.get("skip_product_analysis"):
        _restore("product_analysis.json", "skip_product_analysis")
        _restore("navigation_analysis.json", "skip_product_analysis")
    if state.get("skip_code_generation"):
        _restore("scraper_analysis.json", "skip_code_generation")
        _restore("test_report.json", "skip_code_generation")

    input_urls = state.get("input_urls") or []
    if input_urls:
        input_path = os.path.join(workspace_dir, "input_urls.json")
        try:
            with open(input_path, "w", encoding="utf-8") as fh:
                json.dump({"urls": input_urls}, fh, indent=2, ensure_ascii=False)
            logger.info("setup_workspace: wrote %d URLs from Site model to %s", len(input_urls), input_path)
        except Exception as exc:
            logger.warning("setup_workspace: failed to write input_urls.json: %s", exc)

    # Fail-fast: a url_list job with NO urls anywhere would silently under-extract.
    if (state.get("input_mode") == "url_list") and not input_urls:
        raise RuntimeError(
            f"url_list job for '{slug}' has no input URLs — Site.input_urls is empty "
            f"and no scrapers/{slug}/input_urls.json exists. Provide a URL list or use "
            f"a different input_mode (navigation/search_term)."
        )

    logger.info("setup_workspace: ensured directories for %s", slug)

    return update
