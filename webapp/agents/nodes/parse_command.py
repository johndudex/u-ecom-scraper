"""Parse the incoming job input and populate the initial graph state."""

import logging
import re
from urllib.parse import urlparse

from langgraph.types import Command

from ..state import ScrapeState

logger = logging.getLogger(__name__)


def parse_command(state: ScrapeState) -> Command:
    """Generate ``site_slug`` and set all initial state values from the input.

    * Normalises the URL into a filesystem-safe slug.
    * Sets defaults for every optional field that downstream nodes expect.
    * Returns a ``Command`` routing to the next node (``check_tracker``).
    """
    url = _clean_url(state.get("url", ""))
    sample_url = _clean_url(state.get("sample_url") or state.get("product_url") or "")
    if not url:
        raise ValueError("parse_command: state['url'] is required and empty")

    slug = _url_to_slug(url)
    page_type = state.get("page_type", "product")
    input_mode = state.get("input_mode", "url_list")
    logger.info(
        "parse_command: url=%s sample_url=%s page_type=%s → site_slug=%s",
        url, sample_url[:80], page_type, slug,
    )

    return Command(
        update={
            "url": url,
            "sample_url": sample_url or None,
            "product_url": sample_url or None,
            "site_slug": slug,
            "site_name": "",
            "site_status": "",
            "page_type": page_type,
            "input_mode": input_mode,
            "skip_site_analysis": False,
            "skip_content_analysis": False,
            "skip_product_analysis": False,
            "skip_code_generation": False,
            "current_phase": "parse_command",
            "phases_completed": [],
            "site_analysis_retries": 0,
            "content_analysis_retries": 0,
            "product_analysis_retries": 0,
            "test_retry_count": 0,
            "reanalyze_count": 0,
            "execution_status": "",
            "output_file": "",
            "item_count": 0,
            "product_count": 0,
            "scraping_method": "",
            "platform": "",
            "fields_extracted": [],
            "interrupt_reason": "",
            "interrupt_options": [],
            "human_response": None,
            "error_message": "",
            "agent_logs": [],
        },
        goto="check_tracker",
    )


def _clean_url(url: str) -> str:
    """Remove JSON-escaped forward slashes and trailing garbage from a URL."""
    cleaned = url.replace("\\/", "/")
    cleaned = cleaned.replace("\\", "")
    return cleaned.strip()


def _url_to_slug(url: str) -> str:
    """Convert a URL to a filesystem-safe slug.

    Handles JSON-escaped forward slashes (``\\/``) which occur when URLs
    are stored in JSON strings and not properly unescaped.

    Examples::

        _url_to_slug("https://www.Nike.com/path") → "nike-com"
        _url_to_slug("https://allthedresses.com.au") → "allthedresses-com-au"
        _url_to_slug("https:\\/\\/www.armani.com\\/") → "armani-com"
    """
    cleaned = _clean_url(url)
    parsed = urlparse(cleaned)
    hostname = parsed.hostname or ""
    hostname = hostname.lower()
    hostname = re.sub(r"^www\.", "", hostname)
    hostname = re.sub(r"[^a-z0-9]+", "-", hostname)
    hostname = hostname.strip("-")
    return hostname
