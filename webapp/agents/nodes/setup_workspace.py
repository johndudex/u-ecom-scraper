"""Create the workspace and output directories required by downstream nodes."""

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


def _move_output_files(workspace_dir: str, scrapers_dir: str) -> int:
    moved = 0
    if not os.path.isdir(workspace_dir):
        return moved
    for fname in os.listdir(workspace_dir):
        if fname.startswith("output_") and fname.endswith(".json"):
            src = os.path.join(workspace_dir, fname)
            dst = os.path.join(scrapers_dir, fname)
            if os.path.isfile(src):
                try:
                    os.makedirs(scrapers_dir, exist_ok=True)
                    shutil.move(src, dst)
                    moved += 1
                except Exception as exc:
                    logger.warning("setup_workspace: failed to move %s: %s", src, exc)
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


def _restore_from_archive(analysis_dir: str, workspace_dir: str, filename: str) -> bool:
    """Copy an artifact from scrapers/{slug}/analysis/ to workspace/{slug}/
    if it exists in the archive but not in the workspace (re-hydration for
    selective re-run — the workspace was rmtree'd by _finalize_job)."""
    src = os.path.join(analysis_dir, filename)
    dst = os.path.join(workspace_dir, filename)
    if os.path.isfile(src) and not os.path.isfile(dst):
        try:
            shutil.copy2(src, dst)
            logger.info("setup_workspace: re-hydrated %s from analysis/", filename)
            return True
        except Exception as exc:
            logger.warning("setup_workspace: failed to re-hydrate %s: %s", filename, exc)
    return False


def setup_workspace(state: ScrapeState) -> dict[str, Any]:
    """Ensure ``workspace/{slug}/``, ``scrapers/{slug}/``, and ``logs/`` exist.

    All directory creation is idempotent (``exist_ok=True``).
    Moves output files to scrapers/ folder and removes stale artifacts
    while preserving analysis files that check_tracker said to skip.
    """
    slug = state["site_slug"]
    root = _get_project_root()

    workspace_dir = os.path.join(root, "workspace", slug)
    scrapers_dir = os.path.join(root, "scrapers", slug)

    dirs_to_create = [
        workspace_dir,
        scrapers_dir,
        os.path.join(root, "logs"),
    ]

    for d in dirs_to_create:
        os.makedirs(d, exist_ok=True)

    moved = _move_output_files(workspace_dir, scrapers_dir)
    if moved:
        logger.info("setup_workspace: moved %d output files to scrapers/%s/", moved, slug)

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
            scraper_in_final = os.path.join(scrapers_dir, "scraper.py")
            if os.path.isfile(scraper_in_final):
                try:
                    shutil.copy2(scraper_in_final, draft_in_ws)
                    logger.info(
                        "setup_workspace: restored scraper_draft.py from %s (cleanup had moved it)",
                        scraper_in_final,
                    )
                except Exception as exc:
                    logger.warning(
                        "setup_workspace: failed to restore scraper_draft.py: %s", exc
                    )

    # Re-hydrate analysis artifacts from scrapers/{slug}/analysis/ (the workspace
    # was rmtree'd by _finalize_job at the end of the prior run — the analysis/
    # archive preserves them for selective re-run reuse).
    analysis_dir = os.path.join(scrapers_dir, "analysis")
    if os.path.isdir(analysis_dir):
        if state.get("skip_site_analysis"):
            _restore_from_archive(analysis_dir, workspace_dir, "site_analysis.json")
        if state.get("skip_product_analysis"):
            _restore_from_archive(analysis_dir, workspace_dir, "product_analysis.json")
            _restore_from_archive(analysis_dir, workspace_dir, "navigation_analysis.json")
        if state.get("skip_code_generation"):
            _restore_from_archive(analysis_dir, workspace_dir, "scraper_analysis.json")
            _restore_from_archive(analysis_dir, workspace_dir, "test_report.json")

    input_urls = state.get("input_urls") or []
    if input_urls:
        input_path = os.path.join(workspace_dir, "input_urls.json")
        try:
            with open(input_path, "w", encoding="utf-8") as fh:
                json.dump({"urls": input_urls}, fh, indent=2, ensure_ascii=False)
            logger.info("setup_workspace: wrote %d URLs from Site model to %s", len(input_urls), input_path)
        except Exception as exc:
            logger.warning("setup_workspace: failed to write input_urls.json: %s", exc)

    # Fail-fast: a url_list job with NO urls anywhere (Site.input_urls empty AND
    # no production scrapers/{slug}/input_urls.json) would silently under-extract
    # (the misleading "1 of N coverage gap"). Fail loud with an actionable message
    # instead. (_build_initial_state already loaded the production file into state
    # if it existed, so empty state here means truly no URLs.)
    if (state.get("input_mode") == "url_list") and not input_urls:
        raise RuntimeError(
            f"url_list job for '{slug}' has no input URLs — Site.input_urls is empty "
            f"and no scrapers/{slug}/input_urls.json exists. Provide a URL list or use "
            f"a different input_mode (navigation/search_term)."
        )

    logger.info("setup_workspace: ensured directories for %s", slug)

    return {}
