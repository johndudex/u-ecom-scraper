import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.environ.get("PROJECT_ROOT", "/app")
DISPLAY = os.environ.get("DISPLAY", ":98")
BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://127.0.0.1:8001")

# ── Chrome crash detection ──────────────────────────────────────────────
# When a scraper dies because Chrome became unresponsive/closed (common on
# long browser sessions — e.g. iterating 200+ specialties), the service can
# restart Chrome + retry. Code bugs (Python Traceback) are NOT retried
# (left to code_tester). [browser resilience: B-service]

# Real Chrome/CDP PROCESS death — the browser/tab closed or the endpoint died.
# Only these benefit from a Chrome restart + retry (a fresh browser recovers
# them). Per-page errors are deliberately EXCLUDED (see below).
_CHROME_CRASH_MARKERS = (
    "Target page, has been closed",
    "Target closed",
    "Browser has been closed",
    "Browser closed",
    "connect ECONNREFUSED",
    "playwright._impl._api_types.Error: Target",
    "Page.goto: Target closed",
    "Execution context was destroyed",
)
# Deliberately EXCLUDED — these are per-page/network errors, NOT Chrome crashes,
# so a Chrome restart + full-scrape retry cannot fix them (the same URL is still
# slow/down). Retrying just burns retries × the time limit and wedges the shared
# Chrome for the next caller:
#   "net::ERR_CONNECTION_"      — one URL failed to load (site slow/down/blocked)
#   "Navigation failed because" — single-page navigation failure
#   "CDP"                       — far too broad (matches any CDP log line)

_PYTHON_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")


def _is_chrome_death(stderr: str) -> bool:
    """True if stderr indicates a Chrome/CDP crash (retryable), not a code bug."""
    if not stderr:
        return False
    return any(marker in stderr for marker in _CHROME_CRASH_MARKERS)


def _has_traceback(stderr: str) -> bool:
    """True if stderr contains a Python Traceback (code bug — don't retry)."""
    return bool(_PYTHON_TRACEBACK_RE.search(stderr or ""))


def _restart_scraper_chrome() -> None:
    """Restart the scraper Chrome instance to free resources for the script."""
    try:
        resp = httpx.post(
            f"{BROWSER_SERVICE_URL}/restart-cdp",
            json={"label": "scraper"},
            timeout=30,
        )
        if resp.status_code == 200:
            logger.info("Restarted scraper Chrome before scraper run")
            time.sleep(2)
        else:
            logger.warning("Failed to restart scraper Chrome: %s", resp.text[:200])
    except Exception as exc:
        logger.warning("Could not restart scraper Chrome: %s", exc)


def _delete_discovery_checkpoint(scraper_dir: str) -> None:
    """Delete ``discovered_urls_checkpoint.json`` after a successful run (H3).

    Without this, a checkpoint written during one run (e.g. code_tester's
    capped sample) persists and the next invocation loads it, skipping Phase 1
    and silently extracting only the checkpointed URLs (locumtenens 38-of-3771
    bug). The scraper writes the checkpoint to SCRIPT_DIR (=
    os.path.dirname(scraper_path)) and this container runs the scraper with
    cwd=scraper_dir, so both resolve here. Silent no-op when absent.
    [discovery-coverage-gate §4]

    IMPORTANT: only call after a SUCCESSFUL run. The Chrome-crash retry path
    (returncode != 0) reloads the checkpoint to resume mid-run — deleting it
    before a retry would discard discovered URLs and redo work.
    """
    if not scraper_dir:
        return
    path = os.path.join(scraper_dir, "discovered_urls_checkpoint.json")
    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("scraper_runner: removed discovery checkpoint %s", path)
    except OSError as exc:
        logger.warning(
            "scraper_runner: could not remove checkpoint %s: %s", path, exc
        )


def _post_run(result: Any, scraper_path: str, elapsed: float) -> dict[str, Any]:
    """Find output file, read its CONTENT, build result dict. Shared by success + failure paths.

    Stateless /scrape: returns the output CONTENT (not a filesystem path) so the
    caller never needs access to browser_service's /tmp staging dir. The chown
    block that lived here (root→uid1000 for the old shared volume) is gone — no
    shared volume means no uid mismatch to fix.
    """
    scraper_dir = os.path.dirname(scraper_path) or PROJECT_ROOT
    output_file = _find_output_file(scraper_dir)
    product_count = _count_products(output_file) if output_file else 0

    # H3: after a SUCCESSFUL run, delete the discovery checkpoint so the next
    # invocation starts fresh. Guarded on returncode == 0 because the Chrome-crash
    # retry path (the ONLY legitimate mid-run checkpoint consumer) hits this hook
    # with returncode != 0 between attempts — it must NOT lose its checkpoint.
    # [discovery-coverage-gate §4]
    if result.returncode == 0:
        _delete_discovery_checkpoint(scraper_dir)

    output_content = ""
    output_name = ""
    if output_file:
        logger.info(
            "Post-run: output=%s, items=%d (partial=%s)",
            os.path.basename(output_file), product_count,
            result.returncode != 0,
        )
        try:
            with open(output_file, "r", encoding="utf-8", errors="replace") as f:
                output_content = f.read()
            output_name = os.path.basename(output_file)
        except OSError as exc:
            logger.warning("Post-run: could not read output %s: %s", output_file, exc)

    return {
        "returncode": result.returncode,
        "stdout": (result.stdout or "")[:50000],  # cap to avoid bloating the response
        "stderr": (result.stderr or "")[:50000],
        "output_content": output_content,   # full output JSON text (caller persists it)
        "output_name": output_name,         # e.g. "output_2026-...json"
        "product_count": product_count,
        "duration": elapsed,
    }


def run_scraper_script(
    scraper_path: str,
    args: Optional[list[str]] = None,
    timeout: int = 3600,
    env_overrides: Optional[dict[str, str]] = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Run a scraper script with Chrome-crash retry support.

    On Chrome/CDP death (not a code bug), restarts Chrome + retries with
    exponential backoff (10s → 20s → 40s). The scraper's own checkpoint/resume
    (B-core, if the template implements it) ensures retries don't redo work.
    """
    cmd = ["python3", scraper_path]
    if args:
        cmd.extend(args)
    if "--xvfb" not in cmd:
        try:
            with open(scraper_path, "r", errors="ignore") as _f:
                _src = _f.read()
            if "--xvfb" in _src:
                cmd.append("--xvfb")
        except OSError:
            cmd.append("--xvfb")

    env = {
        **os.environ,
        "DISPLAY": DISPLAY,
        "PROJECT_ROOT": PROJECT_ROOT,
        "PYTHONUNBUFFERED": "1",
    }
    if env_overrides:
        env.update(env_overrides)
    _stealth = (env.get("STEALTH_BROWSER", "") or "").strip().lower()
    if _stealth != "cloak":
        env["BROWSER_CDP_ENDPOINT"] = "http://127.0.0.1:9223"
    else:
        env.pop("BROWSER_CDP_ENDPOINT", None)

    start = time.time()
    backoff = 10  # seconds, doubles each retry (capped at 60)
    last_result_dict: dict[str, Any] = {}

    for attempt in range(1, max_retries + 1):
        attempt_start = time.time()
        logger.info("Scraper run attempt %d/%d: %s %s", attempt, max_retries,
                     os.path.basename(scraper_path), " ".join(args or []))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=os.path.dirname(scraper_path) or PROJECT_ROOT,
                env=env,
            )
            elapsed = round(time.time() - attempt_start, 2)
            logger.info(
                "Scraper exited code %d in %ds (attempt %d/%d)",
                result.returncode, elapsed, attempt, max_retries,
            )

            # Success.
            if result.returncode == 0:
                return _post_run(result, scraper_path, round(time.time() - start, 2))

            # Non-zero exit — classify: Chrome crash (retryable) vs code bug.
            stderr = result.stderr or ""
            chrome_crash = _is_chrome_death(stderr) and not _has_traceback(stderr)

            if chrome_crash:
                logger.warning(
                    "Scraper: Chrome crash detected (attempt %d/%d). stderr: %s",
                    attempt, max_retries, stderr[:500],
                )
                if attempt < max_retries:
                    logger.info(
                        "Scraper: restarting Chrome + retrying in %ds (attempt %d/%d)...",
                        backoff, attempt + 1, max_retries,
                    )
                    _restart_scraper_chrome()
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    last_result_dict = _post_run(result, scraper_path, elapsed)
                    continue
                else:
                    logger.warning(
                        "Scraper: Chrome crashes exhausted after %d attempts — returning partial result",
                        max_retries,
                    )
            else:
                # Code bug (Traceback) — don't retry, return immediately.
                logger.info(
                    "Scraper: code error (not Chrome crash) — not retrying. stderr: %s",
                    stderr[:500],
                )

            return _post_run(result, scraper_path, round(time.time() - start, 2))

        except subprocess.TimeoutExpired:
            # A timeout means the scraper is SLOW or HUNG — NOT a Chrome crash.
            # Retrying the SAME work in the SAME time budget finishes neither
            # (locumtenens' slow form-POST discovery retried 3×180s here for
            # zero benefit, and the intervening Chrome restarts wedged the shared
            # Scraper Chrome so run_execution hung on it). So: do NOT retry.
            # Restart Chrome once so a wedged browser doesn't hang the NEXT
            # caller, then return the timeout to the caller (code_tester/probe
            # treat it as inconclusive; run_execution applies its own budget).
            logger.error(
                "Scraper timed out after %ds — slow/hung work, not a Chrome crash; "
                "NOT retrying (same work would time out again). Restarting Chrome "
                "to clear any wedge for the next caller.",
                timeout,
            )
            _restart_scraper_chrome()
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"Timed out after {timeout}s",
                "output_file": "",
                "product_count": 0,
                "duration": round(time.time() - start, 2),
            }

        except Exception as exc:
            logger.exception("Scraper execution failed (attempt %d/%d)", attempt, max_retries)
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": str(exc),
                "output_file": "",
                "product_count": 0,
                "duration": round(time.time() - start, 2),
            }

    # Exhausted retries with partial output from last attempt.
    if last_result_dict:
        last_result_dict["duration"] = round(time.time() - start, 2)
        logger.info(
            "Scraper: returning partial result from last attempt (%d items)",
            last_result_dict.get("product_count", 0),
        )
        return last_result_dict
    return {
        "returncode": -1,
        "stdout": "",
        "stderr": f"Exhausted all {max_retries} retries",
        "output_file": "",
        "product_count": 0,
        "duration": round(time.time() - start, 2),
    }


def _find_output_file(site_folder: str) -> str:
    if not site_folder or not os.path.isdir(site_folder):
        return ""
    try:
        candidates = sorted(
            [
                os.path.join(site_folder, f)
                for f in os.listdir(site_folder)
                if f.startswith("output_") and f.endswith(".json")
            ]
        )
        return candidates[-1] if candidates else ""
    except Exception:
        return ""


def _count_products(output_path: str) -> int:
    """Count extracted items in an output file.

    The output list key varies by content type — ``products`` for shopping,
    ``jobs`` for job_posting, ``articles``, ``threads``, etc. (see
    ``src/content_types.py`` output_key). Hardcoding ``"products"`` silently
    reports 0 for every non-product job, so the pipeline marks a successful
    600-job extraction as failed. Instead, count the primary item list: the
    largest top-level list in the output JSON (the standard schema has exactly
    one — site/metadata are dicts). Generic across all content types.
    """
    if not output_path or not os.path.isfile(output_path):
        return 0
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return max(
                (len(v) for v in data.values() if isinstance(v, list)),
                default=0,
            )
        return 0
    except Exception:
        return 0
