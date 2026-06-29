"""Run the generated scraper and capture output.

This node NEVER throws.  All exceptions are caught and recorded
in ``state["execution_status"]`` so that ``cleanup`` can always run.

For browser-based scrapers, dispatches to browser-service via HTTP.
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
    return os.environ.get("BROWSER_SERVICE_URL", "http://browser-service:8001")


def _needs_browser(state: ScrapeState) -> bool:
    return state.get("scraping_method", "") in BROWSER_METHODS


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

    args = []
    if state.get("sample_only", False):
        args.append("--sample")

    input_mode = state.get("input_mode", "")
    search_criteria = state.get("search_criteria", "")
    if input_mode in ("navigation", "list_page", "search_term") and search_criteria:
        args.extend(["--query", search_criteria])
        logger.info(
            "run_execution: navigation job, passing --query '%s'", search_criteria
        )

    if _needs_browser(state):
        logger.info("run_execution: browser-based scraper, dispatching to browser-service")
        return _run_via_browser_service(scraper_path, args, site_folder)

    return _run_in_process(scraper_path, args, root, site_folder, workspace_folder)


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
    scraper_path: str, args: list[str], site_folder: str
) -> dict[str, Any]:
    import httpx

    service_url = _get_browser_service_url()
    logger.info(
        "run_execution: dispatching to browser-service at %s: %s", service_url, scraper_path
    )

    timeout = 7200
    try:
        resp = httpx.post(
            f"{service_url}/scrape",
            json={
                "scraper_path": scraper_path,
                "args": args,
                "timeout": timeout,
            },
            timeout=timeout + 60,
        )

        if resp.status_code == 404:
            return {
                "execution_status": "FAILED",
                "error_message": f"Scraper not found on browser-service: {scraper_path}",
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
            "error_message": f"browser-service ({service_url}) is unreachable",
        }
    except httpx.TimeoutException:
        return {
            "execution_status": "FAILED",
            "error_message": f"Scraper timed out on browser-service after {timeout}s",
        }
    except Exception as exc:
        logger.exception("run_execution: browser-service dispatch failed")
        return {
            "execution_status": "FAILED",
            "error_message": f"browser-service dispatch failed: {exc}",
        }


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
    if not output_path or not os.path.isfile(output_path):
        return 0
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        products = data.get("products", [])
        return len(products)
    except Exception:
        return 0
