"""Load system prompts from the project's .opencode/agents/ and .opencode/skills/ trees."""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

_PROMPT_CACHE: dict[str, str] = {}


def _agents_dir() -> Path:
    """Resolve the absolute path to ``.opencode/agents/``."""
    project_root = getattr(settings, "PROJECT_ROOT", None)
    if project_root:
        return Path(project_root) / ".opencode" / "agents"
    return Path(__file__).resolve().parent.parent.parent / ".opencode" / "agents"


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (``---`` delimited block) from markdown."""
    return re.sub(r"^---\n.*?\n---\n*", "", text, count=1, flags=re.DOTALL)


def load_agent_prompt(agent_name: str) -> str:
    """Load and cache a system prompt from ``.opencode/agents/{agent_name}.md``.

    YAML frontmatter is stripped so only the instruction markdown is returned.

    Args:
        agent_name: Filename stem inside ``.opencode/agents/`` (e.g. ``"site-analyzer"``).

    Returns:
        The markdown body of the prompt file.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
    """
    if agent_name in _PROMPT_CACHE:
        return _PROMPT_CACHE[agent_name]

    path = _agents_dir() / f"{agent_name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Agent prompt not found: {path}")

    text = path.read_text(encoding="utf-8")
    cleaned = _strip_frontmatter(text).strip()
    _PROMPT_CACHE[agent_name] = cleaned
    return cleaned


# load_skill removed — dead code (zero callers; skills now resolve via src.skills_store)
