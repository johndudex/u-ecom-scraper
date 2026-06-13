"""End-to-end test of the full LangGraph scraping pipeline (local, no Docker).

Requires the browser stack (chrome + cdp-proxy + playwright-mcp) running via:
    docker compose up -d    (browser stack is default profile)

Run from project root::

    cd /mnt/d/John/u-ecom-scraper && \\
    TEST_URL="https://www.adidas.ie/" \\
    TEST_SLUG="adidas-ie" \\
    python3 tests/test_e2e_local.py

If TEST_URL is omitted, defaults to accessorize.com (a simpler site).
"""

import json
import logging
import os
import sys
import time
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "webapp"))

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "scraper")
os.environ.setdefault("DB_USER", "scraper")
os.environ.setdefault("DB_PASSWORD", "scraper")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "dev-e2e-test-key")
os.environ.setdefault("DEBUG", "True")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-40s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("e2e_test")

MCP_URL = os.environ.get("PLAYWRIGHT_MCP_URL", "http://localhost:8111/sse")
TEST_URL = os.environ.get("TEST_URL", "https://www.accessorize.com/uk/women/jewellery/shop-all/")
TEST_SLUG = os.environ.get("TEST_SLUG", "accessorize-com")


def setup_django():
    """Bootstrap Django settings without DB or migrate."""
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=True,
            SECRET_KEY="dev-e2e-test-key",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "scraper",
            ],
            USE_TZ=True,
            CELERY_BROKER_URL="redis://localhost:6379/0",
            ZAI_API_KEY=os.environ.get("ZAI_API_KEY", ""),
            ZAI_BASE_URL=os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4/"),
            ZAI_MAIN_MODEL=os.environ.get("ZAI_MAIN_MODEL", "glm-5-turbo"),
            ZAI_SMALL_MODEL=os.environ.get("ZAI_SMALL_MODEL", "glm-5-turbo"),
            PLAYWRIGHT_MCP_URL=MCP_URL,
            PROJECT_ROOT=PROJECT_ROOT,
            SCRAPERS_DIR=os.path.join(PROJECT_ROOT, "scrapers"),
            DATA_DIR=os.path.join(PROJECT_ROOT, "data"),
            SRC_DIR=os.path.join(PROJECT_ROOT, "src"),
            CONFIG_DIR=os.path.join(PROJECT_ROOT, "config"),
            TEMPLATES_DIR=os.path.join(PROJECT_ROOT, "templates"),
            LOGS_DIR=os.path.join(PROJECT_ROOT, "logs"),
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        )
    django.setup()


def mock_session_log():
    """Replace SessionLog.objects with a no-op so graph.py _persist_agent_logs
    doesn't crash on missing tables."""
    try:
        from scraper.models import SessionLog
        from unittest.mock import MagicMock

        if not hasattr(SessionLog, "_e2e_mocked"):
            mock_manager = MagicMock()
            mock_manager.count.return_value = 0
            mock_manager.filter.return_value = mock_manager
            mock_manager.order_by.return_value = mock_manager
            mock_manager.create.return_value = MagicMock()
            SessionLog.objects = mock_manager
            SessionLog._e2e_mocked = True
            logger.info("SessionLog.objects mocked (no-op)")
    except Exception as exc:
        logger.warning("Could not mock SessionLog: %s", exc)


def preflight_check() -> bool:
    """Verify Playwright MCP is reachable before starting the graph.

    Returns True if MCP is available, False if not. Prints diagnostics and
    exits with a helpful message if the browser stack is not running.
    """
    logger.info("Pre-flight: checking Playwright MCP at %s", MCP_URL)

    try:
        import asyncio
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _check():
            async with sse_client(MCP_URL) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return len(result.tools)

        tool_count = asyncio.run(_check())
        logger.info("Pre-flight: Playwright MCP OK — %d tools available", tool_count)
        return True
    except Exception as exc:
        error_type = type(exc).__name__
        error_msg = str(exc)[:200]

        logger.error("Pre-flight: Playwright MCP FAILED — %s: %s", error_type, error_msg)

        if "connection" in error_msg.lower() or "refused" in error_msg.lower():
            print()
            print("=" * 60)
            print("BROWSER STACK NOT RUNNING")
            print("=" * 60)
            print()
            print("The Playwright MCP server is not reachable. Start the browser stack:")
            print()
            print("    cd /mnt/d/John/u-ecom-scraper")
            print("    docker compose up -d")
            print()
            print("This starts 3 containers: chrome, cdp-proxy, playwright-mcp")
            print("Then re-run this test.")
            print()
            print("If containers are running but MCP still fails, check:")
            print("    docker compose ps")
            print("    docker compose logs playwright-mcp --tail 20")
            print("=" * 60)
            print()
            sys.exit(2)

        print()
        print("=" * 60)
        print("PLAYWRIGHT MCP ERROR")
        print("=" * 60)
        print(f"Error: {error_type}: {error_msg}")
        print()
        print("Check:")
        print(f"    1. MCP URL correct: {MCP_URL}")
        print("    2. Browser containers healthy: docker compose ps")
        print("    3. MCP logs: docker compose logs playwright-mcp --tail 20")
        print("=" * 60)
        print()
        sys.exit(3)

    return False


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
        "choose_mechanism": "Proceed",
    }

    if reason in approval_map:
        choice = approval_map[reason]
    elif options:
        choice = options[0]
    else:
        choice = "Continue"

    logger.info("AUTO-APPROVE [%s] → choice='%s'", reason, choice)
    return {"choice": choice}


def _extract_interrupt_value(node_output: dict) -> dict:
    """Extract the interrupt value from a __interrupt__ stream event."""
    if not isinstance(node_output, dict):
        return {}

    if "messages" in node_output:
        msgs = node_output["messages"]
        for m in msgs:
            if hasattr(m, "content") and isinstance(m.content, list):
                for item in m.content:
                    if hasattr(item, "text") and isinstance(item.text, str):
                        try:
                            parsed = json.loads(item.text)
                            if isinstance(parsed, dict):
                                return parsed
                        except (json.JSONDecodeError, TypeError):
                            pass
            if hasattr(m, "content") and isinstance(m.content, dict):
                return m.content

    if "interrupt_reason" in node_output:
        return {
            "reason": node_output.get("interrupt_reason", ""),
            "options": node_output.get("interrupt_options", []),
        }

    return {"choice": "Continue anyway"}


def ensure_workspace(slug):
    ws = os.path.join(PROJECT_ROOT, "workspace", slug)
    os.makedirs(ws, exist_ok=True)
    return ws


def main():
    setup_django()
    mock_session_log()
    preflight_check()

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    from agents.graph import build_scrape_graph

    checkpointer = MemorySaver()
    graph = build_scrape_graph(checkpointer=checkpointer)

    thread_id = f"e2e-test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

    ensure_workspace(TEST_SLUG)

    initial_input = {
        "url": TEST_URL,
        "sample_only": True,
    }

    logger.info("=" * 70)
    logger.info("E2E TEST (local)")
    logger.info("  URL:  %s", TEST_URL)
    logger.info("  SLUG: %s", TEST_SLUG)
    logger.info("  MCP:  %s", MCP_URL)
    logger.info("  THREAD: %s", thread_id)
    logger.info("=" * 70)

    nodes_hit = []
    start = time.time()
    max_resumes = 30
    interrupt_count = 0
    fatal_error = None

    input_data = initial_input

    for attempt in range(max_resumes + 1):
        logger.info("-" * 60)
        logger.info(
            "ATTEMPT #%d (input: %s)",
            attempt + 1,
            list(input_data.keys()) if isinstance(input_data, dict) else type(input_data).__name__,
        )
        try:
            for event in graph.stream(input_data, config=config, stream_mode="updates"):
                for node_name, node_output in event.items():
                    t = time.time() - start
                    nodes_hit.append(node_name)

                    if node_name == "__interrupt__":
                        logger.info("[%7.1fs] INTERRUPT detected in stream", t)
                        interrupt_count += 1
                        interrupt_value = _extract_interrupt_value(node_output)
                        reason = interrupt_value.get("reason", "?") if isinstance(interrupt_value, dict) else "?"
                        logger.info("INTERRUPT #%d: reason=%s", interrupt_count, reason)
                        resume_value = auto_approve(interrupt_value)
                        input_data = Command(resume=resume_value)
                        continue

                    msg_keys = list(node_output.keys()) if isinstance(node_output, dict) else []
                    goto = node_output.get("goto", "") if isinstance(node_output, dict) else ""

                    if node_name in (
                        "site_analyzer", "product_analyzer", "code_writer",
                        "code_tester", "cleanup", "skill_learner",
                    ):
                        msgs = node_output.get("messages", [])
                        ai_msgs = [m for m in msgs if getattr(m, "type", None) == "ai"]
                        tool_msgs = [m for m in msgs if getattr(m, "type", None) == "tool"]
                        logger.info(
                            "[%7.1fs] NODE: %-25s ai=%d tool=%d goto=%s",
                            t, node_name, len(ai_msgs), len(tool_msgs), goto,
                        )
                    else:
                        logger.info("[%7.1fs] NODE: %-25s keys=%-30s goto=%s", t, node_name, str(msg_keys)[:30], goto)

            has_interrupt = any(n == "__interrupt__" for n in nodes_hit)
            if has_interrupt:
                logger.info("[%7.1fs] INTERRUPT in stream, resuming...", time.time() - start)
                continue

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
                logger.info("INTERRUPT #%d: reason=%s value=%s", interrupt_count, reason, str(interrupt_value)[:300])
                resume_value = auto_approve(interrupt_value)
                input_data = Command(resume=resume_value)
                continue

            fatal_error = exc
            logger.error("Fatal exception: %s: %s", exc_name, exc)
            break

    elapsed = time.time() - start

    logger.info("=" * 70)
    logger.info("RESULTS")
    logger.info("=" * 70)
    logger.info("Total time: %.1fs", elapsed)
    logger.info("Interrupts handled: %d", interrupt_count)
    if fatal_error:
        logger.error("Fatal error: %s: %s", type(fatal_error).__name__, fatal_error)
    logger.info("Nodes visited (in order):")
    for i, name in enumerate(nodes_hit):
        marker = " *" if name in ("site_analyzer", "product_analyzer", "code_writer", "code_tester", "cleanup", "skill_learner") else ""
        logger.info("  %2d. %s%s", i + 1, name, marker)

    agents_expected = {"site_analyzer", "product_analyzer", "code_writer", "code_tester", "cleanup"}
    agents_hit = set(nodes_hit) & agents_expected
    logger.info("Agents invoked: %s", agents_hit if agents_hit else "NONE")
    missing = agents_expected - agents_hit
    if missing:
        logger.error("MISSING AGENTS: %s", missing)
    else:
        logger.info("ALL 5 CORE AGENTS INVOKED SUCCESSFULLY")

    skill_hit = "skill_learner" in set(nodes_hit)
    logger.info("Skill learner: %s", "invoked" if skill_hit else "skipped")

    logger.info("=" * 70)

    if agents_hit == agents_expected:
        logger.info("E2E TEST PASSED")
    else:
        logger.error("E2E TEST FAILED - missing agents: %s", missing)

    return len(missing) == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
