"""Skill loading tools for LangGraph agent nodes.

Skills live under ``.opencode/skills/{skill_name}/SKILL.md`` and contain
reusable domain knowledge (e.g. Shopify detection, anti-bot handling) that
agents inject into their context at runtime.

The ``load_skill`` tool is assigned to ``skill_learner`` and any other agent
that needs to consult skill knowledge during execution.
"""

import logging
import os
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _resolve_skills_dir(skills_dir: Optional[str] = None) -> str:
    """Return the effective skills directory path."""
    if skills_dir:
        return os.path.abspath(skills_dir)
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return os.path.join(str(settings.PROJECT_ROOT), ".opencode", "skills")
    except Exception:
        pass
    return os.path.join(os.getcwd(), ".opencode", "skills")


def get_skill_tools(skills_dir: Optional[str] = None) -> list:
    """Return skill-related tools.

    Args:
        skills_dir: Override path to the skills directory.  Falls back to
            ``{PROJECT_ROOT}/.opencode/skills/``.

    Returns:
        List of LangChain BaseTool instances.
    """
    base = _resolve_skills_dir(skills_dir)

    @tool
    def load_skill(skill_name: str) -> str:
        """Load a skill's instruction file and return its full content.

        Skills contain reusable domain knowledge that helps agents with
        specific tasks (e.g. detecting Shopify stores, handling anti-bot
        protection, configuring proxies).

        Args:
            skill_name: Name of the skill directory (e.g. ``"shopify-detection"``,
                ``"anti-bot-handling"``).  Do not include the path.

        Returns:
            The full content of the skill's SKILL.md file, or an error
            message if the skill does not exist.
        """
        skill_path = os.path.join(base, skill_name, "SKILL.md")
        if not os.path.isfile(skill_path):
            available = []
            if os.path.isdir(base):
                available = sorted(
                    d
                    for d in os.listdir(base)
                    if os.path.isfile(os.path.join(base, d, "SKILL.md"))
                )
            hint = (
                f"\nAvailable skills: {', '.join(available)}"
                if available
                else f"\nSkills directory '{base}' not found or empty."
            )
            return f"Skill '{skill_name}' not found at {skill_path}.{hint}"

        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
            logger.info("Loaded skill '%s' (%d chars)", skill_name, len(content))

            if len(content) > 3000:
                try:
                    from headroom import compress as _compress

                    cr = _compress(
                        [{"role": "tool", "content": content}],
                        model="glm-5-turbo",
                    )
                    compressed = cr.messages[0]["content"]
                    if len(content) - len(compressed) > 200:
                        logger.info(
                            "Skill '%s' compressed: %d → %d chars",
                            skill_name,
                            len(content),
                            len(compressed),
                        )
                        content = compressed
                except Exception:
                    pass

            return content
        except Exception as e:
            return f"Error reading skill '{skill_name}': {e}"

    @tool
    def list_skills() -> str:
        """List all available skills with their names and descriptions.

        Returns:
            A formatted list of skill names found under the skills directory,
            or a message if none exist.
        """
        if not os.path.isdir(base):
            return f"Skills directory not found at '{base}'"

        entries: list[str] = []
        for name in sorted(os.listdir(base)):
            sk_md = os.path.join(base, name, "SKILL.md")
            if os.path.isfile(sk_md):
                entries.append(name)

        if not entries:
            return f"No skills found in '{base}'"

        return "\n".join(f"- {name}" for name in entries)

    return [load_skill, list_skills]
