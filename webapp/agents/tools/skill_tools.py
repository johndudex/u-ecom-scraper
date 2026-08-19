"""Skill loading tools for LangGraph agent nodes.

Skills live in the **File Master** (``skills/{name}/SKILL.md``) via
``src.skills_store`` — the FM is the source of truth so agent-learned
knowledge survives redeploys without ever dirtying git. The image's
``.opencode/skills/`` copy is the seed + read-only fallback (per-key:
FM 404 → image copy; FM outage → image copy + WARNING).

The ``load_skill`` tool is assigned to agents that need to consult skill
knowledge at runtime; ``learn_skill`` (write path) is assigned ONLY to
nav_skill_review — the effective sole skill writer today (skill_learner's
message forbids writes; its prompt is being aligned in the same change).
"""

import logging
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def get_skill_tools(skills_dir: Optional[str] = None) -> list:
    """Return skill-related tools.

    Args:
        skills_dir: ignored (kept for signature compat). Skills resolve via
            ``src.skills_store`` (FM first, image fallback).

    Returns:
        List of LangChain BaseTool instances: [load_skill, list_skills].
    """
    # skills_dir is accepted-and-ignored: pre-FM callers passed a path.
    from src.skills_store import list_skills, read_skill

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
        content = read_skill(skill_name)
        if content is None:
            available = list_skills()
            hint = (
                f"\nAvailable skills: {', '.join(available)}"
                if available
                else "\nNo skills available (FM empty and no image fallback)."
            )
            return f"Skill '{skill_name}' not found.{hint}"

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

    @tool
    def list_skills() -> str:
        """List all available skills with their names.

        Returns:
            A formatted list of skill names, or a message if none exist.
        """
        names = list_skills()
        if not names:
            return "No skills found."
        return "\n".join(f"- {name}" for name in names)

    return [load_skill, list_skills]


def get_skill_write_tools() -> list:
    """Write-path tools — nav_skill_review ONLY (see module docstring)."""
    from src.skills_store import append_learned, create_skill

    @tool
    def learn_skill(skill_name: str, title: str, source: str, applicability: str, body: str) -> str:
        """Append a '## Learned:' section to an existing skill (append-only).

        Use this INSTEAD OF write_file/edit_file for skill updates — the
        format is enforced for you and the change persists to the File Master
        (survives redeploys). Never modifies existing content; refuses
        duplicate titles.

        Args:
            skill_name: Existing skill to append to (e.g. "navigation-patterns").
            title: Short imperative title, e.g. "Force UTF-8 in requests".
            source: Where this was learned, e.g. "https://books.toscrape.com (2026-08-17)".
            applicability: When to apply it, e.g. "Any requests-based scraper
                when the server omits charset".
            body: The technique/knowledge in markdown (code fences welcome).
        """
        result = append_learned(skill_name, title, source, applicability, body)
        if not result.get("ok"):
            return f"learn_skill failed: {result.get('error')}"
        if not result.get("appended"):
            return f"Skipped (duplicate title): {result.get('note')}"
        return f"Appended '## Learned: {title}' to {result.get('skill')}."

    @tool
    def create_new_skill(name: str, description: str, body: str) -> str:
        """Create a brand-new skill (rare — only for genuinely new patterns).

        Name must be lowercase-hyphenated (e.g. "wcag-table-extraction").
        Fails if the skill already exists anywhere.

        Args:
            name: Lowercase-hyphenated skill name.
            description: One-line description (shown in agents' skill lists).
            body: The skill's markdown content.
        """
        result = create_skill(name, description, body)
        if not result.get("ok"):
            return f"create_new_skill failed: {result.get('error')}"
        return f"Created skill '{result.get('skill')}'."

    return [learn_skill, create_new_skill]
