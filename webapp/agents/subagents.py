"""Agent subgraph factories for the LangGraph scraping workflow.

Each agent in the scraping pipeline is instantiated as a ``create_react_agent``
from ``langgraph.prebuilt``.  These factory functions centralize the
configuration (system prompt, LLM temperature, tool set) for every agent so
that the main graph assembly in ``graph.py`` stays declarative.

Temperature values are drawn from the original OpenCode agent definitions in
``opencode.json`` / ``.opencode/agents/*.md`` frontmatter.

Usage (from graph.py)::

    from webapp.agents.subagents import create_site_analyzer
    agent_subgraph = create_site_analyzer()
    workflow.add_node("site_analyzer", agent_subgraph)
"""

from __future__ import annotations

import logging
import os

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

from .llm import get_main_llm
from .prompts import load_agent_prompt

logger = logging.getLogger(__name__)

# ── Temperature mapping from .opencode/agents/*.md frontmatter ──────────────

AGENT_TEMPERATURES: dict[str, float] = {
    "site-analyzer": 0.2,
    "product-analyzer": 0.2,
    "scraper-analyzer": 0.2,
    "code-writer": 0.4,
    "code-tester": 0.1,
    "cleanup": 0.1,
    "skill-learner": 0.3,
}

# ── Internal name mapping: agent node name → prompt file stem ─────────────

AGENT_PROMPT_MAP: dict[str, str] = {
    "site_analyzer": "site-analyzer",
    "product_analyzer": "product-analyzer",
    "scraper_analyzer": "scraper-analyzer",
    "code_writer": "code-writer",
    "code_tester": "code-tester",
    "cleanup": "cleanup",
    "skill_learner": "skill-learner",
}


AGENT_MAX_ITERATIONS: dict[str, int] = {
    "site_analyzer": 30,
    "product_analyzer": 30,
    "scraper_analyzer": 30,
    "code_writer": 20,
    "code_tester": 20,
    "cleanup": 15,
    "skill_learner": 15,
}

BROWSER_UNAVAILABLE_WARNING = (
    "\n\n"
    "NOTE: Playwright MCP browser tools are unavailable. "
    "Use probe_page to access the page — it handles proxy escalation "
    "automatically. If probe_page also fails, write analysis based on "
    "URL structure and any existing workspace artifacts."
)


def _get_skill_descriptions() -> str:
    """Scan the .opencode/skills/ tree and return a bullet list of skill names
    and their descriptions (first line of the SKILL.md after the frontmatter).

    This is the *progressive disclosure* layer: agents see lightweight
    descriptions in their system prompt and can call ``load_skill`` to get
    the full content on demand.
    """
    skills_dir = _resolve_skills_dir()
    if not os.path.isdir(skills_dir):
        return ""

    lines: list[str] = []
    for name in sorted(os.listdir(skills_dir)):
        sk_md = os.path.join(skills_dir, name, "SKILL.md")
        if not os.path.isfile(sk_md):
            continue
        try:
            text = open(sk_md, encoding="utf-8").read()
        except Exception:
            continue
        description = _extract_frontmatter_field(text, "description") or name
        lines.append(f"- **{name}**: {description}")

    if not lines:
        return ""

    return (
        "\n\n## Available Skills\n\n"
        "You have access to specialized scraping skills. Use `load_skill` "
        "to load full instructions when relevant.\n\n"
        + "\n".join(lines)
        + "\n\n**IMPORTANT:** When you detect a platform matching a skill "
        "(e.g. Shopify, SFCC, Algolia, Amazon, Kibo, Localised), load it "
        "with `load_skill` for proven detection and extraction methods.\n"
    )


def _resolve_skills_dir() -> str:
    """Return the absolute path to .opencode/skills/."""
    try:
        from django.conf import settings

        root = getattr(settings, "PROJECT_ROOT", None)
        if root:
            return os.path.join(str(root), ".opencode", "skills")
    except Exception:
        pass
    return os.path.join(os.getcwd(), ".opencode", "skills")


def _extract_frontmatter_field(text: str, field: str) -> str | None:
    """Extract a field value from YAML frontmatter (``---`` delimited block)."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    block = text[3:end]
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{field}:"):
            return stripped[len(field) + 1 :].strip().strip("\"'")
    return None


def _append_skill_descriptions(system_prompt: str) -> str:
    """Append skill discovery section to the agent system prompt."""
    skill_section = _get_skill_descriptions()
    if not skill_section:
        return system_prompt
    return system_prompt + skill_section


# ── Factory functions ────────────────────────────────────────────────────


def create_site_analyzer(site_slug: str = "") -> object:
    return _build_agent("site_analyzer", site_slug=site_slug)


def create_product_analyzer(site_slug: str = "") -> object:
    return _build_agent("product_analyzer", site_slug=site_slug)


def create_scraper_analyzer(site_slug: str = "") -> object:
    return _build_agent("scraper_analyzer", site_slug=site_slug)


def create_code_writer(site_slug: str = "") -> object:
    return _build_agent("code_writer", site_slug=site_slug)


def create_code_tester(site_slug: str = "") -> object:
    return _build_agent("code_tester", site_slug=site_slug)


def create_cleanup_agent(site_slug: str = "") -> object:
    return _build_agent("cleanup", site_slug=site_slug)


def create_skill_learner(site_slug: str = "") -> object:
    return _build_agent("skill_learner", site_slug=site_slug)


# ── Shared builder ────────────────────────────────────────────────────────


def _build_agent(agent_name: str, site_slug: str = "") -> object:
    prompt_stem = AGENT_PROMPT_MAP[agent_name]
    temperature = AGENT_TEMPERATURES[prompt_stem]

    try:
        system_prompt = load_agent_prompt(prompt_stem)
    except FileNotFoundError:
        logger.warning("Agent prompt not found for '%s', using fallback", prompt_stem)
        system_prompt = (
            f"You are the {prompt_stem} agent for the Universal Ecommerce Scraper."
        )

    system_prompt = _append_skill_descriptions(system_prompt)

    tools = _get_tools_sync(agent_name, workspace_scope=site_slug)

    if not _has_playwright_tools(tools):
        from .tools import AGENT_TOOL_MAP as _atm

        if "playwright" in _atm.get(agent_name, []):
            logger.warning(
                "Playwright MCP unavailable for '%s'. probe_page will handle page access.",
                agent_name,
            )
            system_prompt += BROWSER_UNAVAILABLE_WARNING

    llm = get_main_llm(temperature)

    logger.info(
        "Creating agent '%s' (temp=%.1f, prompt_stem=%s, tools=%d)",
        agent_name,
        temperature,
        prompt_stem,
        len(tools),
    )

    agent = create_react_agent(llm, tools=tools, prompt=system_prompt)
    return agent


def _has_playwright_tools(tools: list) -> bool:
    """Check if the tool list contains any Playwright browser tools."""
    return any(t.name.startswith("playwright_") for t in tools)


def _get_tools_sync(agent_name: str, workspace_scope: str = "") -> list:
    from .tools import AGENT_TOOL_MAP, ALLOWED_PLAYWRIGHT_TOOLS
    from .tools.playwright_tools import get_playwright_status

    requested = AGENT_TOOL_MAP.get(agent_name, [])
    tools: list = []

    needs_playwright = "playwright" in requested
    needs_web = "web" in requested
    needs_probe = "probe" in requested
    fs_tool_names = {
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "search_content",
    }
    needs_fs = bool(fs_tool_names & set(requested))
    needs_bash = "run_bash" in requested
    needs_skill = "load_skill" in requested or "list_skills" in requested

    if needs_playwright:
        try:
            from .tools.playwright_tools import create_playwright_tools_sync

            all_pw_tools = create_playwright_tools_sync()
            allowed = set(ALLOWED_PLAYWRIGHT_TOOLS.get(agent_name, []))
            if allowed:
                filtered = [t for t in all_pw_tools if t.name in allowed]
                if not filtered and all_pw_tools:
                    logger.warning(
                        "No allowed Playwright tools matched for '%s' "
                        "(allowed=%s, got=%s). Using all tools.",
                        agent_name,
                        allowed,
                        [t.name for t in all_pw_tools],
                    )
                    tools.extend(all_pw_tools)
                else:
                    tools.extend(filtered)
            else:
                tools.extend(all_pw_tools)

            if not all_pw_tools:
                status = get_playwright_status()
                logger.warning(
                    "Playwright MCP unavailable for '%s' (error=%s). "
                    "probe_page can still access pages.",
                    agent_name,
                    status.get("error", "unknown"),
                )
        except Exception as exc:
            logger.error("Failed to load Playwright tools: %s", exc)

    if needs_probe:
        try:
            from .tools.probe_tools import get_probe_tools as _gpt

            tools.extend(_gpt())
        except Exception as exc:
            logger.error("Failed to load probe tools for '%s': %s", agent_name, exc)

    if needs_web:
        try:
            from .tools.web_tools import get_web_tools as _gwt

            tools.extend(_gwt())
        except Exception as exc:
            logger.error("Failed to load web tools for '%s': %s", agent_name, exc)

    if needs_fs:
        try:
            from .tools.filesystem_tools import get_filesystem_tools as _gft

            tools.extend(_gft(workspace_scope=workspace_scope or None))
        except Exception as exc:
            logger.error(
                "Failed to load filesystem tools for '%s': %s", agent_name, exc
            )

    if needs_bash:
        try:
            from .tools.shell_tools import get_shell_tools as _gst

            tools.extend(_gst())
        except Exception as exc:
            logger.error("Failed to load shell tools for '%s': %s", agent_name, exc)

    if needs_skill:
        try:
            from .tools.skill_tools import get_skill_tools as _gsk

            tools.extend(_gsk())
        except Exception as exc:
            logger.error("Failed to load skill tools for '%s': %s", agent_name, exc)

    logger.info(
        "Tools for agent '%s': %s",
        agent_name,
        [t.name for t in tools],
    )

    tools = _apply_guards(tools, agent_name)

    return tools


def _apply_guards(tools: list, agent_name: str) -> list:
    from .tools.guards import (
        apply_guard,
        require_non_akamai_tool,
        require_non_blocked_domain,
        require_target_url,
    )

    guarded_agents = {"site_analyzer", "product_analyzer", "scraper_analyzer"}
    url_locked_agents = {"site_analyzer", "product_analyzer"}

    if agent_name not in guarded_agents:
        return tools

    for i, t in enumerate(tools):
        name = getattr(t, "name", "")

        if name.startswith("playwright_browser_"):
            if agent_name in guarded_agents:
                t = apply_guard(t, require_non_akamai_tool)
            if agent_name in url_locked_agents and "navigate" in name:
                t = apply_guard(t, require_target_url)
            tools[i] = t

        elif name == "web_fetch":
            if agent_name in guarded_agents:
                t = apply_guard(t, require_non_akamai_tool)
                t = apply_guard(t, require_non_blocked_domain)
            tools[i] = t

    logger.info(
        "Guards applied for '%s': non_akamai=%s, target_url=%s",
        agent_name,
        agent_name in guarded_agents,
        agent_name in url_locked_agents,
    )
    return tools


# ── Message builders ──────────────────────────────────────────────────────


def build_site_analyzer_message(state: dict) -> list:
    """Build the initial HumanMessage for the site-analyzer agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or "auto-discover"
    currency = state.get("currency") or "auto-detect"

    cached_probe = ""
    probe_result = state.get("probe_result")
    has_verified_probe = False
    if probe_result and probe_result.get("connectivity"):
        conn = probe_result["connectivity"]
        verified = probe_result.get("captcha_verified", False)
        has_verified_probe = True
        cached_probe = (
            f"\n### Pre-verified Probe Result (from accessibility check)\n"
            f"The page has ALREADY been probed{' and verified as captcha-free' if verified else ''}. "
            f"Do NOT call probe_page again — use this data directly:\n"
            f"```\n"
            f"method_that_worked: {conn.get('method_that_worked', 'unknown')}\n"
            f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
            f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
            f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
            f"```\n"
            f"**IMPORTANT**: The connectivity method above is the ONLY one that bypasses "
            f"captcha/anti-bot. Use it in your site_analysis.json connectivity section.\n"
        )

    if has_verified_probe:
        access_strategy = (
            f"### Page Access Strategy\n\n"
            f"The page has already been probed (see Pre-verified Probe Result above). "
            f"**Do NOT call probe_page** — it would waste a tool call and return the same cached data.\n\n"
            f"Use `playwright_browser_*` tools directly if you need deeper analysis "
            f"(network requests, cookies, DOM inspection). Otherwise, proceed directly to "
            f"writing site_analysis.json with the connectivity data from the pre-verified probe.\n\n"
        )
        call_allocation = (
            f"### Call Allocation (target: 3-5 calls)\n"
            f"1. Optional: playwright_browser_* for deeper analysis (0-2 calls)\n"
            f"2. write_file to save analysis (1 call)\n\n"
        )
    else:
        access_strategy = (
            f"### Page Access Strategy\n\n"
            f"Use `probe_page` as your FIRST tool call. It automatically tries "
            f"direct HTTP → browser (no proxy) → browser (datacenter proxy) → "
            f"browser (residential proxy) and returns what worked.\n\n"
            f"```\n"
            f'probe_page(url="{product_url}")\n'
            f"```\n\n"
            f"From the probe result, extract:\n"
            f"- Platform clues (from JSON-LD, HTML structure, meta tags)\n"
            f"- Anti-bot status (probe reports if blocked)\n"
            f"- JSON-LD structured data availability\n"
            f"- Which connection method worked (direct HTTP vs browser vs proxy)\n\n"
            f"If probe_page succeeded, you have all the page data you need. "
            f"Optionally use `playwright_browser_*` tools for deeper analysis "
            f"(network requests, cookies) if the probe result is inconclusive.\n\n"
        )
        call_allocation = (
            f"### Call Allocation (target: 5-8 calls)\n"
            f"1. probe_page on product URL (1 call)\n"
            f"2. Optional: playwright_browser_* for deeper analysis (1-3 calls)\n"
            f"3. write_file to save analysis (1 call)\n\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Building a product scraper for {url}. The scraper reads product URLs "
        f"from `input_urls.json` and extracts data from each product page.\n\n"
        f"## Your Task: Site Analysis\n\n"
        f"Analyze the **product page** below to determine platform, anti-bot "
        f"protection, and the best scraping mechanism.\n\n"
        f"**Product URL (analyze this page):** {product_url}\n"
        f"**Site URL (for reference):** {url}\n"
        f"**Currency:** {currency}\n"
        f"**Site slug:** {slug}\n"
        f"**Save artifact to:** workspace/{slug}/site_analysis.json\n\n"
        f"{cached_probe}"
        f"{access_strategy}"
        f"{call_allocation}"
        f"### BUDGET: 30 tool calls maximum (target 5-8).\n\n"
        f"### WRITE EARLY — CRITICAL\n"
        f"Write site_analysis.json as soon as you have platform + mechanism + anti-bot.\n"
        f"You can overwrite the file later if you learn more. Do NOT wait until the end.\n"
        f"If you are running low on budget and haven't written the file yet, STOP exploring\n"
        f"and write what you have immediately. A partial analysis is better than none.\n\n"
        f"### Connectivity Info in Output\n"
        f"Include a `connectivity` section in your site_analysis.json:\n"
        f"```json\n"
        f'"connectivity": {{\n'
        f'  "method_that_worked": "direct_http|browser_none|uc_chrome_none|...",\n'
        f'  "proxy_tier": "none",\n'
        f'  "js_rendering_needed": true,\n'
        f'  "anti_bot_detected": false\n'
        f"}}\n"
        f"```\n"
        f"Downstream agents (product_analyzer, scraper_analyzer) read this.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT use Wayback Machine, archive.org, cached snapshots, or any archived version\n"
        f"- Do NOT enumerate all Algolia indices or test facet partitioning\n"
        f"- Do NOT crawl categories, sitemaps, or other product pages\n"
        f"- Do NOT read `input_urls.json` — that file is for the code-writer\n"
        f"- Do NOT load skill files — detect from page content only\n"
        f"- Do NOT spend more than 2-3 calls on any single sub-task\n\n"
        f"**CRITICAL: You MUST call write_file to save the analysis as JSON to "
        f"workspace/{slug}/site_analysis.json. Do NOT just print the analysis as text. "
        f"Write the file as soon as you have the core findings.**"
    )
    return [HumanMessage(content=content)]


def build_product_analyzer_message(state: dict) -> list:
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or "auto-discover"

    cached_probe = ""
    probe_result = state.get("probe_result")
    has_verified_probe = False
    if probe_result and probe_result.get("connectivity"):
        conn = probe_result["connectivity"]
        verified = " (captcha-verified)" if probe_result.get("captcha_verified") else ""
        has_verified_probe = True
        cached_probe = (
            f"\n### Cached Probe Result (from site_analyzer){verified}\n"
            f"The site_analyzer already probed this page{verified}. Use this data instead of calling probe_page again:\n"
            f"```\n"
            f"method_that_worked: {conn.get('method_that_worked', 'unknown')}\n"
            f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
            f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
            f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
            f"platform: {probe_result.get('platform', 'unknown')}\n"
            f"```\n\n"
        )

    if has_verified_probe:
        access_strategy = (
            f"### Page Access Strategy\n\n"
            f"The page has already been probed (see Cached Probe Result above). "
            f"**Do NOT call probe_page** — it would waste a tool call and return the same data.\n\n"
            f"Use `playwright_browser_*` tools directly for deeper analysis (DOM inspection, "
            f"additional selectors, network requests) if needed.\n\n"
        )
        workflow = (
            f"### Workflow\n"
            f"1. Read site_analysis.json (1 call)\n"
            f"2. Map all fields from cached probe result — JSON-LD, selectors, meta tags\n"
            f"3. Optionally use playwright_browser_* for additional selector testing (2-5 calls)\n"
            f"4. write_file to save field mapping (1 call)\n\n"
        )
    else:
        access_strategy = (
            f"### Page Access Strategy\n\n"
            f"Use `probe_page` as your FIRST tool call after reading site_analysis.json. "
            f"It automatically tries direct HTTP → browser (no proxy) → browser "
            f"(datacenter proxy) → browser (residential proxy) and returns what worked.\n\n"
            f"```\n"
            f'probe_page(url="{product_url}", render_js=True)\n'
            f"```\n\n"
            f"The probe result includes:\n"
            f"- JSON-LD blocks (with field-level detail)\n"
            f"- Open Graph meta tags\n"
            f"- Common selector test results (h1, price, availability, etc.)\n"
            f"- Which connection method and proxy tier worked\n\n"
        )
        workflow = (
            f"### Workflow\n"
            f"1. Read site_analysis.json (1 call)\n"
            f"2. Call probe_page on the product URL (1 call)\n"
            f"3. Map all fields from probe result — JSON-LD, selectors, meta tags\n"
            f"4. Optionally use playwright_browser_evaluate for additional selector testing (2-5 calls)\n"
            f"5. write_file to save field mapping (1 call)\n\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Building a product scraper for {url}. The scraper reads product URLs "
        f"from `input_urls.json` and extracts data from each product page.\n\n"
        f"## Your Task: Product Field Mapping\n\n"
        f"Critically review the site analysis, then analyze the **ONE product page** "
        f"below to map every extractable field with exact selectors.\n\n"
        f"**Product URL (analyze this page):** {product_url}\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Save artifact to:** workspace/{slug}/product_analysis.json\n"
        f"{cached_probe}"
        f"{access_strategy}"
        f"{workflow}"
        f"### BUDGET: 50 tool calls maximum.\n\n"
        f"### WRITE EARLY — CRITICAL\n"
        f"Write product_analysis.json as soon as you have mapped the core fields.\n"
        f"You can overwrite the file later if you discover more selectors. Do NOT wait\n"
        f"until the end. If you are running low on budget and haven't written the file\n"
        f"yet, STOP exploring and write what you have immediately.\n"
        f"A partial field mapping is better than none.\n\n"
        f"### Connectivity Info in Output\n"
        f"Include a `connectivity` section in your product_analysis.json:\n"
        f"```json\n"
        f'"connectivity": {{\n'
        f'  "method_that_worked": "direct_http|browser_none|uc_chrome_none|...",\n'
        f'  "proxy_tier": "none",\n'
        f'  "js_rendering_needed": true\n'
        f"}}\n"
        f"```\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT use Wayback Machine, archive.org, cached snapshots, or any archived version\n"
        f"- Do NOT explore related products, similar items, or recommendations\n"
        f"- Do NOT click size/color selectors beyond initial verification\n"
        f"- Do NOT test Algolia API or any structured API (site-analyzer did that)\n"
        f"- Do NOT check dataLayer, anti-bot, or load platform skills\n"
        f"- Do NOT examine newsletters, store locators, or site navigation\n"
        f"- Do NOT read `input_urls.json` — that file is for the code-writer\n"
        f"- Do NOT revisit sections you've already analyzed\n\n"
        f"**CRITICAL: You MUST call write_file to save the field mapping as JSON to "
        f"workspace/{slug}/product_analysis.json as your LAST action. "
        f"Do NOT just print the analysis as text.**"
    )
    return [HumanMessage(content=content)]


def build_scraper_analyzer_message(state: dict) -> list:
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or ""

    cached_probe = ""
    probe_result = state.get("probe_result")
    has_verified_probe = False
    cached_proxy_tier = "none"
    if probe_result and probe_result.get("connectivity"):
        conn = probe_result["connectivity"]
        verified = " (captcha-verified)" if probe_result.get("captcha_verified") else ""
        has_verified_probe = True
        cached_proxy_tier = conn.get("proxy_tier", "none")
        cached_probe = (
            f"\n### Cached Probe Result{verified}\n"
            f"Page was already probed{verified}. Method that worked: `{conn.get('method_that_worked', 'unknown')}` "
            f"with proxy tier `{conn.get('proxy_tier', 'none')}`. "
            f"Anti-bot detected: {conn.get('anti_bot_detected', False)}.\n"
            f"**Use this method and proxy tier — do NOT re-probe unless the analysis contradicts it.**\n"
        )

    retry_section = ""
    scraper_draft_path = f"workspace/{slug}/scraper_draft.py"
    scraper_analysis_path = f"workspace/{slug}/scraper_analysis.json"
    if state.get("test_report"):
        retry_section = (
            f"\n{_summarize_test_report(state)}\n"
            f"Read the broken scraper at: {scraper_draft_path}\n"
            f"Read the previous analysis at: {scraper_analysis_path}\n"
            f"Adjust the strategy, proxy tier, and selectors based on the failures above.\n"
            f"Escalate proxy tier if connection was blocked.\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Determine the **working** scraping strategy for {url} by verifying "
        f"what upstream analyses found. Produce verified instructions for the code-writer.\n\n"
        f"## Your Task: Strategy Verification\n\n"
        f"**Product URL (test against this):** {product_url}\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Save artifact to:** workspace/{slug}/scraper_analysis.json\n"
        f"{retry_section}{cached_probe}\n"
        f"### Strategy Verification\n\n"
        f"Site and product analyzers already probed the page using `probe_page`. "
        f"Read their `connectivity` sections to understand what worked:\n"
        f"- What method accessed the page (direct_http, browser_none, browser_datacenter, uc_chrome_none, uc_chrome_datacenter, etc.)\n"
        f"- What proxy tier worked\n"
        f"- Whether JS rendering was needed\n"
        f"- Whether anti-bot was detected\n\n"
        f"**Method → Strategy mapping (CRITICAL):**\n"
        f"- `direct_http` → strategy `http_requests`, proxy_tier `none`\n"
        f"- `browser_*` → strategy `playwright`, proxy_tier from suffix\n"
        f"- `uc_chrome_*` → strategy `seleniumbase_uc`, proxy_tier from suffix\n"
        f"  (means standard Playwright was blocked by anti-bot, but UC Chrome bypassed it)\n"
        f"- `uc_chrome_none` → strategy `seleniumbase_uc`, proxy_tier `none`\n"
        f"- `uc_chrome_datacenter` → strategy `seleniumbase_uc`, proxy_tier `datacenter`\n"
        f"- `uc_chrome_residential` → strategy `seleniumbase_uc`, proxy_tier `residential`\n\n"
        f"If connectivity info is missing or seems unreliable, call `probe_page` yourself:\n"
        f"```\n"
        f'probe_page(url="{product_url}", render_js=True)\n'
        f"```\n\n"
        f"### Workflow\n"
        f"1. Read site_analysis.json and product_analysis.json (1-2 calls)\n"
        f"2. Check connectivity info from upstream analyses\n"
        f"3. Optionally call probe_page to verify (1 call)\n"
        f"4. Determine best strategy + proxy_tier based on connectivity\n"
        f"5. Verify selectors from product_analysis against probe results\n"
        f"6. write_file to save scraper_analysis.json (1 call)\n\n"
    )

    if has_verified_probe and cached_proxy_tier != "none":
        content += (
            f"### Proxy Configuration (PRE-VERIFIED)\n"
            f"The pre-verified probe already determined that `{cached_proxy_tier}` proxy tier is required. "
            f"Use **exactly** this proxy tier in the scraper strategy. Do NOT downgrade to 'none' — "
            f"the site blocks direct connections.\n"
            f"- Use proxy_tier = '{cached_proxy_tier}' in the strategy\n"
            f"- On retry: escalate one level from `{cached_proxy_tier}` if still blocked\n\n"
        )
    else:
        content += (
            f"### Proxy Escalation (start with no proxy)\n"
            f"- First attempt: proxy_tier = 'none' (direct connection)\n"
            f"- If blocked: escalate to 'datacenter'\n"
            f"- If still blocked: escalate to 'residential'\n"
            f"- On retry: start from previous tier and escalate one level\n\n"
        )

    content += (
        f"### Strategy Selection\n"
        f"- If direct HTTP got full data → 'http_requests'\n"
        f"- If direct HTTP got JSON-LD but no price → 'http_requests' with CSS fallback\n"
        f"- If browser_* method worked (no anti-bot) → 'playwright'\n"
        f"- If uc_chrome_* method worked (anti-bot bypassed) → 'seleniumbase_uc'\n"
        f"- Always prefer simpler strategies over complex ones\n\n"
        f"### BUDGET: 30 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT use Wayback Machine, archive.org, cached snapshots, or any archived version\n"
        f"- Do NOT generate scraper code — that's code_writer's job\n"
        f"- Do NOT assume product_analysis selectors are correct — verify them\n"
        f"- Do NOT skip testing — if you can't verify, say so explicitly\n"
        f"- Do NOT read or modify input_urls.json — that's code_writer's concern\n\n"
        f"**CRITICAL: You MUST call write_file to save your analysis as JSON to "
        f"workspace/{slug}/scraper_analysis.json. Do NOT just print it.**"
    )
    return [HumanMessage(content=content)]


def _url_discovery_rules(site_input_urls: list[str]) -> str:
    if site_input_urls:
        return (
            f"- Do NOT discover, crawl, or add any product URLs to input_urls.json\n"
            f"- Do NOT overwrite the existing input_urls.json — read it and use it as-is\n"
            f"- input_urls.json MUST contain ONLY the {len(site_input_urls)} URLs "
            f"provided by the user, NOT discovered links\n"
        )
    return (
        f"- Do NOT read an existing input_urls.json from a previous run — write a fresh one\n"
        f"- Do NOT add discovered/related product URLs to input_urls.json — "
        f"only include the URL(s) provided by the user\n"
        f"- input_urls.json MUST contain ONLY the Product URL(s) listed above, "
        f"NOT discovered links\n"
    )


def _summarize_test_report(state: dict) -> str:
    report = state.get("test_report")
    if not report:
        return ""
    assessment = report.get("overall_assessment", "UNKNOWN")
    confidence = report.get("confidence_score", 0.0)
    issues = report.get("issues", [])
    retry_count = state.get("test_retry_count", 0)

    lines = [
        f"### Previous Test Results (Retry Cycle {retry_count + 1})",
        f"- **Assessment:** {assessment}",
        f"- **Confidence:** {confidence:.0%}",
    ]
    if issues:
        high = [i for i in issues if i.get("severity") == "high"]
        medium = [i for i in issues if i.get("severity") == "medium"]
        if high:
            lines.append(f"\n**HIGH severity ({len(high)}):**")
            for i in high[:5]:
                field = i.get("field", i.get("description", "?"))
                desc = i.get("description", "")
                expected = i.get("expected", "")
                actual = i.get("actual", "")
                lines.append(f"  - `{field}`: {desc}")
                if expected or actual:
                    lines.append(f"    Expected: {expected!r} | Actual: {actual!r}")
        if medium:
            lines.append(f"\n**MEDIUM severity ({len(medium)}):**")
            for i in medium[:3]:
                field = i.get("field", i.get("description", "?"))
                lines.append(f"  - `{field}`: {i.get('description', '')}")
    if retry_count > 0:
        lines.append(f"\n*{retry_count} previous attempt(s) failed.*")
    return "\n".join(lines)


def build_code_writer_message(state: dict) -> list:
    """Build the initial HumanMessage for the code-writer agent.

    On retry cycles, includes the test report so the agent can apply fixes.
    """
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url", "")
    site_analysis = state.get("site_analysis") or {}
    scraper_analysis = state.get("scraper_analysis") or {}
    mechanism = scraper_analysis.get("strategy") or site_analysis.get(
        "scraping_mechanism", ""
    )
    algolia = site_analysis.get("algolia", {})
    site_input_urls = state.get("input_urls") or []

    provided_urls_section = ""
    if site_input_urls:
        count = len(site_input_urls)
        provided_urls_section = (
            f"\n### PROVIDED PRODUCT URLs (FROM SITE MODEL)\n"
            f"The user has provided {count} product URLs via the Site configuration. "
            f"These URLs are already saved to `workspace/{slug}/input_urls.json`.\n\n"
            f"**You MUST use these URLs exactly as provided.** Do NOT discover new URLs, "
            f"do NOT crawl categories or sitemaps, do NOT modify the URL list. "
            f"Read the existing `input_urls.json` and use it as-is in the scraper.\n"
            f"The `--sample` flag should scrape the first 5 URLs from this list.\n"
        )

    retry_section = ""
    test_report_path = f"workspace/{slug}/test_report.json"
    if state.get("test_report"):
        retry_section = (
            f"\n\n{_summarize_test_report(state)}\n"
            f"Read the full test report at: {test_report_path}\n"
            f"Fix the scraper at: workspace/{slug}/scraper_draft.py\n"
            f"Focus on the HIGH severity issues above. Do NOT change parts that work.\n"
        )

    algolia_section = ""
    if algolia and algolia.get("detected"):
        algolia_section = (
            f"\n### Algolia API\n"
            f"If the site uses Algolia for product data:\n"
            f"- Use Algolia as a **per-product lookup**: extract the product ID "
            f"from the URL and query Algolia by objectID\n"
            f"- Endpoint: {algolia.get('endpoint', ' Algolia Search API')}\n"
            f"- App ID: {algolia.get('application_id', '')}, "
            f"Key: {algolia.get('api_key', '')}\n"
            f"- Index: {algolia.get('index_name', '')}\n"
            f"- Do NOT implement discover mode, facet partitioning, or bulk extraction\n"
            f"- Products come from `input_urls.json`, not Algolia discovery\n"
        )

    template_hint = ""
    if mechanism:
        template_file = f"{mechanism}_scraper.py"
        if mechanism in (
            "stealth_browser",
            "undetected_chromedriver",
            "seleniumbase_uc",
        ):
            template_file = "undetected_chromedriver_scraper.py"
        template_hint = (
            f"\n### Template\nRead the template at: templates/{template_file} "
            f"and use it as your base. The scraper will run on a dedicated worker "
            f"container that has Chrome, SeleniumBase, and Playwright installed."
        )

    scraper_analysis_section = ""
    if scraper_analysis:
        proxy_tier = scraper_analysis.get("proxy_tier", "none")
        no_proxy = scraper_analysis.get("no_proxy_flag", proxy_tier == "none")
        verified = scraper_analysis.get("verified_selectors", {})

        proxy_instructions = ""
        if no_proxy:
            proxy_instructions = (
                "\n**PROXY: Do NOT use any proxy.** The scraper_analyzer verified that "
                "direct connection (no proxy) works. The scraper MUST accept `--no-proxy` "
                "flag and should default to NO proxy for this site. Do NOT import or use "
                "the proxy module.\n"
            )
        elif proxy_tier == "datacenter":
            proxy_instructions = (
                "\n**PROXY: Use datacenter proxy.** The scraper should use the datacenter "
                "proxy from `config/proxy.json` via `src/proxy.py`.\n"
            )
        elif proxy_tier == "residential":
            proxy_instructions = (
                "\n**PROXY: Use residential proxy (expensive).** Only use as last resort. "
                "The scraper should use residential proxy from `config/proxy.json`.\n"
            )

        verified_section = ""
        if verified:
            lines = []
            for field_name, field_info in verified.items():
                method = field_info.get("method", "unknown")
                verified_flag = field_info.get("verified", False)
                note = field_info.get("note", "")
                if method == "jsonld":
                    path = field_info.get("path", "")
                    lines.append(
                        f"  - {field_name}: JSON-LD path `{path}` (verified={verified_flag})"
                    )
                elif method == "css":
                    selector = field_info.get("selector", "")
                    lines.append(
                        f"  - {field_name}: CSS `{selector}` (verified={verified_flag})"
                    )
                elif method == "static":
                    value = field_info.get("value", "")
                    lines.append(f"  - {field_name}: static value `{value}`")
                if note:
                    lines.append(f"    Note: {note}")
            verified_section = (
                "\n### Verified Selectors (from scraper_analyzer)\n"
                + "\n".join(lines)
                + "\n"
            )

        extraction_approach = scraper_analysis.get("extraction_approach", "")
        approach_section = ""
        if extraction_approach:
            approach_section = f"\n### Extraction Approach: {extraction_approach}\n"

        warmup = scraper_analysis.get("warmup_required", False)
        cookie = scraper_analysis.get("cookie_consent_required", False)
        extras = []
        if warmup:
            extras.append(
                "- **Warmup required:** Visit homepage first, wait for anti-bot sensors"
            )
        if cookie:
            extras.append(
                "- **Cookie consent required:** Accept cookies before scraping"
            )
        extras_section = ""
        if extras:
            extras_section = (
                "\n### Additional Requirements\n" + "\n".join(extras) + "\n"
            )

        scraper_analysis_section = (
            f"\n### Scraper Analysis (VERIFIED — follow these instructions)\n"
            f"**Strategy:** {mechanism}\n"
            f"**Proxy tier:** {proxy_tier}\n"
            f"**Strategy justification:** {scraper_analysis.get('strategy_justification', '')}\n"
            f"{proxy_instructions}{approach_section}{verified_section}{extras_section}"
            f"\n**Read the full scraper analysis:** workspace/{slug}/scraper_analysis.json\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Build a **product scraper** for {url}. The scraper reads product URLs "
        f"from `input_urls.json` (in its own directory) and extracts data from "
        f"each product page.\n\n"
        f"## Your Task: Write the Scraper\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Product URL:** {product_url}\n"
        f"**Scraping mechanism:** {mechanism or 'auto-detect'}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Scraper analysis:** workspace/{slug}/scraper_analysis.json\n"
        f"**Save scraper to:** workspace/{slug}/scraper_draft.py\n"
        f"**Save input URLs to:** workspace/{slug}/input_urls.json"
        f"{provided_urls_section}"
        f"{retry_section}{algolia_section}{template_hint}{scraper_analysis_section}\n\n"
        f"### Architecture\n"
        f"The scraper reads URLs from `input_urls.json` in SCRIPT_DIR and "
        f"extracts data from EACH product page. For each URL:\n"
        f"1. Extract product ID/codes from the URL\n"
        f"2. Fetch product data (via API lookup, HTTP request, or page scrape)\n"
        f"3. Map raw data to output fields\n"
        f"4. Write results to `output_{{datetime}}.json`\n\n"
        f"### Field Formatting Rules (CRITICAL)\n"
        f'- **price**: Must include the currency symbol, e.g. `"$1,795.00"` not `"1,795.00"`\n'
        f"- **src_url**: Set to the URL where the product was discovered. "
        f"If input comes from input_urls.json, src_url equals the product URL.\n"
        f'- **original_price**: Empty string `""` if not on sale, otherwise include '
        f'currency symbol like `"$2,000.00"`\n'
        f'- **availability**: Normalize to `"In Stock"` or `"Out of Stock"`\n'
        f'- **currency**: ISO 4217 code e.g. `"USD"`, `"EUR"`\n\n'
        f"### DO NOT\n"
        f"- Add 'discover mode', catalog crawling, or site-wide discovery\n"
        f"- Add pagination logic (products come from input_urls.json)\n"
        f"- Add bulk extraction via APIs (Algolia partitioning, etc.)\n"
        f"- Add multiple modes of operation\n"
        f"- Use `Accept-Encoding: gzip, deflate, br` — use only `gzip, deflate` "
        f"(requests library may not support Brotli)\n"
        f"{_url_discovery_rules(site_input_urls)}\n"
        f"- Deviate from the template's input/output structure\n\n"
        f"### Soft 404 Detection (CRITICAL)\n"
        f"Many e-commerce sites return HTTP 200 for deleted/expired products but show "
        f"'Product Not Found', 'No Longer Available', or redirect to a search page.\n\n"
        f"Your scraper MUST detect these cases and set the `remarks` field:\n"
        f"- Check if JSON-LD contains a Product type — if not, likely not a product page\n"
        f"- Check if the page title or H1 contains 'not found', 'unavailable', "
        f"'discontinued', 'no longer available'\n"
        f"- Check if the final URL after redirects differs from the requested product URL\n"
        f"- When detected, set `remarks` to a description like "
        f"'Soft 404: product not found' and leave title/price empty — "
        f"do NOT extract data from a non-product page.\n\n"
        f"### Image Extraction Rules (CRITICAL)\n"
        f"Product images must be scoped to the PRODUCT GALLERY only. Never capture:\n"
        f"- Navigation banners, header images, or hero images\n"
        f"- Recommended/related product thumbnails\n"
        f"- Emoji, icon, flag, or badge images\n"
        f"- Logo or brand images\n\n"
        f"To achieve this:\n"
        f"- Scope image selectors to the product gallery container "
        f"(e.g. [data-auto-id='product-image'], .product-gallery, "
        f"#pdp-gallery, [data-testid*='gallery'])\n"
        f"- Filter collected images by product SKU/code in the src URL\n"
        f"- Skip images with URLs containing /brand.assets/, /emoji/, "
        f"/flags/, /icon/, or /navigation/\n"
        f"- Skip images where the src URL path has no product identifier\n"
        f"- A typical product page should have 3-15 images, NOT 100+\n\n"
        f"### Required CLI Arguments\n"
        f"The scraper MUST support these argparse arguments:\n"
        f"- `--input FILE` — Path to input URLs JSON file\n"
        f"- `--urls URL [URL ...]` — Product URLs as CLI arguments\n"
        f"- `--sample` — Scrape only 5 products (action='store_true')\n"
        f"- `--limit N` — Max products to scrape (type=int)\n\n"
        f"**CRITICAL: You MUST call write_file to save the scraper to "
        f"workspace/{slug}/scraper_draft.py"
        f"{' AND call write_file to save input URLs to workspace/' + slug + '/input_urls.json' if not site_input_urls else ''}. "
        f"Do NOT just print code.**"
    )
    return [HumanMessage(content=content)]


def build_code_tester_message(state: dict) -> list:
    """Build the initial HumanMessage for the code-tester agent."""
    slug = state.get("site_slug", "unknown")

    retry_context = ""
    retry_count = state.get("test_retry_count", 0)
    if retry_count > 0:
        retry_context = (
            f"\n### RETEST MODE (Cycle {retry_count + 1})\n"
            f"The scraper was modified after previous test failures. "
            f"Focus your validation on the fields that previously failed. "
            f"Read the previous test report at: workspace/{slug}/test_report.json\n\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Building a product scraper for {slug}. Validate the generated scraper "
        f"extracts correct field values from product pages.\n\n"
        f"{retry_context}"
        f"## Your Task: Test the Scraper\n\n"
        f"**Site slug:** {slug}\n"
        f"**Scraper path:** workspace/{slug}/scraper_draft.py\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Save test report to:** workspace/{slug}/test_report.json\n\n"
        f"### Workflow\n"
        f"1. Read the scraper source (1 call)\n"
        f"2. Run the scraper using `run_scraper` tool with path `workspace/{slug}/scraper_draft.py` "
        f"and args `--sample` (1-3 calls). "
        f"The run_scraper tool automatically routes browser-based scrapers (Playwright, "
        f"SeleniumBase) to the uc-scraper-worker container which has Chrome + Xvfb.\n"
        f"3. Read the output file and validate each field against product_analysis.json\n"
        f"4. write_file to save test report (1 call)\n\n"
        f"### BUDGET: 20 tool calls maximum.\n\n"
        f"### How Scraper Execution Works\n"
        f"The `run_scraper` tool automatically detects browser-based scrapers (Playwright, "
        f"SeleniumBase, etc.) and dispatches them to a remote `browser-service` container "
        f"that has Chrome + Xvfb + all browser libraries pre-installed. "
        f"HTTP-based scrapers run locally. You NEVER need to install packages.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT modify or fix the scraper — only report issues\n"
        f"- Do NOT re-run the scraper more than 3 times\n"
        f"- Do NOT explore the live product page in a browser\n"
        f"- Do NOT write a new scraper\n"
        f"- Do NOT read or modify input_urls.json — that file is not your concern\n"
        f"- Do NOT use run_bash to run the scraper — use run_scraper instead\n"
        f"- Do NOT run `pip install`, `pip3 install`, or any package installation commands — "
        f"all required packages are pre-installed in the execution environment\n"
        f"- Do NOT do manual bash probing, web_fetch, or inline python scripts — "
        f"just run the scraper and read its output\n\n"
        f"### Optional Fields\n"
        f"Fields `original_price` and `location` are optional — they may not exist on "
        f"every product. Only flag as a high-severity issue if the product is clearly on "
        f"sale (e.g., there's a strikethrough price or 'was' price visible) but the "
        f"field is still empty. Missing optional fields should be severity 'low' or 'info'.\n\n"
        f"### Dead URLs (404s and Redirects)\n"
        f"Some product URLs may no longer be valid — they return 404, redirect to a "
        f"different page, or show 'product not found'. These are NOT scraper bugs.\n\n"
        f"When evaluating scraper output:\n"
        f"- Check the `status_code` field per product. Status codes 301, 302, 303, 307, "
        f"308, 404, 410, or 451 indicate dead/expired product pages — EXCLUDE these "
        f"from your quality assessment entirely.\n"
        f"- Only flag missing `title` or `price` as issues for products with `status_code` "
        f"200 that are clearly real product pages.\n"
        f"- If a product has `status_code` 200 but shows a 'product not found' message, "
        f"check the `remarks` field — the scraper may have noted this.\n"
        f"- When calculating `confidence_score`, count dead URLs as 'skipped' (excluded "
        f"from denominator), NOT as failures.\n"
        f"- If ALL sampled URLs are dead (all non-200), set `overall_assessment` to PASS "
        f"with `confidence_score` 1.0 and note 'all sampled URLs are dead — cannot assess "
        f"scraper quality, but no scraper errors detected'.\n\n"
        f"- Soft 404s: If a product has `status_code` 200 but the `remarks` field "
        f"mentions 'soft 404', 'product not found', or similar, treat it the same as "
        f"a dead URL — exclude from quality assessment.\n\n"
        f"### Image Validation\n"
        f"Check the `images` field for these common problems:\n"
        f"- Too many images (>20) likely means the scraper captured banners, nav images, "
        f"recommended products, or emojis — flag as high severity\n"
        f"- URLs containing `/brand.assets/`, `/emoji/`, `/flags/`, `/icon/`, "
        f"`/navigation/` are NOT product images\n"
        f"- Images should contain the product SKU/code in their URL\n"
        f"- Expected count: 3-15 product images per product\n\n"
        f"**CRITICAL: You MUST call write_file to save your test report to "
        f"workspace/{slug}/test_report.json as your LAST action. "
        f"Include pass/fail per field, with actual vs expected values.**"
    )
    return [HumanMessage(content=content)]


def build_cleanup_message(state: dict) -> list:
    """Build the initial HumanMessage for the cleanup agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")

    content = (
        f"## OBJECTIVE\n"
        f"Finalize the product scraper for {url}.\n\n"
        f"## Your Task: Cleanup\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Scraper draft:** workspace/{slug}/scraper_draft.py\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Target folder:** scrapers/{slug}/\n"
        f"**Save cleanup report to:** workspace/{slug}/cleanup_report.json\n\n"
        f"### Workflow\n"
        f"1. Copy scraper_draft.py to scrapers/{slug}/scraper.py\n"
        f"2. Copy input_urls.json to scrapers/{slug}/input_urls.json\n"
        f"3. write_file to save cleanup report (1 call)\n\n"
        f"### BUDGET: 10 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT modify the scraper code\n"
        f"- Do NOT delete workspace analysis files (site_analysis.json, product_analysis.json, test_report.json)\n"
        f"- Do NOT run the scraper\n\n"
        f"**CRITICAL: You MUST call write_file to save your cleanup report to "
        f"workspace/{slug}/cleanup_report.json as your LAST action.**"
    )
    return [HumanMessage(content=content)]


def build_skill_learner_message(state: dict) -> list:
    """Build the initial HumanMessage for the skill-learner agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    platform = state.get("platform", "custom")

    content = (
        f"## OBJECTIVE\n"
        f"Capture reusable knowledge from the completed scrape of {url}.\n\n"
        f"## Your Task: Skill Learning\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Detected platform:** {platform}\n"
        f"**Scraper:** scrapers/{slug}/scraper.py\n"
        f"**Save learning report to:** workspace/{slug}/learning_report.json\n\n"
        f"### Workflow\n"
        f"1. Read the scraper and analysis files (2-4 calls)\n"
        f"2. Check existing skills in .opencode/skills/ (1-2 calls)\n"
        f"3. write_file to save learning report (1 call)\n\n"
        f"### BUDGET: 10 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT modify any skill files — only report proposals\n"
        f"- Do NOT modify the scraper\n"
        f"- Do NOT run anything\n\n"
        f"**CRITICAL: You MUST call write_file to save your learning report to "
        f"workspace/{slug}/learning_report.json as your LAST action.**"
    )
    return [HumanMessage(content=content)]


__all__ = [
    "create_site_analyzer",
    "create_product_analyzer",
    "create_scraper_analyzer",
    "create_code_writer",
    "create_code_tester",
    "create_cleanup_agent",
    "create_skill_learner",
    "build_site_analyzer_message",
    "build_product_analyzer_message",
    "build_scraper_analyzer_message",
    "build_code_writer_message",
    "build_code_tester_message",
    "build_cleanup_message",
    "build_skill_learner_message",
]
