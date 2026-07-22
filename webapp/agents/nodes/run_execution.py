"""Run the generated scraper and capture output.

This node NEVER throws.  All exceptions are caught and recorded
in ``state["execution_status"]`` so that ``cleanup`` can always run.

For browser-based scrapers, dispatches to browser_service via HTTP.
For lightweight scrapers, runs in-process via subprocess.
"""

import json
import logging
import os
import select
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


def _accepted_cli_flags(scraper_path: str) -> set[str] | None:
    """Return the long-flag names the scraper's argparse accepts.

    Done by static AST analysis of ``add_argument("--flag", ...)`` calls — no
    execution, so it works even if the scraper's deps (playwright, etc.) aren't
    importable in this container. Returns None if undeterminable (caller then
    passes args unchanged rather than risk breaking an opaque scraper).

    Why this exists: the generated scraper's argparse is LLM-written and may
    drop flags run_execution passes (e.g. ``--query`` for nav jobs,
    ``--category-url`` for list pages). An unsupported flag makes argparse
    exit(2) before any scraping happens — the scraper never even starts. This
    probe lets run_execution pass only flags the scraper actually accepts.
    [generic CLI-contract guard, no site-specific assumptions]
    """
    try:
        import ast

        with open(scraper_path, "r", errors="ignore") as fh:
            tree = ast.parse(fh.read())
        flags: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "add_argument"
            ):
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value.startswith("--")
                    ):
                        flags.add(arg.value[2:])
        return flags or None
    except Exception as exc:
        logger.warning("run_execution: could not parse scraper CLI flags: %s", exc)
        return None


def _filter_supported_args(
    args: list[str], accepted: set[str] | None
) -> list[str]:
    """Keep only ``--flag`` tokens (and their values) the scraper accepts.

    If ``accepted`` is None (introspection failed), return args unchanged.
    Handles ``--flag value``, ``--flag v1 v2`` (nargs="+"), and bare
    ``--store_true`` flags by consuming consecutive non-flag tokens as values.
    """
    if accepted is None:
        return args
    filtered: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--") and tok[2:] in accepted:
            filtered.append(tok)
            i += 1
            # consume all consecutive values (handles nargs="+" + single value)
            while i < len(args) and not args[i].startswith("--"):
                filtered.append(args[i])
                i += 1
        elif tok.startswith("--"):
            # unsupported flag — skip it and its values
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                i += 1
        else:
            # orphan value with no kept flag — drop
            i += 1
    return filtered


def _needs_browser(state: ScrapeState, scraper_path: str = "") -> bool:
    """True if this scraper must run in browser_service (Chrome/Playwright).

    Uses the SAME detector as code_tester's ``run_scraper`` tool
    (``agents.tools.shell_tools._scraper_needs_browser``) so execution and
    testing can never disagree on what counts as a browser scraper — that
    disagreement is what previously routed a playwright scraper to celery
    (which has no Playwright) and crashed it instantly. The analyzer's
    ``scraping_method`` is checked first as a cheap fast-path; the shared
    whole-file scan (catches lazy / inner-function imports) is ground truth.
    """
    if state.get("scraping_method", "") in BROWSER_METHODS:
        return True
    # Lazy import avoids a module-load cycle between nodes and tools.
    from agents.tools.shell_tools import _scraper_needs_browser

    return _scraper_needs_browser(scraper_path)


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
    from ..graph import _notify_phase

    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "execution", "running")
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
    # `search_criteria` semantics differ by input_mode (parse_command.py:31-36
    # is the contract): it is a production FILTER only for `search_term` jobs
    # (and url_list+criteria, which parse_command flips to search_term). For
    # navigation/list_page it is a discovery HINT, not a filter — passing
    # `--query` here collapses full taxonomy iteration into a single keyword
    # search (e.g. locumtenens: 1000+ → 25). Route navigation/list_page to
    # full discovery via the proven `--listing-url` (working results page).
    if input_mode == "search_term" and search_criteria:
        args.extend(["--query", search_criteria])
        logger.info("run_execution: search_term job, passing --query '%s'", search_criteria)
    elif input_mode in ("navigation", "list_page"):
        _nav = state.get("navigation_analysis") or {}
        _search = _nav.get("search") or {}
        _working_url = (
            (_search.get("working_url") if isinstance(_search, dict) else "")
            or (_search.get("listing_url_used") if isinstance(_search, dict) else "")
            or ""
        )
        if _working_url:
            args.extend(["--listing-url", _working_url])
            logger.info(
                "run_execution: navigation job, passing --listing-url (full discovery) '%s'",
                _working_url,
            )

    # H3 (checkpoint cross-contamination): code_tester writes a
    # discovered_urls_checkpoint.json during its capped sample run, and without
    # this flag the execution phase loads it and skips Phase 1 — silently
    # extracting only the test sample (the locumtenens 38-of-3771 bug).
    # --fresh-discovery makes the scraper ignore any existing checkpoint and run
    # Phase 1 from scratch. The CLI-contract guard below drops this flag if the
    # generated scraper's argparse doesn't define it yet. [discovery-coverage-gate §4]
    args.append("--fresh-discovery")

    # CLI-contract guard: drop any flag the generated scraper doesn't define,
    # so an LLM-authored argparse can't exit(2) and silently zero the output.
    if args:
        accepted = _accepted_cli_flags(scraper_path)
        filtered = _filter_supported_args(args, accepted)
        if filtered != args:
            logger.warning(
                "run_execution: scraper doesn't accept some flags — "
                "passed %s, accepted %s, filtering to %s",
                args,
                sorted(accepted) if accepted else "unknown",
                filtered,
            )
        args = filtered

    # Execution-mode feature flag (settings.SCRAPER_EXECUTION_MODE):
    #   "auto" (default) — _needs_browser decides per-scraper; today's behavior.
    #   "force_scrape"   — always route via browser_service /scrape (rollback lane
    #                      to the subprocess model; see rework plan §4).
    #   "force_http"     — always run in-process (forces the new HTTP navigation
    #                      model even for legacy Playwright scrapers, post-migration).
    from django.conf import settings

    exec_mode = getattr(settings, "SCRAPER_EXECUTION_MODE", "auto")
    if exec_mode == "force_scrape":
        needs_browser = True
    elif exec_mode == "force_http":
        needs_browser = False
    else:
        needs_browser = _needs_browser(state, scraper_path)

    if needs_browser:
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

    return _run_in_process(
        scraper_path, args, root, site_folder, workspace_folder, job_id=job_id,
        env_overrides=_stealth_env(state),
    )


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
        accepted_flags = _accepted_cli_flags(scraper_path)
        # If the scraper doesn't accept --category-url, multisource can't target
        # a category page — every run would just repeat full discovery (the
        # --query guard already stripped the flag). Skip entirely to avoid
        # several multi-minute duplicate discovery runs. [generic]
        if not accepted_flags or "category-url" not in accepted_flags:
            logger.info(
                "multisource: scraper doesn't support --category-url, skipping "
                "category runs (would duplicate primary discovery)"
            )
            return primary_result

        for cat_url in relevant[:5]:  # max 5 category pages
            logger.info("multisource: running scraper on category %s", cat_url[:60])
            try:
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
            # discovery_coverage: Phase 5 will fully aggregate across sources
            # (sum found, max dimensions_total, stop_reason precedence). For now
            # propagate the primary source's block as-is so the gate has something
            # to read on the merged output. [discovery-coverage-gate §6, H2]
            primary_coverage = _read_discovery_coverage(primary_file)
            merged_meta: dict = {
                "scraping_method": "multisource_playwright",
                "sources": 1 + min(len(relevant), 5),
                "total_products": len(all_products),
                "merged": True,
            }
            if isinstance(primary_coverage, dict):
                merged_meta["discovery_coverage"] = primary_coverage
            merged = {
                "site": site_block,
                output_key: all_products,
                "metadata": merged_meta,
            }
            with open(merged_path, "w") as f:
                _json.dump(merged, f, indent=2, ensure_ascii=False, default=str)
            logger.info("multisource: merged output → %d total products → %s", len(all_products), merged_path)
            return {
                "execution_status": "SUCCESS",
                "output_file": merged_path,
                "product_count": len(all_products),
                "discovery_coverage": primary_coverage,
                "error_message": "",
            }

        return primary_result
    except Exception as exc:
        logger.warning("multisource: failed: %s", exc)
        return primary_result


def _run_in_process(
    scraper_path: str, args: list[str], cwd: str, site_folder: str,
    workspace_folder: str = "", job_id: int = 0,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    cmd = ["python3", scraper_path] + args
    start = time.time()

    # The in-process subprocess can run 30+ min on a long scrape (the HTTP
    # navigation template's 200-page discovery is exactly that). The Celery
    # watchdog (cleanup_stuck_jobs, tasks.py) reaps any RUNNING job whose
    # newest SessionLog row is older than STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES
    # (30 min), so a bare subprocess.run is marked FAILED mid-scrape with the
    # misleading "Worker process crashed ... Likely OOM killed" message even
    # though the subprocess is still running. Mirror _run_via_browser_service's
    # heartbeat (240 s) so the watchdog sees activity throughout. Lazy import
    # avoids a circular dependency (graph imports this node module).
    hb = None
    try:
        from webapp.agents.graph import _start_heartbeat

        if job_id:
            hb = _start_heartbeat(job_id, "run_execution", interval=240)
    except Exception as _hb_exc:
        logger.warning("run_execution: heartbeat start failed: %s", _hb_exc)

    try:
        # Stall-bound execution — applies to ALL scrapers (template AND custom
        # code_writer code). The template DISCOVERY_DEADLINE_SECONDS only governs
        # template scrapers; a custom scraper had no internal bound and could hang
        # for the full wall-clock timeout on a JS-blocked/rate-limited site. Monitor
        # stderr (scrapers log page/item progress there): if no output for
        # EXECUTION_STALL_TIMEOUT the scraper is stalled → kill it. Hard backstop
        # EXECUTION_TIMEOUT. Binary pipes + os.read so a partial line can't block
        # the stall timer.
        from django.conf import settings as _settings
        _stall = getattr(_settings, "EXECUTION_STALL_TIMEOUT", 300)
        _hard = getattr(_settings, "EXECUTION_TIMEOUT", 3600)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
            env={**os.environ, **(env_overrides or {})},
        )
        err_fd = proc.stderr.fileno()
        stderr_buf = bytearray()
        last_activity = time.time()
        stall_reason = ""
        while True:
            ready, _, _ = select.select([proc.stderr], [], [], 5.0)
            if ready:
                chunk = os.read(err_fd, 65536)
                if chunk:
                    last_activity = time.time()
                    stderr_buf.extend(chunk)
                    if len(stderr_buf) > 200_000:
                        del stderr_buf[:100_000]
                elif proc.poll() is not None:
                    break  # EOF and process exited
            elif proc.poll() is not None:
                break  # no output this tick but process exited
            if time.time() - last_activity > _stall:
                stall_reason = (
                    f"scraper stalled — no output for {_stall}s "
                    f"(likely hung/blocked on a JS or rate-limited site)"
                )
                proc.kill()
                break
            if time.time() - start > _hard:
                stall_reason = f"scraper exceeded {_hard}s wall-clock"
                proc.kill()
                break
        try:
            proc.communicate(timeout=30)  # drain + reap the killed/finished process
        except subprocess.TimeoutExpired:
            proc.kill()
        returncode = proc.returncode if proc.returncode is not None else -1
        elapsed = round(time.time() - start, 2)
        stderr = stderr_buf.decode("utf-8", "replace")

        if stall_reason:
            logger.error("run_execution: %s after %ds", stall_reason, elapsed)
            return {
                "execution_status": "FAILED",
                "error_message": f"{stall_reason}. Last output: {stderr[-1500:]}",
            }

        logger.info("run_execution: scraper exited with code %d in %ds", returncode, elapsed)

        if returncode != 0:
            return {
                "execution_status": "FAILED",
                "error_message": f"Scraper exited with code {returncode}. {stderr[-2000:]}",
            }

        output_file = _find_newest_output(workspace_folder, site_folder)
        product_count = _count_products(output_file) if output_file else 0
        discovery_coverage = _read_discovery_coverage(output_file)

        # H3: in-process path owns its own checkpoint cleanup (no browser_service
        # to do it). Delete so a subsequent invocation starts fresh. Tried in both
        # the workspace dir (SCRIPT_DIR location) and the subprocess cwd, since
        # the checkpoint location varies by template. [discovery-coverage-gate §4]
        _delete_discovery_checkpoint(workspace_folder, cwd)

        return {
            "execution_status": "SUCCESS",
            "output_file": output_file or "",
            "product_count": product_count,
            "discovery_coverage": discovery_coverage,
            "error_message": "",
        }
    except Exception as exc:
        logger.exception("run_execution: unexpected error")
        return {
            "execution_status": "FAILED",
            "error_message": str(exc),
        }
    finally:
        if hb is not None:
            try:
                from webapp.agents.graph import _stop_heartbeat

                _stop_heartbeat(hb)
            except Exception:
                pass


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

    # Execution fail-fast bound (same EXECUTION_TIMEOUT backstop as the in-process
    # path — bounds a hung browser_service scrape, applies to all scrapers).
    from django.conf import settings as _settings
    timeout = getattr(_settings, "EXECUTION_TIMEOUT", 3600)
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

        # The output lives on the shared volume, so we can read the
        # discovery_coverage block the scraper emitted. Checkpoint cleanup for
        # this path is owned by browser_service's scraper_runner (post-success).
        output_file = result.get("output_file", "")
        discovery_coverage = _read_discovery_coverage(output_file)

        return {
            "execution_status": "SUCCESS",
            "output_file": output_file,
            "product_count": result.get("product_count", 0),
            "discovery_coverage": discovery_coverage,
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


def _find_newest_output(*directories: str) -> str:
    """Return the newest-mtime ``output_*.json`` across the given directories.

    The scraper writes its output next to itself (``SCRIPT_DIR`` = dirname of
    the scraper file), which during a run is ``workspace/{slug}/``. Selecting by
    name OR scanning ``scrapers/{slug}/`` first returns a *stale* output from a
    prior job on a re-scrape (that dir already holds old outputs), so the
    current run's freshly-written file is never picked — and the wrong file
    propagates to ScrapeJob.output_file / product_count / store_job_listings
    (e.g. 1 job recorded instead of 69). Picking the newest mtime across all
    relevant dirs reliably identifies the file THIS run just wrote, regardless
    of which dir it landed in.
    """
    best_mtime = -1.0
    best_path = ""
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not (name.startswith("output_") and name.endswith(".json")):
                continue
            path = os.path.join(directory, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime, best_path = mtime, path
    return best_path


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


def _read_discovery_coverage(output_path: str) -> Optional[dict[str, Any]]:
    """Read the ``discovery_coverage`` block from a scraper output's metadata.

    Two-phase scrapers emit this block (see
    docs/discovery-coverage-gate-contract.md §1) inside ``metadata``. Returns
    None when absent (url_list scrapers with no discovery phase, older outputs,
    or missing file) so the state field stays unset rather than polluted.
    """
    if not output_path or not os.path.isfile(output_path):
        return None
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            metadata = data.get("metadata") or {}
            if isinstance(metadata, dict):
                block = metadata.get("discovery_coverage")
                if isinstance(block, dict):
                    return block
    except Exception:
        pass
    return None


def _delete_discovery_checkpoint(*candidate_dirs: str) -> None:
    """Delete ``discovered_urls_checkpoint.json`` so the next run starts fresh (H3).

    Without this, code_tester's capped-sample checkpoint persists into
    run_execution, which loads it and skips Phase 1 — silently extracting only
    the test sample (the locumtenens 38-of-3771 bug). Tried across all candidate
    dirs because the checkpoint location varies by template (SCRIPT_DIR vs cwd).
    Silent no-op when the file is absent. [discovery-coverage-gate §4]
    """
    for d in candidate_dirs:
        if not d:
            continue
        path = os.path.join(d, "discovered_urls_checkpoint.json")
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info("run_execution: removed discovery checkpoint %s", path)
        except OSError as exc:
            logger.warning(
                "run_execution: could not remove checkpoint %s: %s", path, exc
            )
