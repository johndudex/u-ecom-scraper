"""Shell execution tools for LangGraph agent nodes.

Provides ``run_bash`` for local commands and ``run_scraper`` for executing
generated scrapers.  Browser-based scrapers are dispatched to
browser-service via HTTP.  HTTP-based scrapers run locally as subprocesses.
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
    "BROWSER_SERVICE_URL", "http://browser-service:8001"
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
                    "run on browser-service which has Chrome, SeleniumBase, and Playwright. "
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
        browser-service which has Chrome + Xvfb.  HTTP-based scrapers run
        locally.

        Args:
            scraper_path: Path to the scraper Python file.
            cli_args: Additional CLI arguments as a string (e.g. "--sample --limit 5").
            timeout: Maximum execution time in seconds (default 300).
            extra_args: Internal — do not use.  Alias for ``cli_args`` accepted as a
                list.  Some LLM providers emit ``extra_args`` instead of ``cli_args``.
        """
        full_path = scraper_path if os.path.isabs(scraper_path) else os.path.join(cwd, scraper_path)
        needs_browser = _scraper_needs_browser(full_path)

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

        if needs_browser:
            logger.info("run_scraper: browser-based, dispatching to browser-service: %s", scraper_path)
            try:
                service_url = _get_browser_service_url()
                resp = httpx.post(
                    f"{service_url}/scrape",
                    json={
                        "scraper_path": full_path,
                        "args": cmd_args,
                        "timeout": timeout,
                    },
                    timeout=timeout + 60,
                )
                if resp.status_code == 404:
                    return f"Scraper not found at {full_path} on browser-service"
                resp.raise_for_status()
                result = resp.json()
                output = _format_result(result)
                output += f"\n[ran on browser-service, duration: {result.get('duration', '?')}s]"
                if result.get("output_file"):
                    output += f"\n[output_file: {result['output_file']}]"
                return output
            except httpx.ConnectError:
                return f"Error: browser-service ({_get_browser_service_url()}) is unreachable"
            except httpx.TimeoutException:
                return f"Scraper timed out after {timeout + 60}s on browser-service"
            except Exception as exc:
                logger.error("run_scraper: browser-service dispatch failed: %s", exc)
                return f"Error dispatching to browser-service: {exc}"
        else:
            logger.info("run_scraper: http-based, running locally: %s", scraper_path)
            try:
                cmd = ["python3", full_path] + cmd_args
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
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
