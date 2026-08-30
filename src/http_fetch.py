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

Fingerprint tier (``"fingerprint"``, above residential) [job-66 birkenstock]:
Akamai-class bot managers score the TLS handshake itself — every
``requests``/urllib3 connection presents the same client fingerprint no
matter which proxy IP carries it, which is why all three IP tiers saw
200-but-zero on birkenstock while a real browser rendered fine. The last
tier re-issues the request through ``curl_cffi`` with an impersonated browser
TLS fingerprint (still riding the residential IP). Import-guarded: without
the dependency the ladder is exactly the legacy three tiers. Kill switch:
``proxy.json`` → ``{"strategy": {"curl_cffi_tier": false}}``.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Optional

import requests
from bs4 import BeautifulSoup
from src.proxy import ProxyConfig, should_warn_residential, warn_residential_usage

try:  # optional dependency — absence degrades the ladder, never crashes
    from curl_cffi import requests as curl_requests

    _CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover - exercised implicitly on slim images
    curl_requests = None
    _CURL_CFFI_AVAILABLE = False

logger = logging.getLogger(__name__)

# Pure ladder arithmetic — testable without network or config.
LADDER_BASE = ["none"]
# Impersonation target (curl_cffi >= 0.6 accepts the "chrome" alias; older
# releases need a pinned version string — override via env then).
CURL_IMPERSONATE = os.environ.get("SCRAPER_CURL_IMPERSONATE", "chrome")


def resolve_tiers(
    min_tier: int, escalation: list, *, fingerprint_tier: bool = False
) -> list:
    """Return the tier sequence for a fetch starting at ``min_tier``.

    ``escalation`` is ProxyConfig's ordered tier list (e.g. datacenter,
    residential). ``min_tier=0`` → ["none", *escalation]; ``min_tier=1``
    drops the unproxied head; beyond the ladder → empty list (caller sees
    ``tiers == []`` and gives up). ``fingerprint_tier=True`` appends the
    curl_cffi browser-TLS tier after the proxy tiers.
    """
    ladder = LADDER_BASE + list(escalation)
    if fingerprint_tier:
        ladder = ladder + ["fingerprint"]
    return ladder[max(0, int(min_tier)):]


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

    # Fingerprint tier (curl_cffi, browser TLS impersonation). Enabled when the
    # dependency imports AND the config kill-switch is not off. Rides the
    # residential proxy when one is configured — the tier changes WHAT the
    # client looks like, the proxy changes WHERE it comes from.
    fingerprint_enabled = (
        _CURL_CFFI_AVAILABLE
        and bool(proxy_config.config.get("strategy", {}).get("curl_cffi_tier", True))
    )
    curl_session = None
    if fingerprint_enabled:
        try:
            curl_session = curl_requests.Session(impersonate=CURL_IMPERSONATE)
            if headers:
                curl_session.headers.update(headers)
        except Exception as exc:
            logger.warning("curl_cffi fingerprint tier disabled: %s", exc)
            fingerprint_enabled = False
            curl_session = None

    def fetch_page(url: str, min_tier: int = 0) -> Optional[tuple[BeautifulSoup, int]]:
        floor = int(getattr(fetch_page, "min_tier_floor", 0))
        tiers = resolve_tiers(
            max(min_tier, floor), escalation, fingerprint_tier=fingerprint_enabled
        )

        for tier in tiers:
            if tier == "fingerprint":
                # Browser-TLS re-issue over the residential exit. curl_cffi
                # raises its own error family — catch broad, this tier is
                # best-effort by design.
                get_exc: type = Exception
                proxies = proxy_config.get_proxy_dict("residential")
                max_retries = proxy_config.get_max_retries("residential")
                cooldown = proxy_config.get_cooldown("residential")
                client = curl_session
            else:
                get_exc = requests.RequestException
                proxies = proxy_config.get_proxy_dict(tier) if tier != "none" else None
                max_retries = (
                    proxy_config.get_max_retries(tier) if tier != "none" else 3
                )
                cooldown = (
                    proxy_config.get_cooldown(tier) if tier != "none" else delay_s * 2
                )
                client = session

            if should_warn_residential(tier):
                warn_residential_usage(url)

            for attempt in range(max_retries):
                try:
                    time.sleep(delay_s)
                    if tier == "fingerprint":
                        logger.info(
                            "Fetching %s via curl_cffi fingerprint tier "
                            "(impersonate=%s)", url[:80], CURL_IMPERSONATE,
                        )
                    response = client.get(
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
                except get_exc as e:
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
    fetch_page.tiers_total = 1 + len(escalation) + (1 if fingerprint_enabled else 0)
    return fetch_page
