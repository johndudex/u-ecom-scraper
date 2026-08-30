"""Shared HTTP fetch for requests-based scrapers. NOT site-adaptable code.

Why this module exists (job-58 + job-62, both birkenstock): code_writer
rewrote ``fetch_page`` inline and stripped the proxy ladder because the
analysis said "direct connection works". Akamai-class sites allow the first
hits, then challenge — the tester phase passed (5/5 items, 0.95), and the
execution run minutes later got 200-wrapped challenge pages with zero item
links. With the ladder stripped there was nothing to escalate to: the 45s
retry just re-hit the same burned IP and the job finalized 0 items.

So the fetch machinery is an imported module the LLM cannot edit — the same
structural defense as ``src/discovery.py`` for the navigation templates.

Two block signatures handled:

- HARD block: 403/503/429 or a ban-text marker → ``is_banned`` fires and the
  ladder escalates inside ``fetch_page`` (unchanged legacy behavior).
- SOFT block: HTTP 200 whose listing body selects zero item links. No status
  code to test, so ``is_banned`` never fires — the DISCOVERY LOOP must detect
  it and re-fetch with ``min_tier`` bumped (see the template's discovery
  loop). ``min_tier`` slices the ladder: 0 = none→datacenter→residential,
  1 = datacenter→residential, 2 = residential only.

The closure carries ``min_tier_floor`` (raised by discovery once a higher
tier starts working) so Phase-2 item fetches reuse the tier that unblocked
Phase 1 instead of starting unproxied again.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Optional

import requests
from bs4 import BeautifulSoup
from src.proxy import ProxyConfig, should_warn_residential, warn_residential_usage

logger = logging.getLogger(__name__)

# Pure ladder arithmetic — testable without network or config.
LADDER_BASE = ["none"]


def resolve_tiers(min_tier: int, escalation: list) -> list:
    """Return the tier sequence for a fetch starting at ``min_tier``.

    ``escalation`` is ProxyConfig's ordered tier list (e.g. datacenter,
    residential). ``min_tier=0`` → ["none", *escalation]; ``min_tier=1``
    drops the unproxied head; beyond the ladder → empty list (caller sees
    ``tiers == []`` and gives up).
    """
    return (LADDER_BASE + list(escalation))[max(0, int(min_tier)):]


def create_fetch_page(delay_s: float = 2.0, headers: Optional[dict] = None) -> Callable:
    """Build a ``fetch_page(url, min_tier=0)`` closure for one scraper run.

    The Session is per-factory (per-scraper-process): cookies round-trip
    across every request and the connection pool is reused, which is what
    keeps an Akamai bot score from re-rising on every hit (job-58 trigger).
    ``headers`` (the template's HEADERS, UA especially) are applied once.
    """
    proxy_config = ProxyConfig.get_instance()
    ssl_verify = proxy_config.config.get("strategy", {}).get("ssl_verify", False)
    escalation = proxy_config.get_escalation_tier()

    session = requests.Session()
    if headers:
        session.headers.update(headers)

    def fetch_page(url: str, min_tier: int = 0) -> Optional[tuple[BeautifulSoup, int]]:
        floor = int(getattr(fetch_page, "min_tier_floor", 0))
        tiers = resolve_tiers(max(min_tier, floor), escalation)

        for tier in tiers:
            if should_warn_residential(tier):
                warn_residential_usage(url)

            proxies = proxy_config.get_proxy_dict(tier) if tier != "none" else None
            max_retries = (
                proxy_config.get_max_retries(tier) if tier != "none" else 3
            )
            cooldown = (
                proxy_config.get_cooldown(tier) if tier != "none" else delay_s * 2
            )

            for attempt in range(max_retries):
                try:
                    time.sleep(delay_s)
                    response = session.get(
                        url,
                        proxies=proxies,
                        timeout=proxy_config.get_timeout(),
                        verify=ssl_verify,
                    )
                    if response.status_code == 200:
                        return BeautifulSoup(response.text, "html.parser"), 200
                    if proxy_config.is_banned(response.status_code, response.text):
                        logger.warning(
                            "Ban detected (%s) on tier '%s' for %s — escalating",
                            response.status_code, tier, url[:80],
                        )
                        break  # next tier
                    response.raise_for_status()
                    return BeautifulSoup(response.text, "html.parser"), response.status_code
                except requests.RequestException as e:
                    logger.error(
                        "Failed to fetch %s (attempt %s/%s, tier=%s): %s",
                        url[:80], attempt + 1, max_retries, tier, e,
                    )
                    if attempt < max_retries - 1:
                        time.sleep(cooldown)

        return None

    # Discovered by the discovery loop (escalation bound) and raised by it
    # once a higher tier unblocks a listing — see templates/requests_scraper.py.
    fetch_page.min_tier_floor = 0
    fetch_page.tiers_total = 1 + len(escalation)
    return fetch_page
