"""Shell execution tools for LangGraph agent nodes.

Provides ``run_bash`` for local commands and ``run_scraper`` for executing
generated scrapers.  Browser-based scrapers are dispatched to
browser_service via HTTP.  HTTP-based scrapers run locally as subprocesses.
"""

import logging
import os
import shlex
import subprocess
from typing import Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 10000
DEFAULT_TIMEOUT = 120

BROWSER_SERVICE_URL = os.environ.get(
    "BROWSER_SERVICE_URL", "http://browser_service:8001"
)
SCRAPER_HTTP_TIMEOUT = int(os.environ.get("SCRAPER_HTTP_TIMEOUT", "7200"))

BROWSER_IMPORTS = {
    "seleniumbase",
    "undetected_chromedriver",
    "selenium",
    "playwright.sync_api",
    "playwright",
}


def _resolve_project_root(project_root: Optional[str] = None) -> str:
    if project_root:
        return os.path.abspath(project_root)
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
    return BROWSER_SERVICE_URL


def _scraper_needs_browser(scraper_path: str) -> bool:
    try:
        with open(scraper_path, "r", encoding="utf-8") as fh:
            head = fh.read().lower()
        for imp in BROWSER_IMPORTS:
            if f"import {imp}" in head or f"from {imp}" in head:
                return True
    except Exception:
        pass
    return False


def _format_result(result: dict) -> str:
    parts = []
    if result.get("stdout"):
        parts.append(result["stdout"])
    if result.get("stderr"):
        parts.append(result["stderr"])
    output = "\n".join(parts) if parts else "(no output)"

    if len(output) > 4000:
        try:
            from headroom import compress as _compress

            cr = _compress(
                [{"role": "tool", "content": output}],
                model="glm-5-turbo",
            )
            compressed = cr.messages[0]["content"]
            if len(output) - len(compressed) > 200:
                logger.info(
                    "run_scraper output compressed: %d → %d chars",
                    len(output),
                    len(compressed),
                )
                output = compressed
        except Exception:
            pass
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
    if result.get("returncode", 0) != 0:
        output += f"\n[exit code: {result['returncode']}]"
    return output


def get_shell_tools(
    project_root: Optional[str] = None,
    allowed_dirs: Optional[list[str]] = None,
) -> list:
    cwd = _resolve_project_root(project_root)

    @tool
    def run_bash(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """Execute a shell command and return its output.

        The command runs inside the project root directory.  Both stdout and
        stderr are captured.  Output is truncated to 10 000 characters.

        Args:
            command: The shell command to execute.
            timeout: Maximum execution time in seconds (default 120).

        Returns:
            Combined stdout + stderr output, or an error message if the
            command times out or fails to execute.
        """
        logger.info("run_bash: %s", command[:200])
        if "pip install" in command or "pip3 install" in command:
            return ("Error: pip install is not allowed. All required packages are "
                    "pre-installed in the execution environment. Browser-based scrapers "
                    "run on browser_service which has Chrome, SeleniumBase, and Playwright. "
                    "Use run_scraper instead of run_bash for scraper execution.")
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr

            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(result.stdout or '') + len(result.stderr or '')} chars total)"

            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"

            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s"
        except Exception as e:
            return f"Error executing command: {e}"

    @tool
    def run_scraper(
        scraper_path: str,
        cli_args: str = "",
        timeout: int = 300,
        extra_args: Optional[list] = None,
    ) -> str:
        """Run a generated scraper and return its output.

        Automatically detects whether the scraper needs a browser (Playwright,
        SeleniumBase, etc.).  Browser-based scrapers are dispatched to
        browser_service which has Chrome + Xvfb.  HTTP-based scrapers run
        locally.

        Args:
            scraper_path: Path to the scraper Python file.
            cli_args: Additional CLI arguments as a string (e.g. "--sample --limit 5").
            timeout: Maximum execution time in seconds (default 300).
            extra_args: Internal — do not use.  Alias for ``cli_args`` accepted as a
                list.  Some LLM providers emit ``extra_args`` instead of ``cli_args``.
        """
        full_path = scraper_path if os.path.isabs(scraper_path) else os.path.join(cwd, scraper_path)
        # Respect the SCRAPER_EXECUTION_MODE feature flag (same switch as
        # run_execution) so the testing path and the final execution path can
        # never disagree on routing:
        #   "force_scrape" — always /scrape (rollback lane)
        #   "force_http"   — always in-process (forces the new HTTP model)
        #   "auto" (default) — sniff the scraper imports (unchanged behavior)
        try:
            from django.conf import settings

            exec_mode = getattr(settings, "SCRAPER_EXECUTION_MODE", "auto")
        except Exception:
            exec_mode = "auto"
        if exec_mode == "force_scrape":
            needs_browser = True
        elif exec_mode == "force_http":
            needs_browser = False
        else:
            needs_browser = _scraper_needs_browser(full_path)

        # Floor the timeout for browser-based scrapers. code_tester's LLM often
        # passes a tight timeout (or the browser_service /scrape 120s default
        # applies), but JS-heavy sites (lw.com Coveo, ~15s/page on networkidle)
        # need ~160s+ even for a small sample — under-budgeting SIGKILLs the run
        # mid-extraction (exit -1) and triggers a false strategy cascade
        # (playwright→internal_api). 240s sits within /scrape's 300s cap and
        # preserves the +60s httpx margin. HTTP runs are unaffected.
        if needs_browser and timeout < 240:
            logger.info("run_scraper: flooring browser timeout %ds → 240s", timeout)
            timeout = 240

        # Write a heartbeat SessionLog entry so the watchdog sees activity
        # during long scraper runs (UC Chrome + residential proxy can take 5+ min)
        try:
            from agents.tools.context import get_state
            tool_state = get_state()
            job_id = (tool_state or {}).get("job_id", 0)
            if job_id:
                from scraper.models import SessionLog
                seq = SessionLog.objects.filter(job_id=job_id).count()
                SessionLog.objects.create(
                    job_id=job_id,
                    role=SessionLog.ROLE_SYSTEM,
                    agent="code-tester",
                    content=f"[RUN_SCRAPER] Starting: {scraper_path} {cli_args}",
                    seq=seq,
                )
        except Exception:
            pass

        if extra_args and not cli_args:
            logger.info("run_scraper: remapping extra_args=%s → cli_args", extra_args)
            cmd_args = list(extra_args)
        else:
            cmd_args = shlex.split(cli_args) if cli_args else []

        # Bug C guard (deterministic): if the caller passed --urls <single_url>
        # (the 1-item trap — always extracts exactly 1, fails the ≥3 ground-truth
        # gate, causes a false cascade), AND input_urls.json exists alongside the
        # scraper, redirect to --input input_urls.json so the test covers the
        # full sample set. The LLM code_tester ignores prompt-level prohibitions;
        # this guard is at the tool level so it CANNOT be bypassed.
        if "--urls" in cmd_args:
            _ws_dir = os.path.dirname(full_path)
            _iu = os.path.join(_ws_dir, "input_urls.json")
            if os.path.isfile(_iu):
                # Replace --urls <url> with --input input_urls.json
                _idx = cmd_args.index("--urls")
                # Remove --urls + its value (next arg if not a flag)
                cmd_args.pop(_idx)
                if _idx < len(cmd_args) and not cmd_args[_idx].startswith("--"):
                    cmd_args.pop(_idx)
                cmd_args.extend(["--input", "input_urls.json"])
                logger.info(
                    "run_scraper: Bug C guard — redirected --urls <single> to "
                    "--input input_urls.json (prevents the 1-item trap)"
                )

        # Discovery env overrides — computed ONCE for BOTH paths. (Previously
        # this block lived inside `if needs_browser:`, so the HTTP/local branch
        # below referenced an unassigned `env_overrides` → UnboundLocalError on
        # every http_requests/internal_api run — the known env_overrides bug.)
        env_overrides = None
        try:
            from agents.tools.context import is_anti_bot_detected

            if is_anti_bot_detected():
                env_overrides = {"STEALTH_BROWSER": "cloak"}
                logger.info("run_scraper: anti-bot detected → STEALTH_BROWSER=cloak")
        except Exception:
            pass
        # DETERMINISTIC DISCOVERY: inject SCRAPER_LISTING_URL so the scraper's
        # main() env-var gate triggers Phase 1 discovery even during code_tester's
        # run (not just run_execution). Without this, code_tester falls into the
        # seed-file path (input_urls.json) → 1-5 items → route_after_testing
        # fails → cascade. The env var bypasses argparse stripping entirely.
        try:
            from agents.tools.context import get_state
            _ts = get_state() or {}
            _nav_ts = _ts.get("navigation_analysis") or {}
            _disc_ts = (_nav_ts.get("discovery") if isinstance(_nav_ts, dict) else None) or {}
            _listing_ts = (
                (_disc_ts.get("listing_url") if isinstance(_disc_ts, dict) else "")
                or ""
            )
            if _listing_ts and _ts.get("input_mode") in ("navigation", "list_page", "search_term"):
                env_overrides = dict(env_overrides or {})
                env_overrides["SCRAPER_LISTING_URL"] = _listing_ts
        except Exception:
            pass

        if needs_browser:
            logger.info("run_scraper: browser-based, dispatching to browser_service: %s", scraper_path)
            try:
                service_url = _get_browser_service_url()
                # Stateless /scrape: send the scraper SOURCE (not a path). Read
                # the local file the caller built (workspace/{slug}/scraper_draft.py).
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as _f:
                        _source = _f.read()
                except OSError as exc:
                    return f"Error: could not read scraper source {full_path}: {exc}"
                resp = httpx.post(
                    f"{service_url}/scrape",
                    json={
                        "scraper_source": _source,
                        "scraper_name": os.path.basename(full_path),
                        "args": cmd_args,
                        "timeout": timeout,
                        "env_overrides": env_overrides,
                    },
                    timeout=timeout + 60,
                )
                if resp.status_code == 404:
                    return f"Scraper rejected by browser_service (source invalid)"
                resp.raise_for_status()
                result = resp.json()
                # browser_service returns output CONTENT (no shared FS). Persist
                # it to the local workspace so downstream (route_after_testing
                # ground-truth, which reads workspace/{slug}/output_*.json) works.
                _output_content = result.get("output_content") or ""
                _output_name = result.get("output_name") or ""
                if _output_content and _output_name:
                    _scraper_dir = os.path.dirname(full_path) or cwd
                    _local_output = os.path.join(_scraper_dir, _output_name)
                    try:
                        os.makedirs(_scraper_dir, exist_ok=True)
                        with open(_local_output, "w", encoding="utf-8") as _of:
                            _of.write(_output_content)
                        result["output_file"] = _local_output
                    except OSError as exc:
                        logger.warning("run_scraper: could not persist output locally: %s", exc)
                output = _format_result(result)
                output += f"\n[ran on browser_service, duration: {result.get('duration', '?')}s]"
                if result.get("output_file"):
                    output += f"\n[output_file: {result['output_file']}]"
                return output
            except httpx.ConnectError:
                return f"Error: browser_service ({_get_browser_service_url()}) is unreachable"
            except httpx.TimeoutException:
                return f"Scraper timed out after {timeout + 60}s on browser_service"
            except Exception as exc:
                logger.error("run_scraper: browser_service dispatch failed: %s", exc)
                return f"Error dispatching to browser_service: {exc}"
        else:
            logger.info("run_scraper: http-based, running locally: %s", scraper_path)
            try:
                cmd = ["python3", full_path] + cmd_args
                # Inherit env + inject discovery env vars (same as browser path).
                _run_env = dict(os.environ)
                _run_env.update(env_overrides or {})
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                    env=_run_env,
                )
                return _format_result({
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                })
            except subprocess.TimeoutExpired:
                return f"Scraper timed out after {timeout}s"
            except Exception as exc:
                return f"Error running scraper: {exc}"

    return [run_bash, run_scraper]
