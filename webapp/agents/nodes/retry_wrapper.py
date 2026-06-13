"""Retry wrapper for LLM agent nodes.

[C1] Wraps agent invocation with artifact-existence checks and automatic
retry (up to *max_retries* attempts).  On exhausted retries, routes to
``human_approval`` for guidance.

Usage::

    node = create_agent_with_retry(
        agent_name="site_analyzer",
        agent_factory=create_site_analyzer,
        artifact_key="site_analysis",
        artifact_path_fn=lambda s: f"workspace/{s['site_slug']}/site_analysis.json",
        state=current_state,
    )
    if node.next_node == "site_analyzer":
        ...  # retry
    elif node.next_node == "__end__":
        ...  # exhausted
    else:
        ...  # success
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


@dataclass
class AgentResult:
    """Outcome of an agent invocation with retry logic."""

    state_update: dict[str, Any]
    next_node: str
    retry_count: int
    error_message: str = ""


def create_agent_with_retry(
    agent_name: str,
    agent_factory: Callable,
    artifact_key: str,
    artifact_path_fn: Callable[[dict], str],
    state: dict,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> AgentResult:
    """Invoke an agent and check for expected artifact production.

    Args:
        agent_name: Identifier used for logging and state keys.
        agent_factory: Zero-arg callable that returns a compiled LangGraph
            agent graph or function.
        artifact_key: State key where the parsed artifact should be stored
            (e.g. ``\"site_analysis\"``).
        artifact_path_fn: Function taking the current state and returning
            the filesystem path where the artifact should appear.
        state: Current graph state.
        max_retries: Maximum retry attempts before escalating to human.
    """
    retry_key = f"{agent_name}_retries"
    retry_count = state.get(retry_key, 0)
    root = _get_project_root()
    expected_path = os.path.join(root, artifact_path_fn(state))

    agent = agent_factory()

    try:
        if hasattr(agent, "invoke"):
            agent.invoke(state)
        else:
            agent(state)
    except Exception as exc:
        logger.error("create_agent_with_retry[%s]: agent raised %s", agent_name, exc)
        retry_count += 1
        if retry_count >= max_retries:
            return AgentResult(
                state_update={
                    retry_key: retry_count,
                    "error_message": f"{agent_name} crashed: {exc}",
                },
                next_node="human_approval",
                retry_count=retry_count,
            )
        return AgentResult(
            state_update={retry_key: retry_count},
            next_node=agent_name,
            retry_count=retry_count,
        )

    if os.path.isfile(expected_path):
        logger.info("create_agent_with_retry[%s]: artifact produced at %s", agent_name, expected_path)
        try:
            with open(expected_path, "r", encoding="utf-8") as fh:
                artifact_data = json.load(fh)
        except Exception as exc:
            logger.warning("create_agent_with_retry[%s]: artifact not valid JSON: %s", agent_name, exc)
            artifact_data = {}

        return AgentResult(
            state_update={
                artifact_key: artifact_data,
                retry_key: 0,
                "error_message": "",
            },
            next_node=_route_after_agent(agent_name),
            retry_count=0,
        )

    logger.warning(
        "create_agent_with_retry[%s]: artifact not produced (attempt %d/%d)",
        agent_name,
        retry_count + 1,
        max_retries,
    )
    retry_count += 1
    if retry_count >= max_retries:
        return AgentResult(
            state_update={
                retry_key: retry_count,
                "error_message": f"{agent_name} did not produce {expected_path} after {max_retries} attempts",
            },
            next_node="human_approval",
            retry_count=retry_count,
        )

    return AgentResult(
        state_update={retry_key: retry_count},
        next_node=agent_name,
        retry_count=retry_count,
    )


def _route_after_agent(agent_name: str) -> str:
    routing = {
        "site_analyzer": "validate_analysis",
        "product_analyzer": "validate_coverage",
        "scraper_analyzer": "code_writer",
        "code_writer": "code_tester",
        "code_tester": "route_after_testing",
        "cleanup": "route_after_cleanup",
        "skill_learner": "__end__",
    }
    return routing.get(agent_name, "human_approval")
