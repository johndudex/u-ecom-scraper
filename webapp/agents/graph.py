"""Main LangGraph assembly for the Universal Ecommerce Scraper.

Builds a ``StateGraph[ScrapeState]`` that orchestrates the full scraping
pipeline: command parsing → tracker check → workspace setup → site analysis →
product analysis → code generation → testing → execution → cleanup → skill
learning.

Each LLM-powered phase (site_analyzer, product_analyzer, code_writer,
code_tester, cleanup, skill_learner) is a ``create_react_agent`` subgraph
produced by the factories in ``subagents.py``.  Deterministic nodes come from
``nodes/`` and handle routing, validation, approval, and artifact management.

Human-in-the-loop is handled via ``langgraph.types.interrupt()`` inside
specific nodes (check_tracker, validate_analysis, validate_coverage,
field_confirmation, pre_execution_approval, human_approval).  The graph
pauses at these points and resumes when the user provides input.

The compiled graph is stateful — checkpointed to PostgreSQL via
``checkpointer.py`` — so jobs can be resumed after interrupts.

Usage::

    from webapp.agents.graph import build_scrape_graph

    graph = build_scrape_graph()
    result = graph.invoke({
        "url": "https://www.nike.com",
        "sample_only": True,
    })
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import functools
from typing import Any, Optional
from django.utils import timezone

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from langchain_core.runnables import RunnableConfig

from .constants import (
    FINAL_RETRY_SENTINEL,
    MAX_CRITICAL_REVIEW_RETRIES,
    MAX_MEDIUM_REVIEW_RETRIES,
)
from .decisions import options_to_decisions
from .nodes import (
    check_tracker,
    field_confirmation,
    human_approval,
    normalize_fields,
    parse_command,
    pre_execution_approval,
    route_after_cleanup,
    route_after_testing,
    run_execution,
    setup_workspace,
    update_tracker_analysis,
    validate_analysis,
    validate_coverage,
)
from .nodes.run_execution import _find_newest_output
from .state import ScrapeState
from .subagents import (
    build_cleanup_message,
    build_code_reviewer_message,
    build_code_tester_message,
    build_code_writer_message,
    build_navigation_agent_message,
    build_product_analyzer_message,
    build_scraper_analyzer_message,
    build_site_analyzer_message,
    build_dagster_converter_message,
    create_cleanup_agent,
    create_code_reviewer,
    create_code_tester,
    create_code_writer,
    create_navigation_agent,
    create_product_analyzer,
    create_scraper_analyzer,
    create_site_analyzer,
    create_skill_learner,
    build_skill_learner_message,
    create_dagster_converter,
)
from .tools.context import set_tool_context, clear_tool_context

logger = logging.getLogger(__name__)

# Default per-agent recursion limit (langgraph counts each model+tool step).
# Raised from 100 -> 150: complex sites (e.g. AMN job-field mapping) legitimately
# exceed 100 steps.  Map entries above override per agent.  If an agent STILL
# exceeds its limit, GraphRecursionError is caught in services.py/tasks.py and
# converted to a human_approval (graceful pause) rather than failing the job.
AGENT_RECURSION_LIMIT = 150
API_MAX_RETRIES = 3
API_RETRY_DELAYS = [5, 15, 30]

# Debug toggle: when False, the post-generation scraper patches AND the
# analysis-level strategy overrides are SKIPPED, so `run_node --no-patches`
# shows the raw LLM output. Lets us prove a source-level fix makes a patch
# redundant before deleting the patch. Production default is True.
_PATCHES_ENABLED = True


def _with_api_retry(func):
    """Decorator that retries on transient API connection errors."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(API_MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                exc_name = type(exc).__name__
                if exc_name == "APIConnectionError" and attempt < API_MAX_RETRIES:
                    delay = API_RETRY_DELAYS[attempt]
                    logger.warning(
                        "%s: API connection error (attempt %d/%d), retrying in %ds",
                        func.__name__,
                        attempt + 1,
                        API_MAX_RETRIES,
                        delay,
                    )
                    time.sleep(delay)
                    last_exc = exc
                else:
                    raise
        raise last_exc

    return wrapper


def _fix_json_artifact(slug: str, filename: str) -> None:
    if not slug:
        return
    try:
        root = _get_project_root()
    except Exception:
        return
    path = os.path.join(root, "workspace", slug, filename)
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        json.loads(content)
    except json.JSONDecodeError:
        try:
            fixed = re.sub(r'(?<=[^\\])\\(?!["\\/bfnrtu])', r"\\\\", content)
            with open(path, "w", encoding="utf-8") as f:
                f.write(fixed)
            json.loads(fixed)
            logger.info("_fix_json_artifact: fixed bad escapes in %s", path)
        except Exception as exc:
            logger.warning("_fix_json_artifact: could not fix %s: %s", path, exc)







def _enforce_anti_bot_strategy(analysis: dict, slug: str, filename: str) -> dict:
    """For anti-bot sites, force the strategy fields to ``playwright`` (cloak).

    Bot protection (Akamai/Cloudflare/PerimeterX) guards **API endpoints too**,
    not just HTML pages — a discovered ``internal_api``/``http_requests`` strategy
    403/400s exactly like direct HTTP does (verified: calvklein's b2c-api returns
    400). So the only reliable strategy for an anti-bot site is playwright (cloak
    is runtime-injected via STEALTH_BROWSER=cloak). code_writer is often tempted
    by a discovered API endpoint despite prompt guidance, so this rewrites the
    analysis fields deterministically before code_writer reads them.

    KEPT after verify-then-delete: `run_node code_writer` showed code_writer picks
    ``internal_api`` for anti-bot sites even with the strengthened prompt, producing
    a non-working scraper. Generic — driven by the anti_bot signal, no site names.
    """
    if not isinstance(analysis, dict) or not slug:
        return analysis
    conn = analysis.get("connectivity") or {}
    anti_bot = analysis.get("anti_bot") or {}
    method = (conn.get("method_that_worked") if isinstance(conn, dict) else "") or ""
    detected = bool(
        (isinstance(anti_bot, dict) and anti_bot.get("detected"))
        or str(method).startswith(("uc_chrome", "cloak"))
    )
    if not detected:
        return analysis
    # Strategies that won't work behind bot protection → playwright.
    _bad = ("seleniumbase", "undetected", "stealth_browser", "uc_chrome",
            "internal_api", "http_requests", "requests", "api")
    _keys = ("scraping_mechanism", "scraping_method", "strategy",
             "recommended_strategy", "mechanism")
    changed = False

    def _rewrite(d: dict) -> None:
        nonlocal changed
        for k, v in list(d.items()):
            if isinstance(v, str) and any(t in v.lower() for t in _bad):
                d[k] = "playwright"
                changed = True

    _rewrite(analysis)
    mr = analysis.get("mechanism_reassessment")
    if isinstance(mr, dict):
        _rewrite(mr)
    if not changed:
        return analysis
    try:
        path = os.path.join(_get_project_root(), "workspace", slug, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        logger.info("_enforce_anti_bot_strategy: anti-bot → playwright in %s/%s", slug, filename)
    except Exception as exc:
        logger.warning("_enforce_anti_bot_strategy: %s", exc)
    return analysis


def _patch_scraper_output_filter(slug: str, content_type: str = "") -> None:
    """Insert a content-type-aware output filter in scraper_draft.py.

    Discovery can capture non-item pages (nav/category roots, soft-404s). This
    filter drops them before the output is written: keep items that have a
    ``title`` AND at least one of the content type's core fields. For ``product``
    that's price/availability (unchanged from the old price filter); for
    ``job_posting`` it's company/location; for ``article`` author/publish_date;
    unknown types keep every item with a title. GENERIC — field set comes from
    ``src.content_types.output_filter_fields``, no per-type hardcoding here.
    """
    if not slug:
        return
    try:
        root = _get_project_root()
    except Exception:
        return
    scraper_path = os.path.join(root, "workspace", slug, "scraper_draft.py")
    if not os.path.isfile(scraper_path):
        return
    try:
        with open(scraper_path, "r", encoding="utf-8") as f:
            code = f.read()
        if "_OUTPUT_FILTER_APPLIED" in code or "_OUTPUT_PRICE_FILTER_APPLIED" in code:
            return
        from src.content_types import output_filter_fields

        fields = [f for f in output_filter_fields(content_type) if isinstance(f, str)]
        # build: keep items with a title AND any of the content type's filter fields.
        if fields:
            checks = " or ".join(f"p.get({f!r})" for f in fields)
            cond = f"p.get('title') and ({checks})"
            label = f"title+{','.join(fields)}"
        else:
            cond = "p.get('title')"
            label = "title"
        filter_code = (
            "# _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)\n"
            f"_FILTER_FIELDS = {fields!r}\n"
            "try:\n"
            "    _before = len(output.get(OUTPUT_KEY, []))\n"
            f"    output[OUTPUT_KEY] = [p for p in output.get(OUTPUT_KEY, []) if {cond}]\n"
            "    _after = len(output[OUTPUT_KEY])\n"
            "    if _before != _after:\n"
            f"        logger.info('output filter: %d → %d items (removed %d without {label})',\n"
            "                     _before, _after, _before - _after)\n"
            "except Exception:\n"
            "    pass\n"
            "\n"
        )
        # Insert before `json.dump(output` (fallback: json.dump( / output_filename)
        marker = "json.dump(output"
        idx = code.find(marker)
        if idx < 0:
            marker = "json.dump("
            idx = code.find(marker)
        if idx < 0:
            marker = "output_filename"
            idx = code.find(marker)
        if idx > 0:
            line_start = code.rfind("\n", 0, idx) + 1
            indent = code[line_start:idx]
            indented_filter = "\n".join(
                indent + line if line else line for line in filter_code.split("\n")
            )
            code = code[:line_start] + indented_filter + "\n" + code[line_start:]
            with open(scraper_path, "w", encoding="utf-8") as f:
                f.write(code)
            logger.info(
                "_patch_scraper_output_filter: inserted filter (%s) for content_type=%s",
                label, content_type or "(unknown)",
            )
        else:
            logger.warning("_patch_scraper_output_filter: could not find output write location")
    except Exception as exc:
        logger.warning("_patch_scraper_output_filter: %s", exc)




def _load_test_report(slug: str) -> dict | None:
    """Load the test report JSON from the agent's workspace folder."""
    if not slug:
        return None
    report_path = os.path.join("workspace", slug, "test_report.json")
    if not os.path.isfile(report_path):
        try:
            from django.conf import settings

            report_path = os.path.join(
                settings.PROJECT_ROOT, "workspace", slug, "test_report.json"
            )
        except Exception:
            pass
    if not os.path.isfile(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("_load_test_report: failed to parse %s: %s", report_path, exc)
    return None


def _preserve_test_report(slug: str) -> None:
    """Copy test_report.json from workspace to scrapers analysis/ for safekeeping."""
    if not slug:
        return
    try:
        import shutil
        from pathlib import Path

        root = _get_project_root()
        src = Path(root) / "workspace" / slug / "test_report.json"
        if not src.is_file():
            return
        dst_dir = Path(root) / "scrapers" / slug / "analysis"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "test_report.json"
        shutil.copy2(src, dst)
        logger.info("_preserve_test_report: copied to %s", dst)
    except Exception as exc:
        logger.warning("_preserve_test_report: failed: %s", exc)


def _load_scraper_analysis(slug: str) -> dict | None:
    """Load scraper_analysis.json from the agent's workspace folder."""
    if not slug:
        return None
    for base in (".",):
        path = os.path.join(base, "workspace", slug, "scraper_analysis.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                logger.warning(
                    "_load_scraper_analysis: failed to parse %s: %s", path, exc
                )
    try:
        from django.conf import settings

        path = os.path.join(
            settings.PROJECT_ROOT, "workspace", slug, "scraper_analysis.json"
        )
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


AGENT_RECURSION_MAP: dict[str, int] = {
    "site_analyzer": 250,
    "product_analyzer": 200,
    "navigation_agent": 200,
    "nav_skill_review": 60,
    "scraper_analyzer": 160,
    "code_writer": 120,
    "code_tester": 120,
    "cleanup": 80,
    "skill_learner": 80,
}


def _agent_config(config: RunnableConfig, agent_name: str = "") -> RunnableConfig:
    """Create a config copy with a higher recursion limit for react agents.

    React agents make many tool-call rounds (each round = 1 recursion step).
    The default limit of 25 is too low for browsing-heavy agents like
    site_analyzer.  Per-agent limits are set in AGENT_RECURSION_MAP.
    """
    limit = AGENT_RECURSION_MAP.get(agent_name, AGENT_RECURSION_LIMIT)
    agent_cfg = {**config}
    agent_cfg["recursion_limit"] = limit
    return agent_cfg


# ═══════════════════════════════════════════════════════════════════════════
# Agent wrapper nodes — bridge between deterministic graph and react agents
# ═══════════════════════════════════════════════════════════════════════════

PHASE_MAP: dict[str, str] = {
    "site_analyzer": "site_analysis",
    "navigation_agent": "navigation_explore",
    "navigation_explore": "navigation_explore",
    "navigation_synthesize": "navigation_synthesize",
    "nav_skill_review": "navigation_skill_review",
    "product_analyzer": "product_analysis",
    "scraper_analyzer": "scraper_analysis",
    "code_writer": "code_generation",
    "code_tester": "testing",
    "cleanup": "cleanup",
    "skill_learner": "skill_learning",
}


import threading


def _start_heartbeat(
    job_id: int, agent_name: str, interval: int = 300
) -> threading.Timer:
    """Start a background heartbeat that writes a SessionLog entry every
    ``interval`` seconds during long agent executions.

    The watchdog kills jobs with no SessionLog activity for 15+ minutes.
    LLM agents (code_writer, site_analyzer, etc.) are blocking calls that
    can run 15+ minutes without producing SessionLog entries. This heartbeat
    keeps the watchdog informed.

    Returns a threading.Timer that must be cancelled when the agent finishes.
    """

    def _beat() -> None:
        try:
            from scraper.models import SessionLog

            seq = SessionLog.objects.filter(job_id=job_id).count()
            SessionLog.objects.create(
                job_id=job_id,
                role=SessionLog.ROLE_SYSTEM,
                agent=agent_name,
                content=f"[HEARTBEAT] Agent {agent_name} still running...",
                seq=seq,
            )
        except Exception:
            pass
        # Schedule next beat
        timer = threading.Timer(interval, _beat)
        timer.daemon = True
        timer.start()
        _store_heartbeat_timer(timer)

    timer = threading.Timer(interval, _beat)
    timer.daemon = True
    timer.start()
    return timer


_heartbeat_timer_holder: list = []


def _store_heartbeat_timer(timer: threading.Timer) -> None:
    _heartbeat_timer_holder.append(timer)


def _stop_heartbeat(timer: threading.Timer) -> None:
    timer.cancel()
    _heartbeat_timer_holder.clear()


def _notify_phase(job_id: int, node_name: str, status: str) -> None:
    phase = PHASE_MAP.get(node_name, node_name)
    try:
        from django.utils import timezone
        from scraper.models import ScrapeJob, Step

        job = ScrapeJob.objects.get(pk=job_id)
        step, _ = Step.objects.get_or_create(job=job, phase=phase)
        step.status = status
        if status == "done":
            step.completed_at = timezone.now()
        elif status == "running" and not step.started_at:
            step.started_at = timezone.now()
        step.save()
    except Exception as exc:
        logger.warning("_notify_phase(%s, %s): %s", node_name, status, exc)

    try:
        from scraper.services import LangGraphService

        LangGraphService._publish_redis(
            job_id, {"type": "step", "phase": phase, "status": status}
        )
    except Exception:
        pass


SITE_ANALYSIS_BUDGET = 10
SITE_ANALYSIS_BUDGET_EXTENDED = 20
SITE_ANALYSIS_MAX_BUDGET = 50
PRODUCT_ANALYSIS_BUDGET = 50
PRODUCT_ANALYSIS_BUDGET_EXTENDED = 70
PRODUCT_ANALYSIS_MAX_BUDGET = 70
MAX_OUTER_RETRIES = 2

MAX_RETRY_SUMMARY_CHARS = 8000


def _read_json_artifact(root: str, slug: str, filename: str) -> dict[str, Any]:
    path = os.path.join(root, "workspace", slug, filename)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _archive_existing_scraper(slug: str) -> None:
    """Archive the current scraper.py before cleanup overwrites it."""
    if not slug:
        return
    try:
        import shutil
        from datetime import datetime, timezone as dt_timezone

        root = _get_project_root()
        scraper_path = os.path.join(root, "scrapers", slug, "scraper.py")
        if not os.path.isfile(scraper_path):
            return
        ts = datetime.now(dt_timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        archive_name = f"scraper-{slug}-{ts}.py"
        archive_path = os.path.join(root, "scrapers", slug, archive_name)
        shutil.copy2(scraper_path, archive_path)
        logger.info("_archive_existing_scraper: archived → %s", archive_name)
    except Exception as exc:
        logger.warning("_archive_existing_scraper: failed: %s", exc)


def _extract_previous_findings(
    result: dict, max_chars: int = MAX_RETRY_SUMMARY_CHARS
) -> str:
    messages = result.get("messages", [])
    parts: list[str] = []
    total_len = 0

    for msg in messages:
        content = ""
        prefix = ""

        if isinstance(msg, AIMessage):
            text = getattr(msg, "content", "")
            if text and isinstance(text, str) and len(text.strip()) > 20:
                prefix = "[Agent]"
                content = text.strip()
        elif isinstance(msg, ToolMessage):
            text = str(getattr(msg, "content", ""))
            if any(
                marker in text
                for marker in [
                    '"jsonlds"',
                    '"platformMarkers"',
                    '"algolia"',
                    '"appId"',
                    '"@type"',
                    '"jsonld_extraction"',
                ]
            ):
                prefix = "[Data]"
                content = text.strip()

        if not content or not prefix:
            continue

        chunk = f"{prefix}: {content[:2000]}"
        if total_len + len(chunk) > max_chars:
            remaining = max_chars - total_len
            if remaining > 100:
                parts.append(chunk[:remaining] + "\n[...truncated]")
            break
        parts.append(chunk)
        total_len += len(chunk)

    return "\n\n".join(parts) if parts else "(no findings extracted from previous run)"


_PLAYWRIGHT_RESULT_HEADERS = [
    "### Ran Playwright code",
    "### Page State",
    "### Result",
    "### Clicked element",
    "### Navigated to",
    "### Browser console",
]


def _summarize_tool_args(tool_name: str, args: dict) -> str:
    if "navigate" in tool_name:
        return f"Navigate to {str(args.get('url', ''))[:80]}"
    if "snapshot" in tool_name:
        return "Accessibility snapshot"
    if "evaluate" in tool_name:
        script = str(args.get("script", args.get("expression", "")))
        return f"Evaluate: {script[:120]}" if script else "Evaluate JS"
    if "click" in tool_name:
        return f"Click {str(args.get('element', args.get('selector', '')))[:80]}"
    if "type" in tool_name and "browser" in tool_name:
        return f"Type into {str(args.get('element', ''))[:60]}"
    if "wait_for" in tool_name:
        return f"Wait for {str(args.get('selector', args.get('time', '')))[:60]}"
    if tool_name == "write_file":
        path = str(args.get("path", ""))
        content = str(args.get("content", ""))
        return f"Write {path} ({len(content)} chars)"
    if tool_name == "read_file":
        return f"Read {str(args.get('path', ''))}"
    if tool_name == "edit_file":
        return f"Edit {str(args.get('path', ''))}"
    if tool_name == "search_files":
        return f"Search files: {str(args.get('pattern', ''))[:60]}"
    if tool_name == "search_content":
        return f"Search content: {str(args.get('pattern', ''))[:60]}"
    if "load_skill" in tool_name:
        return f"Load skill: {str(args.get('name', ''))}"
    if "list_skills" in tool_name:
        return "List available skills"
    if "web_fetch" in tool_name:
        return f"Fetch {str(args.get('url', ''))[:80]}"
    if "run_bash" in tool_name:
        cmd = str(args.get("command", ""))
        return f"Run: {cmd[:120]}"
    if "network_request" in tool_name:
        return f"Network request {str(args.get('requestId', ''))[:30]}"
    if "network_requests" in tool_name:
        return "List network requests"
    if "tabs" in tool_name:
        return "List browser tabs"
    json_args = json.dumps(args, default=str)
    return json_args[:150] if json_args != "{}" else tool_name


def _clean_result_summary(raw: str, max_len: int = 300) -> str:
    text = raw
    for header in _PLAYWRIGHT_RESULT_HEADERS:
        text = text.replace(header, "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = " ".join(lines)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "..."
    return cleaned


def check_accessibility(state: ScrapeState, config: RunnableConfig) -> Command:
    """Probe the target URL with LLM-based captcha verification.

    On fresh start: runs probe + LLM captcha check on each escalation method.
    If all methods hit captcha, ends the job immediately.
    If captcha-free method found, saves probe data and routes to site_analyzer.

    On resume (skip flags set): skips probe and routes to the appropriate node.
    """
    job_id = state.get("job_id", 0)

    if state.get("skip_site_analysis"):
        if state.get("skip_product_analysis"):
            if state.get("skip_code_generation"):
                return Command(goto="code_tester")
            if state.get("scraper_analysis"):
                return Command(goto="code_writer")
            return Command(goto="scraper_analyzer")
        return Command(goto="validate_analysis")

    url = state.get("product_url", "") or state.get("url", "")
    _notify_phase(job_id, "accessibility_check", "running")

    logger.info("check_accessibility: probing %s (job %s)", url[:100], job_id)

    try:
        from .tools.probe_tools import run_probe_with_captcha_check

        data = run_probe_with_captcha_check(url, render_js=True, job_id=job_id)
    except Exception as exc:
        logger.warning("check_accessibility: probe failed, continuing: %s", exc)
        _notify_phase(job_id, "accessibility_check", "done")
        return Command(goto="site_analyzer")

    if data.get("captcha_detected"):
        methods = data.get("methods_tried", [])
        captcha_type = data.get("captcha_type", "unknown")
        reasoning = data.get("captcha_reasoning", "")
        is_akamai = data.get("akamai_detected", False)
        error_msg = (
            f"Akamai Bot Manager protection detected on {url}. "
            f"All {len(methods)} probe methods across all proxy tiers "
            f"were blocked by Akamai. Site skipped."
            if is_akamai
            else f"Captcha detected: {captcha_type}. "
            f"All {len(methods)} probe methods returned captcha pages. "
            f"Methods tried: {', '.join(methods)}. "
            f"{reasoning}"
        )
        status_label = "akamai_blocked" if is_akamai else "captcha_blocked"
        logger.warning(
            "check_accessibility: %s for job %s — %s",
            status_label,
            job_id,
            error_msg[:200],
        )

        try:
            from scraper.models import ScrapeJob

            job_status = (
                ScrapeJob.STATUS_AKAMAI_BLOCKED
                if is_akamai
                else ScrapeJob.STATUS_CAPTCHA_BLOCKED
            )
            ScrapeJob.objects.filter(pk=job_id).update(
                status=job_status,
                error_message=error_msg[:2000],
                completed_at=timezone.now(),
            )
        except Exception as exc:
            logger.warning("check_accessibility: failed to update job status: %s", exc)

        _notify_phase(job_id, "accessibility_check", "done")
        return Command(
            update={
                "error_message": error_msg,
                "probe_result": data,
                "probe_url": url,
            },
            goto=END,
        )

    _notify_phase(job_id, "accessibility_check", "done")

    method = data.get("method", "unknown")
    proxy_tier = data.get("proxy_tier", "none")

    agent_probe_result: dict[str, Any] = {
        "connectivity": {
            "method_that_worked": method,
            "http_method": data.get("http_method"),
            "browser_method": data.get("browser_method"),
            "proxy_tier": proxy_tier,
            "js_rendering_needed": data.get("needs_browser", True),
            "anti_bot_detected": bool(data.get("blocked", False)),
            "spa_detected": bool(data.get("spa_detected", False)),
            "spa_framework": data.get("spa_framework", ""),
        },
        "platform": "unknown",
        "captcha_verified": True,
    }

    _persist_probe_summary(job_id, url, agent_probe_result, data)

    probe_state: dict[str, Any] = {
        "probe_result": agent_probe_result,
        "probe_url": url,
    }

    from .tools.context import update_probe_result

    update_probe_result(data)

    return Command(update=probe_state, goto="site_analyzer")


@_with_api_retry
def _invoke_site_analyzer(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    is_budget_retry = state.get("interrupt_reason") == "budget_exhausted_site"
    is_missing_artifact = state.get("interrupt_reason") == "missing_artifact_site"
    budget_retries = (
        state.get("budget_retry_count", 0)
        + (1 if is_budget_retry else 0)
        + (1 if is_missing_artifact else 0)
    )
    recursion_limit = (
        SITE_ANALYSIS_BUDGET_EXTENDED if budget_retries > 0 else SITE_ANALYSIS_BUDGET
    )
    _notify_phase(job_id, "site_analyzer", "running")
    set_tool_context(dict(state), agent_name="site_analyzer")
    try:
        logger.info(
            "_invoke_site_analyzer: starting (job %s, budget=%d, retry=%d)",
            job_id,
            recursion_limit,
            budget_retries,
        )
        messages = build_site_analyzer_message(state)

        if budget_retries > 0:
            previous_summary = state.get("budget_retry_summary", "")
            augmented = (
                "## BUDGET EXTENSION\n"
                f"Previous analysis ran out of the call budget. "
                f"You now have {recursion_limit} calls.\n\n"
                "### CRITICAL INSTRUCTION\n"
                "You MUST write site_analysis.json before running out of calls. "
                "Write the file as soon as you have enough data — do NOT explore further.\n\n"
                f"### Previous Findings\n"
                f"Use these findings to skip re-discovery. Fill any gaps and write the output file.\n\n"
                f"{previous_summary}\n\n"
                f"---\n\n"
            )
            original_content = messages[0].content
            messages = [HumanMessage(content=augmented + original_content)]

        _log_agent_context(state, "site-analyzer", messages)
        agent = create_site_analyzer(site_slug=slug)
        agent_cfg = _agent_config(config, "site_analyzer")
        hb = _start_heartbeat(job_id, "site-analyzer")
        result = agent.invoke({"messages": messages}, config=agent_cfg)
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "site-analyzer", config)
        _notify_phase(job_id, "site_analyzer", "done")

        output_exists = os.path.isfile(
            os.path.join(_get_project_root(), "workspace", slug, "site_analysis.json")
        )

        if output_exists:
            analysis = _read_json_artifact(
                _get_project_root(), slug, "site_analysis.json"
            )
            update: dict[str, Any] = {
                "messages": [],
                "site_analysis": analysis,
            }
            connectivity = analysis.get("connectivity", {})
            if connectivity:
                product_url = state.get("product_url") or ""
                update["probe_result"] = {
                    "url": product_url,
                    "connectivity": connectivity,
                    "platform": analysis.get("platform", ""),
                    "anti_bot_detected": analysis.get("anti_bot_detected", False),
                }
                update["probe_url"] = product_url
            return update

        tool_call_count = sum(
            1
            for m in (result.get("messages") or [])
            if m.__class__.__name__ == "ToolMessage"
        )
        summary = _extract_previous_findings(result)

        if recursion_limit < SITE_ANALYSIS_MAX_BUDGET and tool_call_count >= 5:
            extended_limit = min(recursion_limit + 10, SITE_ANALYSIS_MAX_BUDGET)
            logger.info(
                "_invoke_site_analyzer: auto-extending budget %d -> %d for job %s (made %d tool calls)",
                recursion_limit,
                extended_limit,
                job_id,
                tool_call_count,
            )
            augmented = (
                "## BUDGET AUTO-EXTENSION\n"
                f"You ran out of calls but made {tool_call_count} tool calls (progress detected).\n"
                f"You now have {extended_limit} calls total.\n\n"
                "### CRITICAL INSTRUCTION\n"
                "You MUST write site_analysis.json NOW. You have all the data you need. "
                "Do NOT explore further — write the output file immediately.\n\n"
                f"### Previous Findings\n{summary}\n\n---\n\n"
            )
            original_content = build_site_analyzer_message(state)[0].content
            retry_messages = [HumanMessage(content=augmented + original_content)]
            agent_cfg2 = _agent_config(config, "site_analyzer")
            result = agent.invoke({"messages": retry_messages}, config=agent_cfg2)
            _persist_agent_logs(state, result, "site-analyzer", config)
            _notify_phase(job_id, "site_analyzer", "done")

            output_exists = os.path.isfile(
                os.path.join(
                    _get_project_root(), "workspace", slug, "site_analysis.json"
                )
            )
            if output_exists:
                analysis = _read_json_artifact(
                    _get_project_root(), slug, "site_analysis.json"
                )
                return {
                    "messages": [],
                    "site_analysis": analysis,
                }
            summary = _extract_previous_findings(result)

        if budget_retries < 1:
            logger.warning(
                "_invoke_site_analyzer: site_analysis.json missing after run (job %s). "
                "Routing to human_approval for budget escalation.",
                job_id,
            )
            options = [
                "Retry with higher budget (50 calls)",
                "Continue anyway",
                "Cancel",
            ]
            return Command(
                update={
                    "messages": [],
                    "interrupt_reason": "budget_exhausted_site",
                    "interrupt_message": (
                        f"Site analysis did not complete — the agent used its call budget "
                        f"({SITE_ANALYSIS_BUDGET} calls) without writing site_analysis.json. "
                        f"This site may be complex. Choose how to proceed."
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                    "budget_retry_count": budget_retries,
                    "budget_retry_summary": summary,
                },
                goto="human_approval",
            )

        site_retries = state.get("site_analysis_retries", 0) + 1
        if site_retries < MAX_OUTER_RETRIES:
            logger.warning(
                "_invoke_site_analyzer: still no output (job %s, site_retries=%d). Offering redo.",
                job_id,
                site_retries,
            )
            options = [
                "Redo site analysis",
                "Continue without site analysis",
                "Cancel entire job",
            ]
            return Command(
                update={
                    "messages": [],
                    "interrupt_reason": "missing_artifact_site",
                    "interrupt_message": (
                        f"Site analysis could not produce site_analysis.json after extended attempts. "
                        f"The agent explored the site but didn't write the output file.\n\n"
                        f"Previous findings summary:\n{summary[:500]}\n\n"
                        f"Choose how to proceed."
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                    "budget_retry_count": budget_retries,
                    "budget_retry_summary": summary,
                    "site_analysis_retries": site_retries,
                },
                goto="human_approval",
            )

        logger.warning(
            "_invoke_site_analyzer: still no output after %d retries (job %s). Proceeding.",
            site_retries,
            job_id,
        )
        return {
            "messages": [],
            "site_analysis_retries": site_retries,
        }
    except Exception:
        _notify_phase(job_id, "site_analyzer", "failed")
        raise
    finally:
        clear_tool_context()


@_with_api_retry
def _invoke_product_analyzer(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    # Re-map mode: route_after_testing sent us here because code_tester flagged a
    # MAPPING failure (test_report.remediation.target == "mapping"). After the
    # agent re-maps the failed fields, route straight to code_writer (skipping
    # normalize/validate) so the scraper regenerates against the corrected mapping.
    _pa_test_report = state.get("test_report") or {}
    _pa_remediation = (
        _pa_test_report.get("remediation") if isinstance(_pa_test_report, dict) else None
    )
    is_remap = isinstance(_pa_remediation, dict) and _pa_remediation.get("target") == "mapping"
    if is_remap:
        logger.info(
            "_invoke_product_analyzer: RE-MAP mode (job %s) — fields %s",
            job_id, _pa_remediation.get("fields"),
        )
    is_budget_retry = state.get("interrupt_reason") == "budget_exhausted_product"
    is_missing_artifact = state.get("interrupt_reason") == "missing_artifact_product"
    budget_retries = (
        state.get("budget_retry_count", 0)
        + (1 if is_budget_retry else 0)
        + (1 if is_missing_artifact else 0)
    )
    recursion_limit = (
        PRODUCT_ANALYSIS_BUDGET_EXTENDED
        if budget_retries > 0
        else PRODUCT_ANALYSIS_BUDGET
    )
    _notify_phase(job_id, "product_analyzer", "running")
    set_tool_context(dict(state), agent_name="product_analyzer")
    try:
        logger.info(
            "_invoke_product_analyzer: starting (job %s, budget=%d, retry=%d)",
            job_id,
            recursion_limit,
            budget_retries,
        )
        messages = build_product_analyzer_message(state)

        if budget_retries > 0:
            previous_summary = state.get("budget_retry_summary", "")
            augmented = (
                "## BUDGET EXTENSION\n"
                f"Previous analysis ran out of the call budget. "
                f"You now have {recursion_limit} calls.\n\n"
                "### CRITICAL INSTRUCTION\n"
                "You MUST write product_analysis.json before running out of calls. "
                "Write the file as soon as you have enough data — do NOT explore further.\n\n"
                f"### Previous Findings\n"
                f"Use these findings to skip re-discovery. Fill any gaps and write the output file.\n\n"
                f"{previous_summary}\n\n"
                f"---\n\n"
            )
            original_content = messages[0].content
            messages = [HumanMessage(content=augmented + original_content)]

        _log_agent_context(state, "product-analyzer", messages)
        agent = create_product_analyzer(site_slug=slug)
        agent_cfg = _agent_config(config, "product_analyzer")
        hb = _start_heartbeat(job_id, "product-analyzer")
        result = agent.invoke({"messages": messages}, config=agent_cfg)
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "product-analyzer", config)
        _notify_phase(job_id, "product_analyzer", "done")

        _fix_json_artifact(slug, "product_analysis.json")

        output_exists = os.path.isfile(
            os.path.join(
                _get_project_root(), "workspace", slug, "product_analysis.json"
            )
        )

        if output_exists:
            analysis = _read_json_artifact(
                _get_project_root(), slug, "product_analysis.json"
            )
            # Anti-bot ⇒ playwright (cloak). KEPT: code_writer otherwise picks the
            # discovered API (which Akamai also guards → 400). Gated by _PATCHES_ENABLED.
            if _PATCHES_ENABLED:
                analysis = _enforce_anti_bot_strategy(analysis, slug, "product_analysis.json")
            update: dict[str, Any] = {
                "messages": [],
                "product_analysis": analysis,
            }
            if is_remap:
                remap_count = int(state.get("remap_count", 0) or 0) + 1
                logger.info(
                    "_invoke_product_analyzer: re-mapped failed fields → code_writer "
                    "(remap %d, job %s)", remap_count, job_id,
                )
                update["remap_count"] = remap_count
                return Command(goto="code_writer", update=update)
            return update

        tool_call_count = sum(
            1
            for m in (result.get("messages") or [])
            if m.__class__.__name__ == "ToolMessage"
        )
        summary = _extract_previous_findings(result)

        if recursion_limit < PRODUCT_ANALYSIS_MAX_BUDGET and tool_call_count >= 5:
            extended_limit = min(recursion_limit + 10, PRODUCT_ANALYSIS_MAX_BUDGET)
            logger.info(
                "_invoke_product_analyzer: auto-extending budget %d -> %d for job %s (made %d tool calls)",
                recursion_limit,
                extended_limit,
                job_id,
                tool_call_count,
            )
            augmented = (
                "## BUDGET AUTO-EXTENSION\n"
                f"You ran out of calls but made {tool_call_count} tool calls (progress detected).\n"
                f"You now have {extended_limit} calls total.\n\n"
                "### CRITICAL INSTRUCTION\n"
                "You MUST write product_analysis.json NOW. You have all the data you need. "
                "Do NOT explore further — write the output file immediately.\n\n"
                f"### Previous Findings\n{summary}\n\n---\n\n"
            )
            original_content = build_product_analyzer_message(state)[0].content
            retry_messages = [HumanMessage(content=augmented + original_content)]
            agent_cfg2 = _agent_config(config, "product_analyzer")
            result = agent.invoke({"messages": retry_messages}, config=agent_cfg2)
            _persist_agent_logs(state, result, "product-analyzer", config)
            _notify_phase(job_id, "product_analyzer", "done")

            _fix_json_artifact(slug, "product_analysis.json")

            output_exists = os.path.isfile(
                os.path.join(
                    _get_project_root(), "workspace", slug, "product_analysis.json"
                )
            )
            if output_exists:
                analysis = _read_json_artifact(
                    _get_project_root(), slug, "product_analysis.json"
                )
                if is_remap:
                    remap_count = int(state.get("remap_count", 0) or 0) + 1
                    logger.info(
                        "_invoke_product_analyzer: re-mapped (retry path) → code_writer "
                        "(remap %d, job %s)", remap_count, job_id,
                    )
                    return Command(
                        goto="code_writer",
                        update={"messages": [], "product_analysis": analysis, "remap_count": remap_count},
                    )
                return {
                    "messages": [],
                    "product_analysis": analysis,
                }
            summary = _extract_previous_findings(result)

        if budget_retries < 1:
            logger.warning(
                "_invoke_product_analyzer: product_analysis.json missing after run (job %s). "
                "Routing to human_approval for budget escalation.",
                job_id,
            )
            options = [
                "Retry with higher budget (70 calls)",
                "Continue anyway",
                "Cancel",
            ]
            return Command(
                update={
                    "messages": [],
                    "interrupt_reason": "budget_exhausted_product",
                    "interrupt_message": (
                        f"Product analysis did not complete — the agent used its call budget "
                        f"({PRODUCT_ANALYSIS_BUDGET} calls) without writing product_analysis.json. "
                        f"This product page may be complex. Choose how to proceed."
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                    "budget_retry_count": budget_retries,
                    "budget_retry_summary": summary,
                },
                goto="human_approval",
            )

        product_retries = state.get("product_analysis_retries", 0) + 1
        if product_retries < MAX_OUTER_RETRIES:
            logger.warning(
                "_invoke_product_analyzer: still no output (job %s, product_retries=%d). Offering redo.",
                job_id,
                product_retries,
            )
            options = [
                "Redo product analysis",
                "Continue without product analysis",
                "Cancel entire job",
            ]
            return Command(
                update={
                    "messages": [],
                    "interrupt_reason": "missing_artifact_product",
                    "interrupt_message": (
                        f"Product analysis could not produce product_analysis.json after extended attempts. "
                        f"The agent explored the page but didn't write the output file.\n\n"
                        f"Previous findings summary:\n{summary[:500]}\n\n"
                        f"Choose how to proceed."
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                    "budget_retry_count": budget_retries,
                    "budget_retry_summary": summary,
                    "product_analysis_retries": product_retries,
                },
                goto="human_approval",
            )

        logger.warning(
            "_invoke_product_analyzer: still no output after %d retries (job %s). Proceeding.",
            product_retries,
            job_id,
        )
        return {
            "messages": [],
            "product_analysis_retries": product_retries,
        }
    except Exception:
        _notify_phase(job_id, "product_analyzer", "failed")
        raise
    finally:
        clear_tool_context()


NAVIGATION_ANALYSIS_BUDGET = 40
NAVIGATION_ANALYSIS_BUDGET_EXTENDED = 60
NAVIGATION_ANALYSIS_MAX_BUDGET = 60


@_with_api_retry
def _invoke_navigation_agent(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    is_budget_retry = state.get("interrupt_reason") == "budget_exhausted_navigation"
    is_missing_artifact = state.get("interrupt_reason") == "missing_artifact_navigation"
    budget_retries = (
        state.get("budget_retry_count", 0)
        + (1 if is_budget_retry else 0)
        + (1 if is_missing_artifact else 0)
    )
    recursion_limit = (
        NAVIGATION_ANALYSIS_BUDGET_EXTENDED
        if budget_retries > 0
        else NAVIGATION_ANALYSIS_BUDGET
    )
    _notify_phase(job_id, "navigation_agent", "running")
    set_tool_context(dict(state), agent_name="navigation_agent")
    try:
        logger.info(
            "_invoke_navigation_agent: starting (job %s, budget=%d, retry=%d)",
            job_id,
            recursion_limit,
            budget_retries,
        )
        messages = build_navigation_agent_message(state)

        if budget_retries > 0:
            previous_summary = state.get("budget_retry_summary", "")
            augmented = (
                "## BUDGET EXTENSION\n"
                f"Previous navigation analysis ran out of the call budget. "
                f"You now have {recursion_limit} calls.\n\n"
                "### CRITICAL INSTRUCTION\n"
                "You MUST write navigation_analysis.json before running out of calls. "
                "Write the file as soon as you have enough data — do NOT explore further.\n\n"
                f"### Previous Findings\n"
                f"Use these findings to skip re-discovery. Fill any gaps and write the output file.\n\n"
                f"{previous_summary}\n\n"
                f"---\n\n"
            )
            original_content = messages[0].content
            messages = [HumanMessage(content=augmented + original_content)]

        _log_agent_context(state, "navigation-agent", messages)
        agent = create_navigation_agent(site_slug=slug)
        agent_cfg = _agent_config(config, "navigation_agent")
        hb = _start_heartbeat(job_id, "navigation-agent")
        result = agent.invoke({"messages": messages}, config=agent_cfg)
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "navigation-agent", config)
        _notify_phase(job_id, "navigation_agent", "done")

        output_exists = os.path.isfile(
            os.path.join(
                _get_project_root(), "workspace", slug, "navigation_analysis.json"
            )
        )

        if output_exists:
            analysis = _read_json_artifact(
                _get_project_root(), slug, "navigation_analysis.json"
            )
            return {
                "messages": [],
                "navigation_analysis": analysis,
            }

        tool_call_count = sum(
            1
            for m in (result.get("messages") or [])
            if m.__class__.__name__ == "ToolMessage"
        )
        summary = _extract_previous_findings(result)

        if recursion_limit < NAVIGATION_ANALYSIS_MAX_BUDGET and tool_call_count >= 3:
            extended_limit = min(recursion_limit + 10, NAVIGATION_ANALYSIS_MAX_BUDGET)
            logger.info(
                "_invoke_navigation_agent: auto-extending budget %d -> %d for job %s (made %d tool calls)",
                recursion_limit,
                extended_limit,
                job_id,
                tool_call_count,
            )
            augmented = (
                "## BUDGET AUTO-EXTENSION\n"
                f"You ran out of calls but made {tool_call_count} tool calls (progress detected).\n"
                f"You now have {extended_limit} calls total.\n\n"
                "### CRITICAL INSTRUCTION\n"
                "You MUST write navigation_analysis.json NOW. You have all the data you need. "
                "Do NOT explore further — write the output file immediately.\n\n"
                f"### Previous Findings\n{summary}\n\n---\n\n"
            )
            original_content = build_navigation_agent_message(state)[0].content
            retry_messages = [HumanMessage(content=augmented + original_content)]
            agent_cfg2 = _agent_config(config, "navigation_agent")
            result = agent.invoke({"messages": retry_messages}, config=agent_cfg2)
            _persist_agent_logs(state, result, "navigation-agent", config)
            _notify_phase(job_id, "navigation_agent", "done")

            output_exists = os.path.isfile(
                os.path.join(
                    _get_project_root(), "workspace", slug, "navigation_analysis.json"
                )
            )
            if output_exists:
                analysis = _read_json_artifact(
                    _get_project_root(), slug, "navigation_analysis.json"
                )
                return {
                    "messages": [],
                    "navigation_analysis": analysis,
                }
            summary = _extract_previous_findings(result)

        if budget_retries < 1:
            logger.warning(
                "_invoke_navigation_agent: navigation_analysis.json missing after run (job %s). "
                "Routing to human_approval for budget escalation.",
                job_id,
            )
            options = [
                "Retry with higher budget",
                "Continue anyway",
                "Cancel",
            ]
            return Command(
                update={
                    "messages": [],
                    "interrupt_reason": "budget_exhausted_navigation",
                    "interrupt_message": (
                        f"Navigation analysis did not complete — the agent used its call budget "
                        f"({NAVIGATION_ANALYSIS_BUDGET} calls) without writing navigation_analysis.json. "
                        f"This site may have complex navigation. Choose how to proceed."
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                    "budget_retry_count": budget_retries,
                    "budget_retry_summary": summary,
                },
                goto="human_approval",
            )

        logger.warning(
            "_invoke_navigation_agent: still no output after retries (job %s). Proceeding.",
            job_id,
        )
        return {
            "messages": [],
        }
    except Exception:
        _notify_phase(job_id, "navigation_agent", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_navigation_explore(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    """Graph wrapper for the deterministic navigation exploration node."""
    from .nodes.navigate_explore import navigate_explore as _explore

    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "navigation_explore", "running")
    try:
        result = _explore(dict(state), config)
        _notify_phase(job_id, "navigation_explore", "done")

        if isinstance(result, dict) and result.get("playwright_unavailable"):
            logger.info(
                "_invoke_navigation_explore: Playwright unavailable, "
                "interrupting for user decision (job %s)",
                job_id,
            )
            options = ["Use probe_html (no interaction)", "Retry Playwright", "Cancel"]
            return Command(
                update={
                    "navigation_findings": result.get("navigation_findings"),
                    "interrupt_reason": "playwright_unavailable",
                    "interrupt_message": (
                        "Playwright MCP is unavailable but the site is NOT Akamai-protected. "
                        "The explore fell back to HTTP but may have missed JS-rendered content.\n\n"
                        "Options:\n"
                        "- **Use probe_html**: Proceed with single-page fetch (no clicking/scrolling)\n"
                        "- **Retry Playwright**: Retry — check that the browser_service container is running\n"
                        "- **Cancel**: Abort this job"
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                },
                goto="human_approval",
            )

        # Handoff to the LLM navigation_agent when the deterministic explorer
        # detected a search form (classic_search) but couldn't get many real item
        # links from it — e.g. a JS/validation-gated POST form (locumtenens
        # QuickSearch: required-specialty + decorative-vs-real submit button). The
        # agent drives the form with browser tools + the navigation-patterns skill.
        # Threshold is generous (< 30) because listing_page.product_links can be
        # inflated by category/nav noise; classic_search detection (a multi-select
        # form was found) is the real signal of a form-driven job board.
        if isinstance(result, dict):
            # navigate_explore has inconsistent return shapes — some paths return
            # {"navigation_findings": findings, ...}, others return the bare
            # findings dict. Handle both.
            _f = result.get("navigation_findings") or result
            _lp = _f.get("listing_page") or {}
            _pl = len(_lp.get("product_links") or [])
            _hn = _f.get("homepage_nav") or {}
            _form = (
                _f.get("classic_search")
                or _hn.get("classic_search")
                or _lp.get("classic_search")
            )
            # Anti-bot guard: don't hand off to navigation_agent for anti-bot sites —
            # its MCP browser isn't cloak-enabled, so Akamai would block it. Anti-bot
            # sites (e.g. calvklein) find few links at analysis time (truncated
            # /render) but the RUNTIME scraper (cloak) gets the products, so a low
            # analysis-time count is expected + not a failure there.
            _probe = state.get("probe_result") or {}
            _ab = _probe.get("anti_bot") if isinstance(_probe, dict) else None
            _conn = _probe.get("connectivity") if isinstance(_probe, dict) else None
            _meth = (
                (_probe.get("method") if isinstance(_probe, dict) else "")
                or (_conn.get("method_that_worked") if isinstance(_conn, dict) else "")
                or ""
            )
            _anti_bot = bool(
                state.get("anti_bot_detected")
                or (isinstance(_ab, dict) and _ab.get("detected"))
                or str(_meth).startswith(("uc_chrome", "cloak"))
            )
            # Hand off when navigate_explore clearly failed to discover the catalog:
            # either a form was detected but yielded few links (form not driven well),
            # OR very few links were found at all (explore missed the listing surface).
            # The agent then decides HOW to recover — it may drive a form, re-capture
            # a backend API (via playwright_browser_network_requests), or find SEO
            # listing pages. It reads navigation_findings.json first, so a backend API
            # navigate_explore already captured is visible to it. (anti_bot guard is a
            # hard limit: the agent's MCP browser isn't cloak-enabled.)
            if not _anti_bot and ((_form and _pl < 30) or (_pl < 5)):
                logger.info(
                    "_invoke_navigation_explore: only %d product link(s) found "
                    "(form_detected=%s, anti_bot=%s) — handing off to navigation_agent (job %s)",
                    _pl,
                    bool(_form),
                    _anti_bot,
                    job_id,
                )
                return Command(
                    update={
                        "navigation_findings": _f,
                        "handoff_reason": "form_driving_needed",
                    },
                    goto="navigation_agent",
                )

        return result
    except Exception as exc:
        logger.exception("_invoke_navigation_explore failed (job %s): %s", job_id, exc)
        _notify_phase(job_id, "navigation_explore", "failed")
        return {}


def _invoke_navigation_synthesize(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    """Graph wrapper for the navigation synthesis node."""
    from .nodes.navigate_synthesize import navigate_synthesize as _synthesize

    job_id = state.get("job_id", 0)

    # If the LLM navigation_agent already wrote navigation_analysis.json (it runs
    # on the form-driven handoff path), skip re-synthesizing from raw findings —
    # the agent's structured output IS the analysis. Synthesize would otherwise
    # overwrite the agent's work with a re-reading of the (sparse) raw findings.
    try:
        slug = state.get("site_slug", "")
        na_path = os.path.join(_get_project_root(), "workspace", slug, "navigation_analysis.json")
        if state.get("handoff_reason") and os.path.isfile(na_path):
            analysis = _read_json_artifact(_get_project_root(), slug, "navigation_analysis.json")
            if analysis:
                logger.info(
                    "_invoke_navigation_synthesize: navigation_analysis.json already "
                    "written by navigation_agent (handoff) — skipping synthesis (job %s)",
                    job_id,
                )
                _notify_phase(job_id, "navigation_synthesize", "done")
                return {"messages": [], "navigation_analysis": analysis}
    except Exception as exc:
        logger.warning("_invoke_navigation_synthesize: skip-check failed: %s", exc)

    _notify_phase(job_id, "navigation_synthesize", "running")
    set_tool_context(dict(state), agent_name="navigation_synthesize")
    try:
        result = _synthesize(dict(state), config)
        _notify_phase(job_id, "navigation_synthesize", "done")

        # SOURCE FIX: navigation_synthesize (LLM) sometimes drops the product
        # URLs discovered by navigation_explore. The product links are in
        # navigation_findings.json > listing_page.product_links — merge them into
        # navigation_analysis.json > item_links.urls if missing. This ensures
        # code_writer has the correct URLs to build the scraper around, instead
        # of generating broken discovery logic. [fix data flow at the source]
        try:
            slug = state.get("site_slug", "")
            root = _get_project_root()
            nf_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
            na_path = os.path.join(root, "workspace", slug, "navigation_analysis.json")
            if os.path.isfile(nf_path) and os.path.isfile(na_path):
                import json as _json
                nf = _json.load(open(nf_path))
                na = _json.load(open(na_path))
                # product URLs are nested in listing_page.product_links (list of dicts with 'href')
                lp = nf.get("listing_page") or {}
                _raw_links = lp.get("product_links") or []
                product_urls = []
                for _rl in _raw_links:
                    if isinstance(_rl, str):
                        product_urls.append(_rl)
                    elif isinstance(_rl, dict) and _rl.get("href"):
                        product_urls.append(_rl["href"])
                if product_urls:
                    il = na.get("item_links")
                    if not isinstance(il, dict):
                        il = {}
                    existing = il.get("urls") or []
                    # Filter to strings only (some items may be dicts)
                    existing_str = [u for u in existing if isinstance(u, str)]
                    product_str = [u for u in product_urls if isinstance(u, str)]
                    if len(existing_str) < len(product_str):
                        il["urls"] = list(dict.fromkeys(existing_str + product_str))
                        na["item_links"] = il
                        with open(na_path, "w") as f:
                            _json.dump(na, f, indent=2, ensure_ascii=False)
                        logger.info(
                            "navigation_synthesize: merged %d product URLs from "
                            "findings into analysis.item_links.urls (had %d)",
                            len(product_urls), len(existing),
                        )
        except Exception as exc_merge:
            logger.warning("navigation_synthesize: URL merge failed: %s", exc_merge)

        return result
    except Exception as exc:
        logger.exception(
            "_invoke_navigation_synthesize failed (job %s): %s", job_id, exc
        )
        _notify_phase(job_id, "navigation_synthesize", "failed")
        return {}
    finally:
        clear_tool_context()


def _invoke_nav_skill_review(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    """Graph wrapper for the navigation skill review node.

    Non-blocking: any failure is logged and an empty dict returned so the
    graph proceeds to scraper_analyzer without skill updates.
    """
    from .nodes.navigate_skill_review import navigate_skill_review as _review

    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "nav_skill_review", "running")
    set_tool_context(dict(state), agent_name="nav_skill_review")
    try:
        result = _review(dict(state), config)
        _notify_phase(job_id, "nav_skill_review", "done")
        return result
    except Exception as exc:
        logger.exception(
            "_invoke_nav_skill_review failed (job %s): %s — non-blocking, "
            "continuing pipeline",
            job_id,
            exc,
        )
        _notify_phase(job_id, "nav_skill_review", "failed")
        return {}
    finally:
        clear_tool_context()


@_with_api_retry
def _invoke_scraper_analyzer(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "scraper_analyzer", "running")
    set_tool_context(dict(state), agent_name="scraper_analyzer")
    try:
        slug = state.get("site_slug", "")
        logger.info("_invoke_scraper_analyzer: starting (job %s)", job_id)
        # ── Strategy cascade ── if re-running because the prior strategy failed
        # testing, record it so the LLM picks a DIFFERENT strategy this time.
        tried = list(state.get("strategies_tried") or [])
        _prior_strategy = (state.get("scraper_analysis") or {}).get("strategy", "")
        _prior_report = state.get("test_report") or {}
        _new_tried: list = []
        if _prior_strategy and isinstance(_prior_report, dict) and _prior_report.get("overall_assessment") not in (None, "PASS"):
            try:
                from .nodes.route_after_testing import classify_test_failure
                _action, _reason = classify_test_failure(_prior_report, _prior_strategy)
                if _action == "strategy" and not any(
                    (t.get("strategy") if isinstance(t, dict) else t) == _prior_strategy
                    for t in tried
                ):
                    _new_tried = [{"strategy": _prior_strategy, "reason": _reason}]
                    logger.info(
                        "_invoke_scraper_analyzer: strategy '%s' failed (%s) — recording; "
                        "will pick a different strategy (job %s)",
                        _prior_strategy, _reason, job_id,
                    )
            except Exception as _e:
                logger.warning("_invoke_scraper_analyzer: failure classify failed: %s", _e)
        # Give the message builder the full tried-list (incl. the just-failed one).
        _state_for_msg = dict(state)
        if _new_tried:
            _state_for_msg["strategies_tried"] = tried + _new_tried
        messages = build_scraper_analyzer_message(_state_for_msg)
        _log_agent_context(state, "scraper-analyzer", messages)
        agent = create_scraper_analyzer(site_slug=slug)
        hb = _start_heartbeat(job_id, "scraper-analyzer")
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "scraper_analyzer")
        )
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "scraper-analyzer", config)
        _notify_phase(job_id, "scraper_analyzer", "done")

        analysis = _load_scraper_analysis(slug)
        # Anti-bot ⇒ playwright (cloak). KEPT (see _enforce_anti_bot_strategy note).
        if _PATCHES_ENABLED:
            analysis = _enforce_anti_bot_strategy(analysis, slug, "scraper_analysis.json")
        update: dict[str, Any] = {"messages": []}
        if analysis:
            try:
                raw_conf = float(analysis.get("confidence_score", 1.0))
            except (ValueError, TypeError):
                raw_conf = 1.0
            penalties = 0.0
            nav_findings = state.get("navigation_findings") or {}
            listing = nav_findings.get("listing_page", {})
            if (
                listing.get("product_links") is not None
                and len(listing.get("product_links", [])) == 0
            ):
                penalties += 0.15
                logger.info("confidence_adj: -0.15 (0 product links from navigation)")
            verified_selectors = analysis.get("verified_selectors", {})
            if verified_selectors:
                verified_count = sum(
                    1
                    for s in verified_selectors.values()
                    if isinstance(s, dict) and s.get("verified")
                )
                total_count = len(verified_selectors)
                if total_count > 0 and verified_count == 0:
                    penalties += 0.20
                    logger.info("confidence_adj: -0.20 (0 verified selectors)")
            if not analysis.get("jsonld_available", False) and not analysis.get(
                "jsonld_fields"
            ):
                site_analysis = state.get("site_analysis") or {}
                if not site_analysis.get("product_page_structure", {}).get(
                    "json_ld_available"
                ):
                    penalties += 0.05
                    logger.info("confidence_adj: -0.05 (no JSON-LD detected)")

            session_gated = any("oops" in e.lower() for e in nav_findings.get("errors", []))
            if session_gated and not analysis.get("warmup_required"):
                analysis["warmup_required"] = True
                analysis["warmup_url"] = state.get("url", "")
                analysis["warmup_wait_seconds"] = 5
                analysis["warmup_details"] = (
                    "Session gating detected — interior pages return 'oops!' "
                    "without visiting homepage first. Navigate to homepage, wait, "
                    "accept cookies, then proceed to product pages."
                )
                logger.info(
                    "scraper_analyzer: overriding warmup_required=True (session gated)"
                )
            adjusted = max(0.1, min(1.0, raw_conf - penalties))
            if adjusted < raw_conf:
                analysis["confidence_score"] = adjusted
                analysis["confidence_notes"] = (
                    analysis.get("confidence_notes", "")
                    + f" Adjusted from {raw_conf:.2f} to {adjusted:.2f} "
                    f"(upstream failures: -{penalties:.2f})."
                ).strip()
                logger.info(
                    "confidence_adj: %.2f -> %.2f (penalties=%.2f)",
                    raw_conf,
                    adjusted,
                    penalties,
                )
            update["scraper_analysis"] = analysis
            logger.info(
                "_invoke_scraper_analyzer: loaded scraper_analysis from workspace/%s/",
                slug,
            )
        else:
            logger.warning(
                "_invoke_scraper_analyzer: no scraper_analysis found at workspace/%s/",
                slug,
            )
        if _new_tried:
            update["strategies_tried"] = _new_tried  # append (Annotated[list, operator.add])
        return update
    except Exception:
        _notify_phase(job_id, "scraper_analyzer", "failed")
        raise
    finally:
        clear_tool_context()


@_with_api_retry
def _fix_scraper_syntax(
    agent, state: ScrapeState, config: RunnableConfig, job_id: int, slug: str,
    max_tries: int = 3,
) -> None:
    """Re-invoke code_writer to fix syntax errors in scraper_draft.py.

    code_writer has no shell tool to self-validate parseability, so the node
    does it: ast.parse the scraper, and on SyntaxError feed the exact error
    (line + message) back to code_writer for an immediate fix. This keeps
    syntax errors out of code_tester's path — code_tester should test
    FUNCTIONALITY, not parseability. Best-effort: if still unparseable after
    max_tries, return and let code_tester catch it (the prior behavior).
    """
    import ast
    from langchain_core.messages import HumanMessage

    scraper_path = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
    for attempt in range(max_tries):
        if not os.path.isfile(scraper_path):
            return
        try:
            with open(scraper_path, "r", errors="ignore") as fh:
                ast.parse(fh.read())
            if attempt > 0:
                logger.info(
                    "_invoke_code_writer: syntax fixed after %d attempt(s) (job %s)",
                    attempt, job_id,
                )
            return  # parses clean
        except SyntaxError as exc:
            logger.warning(
                "_invoke_code_writer: syntax error (attempt %d/%d) in scraper_draft.py line %s: %s",
                attempt + 1, max_tries, exc.lineno, exc.msg,
            )
            line_ctx = f"  { (exc.text or '').strip() }" if exc.text else ""
            fix_msg = [HumanMessage(content=(
                f"Your `workspace/{slug}/scraper_draft.py` has a Python syntax error and will not run:\n"
                f"  **Line {exc.lineno}: {exc.msg}**\n{line_ctx}\n\n"
                f"Read `workspace/{slug}/scraper_draft.py`, locate the error near line {exc.lineno}, "
                f"and use `edit_file` to fix ONLY the parse error — do NOT rewrite the whole scraper. "
                f"Common causes: unclosed bracket/parenthesis/quote, bad indentation, a broken "
                f"f-string, or a stray character. Fix it now."
            ))]
            hb = _start_heartbeat(job_id, "code-writer")
            try:
                result = agent.invoke(
                    {"messages": fix_msg}, config=_agent_config(config, "code_writer")
                )
                _persist_agent_logs(state, result, "code-writer", config)
            finally:
                _stop_heartbeat(hb)
    logger.error(
        "_invoke_code_writer: syntax errors persist after %d attempts (job %s) — letting code_tester catch it",
        max_tries, job_id,
    )


def _invoke_code_writer(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "code_writer", "running")
    set_tool_context(dict(state), agent_name="code_writer")
    try:
        logger.info("_invoke_code_writer: starting (job %s)", job_id)
        update = {}
        if state.get("test_report"):
            current_count = state.get("test_retry_count", 0)
            if current_count != FINAL_RETRY_SENTINEL:
                update["test_retry_count"] = current_count + 1
                logger.info(
                    "_invoke_code_writer: retry cycle %d (job %s)",
                    update["test_retry_count"],
                    job_id,
                )
                assert update["test_retry_count"] <= FINAL_RETRY_SENTINEL - 1, (
                    f"test_retry_count {update['test_retry_count']} exceeds "
                    f"MAX_TEST_RETRIES ({FINAL_RETRY_SENTINEL - 1})"
                )
            else:
                logger.info(
                    "_invoke_code_writer: FINAL retry cycle (job %s)",
                    job_id,
                )
        slug = state.get("site_slug", "")
        # Write sample URLs from nav_analysis to input_urls.json so the
        # scraper can use them in --sample mode (skip slow discovery).
        # Read from the FILE (not state) — the LLM-written file has the
        # discovered URLs; the state copy sometimes loses item_links.urls.
        try:
            import json as _json
            na_path = os.path.join(_get_project_root(), "workspace", slug, "navigation_analysis.json")
            if os.path.isfile(na_path):
                with open(na_path, "r") as _nf:
                    na = _json.load(_nf)
                il = na.get("item_links") or {}
                sample_urls = il.get("urls") or il.get("url_examples") or []
                if sample_urls:
                    iu_path = os.path.join(_get_project_root(), "workspace", slug, "input_urls.json")
                    with open(iu_path, "w") as _f:
                        _json.dump({"urls": sample_urls}, _f, indent=2)
                    logger.info("_invoke_code_writer: wrote %d sample URLs to input_urls.json", len(sample_urls))
        except Exception as _exc:
            logger.warning("_invoke_code_writer: failed to write input_urls.json: %s", _exc)

        messages = build_code_writer_message(state)
        _log_agent_context(state, "code-writer", messages)
        agent = create_code_writer(site_slug=slug)
        hb = _start_heartbeat(job_id, "code-writer")
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "code_writer")
        )
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "code-writer", config)
        _notify_phase(job_id, "code_writer", "done")
        if _PATCHES_ENABLED:
            # Strategy-drift patches REMOVED (verify-then-delete via run_node --no-patches):
            # _patch_scraper_waits, _patch_scraper_to_playwright — code_writer now emits
            # sleep(8)+domcontentloaded and pure Playwright unaided (Phase 2 prompts).
            # _patch_scraper_xvfb / _discovery / _multisource / _write_discovered_urls_to_input
            # — see deletion notes in the commit/plan.
            _ct = (state.get("content_type_config") or {}).get("content_type") or ""
            if not _ct:
                try:
                    from src.content_types import get_content_type
                    _cfg = get_content_type(state.get("page_type", "product"))
                    _ct = _cfg.name if _cfg else ""
                except Exception:
                    _ct = ""
            _patch_scraper_output_filter(slug, _ct)

        # Syntax guard: code_writer has no shell tool to self-validate, so the
        # node parses the scraper and feeds any SyntaxError back for an
        # immediate fix (keeps syntax errors out of code_tester's path).
        _fix_scraper_syntax(agent, state, config, job_id, slug)

        update["messages"] = []
        scraper_analysis = state.get("scraper_analysis") or {}
        strategy = scraper_analysis.get("strategy", "")
        if strategy:
            update["scraping_method"] = strategy
        return update
    except Exception:
        _notify_phase(job_id, "code_writer", "failed")
        raise
    finally:
        clear_tool_context()



def _invoke_code_review(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    """Read-only review of scraper_draft.py between code_writer and code_tester.

    Runs the code_reviewer agent (same context as code_writer + the written
    scraper). On issues, hands the feedback back to code_writer via
    state.review_feedback with severity-aware caps (critical: tester-invisible →
    up to MAX_CRITICAL_REVIEW_RETRIES; medium: tester-visible →
    MAX_MEDIUM_REVIEW_RETRIES then defers to code_tester). Catches logic/intent
    errors the syntax guard can't see before the expensive code_tester run.
    """
    import json as _json

    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "code_review", "running")
    set_tool_context(dict(state), agent_name="code_review")
    try:
        logger.info("_invoke_code_review: starting (job %s)", job_id)
        slug = state.get("site_slug", "")
        messages = build_code_reviewer_message(state)
        _log_agent_context(state, "code-reviewer", messages)
        agent = create_code_reviewer(site_slug=slug)
        hb = _start_heartbeat(job_id, "code-reviewer")
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "code_review")
        )
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "code-reviewer", config)
        _notify_phase(job_id, "code_review", "done")

        update: dict[str, Any] = {"messages": []}
        review_path = os.path.join(_get_project_root(), "workspace", slug, "code_review.json")
        verdict = "pass"
        issues_text = ""
        try:
            with open(review_path, "r") as fh:
                rev = _json.load(fh)
            verdict = (rev.get("verdict") or "pass").lower()
            issues = rev.get("issues") or []
            if isinstance(issues, list) and issues:
                issues_text = "\n".join(
                    f"- [{i.get('severity', '?')}] {i.get('area', '')}: {i.get('problem', '')}"
                    f"  -> FIX: {i.get('fix', '')}"
                    for i in issues if isinstance(i, dict)
                )
        except Exception as exc:
            logger.warning(
                "_invoke_code_review: could not read code_review.json (%s) — assuming pass",
                exc,
            )
            verdict = "pass"

        update["code_review_verdict"] = verdict
        # Determine the max severity from the issues (robust — accept legacy
        # "high" as critical, "low" as medium). Falls back to the reviewer's
        # verdict field if issues lack severities.
        _sevs = [(i.get("severity") or "").lower() for i in issues if isinstance(i, dict)] if isinstance(issues, list) else []
        if any(s in ("critical", "high") for s in _sevs) or verdict == "critical":
            max_sev = "critical"
        elif any(s in ("medium", "low") for s in _sevs) or verdict == "medium":
            max_sev = "medium"
        else:
            max_sev = "pass"

        # Severity-aware routing (decided HERE, atomically via Command — the old
        # conditional edge read a stale count and looped past the cap). Critical
        # (tester-invisible) gets up to MAX_CRITICAL attempts; medium
        # (tester-visible) gets MAX_MEDIUM, then defers to code_tester.
        route_to = "code_tester"
        if max_sev == "critical" and issues_text:
            cnt = (state.get("critical_retries", 0) or 0) + 1
            update["critical_retries"] = cnt
            if cnt <= MAX_CRITICAL_REVIEW_RETRIES:
                update["review_feedback"] = issues_text
                route_to = "code_writer"
                logger.info("_invoke_code_review: CRITICAL (attempt %d/%d) -> code_writer", cnt, MAX_CRITICAL_REVIEW_RETRIES)
            else:
                logger.info("_invoke_code_review: critical cap reached (%d/%d) -> code_tester", cnt, MAX_CRITICAL_REVIEW_RETRIES)
        elif max_sev == "medium" and issues_text:
            cnt = (state.get("medium_retries", 0) or 0) + 1
            update["medium_retries"] = cnt
            if cnt <= MAX_MEDIUM_REVIEW_RETRIES:
                update["review_feedback"] = issues_text
                route_to = "code_writer"
                logger.info("_invoke_code_review: MEDIUM (attempt %d/%d) -> code_writer", cnt, MAX_MEDIUM_REVIEW_RETRIES)
            else:
                logger.info("_invoke_code_review: medium cap reached (%d/%d) -> code_tester (tester will catch)", cnt, MAX_MEDIUM_REVIEW_RETRIES)
        else:
            logger.info("_invoke_code_review: PASS -> code_tester")
        return Command(goto=route_to, update=update)
    except Exception:
        _notify_phase(job_id, "code_review", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_code_tester(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    retry_count = state.get("test_retry_count", 0)
    _notify_phase(job_id, "code_tester", "running")
    if retry_count > 0:
        try:
            from scraper.models import Step

            note = "FINAL retry" if retry_count == FINAL_RETRY_SENTINEL else f"Retry cycle {retry_count}"
            Step.objects.filter(job_id=job_id, phase="testing").update(notes=note)
        except Exception:
            pass
    # _check_strategy_mismatch REMOVED (Fix B): with Phase 2 prompts, code_writer
    # emits the correct Playwright strategy unaided — verified via run_node
    # code_writer --no-patches (0 seleniumbase). The deterministic pre-test guard
    # no longer fires; strategy drift is handled by the normal test→retry loop.
    set_tool_context(dict(state), agent_name="code_tester")
    try:
        logger.info("_invoke_code_tester: starting (job %s)", job_id)
        messages = build_code_tester_message(state)
        _log_agent_context(state, "code-tester", messages)
        slug = state.get("site_slug", "")
        agent = create_code_tester(site_slug=slug)
        hb = _start_heartbeat(job_id, "code-tester")
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "code_tester")
        )
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "code-tester", config)
        _notify_phase(job_id, "code_tester", "done")
        update = {"messages": []}
        report = _load_test_report(slug)
        if report:
            update["test_report"] = report
            logger.info(
                "_invoke_code_tester: loaded test_report from workspace/%s/", slug
            )
            _preserve_test_report(slug)
        else:
            logger.warning(
                "_invoke_code_tester: no test_report found at workspace/%s/", slug
            )
        return update
    except Exception:
        _notify_phase(job_id, "code_tester", "failed")
        raise
    finally:
        clear_tool_context()


@_with_api_retry
def _invoke_cleanup(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "cleanup", "running")
    set_tool_context(dict(state), agent_name="cleanup")
    try:
        logger.info("_invoke_cleanup: starting (job %s)", job_id)

        slug = state.get("site_slug", "")
        _archive_existing_scraper(slug)

        messages = build_cleanup_message(state)
        _log_agent_context(state, "cleanup", messages)
        agent = create_cleanup_agent(site_slug=slug)
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "cleanup")
        )
        _persist_agent_logs(state, result, "cleanup", config)
        _notify_phase(job_id, "cleanup", "done")
        return {"messages": []}
    except Exception:
        _notify_phase(job_id, "cleanup", "failed")
        raise
    finally:
        clear_tool_context()


@_with_api_retry
def _invoke_skill_learner(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "skill_learner", "running")
    set_tool_context(dict(state), agent_name="skill_learner")
    try:
        logger.info("_invoke_skill_learner: starting (job %s)", job_id)
        messages = build_skill_learner_message(state)
        _log_agent_context(state, "skill-learner", messages)
        slug = state.get("site_slug", "")
        agent = create_skill_learner(site_slug=slug)
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "skill-learner")
        )
        _persist_agent_logs(state, result, "skill-learner", config)
        _notify_phase(job_id, "skill_learner", "done")

        if slug:
            try:
                from django.conf import settings

                ws = os.path.join(settings.PROJECT_ROOT, "workspace", slug)
                dest_dir = os.path.join(
                    settings.PROJECT_ROOT, "scrapers", slug, "analysis"
                )
                os.makedirs(dest_dir, exist_ok=True)
                import shutil

                # Preserve learning_report.json
                report_src = os.path.join(ws, "learning_report.json")
                report_dst = os.path.join(dest_dir, "learning_report.json")
                if os.path.isfile(report_src):
                    shutil.copy2(report_src, report_dst)
                    logger.info(
                        "_invoke_skill_learner: copied learning_report.json → scrapers/%s/analysis/",
                        slug,
                    )

                # Preserve nav_learning_report.json (from nav_skill_review)
                nav_report_src = os.path.join(ws, "nav_learning_report.json")
                nav_report_dst = os.path.join(dest_dir, "nav_learning_report.json")
                if os.path.isfile(nav_report_src):
                    shutil.copy2(nav_report_src, nav_report_dst)
                    logger.info(
                        "_invoke_skill_learner: copied nav_learning_report.json → scrapers/%s/analysis/",
                        slug,
                    )
            except Exception as exc:
                logger.debug("skill_learner: failed to preserve reports: %s", exc)

        return {"messages": []}
    except Exception:
        _notify_phase(job_id, "skill_learner", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_dagster_converter(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any]:
    """Post-completion agent: convert the existing scraper into the client's
    BaseTlsScraper format. Non-blocking — failure is logged but doesn't affect
    the job status."""
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")

    # Only run if the scraper exists + job succeeded
    try:
        root = _get_project_root()
        scraper_exists = os.path.isfile(
            os.path.join(root, "scrapers", slug, "scraper.py")
        ) or os.path.isfile(
            os.path.join(root, "workspace", slug, "scraper_draft.py")
        )
        if not scraper_exists:
            logger.info("_invoke_dagster_converter: no scraper found for %s — skipping", slug)
            return {"messages": []}
    except Exception:
        return {"messages": []}

    set_tool_context(dict(state), agent_name="dagster_converter")
    try:
        logger.info("_invoke_dagster_converter: starting (job %s, slug %s)", job_id, slug)
        messages = build_dagster_converter_message(state)
        _log_agent_context(state, "dagster-converter", messages)
        agent = create_dagster_converter(site_slug=slug)
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "dagster_converter")
        )
        _persist_agent_logs(state, result, "dagster-converter", config)

        # Check if the dagster file was written
        ws_dagster = os.path.join(root, "workspace", slug, f"{slug}_dagster.py")
        scrapers_dagster = os.path.join(root, "scrapers", slug, f"{slug}_dagster.py")
        if os.path.isfile(ws_dagster):
            # Syntax check
            try:
                import ast
                with open(ws_dagster, "r") as f:
                    ast.parse(f.read())
                # Copy to scrapers/ (persistent — survives workspace cleans)
                os.makedirs(os.path.dirname(scrapers_dagster), exist_ok=True)
                import shutil
                shutil.copy2(ws_dagster, scrapers_dagster)
                logger.info(
                    "_invoke_dagster_converter: generated %s_dagster.py (syntax OK, copied to scrapers/)",
                    slug,
                )
                return {"messages": [], "dagster_path": scrapers_dagster}
            except SyntaxError as exc:
                logger.warning(
                    "_invoke_dagster_converter: %s_dagster.py has syntax error: %s",
                    slug, exc,
                )
        else:
            logger.warning(
                "_invoke_dagster_converter: agent did not write %s_dagster.py",
                slug,
            )
        return {"messages": []}
    except Exception as exc:
        logger.warning("_invoke_dagster_converter: failed (non-blocking): %s", exc)
        return {"messages": []}
    finally:
        clear_tool_context()


def _invoke_store_job_listings(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any]:
    """Post-completion: ingest job listings from the output JSON into the DB.

    Deterministic (not an LLM agent). Reads the output file, parses the jobs array,
    and inserts/updates JobListing rows. Only runs for job content types.
    Non-blocking — failure is logged but doesn't affect the job.
    """
    page_type = state.get("page_type", "")
    exec_status = state.get("execution_status", "")
    output_file = state.get("output_file", "")
    slug = state.get("site_slug", "")
    job_id = state.get("job_id", 0)

    # Guard: only for job content types + successful execution + output exists
    if "job" not in page_type.lower():
        return {"messages": []}
    if exec_status != "SUCCESS":
        return {"messages": []}
    if not output_file:
        # Try to find the latest output file (newest mtime across workspace
        # and scrapers — see _find_newest_output for why mtime, not name-sorted).
        try:
            root = _get_project_root()
            site_folder = os.path.join(root, "scrapers", slug)
            workspace_folder = os.path.join(root, "workspace", slug)
            output_file = _find_newest_output(workspace_folder, site_folder)
        except Exception:
            output_file = ""
    if not output_file or not os.path.isfile(output_file):
        logger.info("_invoke_store_job_listings: no output file for %s — skipping", slug)
        return {"messages": []}

    try:
        import json as _json
        from datetime import datetime as _dt

        with open(output_file, "r", encoding="utf-8") as f:
            data = _json.load(f)

        # Get the output key (usually "jobs")
        ct_config = state.get("content_type_config") or {}
        output_key = ct_config.get("output_key", "jobs")
        items = data.get(output_key) or data.get("jobs") or data.get("products") or []
        if not items:
            logger.info("_invoke_store_job_listings: no items in %s — skipping", output_file)
            return {"messages": []}

        # Known fields → model columns; everything else → extra_data
        _KNOWN_FIELDS = {
            "title", "company", "location", "description", "salary",
            "job_type", "employment_type", "posted_date", "valid_through",
            "url", "job_id", "src_url", "remarks",
        }

        # Resolve the Site FK
        from scraper.models import JobListing, Site as SiteModel
        site_obj = None
        if slug:
            site_obj = SiteModel.objects.filter(slug=slug).first()

        site_name = state.get("site_name", "") or (site_obj.name if site_obj else "") or slug
        scrape_job_ref = None
        if job_id:
            from scraper.models import ScrapeJob
            scrape_job_ref = ScrapeJob.objects.filter(id=job_id).first()

        created_count = 0
        updated_count = 0
        for item in items:
            if not isinstance(item, dict):
                continue

            # Parse posted_date
            posted_raw = item.get("posted_date") or item.get("date_posted") or ""
            posted_date = None
            if posted_raw:
                for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%d/%m/%Y"):
                    try:
                        posted_date = _dt.strptime(str(posted_raw)[:19], fmt).date()
                        break
                    except ValueError:
                        continue

            valid_raw = item.get("valid_through") or ""
            valid_through = None
            if valid_raw:
                for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        valid_through = _dt.strptime(str(valid_raw)[:19], fmt).date()
                        break
                    except ValueError:
                        continue

            url = item.get("url", "")
            job_src_id = item.get("job_id") or item.get("job_number") or ""

            # Extra data: any field not in the known set
            extra = {k: v for k, v in item.items() if k not in _KNOWN_FIELDS}

            # Dedup key: (site_slug, url) — or (site_slug, job_source_id) if no url
            defaults = {
                "title": (item.get("title") or "")[:500],
                "company": (item.get("company") or "")[:300],
                "location": (item.get("location") or "")[:300],
                "description": item.get("description") or "",
                "salary": (item.get("salary") or "")[:300],
                "job_type": (item.get("job_type") or "")[:100],
                "employment_type": (item.get("employment_type") or item.get("employment_type") or "")[:100],
                "posted_date": posted_date,
                "valid_through": valid_through,
                "site_name": site_name,
                "site": site_obj,
                "scrape_job": scrape_job_ref,
                "extra_data": extra,
            }

            # Dedup: prefer url, fall back to job_source_id
            if url:
                defaults["url"] = url[:1000]
                defaults["job_source_id"] = str(job_src_id)[:200]
                obj, created = JobListing.objects.update_or_create(
                    site_slug=slug, url=url[:1000], defaults=defaults
                )
            elif job_src_id:
                defaults["job_source_id"] = str(job_src_id)[:200]
                obj, created = JobListing.objects.update_or_create(
                    site_slug=slug, job_source_id=str(job_src_id)[:200], defaults=defaults
                )
            else:
                # No dedup key — just create
                obj = JobListing.objects.create(
                    site_slug=slug, url="", job_source_id="", **defaults
                )
                created = True

            if created:
                created_count += 1
            else:
                updated_count += 1

        logger.info(
            "_invoke_store_job_listings: %d created, %d updated from %s (job %s)",
            created_count, updated_count, output_file, job_id,
        )
        return {"messages": [], "listings_stored": created_count + updated_count}
    except Exception as exc:
        logger.warning("_invoke_store_job_listings: failed (non-blocking): %s", exc)
        return {"messages": []}


def _log_agent_context(state: ScrapeState, agent_name: str, messages: list) -> None:
    """Write the agent's initial HumanMessage as a visible [CONTEXT] log entry.

    This makes the context/summary each agent receives from previous agents
    easily visible in the UI under the agent's own log section.
    """
    job_id = state.get("job_id")
    if not job_id or not messages:
        return
    context = ""
    for msg in messages:
        if hasattr(msg, "type") and msg.type == "human":
            context = getattr(msg, "content", "")
            break
    if not context:
        return
    try:
        from scraper.models import SessionLog

        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_SYSTEM,
            agent=agent_name,
            content=f"[CONTEXT] {context[:20000]}",
            seq=seq,
        )
    except Exception:
        pass


def _persist_agent_logs(
    state: ScrapeState, result: dict, agent_name: str, config: RunnableConfig
) -> None:
    """Extract messages from agent result and persist as SessionLog rows."""
    job_id = state.get("job_id")
    if not job_id:
        return
    messages = result.get("messages", [])
    if not messages:
        return

    try:
        from scraper.models import SessionLog, ToolCallLog

        seq_start = SessionLog.objects.filter(job_id=job_id).count()
        for i, msg in enumerate(messages):
            if hasattr(msg, "type"):
                role = msg.type
                content = getattr(msg, "content", "")
                if not content:
                    continue

                if role == "ai":
                    log_role = SessionLog.ROLE_ASSISTANT
                elif role == "tool":
                    log_role = SessionLog.ROLE_TOOL
                else:
                    log_role = SessionLog.ROLE_USER

                SessionLog.objects.create(
                    job_id=job_id,
                    role=log_role,
                    agent=agent_name,
                    content=str(content)[:20000],
                    seq=seq_start + i,
                )
        logger.info(
            "_persist_agent_logs: %d messages for %s (job %s)",
            len(messages),
            agent_name,
            job_id,
        )
    except Exception as exc:
        logger.warning("Failed to persist logs for %s: %s", agent_name, exc)

    try:
        from scraper.models import ToolCallLog

        call_seq_start = ToolCallLog.objects.filter(job_id=job_id).count()
        pending_calls: dict[str, Any] = {}

        for msg in messages:
            if getattr(msg, "type", "") == "ai":
                tool_calls = getattr(msg, "tool_calls", None)
                if not tool_calls:
                    continue
                for tc in tool_calls:
                    tc_id = tc.get("id", "")
                    args_summary = _summarize_tool_args(
                        tc.get("name", ""), tc.get("args", {})
                    )
                    tcl = ToolCallLog.objects.create(
                        job_id=job_id,
                        agent=agent_name,
                        tool_name=tc.get("name", "unknown"),
                        tool_call_id=tc_id,
                        call_seq=call_seq_start,
                        args_summary=args_summary,
                    )
                    if tc_id:
                        pending_calls[tc_id] = tcl
                    call_seq_start += 1

        for msg in messages:
            if getattr(msg, "type", "") == "tool":
                tc_id = getattr(msg, "tool_call_id", "")
                if tc_id and tc_id in pending_calls:
                    result_text = str(getattr(msg, "content", ""))[:500]
                    result_summary = _clean_result_summary(result_text)
                    pending_calls[tc_id].result_summary = result_summary
                    pending_calls[tc_id].save(update_fields=["result_summary"])

        tool_count = len(pending_calls)
        if tool_count:
            logger.info(
                "_persist_agent_logs: %d tool calls for %s (job %s)",
                tool_count,
                agent_name,
                job_id,
            )
    except Exception as exc:
        logger.warning("Failed to persist tool calls for %s: %s", agent_name, exc)


def _persist_probe_summary(
    job_id: int, url: str, probe_result: dict, raw_data: dict
) -> None:
    """Persist check_accessibility probe result as a SessionLog entry."""
    if not job_id:
        return
    try:
        from scraper.models import SessionLog

        conn = probe_result.get("connectivity", {})
        status_code = raw_data.get("status_code", "?")
        method = conn.get("method_that_worked", "unknown")
        proxy_tier = conn.get("proxy_tier", "none")
        needs_browser = conn.get("js_rendering_needed", "?")
        anti_bot = conn.get("anti_bot_detected", False)
        http_method = conn.get("http_method")
        browser_method = conn.get("browser_method")

        summary_lines = [
            f"Probe result for {url[:80]}",
            f"  Method: {method} (proxy: {proxy_tier})",
            f"  HTTP method: {http_method or 'none'}",
            f"  Browser method: {browser_method or 'none'}",
            f"  Status code: {status_code}",
            f"  JS rendering needed: {needs_browser}",
            f"  Anti-bot detected: {anti_bot}",
        ]
        if raw_data.get("captcha_type"):
            summary_lines.append(f"  Captcha type: {raw_data['captcha_type']}")
        if raw_data.get("methods_tried"):
            summary_lines.append(
                f"  Methods tried: {', '.join(raw_data['methods_tried'])}"
            )

        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_SYSTEM,
            agent="check_accessibility",
            content="\n".join(summary_lines),
            seq=seq,
        )
    except Exception as exc:
        logger.warning("Failed to persist probe summary for job %s: %s", job_id, exc)


def _route_after_navigation_explore(state: ScrapeState) -> str:
    """Route after navigation_explore.

    Normally proceeds to navigation_synthesize.  If navigate_explore
    flagged playwright_unavailable, the node already issued a
    Command(goto="human_approval") internally — this function only
    handles the case where the state carries the flag without a Command
    (defensive fallback).
    """
    if state.get("playwright_unavailable"):
        logger.info("route_after_navigate_explore: routing to human_approval")
        return "human_approval"
    return "navigation_synthesize"


# ═══════════════════════════════════════════════════════════════════════════
# Conditional edge functions
# ═══════════════════════════════════════════════════════════════════════════


def route_from_human_approval(state: ScrapeState) -> str:
    """Route the graph after human_approval resolves.

    Handles both legacy ``{"choice": "Cancel"}`` and new
    ``{"decision": "reject", "feedback": "..."}`` format.
    """
    reason = state.get("interrupt_reason", "")
    response = state.get("human_response")

    if isinstance(response, dict):
        choice = response.get("decision", response.get("choice", ""))
        label = response.get("label", choice)
    else:
        choice = str(response) if response else ""
        label = choice

    cancel_values = {"Cancel", "Abort", "reject", "Cancel entire job"}
    if choice in cancel_values:
        logger.info("route_from_human_approval: user cancelled (%s)", reason)
        return "__end__"

    # Handle testing_exhausted BEFORE the approve_values override,
    # because "Provide feedback for final retry" has decision="approve"
    # and would get its label overwritten to "Continue anyway".
    if reason == "testing_exhausted":
        feedback = state.get("human_feedback", "")
        if label == "Provide feedback for final retry":
            if not feedback:
                logger.warning(
                    "route_from_human_approval: testing_exhausted -> final retry "
                    "requested but no feedback provided, proceeding to field_confirmation"
                )
                return "field_confirmation"
            logger.info(
                "route_from_human_approval: testing_exhausted -> scraper_analyzer "
                "(FINAL retry with user feedback: %s)",
                feedback[:200],
            )
            return Command(
                update={
                    "test_retry_count": FINAL_RETRY_SENTINEL,
                    "human_feedback": feedback,
                },
                goto="scraper_analyzer",
            )
        logger.info(
            "route_from_human_approval: testing_exhausted -> field_confirmation"
        )
        return "field_confirmation"

    # low_coverage (validate_coverage gate): honor "Retry content analysis" BEFORE
    # the approve_values override clobbers its label to "Continue anyway" (the
    # retry option is decision-type approve, so without this it silently proceeds).
    # Retry -> product_analyzer (re-map fields); anything else -> proceed.
    if reason == "low_coverage":
        if "retry" in (label or "").lower() or "retry" in (choice or "").lower():
            logger.info("route_from_human_approval: low_coverage -> retry product_analyzer")
            return "product_analyzer"
        logger.info("route_from_human_approval: low_coverage -> proceed to scraper_analyzer")
        return "scraper_analyzer"

    approve_values = {"approve", "yes", "ok", "continue", "continue anyway", "proceed"}
    if choice.lower() in approve_values:
        choice = "continue"
        label = "Continue anyway"

    routing: dict[str, str] = {
        "re_scrape": "setup_workspace",
        "retry_failed": "setup_workspace",
        "choose_mechanism": "code_writer",
        "low_coverage": "code_writer",
        "validation_failed": "field_confirmation",
        "reanalyze_exhausted": "run_execution",
        "pre_execution": "run_execution",
        "skill_approval": "skill_learner",
        "field_confirmation": "run_execution",
        "playwright_unavailable": "navigation_synthesize",
        "review": "run_execution",
    }

    if reason == "low_confidence":
        if "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: low_confidence -> continue to product_analyzer"
            )
            return "product_analyzer"
        logger.info(
            "route_from_human_approval: low_confidence -> retry setup_workspace"
        )
        return "setup_workspace"

    if reason in (
        "budget_exhausted_site",
        "budget_exhausted_product",
        "budget_exhausted_navigation",
    ):
        if "retry" in (label or "").lower() or "higher budget" in (label or "").lower():
            target = (
                "site_analyzer"
                if "site" in reason
                else (
                    "navigation_explore"
                    if "navigation" in reason
                    else "product_analyzer"
                )
            )
            logger.info(
                "route_from_human_approval: %s -> retry %s with higher budget",
                reason,
                target,
            )
            return target
        if "continue" in (label or "").lower():
            logger.info("route_from_human_approval: %s -> continue anyway", reason)
            if reason in ("budget_exhausted_site", "budget_exhausted_navigation"):
                return "scraper_analyzer"
            return "normalize_fields"
        logger.info("route_from_human_approval: %s -> cancelled", reason)
        return "__end__"

    if reason == "missing_artifact_site":
        if "redo" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_site -> redo site_analyzer"
            )
            return "site_analyzer"
        if "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_site -> continue without"
            )
            return "update_tracker_analysis"
        logger.info("route_from_human_approval: missing_artifact_site -> cancelled")
        return "__end__"

    if reason == "missing_artifact_product":
        if "redo" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_product -> redo product_analyzer"
            )
            return "product_analyzer"
        if "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_product -> continue without"
            )
            return "scraper_analyzer"
        logger.info("route_from_human_approval: missing_artifact_product -> cancelled")
        return "__end__"

    if reason == "missing_artifact_navigation":
        if "redo" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_navigation -> redo navigation_explore"
            )
            return "navigation_explore"
        if "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: missing_artifact_navigation -> continue without"
            )
            return "scraper_analyzer"
        logger.info(
            "route_from_human_approval: missing_artifact_navigation -> cancelled"
        )
        return "__end__"

    if reason == "playwright_unavailable":
        if "retry" in (label or "").lower() or "playwright" in (label or "").lower():
            logger.info(
                "route_from_human_approval: playwright_unavailable -> retry navigation_explore"
            )
            return "navigation_explore"
        if "probe_html" in (label or "").lower() or "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: playwright_unavailable -> proceed with probe_html"
            )
            return "navigation_synthesize"
        logger.info("route_from_human_approval: playwright_unavailable -> cancelled")
        return "__end__"

    next_node = routing.get(reason, "cleanup")
    logger.info("route_from_human_approval: reason=%s -> %s", reason, next_node)
    return next_node


# ═══════════════════════════════════════════════════════════════════════════
# Graph builder
# ═══════════════════════════════════════════════════════════════════════════


def build_scrape_graph(
    checkpointer: Optional[Any] = None,
) -> CompiledStateGraph:
    """Build and compile the full scraping StateGraph.

    The graph is assembled with:

    * 6 LLM agent subgraphs (site_analyzer, product_analyzer, code_writer,
      code_tester, cleanup, skill_learner)
    * 12 deterministic nodes (parse_command, check_tracker, setup_workspace,
      update_tracker_analysis, validate_analysis, validate_coverage,
      field_confirmation, pre_execution_approval, run_execution,
      route_after_testing, route_after_cleanup, human_approval)
    * 3 conditional edges (check_tracker → Command-based routing,
      route_after_testing, route_after_cleanup, route_from_human_approval)

    Args:
        checkpointer: Optional LangGraph checkpointer.  When ``None``,
            ``get_checkpointer()`` is used to obtain a PostgresSaver.

    Returns:
        A compiled ``StateGraph`` ready to invoke.
    """
    if checkpointer is None:
        try:
            from .checkpointer import get_checkpointer

            checkpointer = get_checkpointer()
        except Exception as exc:
            logger.warning(
                "Could not create Postgres checkpointer, running without persistence: %s",
                exc,
            )
            checkpointer = None

    workflow = StateGraph(ScrapeState)

    # ── Add nodes (in execution order so the Mermaid diagram reads top-to-bottom) ─
    # Setup
    workflow.add_node("parse_command", parse_command)
    workflow.add_node("check_tracker", check_tracker)
    workflow.add_node("setup_workspace", setup_workspace)
    workflow.add_node("check_accessibility", check_accessibility)
    # Analysis
    workflow.add_node("site_analyzer", _invoke_site_analyzer)
    workflow.add_node("navigation_explore", _invoke_navigation_explore)
    workflow.add_node("navigation_agent", _invoke_navigation_agent)
    workflow.add_node("navigation_synthesize", _invoke_navigation_synthesize)
    workflow.add_node("product_analyzer", _invoke_product_analyzer)
    workflow.add_node("update_tracker_analysis", update_tracker_analysis)
    workflow.add_node("validate_analysis", validate_analysis)
    workflow.add_node("normalize_fields", normalize_fields)
    workflow.add_node("validate_coverage", validate_coverage)
    # Generation & testing
    workflow.add_node("scraper_analyzer", _invoke_scraper_analyzer)
    workflow.add_node("code_writer", _invoke_code_writer)
    workflow.add_node("code_review", _invoke_code_review)
    workflow.add_node("code_tester", _invoke_code_tester)
    workflow.add_node("field_confirmation", field_confirmation)
    workflow.add_node("pre_execution_approval", pre_execution_approval)
    # Execution & post-completion
    workflow.add_node("run_execution", run_execution)
    workflow.add_node("cleanup", _invoke_cleanup)
    workflow.add_node("nav_skill_review", _invoke_nav_skill_review)
    workflow.add_node("skill_learner", _invoke_skill_learner)
    workflow.add_node("dagster_converter", _invoke_dagster_converter)
    workflow.add_node("store_job_listings", _invoke_store_job_listings)
    # Generic human-in-the-loop handler (reached from many gates)
    workflow.add_node("human_approval", human_approval)

    # ── Wire edges ──────────────────────────────────────────────────────

    # START → parse_command → check_tracker
    workflow.add_edge(START, "parse_command")
    workflow.add_edge("parse_command", "check_tracker")

    # check_tracker uses Command-based routing internally (no conditional
    # edge needed — the node itself decides goto).
    # From check_tracker, Command goto may be: setup_workspace, human_approval, __end__

    # setup_workspace → check_accessibility (probe + captcha check)
    workflow.add_edge("setup_workspace", "check_accessibility")

    # check_accessibility uses Command-based routing (skip flags on resume,
    # or probe result on first pass). goto may be: site_analyzer,
    # validate_analysis, scraper_analyzer, code_writer, code_tester, or END.

    # site_analyzer → conditional (navigation_explore vs update_tracker_analysis)
    workflow.add_conditional_edges(
        "site_analyzer",
        _route_after_site_analyzer,
        {
            "navigation_explore": "navigation_explore",
            "update_tracker_analysis": "update_tracker_analysis",
        },
    )

    # navigation_explore → conditional (human_approval if Playwright down, else navigation_synthesize).
    # navigate_explore may also return Command(goto="navigation_agent") when it detects a form-driven
    # site it can't drive deterministically (low product links + form detected) — the LLM navigation_agent
    # then drives the form with browser tools + skills, and flows into navigation_synthesize.
    workflow.add_conditional_edges(
        "navigation_explore",
        _route_after_navigation_explore,
        {
            "navigation_synthesize": "navigation_synthesize",
            "human_approval": "human_approval",
        },
    )
    workflow.add_edge("navigation_agent", "navigation_synthesize")
    workflow.add_edge("navigation_synthesize", "product_analyzer")

    # update_tracker_analysis → validate_analysis
    workflow.add_edge("update_tracker_analysis", "validate_analysis")

    # validate_analysis uses Command-based routing internally.
    # From validate_analysis, Command goto may be: product_analyzer,
    # human_approval, code_writer

    # product_analyzer → normalize_fields → validate_coverage
    workflow.add_edge("product_analyzer", "normalize_fields")
    workflow.add_edge("normalize_fields", "validate_coverage")

    # validate_coverage uses Command-based routing internally.
    # From validate_coverage, Command goto may be: scraper_analyzer,
    # human_approval, code_tester

    # scraper_analyzer → code_writer
    workflow.add_edge("scraper_analyzer", "code_writer")

    # code_writer → code_review. The node returns Command(goto=code_tester|code_writer)
    # atomically (cap enforced in-node), so no conditional edge is needed here.
    workflow.add_edge("code_writer", "code_review")

    # code_tester → route_after_testing (conditional)
    workflow.add_conditional_edges(
        "code_tester",
        route_after_testing,
        {
            "field_confirmation": "field_confirmation",
            "scraper_analyzer": "scraper_analyzer",
            "product_analyzer": "product_analyzer",
            "code_writer": "code_writer",
            "human_approval": "human_approval",
            "cleanup": "cleanup",
        },
    )

    # field_confirmation uses Command-based routing internally (goto is
    # either pre_execution_approval or product_analyzer).
    # No conditional edge needed — the Command decides.

    # pre_execution_approval uses Command-based routing internally (goto is
    # either run_execution or cleanup).
    # No conditional edge needed — the Command decides.

    # run_execution → cleanup (B2: cleanup always runs, never throws)
    workflow.add_edge("run_execution", "cleanup")

    # cleanup → nav_skill_review (capture navigation learnings post-scrape)
    workflow.add_edge("cleanup", "nav_skill_review")

    # nav_skill_review → skill_learner (or END if skill_learner skipped)
    workflow.add_edge("nav_skill_review", "skill_learner")

    # skill_learner → dagster_converter → store_job_listings → END
    workflow.add_edge("skill_learner", "dagster_converter")
    workflow.add_edge("dagster_converter", "store_job_listings")
    workflow.add_edge("store_job_listings", END)

    # human_approval → conditional resume routing
    workflow.add_conditional_edges(
        "human_approval",
        route_from_human_approval,
        {
            "setup_workspace": "setup_workspace",
            "scraper_analyzer": "scraper_analyzer",
            "code_writer": "code_writer",
            "field_confirmation": "field_confirmation",
            "run_execution": "run_execution",
            "skill_learner": "skill_learner",
            "product_analyzer": "product_analyzer",
            "navigation_explore": "navigation_explore",
            "navigation_synthesize": "navigation_synthesize",
            "nav_skill_review": "nav_skill_review",
            "site_analyzer": "site_analyzer",
            "update_tracker_analysis": "update_tracker_analysis",
            "normalize_fields": "normalize_fields",
            "cleanup": "cleanup",
            "__end__": END,
        },
    )

    # ── Compile ─────────────────────────────────────────────────────────
    compiled = workflow.compile(checkpointer=checkpointer)

    logger.info("Scrape graph compiled with %d nodes", len(workflow.nodes))
    for node_name in workflow.nodes:
        logger.info("  node: %s", node_name)

    return compiled


# ═══════════════════════════════════════════════════════════════════════════
# Edge helper functions (not exposed as nodes)
# ═══════════════════════════════════════════════════════════════════════════


def route_from_setup_workspace(state: ScrapeState) -> str:
    """Decide which analysis phase to enter after workspace setup.

    Uses skip flags set by ``check_tracker`` for resume logic (B1).

    IMPORTANT: Navigation jobs (input_mode=navigation|list_page) always need
    to run navigation_explore even when site_analysis is skipped (re-scrape),
    because navigation discovers the product URLs that the scraper needs.
    """
    input_mode = state.get("input_mode", "url_list")

    # Navigation/list_page/search_term jobs always need navigation_explore,
    # even on re-scrape, because navigation discovers the product URLs.
    if input_mode in ("navigation", "list_page", "search_term") and not state.get(
        "navigation_analysis"
    ):
        return "site_analyzer"  # → _route_after_site_analyzer → navigation_explore

    if state.get("skip_site_analysis"):
        if state.get("skip_product_analysis"):
            if state.get("skip_code_generation"):
                return "code_tester"
            if state.get("scraper_analysis"):
                return "code_writer"
            return "scraper_analyzer"
        return "validate_analysis"
    return "site_analyzer"


def _route_after_site_analyzer(state: ScrapeState) -> str:
    """Route after site_analyzer based on input_mode.

    - navigation/list_page/search_term → navigation_explore (deterministic exploration + LLM synthesis)
    - url_list → update_tracker_analysis (existing product/content analysis flow)
    """
    input_mode = state.get("input_mode", "url_list")
    if input_mode in ("navigation", "list_page", "search_term"):
        logger.info(
            "_route_after_site_analyzer: input_mode=%s → navigation_explore",
            input_mode,
        )
        return "navigation_explore"
    logger.info(
        "_route_after_site_analyzer: input_mode=%s → update_tracker_analysis",
        input_mode,
    )
    return "update_tracker_analysis"


__all__ = ["build_scrape_graph"]
