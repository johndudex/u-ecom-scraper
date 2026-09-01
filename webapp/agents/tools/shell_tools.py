"""Shell execution tools for LangGraph agent nodes.

Provides ``run_bash`` for local commands and ``run_scraper`` for executing
generated scrapers.  Browser-based scrapers are dispatched to
browser_service via HTTP.  HTTP-based scrapers run locally as subprocesses.
"""

import logging
import os
import shlex
import subprocess
import time
from contextlib import contextmanager
from typing import Optional

import httpx
from langchain_core.tools import tool

from .browser_http import post_scrape_with_retry

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 10000
DEFAULT_TIMEOUT = 120

BROWSER_SERVICE_URL = os.environ.get(
    "BROWSER_SERVICE_URL", "http://browser_service:8001"
)
SCRAPER_HTTP_TIMEOUT = int(os.environ.get("SCRAPER_HTTP_TIMEOUT", "7200"))

# [job-81] Floor for browser-based scraper runs. Exported so the graph can
# derive the code_tester invocation window from the SAME constant — the
# tester's prompt mandates up to two blocking runs (discovery + sample), so
# its wall clock must be derived from this floor, not hand-tuned beside it.
BROWSER_RUN_TIMEOUT_FLOOR = int(os.environ.get("BROWSER_RUN_TIMEOUT_FLOOR", "600"))

# [wave-14 job-133] Floor for VERIFICATION-SCOPE browser runs: an explicit
# seed (--input/--urls) + --sample extracts from KNOWN URLs — no phase-1
# discovery walk — so the 600s discovery floor is dead weight. Job-133's
# writer burned a ~690s full-scope window on a 5-URL self-test; with the
# cheap floor the same self-test budgets ~180s and the honesty guard stops
# refusing runs that would actually fit the invoking agent's window.
VERIFICATION_RUN_TIMEOUT_FLOOR = int(
    os.environ.get("VERIFICATION_RUN_TIMEOUT_FLOOR", "180")
)

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


def _hygiene_input_seed(cmd_args: list, ws_dir: str, job_url: str) -> str:
    """[wave-14 job-133] Rewrite the seed file a run is about to consume with
    the shared full-host filter (``src/seed_urls.py``).

    The upstream writers (intake, FM sync, the re-run view) filter their own
    writes, but this is the LAST point before the subprocess reads the file —
    the belt that catches a writer this module's callers missed (e.g. the
    code-writer's navigation-derived seed, or a stale file left in the
    workspace by a previous job on the same slug). Returns a human-readable
    note for the log / tool result; "" when there was nothing to do.
    """
    seed_path = ""
    for _i, _arg in enumerate(cmd_args):
        if _arg == "--input" and _i + 1 < len(cmd_args):
            _val = cmd_args[_i + 1]
            seed_path = _val if os.path.isabs(_val) else os.path.join(ws_dir, _val)
            break
        if _arg == "--urls":
            # Bug C redirects this to --input when a sibling seed exists; if
            # it didn't, there is no seed file to filter.
            return ""
    if not seed_path:
        seed_path = os.path.join(ws_dir, "input_urls.json")
    if not seed_path or not os.path.isfile(seed_path):
        return ""
    try:
        import json

        from src.seed_urls import dropped_summary, filter_seed_payload

        with open(seed_path, "r", encoding="utf-8") as _fh:
            payload = json.load(_fh)
        filtered, dropped = filter_seed_payload(payload, job_url)
        if not dropped:
            return ""
        with open(seed_path, "w", encoding="utf-8") as _fh:
            json.dump(filtered, _fh)
        _kept = (
            len(filtered.get("urls") or [])
            if isinstance(filtered, dict)
            else len(filtered)
        )
        return (
            f"seed hygiene: rewrote {os.path.basename(seed_path)} — "
            f"dropped {dropped_summary(dropped)}, kept {_kept}"
        )
    except Exception as exc:
        return f"seed hygiene skipped (unreadable seed file: {exc})"


def _format_result(result: dict) -> str:
    parts = []
    if result.get("stdout"):
        parts.append(result["stdout"])
    if result.get("stderr"):
        parts.append(result["stderr"])
    output = "\n".join(parts) if parts else "(no output)"

    # [QW-4] headroom.compress removed: it was a synchronous LLM call on the
    # agent's event path (P0 precondition for the async-cancellation refactor)
    # that re-phrased tool output non-deterministically. Deterministic
    # truncation only.
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

        # Parse the CLI args EARLY: the verification-scope check below and the
        # Bug C guard both need them, and the floor depends on the scope.
        # (Previously parsed after the floor/guard blocks — the floor could
        # not know what kind of run it was flooring.)
        if extra_args and not cli_args:
            logger.info("run_scraper: remapping extra_args=%s → cli_args", extra_args)
            cmd_args = list(extra_args)
        else:
            cmd_args = shlex.split(cli_args) if cli_args else []

        # [wave-14 job-133] VERIFICATION SCOPE: an explicit seed (--input /
        # --urls) plus --sample means "extract from THESE known URLs" — no
        # phase-1 discovery walk. Job-133's writer self-test (--input 5 URLs,
        # --sample) was floored to the full 600s discovery budget and then
        # refused by the honesty guard (690s needed > window). Seed-scoped
        # runs get the cheap floor; discovery runs keep 600s.
        _verification_scope = "--sample" in cmd_args and (
            "--input" in cmd_args or "--urls" in cmd_args
        )

        # Floor the timeout for browser-based scrapers. code_tester's LLM often
        # passes a tight timeout, but two-phase drafts run discovery AND sample
        # extraction in ONE process — pillowtalk: 124s discovery (5 pages) +
        # 5 PDP samples blew the old 240s floor, SIGKILL mid-phase (exit -1)
        # and a false strategy cascade (job 312; same class as lw.com's
        # ~160s-only-sample run that set the old floor). 600s covers a full
        # multi-page walk + samples with headroom; /scrape accepts up to 7200s
        # and the httpx margin (+60s) scales with it. The runner does NOT retry
        # timeouts, so the worst case is one bounded 660s wait. HTTP unaffected.
        _run_floor = (
            VERIFICATION_RUN_TIMEOUT_FLOOR
            if _verification_scope
            else BROWSER_RUN_TIMEOUT_FLOOR
        )
        if needs_browser and timeout < _run_floor:
            logger.info(
                "run_scraper: flooring browser timeout %ds → %ds%s",
                timeout, _run_floor,
                " (verification scope)" if _verification_scope else "",
            )
            timeout = _run_floor

        # [job-81 N-B] Honesty guard: a blocking browser run that CANNOT finish
        # inside the invoking agent's remaining wall clock must not be launched
        # — job 81's tester launched a 600s-floored run with 370s of window
        # left, the invocation was abandoned mid-flight, and the run's result
        # was lost with it. Skip with an explicit marker the agent can report
        # truthfully (phases_tested=false) instead of a silent loss. No
        # deadline known (e.g. run_execution) → guard stays dormant.
        if needs_browser:
            try:
                from agents.tools.context import get_tool_deadline

                _deadline = get_tool_deadline()
            except Exception:
                _deadline = None
            if _deadline is not None:
                _remaining = _deadline - time.time()
                if _remaining < timeout + 90:  # run + httpx margin + staging
                    logger.warning(
                        "run_scraper: SKIPPING browser run — needs ~%ds but only "
                        "~%ds left before the agent wall clock (%.0fs)",
                        timeout, max(int(_remaining), 0), _remaining,
                    )
                    return (
                        f"SKIPPED: insufficient wall clock — this browser run "
                        f"needs ~{timeout}s but only ~{max(int(_remaining), 0)}s "
                        f"remain before the agent invocation's hard timeout. The "
                        f"phase was NOT tested. Do not retry it in this "
                        f"invocation; record honestly in the report which phases "
                        f"ran and which were skipped."
                        + (
                            " If you only need to verify extraction against "
                            "known URLs, a verification-scope run "
                            "(--input input_urls.json --sample) is budgeted "
                            f"at ~{VERIFICATION_RUN_TIMEOUT_FLOOR}s and may fit."
                            if not _verification_scope
                            else ""
                        )
                    )

        # Write a heartbeat SessionLog entry so the watchdog sees activity
        # during long scraper runs (UC Chrome + residential proxy can take 5+ min)
        _scrape_job_id = 0  # hoisted: also sent in the /scrape payload
        try:
            from agents.tools.context import get_state
            tool_state = get_state()
            job_id = (tool_state or {}).get("job_id", 0)
            _scrape_job_id = int(job_id or 0)
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

        # Job identity fetched ONCE — the seed hygiene and the discovery
        # injection below both read tool state.
        _ts: dict = {}
        try:
            from agents.tools.context import get_state

            _ts = get_state() or {}
        except Exception:
            _ts = {}

        # [wave-14 job-133] SEED HYGIENE (belt): the seed file this run is
        # about to consume is filtered with the same shared full-host rule as
        # intake — no matter which surface wrote it (intake, FM sync, the
        # re-run view, the writer's navigation-derived list, or a previous
        # job's leftovers), a poison link cannot reach the subprocess. Runs
        # BEFORE the extra_files staging in the browser branch, so the copy
        # browser_service receives is the filtered one.
        _seed_note = ""
        if "--input" in cmd_args or "--urls" in cmd_args:
            _seed_note = _hygiene_input_seed(
                cmd_args,
                os.path.dirname(full_path),
                str(_ts.get("url") or ""),
            )
            if _seed_note:
                logger.info("run_scraper: %s", _seed_note)

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
            # [wave-15 3.5] Tester parity: the test run stages the SAME proxy
            # tier execution will, so a draft whose items only arrive on a
            # proxied tier is tested under that identity — not passed on a
            # lucky direct-IP window that execution then loses.
            from agents.tools.context import get_probe_method

            _pm = str(get_probe_method() or "")
            for _tier in ("residential", "datacenter"):
                if _pm.endswith(f"_{_tier}"):
                    env_overrides = dict(env_overrides or {})
                    env_overrides["SCRAPER_PROXY_TIER"] = _tier
                    logger.info(
                        "run_scraper: probe method %s → SCRAPER_PROXY_TIER=%s",
                        _pm, _tier,
                    )
                    break
        except Exception:
            pass
        # DETERMINISTIC DISCOVERY: inject SCRAPER_LISTING_URL so the scraper's
        # main() env-var gate triggers Phase 1 discovery even during code_tester's
        # run (not just run_execution). Without this, code_tester falls into the
        # seed-file path (input_urls.json) → 1-5 items → route_after_testing
        # fails → cascade. The env var bypasses argparse stripping entirely.
        #
        # [wave-14 job-133] The injection is SCOPE-AWARE: a verification-scope
        # run (explicit seed + --sample) must stay a seed verification —
        # injecting a listing URL silently converts it into a full discovery
        # walk, which is exactly how job-133's 5-URL self-test became a ~690s
        # discovery charge. And whichever scope wins is REPORTED to the agent
        # in the tool result instead of being an invisible side effect.
        _scope_note = ""
        try:
            _nav_ts = _ts.get("navigation_analysis") or {}
            _disc_ts = (_nav_ts.get("discovery") if isinstance(_nav_ts, dict) else None) or {}
            _listing_ts = (
                (_disc_ts.get("listing_url") if isinstance(_disc_ts, dict) else "")
                or ""
            )
            if not _listing_ts:
                # [rag-bone job 72] the tester must not be the ONLY phase that
                # knows about a listing asserted solely in search_criteria —
                # the tester proved 25 URLs there while execution (which reads
                # the full candidate chain) discovered 2 off the sample PDP.
                _sc_ts = str(_ts.get("search_criteria") or "").strip()
                if _sc_ts.startswith(("http://", "https://")):
                    _listing_ts = _sc_ts
            if _verification_scope:
                _scope_note = (
                    "[run scope] VERIFICATION — the seed file drives this run; "
                    "SCRAPER_LISTING_URL discovery injection suppressed"
                )
            elif _listing_ts and _ts.get("input_mode") in ("navigation", "list_page", "search_term"):
                env_overrides = dict(env_overrides or {})
                env_overrides["SCRAPER_LISTING_URL"] = _listing_ts
                _scope_note = (
                    f"[run scope] DISCOVERY — SCRAPER_LISTING_URL injected → {_listing_ts}"
                )
        except Exception:
            pass
        if _scope_note:
            logger.info("run_scraper: %s", _scope_note)

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
                # Read sibling files (input_urls.json, discovery_config.json) for staging
                _extra = {}
                _ws_dir = os.path.dirname(full_path)
                for _sf in ("input_urls.json", "discovery_config.json"):
                    _sp = os.path.join(_ws_dir, _sf)
                    if os.path.isfile(_sp):
                        try:
                            with open(_sp, "r", encoding="utf-8", errors="replace") as _fh:
                                _extra[_sf] = _fh.read()
                        except OSError:
                            pass
                # W8: bounded retry on 429/502/503/504 + transport errors — a
                # bare raise_for_status() turned browser-service backpressure
                # into an opaque HTTPStatusError mid-test.
                with _dispatch_alive(timeout + 60):
                    _res = post_scrape_with_retry(
                        f"{service_url}/scrape",
                        {
                            "scraper_source": _source,
                            "scraper_name": os.path.basename(full_path),
                            "extra_files": _extra,
                            "args": cmd_args,
                            "timeout": timeout,
                            "env_overrides": env_overrides,
                            # [wave-14 job-133] Correlate browser_service-side
                            # run dirs/logs with the DB job (rid registry,
                            # orphan cleanup). Old peers ignore unknown fields.
                            "job_id": _scrape_job_id,
                        },
                        timeout=timeout + 60,
                    )
                # W8 migration: _res is a ScrapeResult (browser_http), not an
                # httpx.Response — .status_code here raised AttributeError on
                # EVERY browser-strategy dispatch (sephora job 51: scraper never
                # executed, CRASH verdict). error_class is the 404 bucket.
                if _res.error_class == "not_found":
                    return "Scraper rejected by browser_service (source invalid)"
                if not _res.ok:
                    return (
                        f"Scraper run failed ({_res.error_class}): {_res.error}"
                        + (" — transient, safe to re-run" if _res.transient else "")
                    )
                result = _res.data
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
                    # [A3] normalize formatted price strings ("$17.00") to
                    # numerics at persist time, same as run_execution — the
                    # tester's WRONG_VALUE pass and the deterministic checker
                    # then agree on every output they both read.
                    try:
                        from agents.nodes.run_execution import normalize_output_prices

                        _norm = normalize_output_prices(_local_output)
                        if _norm:
                            logger.info(
                                "run_scraper: normalized %d price string(s) to "
                                "numeric in %s", _norm, _output_name,
                            )
                    except Exception as _norm_exc:
                        logger.debug("run_scraper: price normalize skipped: %s", _norm_exc)
                output = _format_result(result)
                output += f"\n[ran on browser_service, duration: {result.get('duration', '?')}s]"
                if result.get("output_file"):
                    output += f"\n[output_file: {result['output_file']}]"
                if _scope_note:
                    output += f"\n{_scope_note}"
                if _seed_note:
                    output += f"\n[{_seed_note}]"
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
                with _dispatch_alive(timeout):
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=cwd,
                        env=_run_env,
                    )
                _out = _format_result({
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                })
                if _scope_note:
                    _out += f"\n{_scope_note}"
                if _seed_note:
                    _out += f"\n[{_seed_note}]"
                return _out
            except subprocess.TimeoutExpired:
                return f"Scraper timed out after {timeout}s"
            except Exception as exc:
                return f"Error running scraper: {exc}"

    return [run_bash, run_scraper]


@contextmanager
def _dispatch_alive(timeout: int | None = None):
    """[jobs 79/80] ``[EXEC-ALIVE]`` heartbeat across run_scraper's blocking
    dispatch (browser_service POST / local subprocess).

    The pre-dispatch ``[RUN_SCRAPER] Starting`` row STARTS the watchdog's
    30-min silence clock — a 10-min browser run behind an otherwise-quiet
    phase then reads as a corpse (jobs 79/80 went silent at exactly that
    row). The dispatch is independently bounded (browser timeout floored at
    ``BROWSER_RUN_TIMEOUT_FLOOR`` + 60s httpx margin; subprocess ``timeout=``),
    so — same doctrine as run_execution's ``[EXEC-ALIVE]`` rows — these beats
    can only rescue a genuinely-live run, never mask a hang: the context ends
    when the dispatch returns and the beats stop with it.

    ``timeout`` [wave-14 job-133] — that bound, passed as the heartbeat's
    ``beat_budget``: the interval shrinks to bound/3 and the t=0 "started" row
    stamps the dispatch moment, so a death INSIDE the dispatch is provable
    from the row sequence (started → beats stop early) instead of being
    indistinguishable from "the 240s beat wasn't due yet".
    """
    _hb = None
    try:
        from agents.tools.context import get_state
        from agents.graph import _start_heartbeat

        _job_id = ((get_state() or {}).get("job_id") or 0)
        if _job_id:
            _hb = _start_heartbeat(
                _job_id, "run_scraper", interval=240, prefix="[EXEC-ALIVE]",
                beat_budget=timeout,
            )
    except Exception:
        _hb = None
    try:
        yield
    finally:
        if _hb is not None:
            try:
                from agents.graph import _stop_heartbeat

                _stop_heartbeat(_hb)
            except Exception:
                pass
