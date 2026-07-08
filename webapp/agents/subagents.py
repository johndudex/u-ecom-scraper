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
import re

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

from .constants import DEAD_STATUS_CODES, FINAL_RETRY_SENTINEL
from .llm import get_main_llm, get_llm
from .prompts import load_agent_prompt

logger = logging.getLogger(__name__)

# ── Temperature mapping from .opencode/agents/*.md frontmatter ──────────────

AGENT_TEMPERATURES: dict[str, float] = {
    "site-analyzer": 0.2,
    "product-analyzer": 0.2,
    "navigation-agent": 0.2,
    "navigation-synthesize": 0.2,
    "nav-skill-review": 0.2,
    "scraper-analyzer": 0.2,
    "code-writer": 0.4,
    "code-tester": 0.1,
    "code-reviewer": 0.1,
    "cleanup": 0.1,
    "skill-learner": 0.3,
    "dagster-converter": 0.1,
}

# Per-agent model overrides (keyed by prompt_stem, like AGENT_TEMPERATURES).
# Value is the NAME of a settings attr holding the model; resolved lazily in
# _build_agent (settings isn't imported at module level). Agents not listed use
# the main model (get_main_llm / ZAI_MAIN_MODEL). code_writer is pure codegen —
# analysis/nav/field-mapping is done upstream and handed to it as structured
# summaries — so it runs on the faster flash model.
# Set CODE_WRITER_MODEL=glm-5-turbo (or =ZAI_MAIN_MODEL) to revert.
AGENT_MODEL_SETTINGS: dict[str, str] = {
    "code-writer": "CODE_WRITER_MODEL",
}

# ── Internal name mapping: agent node name → prompt file stem ─────────────

AGENT_PROMPT_MAP: dict[str, str] = {
    "site_analyzer": "site-analyzer",
    "product_analyzer": "product-analyzer",
    "navigation_agent": "navigation-agent",
    "navigation_explore": "navigation-agent",
    "navigation_synthesize": "navigation-synthesize",
    "nav_skill_review": "nav-skill-review",
    "scraper_analyzer": "scraper-analyzer",
    "code_writer": "code-writer",
    "code_tester": "code-tester",
    "code_review": "code-reviewer",
    "cleanup": "cleanup",
    "skill_learner": "skill-learner",
    "dagster_converter": "dagster-converter",
}


AGENT_MAX_ITERATIONS: dict[str, int] = {
    "site_analyzer": 30,
    "product_analyzer": 30,
    "navigation_agent": 40,
    "navigation_explore": 20,
    "navigation_synthesize": 15,
    "nav_skill_review": 15,
    "scraper_analyzer": 30,
    "code_writer": 20,
    "code_tester": 20,
    "cleanup": 15,
    "skill_learner": 15,
    "dagster_converter": 15,
}

BROWSER_UNAVAILABLE_WARNING = (
    "\n\n"
    "NOTE: Playwright MCP browser tools are unavailable. "
    "Use probe_page to access the page — it handles proxy escalation "
    "automatically. If probe_page also fails, write analysis based on "
    "URL structure and any existing workspace artifacts."
)


def _build_content_type_context(state: dict) -> str:
    """Build a concise content-type context block for agent messages."""
    content_type_config = state.get("content_type_config", {})
    if not content_type_config:
        return ""
    ct_name = content_type_config.get("content_type", "")
    output_key = content_type_config.get("output_key", "products")
    fields = content_type_config.get("fields", [])
    if not ct_name and not fields:
        return ""
    lines = ["### Content Type Context\n"]
    if ct_name:
        lines.append(f"- Scraping content type: **{ct_name}**")
    if output_key:
        lines.append(f"- Output key in JSON: `{output_key}`")
    if fields:
        core = [f for f in fields if f.get("required")]
        if core:
            field_names = ", ".join(f["name"] for f in core)
            lines.append(f"- Core fields to expect: {field_names}")
    return "\n".join(lines) + "\n\n"


def _summarize_product_analysis(pa: dict) -> str:
    """Complete-but-lean summary of product_analysis for code_writer.

    Replaces ``read_file`` of the full product_analysis.json (20K+). Includes
    EVERYTHING code_writer needs to write the scraper:

    - the per-field extraction map (method + selector + JSON-LD/CSS fallback +
      JS snippet) — compacted; ``examples``/``expectations``/``tested`` are
      dropped (validation is code_tester's concern, not extraction).
    - ``page_structure``, ``extraction_methods``, ``jsonld_extraction``,
      ``mechanism_reassessment``, ``site_analysis_review``, ``variants`` —
      included verbatim (each is small; these carry the page layout, the
      primary extraction approach, JSON-LD details + field guidance like
      "title truncated -> use CSS h1", and mechanism justification).
    - ``content_type`` / ``output_key`` scalars.

    Only genuinely redundant/non-codegen keys are dropped: ``connectivity``
    (site_analysis / scraper_analysis_section already cover it),
    ``confidence_score``, ``site_slug``, ``analyzed_products`` (a count).
    ~6-7K vs 24K, with zero loss of codegen-relevant information.
    """
    import json as _json

    pa = pa or {}
    fields = pa.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        return ""
    lines = ["\n### Product Analysis (COMPLETE summary — do NOT read the file)\n"]
    # Per-field extraction map (compact).
    lines.append("**Field Extraction Map:**")
    for name, info in fields.items():
        if not isinstance(info, dict):
            lines.append(f"- **{name}**: {info}")
            continue
        method = info.get("method", "?")
        sel = info.get("selector") or info.get("path") or ""
        fb = (
            info.get("jsonld_fallback")
            or info.get("css_fallback")
            or info.get("fallback")
            or ""
        )
        js = info.get("js_extraction") or ""
        line = f"- **{name}** [{method}]"
        if sel:
            line += f" sel=`{sel}`"
        if fb:
            line += f" fallback=`{fb}`"
        if js:
            line += f" js=`{js}`"
        lines.append(line)
    # The other extraction-relevant sections — verbatim (small + complete).
    for key, label in (
        ("page_structure", "Page Structure"),
        ("extraction_methods", "Extraction Methods"),
        ("jsonld_extraction", "JSON-LD Extraction"),
        ("mechanism_reassessment", "Mechanism Reassessment"),
        ("site_analysis_review", "Site Analysis Review"),
        ("variants", "Variants"),
    ):
        val = pa.get(key)
        if val:
            lines.append(f"**{label}:** {_json.dumps(val, ensure_ascii=False)}")
    # Tiny scalars code_writer references.
    if pa.get("content_type"):
        lines.append(f"**content_type:** {pa['content_type']}")
    if pa.get("output_key"):
        lines.append(f"**output_key:** {pa['output_key']}")
    return "\n".join(lines) + "\n"


def _summarize_navigation_extras(na: dict) -> str:
    """Navigation mechanics the structured nav_section doesn't already inject:
    filters (e.g. a POST-form discovery strategy), search/category notes. Keeps
    code_writer from needing to read_file navigation_analysis.json.
    """
    na = na or {}
    lines = []
    fl = na.get("filters") or {}
    if isinstance(fl, dict) and (fl.get("has_filters") or fl.get("description")):
        lines.append(
            f"**Filters:** method={fl.get('method', '?')} — {fl.get('description', '')}"
        )
    s = na.get("search") or {}
    if isinstance(s, dict) and s.get("description"):
        lines.append(f"**Search notes:** {s['description']}")
    cats = na.get("categories") or {}
    if isinstance(cats, dict) and cats.get("description"):
        lines.append(f"**Categories:** {cats['description']}")
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


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


def create_navigation_agent(site_slug: str = "") -> object:
    return _build_agent("navigation_agent", site_slug=site_slug)


def create_navigation_synthesize(site_slug: str = "") -> object:
    return _build_agent("navigation_synthesize", site_slug=site_slug)


def create_nav_skill_review(site_slug: str = "") -> object:
    return _build_agent("nav_skill_review", site_slug=site_slug)


def create_code_writer(site_slug: str = "") -> object:
    return _build_agent("code_writer", site_slug=site_slug)


def create_code_tester(site_slug: str = "") -> object:
    # First agent migrated to create_agent (v1). Short agent, no truncation needed.
    return _build_agent("code_tester", site_slug=site_slug, use_create_agent=True)


def create_code_reviewer(site_slug: str = "") -> object:
    # Read-only reviewer (read_file, search_content only) — advises, doesn't edit.
    return _build_agent("code_review", site_slug=site_slug, use_create_agent=True)


def create_cleanup_agent(site_slug: str = "") -> object:
    return _build_agent("cleanup", site_slug=site_slug)


def create_skill_learner(site_slug: str = "") -> object:
    return _build_agent("skill_learner", site_slug=site_slug)


def create_dagster_converter(site_slug: str = "") -> object:
    return _build_agent("dagster_converter", site_slug=site_slug)


# ── Shared builder ────────────────────────────────────────────────────────


def _truncate_messages(input_dict: dict) -> dict:
    """Pre-model hook: compress then truncate messages when context is large.

    Uses headroom.compress to intelligently summarize large tool messages,
    then drops oldest messages if still over budget.  Prevents
    ``Prompt exceeds max length`` errors in long-running agents.
    """
    messages = input_dict.get("messages", [])
    if not messages:
        return input_dict

    max_chars = 60_000

    def _total_chars(msgs):
        return sum(len(str(m.content)) if hasattr(m, "content") else 0 for m in msgs)

    total_chars = _total_chars(messages)
    if total_chars <= max_chars:
        return input_dict

    # Step 1: Compress large tool messages with headroom
    try:
        from headroom import compress as _compress
        from django.conf import settings

        model_name = getattr(settings, "ZAI_MAIN_MODEL", "glm-5-turbo")
        compressed_msgs = []
        compressed_count = 0
        for m in messages:
            content = str(m.content) if hasattr(m, "content") else ""
            if len(content) > 3000 and hasattr(m, "type") and m.type == "tool":
                try:
                    cr = _compress(
                        [{"role": m.type, "content": content}],
                        model=model_name,
                    )
                    new_content = cr.messages[0]["content"]
                    if len(content) - len(new_content) > 200:
                        compressed_msgs.append(type(m)(content=new_content))
                        compressed_count += 1
                        continue
                except Exception:
                    pass
            compressed_msgs.append(m)

        if compressed_count > 0:
            messages = compressed_msgs
            logger.info(
                "headroom: compressed %d tool messages (%d → %d chars)",
                compressed_count,
                total_chars,
                _total_chars(messages),
            )
    except ImportError:
        pass

    total_chars = _total_chars(messages)
    if total_chars <= max_chars:
        return {"messages": messages}

    # Step 2: Drop oldest messages (keep system + recent)
    system_msgs = [m for m in messages if hasattr(m, "type") and m.type == "system"]
    other_msgs = [m for m in messages if not (hasattr(m, "type") and m.type == "system")]

    kept = list(system_msgs)
    budget = max_chars - sum(len(str(m.content)) for m in kept)
    acc = 0

    for msg in reversed(other_msgs):
        msg_len = len(str(msg.content)) if hasattr(msg, "content") else 0
        if acc + msg_len > budget:
            break
        kept.insert(len(kept), msg)
        acc += msg_len

    logger.info(
        "Truncated messages: %d → %d (was %d chars, budget %d)",
        len(messages), len(kept), total_chars, budget,
    )

    return {"messages": kept}


def _build_agent(agent_name: str, site_slug: str = "", use_create_agent: bool = False) -> object:
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

    tools = _strip_v_prefix_from_tools(tools)
    from django.conf import settings as _settings

    _model_setting = AGENT_MODEL_SETTINGS.get(prompt_stem)
    _model_override = (
        getattr(_settings, _model_setting, None) if _model_setting else None
    )
    if _model_override:
        llm = get_llm(model=_model_override, temperature=temperature)
    else:
        llm = get_main_llm(temperature)

    logger.info(
        "Creating agent '%s' (model=%s, temp=%.1f, prompt_stem=%s, tools=%d)",
        agent_name,
        _model_override or getattr(_settings, "ZAI_MAIN_MODEL", "glm-5-turbo"),
        temperature,
        prompt_stem,
        len(tools),
    )

    if use_create_agent:
        # v1 path: langchain.agents.create_agent (create_react_agent is deprecated).
        # code_tester is a short agent (≤10 tool calls) on a large-context model, so
        # it doesn't need the pre_model_hook truncation. Other agents stay on
        # create_react_agent until their truncation is moved to SummarizationMiddleware
        # (see docs/langgraph-v1-enhancements.md). Gated + revertible per agent.
        from langchain.agents import create_agent

        agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
        logger.info("Created agent '%s' via create_agent (v1 path)", agent_name)
    else:
        agent = create_react_agent(
            llm, tools=tools, prompt=system_prompt, pre_model_hook=_truncate_messages
        )
    return agent


def _strip_v_prefix_from_tools(tools: list) -> list:
    """Monkey-patch ``BaseTool._parse_input`` to strip ``v__`` prefixes.

    The GLM model emits tool-call arguments with a ``v__`` prefix (e.g.
    ``v__command`` instead of ``command``).  LangChain's ``_parse_input``
    validates via Pydantic but then checks the **original** raw input dict
    to decide which fields to pass to the tool function — so a Pydantic
    ``model_validator`` alone is insufficient.  We override
    ``BaseTool._parse_input`` globally to strip prefixes from the raw
    input before any validation occurs.

    Idempotent — the patch is applied once on first call.
    """
    from langchain_core.tools import BaseTool

    if getattr(BaseTool, "_v_prefix_patch_applied", False):
        return tools

    _original_parse_input = BaseTool._parse_input

    def _patched_parse_input(self, tool_input, tool_call_id):
        if isinstance(tool_input, dict):
            tool_input = {
                (k[3:] if k.startswith("v__") else k): v for k, v in tool_input.items()
            }
        return _original_parse_input(self, tool_input, tool_call_id)

    BaseTool._parse_input = _patched_parse_input
    BaseTool._v_prefix_patch_applied = True

    return tools


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
    needs_scraper = "run_scraper" in requested
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

    if needs_bash or needs_scraper:
        try:
            from .tools.shell_tools import get_shell_tools as _gst

            all_shell = _gst()
            if needs_scraper and not needs_bash:
                all_shell = [t for t in all_shell if t.name == "run_scraper"]
            tools.extend(all_shell)
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

    # Generic tool-error handling: v1's ToolNode re-raises tool errors (v0.6
    # swallowed them into retry messages). Wrap every tool to catch exceptions
    # → return an error message, so an agent recovers instead of crashing.
    # Applied AFTER guards so it's the outer wrapper (catches guard errors too).
    from .tools.guards import apply_tool_error_catcher

    tools = [apply_tool_error_catcher(t) for t in tools]

    return tools


def _apply_guards(tools: list, agent_name: str) -> list:
    from .tools.guards import (
        apply_guard,
        require_non_akamai_tool,
        require_non_blocked_domain,
        require_same_domain,
        require_target_url,
    )

    guarded_agents = {
        "site_analyzer",
        "product_analyzer",
        "scraper_analyzer",
        "navigation_agent",
    }
    url_locked_agents = {"site_analyzer", "product_analyzer", "navigation_agent"}
    domain_locked_agents = {
        "site_analyzer",
        "product_analyzer",
        "scraper_analyzer",
    }

    if agent_name not in guarded_agents:
        return tools

    for i, t in enumerate(tools):
        name = getattr(t, "name", "")

        if name.startswith("playwright_browser_"):
            if agent_name in guarded_agents:
                t = apply_guard(t, require_non_akamai_tool)
            if agent_name in url_locked_agents and "navigate" in name:
                t = apply_guard(t, require_target_url)
            if agent_name in domain_locked_agents:
                t = apply_guard(t, require_same_domain)
            tools[i] = t

        elif name == "web_fetch":
            if agent_name in guarded_agents:
                t = apply_guard(t, require_non_akamai_tool)
                t = apply_guard(t, require_non_blocked_domain)
                if agent_name in domain_locked_agents:
                    t = apply_guard(t, require_same_domain)
            elif agent_name == "code_tester":
                t = apply_guard(t, require_non_akamai_tool)
            tools[i] = t

        elif name == "probe_page":
            if agent_name in domain_locked_agents:
                t = apply_guard(t, require_same_domain)
            tools[i] = t

    logger.info(
        "Guards applied for '%s': non_akamai=%s, target_url=%s, same_domain=%s",
        agent_name,
        agent_name in guarded_agents,
        agent_name in url_locked_agents,
        agent_name in domain_locked_agents,
    )
    return tools


# ── Message builders ──────────────────────────────────────────────────────


def build_site_analyzer_message(state: dict) -> list:
    """Build the initial HumanMessage for the site-analyzer agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or state.get("sample_url") or "auto-discover"
    currency = state.get("currency") or "auto-detect"

    content_type_context = _build_content_type_context(state)
    probe_result = state.get("probe_result")
    has_verified_probe = False
    cached_probe = ""
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
            f"http_method: {conn.get('http_method', 'none')}\n"
            f"browser_method: {conn.get('browser_method', 'none')}\n"
            f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
            f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
            f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
            f"```\n"
            f"**IMPORTANT**: The connectivity methods above bypass captcha/anti-bot. "
            f"Use `method_that_worked` in your site_analysis.json connectivity section. "
            f"If `http_method` is available, HTTP requests may also work. "
            f"If `browser_method` is available but different from `method_that_worked`, "
            f"prefer `method_that_worked` for scraping.\n"
        )

    if has_verified_probe:
        access_strategy = (
            "### Page Access Strategy\n\n"
            "The page has already been probed (see Pre-verified Probe Result above). "
            "**Do NOT call probe_page** — it would waste a tool call and return the same cached data.\n\n"
            "Use `playwright_browser_*` tools directly if you need deeper analysis "
            "(network requests, cookies, DOM inspection). Otherwise, proceed directly to "
            "writing site_analysis.json with the connectivity data from the pre-verified probe.\n\n"
        )
        call_allocation = (
            "### Call Allocation (target: 3-5 calls)\n"
            "1. Optional: playwright_browser_* for deeper analysis (0-2 calls)\n"
            "2. write_file to save analysis (1 call)\n\n"
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
            "### Call Allocation (target: 5-8 calls)\n"
            "1. probe_page on product URL (1 call)\n"
            "2. Optional: playwright_browser_* for deeper analysis (1-3 calls)\n"
            "3. write_file to save analysis (1 call)\n\n"
        )

    url_is_homepage = product_url == url or product_url.rstrip("/") == url.rstrip("/")
    page_label = (
        "No specific product URL was provided — analyze the homepage "
        "using the pre-verified probe data above."
        if url_is_homepage
        else f"Analyze this product page: {product_url}"
    )

    content = (
        f"## OBJECTIVE\n"
        f"Building a scraper for {url}. The scraper reads URLs "
        f"from `input_urls.json` and extracts data from each page.\n\n"
        f"{content_type_context}"
        f"## Your Task: Site Analysis\n\n"
        f"{page_label}\n\n"
        f"**Site URL (for reference):** {url}\n"
        f"**Currency:** {currency}\n"
        f"**Site slug:** {slug}\n"
        f"**Save artifact to:** workspace/{slug}/site_analysis.json\n\n"
        f"{cached_probe}"
        f"{access_strategy}"
        f"{call_allocation}"
        f"### BUDGET: 10 tool calls maximum (target 3-5).\n\n"
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
        f"### CRITICAL — URL Prohibition\n"
        f"- **Do NOT probe any URL other than the one provided above.**\n"
        f"- Do NOT guess product URLs, probe sitemap.xml, category pages, or variant URLs.\n"
        f"- Do NOT use Wayback Machine, archive.org, cached snapshots, or any archived version\n"
        f"- Do NOT enumerate all Algolia indices or test facet partitioning\n"
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
    product_url = state.get("product_url") or state.get("sample_url") or ""

    if not product_url:
        nav_findings = state.get("navigation_findings") or {}
        nav_analysis = state.get("navigation_analysis") or {}
        listing = nav_findings.get("listing_page", {})
        product_links = listing.get("product_links", [])

        candidates = []
        if product_links:
            candidates = [
                p.get("href", "") if isinstance(p, dict) else str(p)
                for p in product_links
            ]
        if not candidates:
            item_links = nav_analysis.get("item_links", {})
            for u in item_links.get("url_examples", []):
                if u and u not in candidates:
                    candidates.append(u)

        for candidate in candidates:
            from urllib.parse import urlparse

            parsed = urlparse(candidate)
            path = parsed.path.strip("/")
            path_parts = [p for p in path.split("/") if p]
            if path_parts and not candidate.endswith("/"):
                if len(path_parts) >= 2 or re.search(r"[A-Z]\d{5,}", path):
                    product_url = candidate
                    break

        if not product_url and candidates:
            product_url = candidates[0]
    if not product_url:
        product_url = "auto-discover"

    content_type_context = _build_content_type_context(state)

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
            f"http_method: {conn.get('http_method', 'none')}\n"
            f"browser_method: {conn.get('browser_method', 'none')}\n"
            f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
            f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
            f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
            f"platform: {probe_result.get('platform', 'unknown')}\n"
            f"```\n\n"
        )

    if has_verified_probe:
        access_strategy = (
            "### Page Access Strategy\n\n"
            "The page has already been probed (see Cached Probe Result above). "
            "**Do NOT call probe_page** — it would waste a tool call and return the same data.\n\n"
            "Use `playwright_browser_*` tools directly for deeper analysis (DOM inspection, "
            "additional selectors, network requests) if needed.\n\n"
        )
        workflow = (
            "### Workflow\n"
            "1. Read site_analysis.json (1 call)\n"
            "2. Map all fields from cached probe result — JSON-LD, selectors, meta tags\n"
            "3. Optionally use playwright_browser_* for additional selector testing (2-5 calls)\n"
            "4. write_file to save field mapping (1 call)\n\n"
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
            "### Workflow\n"
            "1. Read site_analysis.json (1 call)\n"
            "2. Call probe_page on the product URL (1 call)\n"
            "3. Map all fields from probe result — JSON-LD, selectors, meta tags\n"
            "4. Optionally use playwright_browser_evaluate for additional selector testing (2-5 calls)\n"
            "5. write_file to save field mapping (1 call)\n\n"
        )

    # Re-map mode: code_tester flagged a MAPPING failure → focus on the failed
    # fields instead of redoing the whole analysis. The pipeline routes here from
    # route_after_testing when test_report.remediiation.target == "mapping".
    remap_context = ""
    test_report = state.get("test_report") or {}
    remediation = test_report.get("remediation") if isinstance(test_report, dict) else None
    failed_fields: list[str] = []
    if isinstance(remediation, dict) and remediation.get("target") == "mapping":
        failed_fields = [f for f in (remediation.get("fields") or []) if isinstance(f, str)]
        fields_str = ", ".join(failed_fields) or "(unspecified)"
        remap_context = (
            f"\n### CRITICAL — RE-MAP FAILED FIELDS (mapping-failure recovery)\n"
            f"code_tester ran the generated scraper and these required fields FAILED because their "
            f"MAPPING in product_analysis.json is missing/wrong/unverified:\n"
            f"  **{fields_str}**\n"
            f"Steps:\n"
            f"1. Read `workspace/{slug}/test_report.json` (why each field failed).\n"
            f"2. Read `workspace/{slug}/product_analysis.json` (the current mapping).\n"
            f"3. Re-probe the product page and re-map ONLY the failed fields with corrected "
            f"selectors/methods — try JSON-LD, the backend API (see hint below), or the rendered "
            f"DOM. Keep the `expectations` blocks; set `tested: true` with a real example.\n"
            f"4. Write product_analysis.json back (full file).\n"
            f"Do NOT redo fields that already work. Spend your budget on the failed fields.\n\n"
        )

    # Backend product/listing API hint — captured by navigate_explore. Generic:
    # let the agent decide if the API covers the fields it needs.
    api_hint = ""
    nav_analysis = state.get("navigation_analysis") or {}
    api_endpoint = nav_analysis.get("api_endpoint") if isinstance(nav_analysis, dict) else None
    api_endpoint = api_endpoint if isinstance(api_endpoint, dict) else {}
    if api_endpoint.get("url"):
        api_hint = (
            f"\n### Backend API Hint (may contain the fields you need)\n"
            f"navigate_explore captured this backend API on the site:\n"
            f"- URL: `{api_endpoint.get('url')}`\n"
            f"- method: {api_endpoint.get('method', 'GET')}, pagination: "
            f"{api_endpoint.get('pagination_param', '?')}\n"
            f"Test whether this API returns the data for the fields you're mapping (title, price, "
            f"availability, etc.). If it does, prefer mapping from the API (structured JSON) over "
            f"DOM selectors — it's more reliable. If it doesn't cover a field, use selectors.\n\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Building a scraper for {url}. The scraper reads URLs "
        f"from `input_urls.json` and extracts data from each page.\n\n"
        f"{content_type_context}"
        f"## Your Task: Content Field Mapping\n\n"
        f"Critically review the site analysis, then analyze the **page** "
        f"below to map every extractable field with exact selectors.\n\n"
        f"**Page URL (analyze this page):** {product_url}\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Save artifact to:** workspace/{slug}/product_analysis.json\n"
        f"{remap_context}"
        f"{api_hint}"
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
        f"- Do NOT revisit sections you've already analyzed\n"
        f"- Do NOT guess or probe random URLs — only analyze the product URL provided above\n"
        f"- Do NOT probe category pages or site sections unrelated to the product URL\n\n"
        f"**CRITICAL: You MUST call write_file to save the field mapping as JSON to "
        f"workspace/{slug}/product_analysis.json as your LAST action. "
        f"Do NOT just print the analysis as text.**"
    )
    return [HumanMessage(content=content)]


def build_navigation_agent_message(state: dict) -> list:
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    input_mode = state.get("input_mode", "navigation")
    search_criteria = state.get("search_criteria", "")

    content_type_context = _build_content_type_context(state)

    probe_result = state.get("probe_result")
    connectivity_section = ""
    if probe_result and probe_result.get("connectivity"):
        conn = probe_result["connectivity"]
        connectivity_section = (
            f"\n### Pre-verified Connectivity (from site_analyzer)\n"
            f"```\n"
            f"method_that_worked: {conn.get('method_that_worked', 'unknown')}\n"
            f"http_method: {conn.get('http_method', 'none')}\n"
            f"browser_method: {conn.get('browser_method', 'none')}\n"
            f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
            f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
            f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
            f"```\n"
            f"Use this connectivity method for all page access. Do NOT call probe_page.\n\n"
        )

    mode_section = ""
    if input_mode == "list_page":
        sample_url = state.get("sample_url") or state.get("product_url") or ""
        mode_section = (
            f"\n### Input Mode: List Page Analysis\n"
            f"The user has provided a listing page URL. Analyze THIS page:\n"
            f"**Listing page URL:** {sample_url}\n\n"
            f"Focus on:\n"
            f"- Item link pattern (how to extract content page URLs from this listing)\n"
            f"- Pagination (how to get more pages)\n"
            f"- Skip search and category analysis — the user already has the listing page\n\n"
        )
    else:
        mode_section = (
            f"\n### Input Mode: Navigation Analysis\n"
            f'**Search criteria:** "{search_criteria}"\n\n'
            f"Analyze:\n"
            f"- Search functionality (search box, URL-based search, or both)\n"
            f"- Category navigation (menus, dropdowns, category links)\n"
            f"- Pagination (next button, page params, infinite scroll)\n"
            f"- Item link patterns from search/category results\n\n"
        )

    # Skills reuse: always tell the agent to load navigation-patterns first — it
    # carries reusable patterns + `## Learned:` sections captured from prior sites
    # by nav_skill_review (e.g. ASP.NET POST-to-session-id job boards). This closes
    # the learn→reuse loop.
    skills_section = (
        "\n### Reuse captured patterns (Skills)\n"
        "BEFORE exploring from scratch, call `load_skill(\"navigation-patterns\")`. "
        "Its `## Learned:` sections record reusable patterns from prior sites "
        "(e.g. ASP.NET MVC job boards that POST to a `/SearchResults?sId=...` page). "
        "If a Learned pattern matches this site, apply it directly. Also `list_skills` "
        "and load any platform-specific skill that fits.\n\n"
    )

    # Handoff context: when the deterministic navigate_explore couldn't drive a
    # form, it stashes its partial findings + a handoff_reason. Tell the agent what
    # was already tried so it doesn't repeat the dead-end.
    handoff_section = ""
    if state.get("handoff_reason") or state.get("navigation_findings"):
        handoff_section = (
            "\n### Handed off from the deterministic explorer\n"
            f"The fast deterministic explorer ran first and got stuck. READ "
            f"`workspace/{slug}/navigation_findings.json` to see what it already tried "
            f"(homepage nav, the search form it found, category links, backend endpoints) "
            f"and continue from there — don't repeat its work.\n"
            f"**Handoff reason:** {state.get('handoff_reason', 'form_driving_needed')}\n\n"
            "### Form-driving mechanics discovery (SCOPE-LIMITED)\n"
            "You are discovering HOW the form works so code_writer can iterate it at scale "
            "— you are NOT doing bulk extraction.\n"
            "- The deterministic explorer usually fails because: (a) it clicked a decorative "
            "submit button OUTSIDE the form instead of the form's real `<input type=submit>` "
            "INSIDE it, or (b) the form has a required field (often Specialty) enforced by "
            "client-side validation whose rule lives in a JS bundle, not the HTML.\n"
            "- Drive ONE successful form submission: fill EVERY `<select>`/input (use "
            "`playwright_browser_evaluate` to set values + dispatch change events so the "
            "validation library sees them), click the form's OWN submit input (inside "
            "`<form>`), and if submit is blocked, read the validation message and satisfy it.\n"
            "- CAPTURE the result: the results page URL (often a redirect to "
            "`/SearchResults?sId=...`) AND any AJAX endpoint that carries the items "
            "(`playwright_browser_network_requests`). The downstream scraper will replay this.\n"
            "- STOP once you have a working results URL / AJAX endpoint + a sample item link. "
            "Do NOT iterate all dropdown combinations — that is code_writer's job.\n\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Analyze the navigation patterns of {url} to enable a self-navigating scraper.\n\n"
        f"{content_type_context}"
        f"## Your Task: Navigation Pattern Analysis\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Input mode:** {input_mode}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Save artifact to:** workspace/{slug}/navigation_analysis.json\n"
        f"{connectivity_section}"
        f"{mode_section}"
        f"{skills_section}"
        f"{handoff_section}"
        f"### Workflow\n"
        f"1. `load_skill(\"navigation-patterns\")` — apply any matching Learned pattern (1 call)\n"
        f"2. Read site_analysis.json (1 call); if handed off, also read navigation_findings.json\n"
        f"3. Navigate to the site homepage or listing page (1 call)\n"
        f"4. Explore navigation patterns: search, categories, pagination, item links (5-15 calls)\n"
        f"5. Write navigation_analysis.json (1 call)\n\n"
        f"### BUDGET: {'40' if input_mode == 'navigation' else '20'} tool calls maximum.\n\n"
        f"### WRITE EARLY — CRITICAL\n"
        f"Write navigation_analysis.json as soon as you have the key patterns "
        f"(search + item_links minimum). Overwrite later if you find more.\n\n"
        f"### STRICT JSON — CRITICAL\n"
        f"The file MUST be strict, parseable JSON. No `...`/ellipsis placeholders, "
        f"no `// comments`, no trailing commas, no unquoted keys. If a list is long, "
        f"write the first 10 REAL entries and stop — never write `...` as a stand-in. "
        f"Validate it parses before finishing.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT collect individual content URLs — analyze patterns only\n"
        f"- Do NOT scrape content from individual pages\n"
        f"- Do NOT call probe_page — use connectivity info from site_analysis\n"
        f"- Do NOT crawl more than 2-3 pages\n"
        f"- Do NOT explore related search terms beyond the given criteria\n"
        f"- Do NOT write scraper code\n\n"
        f"**CRITICAL: You MUST call write_file to save the analysis as JSON to "
        f"workspace/{slug}/navigation_analysis.json. Do NOT just print the analysis as text.**"
    )
    return [HumanMessage(content=content)]


def build_navigation_synthesize_message(state: dict) -> list:
    """Build the prompt for the navigation synthesis agent.

    This agent reads raw findings (from navigate_explore) and produces
    the structured navigation_analysis.json. It has NO browser/web tools —
    it can only read files and write files.
    """
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    search_criteria = state.get("search_criteria", "")
    input_mode = state.get("input_mode", "navigation")

    content = (
        f"## OBJECTIVE\n"
        f"Convert raw navigation exploration data into structured navigation_analysis.json.\n\n"
        f"## Context\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Input mode:** {input_mode}\n"
        f"**Search criteria:** {search_criteria}\n\n"
        f"## Your Task\n\n"
        f"You have TWO files to read:\n"
        f"1. `workspace/{slug}/navigation_findings.json` — raw data extracted by the "
        f"deterministic explorer (category links, search form, pagination, item links)\n"
        f"2. `workspace/{slug}/site_analysis.json` — site platform info, connectivity, "
        f"product URL patterns\n\n"
        f"Read both files, then **write** `workspace/{slug}/navigation_analysis.json` "
        f"with this exact structure:\n\n"
        f"```json\n"
        f"{{\n"
        f'  "discovery_method": "search | category | url_pattern",\n'
        f'  "search": {{\n'
        f'    "has_search": true/false,\n'
        f'    "input_selector": "CSS selector for search input",\n'
        f'    "submit_selector": "CSS selector for submit button",\n'
        f'    "url_pattern": "URL pattern with {{query}} placeholder",\n'
        f'    "has_url_search": true/false,\n'
        f'    "search_url_pattern": "/search?q={{query}}",\n'
        f'    "working_url": "ACTUAL URL where products were found (from listing_page.url in findings)"\n'
        f"  }},\n"
        f'  "categories": {{\n'
        f'    "menu_selector": "CSS selector for category menu",\n'
        f'    "category_links": ["url1", "url2"],\n'
        f'    "url_patterns": ["/category/{{slug}}"]\n'
        f"  }},\n"
        f'  "pagination": {{\n'
        f'    "type": "next_button | page_param | infinite_scroll | load_more",\n'
        f'    "next_button_selector": "CSS selector",\n'
        f'    "page_param_name": "page | pnum | p",\n'
        f'    "max_pages": null,\n'
        f'    "total_count_selector": "CSS selector for item count text"\n'
        f"  }},\n"
        f'  "item_links": {{\n'
        f'    "container_selector": "CSS selector for item grid container",\n'
        f'    "link_selector": "CSS selector for item links",\n'
        f'    "url_pattern": "URL pattern for items",\n'
        f'    "url_examples": ["url1", "url2"]\n'
        f"  }}\n"
        f"}}\n"
        f"```\n\n"
        f"## Rules\n\n"
        f"- READ the findings file FIRST (1 call)\n"
        f"- READ site_analysis.json if you need platform/URL info (1 call)\n"
        f"- WRITE navigation_analysis.json (1 call)\n"
        f"- That's 2-3 calls total. Do NOT do anything else.\n"
        f"- If the findings have 0 category links AND 0 product links, the "
        f"exploration FAILED. Write discovery_method: 'failed' and leave all "
        f"selectors and url_examples as empty strings. Do NOT fabricate URLs, "
        f"selectors, or platform-specific details. Empty is better than wrong.\n"
        f"- Only include url_examples that appear verbatim in the findings JSON. "
        f"Never invent URLs or selectors that are not grounded in the data.\n"
        f"- Choose `discovery_method` based on what's available: prefer 'search' if "
        f"the site has a working search and criteria was provided; use 'category' if "
        f"categories were found; use 'failed' if both are empty.\n"
        f"- Check the top-level `search_attempted` field in navigation_findings.json. "
        f"If it is `true`, the explorer tried searching even if `homepage_nav.search_form` "
        f"is null or absent. In that case, set `search.has_search: true` and "
        f"`search.has_url_search: true`.\n"
        f"- **CRITICAL: working_url.** Read `listing_page.url` from navigation_findings.json. "
        f"If it exists AND `listing_page.product_links` is non-empty, set `search.working_url` "
        f"to that exact URL. This is the actual browser URL where products were found — it is "
        f"more reliable than the `url_pattern` or `search_url_pattern` (which are guessed from "
        f"the homepage form's action attribute). Downstream agents use `working_url` as the "
        f"authoritative search URL.\n"
        f"- For selectors, use the most specific CSS selector you can derive from the "
        f"raw data (parent classes, element types, attributes). If no data, leave empty.\n\n"
        f"**You MUST call write_file to save the output. Do NOT just print the JSON as text.**\n"
    )
    return [HumanMessage(content=content)]


def build_nav_skill_review_message(state: dict) -> list:
    """Build the initial HumanMessage for the nav-skill-review agent.

    This agent reads raw navigation findings, compares them against existing
    skills, and auto-applies reusable learnings by appending ``## Learned:``
    sections to skill files. It runs after navigation_synthesize and is
    non-blocking (failures don't halt the pipeline).
    """
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    platform = state.get("platform", "custom")

    content = (
        f"## OBJECTIVE\n"
        f"Review navigation findings for {url} against existing skills and "
        f"auto-apply any new reusable patterns.\n\n"
        f"## Your Task: Navigation Skill Review\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Detected platform:** {platform}\n\n"
        f"## Files to Read\n"
        f"1. `workspace/{slug}/navigation_findings.json` — raw explorer data "
        f"(category links, search form, pagination, item links, platform signals)\n"
        f"2. `workspace/{slug}/site_analysis.json` — platform info, connectivity\n"
        f"3. `workspace/{slug}/navigation_analysis.json` — structured analysis "
        f"from synthesize (optional, for reference)\n\n"
        f"## Workflow\n"
        f"1. READ navigation_findings.json (1 call)\n"
        f"2. READ site_analysis.json (1 call)\n"
        f"3. LIST existing skills (1 call)\n"
        f"4. LOAD 'navigation-patterns' skill — this is the PRIMARY skill to "
        f"compare against (1 call)\n"
        f"5. Optionally LOAD platform-specific skills if the site matches "
        f"(shopify-detection, sfcc-detection, etc.) — 0-2 calls\n"
        f"6. COMPARE findings against skills — identify 0-3 genuinely new patterns\n"
        f"7. APPLY learnings: for each new pattern, use edit_file to APPEND a "
        f"'## Learned:' section to the relevant skill. Use write_file ONLY to "
        f"create a new skill file (rare).\n"
        f"8. WRITE your report to workspace/{slug}/nav_learning_report.json "
        f"(1 call — your LAST action)\n\n"
        f"## BUDGET: 15 tool calls maximum.\n\n"
        f"## ⚠️ Safe Auto-Apply Rules\n"
        f"- You MAY append '## Learned: {{title}}' sections to existing skills\n"
        f"- You MUST NOT remove or modify existing skill content\n"
        f"- You MUST NOT overwrite entire skill files\n"
        f"- You MUST NOT modify YAML frontmatter (the --- block at top)\n"
        f"- When in doubt, append rather than create a new skill\n"
        f"- Quality over quantity — ZERO learnings is better than wrong ones\n\n"
        f"## When NOT to Apply\n"
        f"- Pattern already documented in any skill (check carefully!)\n"
        f"- Pattern is site-specific (e.g., a unique CSS class for one site)\n"
        f"- Pattern is trivial (e.g., standard <nav> links)\n"
        f"- Findings are incomplete (don't guess patterns from partial data)\n\n"
        f"## Output Format for nav_learning_report.json\n"
        f"```json\n"
        f"{{\n"
        f'  "site_slug": "{slug}",\n'
        f'  "site_url": "{url}",\n'
        f'  "platform": "{platform}",\n'
        f'  "review_timestamp": "ISO-8601",\n'
        f'  "patterns_reviewed": 0,\n'
        f'  "new_patterns_found": 0,\n'
        f'  "skills_updated": [],\n'
        f'  "skills_created": [],\n'
        f'  "patterns_skipped": [],\n'
        f'  "status": "applied|no_new_patterns"\n'
        f"}}\n"
        f"```\n\n"
        f"**CRITICAL: You MUST call write_file to save your report to "
        f"workspace/{slug}/nav_learning_report.json as your LAST action. "
        f"Even if you found no new patterns, write a report saying so.**"
    )
    return [HumanMessage(content=content)]


def build_scraper_analyzer_message(state: dict) -> list:
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")

    retry_section = ""
    scraper_draft_path = f"workspace/{slug}/scraper_draft.py"
    scraper_analysis_path = f"workspace/{slug}/scraper_analysis.json"
    if state.get("test_report"):
        retry_section = (
            f"\n{_summarize_test_report(state)}\n"
            f"Read the previous analysis at: {scraper_analysis_path}\n"
            f"Adjust strategy and proxy tier based on failures. Escalate ONE proxy tier.\n"
        )

    # Strategy cascade: strategies that already failed testing + why. The LLM must
    # pick a DIFFERENT strategy this time (the prior one provably can't reach the
    # content for this site). Generic — driven by the failure log, not site names.
    _tried = state.get("strategies_tried") or []
    cascade_section = ""
    if _tried:
        _lines = []
        for t in _tried:
            if isinstance(t, dict):
                _lines.append(f"  - `{t.get('strategy', '?')}`: {t.get('reason', 'failed')}")
            else:
                _lines.append(f"  - `{t}`: failed")
        cascade_section = (
            "\n### Strategies already tried + failed (DO NOT re-pick)\n"
            + "\n".join(_lines)
            + "\nPick a DIFFERENT strategy from the remaining options (http_requests, "
            "playwright, internal_api, shopify_api), guided by the analysis signals "
            "(anti_bot / js_rendering_needed / method_that_worked / api_endpoint / SSR). "
            "If the failed strategy was Playwright (timed out), prefer http_requests for "
            "SSR sites with a discoverable listing-page taxonomy. If http/api was blocked "
            "or empty, prefer Playwright. Do NOT regenerate the same failed strategy.\n"
        )

    navigation_context = state.get("navigation_analysis")
    nav_line = ""
    nav_read = ""
    if navigation_context:
        nav_line = "This job uses a two-phase navigation scraper. Read navigation_analysis.json for discovery patterns.\n"
        nav_read = "3. Read navigation_analysis.json (1 call)\n"

    # Anti-bot detection drives the cloak stealth path. CloakBrowser
    # (STEALTH_BROWSER=cloak, auto-applied at runtime by the run_scraper tool)
    # defeats Akamai at the C++ fingerprint level — stronger than SeleniumBase
    # UC's config-level stealth. So when anti-bot is present, the probe's
    # "Playwright blocked" finding (which tested VANILLA Playwright) is
    # superseded: prefer playwright+cloak over UC.
    _site_analysis = state.get("site_analysis") or {}
    _ab = _site_analysis.get("anti_bot") or {}
    _anti_bot = isinstance(_ab, dict) and bool(_ab.get("detected"))
    # Robust fallback: if the ONLY working access method is a stealth browser
    # (uc_chrome_* or cloak_*), vanilla browser/HTTP were blocked → anti-bot is
    # present even if the probe didn't raise an explicit flag. CloakBrowser
    # defeats this where UC is fragile.
    _conn = _site_analysis.get("connectivity") or {}
    _method = (_conn.get("method_that_worked") or "") if isinstance(_conn, dict) else ""
    if not _anti_bot and (_method.startswith("uc_chrome") or _method.startswith("cloak")):
        _anti_bot = True
    # When anti-bot is detected, route uc_chrome_* → playwright (cloak) instead of
    # seleniumbase_uc. CloakBrowser (STEALTH_BROWSER=cloak, applied to the Playwright
    # launch at runtime) defeats Akamai at the C++ fingerprint level — stronger than
    # SeleniumBase UC's config-level stealth, which Akamai can still detect.
    if _anti_bot:
        _uc_strategy_mapping = (
            f"   - `uc_chrome_none` → **`playwright`** (cloak), proxy `none`\n"
            f"   - `uc_chrome_datacenter` → **`playwright`** (cloak), proxy `datacenter`\n"
            f"   - `uc_chrome_residential` → **`playwright`** (cloak), proxy `residential`\n"
            f"     (ANTI-BOT/AKAMAI: CloakBrowser stealth is applied to Playwright via "
            f"STEALTH_BROWSER=cloak at runtime — stronger than UC. The probe's "
            f"'Playwright blocked' note referred to VANILLA Playwright and is superseded.)\n"
        )
    else:
        _uc_strategy_mapping = (
            f"   - `uc_chrome_none` → `seleniumbase_uc`, proxy `none`\n"
            f"   - `uc_chrome_datacenter` → `seleniumbase_uc`, proxy `datacenter`\n"
            f"   - `uc_chrome_residential` → `seleniumbase_uc`, proxy `residential`\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Read upstream analyses and determine the scraping strategy for {url}.\n\n"
        f"## Your Task: Strategy Analysis\n\n"
        f"**Site slug:** {slug}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Save to:** workspace/{slug}/scraper_analysis.json\n\n"
        f"{nav_line}{retry_section}{cascade_section}"
        f"### Workflow\n"
        f"1. Read site_analysis.json (1 call)\n"
        f"2. Read product_analysis.json (1 call)\n"
        f"{nav_read}"
        f"3. Determine strategy from `connectivity.method_that_worked`:\n"
        f"   - `direct_http` → `http_requests`, proxy `none`\n"
        f"   - `browser_none` → `playwright`, proxy `none`\n"
        f"{_uc_strategy_mapping}"
        f"   - SPA detected → MUST use browser strategy, NOT http_requests\n"
        f"4. write_file to save scraper_analysis.json (1 call)\n\n"
        f"### BUDGET: 8 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT probe the site — site_analyzer already confirmed connectivity\n"
        f"- Do NOT fetch pages or test selectors\n"
        f"- Do NOT override site_analysis proxy findings without evidence\n"
        f"- Do NOT generate scraper code\n"
        f"- Do NOT read or modify input_urls.json\n\n"
        f"**CRITICAL: You MUST call write_file to save your analysis as JSON to "
        f"workspace/{slug}/scraper_analysis.json.**"
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
        "- Do NOT read an existing input_urls.json from a previous run — write a fresh one\n"
        "- Do NOT add discovered/related product URLs to input_urls.json — "
        "only include the URL(s) provided by the user\n"
        "- input_urls.json MUST contain ONLY the Product URL(s) listed above, "
        "NOT discovered links\n"
    )


def _summarize_test_report(state: dict) -> str:
    report = state.get("test_report")
    if not report:
        return ""
    assessment = report.get("overall_assessment", "UNKNOWN")
    try:
        confidence = float(report.get("confidence_score", 0.0))
    except (ValueError, TypeError):
        confidence = 0.0
    issues = report.get("issues", [])
    retry_count = state.get("test_retry_count", 0)
    if retry_count == FINAL_RETRY_SENTINEL:
        retry_label = "FINAL ATTEMPT"
    else:
        retry_label = f"Retry Cycle {retry_count + 1}"

    lines = [
        f"### Previous Test Results ({retry_label})",
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
    # Fix A: surface the RAW error so code_writer makes a targeted fix instead of
    # regenerating from scratch (which reintroduces variance).
    strategy_error = report.get("strategy_error") if isinstance(report, dict) else None
    crash_error = report.get("crash_error") if isinstance(report, dict) else None
    if strategy_error:
        lines.append(
            f"\n**⚠️ STRATEGY MISMATCH — rewrite using the correct strategy:**\n"
            f"{strategy_error}\n"
            f"Rewrite using the correct strategy; do NOT keep the wrong approach."
        )
    elif crash_error:
        lines.append(
            "\n**⚠️ THE SCRAPER CRASHED — make a MINIMAL, targeted fix for THIS error "
            "(do NOT rewrite from scratch — that reintroduces variance):**\n"
            f"```\n{str(crash_error)[:1500]}\n```"
        )
    if retry_count > 0 and retry_count != FINAL_RETRY_SENTINEL:
        lines.append(f"\n*{retry_count} previous attempt(s) failed.*")
    elif retry_count == FINAL_RETRY_SENTINEL:
        lines.append("\n*This is the FINAL retry attempt based on user feedback. "
                      "If this does not pass, the job will end.*")
    return "\n".join(lines)


def build_code_writer_message(state: dict) -> list:
    """Build the initial HumanMessage for the code-writer agent.

    On retry cycles, includes the test report so the agent can apply fixes.
    """
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or state.get("sample_url") or ""
    site_analysis = state.get("site_analysis") or {}
    scraper_analysis = state.get("scraper_analysis") or {}
    mechanism = scraper_analysis.get("strategy") or site_analysis.get(
        "scraping_mechanism", ""
    )
    # When anti-bot is detected, stealth is applied at runtime via CloakBrowser
    # (a Playwright backend, injected when STEALTH_BROWSER=cloak).  Normalize a
    # UC-style mechanism to "playwright" so code_writer gets ONE consistent
    # instruction set (the cloak note: use p.chromium.launch()) instead of
    # CONFLICTING guidance (seleniumbase_section saying "MUST use SeleniumBase"
    # vs cloak note saying "do NOT use UC/Selenium").  Non-anti-bot sites that
    # genuinely need SeleniumBase are unaffected.  Generic. [code_writer clarity]
    try:
        from agents.tools.context import is_anti_bot_detected

        if is_anti_bot_detected() and mechanism in (
            "seleniumbase_uc", "undetected_chromedriver", "stealth_browser", "uc_chrome",
        ):
            mechanism = "playwright"
    except Exception:
        pass
    algolia = site_analysis.get("algolia", {})
    site_input_urls = state.get("input_urls") or []

    content_type_context = _build_content_type_context(state)
    output_schema = state.get("output_schema", {})
    _template_family = ""
    if output_schema and "template_family" in output_schema:
        _template_family = output_schema["template_family"]

    provided_urls_section = ""
    if site_input_urls:
        count = len(site_input_urls)
        provided_urls_section = (
            f"\n### PROVIDED URLs (FROM SITE MODEL)\n"
            f"The user has provided {count} URLs via the Site configuration. "
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

    human_feedback_section = ""
    human_feedback = state.get("human_feedback", "")
    if human_feedback:
        human_feedback_section = (
            f"\n\n### User Feedback (from approval)\n"
            f"The user provided this feedback after reviewing the failed test:\n"
            f"> {human_feedback}\n\n"
            f"Address this feedback in your fix. The user's insight may identify "
            f"the root cause that automated testing missed.\n"
        )

    # code_reviewer feedback: the read-only reviewer found these issues in the
    # previous draft. Fix them (and don't reintroduce them). [code_reviewer loop]
    review_feedback_section = ""
    review_feedback = state.get("review_feedback", "")
    if review_feedback:
        review_feedback_section = (
            f"\n\n### Code Review Feedback (MUST FIX — from code_reviewer)\n"
            f"The code reviewer read your previous scraper_draft.py and found these issues. "
            f"Fix each one:\n{review_feedback}\n\n"
            f"Address every issue; do not reintroduce them.\n"
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
    input_mode = (state.get("input_mode") or "").lower()
    template_file = ""
    # Navigation-style jobs (discovery required) use the two-phase navigation
    # template — but ONLY for BROWSER access strategies. API/HTTP strategies
    # (e.g. AMN's backend-API discovery, method=http_requests) keep their own
    # single-phase template. Stealth for browser jobs is applied at runtime via
    # STEALTH_BROWSER (cloak), NOT by switching to the UC template.
    _browser_strategies = (
        "playwright", "stealth_browser", "undetected_chromedriver",
        "seleniumbase_uc", "browser", "uc_chrome", "",
    )
    if input_mode in ("navigation", "list_page", "search_term") and mechanism in _browser_strategies:
        template_file = "navigation_scraper.py"
    elif mechanism:
        template_file = f"{mechanism}_scraper.py"
        if mechanism in (
            "stealth_browser",
            "undetected_chromedriver",
            "seleniumbase_uc",
        ):
            template_file = "undetected_chromedriver_scraper.py"
    if template_file:
        _cloak_note = ""
        try:
            from agents.tools.context import is_anti_bot_detected

            if is_anti_bot_detected():
                _cloak_note = (
                    "\n**STEALTH:** This site uses anti-bot/Akamai protection. Stealth "
                    "is handled AUTOMATICALLY at runtime — browser_service's cloak_stealth "
                    "patch wraps Playwright's launch() to drive CloakBrowser's stealth "
                    "Chromium (C++ fingerprint patches + build_args + ignore_default_args) "
                    "when STEALTH_BROWSER=cloak is set. **Just use a normal "
                    "`p.chromium.launch()`** inside `with sync_playwright() as p:` — do NOT "
                    "call `cloakbrowser.launch()` directly (it starts a second Playwright and "
                    "crashes), do NOT swap to UC/Selenium, do NOT add anti-bot workarounds. "
                    "Use the `playwright` strategy.\n"
                    "**ANTI-BOT PLAYBOOK (both phases via the cloak browser):**\n"
                    "- **Discovery (Phase 1):** render the search/category URL "
                    "(`navigation_analysis.search.working_url`) via `page.goto(url, "
                    "wait_until='domcontentloaded')` + a short wait; then extract product "
                    "links from the RENDERED DOM with `page.eval_on_selector_all('a[href]', "
                    "'els => els.map(e => e.href)')`. Keep every SAME-DOMAIN link, then DROP "
                    "obvious non-product links (nav/category/account/help). Verify "
                    "`len(product_urls) > 0` before Phase 2. Do NOT use a backend HTTP API "
                    "for discovery on an anti-bot site (likely protected: HTTP 400/403).\n"
                    "- **Extraction (Phase 2):** for each product URL, render via cloak + "
                    "read JSON-LD (`<script type=\"application/ld+json\">` → Product schema: "
                    "Offers.price / priceCurrency / availability). Cloak renders JSON-LD "
                    "reliably.\n"
                )
        except Exception:
            pass
        template_hint = (
            f"\n### Template\nRead the template at: templates/{template_file} "
            f"and use it as your base. The scraper will run on a dedicated worker "
            f"container that has Chrome, SeleniumBase, Playwright, and CloakBrowser "
            f"installed.{_cloak_note}"
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
            for field_name, field_info in verified.items() if isinstance(verified, dict) else []:
                if isinstance(field_info, str):
                    lines.append(f"  - {field_name}: {field_info}")
                    continue
                if not isinstance(field_info, dict):
                    lines.append(f"  - {field_name}: {field_info}")
                    continue
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

        seleniumbase_section = ""
        if mechanism in (
            "stealth_browser",
            "undetected_chromedriver",
            "seleniumbase_uc",
        ):
            seleniumbase_section = (
                "\n### SeleniumBase UC Mode — MANDATORY API Constraints\n"
                "The scraper MUST use SeleniumBase with UC Mode. Follow these rules EXACTLY:\n\n"
                "**SB() constructor — ONLY valid kwargs (SeleniumBase 4.44+):**\n"
            "```python\n"
            "with SB(uc=True, xvfb=args.xvfb, locale_code='en-gb') as sb:\n"
            "    driver = sb.driver\n"
            "```\n"
            "Valid kwargs: `uc`, `xvfb`, `locale_code`, `proxy`, `chromium_arg`, "
            "`page_load_strategy`, `driver_type`, `extension_zip`, `extension_dir`, "
            "`use_auto_ext`\n"
            "The run_scraper tool auto-injects `--xvfb` CLI flag. "
            "Your argparse MUST accept `--xvfb` (action='store_true') and use "
            "`args.xvfb` in SESSION_KWARGS — otherwise argparse rejects the flag and the scraper crashes.\n\n"
            "**INVALID kwargs (NEVER use):** `browser_args` (wrong → use `chromium_arg`), "
            "`chrome_args` (wrong → use `chromium_arg`), "
            "`headless=True` with `uc=True` (unreliable → use `xvfb=True`), "
            "`driver_kwargs` (doesn't exist)\n\n"
            "**Proxy with auth:** Use `extension_zip=/path/to/auth.zip` kwarg "
            "(NOT `use_auto_ext` — that enables Chrome's built-in automation extension). "
            "The template already has `_make_proxy_auth_extension()` — call it and pass "
            "the result as `extension_zip`. Use `chromium_arg` for `--proxy-server` flag.\n\n"
                "**Page navigation — ALWAYS use driver.uc_open_with_reconnect():**\n"
                "```python\n"
                "driver.uc_open_with_reconnect(url, reconnect_time=4)\n"
                "time.sleep(3)\n"
                "```\n"
                "Do NOT use `sb.open()` — it triggers EMPTY_PAGE_BLOCK detection that kills the session.\n\n"
                "**JS execution — ALWAYS use driver.execute_script() directly:**\n"
                "```python\n"
                "data = driver.execute_script('return document.title')\n"
                "```\n"
                "Do NOT use `sb.execute_script()` (CDP Mode limitations) or "
                "`sb.driver.execute_script()` (can crash CDP). "
                "Just `driver.execute_script()` — it's the raw WebDriver API.\n\n"
                "**Pattern summary:**\n"
            "```python\n"
            "with SB(uc=True, xvfb=args.xvfb) as sb:\n"
                "    driver = sb.driver\n"
                "    driver.uc_open_with_reconnect(url, reconnect_time=4)\n"
                "    time.sleep(3)\n"
                "    data = driver.execute_script('return document.title')\n"
                "```\n"
            )

        scraper_analysis_section = (
            f"\n### Scraper Analysis (VERIFIED — follow these instructions)\n"
            f"**Strategy:** {mechanism}\n"
            f"**Proxy tier:** {proxy_tier}\n"
            f"**Strategy justification:** {scraper_analysis.get('strategy_justification', '')}\n"
            f"{proxy_instructions}{approach_section}{verified_section}{extras_section}"
            f"{seleniumbase_section}"
        )

    navigation_section = ""
    navigation_analysis = state.get("navigation_analysis") or {}
    # SOURCE FIX: navigation_synthesize (LLM) sometimes drops the product URLs
    # that navigation_explore discovered. Merge them from navigation_findings.json
    # HERE — at the point code_writer consumes the analysis. This ensures
    # code_writer has the actual product URLs to build the scraper around.
    if navigation_analysis:
        try:
            import json as _json_nf, os as _os_nf
            _slug = state.get("site_slug", "")
            _nf_path = _os_nf.join(settings.PROJECT_ROOT, "workspace", _slug, "navigation_findings.json")
            if _os_nf.isfile(_nf_path):
                _nf = _json_nf.load(open(_nf_path))
                _lp = _nf.get("listing_page") or {}
                _purls_raw = _lp.get("product_links") or []
                _purls = []
                for u in _purls_raw:
                    if isinstance(u, str):
                        _purls.append(u)
                    elif isinstance(u, dict) and u.get("href"):
                        _purls.append(u["href"])
                if _purls:
                    _il = navigation_analysis.get("item_links")
                    if not isinstance(_il, dict):
                        _il = {}
                    _existing = [u for u in (_il.get("urls") or []) if isinstance(u, str)]
                    if len(_existing) < len(_purls):
                        _il["urls"] = list(dict.fromkeys(_existing + _purls))
                        navigation_analysis["item_links"] = _il
                        logger.info("build_code_writer_message: merged %d product URLs from findings → nav_analysis", len(_purls))
        except Exception:
            pass
        input_mode = state.get("input_mode", "url_list")
        discovery = navigation_analysis.get("discovery_method", "unknown")
        search_info = navigation_analysis.get("search", {})
        pagination_info = navigation_analysis.get("pagination", {})
        item_links_info = navigation_analysis.get("item_links", {})
        search_criteria = state.get("search_criteria", "")

        nav_lines = [
            "\n### Navigation Analysis (TWO-PHASE SCRAPER REQUIRED)\n",
            f"**Discovery method:** {discovery}",
            f"**Input mode:** {input_mode}",
        ]

        if search_info.get("has_search") or search_info.get("has_url_search"):
            nav_lines.append("**Search:** supported")
            working_search_url = search_info.get("working_url") or search_info.get("listing_url_used")
            if working_search_url:
                nav_lines.append(f"  - **Working search URL (USE THIS):** `{working_search_url}`")
            if search_info.get("url_pattern") and search_info.get("url_pattern") != working_search_url:
                nav_lines.append(f"  - URL pattern (from form action — may be WRONG): `{search_info['url_pattern']}`")
            if search_info.get("search_url_pattern") and search_info.get("search_url_pattern") != working_search_url:
                nav_lines.append(f"  - Search URL pattern (may be WRONG): `{search_info['search_url_pattern']}`")
            if search_info.get("input_selector"):
                nav_lines.append(f"  - Search input: `{search_info['input_selector']}`")
            if search_criteria:
                nav_lines.append(f'  - Search criteria: "{search_criteria}"')

        if pagination_info.get("type"):
            nav_lines.append(f"**Pagination:** {pagination_info['type']}")
        elif pagination_info and any(pagination_info.values()):
            nav_lines.append("**Pagination:** detected (type not specified)")
            if pagination_info.get("next_button_selector"):
                nav_lines.append(
                    f"  - Next button: `{pagination_info['next_button_selector']}`"
                )
            if pagination_info.get("next_text"):
                nav_lines.append(
                    f"  - Next button text: \"{pagination_info['next_text']}\""
                )
            if pagination_info.get("next_href"):
                nav_lines.append(
                    f"  - Next href: `{pagination_info['next_href']}`"
                )
            if pagination_info.get("page_param_name"):
                nav_lines.append(
                    f"  - Page param: `{pagination_info['page_param_name']}`"
                )
            if pagination_info.get("max_pages"):
                nav_lines.append(f"  - Max pages: {pagination_info['max_pages']}")
            if pagination_info.get("note"):
                nav_lines.append(f"  - Note: {pagination_info['note']}")
            if pagination_info.get("page_indicator_text"):
                nav_lines.append(
                    f"  - Page indicator: \"{pagination_info['page_indicator_text']}\""
                )

        # Strong directive: paginate the FULL catalog (don't stop at page 1).
        nav_lines.append(
            "\n**DISCOVERY — paginate EVERY page (CRITICAL for full extraction):** "
            "Search/category pages usually show only 24-48 products each. You MUST "
            "follow pagination to the LAST page to discover the full catalog (often "
            "65-200+ products across multiple pages). Set `MAX_PAGES` high (e.g. 20) "
            "or null (unlimited), and keep paginating (next button / `?page=N` / "
            "scroll) until a page returns NO new product URLs. Stopping at page 1 "
            "misses most products — that is a discovery failure. The template's "
            "`_get_next_page_url` already has runtime fallbacks for unknown pagination "
            "(next-button selectors + `?page=N`); preserve that loop.\n"
        )

        if item_links_info.get("container_selector"):
            nav_lines.append(
                f"**Item links:** container `{item_links_info['container_selector']}` "
                f"→ link `{item_links_info.get('link_selector', 'a')}`"
            )
        if item_links_info.get("url_pattern"):
            nav_lines.append(f"  - URL pattern: `{item_links_info['url_pattern']}`")

        # Discovery mechanics the structured nav_lines above don't cover
        # (filters/POST-form strategy, search/category notes). Injected so
        # code_writer doesn't need to read_file navigation_analysis.json.
        nav_lines.append(_summarize_navigation_extras(navigation_analysis))
        nav_lines.append(
            "\n### Two-Phase Architecture (REQUIRED for this scraper)\n"
            "This scraper must implement TWO phases:\n\n"
            "**Phase 1: Discover item URLs (use navigation_analysis — do NOT re-discover)\n"
            "- Phase 1 MUST start from the listing URL in navigation_analysis.search\n"
        )

        working_first_url = search_info.get("working_url") or search_info.get("listing_url_used")

        if working_first_url:
            nav_lines.append(
                f"- First URL: `{working_first_url}` "
                f"(this is where {len(item_links_info.get('url_examples', []))} products were found)\n"
            )
        elif search_info.get("search_url_pattern"):
            nav_lines.append(
                f"- First URL: `{search_info['search_url_pattern']}` (replace `{{criteria}}` with `{search_criteria}`)\n"
            )
        elif search_info.get("url_pattern"):
            nav_lines.append(
                f"- First URL: `{search_info['url_pattern']}`\n"
            )

        if item_links_info.get("link_selector") and item_links_info.get("link_selector") != "a[href]":
            nav_lines.append(
                f"- Extract product links using selector: `{item_links_info['link_selector']}` "
                f"within container: `{item_links_info['container_selector']}`\n"
            )
        elif item_links_info.get("link_selector"):
            nav_lines.append(
                f"- Extract product links within container: `{item_links_info.get('container_selector', 'product grid')}`\n"
            )

        nav_lines.append(
            "- Paginate through all result pages (click 'next page' links, "
            "load more buttons, or scroll for infinite scroll)\n"
            "- Collect item page URLs (NOT content — just URLs)\n"
            "- Filter: only keep URLs matching the pattern from navigation_analysis\n"
            "- Store discovered URLs in a list\n\n"
            "**Phase 2: Scrape each item page**\n"
            "- For each discovered URL, extract field data\n"
            "- Map raw data to output fields\n"
            "- Write results to output file\n\n"
            "**CRITICAL:** Do NOT write your own discovery logic from scratch. "
            "The navigation_analysis has the exact URL and selectors that found "
            f"{len(item_links_info.get('url_examples', []))} products. Use them.\n"
        )

        _template_family = "navigation"
        nav_template_hint = (
            "\n### Template\nRead the template at: templates/navigation_scraper.py "
            "and use it as your base for the two-phase architecture. "
            "Adapt the Phase 1 (navigation) and Phase 2 (extraction) logic "
            "to match this site's patterns.\n"
        )

        navigation_section = "\n".join(nav_lines)

        # If navigate_explore captured a backend JSON search API (React/Vue SPA,
        # e.g. AMN Healthcare's /JobSearch), PREFER a clean HTTP api_scraper over
        # driving the browser.  The API returns fully-populated items, so the
        # browser two-phase discovery below is superseded.
        api_endpoint = navigation_analysis.get("api_endpoint") or {}
        api_section = ""

        if api_endpoint.get("url"):
            api_base = api_endpoint.get("base") or str(api_endpoint.get("url", "")).split("?")[0]
            api_params = api_endpoint.get("query_params") or []
            page_param = api_endpoint.get("pagination_param") or (
                "PageNumber" if "PageNumber" in api_params
                else ("page" if "page" in api_params else "page")
            )
            page_size_param = api_endpoint.get("page_size_param") or "PageSize"
            api_section = (
                "\n### CRITICAL — Backend JSON search API discovered (PREFERRED — do NOT drive a browser)\n"
                "The site renders listings client-side by calling a JSON search API, which "
                "navigate_explore captured from the browser's network calls. **Use this API "
                "directly with HTTP `requests` — do NOT use Playwright/Selenium, do NOT parse "
                "the DOM, and do NOT follow the two-phase browser discovery above.**\n"
                f"- **Endpoint:** `{api_endpoint.get('method', 'GET')} {api_base}`\n"
                f"- **Discovered query params:** {api_params}\n"
                "- The complete working URL (with every param + value) is in "
                "`navigation_analysis.api_endpoint.url` — READ it. But you do NOT need every "
                "captured param: many are **facet selectors** (e.g. repeated `FilterTypes=`) that "
                "only control which filter facets are RETURNED in the response, NOT which items "
                "match. **Use a MINIMAL URL** — just the search/location param + "
                f"`{page_param}` + `{page_size_param}` (+ orderby if present). Omit facet "
                "params entirely. A truncated/guessed facet value (e.g. `PayRateTyp` instead of "
                "`PayRateType`) causes HTTP 400, so dropping facets is the safe choice.\n"
                f"- **Pagination:** increment `{page_param}` starting at 1; use a large "
                f"`{page_size_param}` (e.g. 100) to minimize calls. Stop when "
                "`len(items) >= response total` — the total is in a key like `jobCount`, "
                "`totalCount`, `count`, or `total` (inspect the first response).\n"
                "- **Headers:** set a real browser `User-Agent`, `Accept: application/json`, "
                "and `Origin` + `Referer` matching the site.\n"
                "- **No proxy needed — this is a public API.** Send DIRECT requests "
                "(`proxies=None`); a datacenter/residential proxy is unnecessary and may be "
                "rejected. If a proxy helper is in the template, force the no-proxy/direct path.\n"
                "- **No auth/cookies/subscription key required.** If a request fails, re-check "
                "the param NAMES — do not add login or key logic.\n"
            )
            # Anti-bot caveat: the captured API may itself be protected (e.g. calvklein's
            # PVH API returns 400 directly). When anti-bot is detected, tell code_writer to
            # VERIFY the API + fall back to browser+cloak+JSON-LD if it fails. Generic.
            try:
                from agents.tools.context import is_anti_bot_detected

                if is_anti_bot_detected():
                    api_section += (
                        "\n**⚠️ ANTI-BOT SITE — VERIFY THE API, ELSE FALL BACK TO BROWSER.** "
                        "This site uses anti-bot protection, so the captured API may itself be "
                        "protected (returns 400/403/empty when called directly, without the "
                        "browser-warmed headers/cookies). **On the FIRST run, make a single test "
                        "request and check you actually get items back.** If the API returns 0 "
                        "items or an error, do NOT keep retrying it — SWITCH to rendering each "
                        "page with the cloak browser (`p.chromium.launch()`; stealth is applied "
                        "automatically via STEALTH_BROWSER=cloak) and extract fields from JSON-LD "
                        "(`<script type=\"application/ld+json\">` — Product schema → Offers.price / "
                        "priceCurrency / availability), which the cloak browser renders reliably. "
                        "The browser path is the reliable fallback for anti-bot sites.\n"
                    )
            except Exception:
                pass
            if (state.get("page_type") or "").lower().startswith("job"):
                api_section += (
                    "\n**This API returns FULLY-populated items — Phase 2 (per-detail scrape) "
                    "is NOT needed.** Map fields with the GENERIC resolver; do NOT hardcode any "
                    "site-specific field names (no `divisionCompany.companyName`, no `or`-chains):\n"
                    "- **Use `src/job_fields.py`.** `from src.job_fields import map_jobs` then "
                    "`jobs = map_jobs(sample_items=first_page, raw_items=all_items)`. It "
                    "auto-detects the source path for each standard job field (title, company, "
                    "location, description, salary, job_type, posted_date, apply_url, "
                    "requirements) by coverage over the sample — it handles nested objects, "
                    "composite location (city.name + state.abbrev -> 'City, ST'), salary ranges, "
                    "list-valued employmentType, and normalizes posted_date to ISO-8601. The "
                    "resolver picks whatever source is actually populated for THIS site, so the "
                    "same code works across job platforms without per-site edits.\n"
                    "- **Verify each field.** code_tester reports per-field coverage in "
                    "`results.field_coverage`. If a field shows `MISSING`/0% the resolver found "
                    "no populated candidate — if a real source exists that the alias table "
                    "misses, ADD it to `JOB_ALIASES` in `src/job_fields.py` (do not patch the "
                    "generated scraper). Never ship a core field at 0%.\n"
                    "- **Construct `url` when the API has none.** Many job APIs expose a job "
                    "ID but no direct URL (the posting is a SPA route). If `map_jobs` leaves "
                    "`url` empty, build it per item from the job ID using the job-link pattern "
                    "navigate_explore discovered (see `navigation_findings.json` product_links / "
                    "`navigation_analysis.item_links.url_pattern`, e.g. "
                    "`https://site/job-details/{jobID}/{slug}/`). Set this constructed URL on "
                    "each mapped job so the `url` core field is populated.\n"
                    f"- **Date filter (last 7 days):** there is usually NO server-side "
                    "posted-date param. Fetch ALL pages, then KEEP ONLY items whose `posted_date` "
                    "(normalized to ISO-8601 `YYYY-MM-DD` by the resolver) is within "
                    "`datetime.now(timezone.utc) - 7 days`.\n"
                    "- Add a `--query` arg defaulting to the target location (e.g. 'Alabama') "
                    "and feed it into the API's location query param. The captured URL may have "
                    "been taken from a category/browse page and OMIT the location param — the "
                    "site's location search box is in "
                    "`navigation_analysis.filters.location_filter` / findings `filter_ui.location_selectors`. "
                    "Add the location param and VERIFY it works: the response total should DROP "
                    "to only jobs in that location. Common param names to try (in order): "
                    "`LocationSearch`, `location`, `state`, `State`, `city`, `q`. Pair with a "
                    "radius/distance param if one appears in the captured URL (e.g. "
                    "`LocationDistance`).\n"
                )
            api_section += (
                "\n**Output:** write the filtered list to `output_{datetime}.json`.\n"
            )
            # The API loop replaces the browser navigation template.
            nav_template_hint = (
                "\n### Template\nRead templates/api_scraper.py as your base (HTTP + JSON). "
                "The entire scraper is ONE paginated API loop — no Playwright/browser.\n"
            )
            # Prepend so the API guidance is read first and supersedes the
            # generic two-phase browser text that follows.
            navigation_section = api_section + navigation_section

        # Filter requirements (date/location/category — job portals & search sites)
        filters_info = navigation_analysis.get("filters", {}) or {}
        # fmethod is set inside the has_filters block below, but referenced by
        # the form/classic-search check further down — initialise it so a
        # navigation job with NO detected filters doesn't NameError there.
        fmethod = filters_info.get("method") if filters_info.get("has_filters") else None
        if filters_info.get("has_filters"):
            fmethod = filters_info.get("method", "url")
            filter_lines = [
                "\n### Filter Requirements (apply during Phase 1 discovery)\n",
                f"This site supports result filtering via: **{fmethod}**\n",
            ]
            for label, key in [
                ("Date", "date_filter"),
                ("Location", "location_filter"),
                ("Category", "category_filter"),
            ]:
                fcfg = filters_info.get(key) or {}
                if not fcfg:
                    continue
                _strategy = fcfg.get("strategy", "")
                _sval = fcfg.get("strategy_value")
                _dval = fcfg.get("detected_value", "")
                _values = fcfg.get("values") or []
                detail = []
                if fcfg.get("param_name"):
                    detail.append(f"URL param `{fcfg['param_name']}`")
                if fcfg.get("url_pattern"):
                    detail.append(f"pattern `{fcfg['url_pattern']}`")
                if fcfg.get("selector"):
                    detail.append(f"form element `{fcfg['selector']}`")
                if fcfg.get("form_action") or fcfg.get("submit_button"):
                    detail.append(
                        f"submit form `{fcfg.get('form_id') or fcfg.get('form_action')}`"
                        f" (button `{fcfg.get('submit_button')}`)"
                    )
                # Strategy-driven filter guidance (replaces hardcoded "Alabama" etc.).
                # pin → use the specific value from the query; iterate → loop options + dedup.
                if _strategy == "pin" and _sval:
                    filter_lines.append(
                        f"- **{label}**: PIN to `{_sval}` ({fcfg.get('reason','')}). "
                        f"{', '.join(detail)}.\n"
                    )
                elif _strategy == "iterate" or (not _strategy and _dval in ("all", "any", "", None) and _values):
                    _opts = [
                        (v.get("v", "") if isinstance(v, dict) else v)
                        for v in _values
                    ]
                    _opts = [o for o in _opts if o and o not in ("all", "any")]
                    filter_lines.append(
                        f"- **{label}**: ITERATE over {len(_opts)} options "
                        f"({_opts[:12]}{'...' if len(_opts) > 12 else ''}). For each, "
                        f"build the URL via the pattern above, collect item links, and "
                        f"**dedup by job/item ID** across iterations. (detected_value="
                        f"'{_dval}'; query didn't specify this dimension → enumerate "
                        f"for full catalog.)\n"
                    )
                elif _strategy == "ignore":
                    continue  # don't mention irrelevant filters
                else:
                    # Fallback (older analysis without strategy): use detected_value,
                    # NOT a hardcoded default. Surface options for code_writer to decide.
                    filter_lines.append(
                        f"- **{label}**: use detected_value `{_dval}`. {', '.join(detail)}. "
                        f"Options: {_values[:8]}{'...' if len(_values) > 8 else ''}\n"
                    )
            filter_lines.append(
                "\nApply pin filters to their values; iterate iterate-filters "
                "(loop options, dedup by ID). For URL-based filters, append params; "
                "for form-based, interact with the form. Sample job URLs "
                "(item_links.url_examples) are for field/selector mapping ONLY — "
                "do NOT infer filter values from their content.\n"
            )
            navigation_section += "\n".join(filter_lines)

        # Form-based / "classic" search discovery CANNOT be done via raw HTTP
        # (anti-forgery tokens + server-side session → HTTP 500). Force a
        # browser-driven Phase 1 and tell the code-writer exactly how.
        classic = (navigation_analysis.get("homepage_nav", {}) or {}).get("classic_search")
        if fmethod == "form" or classic:
            navigation_section += (
                "\n**CRITICAL — browser-driven Phase 1 (form/classic search):** "
                "This site's search is a POST form protected by anti-forgery tokens "
                "and a server-side session, so raw `requests.post(...)` returns HTTP "
                "500 and discovers nothing. Phase 1 MUST use **Playwright in a real "
                f"browser**. Read `workspace/{slug}/navigation_findings.json` "
                "(`homepage_nav.classic_search`) for the search-page URL and its "
                "`<select>` fields, then: open the search page, fill the dropdowns "
                "(Location → Alabama/AL; leave category at 'Any'), **click the submit "
                "button** to obtain the session results URL, then apply the result "
                "filters (set the date `<select>` to the 'Last 7 Days' option and "
                "click the result-form submit button), and paginate. NOTE: this "
                "site's form REQUIRES a **Specialty** selection to submit — a "
                "Discipline or Location alone will NOT submit (validation blocks "
                "it). So for **ALL categories** you MUST iterate the **Specialties** "
                "`<select>` (NOT Disciplines), submitting once per specialty and "
                "deduping the discovered links. Phase 2 (field extraction from the "
                "detail pages) may use HTTP since pages are server-rendered.\n"
                "**Form-interaction tips (important — sites vary):**\n"
                "- Submit buttons may be `input[type='submit']` OR `button[type='submit']` "
                "(LocumTenens' search form uses `input[type='submit']`; its results filter "
                "form uses `button[type='submit']`). Click with a fallback chain, e.g. try "
                "`input[type='submit']`, then `button[type='submit']`, then "
                "`form button`, then `form.requestSubmit()` — never assume `button` only.\n"
                "- `<select>` changes on jQuery sites need `page.select_option(sel, value)` "
                "(Playwright) which fires the right events; if a `<select>` is a "
                "bootstrap-multiselect, also dispatch a jQuery `change` after setting.\n"
            )

    if navigation_section:
        # Navigation jobs always use the two-phase navigation_scraper template
        # (Playwright). Do NOT fall back to the SeleniumBase UC template — it
        # bypasses the cloak safety net (STEALTH_BROWSER=cloak) that lets
        # Playwright defeat Akamai on anti-bot sites.
        template_hint = _template_family and nav_template_hint or ""
        _sa = state.get("site_analysis") or {}
        _ab = _sa.get("anti_bot")
        _conn = _sa.get("connectivity")
        _method = _conn.get("method_that_worked", "") if isinstance(_conn, dict) else ""
        _nav_anti_bot = (isinstance(_ab, dict) and bool(_ab.get("detected"))) or _method.startswith("uc_chrome")
        if _nav_anti_bot:
            template_hint += (
                "\n**STEALTH:** Anti-bot/Akamai detected. Stealth is AUTOMATIC at "
                "runtime — browser_service wraps Playwright launch() to drive "
                "CloakBrowser's stealth Chromium when STEALTH_BROWSER=cloak is set. "
                "**Use a normal `p.chromium.launch()`** — do NOT use UC/Selenium, do "
                "NOT call cloakbrowser.launch() directly (it conflicts with "
                "sync_playwright). Use the `playwright` strategy.\n"
            )

    content = (
        f"## OBJECTIVE\n"
        f"Build a **scraper** for {url}. "
        f"{'The scraper discovers content via site navigation and extracts data from each page.' if navigation_section else 'The scraper reads URLs from `input_urls.json` (in its own directory) and extracts data from each page.'}\n\n"
        f"{content_type_context}"
        f"## Your Task: Write the Scraper\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Sample URL:** {product_url}\n"
        f"**Scraping mechanism:** {mechanism or 'auto-detect'}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Scraper analysis:** workspace/{slug}/scraper_analysis.json\n"
        f"**Save scraper to:** workspace/{slug}/scraper_draft.py\n"
        f"**Save input URLs to:** workspace/{slug}/input_urls.json"
        f"{provided_urls_section}"
        f"{retry_section}{human_feedback_section}{review_feedback_section}{algolia_section}{navigation_section}{template_hint}{scraper_analysis_section}\n\n"
        f"### Full Extraction (MANDATORY — no item caps)\n"
        f"The scraper MUST extract EVERY item the source exposes — never cap the count.\n"
        f"- Do NOT set an arbitrary `MAX_PAGES` / `MAX_ITEMS` limit. Paginate until exhaustion "
        f"(a page returns fewer items than the page size) OR until the API's reported total "
        f"(`totalCount`/`count`/`total`) is reached.\n"
        f"- Set the page size to the source's MAX (often 100-500; e.g. Aya=500, AMN=100). "
        f"Larger pages = fewer requests = faster full extraction.\n"
        f"- **Concurrency:** if Phase 2 (per-detail-page extraction) is HTTP-based "
        f"(`requests`/`httpx`, not browser), extract items concurrently with a "
        f"`ThreadPoolExecutor(max_workers=8)` and a thread-local `requests.Session` "
        f"(the Session is NOT thread-safe to share). This turns thousands of sequential "
        f"2s fetches into minutes (e.g. 1300 jobs in ~7min vs ~54min). Preserve order by "
        f"indexing results to the discovery position.\n"
        f"- The default (no-args) run must do the FULL extraction. `--sample`/`--limit` are "
        f"only for quick tests; never make them the default behavior.\n\n"
        f"### Architecture\n"
    )
    if navigation_section:
        content += (
            "The scraper has TWO phases:\n"
            "**Phase 1: Navigate and discover item URLs.** "
            "Use the search/category/pagination patterns from navigation_analysis.json. "
            "Collect all item page URLs into a list.\n"
            "**Phase 2: Scrape each discovered URL.** "
            "Extract field data from each item page and map to output fields.\n"
            "Write results to `output_{datetime}.json`.\n\n"
            "### DO NOT (Navigation Scraper)\n"
            "- Hardcode URLs — discover them dynamically using the navigation patterns\n"
            "- Skip pagination — scrape ALL pages up to max_pages\n"
            "- Use input_urls.json — the scraper discovers its own URLs\n"
            "- Deviate from the navigation_analysis.json patterns\n"
            "- Add a fallback to read input_urls.json in the default (no-args) branch — "
            "the scraper MUST default to Phase 1 search discovery (using `--query` or the "
            "DEFAULT_QUERY constant), NEVER fall back to input_urls.json\n\n"
        )
    else:
        content += (
            f"The scraper reads URLs from `input_urls.json` in SCRIPT_DIR and "
            f"extracts data from EACH page. For each URL:\n"
            f"1. Extract ID/codes from the URL\n"
            f"2. Fetch data (via API lookup, HTTP request, or page scrape)\n"
            f"3. Map raw data to output fields\n"
            f"4. Write results to `output_{{datetime}}.json`\n\n"
            f"### DO NOT\n"
            f"- Add 'discover mode', catalog crawling, or site-wide discovery\n"
            f"- Add pagination logic (items come from input_urls.json)\n"
            f"- Add bulk extraction via APIs (Algolia partitioning, etc.)\n"
            f"- Add multiple modes of operation\n"
            f"- Use `Accept-Encoding: gzip, deflate, br` — use only `gzip, deflate` "
            f"(requests library may not support Brotli)\n"
            f"{_url_discovery_rules(site_input_urls)}\n"
            f"- Deviate from the template's input/output structure\n\n"
        )

    content += (
        f"### Field Formatting Rules (CRITICAL)\n"
        f'- **price**: Must include the currency symbol, e.g. `"$1,795.00"` not `"1,795.00"`\n'
        f"- **src_url**: Set to the URL where the item was discovered. "
        f"If input comes from input_urls.json, src_url equals the item URL. "
        f"For navigation scrapers, src_url is the listing/search page URL.\n"
        f'- **original_price**: Empty string `""` if not on sale, otherwise include '
        f'currency symbol like `"$2,000.00"`\n'
        f'- **availability**: Normalize to `"In Stock"` or `"Out of Stock"`\n'
        f'- **currency**: ISO 4217 code e.g. `"USD"`, `"EUR"`\n\n'
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
        f"{' AND call write_file to save input URLs to workspace/' + slug + '/input_urls.json' if not site_input_urls and not navigation_section else ''}. "
        f"Do NOT just print code.**"
    )

    # Inject COMPLETE summaries of the analysis JSONs so code_writer does NOT
    # read_file them. The full files (product_analysis 20K+, navigation_analysis
    # 8K, scraper_analysis 4K) bloat the conversation past the context budget and
    # thrash truncation — these summaries carry every extraction method/selector
    # and discovery mechanic losslessly at ~10x smaller. [code_writer bloat]
    pa_summary = _summarize_product_analysis(state.get("product_analysis") or {})
    if pa_summary:
        pa_summary += (
            "\n### DO NOT read_file the analysis JSONs\n"
            "The field-extraction map and navigation mechanics above are COMPLETE. "
            "Do NOT call read_file on product_analysis.json, navigation_analysis.json, "
            "or scraper_analysis.json — re-reading them bloats context and slows you "
            "down. The template (templates/*.py) is the only file you need to read.\n"
        )
        content = content + pa_summary
    return [HumanMessage(content=content)]


def build_code_reviewer_message(state: dict) -> list:
    """Build the review task for the code-reviewer agent.

    Same analysis context as code_writer (so it can judge intent vs implementation),
    plus the prior-cycle failure note if a previous review/test failed. Read-only:
    the agent reads scraper_draft.py + writes code_review.json.
    """
    import json as _json
    from langchain_core.messages import HumanMessage

    slug = state.get("site_slug", "unknown")
    scraper_analysis = state.get("scraper_analysis") or {}
    site_analysis = state.get("site_analysis") or {}
    navigation_analysis = state.get("navigation_analysis") or {}
    product_analysis = state.get("product_analysis") or {}
    strategy = scraper_analysis.get("strategy") or site_analysis.get("scraping_mechanism", "")
    rendering = (
        (navigation_analysis.get("rendering_verified") or "").lower()
        if isinstance(navigation_analysis, dict) else ""
    )

    discovery_section = ""
    if isinstance(navigation_analysis, dict) and navigation_analysis:
        na = navigation_analysis
        il = na.get("item_links") or {}
        strat = na.get("scraper_strategy") or {}
        ph1 = strat.get("phase1_discovery") if isinstance(strat, dict) else {}
        discovery_section = (
            f"**Discovery intent** — rendering={rendering}, strategy={strategy}, "
            f"discovery_method={na.get('discovery_method')}. "
        )
        if isinstance(ph1, dict) and ph1.get("steps"):
            discovery_section += "phase1_discovery.steps: " + _json.dumps(ph1.get("steps"))[:400] + ". "
        if isinstance(il, dict) and il.get("url_pattern"):
            discovery_section += f"item url_pattern=`{il.get('url_pattern')}`."
        discovery_section += (
            "\nFor navigation jobs the scraper MUST iterate EVERY discovery dimension "
            "(all specialties/categories/locations) AND paginate EVERY result page — verify this."
        )

    field_section = _summarize_product_analysis(product_analysis)

    prior_note = ""
    rf = state.get("review_feedback") or ""
    if rf:
        prior_note = (
            "\n### Prior review feedback (code-writer was asked to fix these — VERIFY fixed)\n"
            f"{rf}\n"
        )

    content = (
        f"## Review Task\n"
        f"Read `workspace/{slug}/scraper_draft.py` and verify it implements the intent below. "
        f"You are READ-ONLY — do NOT edit the scraper; write your verdict to "
        f"`workspace/{slug}/code_review.json` as your LAST action "
        f"({{verdict: 'pass'|'issues', summary, issues:[{{severity, area, problem, fix}}]}}).\n\n"
        f"**Mechanism expected:** {strategy or 'http_requests'} (rendering={rendering or 'unknown'}). "
        f"SSR + form-search → HTTP (requests.Session + POST + BeautifulSoup) — flag Playwright for SSR. "
        f"JSON api_endpoint → HTTP GET. Rendered-DOM/anti-bot → Playwright/cloak.\n\n"
        f"{discovery_section}\n\n"
        f"{field_section}\n"
        f"{prior_note}\n"
        f"Apply your checklist. `verdict: 'pass'` ONLY if the scraper fully implements the intent "
        f"(full discovery loop + all fields + right mechanism). Otherwise return concrete, actionable issues."
    )
    return [HumanMessage(content=content)]


def build_code_tester_message(state: dict) -> list:
    """Build the initial HumanMessage for the code-tester agent."""
    slug = state.get("site_slug", "unknown")

    retry_context = ""
    retry_count = state.get("test_retry_count", 0)
    if retry_count == FINAL_RETRY_SENTINEL:
        retry_context = (
            f"\n### FINAL RETEST MODE (User-Initiated Final Retry)\n"
            f"This is the FINAL test attempt based on user feedback. "
            f"If the scraper does not pass this test, the job will end.\n"
            f"Read the previous test report at: workspace/{slug}/test_report.json\n\n"
        )
        human_feedback = state.get("human_feedback", "")
        if human_feedback:
            retry_context += (
                f"### User-Flagged Issue (CRITICAL — verify this is fixed)\n"
                f"The user specifically reported:\n"
                f"> {human_feedback}\n\n"
                f"Your validation MUST specifically check this issue. "
                f"If it is NOT resolved, this is a FAIL regardless of other "
                f"field quality. Do NOT give the scraper credit for fixing "
                f"this issue unless the output JSON demonstrably shows the "
                f"correct value.\n\n"
            )
    elif retry_count > 0:
        retry_context = (
            f"\n### RETEST MODE (Cycle {retry_count + 1})\n"
            f"The scraper was modified after previous test failures. "
            f"Focus your validation on the fields that previously failed. "
            f"Read the previous test report at: workspace/{slug}/test_report.json\n\n"
        )

    input_mode = state.get("input_mode", "url_list")
    search_criteria = state.get("search_criteria", "")
    nav_validation = ""
    if input_mode in ("navigation", "list_page", "search_term"):
        nav_analysis = state.get("navigation_analysis") or {}
        discovery = nav_analysis.get("discovery_method", "")
        nav_validation = (
            f"\n### Navigation Job Validation (input_mode={input_mode})\n"
            f"This is a navigation job — the scraper discovers products via search/category.\n"
        )
        if discovery == "search":
            nav_validation += (
                f"- **Phase 1 MUST start from the search/listing URL in navigation_analysis.json** — "
                f"not from a guessed category page\n"
                f"- Validate that discovered URLs are PRODUCT pages, not category pages\n"
            )
        nav_validation += (
            f"- `--sample --query \"{search_criteria}\"` — use these exact args so Phase 1 discovery runs.\n"
            f"- Do NOT run with only `--sample` — the scraper will fall back to input_urls.json "
            f"instead of discovering products via search\n"
            f"- A FAIL is expected if Phase 1 discovers category/landing page URLs instead of product URLs\n"
            f"- This is a navigation scraper — input_urls.json is NOT used. "
            f"Products come from the scraper's own discovery.\n"
        )

    # Job-portal filter validation: verify date/location filtering was applied
    page_type = state.get("page_type", "")
    if page_type in ("job_navigation", "job_posting") or state.get("site_type") == "jobs":
        nav_analysis = state.get("navigation_analysis") or {}
        filters_info = nav_analysis.get("filters", {}) or {}
        if filters_info.get("has_filters"):
            nav_validation += (
                "\n### Job Filter Validation (REQUIRED)\n"
                "This job portal supports filtering. Verify the scraper applied it:\n"
                "- Every output item's `location` must match Alabama (or be empty if "
                "unparseable — never an out-of-state location)\n"
                "- Every output item's `posted_date` must be within the last 7 days "
                "(parse the date; reject items older than 7 days)\n"
                "- If items violate the date/location filter, this is a HIGH severity "
                "issue (filters not applied)\n"
                "- Confirm the `posted_date` field is populated for most items\n"
            )

    # Strategy constraint (Fix C): code_tester must not recommend switching strategies.
    strategy = (state.get("scraper_analysis") or {}).get("strategy", "") or (
        state.get("site_analysis") or {}
    ).get("scraping_mechanism", "")
    strategy_constraint = ""
    if strategy:
        strategy_constraint = (
            f"\n### Strategy Constraint (CRITICAL for remediation)\n"
            f"The scraping strategy was chosen upstream by scraper_analyzer as **{strategy}**. "
            f"Default: work WITHIN this strategy — if the scraper used the wrong selectors or "
            f"missed a field, say so with `target: \"mapping\"` or `target: \"scraper\"`.\n\n"
            f"EXCEPTION — access/strategy failure: if the scraper extracted ~0 items BECAUSE "
            f"the strategy itself can't reach the content (e.g. `http_requests`/`api` returned "
            f"empty/403/blocked, or `playwright` TIMED OUT trying to drive a heavy form), set "
            f"`target: \"strategy\"` and name the cause in `reason` (timeout / blocked / api-400 "
            f"/ http-empty). The pipeline will switch to a different strategy. Use `strategy` "
            f"ONLY for these access-class failures — never for a field-mapping or selector bug.\n"
        )
    # Crash capture (Fix A): record the raw traceback so code_writer can make a targeted fix.
    crash_capture = (
        "\n### If the Scraper CRASHED\n"
        "If `run_scraper` shows the scraper exited non-zero, raised an exception, or hit a "
        "syntax/argparse/import error (it never produced valid output), set "
        "`overall_assessment: \"CRASH\"` and put the EXACT stderr/traceback verbatim in a "
        "top-level `crash_error` field. A crash is a code bug — the pipeline routes it to a "
        "targeted bug fix, not a full rewrite, so the verbatim error is essential.\n"
    )

    content = (
        f"## OBJECTIVE\n"
        f"Validate the generated scraper for {slug}.\n\n"
        f"{retry_context}"
        f"## Your Task: Test the Scraper\n\n"
        f"**Scraper path:** workspace/{slug}/scraper_draft.py\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Save test report to:** workspace/{slug}/test_report.json\n\n"
        f"{nav_validation}"
        f"### Workflow (4 steps)\n"
        f"1. Run scraper: `run_scraper(path=\"workspace/{slug}/scraper_draft.py\", "
        f"args=" + str(["--sample", "--input", "input_urls.json"]) + ")` (1-2 calls)\n"
        f"**For navigation jobs**: use `--input input_urls.json` (NOT `--query`). "
        f"The input_urls.json contains product URLs already discovered by navigation_explore. "
        f"Using `--input` tests the ACTUAL products (with prices), not re-discovered URLs.\n"
        f"2. Read `workspace/{slug}/product_analysis.json` for field expectations (1 call)\n"
        f"3. Read the output JSON file that run_scraper produced (1 call)\n"
        f"4. Write test_report.json (1 call)\n\n"
        f"**IMPORTANT: Do NOT read scraper_draft.py.** Assess the scraper ONLY by its output. "
        f"The code contains post-generation patches (robust overrides, output filters) that are correct by design. "
        f"Judge solely by whether the output JSON has correctly-populated product fields.\n\n"
        f"### Validation Method\n"
        f"Compare scraper output against `product_analysis.json > fields > {{field}} > expectations`. "
        f"Each field has a validation contract (type, required, min_length, should_not_match, "
        f"sample_values, known_bad_values, format_hint). Do NOT re-fetch live pages.\n\n"
        f"### IMPORTANT: Non-Product URLs Are Expected\n"
        f"The scraper uses BROAD link discovery (captures all same-domain links). Some discovered "
        f"URLs will be category/nav/non-product pages — the scraper's output filter removes items "
        f"without a price. **This is correct behavior, not a failure.** Judge the scraper ONLY by "
        f"the PRICED products in the output. If the priced products have correctly populated "
        f"title/price/availability, it's a PASS — do NOT flag missing items from non-product URLs "
        f"as high severity. A sample of 5 URLs that yields 3-5 priced products with good fields is a PASS.\n\n"
        f"### BUDGET: 10 tool calls maximum.\n\n"
        f"### How Scraper Execution Works\n"
        f"The `run_scraper` tool automatically detects browser-based scrapers (Playwright, "
        f"SeleniumBase, etc.) and dispatches them to a remote `browser_service` container "
        f"that has Chrome + Xvfb + all browser libraries pre-installed. "
        f"HTTP-based scrapers run locally. You NEVER need to install packages.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT modify or fix the scraper — only report issues\n"
        f"- Do NOT re-fetch live product pages — validate against product_analysis expectations\n"
        f"- Do NOT run the scraper more than 2 times\n"
        f"- Do NOT install packages, run bash commands, or load skills\n"
        f"- Do NOT read input_urls.json — that file is not your concern\n\n"
        f"### Dead URLs\n"
        f"Products with status_code in {sorted(DEAD_STATUS_CODES)} are dead URLs "
        f"— exclude from quality assessment. If ALL are dead, set PASS with confidence 1.0.\n\n"
        f"### Optional Fields\n"
        f"Fields `original_price` and `location` are optional — missing = severity low, never high.\n\n"
        f"### Remediation Recommendation (REQUIRED in test_report)\n"
        f"After assessing the scraper, add a top-level `remediation` object telling the pipeline "
        f"WHERE the fix should happen:\n"
        f"```json\n"
        f"\"remediation\": {{\n"
        f"  \"target\": \"mapping\" | \"scraper\" | \"strategy\",\n"
        f"  \"fields\": [\"price\"],\n"
        f"  \"reason\": \"...\"\n"
        f"}}\n"
        f"```\n"
        f"Decision rule — for each FAILED **required** field, compare the scraper output against "
        f"`product_analysis.json`'s mapping for that field:\n"
        f"- If the field's mapping is **missing / `tested: false` / selector looks wrong or "
        f"unverified** → the root cause is the MAPPING → set `target: \"mapping\"` and list those "
        f"fields. The pipeline re-runs product_analyzer to fix the mapping (not just regenerate the "
        f"scraper with the same bad input).\n"
        f"- If the mapping looks correct (right selector/method, `tested: true`) but the scraper "
        f"didn't implement it → `target: \"scraper\"`.\n"
        f"- If the scraper extracted ~0 items because the STRATEGY can't access the content "
        f"(http/api empty or 403/blocked; playwright timed out) → `target: \"strategy\"` with the "
        f"cause in `reason` (timeout / blocked / api-400 / http-empty). The pipeline switches "
        f"strategy. Do NOT use `strategy` for selector or field-mapping bugs.\n"
        f"- If everything passes → `target: \"scraper\"` (no real remediation needed).\n"
        f"When unsure, default to `\"scraper\"`. Only list fields that are required AND failed.\n\n"
        f"{strategy_constraint}{crash_capture}"
        f"**CRITICAL: You MUST call write_file to save your test report to "
        f"workspace/{slug}/test_report.json as your LAST action.**"
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
        f"1. Use `run_bash` to copy files (NOT read_file — you don't need to read their contents):\n"
        f"   - `cp workspace/{slug}/scraper_draft.py scrapers/{slug}/scraper.py`\n"
        f"   - `cp workspace/{slug}/input_urls.json scrapers/{slug}/input_urls.json` (if it exists)\n"
        f"   - `cp workspace/{slug}/output_*.json scrapers/{slug}/` (if any exist)\n"
        f"   - `cp -r workspace/{slug}/analysis scrapers/{slug}/analysis` (if it exists)\n"
        f"2. Use `search_files` to list what's in the workspace (NOT read_file).\n"
        f"3. write_file to save cleanup report (1 call).\n\n"
        f"### BUDGET: 10 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT use read_file on output_*.json files — they can be very large and will\n"
        f"  exceed the LLM's prompt limit. Use `search_files` to check they exist, then\n"
        f"  `run_bash` (`cp`) to copy them.\n"
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

    nav_review_note = ""
    nav_report = state.get("nav_learning_report")
    if nav_report:
        updated = nav_report.get("skills_updated", [])
        nav_review_note = (
            f"\n### nav-skill-review already applied (do NOT duplicate)\n"
            f"The nav-skill-review agent ran during this pipeline and auto-applied "
            f"{len(updated)} navigation learnings. Read "
            f"`workspace/{slug}/nav_learning_report.json` for details. "
            f"Focus your analysis on NON-navigation learnings (product extraction, "
            f"code patterns, anti-bot) and skip anything already covered.\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Capture reusable knowledge from the completed scrape of {url}.\n\n"
        f"## Your Task: Skill Learning\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Detected platform:** {platform}\n"
        f"**Scraper:** scrapers/{slug}/scraper.py\n"
        f"**Save learning report to:** workspace/{slug}/learning_report.json\n"
        f"{nav_review_note}\n"
        f"### Workflow\n"
        f"1. Read the scraper and analysis files (2-4 calls):\n"
        f"   - scrapers/{slug}/scraper.py\n"
        f"   - workspace/{slug}/site_analysis.json\n"
        f"   - workspace/{slug}/product_analysis.json\n"
        f"   - workspace/{slug}/test_report.json (if present)\n"
        f"   - workspace/{slug}/navigation_findings.json (if present — raw nav data)\n"
        f"   - workspace/{slug}/nav_learning_report.json (if present — already-applied nav learnings)\n"
        f"2. Check existing skills in .opencode/skills/ (1-2 calls)\n"
        f"3. write_file to save learning report (1 call)\n\n"
        f"### BUDGET: 15 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT modify any skill files — only report proposals\n"
        f"- Do NOT modify the scraper\n"
        f"- Do NOT run anything\n"
        f"- Do NOT propose navigation learnings already applied by nav-skill-review\n\n"
        f"**CRITICAL: You MUST call write_file to save your learning report to "
        f"workspace/{slug}/learning_report.json as your LAST action.**"
    )
    return [HumanMessage(content=content)]


def build_dagster_converter_message(state: dict) -> list:
    """Build the message for the dagster_converter agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    site_name = state.get("site_name", slug)

    # Find the existing scraper path (production first, workspace fallback)
    scraper_path = f"scrapers/{slug}/scraper.py"
    workspace_scraper = f"workspace/{slug}/scraper_draft.py"

    content = (
        f"## OBJECTIVE\n"
        f"Convert the existing scraper for {url} into the client's `BaseTlsScraper` format.\n\n"
        f"## Files to Read\n"
        f"1. **Existing scraper**: `{scraper_path}` (or `{workspace_scraper}` if the production "
        f"version doesn't exist yet). Read the FULL scraper — understand Phase 1 (discovery) + "
        f"Phase 2 (extraction).\n"
        f"2. **Client template**: `templates/dagster_template.py` — the `BaseTlsScraper` class. "
        f"Understand `self._fetch()`, `scrape_one(url)`, `discover_urls()`.\n"
        f"3. **Field mappings**: `workspace/{slug}/product_analysis.json` — which selectors/methods "
        f"extract each field.\n"
        f"4. **Navigation**: `workspace/{slug}/navigation_analysis.json` — filter dimensions, URL "
        f"patterns, option lists (for `discover_urls`).\n"
        f"5. **Site info**: `workspace/{slug}/site_analysis.json` — platform, scraping method.\n\n"
        f"## What to Generate\n"
        f"Write `workspace/{slug}/{slug}_dagster.py` — a `BaseTlsScraper` subclass with:\n"
        f"- `discover_urls(self) -> list[str]`: Phase 1 — iterate filter combinations from "
        f"navigation_analysis, build listing URLs, fetch each, extract + dedup item URLs.\n"
        f"- `scrape_one(self, url: str) -> dict`: Phase 2 — fetch one item page + parse fields "
        f"(using the SAME selectors/logic as the original scraper).\n"
        f"- Helper methods as needed (copied/adapted from the original).\n"
        f"- All required imports (tls_client, requests, BeautifulSoup, re, json, etc.).\n\n"
        f"## Strategy Adaptation\n"
        f"- `http_requests`: use `self._fetch()` for both phases + BeautifulSoup/regex parse.\n"
        f"- `playwright`: use browser_service `/render` endpoint or Playwright internally "
        f"(keep the `BaseTlsScraper` interface; change the implementation).\n"
        f"- `internal_api`: convert API calls to `self._fetch()` or `requests.get()`.\n\n"
        f"## Rules\n"
        f"- PRESERVE the parsing logic faithfully (same selectors, same field names).\n"
        f"- Keep the class extending `BaseTlsScraper` + `scrape_one(self, url) -> dict`.\n"
        f"- Include `discover_urls()` for navigation jobs; return `[]` for url_list jobs.\n"
        f"- Dedup by job/item ID in `discover_urls()`.\n"
        f"- Syntax-check before writing (valid Python).\n"
        f"- Write to `workspace/{slug}/{slug}_dagster.py`.\n"
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
    "create_dagster_converter",
    "create_nav_skill_review",
    "build_site_analyzer_message",
    "build_product_analyzer_message",
    "build_scraper_analyzer_message",
    "build_code_writer_message",
    "build_code_tester_message",
    "build_cleanup_message",
    "build_skill_learner_message",
    "build_nav_skill_review_message",
    "build_dagster_converter_message",
]
