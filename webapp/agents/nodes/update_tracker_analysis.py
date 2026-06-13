"""Update the Site model with platform and product listing URL after analysis."""

import logging
from typing import Any

from ..state import ScrapeState

logger = logging.getLogger(__name__)


def update_tracker_analysis(state: ScrapeState) -> dict[str, Any]:
    """Write platform and product_listing_url from ``site_analysis`` into the Site model."""
    site_analysis: dict = state.get("site_analysis", {})
    url = state.get("url", "")
    if not site_analysis or not url:
        logger.warning("update_tracker_analysis: no site_analysis or url, skipping")
        return {}

    scraper_analysis: dict = state.get("scraper_analysis", {})
    site_block = site_analysis.get("site", site_analysis)
    platform = site_block.get("platform", "")
    scraping_method = scraper_analysis.get("strategy", "") or site_block.get("scraping_mechanism", "")
    site_name = site_block.get("name", "")
    product_listing_url = site_analysis.get("product_listing_url", "")

    try:
        from scraper.models import Site

        site = Site.objects.filter(url=url.rstrip("/")).first()
        if site:
            updates = {}
            if platform:
                updates["platform"] = platform
            if product_listing_url:
                site.sample_url = product_listing_url
                updates["sample_url"] = product_listing_url
            if site_name:
                updates["name"] = site_name
            if scraping_method:
                updates["scraping_method"] = scraping_method
            if updates:
                site.save(update_fields=list(updates.keys()))
            logger.info(
                "update_tracker_analysis: updated Site %s (platform=%s)",
                url.rstrip("/"),
                platform,
            )
        else:
            logger.warning("update_tracker_analysis: site %s not found in DB", url.rstrip("/"))
    except Exception as exc:
        logger.warning("update_tracker_analysis: DB error: %s", exc)

    return {
        "platform": platform or state.get("platform", ""),
        "scraping_method": scraping_method or state.get("scraping_method", ""),
        "site_name": site_name or state.get("site_name", ""),
    }
