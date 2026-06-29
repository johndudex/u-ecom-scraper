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
from .state import ScrapeState
from .subagents import (
    build_cleanup_message,
    build_code_tester_message,
    build_code_writer_message,
    build_navigation_agent_message,
    build_product_analyzer_message,
    build_scraper_analyzer_message,
    build_site_analyzer_message,
    build_skill_learner_message,
    create_cleanup_agent,
    create_code_tester,
    create_code_writer,
    create_navigation_agent,
    create_product_analyzer,
    create_scraper_analyzer,
    create_site_analyzer,
    create_skill_learner,
)
from .tools.context import set_tool_context, clear_tool_context

logger = logging.getLogger(__name__)

AGENT_RECURSION_LIMIT = 100
API_MAX_RETRIES = 3
API_RETRY_DELAYS = [5, 15, 30]


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


def _patch_scraper_xvfb(slug: str) -> None:
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
        patched = code.replace("SB(uc=True, xvfb=True,", "SB(uc=True, xvfb=args.xvfb,")
        if patched != code:
            with open(scraper_path, "w", encoding="utf-8") as f:
                f.write(patched)
            logger.info("_patch_scraper_xvfb: patched hardcoded xvfb=True → args.xvfb")
    except Exception as exc:
        logger.warning("_patch_scraper_xvfb: %s", exc)


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
    "site_analyzer": 150,
    "product_analyzer": 100,
    "navigation_agent": 120,
    "nav_skill_review": 30,
    "scraper_analyzer": 80,
    "code_writer": 60,
    "code_tester": 60,
    "cleanup": 40,
    "skill_learner": 40,
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
            update: dict[str, Any] = {
                "messages": [],
                "product_analysis": analysis,
            }
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
    from .decisions import options_to_decisions

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
                        "- **Retry Playwright**: Retry — check that the browser-service container is running\n"
                        "- **Cancel**: Abort this job"
                    ),
                    "interrupt_options": options,
                    "interrupt_decisions": options_to_decisions(options),
                },
                goto="human_approval",
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
    _notify_phase(job_id, "navigation_synthesize", "running")
    set_tool_context(dict(state), agent_name="navigation_synthesize")
    try:
        result = _synthesize(dict(state), config)
        _notify_phase(job_id, "navigation_synthesize", "done")
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
        messages = build_scraper_analyzer_message(state)
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
        update: dict[str, Any] = {"messages": []}
        if analysis:
            raw_conf = float(analysis.get("confidence_score", 1.0))
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
        return update
    except Exception:
        _notify_phase(job_id, "scraper_analyzer", "failed")
        raise
    finally:
        clear_tool_context()


@_with_api_retry
def _invoke_code_writer(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "code_writer", "running")
    set_tool_context(dict(state), agent_name="code_writer")
    try:
        logger.info("_invoke_code_writer: starting (job %s)", job_id)
        update = {}
        if state.get("test_report"):
            update["test_retry_count"] = state.get("test_retry_count", 0) + 1
            logger.info(
                "_invoke_code_writer: retry cycle %d (job %s)",
                update["test_retry_count"],
                job_id,
            )
        messages = build_code_writer_message(state)
        _log_agent_context(state, "code-writer", messages)
        slug = state.get("site_slug", "")
        agent = create_code_writer(site_slug=slug)
        hb = _start_heartbeat(job_id, "code-writer")
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "code_writer")
        )
        _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "code-writer", config)
        _notify_phase(job_id, "code_writer", "done")
        slug = state.get("site_slug", "")
        _patch_scraper_xvfb(slug)
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


@_with_api_retry
def _invoke_code_tester(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    retry_count = state.get("test_retry_count", 0)
    _notify_phase(job_id, "code_tester", "running")
    if retry_count > 0:
        try:
            from scraper.models import Step

            Step.objects.filter(job_id=job_id, phase="testing").update(
                notes=f"Retry cycle {retry_count}"
            )
        except Exception:
            pass
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

    MAX_TOTAL_RESUMES = 5
    total_resumes = state.get("test_retry_count", 0)
    if total_resumes >= MAX_TOTAL_RESUMES:
        logger.warning(
            "route_from_human_approval: hard cap reached (%d resumes) → cleanup",
            total_resumes,
        )
        return "cleanup"

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
        "testing_exhausted": "field_confirmation",
        "playwright_unavailable": "navigation_synthesize",
        "review": "run_execution",
    }

    if reason == "testing_exhausted":
        if choice in cancel_values:
            logger.info("route_from_human_approval: testing_exhausted -> cancelled")
            return "__end__"
        feedback = state.get("human_feedback", "")
        if label == "Provide feedback" and feedback:
            logger.info(
                "route_from_human_approval: testing_exhausted -> scraper_analyzer "
                "(user feedback: %s)",
                feedback[:200],
            )
            return Command(
                update={
                    "test_retry_count": 0,
                    "human_feedback": feedback,
                },
                goto="scraper_analyzer",
            )
        logger.info(
            "route_from_human_approval: testing_exhausted -> field_confirmation"
        )
        return "field_confirmation"

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

    # ── Add deterministic nodes ──────────────────────────────────────────
    workflow.add_node("parse_command", parse_command)
    workflow.add_node("check_tracker", check_tracker)
    workflow.add_node("setup_workspace", setup_workspace)
    workflow.add_node("check_accessibility", check_accessibility)
    workflow.add_node("update_tracker_analysis", update_tracker_analysis)
    workflow.add_node("validate_analysis", validate_analysis)
    workflow.add_node("normalize_fields", normalize_fields)
    workflow.add_node("validate_coverage", validate_coverage)
    workflow.add_node("field_confirmation", field_confirmation)
    workflow.add_node("pre_execution_approval", pre_execution_approval)
    workflow.add_node("run_execution", run_execution)
    workflow.add_node("human_approval", human_approval)

    # ── Add LLM agent wrapper nodes ────────────────────────────────────
    workflow.add_node("site_analyzer", _invoke_site_analyzer)
    workflow.add_node("navigation_explore", _invoke_navigation_explore)
    workflow.add_node("navigation_synthesize", _invoke_navigation_synthesize)
    workflow.add_node("nav_skill_review", _invoke_nav_skill_review)
    workflow.add_node("product_analyzer", _invoke_product_analyzer)
    workflow.add_node("scraper_analyzer", _invoke_scraper_analyzer)
    workflow.add_node("code_writer", _invoke_code_writer)
    workflow.add_node("code_tester", _invoke_code_tester)
    workflow.add_node("cleanup", _invoke_cleanup)
    workflow.add_node("skill_learner", _invoke_skill_learner)

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

    # navigation_explore → conditional (human_approval if Playwright down, else navigation_synthesize)
    workflow.add_conditional_edges(
        "navigation_explore",
        _route_after_navigation_explore,
        {
            "navigation_synthesize": "navigation_synthesize",
            "human_approval": "human_approval",
        },
    )
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

    # code_writer → code_tester
    workflow.add_edge("code_writer", "code_tester")

    # code_tester → route_after_testing (conditional)
    workflow.add_conditional_edges(
        "code_tester",
        route_after_testing,
        {
            "field_confirmation": "field_confirmation",
            "scraper_analyzer": "scraper_analyzer",
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

    # skill_learner is the last node — always goes to END
    workflow.add_edge("skill_learner", END)

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
