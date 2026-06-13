"""End-to-end test of the full LangGraph scraping pipeline.

Invokes ``build_scrape_graph`` with a real site, streams events, and
auto-resumes through all human-in-the-loop interrupts so that every
agent subgraph is exercised.

Run inside the Django container::

    cd /app && PLAYWRIGHT_MCP_URL=http://playwright-mcp:8111/sse \
        python3 webapp/tests/test_e2e_graph.py
"""

import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, "/app/webapp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-30s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("e2e_test")

MCP_URL = os.environ.get(
    "PLAYWRIGHT_MCP_URL",
    "http://playwright-mcp:8111/sse",
)

TEST_URL = os.environ.get(
    "TEST_URL",
    "https://www.accessorize.com/uk/women/jewellery/shop-all/",
)
TEST_SLUG = os.environ.get("TEST_SLUG", "accessorize-com")


def auto_approve(interrupt_value):
    """Auto-approve all interrupt points for testing."""
    if not isinstance(interrupt_value, dict):
        return {"choice": "Continue anyway"}

    reason = interrupt_value.get("reason", "")
    options = interrupt_value.get("options", [])

    approval_map = {
        "re_scrape": "Yes, re-scrape",
        "retry_failed": "Yes, retry",
        "low_confidence": "Continue anyway",
        "low_coverage": "Continue anyway",
        "validation_failed": "Continue anyway",
        "field_confirmation": "Approve",
        "pre_execution": "Proceed",
        "skill_approval": "Skip",
        "reanalyze_exhausted": "Continue anyway",
    }

    if reason in approval_map:
        choice = approval_map[reason]
    elif options:
        choice = options[0]
    else:
        choice = "Continue"

    logger.info("AUTO-APPROVE [%s] → choice='%s'", reason, choice)
    return {"choice": choice}


def main():
    from langgraph.types import Command
    from agents.graph import build_scrape_graph

    graph = build_scrape_graph(checkpointer=None)

    thread_id = f"e2e-test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    initial_input = {
        "url": TEST_URL,
        "sample_only": True,
    }

    logger.info("=" * 60)
    logger.info("E2E TEST: url=%s slug=%s thread=%s", TEST_URL, TEST_SLUG, thread_id)
    logger.info("=" * 60)

    nodes_hit = []
    start = time.time()
    max_resumes = 20
    interrupt_count = 0

    input_data = initial_input

    for attempt in range(max_resumes + 1):
        logger.info("-" * 50)
        logger.info("ATTEMPT #%d (input keys: %s)", attempt + 1, list(input_data.keys()) if isinstance(input_data, dict) else type(input_data).__name__)

        try:
            for event in graph.stream(input_data, config=config, stream_mode="updates"):
                for node_name, node_output in event.items():
                    t = time.time() - start
                    nodes_hit.append(node_name)

                    msg_keys = list(node_output.keys()) if isinstance(node_output, dict) else []
                    goto = node_output.get("goto", "") if isinstance(node_output, dict) else ""
                    logger.info("[%7.1fs] NODE: %-25s keys=%-30s goto=%s", t, node_name, str(msg_keys)[:30], goto)

                    if node_name in ("site_analyzer", "product_analyzer", "code_writer", "code_tester", "cleanup", "skill_learner"):
                        msgs = node_output.get("messages", [])
                        tool_calls = sum(1 for m in msgs if getattr(m, "type", None) == "tool")
                        logger.info("  → Agent messages: %d, tool_calls: %d", len(msgs), tool_calls)

            logger.info("[%7.1fs] STREAM COMPLETE on attempt #%d", time.time() - start, attempt + 1)
            break

        except Exception as exc:
            exc_name = type(exc).__name__
            logger.warning("[%7.1fs] EXCEPTION: %s: %s", time.time() - start, exc_name, exc)

            if exc_name == "GraphInterrupt":
                interrupt_count += 1
                interrupt_value = exc.args[0] if exc.args else None
                reason = "?"
                if isinstance(interrupt_value, dict):
                    reason = interrupt_value.get("reason", "?")
                logger.info("INTERRUPT #%d: reason=%s value=%s", interrupt_count, reason, str(interrupt_value)[:200])

                resume_value = auto_approve(interrupt_value)
                input_data = Command(resume=resume_value)
                continue

            logger.error("Fatal exception: %s: %s", exc_name, exc)
            break

    elapsed = time.time() - start

    logger.info("=" * 60)
    logger.info("RESULTS")
    logger.info("=" * 60)
    logger.info("Total time: %.1fs", elapsed)
    logger.info("Interrupts handled: %d", interrupt_count)
    logger.info("Nodes visited (in order):")
    for i, name in enumerate(nodes_hit):
        logger.info("  %2d. %s", i + 1, name)

    agents_expected = {"site_analyzer", "product_analyzer", "code_writer", "code_tester", "cleanup"}
    agents_hit = set(nodes_hit) & agents_expected
    logger.info("Agents invoked: %s", agents_hit if agents_hit else "NONE")
    missing = agents_expected - agents_hit
    if missing:
        logger.error("MISSING AGENTS: %s", missing)
    else:
        logger.info("ALL 5 CORE AGENTS INVOKED SUCCESSFULLY")

    skill_hit = "skill_learner" in set(nodes_hit)
    logger.info("Skill learner: %s", "invoked" if skill_hit else "skipped (expected for sample)")

    logger.info("=" * 60)
    if agents_hit == agents_expected:
        logger.info("E2E TEST PASSED")
    else:
        logger.error("E2E TEST FAILED - missing agents: %s", missing)


if __name__ == "__main__":
    main()
