"""Tool factories for LangGraph agent nodes.

Each agent receives only the tools it needs — no permission system required
because tools are pre-assigned at graph-assembly time (see migration plan gap D6).

Usage::

    from agents.tools import get_tools_for_agent
    tools = get_tools_for_agent("site_analyzer")
"""

import logging
from typing import Optional

from agents.tools.filesystem_tools import get_filesystem_tools as _get_fs_tools
from agents.tools.probe_tools import get_probe_tools as _get_probe_tools
from agents.tools.shell_tools import get_shell_tools as _get_bash_tools
from agents.tools.skill_tools import get_skill_tools as _get_skill_tools
from agents.tools.web_tools import get_web_tools as _get_web_tools

logger = logging.getLogger(__name__)

AGENT_TOOL_MAP: dict[str, list[str]] = {
    "site_analyzer": ["playwright", "web", "write_file", "read_file", "probe"],
    "product_analyzer": ["playwright", "web", "write_file", "read_file", "probe"],
    "scraper_analyzer": ["playwright", "web", "write_file", "read_file", "run_bash", "probe", "load_skill", "list_skills"],
    "code_writer": ["read_file", "write_file", "edit_file", "search_files", "search_content", "load_skill", "list_skills"],
    "code_tester": ["read_file", "write_file", "edit_file", "run_bash", "run_scraper", "web", "load_skill", "list_skills"],
    "cleanup": ["read_file", "write_file", "run_bash", "search_files"],
    "skill_learner": ["read_file", "write_file", "edit_file", "search_files", "load_skill", "list_skills"],
}

ALLOWED_PLAYWRIGHT_TOOLS: dict[str, list[str]] = {
    "site_analyzer": [
        "playwright_browser_navigate",
        "playwright_browser_snapshot",
        "playwright_browser_evaluate",
        "playwright_browser_click",
        "playwright_browser_network_requests",
        "playwright_browser_network_request",
    ],
    "product_analyzer": [
        "playwright_browser_navigate",
        "playwright_browser_snapshot",
        "playwright_browser_evaluate",
        "playwright_browser_click",
        "playwright_browser_network_requests",
        "playwright_browser_network_request",
        "playwright_browser_wait_for",
        "playwright_browser_tabs",
    ],
    "scraper_analyzer": [
        "playwright_browser_navigate",
        "playwright_browser_snapshot",
        "playwright_browser_evaluate",
        "playwright_browser_click",
        "playwright_browser_network_requests",
    ],
    "code_tester": [
        "playwright_browser_navigate",
        "playwright_browser_snapshot",
    ],
}


async def get_playwright_tools() -> list:
    """Connect to the Playwright MCP server and return LangChain-compatible tools.

    Returns an empty list if the MCP server is unreachable — callers must
    check the tool count and skip web_fetch for browser-required agents.
    """
    from agents.tools.playwright_tools import create_playwright_tools

    return await create_playwright_tools()


async def get_tools_for_agent(
    agent_name: str,
    project_root: Optional[str] = None,
) -> list:
    """Return the exact set of tools a given agent is allowed to use.

    This is the primary entry point called during graph assembly.  Tool sets
    are hard-coded per agent so that no runtime permission checks are needed.

    Args:
        agent_name: Key from AGENT_TOOL_MAP (e.g. ``"site_analyzer"``).
        project_root: Root directory for filesystem/shell sandboxing.

    Returns:
        List of LangChain BaseTool instances ready for ``create_react_agent``.
    """
    requested = AGENT_TOOL_MAP.get(agent_name, [])
    tools: list = []

    needs_playwright = "playwright" in requested
    needs_web = "web" in requested
    needs_probe = "probe" in requested
    fs_tool_names = {"read_file", "write_file", "edit_file", "search_files", "search_content"}
    needs_fs = bool(fs_tool_names & set(requested))
    needs_bash = "run_bash" in requested
    needs_skill = "load_skill" in requested or "list_skills" in requested

    if needs_playwright:
        pw_tools = await get_playwright_tools()
        if pw_tools:
            tools.extend(pw_tools)
        else:
            logger.warning(
                "Playwright MCP unavailable for '%s'. probe_page can still access pages.",
                agent_name,
            )

    if needs_probe:
        tools.extend(_get_probe_tools())

    if needs_web:
        tools.extend(_get_web_tools())

    if needs_fs:
        tools.extend(_get_fs_tools(project_root=project_root))

    if needs_bash:
        tools.extend(_get_bash_tools(project_root=project_root))

    if needs_skill:
        tools.extend(_get_skill_tools())

    logger.info(
        "Tools for agent '%s': %s",
        agent_name,
        [t.name for t in tools],
    )
    return tools
