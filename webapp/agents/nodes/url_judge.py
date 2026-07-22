"""One-shot LLM listing-URL selector.

Given a site's candidate nav/category URLs + the content type + search query
(the "ask"), an LLM judges which URLs are CORRECT listing/data pages vs WRONG
(marketing, info, account, legal…). This is a **pre-visit** judgment — no page
is fetched, so it's one cheap LLM call.

Mirrors the codebase's only one-shot-LLM pattern (``webapp/agents/tools/probe_tools.py``
captcha classifier): prompt-forced JSON + manual fence-strip + ``json.loads`` +
``try/except`` safe-default. No agent, no tools, no guards (a bare
``llm.invoke`` bypasses all of those by design).

Generic across content types (jobs / products / articles / …). Used by
``navigate_explore`` to decide which candidate pages to visit for extraction,
replacing the brittle keyword/category-path heuristics that picked marketing
pages (e.g. ayahealthcare's ``/travel-nursing/`` instead of the jobs listing).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# Path words that signal a LISTING / data page (lean "correct").
_LISTING_PATH_WORDS = (
    "jobs", "job", "search", "results", "result", "listings", "listing",
    "category", "collection", "collections", "shop", "browse", "products",
    "catalog", "inventory", "feed", "board", "postings", "vacanc",
)
# Words that signal a NON-listing page (lean "wrong").
_NONLISTING_WORDS = (
    "pay", "salary", "benefits", "housing", "scholarship", "how-to", "faq",
    "about", "contact", "login", "signin", "register", "account", "cart",
    "privacy", "terms", "policy", "blog", "news", "press", "team", "recruiter",
    "compliance", "licensure", "review", "testimonial", "story", "guide",
)


def _clamp(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


def _strip_fences(text: str) -> str:
    """Strip ``` / ```json fences GLM sometimes wraps JSON in."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if text[:4].lower() == "json":  # bare "json{...}" (no newline after fence)
        text = text[4:].lstrip()
    return text


def _candidate_line(c: dict) -> str:
    href = (c.get("href") or c.get("url") or "").strip()
    text = (c.get("text") or "").strip()
    return f"url={href}\ttext={text}"


def judge_candidate_urls(
    candidates: list[dict],
    content_type: str,
    search_query: str,
    site_url: str,
) -> dict[str, Any]:
    """LLM pre-visit judgment of candidate listing URLs.

    Args:
        candidates: list of ``{href|url, text}`` dicts (homepage nav/category/footer links).
        content_type: the content type being scraped (e.g. ``job_posting``, ``product``).
        search_query: the user's search criteria / query.
        site_url: the site root (for context).

    Returns ``{"ranking": [{url, text, verdict, confidence, reason}], "model_notes": str}``
    preserving the model's preference order. On any failure returns
    ``{"ranking": [], "error": "..."}`` — callers fall back to deterministic heuristics.
    """
    if not candidates:
        return {"ranking": [], "model_notes": "no candidates"}

    capped = candidates[:30]
    cand_block = "\n".join(_candidate_line(c) for c in capped)

    prompt = (
        "You are a URL selector for a web scraper. You are given a site's candidate navigation "
        "links, the CONTENT TYPE the user wants to extract, and their SEARCH QUERY. Classify each "
        "URL as CORRECT or WRONG.\n"
        "- CORRECT = a page that LISTS MANY items of this content type matching the query (a job "
        "search/results/category page, a product collection/search results page, an article index, "
        "a forum thread list). This is where the data lives.\n"
        "- WRONG = a marketing/landing hub, pay & benefits, salary, housing, scholarship, blog, "
        "news, about, contact, FAQ, recruiter/team, compliance/licensure, account/login, legal.\n\n"
        f"CONTENT TYPE wanted: {content_type or 'unspecified'}\n"
        f"SEARCH QUERY: {search_query or '(none — general extraction)'}\n"
        f"SITE: {site_url}\n\n"
        "Use BOTH the URL path and the anchor text. Prefer URLs whose path contains listing words "
        f"({', '.join(_LISTING_PATH_WORDS[:12])}). Mark URLs about info/marketing words "
        f"({', '.join(_NONLISTING_WORDS[:12])}) as wrong. A marketing hub (e.g. /travel-nursing/) "
        "is WRONG even if it matches the query word — only pages that LIST actual items are CORRECT. "
        "Order the ranking best-first.\n\n"
        f"CANDIDATES ({len(capped)}):\n{cand_block}\n\n"
        "Respond with ONLY a JSON object (no markdown, no backticks), exactly this shape:\n"
        '{"ranking": [{"url": "...", "text": "...", "verdict": "correct"|"wrong", '
        '"confidence": 0.0-1.0, "reason": "short"}], "model_notes": "one line"}'
    )

    try:
        from langchain_core.messages import HumanMessage

        from agents.llm import get_small_llm

        llm = get_small_llm(temperature=0.0)
        resp = llm.invoke([HumanMessage(content=prompt)])
        result = json.loads(_strip_fences(resp.content or ""))
        if not isinstance(result, dict) or not isinstance(result.get("ranking"), list):
            raise ValueError("response missing 'ranking' list")

        ranking: list[dict[str, Any]] = []
        for r in result.get("ranking", []):
            if not isinstance(r, dict):
                continue
            url = (r.get("url") or "").strip()
            if not url:
                continue
            ranking.append({
                "url": url,
                "text": (r.get("text") or "").strip(),
                "verdict": "correct" if str(r.get("verdict", "")).lower() == "correct" else "wrong",
                "confidence": _clamp(r.get("confidence", 0.5)),
                "reason": (r.get("reason") or "").strip()[:200],
            })
        logger.info(
            "url_judge: judged %d candidates (%d correct) for ct=%s query=%r",
            len(ranking), sum(1 for r in ranking if r["verdict"] == "correct"),
            content_type, (search_query or "")[:40],
        )
        return {"ranking": ranking, "model_notes": (result.get("model_notes") or "")[:300]}
    except Exception as exc:
        logger.warning("url_judge: LLM judgment failed: %s", exc)
        return {"ranking": [], "error": str(exc)[:200]}


def ranked_correct(judgment: dict, limit: int = 8) -> list[str]:
    """Extract the 'correct' URLs from a judgment, best-first (confidence desc)."""
    ranking = (judgment or {}).get("ranking") or []
    correct = [r for r in ranking if r.get("verdict") == "correct"]
    correct.sort(key=lambda r: r.get("confidence", 0), reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for r in correct:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:limit]
