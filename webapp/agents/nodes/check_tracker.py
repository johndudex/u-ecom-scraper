"""Check the site tracker and decide how to proceed.

Uses the ``Site`` Django model as the single source of truth.
"""

import logging
import os

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


def _clean_workspace(root: str, slug: str, keep_draft: bool = False) -> None:
    import shutil

    workspace_dir = os.path.join(root, "workspace", slug)
    scrapers_dir = os.path.join(root, "scrapers", slug)

    if os.path.isdir(workspace_dir):
        for fname in os.listdir(workspace_dir):
            # [wave-13 B1] ``keep_draft`` arms (re-drive re-entry) must not
            # destroy a draft THIS job wrote — setup_workspace's mtime guard
            # and per-job FM restore exist precisely for that work. The
            # force_full arm keeps keep_draft=False: a user-declared full
            # re-run must be able to kill a poisoned prior draft (job-12
            # class), so the sledgehammer stays available there.
            if keep_draft and fname == "scraper_draft.py":
                continue
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
            # [wave-13 B1] jobs/ is the per-job draft archive code_writer
            # snapshots to the FM and setup_workspace restores from — wiping
            # the local mirror destroyed the restore source mid-job. Never
            # sweep it here; FM-side retention is governed by artifacts.
            if fname == "jobs":
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


def _compute_rescrape_skip_flags(state, url: str):
    """Compute selective skip flags for a rescrape based on config diff vs the
    prior completed job for this URL. Returns (skip_site, skip_product, skip_code).

    - site_analyzer is ALWAYS skippable (reads zero config fields; the site
      structure hasn't changed).
    - product_analyzer is skippable unless nav/search changed (the page-level
      field map is invariant to target_fields changes — normalize_fields re-filters).
    - code_writer is skippable unless fields or nav changed (the generated
      scraper bakes in the field map).
    """
    try:
        from scraper.models import ScrapeJob

        prior = (
            ScrapeJob.objects
            .filter(url=url, status=ScrapeJob.STATUS_COMPLETED)
            .exclude(pk=state.get("job_id", 0))
            .order_by("-completed_at")
            .first()
        )
        if not prior:
            logger.info("_compute_rescrape_skip_flags: no prior completed job → full run")
            return False, False, False

        fields_changed = set(state.get("target_fields") or []) != set(prior.target_fields or [])
        nav_changed = (
            (state.get("input_mode") or "") != (prior.input_mode or "")
            or (state.get("search_criteria") or "") != (prior.search_criteria or "")
        )

        skip_site = True
        skip_product = not nav_changed
        skip_code = skip_product and not fields_changed

        logger.info(
            "_compute_rescrape_skip_flags: fields_changed=%s nav_changed=%s → skip(%s/%s/%s)",
            fields_changed, nav_changed, skip_site, skip_product, skip_code,
        )
        return skip_site, skip_product, skip_code
    except Exception as exc:
        logger.warning("_compute_rescrape_skip_flags: errored (%s) → full run", exc)
        return False, False, False


def _log_resume_invocation(state: ScrapeState, site_status: str, rescrape: bool) -> None:
    """[T3.13h] One SessionLog row when this invocation reuses a prior Site.

    During the 70-112 campaign, re-drives and new jobs against an EXISTING
    site were indistinguishable in the job log — "did this run re-inject the
    prior artifacts or start clean?" needed DB archaeology per job. Emitted
    once, at check_tracker, before any skip-flag resolution.
    """
    try:
        job_id = state.get("job_id")
        if not job_id:
            return
        from ..graph import _log_event_row

        _log_event_row(
            job_id,
            "check_tracker",
            "[RESUME-INVOCATION] site_status={status} rescrape={rescrape} "
            "force_full={force_full} input_mode={mode} — prior Site record "
            "reused (not a fresh site)".format(
                status=site_status,
                rescrape=rescrape,
                force_full=bool(state.get("force_full")),
                mode=state.get("input_mode", ""),
            ),
        )
    except Exception:
        pass


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
    site_type: str = state.get("site_type", "shopping")
    input_mode: str = state.get("input_mode", "")

    root = _get_project_root()
    site = _find_site(url)

    if site is None:
        return _handle_new_site(url, slug, site_type)

    _log_resume_invocation(state, site.status, rescrape)

    # ── Selective rescrape: skip stages whose inputs didn't change ───────
    # Instead of wiping + re-running everything, compute a config diff vs the
    # prior completed job for this URL and set selective skip flags. The
    # existing check_accessibility cascade + setup_workspace preserve-set
    # handle the rest. DON'T call _clean_workspace (the force_full arm below
    # is the ONE exception) — preserve the analysis archive in
    # scrapers/{slug}/analysis/ for setup_workspace to re-hydrate.
    if rescrape and site.status in ("complete", "in_progress"):
        if state.get("force_full"):
            # Full re-run (explicit user intent from the Full re-run button):
            # NO selective reuse, NO archive re-hydration. Wipe the workspace
            # AND the analysis archive (output_*.json history is kept) so a
            # killed/poisoned prior run can't re-inject stale artifacts
            # (job-12 class), then regenerate every phase. Note the flags must
            # be forced False HERE — _compute_rescrape_skip_flags would still
            # return True for a completed twin and setup_workspace re-hydrates
            # anything a True flag names.
            _clean_workspace(root, slug)
            skip_site = skip_product = skip_code = False
            logger.info(
                "check_tracker: FULL re-run for '%s' — workspace + analysis "
                "archive wiped, every phase regenerates", slug,
            )
        else:
            skip_site, skip_product, skip_code = _compute_rescrape_skip_flags(state, url)
            logger.info(
                "check_tracker: selective rescrape for '%s' → skip_site=%s skip_product=%s skip_code=%s",
                slug, skip_site, skip_product, skip_code,
            )
        if site.status != "in_progress":
            site.status = "in_progress"
            site.save(update_fields=["status"])
        return Command(
            update={
                "site_status": "in_progress",
                "skip_site_analysis": skip_site,
                "skip_product_analysis": skip_product,
                "skip_code_generation": skip_code,
            },
            goto="setup_workspace",
        )

    if site.status == "complete":
        return _handle_complete(site, slug, full_extraction, state.get("skip_approvals", False))
    if site.status == "failed":
        return _handle_failed(site, slug, state.get("skip_approvals", False))
    if site.status == "in_progress":
        return _handle_in_progress(site, slug, root, input_mode)

    logger.warning("check_tracker: unknown status '%s' for %s, treating as new", site.status, url)
    return _handle_new_site(url, slug, site_type)


def _handle_new_site(url: str, slug: str, site_type: str = "shopping") -> Command:
    try:
        from scraper.models import Site

        site = Site.objects.filter(url=url.rstrip("/")).first()
        if site:
            if site_type and not site.site_type:
                site.site_type = site_type
            site.status = "in_progress"
            site.save(update_fields=["status", "site_type"])
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
                site_type=site_type,
                status="in_progress",
            )
            logger.info("check_tracker: new site created → %s (type=%s)", slug, site_type)
    except Exception as exc:
        logger.warning("check_tracker: failed to create/update site: %s", exc)

    return Command(
        update={
            "site_status": "in_progress",
        },
        goto="setup_workspace",
    )


def _handle_complete(site, slug: str, full_extraction: bool, skip: bool = False) -> Command:
    logger.info("check_tracker: site '%s' already complete", slug)

    # Intake jobs run unattended — skip the re-scrape confirmation, proceed.
    if skip:
        logger.info("check_tracker: skip_approvals → auto re-scrape '%s'", slug)
        site.status = "in_progress"
        site.save(update_fields=["status"])
        return Command(
            update={"site_status": "in_progress", "human_response": {"decision": "approve"}},
            goto="setup_workspace",
        )

    if full_extraction:
        return Command(
            update={
                "site_status": "in_progress",                "skip_site_analysis": False,
                "skip_product_analysis": False,
                "skip_code_generation": False,
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


def _handle_failed(site, slug: str, skip: bool = False) -> Command:
    logger.info("check_tracker: site '%s' previously failed, asking user", slug)

    # Intake jobs run unattended — skip the retry confirmation, proceed.
    if skip:
        logger.info("check_tracker: skip_approvals → auto retry '%s'", slug)
        site.status = "in_progress"
        site.save(update_fields=["status"])
        return Command(
            update={"site_status": "in_progress", "human_response": {"decision": "approve"}},
            goto="setup_workspace",
        )

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


def _handle_in_progress(site, slug: str, root: str, input_mode: str = "") -> Command:
    logger.info("check_tracker: site '%s' in_progress from a previous run, checking for existing artifacts", slug)

    # For navigation mode, always start fresh — don't skip to product analysis
    # based on cached artifacts from a previous url_list run
    if input_mode == "navigation":
        logger.info(
            "check_tracker: navigation mode — starting fresh (ignoring cached artifacts)"
        )
        # keep_draft: a watchdog re-drive of a navigation job must not lose the
        # draft its own writer already produced (jobs-79/80 class).
        _clean_workspace(root, slug, keep_draft=True)
        return Command(
            update={
                "site_status": "in_progress",                "skip_site_analysis": False,
                "skip_product_analysis": False,
                "skip_code_generation": False,
            },
            goto="setup_workspace",
        )

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
                "site_status": "in_progress",                "skip_site_analysis": True,
                "skip_product_analysis": True,
                "skip_code_generation": True,
            },
            goto="setup_workspace",
        )

    if skip_site and skip_product:
        logger.info("check_tracker: site+product analysis exist for %s, skipping to code gen", slug)
        return Command(
            update={
                "site_status": "in_progress",                "skip_site_analysis": True,
                "skip_product_analysis": True,
                "skip_code_generation": False,
            },
            goto="setup_workspace",
        )

    if skip_site:
        logger.info("check_tracker: site analysis exists for %s, skipping to product analysis", slug)
        return Command(
            update={
                "site_status": "in_progress",                "skip_site_analysis": True,
                "skip_product_analysis": False,
                "skip_code_generation": False,
            },
            goto="setup_workspace",
        )

    logger.info("check_tracker: no artifacts for %s, starting from scratch", slug)
    _clean_workspace(root, slug, keep_draft=True)

    return Command(
        update={
            "site_status": "in_progress",
            "skip_site_analysis": False,
            "skip_product_analysis": False,
            "skip_code_generation": False,
        },
        goto="setup_workspace",
    )
