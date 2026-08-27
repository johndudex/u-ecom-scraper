"""Shared normalizer for the ``site`` block of scraper outputs.

Two of the scraper templates emitted ``site`` as a bare host STRING while the
other seven emit a dict — every reader that calls ``output["site"].get(...)``
AttributeError'd on the string form, and tasks.py's ground-truth override
(swallowing product_count/site_name/platform/scraping_method) died in a bare
except that discarded ALL of it. Templates are being fixed to emit dicts; this
normalizer is the reader-side belt-and-braces so ANY shape degrades to a dict.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit


def normalize_site_block(value: Any, fallback_platform: str = "") -> dict:
    """Coerce an output's ``site`` value into the dict shape readers expect.

    - dict → returned as-is (missing ``platform`` backfilled from the fallback).
    - str  → treated as a host/URL: ``{"url": <str>, "name": <str>,
      "platform": fallback_platform}``.
    - anything else → ``{"platform": fallback_platform}``.
    """
    fallback_platform = str(fallback_platform or "")
    if isinstance(value, dict):
        block = dict(value)
        if not block.get("platform"):
            block["platform"] = fallback_platform
        return block
    if isinstance(value, str) and value.strip():
        host = urlsplit(value if "://" in value else f"//{value}", scheme="https").netloc or value.strip()
        return {"url": value.strip(), "name": host, "platform": fallback_platform}
    return {"platform": fallback_platform} if fallback_platform else {}


def ground_truth_platform(out_data: Optional[dict], site_analysis: Optional[dict] = None) -> str:
    """Best platform for output-site backfill: site_analysis.site.platform first,
    then the analysis top level — the same precedence the pipeline already uses."""
    for source in (site_analysis or {},):
        site = source.get("site")
        if isinstance(site, dict) and site.get("platform"):
            return str(site["platform"])
        if source.get("platform"):
            return str(source["platform"])
    return ""
