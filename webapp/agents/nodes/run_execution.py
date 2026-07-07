"""Run the generated scraper and capture output.

This node NEVER throws.  All exceptions are caught and recorded
in ``state["execution_status"]`` so that ``cleanup`` can always run.

For browser-based scrapers, dispatches to browser_service via HTTP.
For lightweight scrapers, runs in-process via subprocess.
"""

import json
import logging
import os
import subprocess
import time
from typing import Any, Optional

from ..state import ScrapeState

logger = logging.getLogger(__name__)

BROWSER_METHODS = {
    "undetected_chromedriver",
    "seleniumbase_uc",
    "playwright",
    "undetected_chromedriver_scraper",
    "stealth_browser",
    "uc_chrome",
}


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _get_browser_service_url() -> str:
    try:
        from django.conf import settings

        url = getattr(settings, "BROWSER_SERVICE_URL", "")
        if url:
            return url
    except Exception:
        pass
    return os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")


# Ground-truth markers: if the generated scraper imports a browser library it
# MUST run in browser_service (the celery container has no playwright/Chrome).
# The analyzer's `scraping_method` is often null for hybrid scrapers — e.g. a
# two-phase job-board scraper whose strategy is http_requests but whose phase 1
# imports playwright to drive a search form. Trusting the null field routes such
# scrapers in-process and they crash instantly on `import playwright`.
_BROWSER_IMPORT_MARKERS = (
    "import playwright",
    "from playwright",
    "import seleniumbase",
    "from seleniumbase",
    "undetected_chromedriver",
    "from selenium",
    "import selenium",
    "webdriver",
)


def _scraper_source_needs_browser(scraper_path: str) -> bool:
    """Inspect the generated scraper's imports — ground truth for browser need.

    Reads only the top of the file (imports + SCRAPING_METHOD live there).
    Generic: works for any scraper regardless of how the analyzer labeled it.
    """
    if not scraper_path or not os.path.isfile(scraper_path):
        return False
    try:
        with open(scraper_path, "r", errors="ignore") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    return any(marker in head for marker in _BROWSER_IMPORT_MARKERS)


def _needs_browser(state: ScrapeState, scraper_path: str = "") -> bool:
    if state.get("scraping_method", "") in BROWSER_METHODS:
        return True
    # Fallback: the analyzer field is unreliable (null for hybrids). The
    # scraper's own imports are the source of truth.
    return _scraper_source_needs_browser(scraper_path)


def _needs_cloak(state: ScrapeState) -> bool:
    """True when the site needs CloakBrowser stealth at scrape runtime.

    Anti-bot sites (Akamai/Cloudflare) block a plain Playwright Chromium, so the
    generated scraper must drive CloakBrowser's stealth Chromium (auto-applied via
    the browser_service cloak_stealth_patch .pth when STEALTH_BROWSER=cloak). This
    is checked from several state sources because the signal lives in different
    places depending on which node last wrote it (probe_result.anti_bot.detected,
    probe_result.method, connectivity.method_that_worked, or a denormalised
    anti_bot_detected flag). Generic — no site names.
    """
    if state.get("anti_bot_detected"):
        return True
    probe = state.get("probe_result") or {}
    if isinstance(probe, dict):
        ab = probe.get("anti_bot") or {}
        if isinstance(ab, dict) and ab.get("detected"):
            return True
        conn = probe.get("connectivity") or {}
        method = (
            probe.get("method")
            or (conn.get("method_that_worked") if isinstance(conn, dict) else "")
            or ""
        )
        if isinstance(method, str) and method.startswith(("uc_chrome", "cloak")):
            return True
    pm = state.get("probe_method") or ""
    if isinstance(pm, str) and pm.startswith(("uc_chrome", "cloak")):
        return True
    return False


def _stealth_env(state: ScrapeState) -> dict[str, str]:
    return {"STEALTH_BROWSER": "cloak"} if _needs_cloak(state) else {}


def run_execution(state: ScrapeState) -> dict:
    slug = state["site_slug"]
    root = _get_project_root()
    workspace_folder = os.path.join(root, "workspace", slug)
    scraper_path = os.path.join(workspace_folder, "scraper_draft.py")
    site_folder = os.path.join(root, "scrapers", slug)

    if not os.path.isfile(scraper_path):
        logger.error("run_execution: scraper not found at %s", scraper_path)
        return {
            "execution_status": "FAILED",
            "error_message": f"scraper_draft.py not found at {scraper_path}",
        }

    # NOTE: the FINAL execution always extracts the FULL result set (--sample is
    # only for code_tester validation). "sample_only" still skips the approval
    # gates for unattended runs, but must NOT cap the extraction — users expect
    # complete data (no item limits). [goal: full extraction / no limits]
    args = []

    input_mode = state.get("input_mode", "")
    search_criteria = state.get("search_criteria", "")
    if input_mode in ("navigation", "list_page", "search_term") and search_criteria:
        args.extend(["--query", search_criteria])
        logger.info(
            "run_execution: navigation job, passing --query '%s'", search_criteria
        )

    if _needs_browser(state, scraper_path):
        logger.info("run_execution: browser-based scraper, dispatching to browser_service")
        result = _run_via_browser_service(scraper_path, args, site_folder, state)

        # MULTI-SOURCE: for navigation jobs, also run the scraper against
        # watch-related category pages. A single search page often returns
        # fewer products than the site has. This re-runs with --category-url
        # for each relevant category + merges the output. Generic.
        if input_mode in ("navigation", "list_page", "search_term") and search_criteria:
            result = _run_category_sources(
                state, scraper_path, args, site_folder, result, search_criteria
            )

        return result

    return _run_in_process(scraper_path, args, root, site_folder, workspace_folder)


def _run_category_sources(state, scraper_path, base_args, site_folder, primary_result, search_term):
    """Run the scraper against category pages related to the search term + merge.

    Generic: reads category URLs from navigation_findings.json, filters to those
    containing the search term, runs the scraper with --category-url for each,
    and merges new products into the primary result. [multi-source discovery]
    """
    import json as _json
    import os as _os
    import httpx

    try:
        slug = state.get("site_slug", "")
        root = _os.environ.get("PROJECT_ROOT", "/app")
        nf_path = _os.path.join(root, "workspace", slug, "navigation_findings.json")
        if not _os.path.isfile(nf_path):
            return primary_result

        nf = _json.load(open(nf_path))
        # Get category URLs from homepage_nav.category_links
        hp = nf.get("homepage_nav") or {}
        cat_links = hp.get("category_links") or []
        # Also check navigation_analysis categories
        na_path = _os.path.join(root, "workspace", slug, "navigation_analysis.json")
        if _os.path.isfile(na_path):
            na = _json.load(open(na_path))
            na_cats = na.get("categories") or []
            if isinstance(na_cats, list):
                cat_links = list(cat_links) + list(na_cats)

        # Filter to categories related to the search term
        term = search_term.lower()
        relevant = []
        seen = set()
        for cat in cat_links:
            cat_url = cat if isinstance(cat, str) else (cat.get("url", "") if isinstance(cat, dict) else "")
            if not cat_url or cat_url in seen:
                continue
            if term in cat_url.lower() or (isinstance(cat, dict) and term in str(cat.get("name", "")).lower()):
                relevant.append(cat_url)
                seen.add(cat_url)

        if not relevant:
            logger.info("multisource: no category pages matching '%s'", search_term)
            return primary_result

        logger.info("multisource: %d category pages match '%s'", len(relevant), search_term)

        # Load primary output products
        ct_config = state.get("content_type_config") or {}
        output_key = ct_config.get("output_key", "products") if ct_config else "products"

        primary_file = primary_result.get("output_file", "")
        primary_products = []
        if primary_file and _os.path.isfile(primary_file):
            primary_data = _json.load(open(primary_file))
            primary_products = primary_data.get(output_key, [])

        existing_urls = set(p.get("url", "") for p in primary_products)
        all_products = list(primary_products)
        service_url = _os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")
        stealth_env = _stealth_env(state)

        for cat_url in relevant[:5]:  # max 5 category pages
            logger.info("multisource: running scraper on category %s", cat_url[:60])
            try:
                cat_args = ["--category-url", cat_url] + [a for a in base_args if a != "--query" and not base_args[base_args.index(a)-1] == "--query" if a != search_term]
                # simpler: just --category-url
                cat_args = ["--category-url", cat_url]
                resp = httpx.post(
                    f"{service_url}/scrape",
                    json={"scraper_path": scraper_path, "args": cat_args, "timeout": 600, "env_overrides": stealth_env},
                    timeout=620,
                )
                cat_result = resp.json()
                cat_output = cat_result.get("output_file", "")
                if cat_output and _os.path.isfile(cat_output):
                    cat_data = _json.load(open(cat_output))
                    cat_products = cat_data.get(output_key, [])
                    new_count = 0
                    for p in cat_products:
                        url = p.get("url", "")
                        if url and url not in existing_urls:
                            if p.get("price"):  # only priced products
                                all_products.append(p)
                                existing_urls.add(url)
                                new_count += 1
                    if new_count:
                        logger.info("multisource: %s → %d new products (total %d)", cat_url[:40], new_count, len(all_products))
            except Exception as exc:
                logger.warning("multisource: category %s failed: %s", cat_url[:40], exc)

        # Write merged output
        if len(all_products) > len(primary_products):
            from datetime import datetime, timezone
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            merged_path = _os.path.join(site_folder, f"output_merged_{timestamp}.json")
            site_block = primary_data.get("site", {}) if primary_file else {}
            merged = {
                "site": site_block,
                output_key: all_products,
                "metadata": {
                    "scraping_method": "multisource_playwright",
                    "sources": 1 + min(len(relevant), 5),
                    "total_products": len(all_products),
                    "merged": True,
                },
            }
            with open(merged_path, "w") as f:
                _json.dump(merged, f, indent=2, ensure_ascii=False, default=str)
            logger.info("multisource: merged output → %d total products → %s", len(all_products), merged_path)
            return {
                "execution_status": "SUCCESS",
                "output_file": merged_path,
                "product_count": len(all_products),
                "error_message": "",
            }

        return primary_result
    except Exception as exc:
        logger.warning("multisource: failed: %s", exc)
        return primary_result


def _run_in_process(
    scraper_path: str, args: list[str], cwd: str, site_folder: str,
    workspace_folder: str = "",
) -> dict[str, Any]:
    cmd = ["python3", scraper_path] + args
    start = time.time()

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600, cwd=cwd,
        )

        elapsed = round(time.time() - start, 2)
        logger.info(
            "run_execution: scraper exited with code %d in %ds", result.returncode, elapsed
        )

        if result.returncode != 0:
            stderr = result.stderr[:2000] if result.stderr else ""
            return {
                "execution_status": "FAILED",
                "error_message": f"Scraper exited with code {result.returncode}. {stderr}",
            }

        output_file = _find_output_file(site_folder)
        if not output_file and workspace_folder:
            output_file = _find_output_file(workspace_folder)
        product_count = _count_products(output_file) if output_file else 0

        return {
            "execution_status": "SUCCESS",
            "output_file": output_file or "",
            "product_count": product_count,
            "error_message": "",
        }

    except subprocess.TimeoutExpired:
        logger.error("run_execution: scraper timed out after 3600s")
        return {
            "execution_status": "FAILED",
            "error_message": "Scraper timed out (3600s limit)",
        }
    except Exception as exc:
        logger.exception("run_execution: unexpected error")
        return {
            "execution_status": "FAILED",
            "error_message": str(exc),
        }


def _run_via_browser_service(
    scraper_path: str, args: list[str], site_folder: str, state: ScrapeState | None = None
) -> dict[str, Any]:
    import httpx

    service_url = _get_browser_service_url()
    stealth_env = _stealth_env(state) if state else {}
    logger.info(
        "run_execution: dispatching to browser_service at %s: %s (cloak=%s)",
        service_url,
        scraper_path,
        bool(stealth_env),
    )

    timeout = 7200
    # The /scrape call blocks for the whole run (a 60-100 item scrape takes
    # 15-20 min). The celery watchdog (cleanup_stuck_jobs) kills jobs with no
    # SessionLog activity for STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES, so without a
    # heartbeat it would mark this job failed mid-scrape. Reuse the graph
    # heartbeat to keep the watchdog informed. Lazy import avoids a circular
    # dependency (graph imports this node module).
    hb = None
    try:
        from webapp.agents.graph import _start_heartbeat, _stop_heartbeat

        _job_id = (state or {}).get("job_id", 0)
        if _job_id:
            hb = _start_heartbeat(_job_id, "run_execution", interval=240)
    except Exception as _hb_exc:
        logger.warning("run_execution: heartbeat start failed: %s", _hb_exc)

    try:
        resp = httpx.post(
            f"{service_url}/scrape",
            json={
                "scraper_path": scraper_path,
                "args": args,
                "timeout": timeout,
                "env_overrides": stealth_env,
            },
            timeout=timeout + 60,
        )

        if resp.status_code == 404:
            return {
                "execution_status": "FAILED",
                "error_message": f"Scraper not found on browser_service: {scraper_path}",
            }

        resp.raise_for_status()
        result = resp.json()

        if result.get("returncode", 0) != 0:
            stderr = result.get("stderr", "")[:2000]
            return {
                "execution_status": "FAILED",
                "error_message": f"Scraper exited with code {result['returncode']}. {stderr}",
            }

        return {
            "execution_status": "SUCCESS",
            "output_file": result.get("output_file", ""),
            "product_count": result.get("product_count", 0),
            "error_message": "",
        }

    except httpx.ConnectError:
        return {
            "execution_status": "FAILED",
            "error_message": f"browser_service ({service_url}) is unreachable",
        }
    except httpx.TimeoutException:
        return {
            "execution_status": "FAILED",
            "error_message": f"Scraper timed out on browser_service after {timeout}s",
        }
    except Exception as exc:
        logger.exception("run_execution: browser_service dispatch failed")
        return {
            "execution_status": "FAILED",
            "error_message": f"browser_service dispatch failed: {exc}",
        }
    finally:
        if hb is not None:
            try:
                _stop_heartbeat(hb)
            except Exception:
                pass


def _find_output_file(site_folder: str) -> str:
    if not os.path.isdir(site_folder):
        return ""
    candidates = sorted(
        [
            os.path.join(site_folder, f)
            for f in os.listdir(site_folder)
            if f.startswith("output_") and f.endswith(".json")
        ]
    )
    return candidates[-1] if candidates else ""


def _count_products(output_path: str) -> int:
    """Count extracted items in an output file, across content types.

    Outputs use different keys by domain (products/jobs/articles/results/...).
    Count the first non-empty list so the job's ``product_count`` reflects
    reality regardless of content type.  Generic. [summary correctness]
    """
    if not output_path or not os.path.isfile(output_path):
        return 0
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    return len(val)
        return 0
    except Exception:
        return 0
