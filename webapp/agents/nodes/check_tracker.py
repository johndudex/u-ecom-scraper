"""Check the site tracker and decide how to proceed.

Uses the ``Site`` Django model as the single source of truth.
"""

import logging
import os
from typing import Optional

from langgraph.types import Command, interrupt

from ..decisions import _parse_decision, build_decisions, is_cancel
from ..state import ScrapeState

logger = logging.getLogger(__name__)


def _get_project_root() -> str:
    try:
        from django.conf import settings
        return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _find_site(url: str):
    try:
        from scraper.models import Site
        return Site.objects.filter(url=url.rstrip("/")).first()
    except Exception as exc:
        logger.warning("check_tracker: could not query Site model: %s", exc)
        return None


def _clean_workspace(root: str, slug: str) -> None:
    import shutil

    workspace_dir = os.path.join(root, "workspace", slug)
    scrapers_dir = os.path.join(root, "scrapers", slug)

    if os.path.isdir(workspace_dir):
        for fname in os.listdir(workspace_dir):
            fpath = os.path.join(workspace_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
                elif os.path.isdir(fpath):
                    shutil.rmtree(fpath)
            except Exception as exc:
                logger.warning("check_tracker: failed to remove %s: %s", fpath, exc)
        logger.info("check_tracker: cleaned workspace/%s", slug)

    if os.path.isdir(scrapers_dir):
        for fname in os.listdir(scrapers_dir):
            if fname.startswith("output_") and fname.endswith(".json"):
                continue
            fpath = os.path.join(scrapers_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
                elif os.path.isdir(fpath):
                    shutil.rmtree(fpath)
            except Exception as exc:
                logger.warning("check_tracker: failed to remove %s: %s", fpath, exc)
        logger.info("check_tracker: cleaned scrapers/%s (kept output files)", slug)


def check_tracker(state: ScrapeState) -> Command:
    """Read the Site model, set skip-flags, and route appropriately.

    Handles four cases:

    1. **Site not found** — create a new ``Site`` entry with status
       ``in_progress`` and proceed to ``setup_workspace``.
    2. **Site complete** — if ``full_extraction`` is implied, auto-proceed;
       otherwise interrupt with reason ``re_scrape`` (HIP #1).
    3. **Site failed** — interrupt with reason ``retry_failed`` (HIP #2).
    4. **Site in_progress** — clean workspace and start fresh.
    """
    url = state["url"]
    slug = state["site_slug"]
    sample_only: bool = state.get("sample_only", False)
    full_extraction = not sample_only
    rescrape: bool = state.get("rescrape", False)

    root = _get_project_root()
    site = _find_site(url)

    if site is None:
        return _handle_new_site(url, slug)
    if site.status == "complete" and rescrape:
        logger.info("check_tracker: rescrape=True for complete site '%s', starting fresh", slug)
        _clean_workspace(root, slug)
        site.status = "in_progress"
        site.save(update_fields=["status"])
        return Command(
            update={
                "site_status": "in_progress",
                "current_phase": "check_tracker",
                "skip_site_analysis": False,
                "skip_product_analysis": False,
                "skip_code_generation": False,
            },
            goto="setup_workspace",
        )
    if site.status == "complete":
        return _handle_complete(site, slug, full_extraction)
    if site.status == "failed":
        return _handle_failed(site, slug)
    if site.status == "in_progress":
        return _handle_in_progress(site, slug, root)

    logger.warning("check_tracker: unknown status '%s' for %s, treating as new", site.status, url)
    return _handle_new_site(url, slug)


def _handle_new_site(url: str, slug: str) -> Command:
    try:
        from scraper.models import Site

        site = Site.objects.filter(url=url.rstrip("/")).first()
        if site:
            site.status = "in_progress"
            site.save(update_fields=["status"])
            logger.info(
                "check_tracker: existing site '%s' updated to in_progress (was %s)",
                slug,
                site.status,
            )
        else:
            Site.objects.create(
                url=url.rstrip("/"),
                name=slug,
                slug=slug,
                status="in_progress",
            )
            logger.info("check_tracker: new site created → %s", slug)
    except Exception as exc:
        logger.warning("check_tracker: failed to create/update site: %s", exc)

    return Command(
        update={
            "site_status": "in_progress",
            "current_phase": "check_tracker",
        },
        goto="setup_workspace",
    )


def _handle_complete(site, slug: str, full_extraction: bool) -> Command:
    logger.info("check_tracker: site '%s' already complete", slug)

    if full_extraction:
        return Command(
            update={
                "site_status": "complete",
                "current_phase": "check_tracker",
                "skip_site_analysis": True,
                "skip_product_analysis": True,
                "skip_code_generation": True,
            },
            goto="setup_workspace",
        )

    human_response = interrupt({
        "reason": "re_scrape",
        "message": f"Site '{slug}' was already scraped successfully. Re-scrape?",
        "site_entry": {"url": site.url, "status": site.status},
        "decisions": build_decisions(
            approve_label="Yes, re-scrape",
            reject_label="Cancel",
            reject_with_feedback=False,
        ),
    })

    decision = _parse_decision(human_response)
    if is_cancel(decision):
        return Command(
            update={
                "site_status": "complete",
                "human_response": decision,
            },
            goto="__end__",
        )

    site.status = "in_progress"
    site.save(update_fields=["status"])

    return Command(
        update={
            "site_status": "in_progress",
            "human_response": decision,
        },
        goto="setup_workspace",
    )


def _handle_failed(site, slug: str) -> Command:
    logger.info("check_tracker: site '%s' previously failed, asking user", slug)

    human_response = interrupt({
        "reason": "retry_failed",
        "message": f"Site '{slug}' previously failed. Retry from the beginning?",
        "site_entry": {"url": site.url, "status": site.status},
        "decisions": build_decisions(
            approve_label="Yes, retry",
            reject_label="Cancel",
            reject_with_feedback=False,
        ),
    })

    decision = _parse_decision(human_response)
    if is_cancel(decision):
        return Command(
            update={
                "site_status": "failed",
                "human_response": decision,
            },
            goto="__end__",
        )

    return Command(
        update={
            "site_status": "in_progress",
            "human_response": decision,
        },
        goto="setup_workspace",
    )


def _handle_in_progress(site, slug: str, root: str) -> Command:
    logger.info("check_tracker: site '%s' in_progress from a previous run, checking for existing artifacts", slug)

    workspace_dir = os.path.join(root, "workspace", slug)
    skip_site = os.path.isfile(os.path.join(workspace_dir, "site_analysis.json"))
    skip_product = os.path.isfile(os.path.join(workspace_dir, "product_analysis.json"))
    skip_code = os.path.isfile(os.path.join(workspace_dir, "scraper_draft.py"))

    if skip_site and skip_product and skip_code:
        logger.info(
            "check_tracker: all artifacts exist for %s, skipping to testing (skip_site=%s, skip_product=%s, skip_code=%s)",
            slug, skip_site, skip_product, skip_code,
        )
        return Command(
            update={
                "site_status": "in_progress",
                "current_phase": "check_tracker",
                "skip_site_analysis": True,
                "skip_product_analysis": True,
                "skip_code_generation": True,
            },
            goto="setup_workspace",
        )

    if skip_site and skip_product:
        logger.info("check_tracker: site+product analysis exist for %s, skipping to code gen", slug)
        return Command(
            update={
                "site_status": "in_progress",
                "current_phase": "check_tracker",
                "skip_site_analysis": True,
                "skip_product_analysis": True,
                "skip_code_generation": False,
            },
            goto="setup_workspace",
        )

    if skip_site:
        logger.info("check_tracker: site analysis exists for %s, skipping to product analysis", slug)
        return Command(
            update={
                "site_status": "in_progress",
                "current_phase": "check_tracker",
                "skip_site_analysis": True,
                "skip_product_analysis": False,
                "skip_code_generation": False,
            },
            goto="setup_workspace",
        )

    logger.info("check_tracker: no artifacts for %s, starting from scratch", slug)
    _clean_workspace(root, slug)

    return Command(
        update={
            "site_status": "in_progress",
            "current_phase": "check_tracker",
            "skip_site_analysis": False,
            "skip_product_analysis": False,
            "skip_code_generation": False,
        },
        goto="setup_workspace",
    )
