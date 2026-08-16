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
field_confirmation, human_approval).  The graph pauses at these points and
resumes when the user provides input.

Note: ``pre_execution_approval`` was a second consecutive gate after
``field_confirmation`` (Wave 2 Cut 2 merged it into field_confirmation — the
item-count estimate now appears in field_confirmation's interrupt and an approve
there routes straight to run_execution). The node is no longer registered here.

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
from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from langchain_core.runnables import RunnableConfig

from .constants import (
    FINAL_RETRY_SENTINEL,
)
from .decisions import options_to_decisions
from .nodes import (
    check_tracker,
    field_confirmation,
    human_approval,
    normalize_fields,
    parse_command,
    route_after_cleanup,
    route_after_testing,
    run_execution,
    setup_workspace,
    update_tracker_analysis,
    validate_analysis,
    validate_coverage,
)
from .nodes.run_execution import _find_newest_output, _read_discovery_coverage
from .state import ScrapeState
from .subagents import (
    build_cleanup_message,
    build_code_tester_message,
    build_code_writer_message,
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # build_navigation_agent_message,
    # ═══ END ARCHIVED ═══
    build_product_analyzer_message,
    build_site_analyzer_message,
    build_dagster_converter_message,
    create_cleanup_agent,
    create_code_tester,
    create_code_writer,
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # create_navigation_agent,
    # ═══ END ARCHIVED ═══
    create_product_analyzer,
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
    """For anti-bot sites, force the strategy fields to ``http_navigation`` (cloak).

    Bot protection (Akamai/Cloudflare/PerimeterX) guards **API endpoints too**,
    not just HTML pages — a discovered ``internal_api``/``http_requests`` strategy
    403/400s exactly like direct HTTP does (verified: calvklein's b2c-api returns
    400). So the only reliable strategy for an anti-bot site is a browser-backed
    one. ``http_navigation`` is the preferred anti-bot strategy because the
    browser_service ``/navigate`` endpoint supports ``stealth: "cloak"`` per call
    (CloakBrowser's C++ fingerprint patches defeat Akamai). The legacy
    ``playwright`` strategy is NOT in ``_bad`` — it stays for back-compat.

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
    # Strategies that won't work behind bot protection → http_navigation (cloak).
    # NOTE: ``playwright`` is deliberately NOT in ``_bad`` — it is retained as a
    # legacy browser strategy. Only the explicitly-bad tokens (UC, HTTP/API) are
    # rewritten to ``http_navigation``, the new preferred anti-bot strategy
    # (the /navigate endpoint applies cloak server-side via ``stealth: "cloak"``).
    _bad = ("seleniumbase", "undetected", "stealth_browser", "uc_chrome",
            "internal_api", "http_requests", "requests", "api")
    _keys = ("scraping_mechanism", "scraping_method", "strategy",
             "recommended_strategy", "mechanism")
    changed = False

    def _rewrite(d: dict) -> None:
        nonlocal changed
        for k, v in list(d.items()):
            if isinstance(v, str) and any(t in v.lower() for t in _bad):
                d[k] = "http_navigation"
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
        logger.info("_enforce_anti_bot_strategy: anti-bot → http_navigation in %s/%s", slug, filename)
    except Exception as exc:
        logger.warning("_enforce_anti_bot_strategy: %s", exc)
    return analysis


def _patch_scraper_output_filter(
    slug: str, content_type: str = "", target_fields: list | None = None
) -> None:
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
        if target_fields:
            # Custom schema: keep items with ANY of the user's requested fields.
            # Don't require title/price (product-specific) — that would strip
            # every record on non-product sites (profiles, jobs, articles).
            checks = " or ".join(f"p.get({f!r})" for f in target_fields)
            cond = checks
            label = f"any of {','.join(target_fields)}"
            fields = list(target_fields)
        else:
            from src.content_types import output_filter_fields

            fields = [f for f in output_filter_fields(content_type) if isinstance(f, str)]
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


def _enforce_discovery_import(slug: str) -> None:
    """Post-generation enforcement: ensure the generated scraper imports src.discovery.

    Modeled on _patch_scraper_output_filter (string-marker injection + idempotency
    sentinel). code_writer frequently drops the `from src.discovery import` line
    and hand-rolls inline pagination (verified across lw.com scraper-177/179/181).
    Prompt rules mandate keeping it but cannot reliably constrain codegen — this
    deterministic backstop catches the drift after generation and injects the
    import if missing.
    """
    if not slug:
        return
    try:
        draft_path = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
        if not os.path.isfile(draft_path):
            return
        with open(draft_path, "r", encoding="utf-8") as f:
            code = f.read()

        # Already compliant?
        if "from src.discovery import" in code and "discover_item_urls(" in code:
            return

        # Hand-rolled pagination detected (inline _click_load_more / _get_next_page_url)?
        has_inline_pagination = any(
            marker in code
            for marker in ("def _click_load_more", "def _get_next_page_url", "_click_load_more(page)")
        )

        if has_inline_pagination:
            logger.warning(
                "_enforce_discovery_import: %s has INLINE pagination (_click_load_more/"
                "_get_next_page_url defined) instead of src.discovery import — "
                "code_writer drifted from the template. Injecting the import as a "
                "backstop, but the inline code may still break.",
                slug,
            )

        # Inject the import after the last `from src.` or `from playwright` line
        # (top of file, column 0 — no indent needed).
        import_line = (
            "from src.discovery import discover_item_urls, config_for_load_more  "
            "# _DISCOVERY_IMPORT_APPLIED (enforced — do not remove)"
        )
        if "from src.discovery import" not in code:
            # Find insertion point: after last top-level import
            last_import = max(
                code.rfind("\nfrom src."),
                code.rfind("\nfrom playwright"),
                code.rfind("\nimport "),
            )
            if last_import > 0:
                # Insert after the import line (find the newline after it)
                line_end = code.find("\n", last_import + 1)
                if line_end > 0:
                    code = code[:line_end + 1] + import_line + "\n" + code[line_end + 1:]
                else:
                    code = import_line + "\n" + code
            else:
                code = import_line + "\n" + code
            logger.info("_enforce_discovery_import: injected src.discovery import into %s", slug)

        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as exc:
        logger.warning("_enforce_discovery_import: %s", exc)


def _enforce_env_discovery_gate(slug: str) -> None:
    """Post-generation enforcement: ensure the SCRAPER_LISTING_URL env-var gate
    has its initializer lines.

    code_writer drops the ``_env_listing = os.environ.get("SCRAPER_LISTING_URL"…)``
    and ``_env_force = …`` assignments but KEEPS the consumer
    (``if/elif _env_listing or _env_force or args.fresh_discovery…``). Any
    ``--listing-url`` / ``--fresh-discovery`` / ``SCRAPER_LISTING_URL`` invocation
    then raises ``NameError`` before discovery runs (verified: lw.com scraper-187
    crashed at run_execution, exit code 1 in 0s). Invisible to code_tester
    because ``--sample`` takes a different branch — the classic scratch-vs-exec
    blind-spot. Modeled on _enforce_discovery_import.
    """
    if not slug:
        return
    try:
        draft_path = os.path.join(_get_project_root(), "workspace", slug, "scraper_draft.py")
        if not os.path.isfile(draft_path):
            return
        with open(draft_path, "r", encoding="utf-8") as f:
            code = f.read()

        # Already compliant?
        if "_ENV_GATE_APPLIED" in code or '_env_listing = os.environ.get("SCRAPER_LISTING_URL"' in code:
            return

        # Find the consumer: the if/elif that references _env_listing / _env_force.
        consumer_re = re.compile(r"(?m)^(\s*)(elif|if)\s+_env_listing\b.*:\s*$")
        m = consumer_re.search(code)
        if not m:
            return  # no env-gate consumer → nothing to enforce
        indent, kw = m.group(1), m.group(2)

        # If it's an `elif`, the initializers must be defined before the WHOLE
        # if/elif chain evaluates, so insert before the chain's leading `if`.
        insert_pos = m.start()
        if kw == "elif":
            chain_re = re.compile(r"(?m)^" + re.escape(indent) + r"if\b.*:\s*$")
            chain_matches = list(chain_re.finditer(code, 0, m.start()))
            if chain_matches:
                insert_pos = chain_matches[-1].start()

        init_lines = (
            f'{indent}_env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()'
            "  # _ENV_GATE_APPLIED (enforced — do not remove)\n"
            f'{indent}_env_force = os.environ.get("SCRAPER_FORCE_DISCOVERY", "").strip().lower() in ("1", "true", "yes")\n'
        )
        code = code[:insert_pos] + init_lines + code[insert_pos:]
        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(code)
        logger.info("_enforce_env_discovery_gate: injected env-var initializers into %s", slug)
    except Exception as exc:
        logger.warning("_enforce_env_discovery_gate: %s", exc)


def _warn_unaddressed_critical_fix(slug: str, scraper_analysis: dict) -> None:
    """Deterministic backstop for the critical_fix loop.

    After code_writer regenerates ``scraper_draft.py``, check whether any
    selector documented as non-existent in ``scraper_analysis.critical_fix``
    still appears in the generated code. If it does, log a prominent warning —
    the documented defect was not addressed and the next code_tester run will
    almost certainly crash the same way.

    This does NOT mutate the scraper (selector choice is the LLM's call); it
    surfaces the regression loudly so it is visible in logs and the code_review
    loop can act on it. GENERIC — runs for any site whose analyzer wrote a
    ``critical_fix`` block.
    """
    if not slug or not isinstance(scraper_analysis, dict):
        return
    critical_fix = scraper_analysis.get("critical_fix") or {}
    if not isinstance(critical_fix, dict) or not critical_fix:
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
        # Extract the FAILED selector from the crash message in `issue` — the
        # pattern is `selector 'X'` / `selector "X"` (Playwright/Selenium
        # wording). That selector is definitively broken on the page.
        import re as _re

        blob = " ".join(
            str(critical_fix.get(k, ""))
            for k in ("issue", "root_cause", "fix")
            if critical_fix.get(k)
        )
        # Mark selectors flagged as non-existent; the issue text usually reads
        # "DOES NOT EXIST" / "does not exist" near the broken selector.
        forbidden: list[str] = []
        for m in _re.finditer(r"selector\s*['\"]([^'\"]+)['\"]", blob, flags=_re.IGNORECASE):
            forbidden.append(m.group(1))
        # Dedupe while preserving order.
        seen: set[str] = set()
        forbidden = [s for s in forbidden if not (s in seen or seen.add(s))]
        if not forbidden:
            return
        offenders = [s for s in forbidden if s in code]
        if offenders:
            logger.warning(
                "_warn_unaddressed_critical_fix: %s STILL CONTAINS documented-"
                "non-existent selector(s) %s despite critical_fix — code_tester "
                "will likely crash again (slug=%s)",
                os.path.basename(scraper_path), offenders, slug,
            )
        else:
            logger.info(
                "_warn_unaddressed_critical_fix: OK — documented-non-existent "
                "selector(s) %s absent from regenerated scraper (slug=%s)",
                forbidden, slug,
            )
    except Exception as exc:
        logger.warning("_warn_unaddressed_critical_fix: %s", exc)




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


def _attach_discovery_coverage(report: dict, slug: str) -> dict:
    """Deterministically attach the scraper's ``discovery_coverage`` to the test report.

    code_tester's LLM-written ``test_report.json`` does not reliably carry the
    ``discovery_coverage`` block the scraper emits in its output metadata. Read it
    from the test scrape's output file and inject it so the coverage-aware
    classifier (``route_after_testing._discovery_coverage_failure``) can see it.
    No-op when there is no output file or no block (url_list scrapers, Phase 1 not
    run) — keeps the gate dormant rather than erroring.
    """
    if not isinstance(report, dict) or not slug:
        return report
    try:
        from django.conf import settings

        root = settings.PROJECT_ROOT
    except Exception:
        root = "."
    workspace_dir = os.path.join(root, "workspace", slug)
    site_dir = os.path.join(root, "scrapers", slug)
    try:
        output_file = _find_newest_output(workspace_dir, site_dir, slug=slug)
    except Exception as exc:
        logger.debug("_attach_discovery_coverage: newest-output lookup failed: %s", exc)
        output_file = None
    if not output_file:
        return report
    try:
        cov = _read_discovery_coverage(output_file)
        if isinstance(cov, dict):
            report["discovery_coverage"] = cov
            logger.info(
                "_attach_discovery_coverage: attached (stop_reason=%s, found=%s, "
                "dims=%s/%s) from %s",
                cov.get("stop_reason"),
                cov.get("found"),
                cov.get("dimensions_iterated"),
                cov.get("dimensions_total"),
                os.path.basename(output_file),
            )
    except Exception as exc:
        logger.warning("_attach_discovery_coverage: failed to read %s: %s", output_file, exc)
    return report


def _preserve_test_report(slug: str) -> None:
    """Copy test_report.json from LOCAL workspace to the File Master (scrapers analysis/)."""
    if not slug:
        return
    try:
        import src.artifacts as artifacts

        root = _get_project_root()
        src = os.path.join(root, "workspace", slug, "test_report.json")  # LOCAL
        if not os.path.isfile(src):
            return
        with open(src, "rb") as _f:
            _bytes = _f.read()
        dst_key = artifacts.scrapers_key(slug, "analysis", "test_report.json")
        artifacts.write(dst_key, _bytes)
        logger.info("_preserve_test_report: copied to %s", dst_key)
    except Exception as exc:
        logger.warning("_preserve_test_report: failed: %s", exc)


AGENT_RECURSION_MAP: dict[str, int] = {
    "site_analyzer": 250,
    "product_analyzer": 200,
    # ARCHIVED: "navigation_agent": 200,
    "nav_skill_review": 60,
    "scraper_analyzer": 160,
    "code_writer": 120,  # recursion limit — high enough to finish (read+write+test+fix
                         # needs ~25 steps). The wall-clock cap (_invoke_agent_with_timeout
                         # at 900s) is the real backstop; this just prevents the react loop
                         # from iterating past GraphRecursionError.
    "code_tester": 120,
    "cleanup": 80,
    "skill_learner": 80,
}


class _ToolCallLogger(BaseCallbackHandler):
    """Write a SessionLog per tool call, in real time.

    Why this exists: agent tool calls are batch-persisted only AFTER
    ``agent.invoke()`` returns (``_persist_agent_logs``). During a long run
    (code_writer ~15 min) the only SessionLog entries are content-free heartbeats
    — the job *looks* idle/hung while actively working, which caused a healthy
    run to be misdiagnosed as a hang and cancelled. This callback writes a
    SessionLog on every ``on_tool_start`` so monitoring sees real progress
    (which tool, which args) as it happens, and can distinguish slow-but-working
    from a genuinely stuck LLM call. Generic — attached centrally in
    ``_agent_config`` so every agent benefits.
    """

    def __init__(self, job_id: int, agent_name: str) -> None:
        self.job_id = job_id
        self.agent_name = agent_name

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:  # type: ignore[override]
        try:
            name = ""
            if isinstance(serialized, dict):
                name = serialized.get("name") or ""
            from scraper.models import SessionLog

            seq = SessionLog.objects.filter(job_id=self.job_id).count()
            SessionLog.objects.create(
                job_id=self.job_id,
                # P0-18: tool-call traces are NOT assistant messages. Writing
                # them as ROLE_ASSISTANT polluted the agent-summary view (tool
                # noise masqueraded as the agent's reasoning text). Use
                # ROLE_SYSTEM so "assistant" means real LLM output only.
                role=SessionLog.ROLE_SYSTEM,
                agent=self.agent_name,
                content=f"[TOOL] {name}: {(input_str or '')[:140]}",
                seq=seq,
            )
        except Exception:
            # A logging callback must NEVER crash the agent it observes.
            pass


def _agent_config(config: RunnableConfig, agent_name: str = "") -> RunnableConfig:
    """Create a config copy with a higher recursion limit for react agents.

    React agents make many tool-call rounds (each round = 1 recursion step).
    The default limit of 25 is too low for browsing-heavy agents like
    site_analyzer.  Per-agent limits are set in AGENT_RECURSION_MAP.

    Also attaches ``_ToolCallLogger`` (real-time tool-call SessionLog entries)
    when ``job_id`` is present in the config metadata, so long agent runs don't
    look idle. [progress-visibility fix]
    """
    limit = AGENT_RECURSION_MAP.get(agent_name, AGENT_RECURSION_LIMIT)
    agent_cfg = {**config}
    agent_cfg["recursion_limit"] = limit
    # Real-time tool-call logging: job_id is placed in config metadata by
    # LangGraphService.get_config, so it propagates to every node. Attaching
    # here (not per-invoke-site) covers all agents in one place.
    job_id = (config.get("metadata") or {}).get("job_id") if isinstance(config, dict) else None
    if job_id:
        # P0-18: Canonicalize the agent name to the hyphenated display form
        # (matching _persist_agent_logs). Without this, _ToolCallLogger writes
        # underscore names (code_writer) while _persist_agent_logs writes
        # hyphen names (code-writer) — splitting every agent into two DB
        # buckets. AGENT_PROMPT_MAP maps underscore → hyphen stem.
        from .subagents import AGENT_PROMPT_MAP
        _display_name = AGENT_PROMPT_MAP.get(agent_name, agent_name)
        cb = _ToolCallLogger(int(job_id), _display_name)
        # Circuit-breaker observation: records per-LLM-call success/failure so
        # a stalling model trips the breaker (llm_breaker) and traffic routes
        # to ZAI_FALLBACK_MODEL. Attached here so every agent's LLM calls feed it.
        from .llm_breaker import CircuitBreakerCallback

        cb_breaker = CircuitBreakerCallback()
        existing = agent_cfg.get("callbacks")
        # config["callbacks"] can be: None, a list of handlers, OR a
        # BaseCallbackManager (langgraph passes one; it's NOT iterable — calling
        # list() on it raises TypeError). Normalise to a flat handler list so
        # langgraph's run-tracking handlers are preserved alongside ours.
        if existing is None:
            agent_cfg["callbacks"] = [cb, cb_breaker]
        elif isinstance(existing, list):
            agent_cfg["callbacks"] = [*existing, cb, cb_breaker]
        elif isinstance(existing, BaseCallbackManager):
            agent_cfg["callbacks"] = [*existing.handlers, cb, cb_breaker]
        else:
            agent_cfg["callbacks"] = [existing, cb, cb_breaker]
    return agent_cfg


# ═══════════════════════════════════════════════════════════════════════════
# Agent wrapper nodes — bridge between deterministic graph and react agents
# ═══════════════════════════════════════════════════════════════════════════

PHASE_MAP: dict[str, str] = {
    "site_analyzer": "site_analysis",
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # "navigation_explore": "navigation_explore",
    # "navigation_agent": "navigation_agent",
    # "navigation_synthesize": "navigation_synthesize",
    # ═══ END ARCHIVED ═══
    "browser_traverse": "Browser Navigation",
    "nav_skill_review": "navigation_skill_review",
    "product_analyzer": "product_analysis",
    "scraper_analyzer": "scraper_analysis",
    "code_writer": "code_generation",
    "code_tester": "testing",
    "cleanup": "cleanup",
    "skill_learner": "skill_learning",
    "dagster_converter": "dagster_converter",
    "store_job_listings": "store_job_listings",
}


import threading


class _HeartbeatHandle:
    """Per-invocation heartbeat state.

    Holds a stop flag + the live timers so ``_stop_heartbeat`` can cancel
    EVERY rescheduled timer (not just the initial one) AND signal ``_beat`` to
    stop rescheduling. Per-invocation (not a module-global list) so the
    concurrency=2 worker's two in-flight jobs don't clobber each other's timers.
    """

    __slots__ = ("stop", "timers", "beats")

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.timers: list = []
        self.beats = 0


# M4: hard cap on the self-rescheduling chain. Even with the F5 try/finally
# at every call site, a future copy-pasted site (the pattern has already been
# pasted five times) or a worker-level kill could leave the chain immortal —
# job 333's leaked timer wrote DB rows every 5 minutes for days. 60 beats at
# the default 300s interval ≈ 5h; at the execution interval (240s) ≈ 4h.
_HEARTBEAT_MAX_BEATS = 60


def _start_heartbeat(
    job_id: int, agent_name: str, interval: int = 300,
    prefix: str = "[HEARTBEAT]",
) -> _HeartbeatHandle:
    """Start a background heartbeat that writes a SessionLog entry every
    ``interval`` seconds during long agent executions.

    The watchdog kills jobs with no SessionLog activity for 15+ minutes.
    LLM agents (code_writer, site_analyzer, etc.) are blocking calls that
    can run 15+ minutes without producing SessionLog entries. This heartbeat
    keeps the watchdog informed.

    ``prefix`` selects the watchdog treatment: ``[HEARTBEAT]`` rows are
    EXCLUDED from the stuck-job activity check (a leaked timer chain must
    not mask a dead agent — see cleanup_stuck_jobs), while run_execution
    passes ``[EXEC-ALIVE]`` so its rows COUNT: execution liveness is
    independently bounded (EXECUTION_STALL_TIMEOUT/EXECUTION_TIMEOUT and
    the /scrape timeouts), so an [EXEC-ALIVE] row can only rescue a
    genuinely-live 30+ min scrape — never mask a hang. Without this, a
    healthy long scrape whose only signal is heartbeats gets SIGKILLed
    as "crashed" the moment the watchdog is revived.

    Returns a ``_HeartbeatHandle`` that must be passed to ``_stop_heartbeat``
    when the agent finishes. The handle's stop flag is what actually ends the
    chain — ``_beat`` checks it before rescheduling, so cancellation is reliable
    even if a beat fires mid-cancel.

    M4 belt-and-braces: the chain self-terminates after _HEARTBEAT_MAX_BEATS
    beats OR when the job reaches a terminal status — so no leak path can
    run forever even if _stop_heartbeat is never called.
    """
    handle = _HeartbeatHandle()

    def _beat() -> None:
        if handle.stop.is_set():
            return
        # M4: self-cap — an immortal chain is worse than a missing heartbeat.
        handle.beats += 1
        if handle.beats > _HEARTBEAT_MAX_BEATS:
            logger.warning(
                "heartbeat for job %s (%s) exceeded %d beats — self-terminating",
                job_id, agent_name, _HEARTBEAT_MAX_BEATS,
            )
            return
        _job_terminal = False
        try:
            from scraper.models import ScrapeJob, SessionLog

            _job_terminal = job_id in () or ScrapeJob.objects.filter(
                pk=job_id, status__in=(
                    ScrapeJob.STATUS_COMPLETED, ScrapeJob.STATUS_FAILED,
                    ScrapeJob.STATUS_CANCELLED, ScrapeJob.STATUS_CAPTCHA_BLOCKED,
                    ScrapeJob.STATUS_AKAMAI_BLOCKED,
                ),
            ).exists()
            if not _job_terminal:
                seq = SessionLog.objects.filter(job_id=job_id).count()
                SessionLog.objects.create(
                    job_id=job_id,
                    role=SessionLog.ROLE_SYSTEM,
                    agent=agent_name,
                    content=f"{prefix} Agent {agent_name} still running...",
                    seq=seq,
                )
        except Exception:
            pass
        if handle.stop.is_set() or _job_terminal:
            return
        # Re-check before rescheduling — _stop_heartbeat may have fired while
        # the SessionLog write was in flight.
        if handle.stop.is_set():
            return
        timer = threading.Timer(interval, _beat)
        timer.daemon = True
        handle.timers.append(timer)
        timer.start()

    timer = threading.Timer(interval, _beat)
    timer.daemon = True
    handle.timers.append(timer)
    timer.start()
    return handle


def _stop_heartbeat(handle: _HeartbeatHandle | None) -> None:
    """Stop the heartbeat: set the stop flag (any in-flight ``_beat`` won't
    reschedule) and cancel every live timer.

    The old version cancelled only the INITIAL timer and then ``clear()``-ed a
    shared module-global list — which (a) left the self-rescheduled timers
    firing forever (an immortal chain that masked agent hangs from the watchdog
    AND was ~30% of all SessionLog writes), and (b) under concurrency=2 let one
    job's stop clear another job's timers. The per-handle stop flag fixes both.
    """
    if handle is None:
        return
    handle.stop.set()
    for t in handle.timers:
        try:
            t.cancel()
        except Exception:
            pass
    handle.timers.clear()


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


def _budget_setting(name: str, default: int) -> int:
    """Budget/timeout constant, env-overridable via Django settings.

    Lets the Phase-1 gate tune per-agent budgets from measured per-step latency
    WITHOUT a code change. The structural mismatch to fix: PRODUCT_ANALYSIS_BUDGET
    (recursion steps) × ~per-step latency can exceed _AGENT_INVOKE_TIMEOUT (the
    wall-clock cap), so the wall-clock abandons mid-budget → empty result →
    budget-escalation cascade. Tuning lowers the budget to fit, NOT raises the
    wall-clock (which would widen the leaked-thread window).
    """
    try:
        from django.conf import settings

        return int(getattr(settings, name, default))
    except Exception:
        return default


SITE_ANALYSIS_BUDGET = _budget_setting("SITE_ANALYSIS_BUDGET", 10)
SITE_ANALYSIS_BUDGET_EXTENDED = _budget_setting("SITE_ANALYSIS_BUDGET_EXTENDED", 20)
SITE_ANALYSIS_MAX_BUDGET = _budget_setting("SITE_ANALYSIS_MAX_BUDGET", 50)
PRODUCT_ANALYSIS_BUDGET = _budget_setting("PRODUCT_ANALYSIS_BUDGET", 50)
PRODUCT_ANALYSIS_BUDGET_EXTENDED = _budget_setting("PRODUCT_ANALYSIS_BUDGET_EXTENDED", 70)
PRODUCT_ANALYSIS_MAX_BUDGET = _budget_setting("PRODUCT_ANALYSIS_MAX_BUDGET", 70)
MAX_OUTER_RETRIES = _budget_setting("MAX_OUTER_RETRIES", 2)

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


def _archive_existing_scraper(slug: str) -> str | None:
    """Archive the current scraper.py before cleanup overwrites it.

    Returns the archive KEY (or None if there was nothing to archive) so the
    caller can restore the prior good scraper on a failed run. Artifacts live in
    the File Master (cross-service); keys are logical ``scrapers/{slug}/...``.
    """
    if not slug:
        return None
    try:
        import src.artifacts as artifacts
        from datetime import datetime, timezone as dt_timezone

        prod_key = artifacts.scrapers_key(slug, "scraper.py")
        if not artifacts.exists(prod_key):
            return None
        ts = datetime.now(dt_timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        archive_name = f"scraper-{slug}-{ts}.py"
        archive_key = artifacts.scrapers_key(slug, archive_name)
        artifacts.write(archive_key, artifacts.read(prod_key))
        logger.info("_archive_existing_scraper: archived → %s", archive_name)
        return archive_key
    except Exception as exc:
        logger.warning("_archive_existing_scraper: failed: %s", exc)
        return None


def _promote_scraper(
    slug: str, job_id: int, execution_status: str, archive_key: str | None
) -> str | None:
    """Deterministic, failure-safe scraper finalization (replaces the LLM cp).

    1. Always copy this job's LOCAL ``workspace/{slug}/scraper_draft.py`` to a
       per-job File Master key ``scrapers/{slug}/jobs/scraper-{job_id}.py``
       (attributed, survives later jobs).
    2. Promote to production ``scrapers/{slug}/scraper.py`` ONLY on SUCCESS. On
       non-success, leave production untouched and restore the prior good scraper
       from the archive key (defends against any stray clobber).

    Returns the per-job scraper KEY (or None if no draft was produced). The draft
    read is local (workspace stays on the worker); all scrapers/ writes go to FM.
    """
    if not slug:
        return None
    try:
        import src.artifacts as artifacts

        root = _get_project_root()
        draft = os.path.join(root, "workspace", slug, "scraper_draft.py")  # LOCAL
        per_job_key = artifacts.scrapers_key(slug, "jobs", f"scraper-{job_id}.py")
        prod_key = artifacts.scrapers_key(slug, "scraper.py")

        promoted = None
        if os.path.isfile(draft):
            with open(draft, "rb") as _f:
                _draft_bytes = _f.read()
            artifacts.write(per_job_key, _draft_bytes)
            promoted = per_job_key
            logger.info("_promote_scraper: per-job copy → jobs/scraper-%s.py", job_id)

        if execution_status == "SUCCESS":
            if promoted:
                artifacts.write(prod_key, artifacts.read(per_job_key))
                logger.info(
                    "_promote_scraper: SUCCESS → promoted to scraper.py (job %s)", job_id
                )
        else:
            # Non-success: do NOT promote the (possibly broken) draft. Restore
            # the prior good production scraper if anything clobbered it.
            if archive_key and artifacts.exists(archive_key) and artifacts.exists(prod_key):
                artifacts.write(prod_key, artifacts.read(archive_key))
                logger.info(
                    "_promote_scraper: non-SUCCESS → restored scraper.py from archive (job %s)",
                    job_id,
                )
            logger.info(
                "_promote_scraper: non-SUCCESS (execution_status=%s) → production "
                "scraper.py left as-is (job %s)",
                execution_status, job_id,
            )
        return promoted
    except Exception as exc:
        logger.warning("_promote_scraper: failed: %s", exc)
        return None


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


def _accessibility_goto(state: ScrapeState, update: dict[str, Any] | None = None) -> Command:
    """Pick the next node after check_accessibility based on input_mode.

    For navigation/list_page/search_term jobs, route directly to browser_traverse
    and set ``skip_site_analysis``. site_analyzer's output is never consumed by
    the browser path (browser_traverse reads url/page_type/search_criteria only),
    so running it wastes ~5 minutes. url_list jobs still go through site_analyzer,
    where site_analysis informs product/content analysis.
    """
    input_mode = state.get("input_mode", "url_list")
    if input_mode in ("navigation", "list_page", "search_term"):
        logger.info(
            "check_accessibility: input_mode=%s → browser_traverse "
            "(skipping site_analyzer)",
            input_mode,
        )
        upd: dict[str, Any] = dict(update or {})
        upd["skip_site_analysis"] = True
        return Command(update=upd, goto="browser_traverse")
    if update:
        return Command(update=update, goto="site_analyzer")
    return Command(goto="site_analyzer")


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
        return _accessibility_goto(state)

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

    return _accessibility_goto(state, probe_state)


_AGENT_INVOKE_TIMEOUT = 900  # seconds — hard wall-clock cap per agent.invoke().
                            # glm-5-turbo needs ~700-900s for code_writer (read
                            # template + generate ~500 lines + self-test + fix).


def _async_execution_enabled() -> bool:
    """Kill-switch for Phase 4 async cancellation (Per-Phase Execution Contract).

    True → ``_invoke_agent_with_timeout`` runs ``agent.ainvoke`` under
    ``asyncio.wait_for``: on timeout, ``CancelledError`` propagates into the
    react loop and the async httpx client CLOSES the in-flight z.ai socket —
    the work actually stops (vs the sync path's daemon-thread abandon-then-leak
    that held the socket + ~180K-char context until the Celery time_limit
    SIGKILLed the worker). This is the contract's real cancellation.

    Default False: the async path is new; keep sync as the safe default until a
    regression (lw.com/locumtenens/aya) verifies it. Phase 2 already removed the
    sync ``headroom.compress`` call from the cancellation path (the P0
    precondition), so a timeout during the LLM call cancels cleanly; a timeout
    during a sync tool (run_in_executor) abandons that tool's executor thread
    (bounded by the per-tool guards; same shape as today's thread abandon).
    """
    try:
        from django.conf import settings

        return bool(getattr(settings, "LLM_ASYNC_EXECUTION", False))
    except Exception:
        return False


def _invoke_agent_async(agent, messages, agent_cfg, phase, job_id, timeout):
    """Run ``agent.ainvoke`` under ``asyncio.wait_for`` in a fresh event loop.

    The graph runs synchronously today (graph.invoke from the Celery task), so
    there is no running event loop when this node executes — ``asyncio.run`` is
    safe. On timeout the ``wait_for`` cancels the ainvoke await; for an in-flight
    LLM call the CancelledError reaches the async httpx client which closes the
    z.ai socket (verified cancellable). Returns ``{"messages": []}`` on timeout
    (callers treat as budget-exhausted), ``{"_error": ...}`` on other errors —
    matching the sync path's contract.
    """
    import asyncio

    async def _run():
        return await asyncio.wait_for(
            agent.ainvoke({"messages": messages}, agent_cfg), timeout=timeout
        )

    try:
        return asyncio.run(_run())
    except asyncio.TimeoutError:
        logger.error(
            "_invoke_agent_with_timeout[%s]: ainvoke exceeded %ds wall-clock "
            "— cancelled (socket closed), returning empty (job %s)",
            phase, timeout, job_id,
        )
        return {"messages": []}
    except Exception as exc:
        return {"_error": str(exc)[:200]}


def _invoke_agent_with_timeout(agent, messages, agent_cfg, phase: str, job_id, timeout: int = _AGENT_INVOKE_TIMEOUT):
    """Run the agent with a wall-clock timeout.

    Two modes (kill-switch ``LLM_ASYNC_EXECUTION``, see
    ``_async_execution_enabled``):

    - **async** (default off): ``agent.ainvoke`` under ``asyncio.wait_for`` — on
      timeout the in-flight z.ai call is genuinely CANCELLED (httpx closes the
      socket), no abandoned thread leaks its context.
    - **sync** (default): raw daemon thread + ``thread.join``; on timeout the
      thread is abandoned (leaks until the Celery ``time_limit`` reclaims the
      worker). Pre-Phase-4 behavior.

    Both return ``{"messages": []}`` on timeout (callers treat as
    budget-exhausted).
    """
    if _async_execution_enabled():
        return _invoke_agent_async(agent, messages, agent_cfg, phase, job_id, timeout)

    import threading

    result_box = [None]
    def _run():
        try:
            result_box[0] = agent.invoke({"messages": messages}, agent_cfg)
        except Exception as exc:
            result_box[0] = {"_error": str(exc)[:200]}

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        logger.error(
            "_invoke_agent_with_timeout[%s]: agent.invoke exceeded %ds wall-clock "
            "— abandoning thread, returning empty (job %s)",
            phase, timeout, job_id,
        )
        return {"messages": []}
    return result_box[0] or {"messages": []}


def _run_budgeted_agent(
    state: ScrapeState,
    config: RunnableConfig,
    *,
    phase: str,
    display_name: str,
    agent_factory,
    message_builder,
    artifact_name: str,
    state_key: str,
    budget: int,
    budget_extended: int,
    budget_max: int,
    budget_exhausted_reason: str,
    budget_exhausted_options: list[str],
    budget_exhausted_message: str,
    missing_artifact_reason: Optional[str] = None,
    missing_retries_state_key: Optional[str] = None,
    missing_redo_label: str = "",
    missing_skip_label: str = "",
    missing_message: str = "",
    auto_extend_min_tool_calls: int = 5,
    artifact_fix_fn=None,
    on_success=None,
) -> dict[str, Any] | Command:
    """Shared control flow for the three budgeted analysis agents
    (site_analyzer, product_analyzer, navigation_agent).

    Encapsulates the previously triplicated pattern:
      budget-retry count math → optional budget-extension prompt → invoke →
      (on missing artifact) auto-extend-if-≥N-tool-calls → re-invoke →
      budget_exhausted_* interrupt → [missing_artifact_* interrupt].

    Per-phase variation is passed via kwargs. The ``on_success`` hook (if given)
    is applied in BOTH the primary and auto-extend success paths, normalising a
    prior asymmetry where the auto-extend path skipped phase-specific
    post-processing (e.g. product's anti-bot strategy enforcement, site's
    probe_result surfacing) — both are idempotent/additive, so applying them
    consistently is strictly safer.

    Resume contract: the budget interrupts here only SET ``interrupt_reason``
    and ``goto="human_approval"``; the actual ``interrupt()`` call site lives in
    the ``human_approval`` node, so LangGraph's interrupt_id is assigned there
    and is unaffected by this centralisation. Each phase's reason string
    (budget_exhausted_site / _product / _navigation, missing_artifact_site /
    _product) is preserved verbatim, so services.py INTERRUPT_TO_APPROVAL_TYPE
    and route_from_human_approval resume routing keep working unchanged.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    is_budget_retry = state.get("interrupt_reason") == budget_exhausted_reason
    is_missing_artifact = (
        missing_artifact_reason is not None
        and state.get("interrupt_reason") == missing_artifact_reason
    )
    # Reset on phase entry: only carry the count forward when THIS is a
    # budget-retry re-entry for THIS phase. A stale count from a prior phase
    # (e.g. site exhaustion → product reads count=1) corrupts the downstream
    # budget + skips the escalation interrupt. P0-8.
    _prior = (
        state.get("budget_retry_count", 0)
        if (is_budget_retry or is_missing_artifact)
        else 0
    )
    budget_retries = (
        _prior
        + (1 if is_budget_retry else 0)
        + (1 if is_missing_artifact else 0)
    )
    recursion_limit = budget_extended if budget_retries > 0 else budget
    _notify_phase(job_id, phase, "running")
    set_tool_context(dict(state), agent_name=phase)
    try:
        logger.info(
            "_run_budgeted_agent[%s]: starting (job %s, budget=%d, retry=%d)",
            phase,
            job_id,
            recursion_limit,
            budget_retries,
        )
        messages = message_builder(state)

        if budget_retries > 0:
            previous_summary = state.get("budget_retry_summary", "")
            augmented = (
                "## BUDGET EXTENSION\n"
                f"Previous analysis ran out of the call budget. "
                f"You now have {recursion_limit} calls.\n\n"
                "### CRITICAL INSTRUCTION\n"
                f"You MUST write {artifact_name} before running out of calls. "
                "Write the file as soon as you have enough data — do NOT explore further.\n\n"
                f"### Previous Findings\n"
                f"Use these findings to skip re-discovery. Fill any gaps and write the output file.\n\n"
                f"{previous_summary}\n\n"
                f"---\n\n"
            )
            original_content = messages[0].content
            messages = [HumanMessage(content=augmented + original_content)]

        _log_agent_context(state, display_name, messages)
        agent = agent_factory(site_slug=slug)
        agent_cfg = _agent_config(config, phase)
        hb = _start_heartbeat(job_id, display_name)
        # F5: any raise between start and stop (DB outage, agent factory
        # failure) previously leaked the self-rescheduling timer chain.
        try:
            result = _invoke_agent_with_timeout(agent, messages, agent_cfg, phase, job_id)
        finally:
            _stop_heartbeat(hb)
        _persist_agent_logs(state, result, display_name, config)

        if artifact_fix_fn is not None:
            artifact_fix_fn(slug)

        root = _get_project_root()
        output_exists = os.path.isfile(
            os.path.join(root, "workspace", slug, artifact_name)
        )

        if output_exists:
            _notify_phase(job_id, phase, "done")
            analysis = _read_json_artifact(root, slug, artifact_name)
            if on_success is not None:
                _ret = on_success(analysis, state)
                if _ret is not None:
                    return _ret
            return {"messages": [], state_key: analysis}

        tool_call_count = sum(
            1
            for m in (result.get("messages") or [])
            if m.__class__.__name__ == "ToolMessage"
        )
        summary = _extract_previous_findings(result)

        # Auto-extend: if the agent ran out of calls BUT made real progress
        # (≥ N tool calls), give it +10 calls once and re-invoke with a
        # "write the file NOW" instruction. This catches the common case where
        # the agent was one step away from writing the artifact.
        if recursion_limit < budget_max and tool_call_count >= auto_extend_min_tool_calls:
            extended_limit = min(recursion_limit + 10, budget_max)
            logger.info(
                "_run_budgeted_agent[%s]: auto-extending budget %d -> %d for job %s (made %d tool calls)",
                phase,
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
                f"You MUST write {artifact_name} NOW. You have all the data you need. "
                "Do NOT explore further — write the output file immediately.\n\n"
                f"### Previous Findings\n{summary}\n\n---\n\n"
            )
            original_content = message_builder(state)[0].content
            retry_messages = [HumanMessage(content=augmented + original_content)]
            agent_cfg2 = _agent_config(config, phase)
            result = _invoke_agent_with_timeout(agent, retry_messages, agent_cfg2, phase, job_id)
            _persist_agent_logs(state, result, display_name, config)

            if artifact_fix_fn is not None:
                artifact_fix_fn(slug)

            output_exists = os.path.isfile(
                os.path.join(root, "workspace", slug, artifact_name)
            )
            if output_exists:
                _notify_phase(job_id, phase, "done")
                analysis = _read_json_artifact(root, slug, artifact_name)
                if on_success is not None:
                    _ret = on_success(analysis, state)
                    if _ret is not None:
                        return _ret
                return {"messages": [], state_key: analysis}
            summary = _extract_previous_findings(result)

        # No artifact after invoke (+ optional auto-extend). First time around,
        # surface a budget-escalation interrupt; on retry, fall through to the
        # missing-artifact gate (or proceed) below.
        if budget_retries < 1:
            logger.warning(
                "_run_budgeted_agent[%s]: %s missing after run (job %s). "
                "Routing to human_approval for budget escalation.",
                phase,
                artifact_name,
                job_id,
            )
            return Command(
                update={
                    "messages": [],
                    "interrupt_reason": budget_exhausted_reason,
                    "interrupt_message": budget_exhausted_message,
                    "interrupt_options": budget_exhausted_options,
                    "interrupt_decisions": options_to_decisions(budget_exhausted_options),
                    "budget_retry_count": budget_retries,
                    "budget_retry_summary": summary,
                },
                goto="human_approval",
            )

        if missing_artifact_reason is not None:
            retries_now = int(state.get(missing_retries_state_key, 0)) + 1
            if retries_now < MAX_OUTER_RETRIES:
                logger.warning(
                    "_run_budgeted_agent[%s]: still no output (job %s, retries=%d). Offering redo.",
                    phase,
                    job_id,
                    retries_now,
                )
                options = [
                    missing_redo_label,
                    missing_skip_label,
                    "Cancel entire job",
                ]
                return Command(
                    update={
                        "messages": [],
                        "interrupt_reason": missing_artifact_reason,
                        "interrupt_message": missing_message.format(summary=summary[:500]),
                        "interrupt_options": options,
                        "interrupt_decisions": options_to_decisions(options),
                        "budget_retry_count": budget_retries,
                        "budget_retry_summary": summary,
                        missing_retries_state_key: retries_now,
                    },
                    goto="human_approval",
                )

            logger.warning(
                "_run_budgeted_agent[%s]: still no output after %d retries (job %s). Proceeding.",
                phase,
                retries_now,
                job_id,
            )
            return {
                "messages": [],
                missing_retries_state_key: retries_now,
            }

        # Navigation has no missing-artifact gate (Wave 1 removed
        # missing_artifact_navigation) — proceed with whatever we have.
        logger.warning(
            "_run_budgeted_agent[%s]: still no output after retries (job %s). Proceeding.",
            phase,
            job_id,
        )
        return {"messages": []}
    except Exception:
        _notify_phase(job_id, phase, "failed")
        raise
    finally:
        clear_tool_context()


NAVIGATION_ANALYSIS_BUDGET = 40
NAVIGATION_ANALYSIS_BUDGET_EXTENDED = 60
NAVIGATION_ANALYSIS_MAX_BUDGET = 60


def _invoke_site_analyzer(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    def _on_success(analysis: dict, st: ScrapeState):
        update: dict[str, Any] = {"messages": [], "site_analysis": analysis}
        connectivity = analysis.get("connectivity", {})
        if connectivity:
            product_url = st.get("product_url") or ""
            update["probe_result"] = {
                "url": product_url,
                "connectivity": connectivity,
                "platform": analysis.get("platform", ""),
                "anti_bot_detected": analysis.get("anti_bot_detected", False),
            }
            update["probe_url"] = product_url
        return update

    return _run_budgeted_agent(
        state,
        config,
        phase="site_analyzer",
        display_name="site-analyzer",
        agent_factory=create_site_analyzer,
        message_builder=build_site_analyzer_message,
        artifact_name="site_analysis.json",
        state_key="site_analysis",
        budget=SITE_ANALYSIS_BUDGET,
        budget_extended=SITE_ANALYSIS_BUDGET_EXTENDED,
        budget_max=SITE_ANALYSIS_MAX_BUDGET,
        budget_exhausted_reason="budget_exhausted_site",
        budget_exhausted_options=[
            "Retry with higher budget (50 calls)",
            "Continue anyway",
            "Cancel",
        ],
        budget_exhausted_message=(
            f"Site analysis did not complete — the agent used its call budget "
            f"({SITE_ANALYSIS_BUDGET} calls) without writing site_analysis.json. "
            f"This site may be complex. Choose how to proceed."
        ),
        missing_artifact_reason="missing_artifact_site",
        missing_retries_state_key="site_analysis_retries",
        missing_redo_label="Redo site analysis",
        missing_skip_label="Continue without site analysis",
        missing_message=(
            "Site analysis could not produce site_analysis.json after extended attempts. "
            "The agent explored the site but didn't write the output file.\n\n"
            "Previous findings summary:\n{summary}\n\n"
            "Choose how to proceed."
        ),
        auto_extend_min_tool_calls=5,
        on_success=_on_success,
    )


def _invoke_product_analyzer(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
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
            state.get("job_id", 0),
            _pa_remediation.get("fields"),
        )

    def _on_success(analysis: dict, st: ScrapeState):
        # Anti-bot ⇒ playwright (cloak). KEPT: code_writer otherwise picks the
        # discovered API (which Akamai also guards → 400). Gated by _PATCHES_ENABLED.
        # Applied in both the primary and auto-extend success paths (normalised
        # by _run_budgeted_agent): previously the auto-extend path skipped this,
        # leaking an un-enforced strategy when the second invoke produced output.
        if _PATCHES_ENABLED:
            _enforce_anti_bot_strategy(
                analysis, st.get("site_slug", ""), "product_analysis.json"
            )
        if is_remap:
            remap_count = int(st.get("remap_count", 0) or 0) + 1
            logger.info(
                "_invoke_product_analyzer: re-mapped failed fields → code_writer "
                "(remap %d, job %s)",
                remap_count,
                st.get("job_id", 0),
            )
            return Command(
                goto="code_writer",
                update={
                    "messages": [],
                    "product_analysis": analysis,
                    "remap_count": remap_count,
                },
            )
        return {"messages": [], "product_analysis": analysis}

    return _run_budgeted_agent(
        state,
        config,
        phase="product_analyzer",
        display_name="product-analyzer",
        agent_factory=create_product_analyzer,
        message_builder=build_product_analyzer_message,
        artifact_name="product_analysis.json",
        state_key="product_analysis",
        budget=PRODUCT_ANALYSIS_BUDGET,
        budget_extended=PRODUCT_ANALYSIS_BUDGET_EXTENDED,
        budget_max=PRODUCT_ANALYSIS_MAX_BUDGET,
        budget_exhausted_reason="budget_exhausted_product",
        budget_exhausted_options=[
            "Retry with higher budget (70 calls)",
            "Continue anyway",
            "Cancel",
        ],
        budget_exhausted_message=(
            f"Product analysis did not complete — the agent used its call budget "
            f"({PRODUCT_ANALYSIS_BUDGET} calls) without writing product_analysis.json. "
            f"This product page may be complex. Choose how to proceed."
        ),
        missing_artifact_reason="missing_artifact_product",
        missing_retries_state_key="product_analysis_retries",
        missing_redo_label="Redo product analysis",
        missing_skip_label="Continue without product analysis",
        missing_message=(
            "Product analysis could not produce product_analysis.json after extended attempts. "
            "The agent explored the page but didn't write the output file.\n\n"
            "Previous findings summary:\n{summary}\n\n"
            "Choose how to proceed."
        ),
        auto_extend_min_tool_calls=5,
        artifact_fix_fn=lambda slug: _fix_json_artifact(slug, "product_analysis.json"),
        on_success=_on_success,
    )


def _invoke_navigation_traverse(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    """Graph wrapper for the browser-driven navigation traversal node.

    Replaces the 3-node navigation_explore → navigation_agent → navigation_synthesize
    pipeline with a single browser_traverse call (LLM-driven MCP browser walk from
    the homepage to a listing page). The TraversalResult is converted to the same
    navigation_analysis dict shape that downstream nodes (product_analyzer, etc.)
    already consume. Falls back to the archived navigate_explore + navigate_synthesize
    when the MCP browser is unavailable.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")
    url = state.get("url", "")
    content_type = state.get("page_type", "product")
    query = state.get("search_criteria", "") or ""

    _notify_phase(job_id, "browser_traverse", "running")
    try:
        from experimental.nav_traversal.traversal import browser_traverse, traverse

        _input_mode = state.get("input_mode") or ""
        result = browser_traverse(
            url, content_type, query,
            trust_start_as_listing=_input_mode in ("list_page", "search_term"),
        )

        # MCP unavailable → fall back to the archived deterministic explorer +
        # synthesizer (imported lazily here so the fallback path is self-contained).
        if "MCP" in (result.notes or ""):
            logger.info(
                "browser_traverse: MCP unavailable (%s) — falling back to "
                "navigate_explore + navigate_synthesize (job %s)",
                result.notes, job_id,
            )
            from .nodes.navigate_explore import navigate_explore
            from .nodes.navigate_synthesize import navigate_synthesize

            explore_result = navigate_explore(dict(state), config)
            if isinstance(explore_result, dict):
                state.update(explore_result)
            synth_result = navigate_synthesize(dict(state), config)
            _notify_phase(job_id, "browser_traverse", "done")
            return synth_result if isinstance(synth_result, dict) else {"messages": []}

        # browser_traverse didn't reach the goal (but MCP was available) —
        # fall back to the HTTP-first traverse() which handles form-driven
        # sites (locumtenens QuickSearch) and API discovery (aya) that the
        # LLM-driven browser approach couldn't complete within its budget.
        if not result.reached:
            logger.info(
                "browser_traverse: didn't reach goal (%s) — falling back to "
                "HTTP traverse() (job %s)",
                result.notes, job_id,
            )
            fb_result = traverse(url, content_type, query)
            if fb_result.reached:
                result = fb_result  # use the fallback's result
                logger.info(
                    "HTTP traverse fallback reached goal: %s (job %s)",
                    result.goal_url, job_id,
                )
            else:
                logger.info(
                    "HTTP traverse also didn't reach goal — using best partial (job %s)",
                    job_id,
                )

        # Extract item URL examples so product_analyzer + code_tester have
        # ready-made sample URLs (avoids ~6.5 min of auto-discovery). Prefer the
        # real item hrefs browser_traverse captured from the RENDERED goal page
        # (correct for CSR/JS-rendered listings — Coveo/React/Vue). Fall back to a
        # plain HTTP fetch + link parse ONLY when no browser item links exist
        # (SSR pages where the raw HTML already contains the item anchors).
        url_examples: list[str] = list(getattr(result, "item_links", []) or [])[:20]
        if not url_examples and result.goal_url:
            try:
                from experimental.nav_traversal.traversal import _default_fetch, extract_links

                page_resp = _default_fetch(result.goal_url)
                if page_resp.get("ok"):
                    links = extract_links(page_resp.get("text", ""), result.goal_url)
                    url_examples = [l["href"] for l in links[:20] if l.get("href")]
            except Exception as exc:
                logger.info(
                    "browser_traverse: url_examples extraction failed (%s)", exc
                )
            if url_examples:
                logger.info(
                    "browser_traverse: extracted %d url_examples from goal page (job %s)",
                    len(url_examples), job_id,
                )
        elif url_examples:
            logger.info(
                "browser_traverse: using %d browser-captured item links as url_examples (job %s)",
                len(url_examples), job_id,
            )

        # ── Listing-reachability fallback (uindex class) ────────────────────
        # browser_traverse can exhaust its budget without judging any page a
        # listing (Cloudflare wall, JS gate, slow render) → discovery comes back
        # {listing_url: null, listing_reached: false}. That cascades to no
        # discovery_config, a detail-page sample, and 0 items — even when the
        # site ROOT is a perfectly good listing (uindex homepage: 120 torrents,
        # HTTP 200). When the navigator didn't reach a listing but the site root
        # is already proven reachable (probe_result.connectivity.method_that_worked,
        # populated upstream by check_accessibility — ZERO new network calls),
        # fall back to the root as the listing. Fixes the value at the source so
        # every downstream consumer (run_execution, _derive_strategy, code_writer)
        # reads the corrected URL. url_list mode is exempt — it has no listing.
        _disc_fb = dict(getattr(result, "discovery", {}) or {})
        _input_mode = (state.get("input_mode") or "").strip()
        _probe = state.get("probe_result") or {}
        _conn = _probe.get("connectivity") if isinstance(_probe, dict) else None
        _root_method = ""
        if isinstance(_conn, dict):
            _root_method = str(_conn.get("method_that_worked") or "").strip()
        # Site ORIGIN (scheme+host), NOT state.url verbatim — the input URL can be
        # a detail/sample page (uindex job 184 submitted a details.php?id=... URL).
        # Falling back to that would point discovery at a single detail page.
        _site_root = ""
        try:
            from urllib.parse import urlparse as _urlparse
            _p = _urlparse((state.get("url") or "").strip())
            if _p.scheme in ("http", "https") and _p.netloc:
                _site_root = f"{_p.scheme}://{_p.netloc}/"
        except Exception:
            _site_root = ""
        if (
            _input_mode in ("navigation", "list_page", "search_term")
            and _site_root
            and _root_method
            and not _disc_fb.get("listing_reached")
        ):
            _disc_fb = {
                "listing_url": _site_root,
                "listing_reached": True,
                "pagination": _disc_fb.get("pagination") or {"type": "load_more"},
            }
            logger.warning(
                "browser_traverse: listing not reached — falling back to reachable site origin %s (via %s, job %s)",
                _site_root, _root_method, job_id,
            )

        analysis = {
            "discovery_method": "browser_traverse" if result.reached else "fallback",
            "search": {
                "working_url": result.goal_url,
                "has_search": True,
                # propagate form-POST replay info so code_writer can replay the search
                "form_method": result.goal_method if hasattr(result, "goal_method") else "GET",
                "form_data": dict(result.goal_data) if hasattr(result, "goal_data") and result.goal_data else {},
                "form_action": result.goal_request_url if hasattr(result, "goal_request_url") else "",
            },
            "item_links": {
                "url_examples": url_examples,
                # propagate signals so code_writer has SOMETHING to work with
                "signals": result.signals if hasattr(result, "signals") else {},
            },
            "data_source": getattr(result, "mechanism", "") or "unknown",
            # Bug fix: use "url" key (what subagents.py:2208 checks), not "api_url".
            # Preserve count/items_per_page so _derive_strategy can gate the
            # internal_api override on the API having DEMONSTRABLY returned records
            # (items_per_page>0) — a bare URL with 0 results (Coveo /coveo/rest/search
            # returns totalCount=0 without the browser's filter) must NOT trigger
            # internal_api, or the job diverts from playwright to a doomed strategy.
            "api_endpoint": (
                {"url": result.api["url"],
                 "count": result.api.get("count"),
                 "items_per_page": result.api.get("items_per_page")}
                if result.api and isinstance(result.api, dict) and result.api.get("url")
                else (result.api or {})
            ),
            "rendering_verified": "browser",
            # propagate the full path so downstream can see how we got here
            "traversal_path": result.path[:8] if hasattr(result, "path") else [],
            # Phase 1 (JS-listing+pagination class fix): carry the discovery
            # contract (listing_reached, listing_url, pagination type) from the
            # navigator through state to run_execution + code_writer. The
            # navigator ALREADY detects these; the graph must not drop them.
            "discovery": _disc_fb,
            "pagination": _disc_fb.get("pagination") or {},
        }

        # Persist to workspace/{slug}/navigation_analysis.json
        root = _get_project_root()
        na_path = os.path.join(root, "workspace", slug, "navigation_analysis.json")
        try:
            os.makedirs(os.path.dirname(na_path), exist_ok=True)
            with open(na_path, "w", encoding="utf-8") as f:
                json.dump(analysis, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning(
                "browser_traverse: failed to write navigation_analysis.json: %s", exc
            )

        _notify_phase(job_id, "browser_traverse", "done")
        return {"navigation_analysis": analysis, "messages": []}
    except Exception as exc:
        logger.exception("_invoke_navigation_traverse failed (job %s): %s", job_id, exc)
        _notify_phase(job_id, "browser_traverse", "failed")
        return {"messages": []}


# ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
# ARCHIVED @_with_api_retry
# ARCHIVED def _invoke_navigation_agent(
# ARCHIVED     state: ScrapeState, config: RunnableConfig
# ARCHIVED ) -> dict[str, Any] | Command:
# ARCHIVED     return _run_budgeted_agent(
# ARCHIVED         state,
# ARCHIVED         config,
# ARCHIVED         phase="navigation_agent",
# ARCHIVED         display_name="navigation-agent",
# ARCHIVED         agent_factory=create_navigation_agent,
# ARCHIVED         message_builder=build_navigation_agent_message,
# ARCHIVED         artifact_name="navigation_analysis.json",
# ARCHIVED         state_key="navigation_analysis",
# ARCHIVED         budget=NAVIGATION_ANALYSIS_BUDGET,
# ARCHIVED         budget_extended=NAVIGATION_ANALYSIS_BUDGET_EXTENDED,
# ARCHIVED         budget_max=NAVIGATION_ANALYSIS_MAX_BUDGET,
# ARCHIVED         budget_exhausted_reason="budget_exhausted_navigation",
# ARCHIVED         budget_exhausted_options=[
# ARCHIVED             "Retry with higher budget",
# ARCHIVED             "Continue anyway",
# ARCHIVED             "Cancel",
# ARCHIVED         ],
# ARCHIVED         budget_exhausted_message=(
# ARCHIVED             f"Navigation analysis did not complete — the agent used its call budget "
# ARCHIVED             f"({NAVIGATION_ANALYSIS_BUDGET} calls) without writing navigation_analysis.json. "
# ARCHIVED             f"This site may have complex navigation. Choose how to proceed."
# ARCHIVED         ),
# ARCHIVED         missing_artifact_reason=None,
# ARCHIVED         auto_extend_min_tool_calls=3,
# ARCHIVED     )
# ARCHIVED
# ARCHIVED
# ARCHIVED def _explore_findings_solid(findings: dict) -> bool:
# ARCHIVED     """True when the deterministic explorer already found a real listing + data.
# ARCHIVED
# ARCHIVED     Used to decide whether to SKIP the heavy navigation_agent. "Solid" = a
# ARCHIVED     working listing URL AND a real data signal: embedded-JSON blob, a captured
# ARCHIVED     backend API endpoint, or >=10 real detail links. (Classic-search forms are
# ARCHIVED     handled separately by ``_explore_has_classic_form`` — they still need the
# ARCHIVED     agent to drive the form even when findings look solid.)
# ARCHIVED     """
# ARCHIVED     if not isinstance(findings, dict):
# ARCHIVED         return False
# ARCHIVED     lp = findings.get("listing_page") or {}
# ARCHIVED     working_url = bool((lp.get("url") or "").strip())
# ARCHIVED     ds = lp.get("data_source")
# ARCHIVED     real_links = len(lp.get("product_links") or [])
# ARCHIVED     has_api = bool(findings.get("api_endpoints") or lp.get("api_endpoints"))
# ARCHIVED     solid_data = (
# ARCHIVED         ds == "embedded_json"
# ARCHIVED         or has_api
# ARCHIVED         or (ds == "detail_links" and real_links >= 10)
# ARCHIVED     )
# ARCHIVED     return working_url and bool(solid_data)
# ARCHIVED
# ARCHIVED
# ARCHIVED def _explore_has_classic_form(findings: dict) -> bool:
# ARCHIVED     """True when explore detected a classic multi-select POST search form.
# ARCHIVED
# ARCHIVED     Those forms (e.g. locumtenens QuickSearch) need the agent's browser tools to
# ARCHIVED     drive, so they trigger a navigation_agent handoff even when explore is solid.
# ARCHIVED     """
# ARCHIVED     if not isinstance(findings, dict):
# ARCHIVED         return False
# ARCHIVED     lp = findings.get("listing_page") or {}
# ARCHIVED     hn = findings.get("homepage_nav") or {}
# ARCHIVED     return bool(
# ARCHIVED         findings.get("classic_search")
# ARCHIVED         or (hn.get("classic_search") if isinstance(hn, dict) else None)
# ARCHIVED         or (lp.get("classic_search") if isinstance(lp, dict) else None)
# ARCHIVED     )
# ARCHIVED
# ARCHIVED
# ARCHIVED def _navigation_handoff_decision(findings: dict, anti_bot: bool) -> tuple[bool, str | None]:
# ARCHIVED     """Decide whether to hand off to the LLM navigation_agent after explore.
# ARCHIVED
# ARCHIVED     Returns ``(handoff, reason)``. ``handoff=False`` means SKIP the agent (explore
# ARCHIVED     succeeded) and let ``navigation_synthesize`` build the analysis from findings.
# ARCHIVED     Rules: never hand off for anti-bot (agent's MCP browser isn't cloak-enabled);
# ARCHIVED     hand off when explore is NOT solid OR a classic POST form was detected.
# ARCHIVED     """
# ARCHIVED     if anti_bot:
# ARCHIVED         return False, None
# ARCHIVED     solid = _explore_findings_solid(findings)
# ARCHIVED     form = _explore_has_classic_form(findings)
# ARCHIVED     if (not solid) or form:
# ARCHIVED         return True, ("form_driving_needed" if form else "explore_insufficient")
# ARCHIVED     return False, None
# ARCHIVED
# ARCHIVED
# ARCHIVED def _invoke_navigation_explore(
# ARCHIVED     state: ScrapeState, config: RunnableConfig
# ARCHIVED ) -> dict[str, Any] | Command:
# ARCHIVED     """Graph wrapper for the deterministic navigation exploration node."""
# ARCHIVED     from .nodes.navigate_explore import navigate_explore as _explore
# ARCHIVED
# ARCHIVED     job_id = state.get("job_id", 0)
# ARCHIVED     _notify_phase(job_id, "navigation_explore", "running")
# ARCHIVED     try:
# ARCHIVED         result = _explore(dict(state), config)
# ARCHIVED         _notify_phase(job_id, "navigation_explore", "done")
# ARCHIVED
# ARCHIVED         if isinstance(result, dict) and result.get("playwright_unavailable"):
# ARCHIVED             logger.info(
# ARCHIVED                 "_invoke_navigation_explore: Playwright unavailable, "
# ARCHIVED                 "interrupting for user decision (job %s)",
# ARCHIVED                 job_id,
# ARCHIVED             )
# ARCHIVED             options = ["Use probe_html (no interaction)", "Retry Playwright", "Cancel"]
# ARCHIVED             return Command(
# ARCHIVED                 update={
# ARCHIVED                     "navigation_findings": result.get("navigation_findings"),
# ARCHIVED                     "interrupt_reason": "playwright_unavailable",
# ARCHIVED                     "interrupt_message": (
# ARCHIVED                         "Playwright MCP is unavailable but the site is NOT Akamai-protected. "
# ARCHIVED                         "The explore fell back to HTTP but may have missed JS-rendered content.\n\n"
# ARCHIVED                         "Options:\n"
# ARCHIVED                         "- **Use probe_html**: Proceed with single-page fetch (no clicking/scrolling)\n"
# ARCHIVED                         "- **Retry Playwright**: Retry — check that the browser_service container is running\n"
# ARCHIVED                         "- **Cancel**: Abort this job"
# ARCHIVED                     ),
# ARCHIVED                     "interrupt_options": options,
# ARCHIVED                     "interrupt_decisions": options_to_decisions(options),
# ARCHIVED                 },
# ARCHIVED                 goto="human_approval",
# ARCHIVED             )
# ARCHIVED
# ARCHIVED         # Handoff to the LLM navigation_agent when the deterministic explorer
# ARCHIVED         # detected a search form (classic_search) but couldn't get many real item
# ARCHIVED         # links from it — e.g. a JS/validation-gated POST form (locumtenens
# ARCHIVED         # QuickSearch: required-specialty + decorative-vs-real submit button). The
# ARCHIVED         # agent drives the form with browser tools + the navigation-patterns skill.
# ARCHIVED         # Threshold is generous (< 30) because listing_page.product_links can be
# ARCHIVED         # inflated by category/nav noise; classic_search detection (a multi-select
# ARCHIVED         # form was found) is the real signal of a form-driven job board.
# ARCHIVED         if isinstance(result, dict):
# ARCHIVED             # navigate_explore has inconsistent return shapes — some paths return
# ARCHIVED             # {"navigation_findings": findings, ...}, others return the bare
# ARCHIVED             # findings dict. Handle both.
# ARCHIVED             _f = result.get("navigation_findings") or result
# ARCHIVED             _lp = _f.get("listing_page") or {}
# ARCHIVED             _pl = len(_lp.get("product_links") or [])
# ARCHIVED             # Anti-bot guard: don't hand off to navigation_agent for anti-bot sites —
# ARCHIVED             # its MCP browser isn't cloak-enabled, so Akamai would block it. Anti-bot
# ARCHIVED             # sites (e.g. calvklein) find few links at analysis time (truncated
# ARCHIVED             # /render) but the RUNTIME scraper (cloak) gets the products, so a low
# ARCHIVED             # analysis-time count is expected + not a failure there.
# ARCHIVED             _probe = state.get("probe_result") or {}
# ARCHIVED             _ab = _probe.get("anti_bot") if isinstance(_probe, dict) else None
# ARCHIVED             _conn = _probe.get("connectivity") if isinstance(_probe, dict) else None
# ARCHIVED             _meth = (
# ARCHIVED                 (_probe.get("method") if isinstance(_probe, dict) else "")
# ARCHIVED                 or (_conn.get("method_that_worked") if isinstance(_conn, dict) else "")
# ARCHIVED                 or ""
# ARCHIVED             )
# ARCHIVED             _anti_bot = bool(
# ARCHIVED                 state.get("anti_bot_detected")
# ARCHIVED                 or (isinstance(_ab, dict) and _ab.get("detected"))
# ARCHIVED                 or str(_meth).startswith(("uc_chrome", "cloak"))
# ARCHIVED             )
# ARCHIVED             # Decide whether the heavy LLM navigation_agent is needed. The
# ARCHIVED             # deterministic explorer is now reliable (LLM URL selector + embedded-
# ARCHIVED             # JSON detector), so SKIP the agent (~10-26 min) when explore already
# ARCHIVED             # found a real listing with data — UNLESS a classic POST search form
# ARCHIVED             # was detected (the agent drives those forms) or the site is anti-bot
# ARCHIVED             # (the agent's MCP browser isn't cloak-enabled → never hand off there).
# ARCHIVED             _handoff, _reason = _navigation_handoff_decision(_f, _anti_bot)
# ARCHIVED             if _handoff:
# ARCHIVED                 logger.info(
# ARCHIVED                     "_invoke_navigation_explore: handing off to navigation_agent "
# ARCHIVED                     "(reason=%s, anti_bot=%s, %d links) (job %s)",
# ARCHIVED                     _reason, _anti_bot, _pl, job_id,
# ARCHIVED                 )
# ARCHIVED                 return Command(
# ARCHIVED                     update={
# ARCHIVED                         "navigation_findings": _f,
# ARCHIVED                         "handoff_reason": _reason,
# ARCHIVED                     },
# ARCHIVED                     goto="navigation_agent",
# ARCHIVED                 )
# ARCHIVED             logger.info(
# ARCHIVED                 "_invoke_navigation_explore: explore solid — SKIPPING "
# ARCHIVED                 "navigation_agent → synthesize (anti_bot=%s, %d links) (job %s)",
# ARCHIVED                 _anti_bot, _pl, job_id,
# ARCHIVED             )
# ARCHIVED
# ARCHIVED         return result
# ARCHIVED     except Exception as exc:
# ARCHIVED         logger.exception("_invoke_navigation_explore failed (job %s): %s", job_id, exc)
# ARCHIVED         _notify_phase(job_id, "navigation_explore", "failed")
# ARCHIVED         return {}
# ARCHIVED
# ARCHIVED
# ARCHIVED def _merge_explore_findings_into_analysis(analysis: dict, root: str, slug: str) -> dict:
# ARCHIVED     """Fill gaps in the navigation_agent's analysis from the explorer's findings.
# ARCHIVED
# ARCHIVED     The agent re-discovers and can produce a sparse/wrong analysis (e.g. aya: it
# ARCHIVED     overwrote a correct embedded-JSON finding with an empty analysis). Critical
# ARCHIVED     fields the explorer reliably found — working URL, data_source, embedded_json,
# ARCHIVED     item links, category links, api_endpoint — are merged in ONLY when the agent
# ARCHIVED     left them missing/empty. Never overwrites a field the agent populated.
# ARCHIVED     """
# ARCHIVED     if not isinstance(analysis, dict):
# ARCHIVED         return analysis
# ARCHIVED     try:
# ARCHIVED         nf_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
# ARCHIVED         if not os.path.isfile(nf_path):
# ARCHIVED             return analysis
# ARCHIVED         with open(nf_path, "r", encoding="utf-8") as f:
# ARCHIVED             findings = json.load(f)
# ARCHIVED     except Exception:
# ARCHIVED         return analysis
# ARCHIVED     lp = findings.get("listing_page") or {}
# ARCHIVED     hn = findings.get("homepage_nav") or {}
# ARCHIVED
# ARCHIVED     # Top-level data-model signals from the listing page
# ARCHIVED     for k in ("data_source", "embedded_json", "rendering_verified", "data_richness"):
# ARCHIVED         v = lp.get(k)
# ARCHIVED         if v not in (None, "", [], {}) and not analysis.get(k):
# ARCHIVED             analysis[k] = v
# ARCHIVED
# ARCHIVED     # search.working_url / listing_url_used
# ARCHIVED     search = analysis.get("search")
# ARCHIVED     if not isinstance(search, dict):
# ARCHIVED         search = {}
# ARCHIVED     wurl = (lp.get("url") or "").strip()
# ARCHIVED     if wurl and not (search.get("working_url") or search.get("listing_url_used")):
# ARCHIVED         search["working_url"] = wurl
# ARCHIVED         search["listing_url_used"] = wurl
# ARCHIVED         analysis["search"] = search
# ARCHIVED
# ARCHIVED     # item_links.url_examples / urls
# ARCHIVED     il = analysis.get("item_links")
# ARCHIVED     if not isinstance(il, dict):
# ARCHIVED         il = {}
# ARCHIVED     if not (il.get("urls") or il.get("url_examples")):
# ARCHIVED         hrefs = []
# ARCHIVED         for p in (lp.get("product_links") or []):
# ARCHIVED             h = p.get("href") if isinstance(p, dict) else p
# ARCHIVED             if isinstance(h, str) and h:
# ARCHIVED                 hrefs.append(h)
# ARCHIVED         if hrefs:
# ARCHIVED             il.setdefault("url_pattern", "")
# ARCHIVED             il["url_examples"] = hrefs[:10]
# ARCHIVED             il["urls"] = hrefs
# ARCHIVED             analysis["item_links"] = il
# ARCHIVED
# ARCHIVED     # categories.category_links
# ARCHIVED     cats = analysis.get("categories")
# ARCHIVED     if not (isinstance(cats, dict) and (cats.get("category_links") or [])):
# ARCHIVED         cat_links = [
# ARCHIVED             c.get("href") for c in (hn.get("category_links") or [])
# ARCHIVED             if isinstance(c, dict) and c.get("href")
# ARCHIVED         ]
# ARCHIVED         if cat_links:
# ARCHIVED             cats = cats if isinstance(cats, dict) else {}
# ARCHIVED             cats["category_links"] = cat_links[:20]
# ARCHIVED             analysis["categories"] = cats
# ARCHIVED
# ARCHIVED     # api_endpoint
# ARCHIVED     if not (isinstance(analysis.get("api_endpoint"), dict) and analysis["api_endpoint"].get("url")):
# ARCHIVED         try:
# ARCHIVED             from .nodes.navigate_synthesize import _best_api_endpoint
# ARCHIVED
# ARCHIVED             best = _best_api_endpoint(findings)
# ARCHIVED             if isinstance(best, dict) and best.get("url"):
# ARCHIVED                 analysis["api_endpoint"] = best
# ARCHIVED         except Exception:
# ARCHIVED             pass
# ARCHIVED
# ARCHIVED     return analysis
# ARCHIVED
# ARCHIVED
# ARCHIVED def _invoke_navigation_synthesize(
# ARCHIVED     state: ScrapeState, config: RunnableConfig
# ARCHIVED ) -> dict[str, Any] | Command:
# ARCHIVED     """Graph wrapper for the navigation synthesis node."""
# ARCHIVED     from .nodes.navigate_synthesize import navigate_synthesize as _synthesize
# ARCHIVED
# ARCHIVED     job_id = state.get("job_id", 0)
# ARCHIVED
# ARCHIVED     # If the LLM navigation_agent already wrote navigation_analysis.json (it runs
# ARCHIVED     # on the form-driven handoff path), skip re-synthesizing from raw findings —
# ARCHIVED     # the agent's structured output IS the analysis. Synthesize would otherwise
# ARCHIVED     # overwrite the agent's work with a re-reading of the (sparse) raw findings.
# ARCHIVED     try:
# ARCHIVED         slug = state.get("site_slug", "")
# ARCHIVED         na_path = os.path.join(_get_project_root(), "workspace", slug, "navigation_analysis.json")
# ARCHIVED         if state.get("handoff_reason") and os.path.isfile(na_path):
# ARCHIVED             root = _get_project_root()
# ARCHIVED             analysis = _read_json_artifact(root, slug, "navigation_analysis.json")
# ARCHIVED             if analysis:
# ARCHIVED                 # Merge guard: never let a sparse agent run discard the explorer's
# ARCHIVED                 # reliable findings — fill missing fields from navigation_findings.
# ARCHIVED                 analysis = _merge_explore_findings_into_analysis(analysis, root, slug)
# ARCHIVED                 try:
# ARCHIVED                     with open(na_path, "w", encoding="utf-8") as f:
# ARCHIVED                         json.dump(analysis, f, indent=2, ensure_ascii=False)
# ARCHIVED                 except Exception:
# ARCHIVED                     pass
# ARCHIVED                 logger.info(
# ARCHIVED                     "_invoke_navigation_synthesize: navigation_analysis.json from "
# ARCHIVED                     "navigation_agent (handoff) — merged with explore findings (job %s)",
# ARCHIVED                     job_id,
# ARCHIVED                 )
# ARCHIVED                 _notify_phase(job_id, "navigation_synthesize", "done")
# ARCHIVED                 return {"messages": [], "navigation_analysis": analysis}
# ARCHIVED     except Exception as exc:
# ARCHIVED         logger.warning("_invoke_navigation_synthesize: skip-check failed: %s", exc)
# ARCHIVED
# ARCHIVED     _notify_phase(job_id, "navigation_synthesize", "running")
# ARCHIVED     set_tool_context(dict(state), agent_name="navigation_synthesize")
# ARCHIVED     try:
# ARCHIVED         result = _synthesize(dict(state), config)
# ARCHIVED         _notify_phase(job_id, "navigation_synthesize", "done")
# ARCHIVED
# ARCHIVED         # SOURCE FIX: navigation_synthesize (LLM) sometimes drops the product
# ARCHIVED         # URLs discovered by navigation_explore. The product links are in
# ARCHIVED         # navigation_findings.json > listing_page.product_links — merge them into
# ARCHIVED         # navigation_analysis.json > item_links.urls if missing. This ensures
# ARCHIVED         # code_writer has the correct URLs to build the scraper around, instead
# ARCHIVED         # of generating broken discovery logic. [fix data flow at the source]
# ARCHIVED         try:
# ARCHIVED             slug = state.get("site_slug", "")
# ARCHIVED             root = _get_project_root()
# ARCHIVED             nf_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
# ARCHIVED             na_path = os.path.join(root, "workspace", slug, "navigation_analysis.json")
# ARCHIVED             if os.path.isfile(nf_path) and os.path.isfile(na_path):
# ARCHIVED                 import json as _json
# ARCHIVED                 nf = _json.load(open(nf_path))
# ARCHIVED                 na = _json.load(open(na_path))
# ARCHIVED                 # product URLs are nested in listing_page.product_links (list of dicts with 'href')
# ARCHIVED                 lp = nf.get("listing_page") or {}
# ARCHIVED                 _raw_links = lp.get("product_links") or []
# ARCHIVED                 product_urls = []
# ARCHIVED                 for _rl in _raw_links:
# ARCHIVED                     if isinstance(_rl, str):
# ARCHIVED                         product_urls.append(_rl)
# ARCHIVED                     elif isinstance(_rl, dict) and _rl.get("href"):
# ARCHIVED                         product_urls.append(_rl["href"])
# ARCHIVED                 if product_urls:
# ARCHIVED                     il = na.get("item_links")
# ARCHIVED                     if not isinstance(il, dict):
# ARCHIVED                         il = {}
# ARCHIVED                     existing = il.get("urls") or []
# ARCHIVED                     # Filter to strings only (some items may be dicts)
# ARCHIVED                     existing_str = [u for u in existing if isinstance(u, str)]
# ARCHIVED                     product_str = [u for u in product_urls if isinstance(u, str)]
# ARCHIVED                     if len(existing_str) < len(product_str):
# ARCHIVED                         il["urls"] = list(dict.fromkeys(existing_str + product_str))
# ARCHIVED                         na["item_links"] = il
# ARCHIVED                         with open(na_path, "w") as f:
# ARCHIVED                             _json.dump(na, f, indent=2, ensure_ascii=False)
# ARCHIVED                         logger.info(
# ARCHIVED                             "navigation_synthesize: merged %d product URLs from "
# ARCHIVED                             "findings into analysis.item_links.urls (had %d)",
# ARCHIVED                             len(product_urls), len(existing),
# ARCHIVED                         )
# ARCHIVED                         # ALSO update the state return value (result) so downstream
# ARCHIVED                         # nodes (code_writer, etc.) see the URLs without needing to
# ARCHIVED                         # re-read the file. This is the root-cause fix for the
# ARCHIVED                         # state-loses-URLs bug that required the input_urls.json
# ARCHIVED                         # workaround in _invoke_code_writer.
# ARCHIVED                         if isinstance(result, dict):
# ARCHIVED                             result["navigation_analysis"] = na
# ARCHIVED         except Exception as exc_merge:
# ARCHIVED             logger.warning("navigation_synthesize: URL merge failed: %s", exc_merge)
# ARCHIVED
# ARCHIVED         return result
# ARCHIVED     except Exception as exc:
# ARCHIVED         logger.exception(
# ARCHIVED             "_invoke_navigation_synthesize failed (job %s): %s", job_id, exc
# ARCHIVED         )
# ARCHIVED         _notify_phase(job_id, "navigation_synthesize", "failed")
# ARCHIVED         return {}
# ARCHIVED     finally:
# ARCHIVED         clear_tool_context()
# ═══ END ARCHIVED ═══


def _invoke_nav_skill_review(
    state: ScrapeState, config: RunnableConfig
) -> dict[str, Any] | Command:
    """Graph wrapper for the navigation skill review node.

    Non-blocking: any failure is logged and an empty dict returned so the
    graph proceeds to scraper_analyzer without skill updates.
    """
    from .nodes.navigate_skill_review import navigate_skill_review as _review

    job_id = state.get("job_id", 0)
    # Skip on non-SUCCESS (see _invoke_skill_learner guard for rationale).
    if state.get("execution_status", "FAILED") != "SUCCESS":
        logger.info(
            "_invoke_nav_skill_review: skipping (execution_status=%s, job %s)",
            state.get("execution_status"), job_id,
        )
        _notify_phase(job_id, "nav_skill_review", "skipped")
        return {"messages": []}
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


def _decide_strategy(state: ScrapeState) -> dict[str, Any]:
    """Deterministic strategy selection (replaces the LLM scraper_analyzer).

    Derives the scraping strategy from ``probe_result.connectivity.method_that_worked``
    (mirroring the old prompt's method -> strategy mapping), copies the proxy tier
    from the probe, and carries a ``critical_fix`` synthesized from the prior test
    crash on retry. ``_enforce_anti_bot_strategy`` remains the sole strategy
    authority (rewrites bad tokens to http_navigation for anti-bot sites).
    """
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "scraper_analyzer", "running")
    slug = state.get("site_slug", "")
    try:
        # ── Strategy cascade: record a failed prior strategy so it isn't re-picked.
        tried = list(state.get("strategies_tried") or [])
        _prior_strategy = (state.get("scraper_analysis") or {}).get("strategy", "")
        _prior_report = state.get("test_report") or {}
        _new_tried: list = []
        # A field-PASS can be downgraded by route_after_testing for insufficient
        # discovery coverage. Record the strategy in that case too, so it isn't
        # re-picked — otherwise the cascade loops on the same failed strategy.
        _cov_bad = False
        if isinstance(_prior_report, dict):
            try:
                from .nodes.route_after_testing import _discovery_coverage_failure
                _cov_bad = bool(_discovery_coverage_failure(_prior_report))
            except Exception as _e:
                logger.debug("_decide_strategy: coverage check skipped: %s", _e)
        if _prior_strategy and isinstance(_prior_report, dict) and (
            _prior_report.get("overall_assessment") not in (None, "PASS") or _cov_bad
        ):
            try:
                from .nodes.route_after_testing import classify_test_failure
                _action, _reason = classify_test_failure(_prior_report, _prior_strategy)
                if _action == "strategy" and not any(
                    (t.get("strategy") if isinstance(t, dict) else t) == _prior_strategy
                    for t in tried
                ):
                    _new_tried = [{"strategy": _prior_strategy, "reason": _reason}]
                    logger.info(
                        "_decide_strategy: strategy '%s' failed (%s) — recording (job %s)",
                        _prior_strategy, _reason, job_id,
                    )
            except Exception as _e:
                logger.warning("_decide_strategy: failure classify failed: %s", _e)

        analysis = _derive_strategy(state)
        # Anti-bot ⇒ http_navigation (cloak). Sole strategy authority.
        if _PATCHES_ENABLED:
            analysis = _enforce_anti_bot_strategy(analysis, slug, "scraper_analysis.json")
        # Escalation: _derive_strategy is a pure function of the probe method, so
        # without this it re-picks the SAME failing strategy every retry (the old
        # LLM analyzer read strategies_tried; the deterministic one must too). If
        # the chosen strategy was already tried+failed, escalate to a more capable
        # one (http_requests -> http_navigation -> playwright -> internal_api).
        _all_tried = {
            (_t.get("strategy") if isinstance(_t, dict) else _t)
            for _t in (tried + _new_tried)
        }
        _ESCALATION = ["http_requests", "http_navigation", "playwright", "internal_api"]
        _chosen = analysis.get("strategy")
        if _chosen in _all_tried:
            _idx = _ESCALATION.index(_chosen) if _chosen in _ESCALATION else -1
            for _next in _ESCALATION[_idx + 1:]:
                if _next not in _all_tried:
                    for _k in ("strategy", "scraping_mechanism", "scraping_method", "recommended_strategy"):
                        analysis[_k] = _next
                    analysis["strategy_justification"] = (
                        f"Deterministic escalation: {_chosen} tried+failed -> {_next}"
                    )
                    logger.info(
                        "_decide_strategy: %s tried+failed -> escalating to %s (job %s)",
                        _chosen, _next, job_id,
                    )
                    break
        # Persist so downstream nodes/code_writer read the artifact from disk.
        try:
            root = _get_project_root()
            with open(os.path.join(root, "workspace", slug, "scraper_analysis.json"),
                      "w", encoding="utf-8") as f:
                json.dump(analysis, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("_decide_strategy: could not write scraper_analysis.json: %s", exc)

        update: dict[str, Any] = {"messages": [], "scraper_analysis": analysis}
        if _new_tried:
            update["strategies_tried"] = _new_tried  # append (Annotated[list, operator.add])
        _notify_phase(job_id, "scraper_analyzer", "done")
        return update
    except Exception:
        _notify_phase(job_id, "scraper_analyzer", "failed")
        raise


def _derive_strategy(state: ScrapeState) -> dict[str, Any]:
    """Map probe_result.connectivity.method_that_worked to a scraping strategy.

    Mirrors the mapping the old LLM scraper_analyzer was prompted with:
      - direct_http  -> http_requests (proxy none), unless discovery is form-POST-only
        (the requests template can't POST forms) -> http_navigation
      - browser_none -> http_navigation (proxy none)
      - uc_chrome_* / cloak_* -> http_navigation, proxy from the method suffix
    """
    probe = state.get("probe_result") or {}
    if not isinstance(probe, dict):
        probe = {}
    conn = probe.get("connectivity") or {}
    method = (conn.get("method_that_worked") if isinstance(conn, dict) else "") or ""
    # Fallback to site_analysis connectivity (probe may be sparse on resume).
    if not method:
        _sa = state.get("site_analysis") or {}
        _sa_conn = (_sa.get("connectivity") if isinstance(_sa, dict) else {}) or {}
        method = (
            (_sa_conn.get("method_that_worked") if isinstance(_sa_conn, dict) else "")
            or ""
        )
    method = method or ""

    # Anti-bot signal: explicit flag OR only-working-method is a stealth browser.
    _ab = probe.get("anti_bot") or {}
    anti_bot = isinstance(_ab, dict) and bool(_ab.get("detected"))
    if not anti_bot and method.startswith(("uc_chrome", "cloak")):
        anti_bot = True

    # Proxy tier from the method suffix (mirrors the prompt mapping).
    if "residential" in method:
        proxy_tier = "residential"
    elif "datacenter" in method:
        proxy_tier = "datacenter"
    else:
        proxy_tier = "none"

    meth = method.lower()
    # Listing-page JS-rendering signal (navigate_explore._verify_rendering, propagated
    # via navigate_synthesize). "csr" = item links only appear after JS rendering, so
    # http_requests can't reach them → pick a browser strategy upfront. Fixes the
    # ayahealthcare class: homepage reachable via direct_http but listings JS-rendered.
    _nav = state.get("navigation_analysis") or {}
    _rendering = (_nav.get("rendering_verified") if isinstance(_nav, dict) else None) or "unknown"
    # Embedded-JSON data-model signal (navigate_explore detector → navigate_synthesize).
    # Surfaced on scraper_analysis so code_writer sees it in one place and it survives
    # retries. "embedded_json" = items live in a <script> JSON blob in the listing page,
    # NOT detail pages — a third data model. The strategy itself still comes from the
    # rendering cascade above (ssr→http_requests / csr→http_navigation); this only tags
    # the model so code_writer extracts records from the listing JSON (no per-detail Phase 2).
    _data_source = (_nav.get("data_source") if isinstance(_nav, dict) else None) or "none"
    _embedded_json = (_nav.get("embedded_json") if isinstance(_nav, dict) else None) or None
    # JS-rendered listing → browser-backed strategy, REGARDLESS of how the probe
    # reached the page (method_that_worked). The listing's render need is
    # independent of probe access: a site whose homepage is reachable via
    # cloak_none can still have a Coveo/React listing that http_navigation's
    # /navigate (2s render wait) can't surface. The previous gate nested this
    # inside `if meth == "direct_http"`, so a cloak_none probe bypassed it and
    # picked http_navigation for a Coveo site → 0 discovered.
    if _rendering == "browser":
        # browser_traverse ran (rendering=browser), but that ALONE doesn't mean the
        # listing is JS-rendered — a form-POST→SSR site (locumtenens) is HTTP-
        # reachable via POST replay and must stay on http_requests. Only a GET-
        # navigated listing that needed the browser to render (Coveo/React) needs
        # playwright. Distinguish by the discovery form method the traverser recorded:
        # POST → SSR results → http_requests; GET → JS-rendered listing → playwright.
        _form_method = ""
        _search = _nav.get("search") if isinstance(_nav, dict) else None
        if isinstance(_search, dict):
            _form_method = (_search.get("form_method") or "").upper()
        strategy = "http_requests" if _form_method == "POST" else "playwright"
    elif _rendering == "csr":
        # navigate_explore._verify_rendering: lighter CSR where the server-side
        # /navigate render surfaces the links.
        strategy = "http_navigation"
    elif meth == "direct_http" and not _is_form_only_discovery(state, state.get("url", "")):
        strategy = "http_requests"
    else:
        # browser_none, uc_chrome_*, cloak_*, or form-only direct_http → browser-backed.
        strategy = "http_navigation"

    # API-strategy override: when browser_traverse captured a backend JSON data API
    # (data_source == "api" + an api_endpoint), the items come from that API — use
    # internal_api (HTTP + JSON paginated loop), NOT http_requests/http_navigation.
    # This aligns scraper_analysis.strategy with the api_section + api_scraper.py
    # template hint build_code_writer_message already emits; without it, code_writer
    # follows the http_requests strategy field and builds a listing-paginating scraper
    # that hangs on CSR pages with no paginated listing (aya).
    _nav_api = (_nav.get("api_endpoint") if isinstance(_nav, dict) else None) or {}
    _nav_api = _nav_api if isinstance(_nav_api, dict) else {}
    # Gate on the API being a REAL data source for this query. Two signals, both
    # required:
    #   - items_per_page > 0  (verify_api's probe got actual records), AND
    #   - count != 0          (the API's reported total isn't explicitly zero).
    # The count!=0 check is load-bearing: Coveo /coveo/rest/search reports
    # totalCount=0 for the generic query yet returns ~15 default-sample items to
    # verify_api's limit/pageSize probe — items_per_page>0 ALONE wrongly picks
    # internal_api, then the api_scraper (which can't reconstruct the browser's
    # @filter) gets ~0-1 items (lw.com regression: 1 item vs playwright's 20).
    # count>0 (aya: 26955) OR count is None (an API that doesn't report a total
    # but returns real items) both pass; only an EXPLICIT zero total is rejected.
    _api_items = _nav_api.get("items_per_page")
    _api_count = _nav_api.get("count")
    if (
        _data_source == "api"
        and (_nav_api.get("url") or _nav_api.get("api_url"))
        and isinstance(_api_items, int)
        and _api_items > 0
        and _api_count != 0
    ):
        strategy = "internal_api"

    # Discovery config: propagate the navigator's pagination detection so the
    # template uses the RIGHT config_for_* preset (load_more vs page_param vs
    # next_button) — deterministic, not code_writer's guess.
    _disc_pag = (_nav.get("discovery") or {}).get("pagination") if isinstance(_nav, dict) else None
    if not _disc_pag:
        _disc_pag = _nav.get("pagination") if isinstance(_nav, dict) else None
    _discovery_config = None
    if isinstance(_disc_pag, dict) and _disc_pag.get("type"):
        # The navigator (navigate_explore.py) emits `page_param`/`page_size` for
        # offset_param (?start=0&sz=24); other paths emit canonical
        # `page_param_name`/`items_per_page`. Accept BOTH so offset_param values
        # reach config_for_page_param via discovery_config.json instead of being
        # silently dropped (graph.py field-name pipeline bug).
        _discovery_config = {
            "type": _disc_pag.get("type"),
            "page_param_name": _disc_pag.get("page_param_name") or _disc_pag.get("page_param"),
            "items_per_page": _disc_pag.get("items_per_page") or _disc_pag.get("page_size"),
            "next_button_selector": _disc_pag.get("next_button_selector"),
            "max_pages": _disc_pag.get("max_pages"),
        }

    analysis: dict[str, Any] = {
        "strategy": strategy,
        "scraping_mechanism": strategy,
        "scraping_method": strategy,
        "recommended_strategy": strategy,
        "proxy_tier": proxy_tier,
        "connectivity": {"method_that_worked": method},
        "anti_bot": {"detected": anti_bot},
        "confidence_score": 0.9,
        "data_source": _data_source,
        "embedded_json": _embedded_json,
        "api_endpoint": _nav_api,
        "discovery_config": _discovery_config,
        "strategy_justification": (
            f"Deterministic: method_that_worked={method or 'unknown'} -> {strategy} "
            f"(proxy={proxy_tier}, anti_bot={anti_bot}, rendering={_rendering}, "
            f"data_source={_data_source})"
        ),
    }

    # On retry, carry a critical_fix synthesized from the prior crash so code_writer
    # makes a targeted fix (the read-only analyzer that authored critical_fix is gone).
    _tr = state.get("test_report") or {}
    _crash = (_tr.get("crash_error") or "") if isinstance(_tr, dict) else ""
    # code_tester nests crash info at script_checks.crash_error (not top-level).
    if not _crash and isinstance(_tr, dict):
        _sc = _tr.get("script_checks")
        if isinstance(_sc, dict):
            _crash = _sc.get("crash_error") or _sc.get("error_message") or ""
    if _crash:
        analysis["critical_fix"] = {
            "issue": f"Previous scraper crashed: {str(_crash)[:300]}",
            "root_cause": "See crash above — the scraper hit this error during testing.",
            "fix": "Make a MINIMAL, targeted fix for THIS error; do NOT rewrite from scratch.",
        }
    return analysis


def _is_form_only_discovery(state: dict, url: str) -> bool:
    """True when discovery requires POSTing a form the requests template can't do.

    Generic — keys on structural signals in navigation_analysis (no category_links
    + POST/CSRF search or form-method filters), excluding sites with a same-domain
    JSON API (those use internal_api). Mirrors the old prompt override.
    """
    nav = state.get("navigation_analysis") or {}
    if not isinstance(nav, dict):
        return False
    categories = nav.get("categories") or {}
    category_links = (
        categories.get("category_links") if isinstance(categories, dict) else None
    ) or []
    search = nav.get("search") or {}
    filters = nav.get("filters") or {}
    form_only = (
        (not category_links)
        and (
            (isinstance(search, dict) and search.get("classic_search_method") == "post")
            or (isinstance(search, dict) and bool(search.get("classic_search_requires_csrf")))
            or (isinstance(filters, dict) and filters.get("method") == "form")
        )
    )
    if not form_only:
        return False
    from urllib.parse import urlparse as _urlparse

    api = nav.get("api_endpoint") or {}
    api_url = (api.get("url") or "") if isinstance(api, dict) else ""
    if api_url:
        api_host = _urlparse(api_url).hostname or ""
        site_host = _urlparse(url).hostname or ""
        if api_host and site_host and api_host == site_host:
            return False
    return True



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
                result = _invoke_agent_with_timeout(
                    agent, fix_msg, _agent_config(config, "code_writer"),
                    "code_writer", job_id,
                )
                _persist_agent_logs(state, result, "code-writer", config)
            finally:
                _stop_heartbeat(hb)
    logger.error(
        "_invoke_code_writer: syntax errors persist after %d attempts (job %s) — letting code_tester catch it",
        max_tries, job_id,
    )




def _select_template_file(state: ScrapeState) -> str:
    """Return the template filename for this job's strategy/data_source.

    Simplified selection covering the 5 main templates. The message builder
    (build_code_writer_message) has a more detailed selection (mechanism-based,
    anti-bot notes, embedded_json variants) — that runs in parallel and its
    template_hint still appears in the message. This function selects the
    template for the SYSTEM PROMPT (where the full code is injected so it's
    never summarized). For edge cases (undetected_chromedriver, navigation_scraper),
    the LLM can still read_file the template — the system prompt's template is a
    reference, not a replacement for the message's hint.
    """
    nav = state.get("navigation_analysis") or {}
    sa = state.get("scraper_analysis") or {}
    strategy = (sa.get("strategy") or "").lower()
    data_source = nav.get("data_source", "")
    api_ep = nav.get("api_endpoint") or {}

    if isinstance(api_ep, dict) and (api_ep.get("url") or api_ep.get("api_url")):
        return "api_scraper.py"
    if data_source == "ssr_div_list":
        return "ssr_div_list_scraper.py"
    if strategy in ("http_requests", "requests"):
        # Form-POST sites (locumtenens: QuickSearch POST → SSR) need the
        # navigation template (playwright form-POST replay + FORM_ACTION),
        # not the plain requests template. Verified: locumtenens' working
        # scraper imports playwright.sync_api + uses FORM_ACTION.
        _nav_fm = state.get("navigation_analysis") or {}
        _form_method = ((_nav_fm.get("search") or {}).get("form_method") or "").upper()
        if _form_method == "POST":
            return "navigation_scraper.py"
        return "requests_scraper.py"
    if strategy == "http_navigation":
        return "http_navigation_scraper.py"
    if strategy == "playwright":
        # Playwright strategy → playwright_scraper.py (its discover step
        # render-polls, which is what surfaces JS-rendered listings like Coveo
        # that http_navigation's /navigate 2s wait cannot). Mapping playwright
        # to http_navigation_scraper.py silently defeated the strategy.
        return "playwright_scraper.py"
    if strategy in ("internal_api", "api"):
        return "api_scraper.py"
    return "requests_scraper.py"


def _invoke_code_writer(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "code_writer", "running")
    set_tool_context(dict(state), agent_name="code_writer")
    try:
        logger.info("_invoke_code_writer: starting (job %s)", job_id)
        update = {}
        # Count a test-retry whenever re-entering from route_after_testing with a
        # prior test_report (a real test failure). The test_retry_count budget
        # caps the regenerate-test loop (MAX_TEST_RETRIES).
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
        # Reads from STATE (navigation_synthesize merges URLs into state
        # via the result update — see _invoke_navigation_synthesize).
        try:
            import json as _json
            na = state.get("navigation_analysis") or {}
            na = na if isinstance(na, dict) else {}
            sample_urls: list = []
            if na.get("data_source") == "embedded_json":
                # Embedded-JSON model: the LISTING/category pages carry the data
                # (not detail pages). Seed listing + category URLs so --input/--sample
                # tests fetch listing pages and extract the embedded JSON — the correct
                # test for this model. [plan: embedded-json model]
                search = na.get("search") or {}
                search = search if isinstance(search, dict) else {}
                for k in ("working_url", "listing_url_used", "url_pattern", "search_url_pattern"):
                    v = search.get(k)
                    if isinstance(v, str) and v and not v.startswith(("javascript", "#")):
                        sample_urls.append(v)
                cats = na.get("categories") or {}
                for c in (cats.get("category_links") or []) if isinstance(cats, dict) else []:
                    if isinstance(c, str) and c:
                        sample_urls.append(c)
                sample_urls = list(dict.fromkeys(sample_urls))
                logger.info(
                    "_invoke_code_writer: embedded_json model — seeding %d listing/category URLs",
                    len(sample_urls),
                )
            elif na.get("data_source") == "ssr_div_list":
                # Seed the LISTING URL (not per-item URLs) — the ssr_div_list
                # scraper fetches the listing page + extracts records from the
                # DOM directly (no per-item detail pages).
                search = na.get("search") or {}
                sample_urls = [v for v in (search.get("working_url"), search.get("listing_url_used")) if v]
            else:
                il = na.get("item_links") or {}
                sample_urls = il.get("urls") or il.get("url_examples") or []
            if sample_urls:
                iu_path = os.path.join(_get_project_root(), "workspace", slug, "input_urls.json")
                # Bug 1 fix: don't overwrite input_urls.json if it already has MORE
                # URLs than the seed set. code_tester's discovery may have saved
                # hundreds/thousands of URLs; overwriting with 5-20 seeds destroys
                # them before run_execution can use them. Only overwrite when the
                # seed set is richer (first run or navigation found more URLs).
                try:
                    if os.path.isfile(iu_path):
                        with open(iu_path, "r") as _ef:
                            _existing = _json.load(_ef).get("urls", [])
                        if len(_existing) > len(sample_urls):
                            logger.info(
                                "_invoke_code_writer: preserving existing input_urls.json "
                                "(%d URLs > %d seeds) — not overwriting",
                                len(_existing), len(sample_urls),
                            )
                            sample_urls = []  # skip the write
                except Exception:
                    pass
                if sample_urls:
                    with open(iu_path, "w") as _f:
                        _json.dump({"urls": sample_urls}, _f, indent=2)
                    logger.info("_invoke_code_writer: wrote %d sample URLs to input_urls.json", len(sample_urls))
        except Exception as _exc:
            logger.warning("_invoke_code_writer: failed to write input_urls.json: %s", _exc)

        # Write discovery_config.json (from scraper_analysis) so the template's
        # discover_product_urls can select the RIGHT config_for_* preset
        # deterministically (load_more vs page_param vs next_button) — driven by
        # the navigator's observation, not code_writer's guess.
        try:
            _sa = state.get("scraper_analysis") or {}
            _dc = _sa.get("discovery_config") if isinstance(_sa, dict) else None
            if _dc and isinstance(_dc, dict) and _dc.get("type"):
                _dc_path = os.path.join(_get_project_root(), "workspace", slug, "discovery_config.json")
                with open(_dc_path, "w") as _df:
                    _json.dump(_dc, _df, indent=2)
                logger.info("_invoke_code_writer: wrote discovery_config.json (type=%s)", _dc.get("type"))
        except Exception as _exc:
            logger.warning("_invoke_code_writer: failed to write discovery_config.json: %s", _exc)

        messages = build_code_writer_message(state)
        _log_agent_context(state, "code-writer", messages)

        # Read the selected template file + inject into the system prompt (so the
        # template code is NEVER summarized by SummarizationMiddleware — the system
        # prompt is always present in full, only the conversation history is
        # summarized). This also saves a read_file round-trip (the LLM already has
        # the template; no need to read_file it).
        _template_file = _select_template_file(state)
        _template_code = ""
        try:
            _tp = os.path.join(_get_project_root(), "templates", _template_file)
            with open(_tp) as _tf:
                _template_code = _tf.read()
            logger.info("_invoke_code_writer: template %s (%d lines) injected into system prompt",
                        _template_file, _template_code.count("\n"))
        except Exception as _exc:
            logger.warning("_invoke_code_writer: could not read template %s: %s", _template_file, _exc)

        agent = create_code_writer(site_slug=slug, template_code=_template_code)
        hb = _start_heartbeat(job_id, "code-writer")
        # F5: try/finally — an exception here previously leaked the timer chain.
        _cw_cfg = _agent_config(config, "code_writer")
        try:
            result = _invoke_agent_with_timeout(agent, messages, _cw_cfg, "code_writer", job_id)
        finally:
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
            _patch_scraper_output_filter(slug, _ct, state.get("target_fields") or [])
            _enforce_discovery_import(slug)
            _enforce_env_discovery_gate(slug)

        # Deterministic backstop: if scraper_analysis documented a non-existent
        # selector in critical_fix, warn loudly if the regenerated scraper still
        # uses it (catches the regression the prompt-level fix in subagents.py
        # is designed to prevent).
        _warn_unaddressed_critical_fix(slug, state.get("scraper_analysis") or {})

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




def _probe_phase1_discovery(slug: str, state: dict, job_id: int) -> tuple[bool, str | None]:
    """Deterministically run the draft's Phase-1 discovery to catch discovery-path
    bugs that ``--sample`` testing skips.

    ``--sample`` scrapes pre-seeded URLs and never enters Phase 1, so a crash in
    discovery/pagination (e.g. a hallucinated ``session.url``) sails through
    testing and only blows up at execution. This probe runs
    ``scraper_draft.py --discover-only --fresh-discovery`` (Phase 1 only, skips
    the expensive Phase 2 extraction) and fails the test on a real crash.

    Returns ``(crashed, traceback_tail)``. Timeouts / unsupported flags are
    inconclusive (``crashed=False``) — we don't fail on slow or opaque discovery.
    """
    if not slug:
        return False, None
    # Only jobs with a discovery phase (nav modes); url_list has no Phase 1.
    if state.get("input_mode") not in ("search_term", "list_page", "navigation"):
        return False, None
    import subprocess

    try:
        root = _get_project_root()
        draft = os.path.join(root, "workspace", slug, "scraper_draft.py")
        if not os.path.isfile(draft):
            return False, None
        # Only probe if the draft actually supports --discover-only (static AST check).
        from agents.nodes.run_execution import _accepted_cli_flags

        accepted = _accepted_cli_flags(draft)
        if accepted is not None and "discover-only" not in accepted:
            return False, None
        logger.info("_probe_phase1_discovery: running --discover-only (job %s)", job_id)
        probe_args = ["--discover-only", "--fresh-discovery"]
        # Browser scrapers (Playwright/Selenium) can ONLY run in browser_service —
        # celery-worker has neither installed. Running the draft directly here
        # ModuleNotFoundError-crashes every browser draft, which route_after_testing
        # reads as "playwright failed (no items)" → wrong strategy switch. Mirror
        # run_scraper's dispatch: browser draft → browser_service /scrape; else local.
        from agents.tools.shell_tools import _scraper_needs_browser, _get_browser_service_url

        if _scraper_needs_browser(draft):
            import httpx

            try:
                # Stateless /scrape: read the local draft source, POST it.
                try:
                    with open(draft, "r", encoding="utf-8", errors="replace") as _pf:
                        _draft_source = _pf.read()
                except OSError:
                    _draft_source = ""
                # Read sibling files (discovery_config.json) for staging
                _probe_extra = {}
                for _sf in ("input_urls.json", "discovery_config.json"):
                    _sp = os.path.join(os.path.dirname(draft), _sf)
                    if os.path.isfile(_sp):
                        try:
                            with open(_sp, "r", encoding="utf-8", errors="replace") as _fh:
                                _probe_extra[_sf] = _fh.read()
                        except OSError:
                            pass
                resp = httpx.post(
                    f"{_get_browser_service_url()}/scrape",
                    json={"scraper_source": _draft_source, "scraper_name": os.path.basename(draft), "extra_files": _probe_extra, "args": probe_args, "timeout": 180, "max_retries": 1},
                    timeout=180 + 60,
                )
                resp.raise_for_status()
                result = resp.json()
                rc = result.get("returncode", 0)
                stderr = result.get("stderr") or ""
            except Exception as exc:
                logger.warning(
                    "_probe_phase1_discovery: browser_service dispatch failed (%s) — inconclusive",
                    exc,
                )
                return False, None
        else:
            proc = subprocess.run(
                ["python3", draft] + probe_args,
                cwd=os.path.join(root, "workspace", slug),
                capture_output=True, text=True, timeout=180,
            )
            rc = proc.returncode
            stderr = proc.stderr or ""
        if rc != 0 and "Traceback" in stderr:
            lines = stderr.strip().splitlines()
            tail = "\n".join(lines[-12:]) if lines else stderr[:800]
            logger.warning(
                "_probe_phase1_discovery: CRASHED (job %s, rc=%s):\n%s",
                job_id, rc, tail,
            )
            return True, tail
        logger.info(
            "_probe_phase1_discovery: OK (job %s, rc=%s)", job_id, rc
        )
    except subprocess.TimeoutExpired:
        logger.info(
            "_probe_phase1_discovery: timed out (job %s) — inconclusive", job_id
        )
    except Exception as exc:
        logger.warning("_probe_phase1_discovery: errored (job %s): %s", job_id, exc)
    return False, None


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
        # F5: try/finally — an exception here previously leaked the timer chain.
        # (Note: this site uses raw agent.invoke with NO timeout wrapper —
        # pre-existing; the finally at least guarantees heartbeat cleanup.)
        try:
            result = agent.invoke(
                {"messages": messages}, config=_agent_config(config, "code_tester")
            )
        finally:
            _stop_heartbeat(hb)
        _persist_agent_logs(state, result, "code-tester", config)
        _notify_phase(job_id, "code_tester", "done")
        update = {"messages": []}
        report = _load_test_report(slug)
        if report:
            # Phase 4a: deterministically attach the scraper's discovery_coverage
            # so the coverage-aware classifier sees it (the LLM-written report
            # doesn't reliably carry it).
            report = _attach_discovery_coverage(report, slug)
            update["test_report"] = report
            logger.info(
                "_invoke_code_tester: loaded test_report from workspace/%s/", slug
            )
            _preserve_test_report(slug)
        else:
            logger.warning(
                "_invoke_code_tester: no test_report found at workspace/%s/", slug
            )

        # Deterministic Phase-1 discovery probe: catches discovery-path crashes
        # that --sample testing skips (e.g. session.url phantom attributes). On a
        # real crash, force the test to FAIL so route_after_testing retries
        # code_writer with the traceback.
        crashed, tb = _probe_phase1_discovery(slug, dict(state), job_id)
        if crashed:
            report = report or {}
            report["overall_assessment"] = "FAIL"
            report["confidence_score"] = 0.0
            report["ready_for_execution"] = False
            report.setdefault("issues", []).insert(
                0, {"severity": "high", "message": "Phase-1 discovery crashed: " + (tb or "")}
            )
            report["feedback_for_writer"] = (
                "PHASE-1 DISCOVERY CRASH — caught by the deterministic discovery "
                "probe (which --sample skips, since --sample uses pre-seeded URLs "
                "and never enters Phase 1):\n" + (tb or "")
                + "\nFix the discovery/pagination code. Do NOT re-signature the "
                "template's helpers or reference nonexistent attributes (e.g. "
                "`session.url` — capture the URL from the response instead)."
            )
            update["test_report"] = report
            logger.warning(
                "_invoke_code_tester: discovery probe FAILED the test (job %s) → retry", job_id
            )
        return update
    except Exception:
        _notify_phase(job_id, "code_tester", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_cleanup(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "cleanup", "running")
    set_tool_context(dict(state), agent_name="cleanup")
    try:
        logger.info("_invoke_cleanup: starting (job %s)", job_id)

        slug = state.get("site_slug", "")
        # Archive the current production scraper BEFORE the agent runs, so we can
        # restore it on failure (the agent used to clobber it unconditionally).
        archive_path = _archive_existing_scraper(slug)

        messages = build_cleanup_message(state)
        _log_agent_context(state, "cleanup", messages)
        agent = create_cleanup_agent(site_slug=slug)
        result = agent.invoke(
            {"messages": messages}, config=_agent_config(config, "cleanup")
        )
        _persist_agent_logs(state, result, "cleanup", config)

        # Deterministic, failure-safe scraper promotion (the agent no longer cp's
        # scraper.py — see build_cleanup_message). Per-job copy + success gate.
        scraper_path = _promote_scraper(
            slug, job_id, state.get("execution_status", ""), archive_path
        )
        _notify_phase(job_id, "cleanup", "done")
        out: dict[str, Any] = {"messages": []}
        if scraper_path:
            out["scraper_path"] = scraper_path
        return out
    except Exception:
        _notify_phase(job_id, "cleanup", "failed")
        raise
    finally:
        clear_tool_context()


def _invoke_skill_learner(state: ScrapeState, config: RunnableConfig) -> dict[str, Any]:
    job_id = state.get("job_id", 0)
    # Skip on non-SUCCESS: learning from a failed/incomplete scrape injects
    # garbage into the skill DB (skill_learner writes reusable skills +
    # copies learning_report.json into scrapers/<slug>/analysis/). Mirrors
    # _invoke_store_job_listings's guard. != SUCCESS is forward-compatible
    # with a future PARTIAL status.
    if state.get("execution_status", "FAILED") != "SUCCESS":
        logger.info(
            "_invoke_skill_learner: skipping (execution_status=%s, job %s)",
            state.get("execution_status"), job_id,
        )
        _notify_phase(job_id, "skill_learner", "skipped")
        return {"messages": []}
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
                import src.artifacts as artifacts
                from django.conf import settings

                ws = os.path.join(settings.PROJECT_ROOT, "workspace", slug)
                # Preserve learning + nav_learning reports to the File Master.
                for _name in ("learning_report.json", "nav_learning_report.json"):
                    _src = os.path.join(ws, _name)
                    if os.path.isfile(_src):
                        with open(_src, "rb") as _f:
                            _bytes = _f.read()
                        _key = artifacts.scrapers_key(slug, "analysis", _name)
                        artifacts.write(_key, _bytes)
                        logger.info(
                            "_invoke_skill_learner: copied %s → scrapers/%s/analysis/",
                            _name, slug,
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
    _notify_phase(job_id, "dagster_converter", "running")

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
            # Syntax + import-binding check (P0-5: ast.parse alone misses
            # commented-out imports and undefined base classes — the file
            # "syntax OK"s but NameErrors at import time).
            try:
                import ast
                with open(ws_dagster, "r") as f:
                    _src = f.read()
                _tree = ast.parse(_src)
                # Collect names bound by imports/classdefs/assignments at module scope.
                _bound = set()
                for _node in ast.iter_child_nodes(_tree):
                    if isinstance(_node, (ast.Import, ast.ImportFrom)):
                        for _alias in _node.names:
                            _bound.add(_alias.asname or _alias.name.split(".")[0])
                    elif isinstance(_node, ast.ClassDef):
                        _bound.add(_node.name)
                    elif isinstance(_node, ast.Assign):
                        for _t in _node.targets:
                            if isinstance(_t, ast.Name):
                                _bound.add(_t.id)
                # Check that every base class referenced in a ClassDef is bound
                # (not commented out). Catches `class X(BaseTlsScraper):` when
                # `# from dagster_scraper_base import BaseTlsScraper` is commented.
                _unresolved = []
                for _node in ast.walk(_tree):
                    if isinstance(_node, ast.ClassDef):
                        for _base in _node.bases:
                            if isinstance(_base, ast.Name) and _base.id not in _bound:
                                _unresolved.append(f"class {_node.name}: base '{_base.id}' not imported")
                if _unresolved:
                    logger.warning(
                        "_invoke_dagster_converter: %s_dagster.py has unresolved "
                        "names (won't import): %s — file NOT copied to scrapers/",
                        slug, "; ".join(_unresolved[:3]),
                    )
                else:
                    # Per-job copy to the File Master always; promote to production
                    # {slug}_dagster.py only on success (mirrors _promote_scraper —
                    # don't clobber a good dagster file with a failed job's output).
                    import src.artifacts as artifacts

                    with open(ws_dagster, "rb") as _f:
                        _dagster_bytes = _f.read()
                    per_job_key = artifacts.scrapers_key(slug, "jobs", f"dagster-{job_id}.py")
                    artifacts.write(per_job_key, _dagster_bytes)
                    if state.get("execution_status") == "SUCCESS":
                        prod_key = artifacts.scrapers_key(slug, f"{slug}_dagster.py")
                        artifacts.write(prod_key, artifacts.read(per_job_key))
                        logger.info(
                            "_invoke_dagster_converter: SUCCESS → promoted %s_dagster.py (job %s)",
                            slug, job_id,
                        )
                    else:
                        logger.info(
                            "_invoke_dagster_converter: non-SUCCESS → %s_dagster.py "
                            "left as-is, per-job copy at jobs/dagster-%s.py",
                            slug, job_id,
                        )
                    return {"messages": [], "dagster_path": per_job_key}
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
    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "store_job_listings", "running")
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
            output_file = _find_newest_output(workspace_folder, site_folder, slug=slug)
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
            # P0-13: assess posted_date reliability. Sites that dynamically set
            # datePosted to "today" produce fabricated freshness. Don't overwrite
            # a prior reliable date with an unreliable one on update.
            _scrape_date = _dt.now().date()
            _date_str, _reliable, _reason = (None, True, "ok")
            if posted_raw:
                try:
                    from src.job_fields import assess_date_reliability
                    _date_str, _reliable, _reason = assess_date_reliability(str(posted_raw), _scrape_date)
                except Exception:
                    _reliable = True  # conservative: trust the date if assessment fails

            defaults = {
                "title": (item.get("title") or "")[:500],
                "company": (item.get("company") or "")[:300],
                "location": (item.get("location") or "")[:300],
                "description": item.get("description") or "",
                "salary": (item.get("salary") or "")[:300],
                "job_type": (item.get("job_type") or "")[:100],
                "employment_type": (item.get("employment_type") or item.get("employment_type") or "")[:100],
                "date_posted_reliable": _reliable,
                "valid_through": valid_through,
                "site_name": site_name,
                "site": site_obj,
                "scrape_job": scrape_job_ref,
                "extra_data": extra,
            }

            # Dedup: prefer url, fall back to job_source_id
            # P0-13: only set posted_date when reliable (avoids overwriting
            # with a fabricated "today" on every re-scrape). When unreliable,
            # leave posted_date as-is (NULL on first create) — the dashboard
            # uses scraped_at (first_seen_at) as the freshness signal instead.
            if _reliable and posted_date:
                defaults["posted_date"] = posted_date

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
                # No natural dedup key — synthesize a DETERMINISTIC one from the
                # stable fields so re-scrapes (and acks_late redeliveries in
                # Phase 3) UPDATE instead of creating duplicates. The old bare
                # .create(url="", job_source_id="", ...) produced a fresh row per
                # item per run (the locumtenens-style dup explosion).
                import hashlib as _hashlib

                _key_src = "␟".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("company") or ""),
                        str(item.get("location") or ""),
                    ]
                )
                _synth_id = "synth:" + _hashlib.sha1(
                    _key_src.encode("utf-8")
                ).hexdigest()[:24]
                defaults["job_source_id"] = _synth_id
                obj, created = JobListing.objects.update_or_create(
                    site_slug=slug, job_source_id=_synth_id, defaults=defaults
                )

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


# ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
# ARCHIVED def _route_after_navigation_explore(state: ScrapeState) -> str:
# ARCHIVED     """Route after navigation_explore.
# ARCHIVED
# ARCHIVED     Normally proceeds to navigation_synthesize.  If navigate_explore
# ARCHIVED     flagged playwright_unavailable, the node already issued a
# ARCHIVED     Command(goto="human_approval") internally — this function only
# ARCHIVED     handles the case where the state carries the flag without a Command
# ARCHIVED     (defensive fallback).
# ARCHIVED     """
# ARCHIVED     if state.get("playwright_unavailable"):
# ARCHIVED         logger.info("route_after_navigate_explore: routing to human_approval")
# ARCHIVED         return "human_approval"
# ARCHIVED     return "navigation_synthesize"
# ═══ END ARCHIVED ═══


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
            # F18: the sentinel (test_retry_count=FINAL_RETRY_SENTINEL) is set
            # by the human_approval node itself — a path fn may only return a
            # plain node name (Command(update=...) here raised TypeError).
            return "scraper_analyzer"
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

    # coverage_exhausted (validate_coverage gate, after MAX_COVERAGE_RETRIES):
    # "Continue anyway" -> proceed with partial coverage; "Abort"/cancel -> end.
    if reason == "coverage_exhausted":
        if choice in cancel_values:
            logger.info("route_from_human_approval: coverage_exhausted -> abort (END)")
            return "__end__"
        logger.info("route_from_human_approval: coverage_exhausted -> proceed to scraper_analyzer")
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
        # pre_execution node was removed (Wave 2 Cut 2); keep this entry as a
        # safety net so any in-flight job resuming a legacy pre_execution
        # interrupt routes straight to run_execution (the merged behaviour).
        "pre_execution": "run_execution",
        "field_confirmation": "run_execution",
        "playwright_unavailable": "browser_traverse",
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
                    "browser_traverse"
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

    if reason == "playwright_unavailable":
        if "retry" in (label or "").lower() or "playwright" in (label or "").lower():
            logger.info(
                "route_from_human_approval: playwright_unavailable -> retry browser_traverse"
            )
            return "browser_traverse"
        if "probe_html" in (label or "").lower() or "continue" in (label or "").lower():
            logger.info(
                "route_from_human_approval: playwright_unavailable -> proceed with probe_html"
            )
            return "product_analyzer"
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
      field_confirmation, run_execution, route_after_testing,
      route_after_cleanup, human_approval)
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
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # workflow.add_node("navigation_explore", _invoke_navigation_explore)
    # workflow.add_node("navigation_agent", _invoke_navigation_agent)
    # workflow.add_node("navigation_synthesize", _invoke_navigation_synthesize)
    # ═══ END ARCHIVED ═══
    workflow.add_node("browser_traverse", _invoke_navigation_traverse)
    workflow.add_node("product_analyzer", _invoke_product_analyzer)
    workflow.add_node("update_tracker_analysis", update_tracker_analysis)
    workflow.add_node("validate_analysis", validate_analysis)
    workflow.add_node("normalize_fields", normalize_fields)
    workflow.add_node("validate_coverage", validate_coverage)
    # Generation & testing
    workflow.add_node("scraper_analyzer", _decide_strategy)
    workflow.add_node("code_writer", _invoke_code_writer)
    workflow.add_node("code_tester", _invoke_code_tester)
    workflow.add_node("field_confirmation", field_confirmation)
    # (Wave 2 Cut 2) pre_execution_approval node removed — its gate was merged
    # into field_confirmation (item-count now shown there); field_confirmation
    # routes straight to run_execution on approve.
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

    # site_analyzer → conditional (browser_traverse vs update_tracker_analysis)
    workflow.add_conditional_edges(
        "site_analyzer",
        _route_after_site_analyzer,
        {
            "browser_traverse": "browser_traverse",
            "update_tracker_analysis": "update_tracker_analysis",
        },
    )

    # browser_traverse → product_analyzer (replaces the 3-node navigation pipeline).
    workflow.add_edge("browser_traverse", "product_analyzer")
    # ═══ ARCHIVED NAVIGATION (replaced by browser_traverse) ═══
    # # navigation_explore → conditional (human_approval if Playwright down, else navigation_synthesize).
    # # navigate_explore may also return Command(goto="navigation_agent") when it detects a form-driven
    # # site it can't drive deterministically (low product links + form detected) — the LLM navigation_agent
    # # then drives the form with browser tools + skills, and flows into navigation_synthesize.
    # workflow.add_conditional_edges(
    #     "navigation_explore",
    #     _route_after_navigation_explore,
    #     {
    #         "navigation_synthesize": "navigation_synthesize",
    #         "human_approval": "human_approval",
    #     },
    # )
    # workflow.add_edge("navigation_agent", "navigation_synthesize")
    # workflow.add_edge("navigation_synthesize", "product_analyzer")
    # ═══ END ARCHIVED ═══

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

    # code_writer → code_tester (the read-only code_review phase was removed;
    # code_tester validates functionality and route_after_testing handles retries).
    workflow.add_edge("code_writer", "code_tester")

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

    # field_confirmation uses Command-based routing internally (goto is either
    # run_execution on approve, or product_analyzer on reject for re-analysis).
    # No conditional edge needed — the Command decides.
    # (Wave 2 Cut 2: the old pre_execution_approval hop in between was removed.)

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
            "browser_traverse": "browser_traverse",
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


def _route_after_site_analyzer(state: ScrapeState) -> str:
    """Route after site_analyzer based on input_mode.

    - navigation/list_page/search_term → browser_traverse (browser-driven navigation)
    - url_list → update_tracker_analysis (existing product/content analysis flow)
    """
    input_mode = state.get("input_mode", "url_list")
    if input_mode in ("navigation", "list_page", "search_term"):
        logger.info(
            "_route_after_site_analyzer: input_mode=%s → browser_traverse",
            input_mode,
        )
        return "browser_traverse"
    logger.info(
        "_route_after_site_analyzer: input_mode=%s → update_tracker_analysis",
        input_mode,
    )
    return "update_tracker_analysis"


__all__ = ["build_scrape_graph"]
