"""LLM factory — thin wrapper around langchain-openai ChatOpenAI for Z.AI."""

from __future__ import annotations

from typing import Optional

from django.conf import settings
from langchain_openai import ChatOpenAI


def get_llm(model: Optional[str] = None, temperature: float = 0.3) -> ChatOpenAI:
    """Create a ChatOpenAI instance configured for the Z.AI API.

    Args:
        model: Model identifier. Falls back to ``ZAI_MAIN_MODEL`` setting.
        temperature: Sampling temperature.

    Returns:
        A ready-to-use ``ChatOpenAI`` LLM.
    """
    return ChatOpenAI(
        openai_api_base=getattr(settings, "ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4/"),
        openai_api_key=settings.ZAI_API_KEY,
        model=model or getattr(settings, "ZAI_MAIN_MODEL", "glm-5-turbo"),
        temperature=temperature,
        # Retry transient api.z.ai connection drops (large code_writer calls
        # fail mid-transfer with openai.APIConnectionError). Default is 2; bump
        # so a momentary network blip doesn't kill the agent. [generic]
        max_retries=getattr(settings, "LLM_MAX_RETRIES", 5),
        # Bound each call so a *stuck* call (api.z.ai accepts it but never
        # responds) fails fast and max_retries can retry it — without this a
        # silent hang parks the agent forever (the heartbeat masks it from the
        # watchdog). 600s is generous: legitimate codegen is ~5-8 min, so this
        # only fires on true hangs. (An earlier 300s cut off real codegen; no
        # timeout at all let hangs persist — 600s is the balance.)
        timeout=getattr(settings, "LLM_REQUEST_TIMEOUT", 600),
    )


def get_main_llm(temperature: float = 0.3) -> ChatOpenAI:
    """Return the main model (glm-5-turbo) for subagent reasoning."""
    return get_llm(
        model=getattr(settings, "ZAI_MAIN_MODEL", "glm-5-turbo"),
        temperature=temperature,
    )


def get_small_llm(temperature: float = 0.3) -> ChatOpenAI:
    """Return the small / fast model (glm-5-turbo) for quick decisions."""
    return get_llm(
        model=getattr(settings, "ZAI_SMALL_MODEL", "glm-5-turbo"),
        temperature=temperature,
    )
