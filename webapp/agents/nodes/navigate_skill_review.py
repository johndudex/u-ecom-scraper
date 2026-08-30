"""Navigation skill review node — LLM agent that compares raw navigation
findings against existing skills and auto-applies reusable learnings.

Runs **after** ``navigation_synthesize`` and **before** ``scraper_analyzer``.
This node is **non-blocking**: if it fails or times out, the pipeline
continues to ``scraper_analyzer`` without skill updates.

The agent reads ``navigation_findings.json``, ``site_analysis.json``, and
``navigation_analysis.json``, loads existing skills for comparison, then
appends ``## Learned:`` sections to skills and writes
``nav_learning_report.json``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from django.conf import settings

from agents.graph import (
    _agent_config,
    _invoke_agent_with_timeout,
    _log_agent_context,
    _persist_agent_logs,
    _start_heartbeat,
    _stop_heartbeat,
)

logger = logging.getLogger(__name__)

NAV_SKILL_REVIEW_BUDGET = 15


def navigate_skill_review(state: dict, config=None) -> dict[str, Any]:
    """LLM skill-review node — reads findings, compares to skills, applies.

    Non-blocking: any failure is logged and an empty dict returned so the
    graph proceeds to ``scraper_analyzer``.
    """
    job_id = state.get("job_id", 0)
    slug = state.get("site_slug", "")

    logger.info(
        "navigate_skill_review: starting (job %s, slug=%s)", job_id, slug
    )

    root = getattr(settings, "PROJECT_ROOT", os.getcwd())

    # [QW-2] Known-site re-run marker: skip_site_analysis means this job is a
    # RE-scrape of an already-analyzed site. The navigation skills were already
    # reviewed on the first run (learned sections persist in the skill files),
    # so an LLM re-review re-derives the same sections every re-scrape —
    # minutes of tail latency for zero new knowledge. NEVER gate fresh sites:
    # first-run learning is the point of this node.
    if state.get("skip_site_analysis"):
        logger.info(
            "navigate_skill_review: known-site re-run (skip_site_analysis) — "
            "skills already reviewed on the first run, skipping (job %s)",
            job_id,
        )
        return {}

    # Bail out early if there are no navigation findings to review. This
    # node only makes sense after a successful navigation_explore run.
    findings_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
    if not os.path.isfile(findings_path):
        logger.info(
            "navigate_skill_review: navigation_findings.json not found at %s "
            "— skipping skill review (job %s)",
            findings_path,
            job_id,
        )
        return {}

    try:
        from agents.subagents import (
            build_nav_skill_review_message,
            create_nav_skill_review,
        )

        messages = build_nav_skill_review_message(state)
        _log_agent_context(state, "nav-skill-review", messages)
        agent = create_nav_skill_review(site_slug=slug)

        agent_cfg: dict = {}
        if config:
            agent_cfg.update(config)

        # [QW-2] wall-clock cap + recursion cap (underscore key so
        # AGENT_RECURSION_MAP["nav_skill_review"] applies) + heartbeat — this
        # was a raw un-walled invoke; a hung LLM call here stalled the whole
        # tail of the job. Timeout returns a dead-result dict, which the
        # report check below treats exactly like "agent wrote nothing" —
        # the node stays non-blocking.
        hb = _start_heartbeat(job_id, "nav-skill-review")
        try:
            result = _invoke_agent_with_timeout(
                agent, messages, _agent_config(config, "nav_skill_review"),
                "nav_skill_review", job_id,
            )
        except Exception as exc:
            logger.warning(
                "navigate_skill_review: agent invocation error (job %s): %s — "
                "continuing pipeline without skill updates",
                job_id,
                str(exc)[:200],
            )
            return {}
        finally:
            _stop_heartbeat(hb)

        _persist_agent_logs(state, result, "nav-skill-review", agent_cfg)

        # Read the report the agent was supposed to write
        report_path = os.path.join(
            root, "workspace", slug, "nav_learning_report.json"
        )
        if os.path.isfile(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                logger.info(
                    "navigate_skill_review: success — nav_learning_report.json "
                    "written (job %s, skills_updated=%s)",
                    job_id,
                    len(report.get("skills_updated", [])),
                )
                return {
                    "nav_learning_report": report,
                    "messages": [],
                }
            except json.JSONDecodeError as exc:
                logger.warning(
                    "navigate_skill_review: report written but invalid JSON: %s",
                    exc,
                )
        else:
            logger.info(
                "navigate_skill_review: agent did not write nav_learning_report.json "
                "(job %s) — pipeline continues without skill updates",
                job_id,
            )

        return {"messages": []}

    except Exception as exc:
        logger.exception(
            "navigate_skill_review: failed (job %s): %s — non-blocking, continuing",
            job_id,
            exc,
        )
        return {}
