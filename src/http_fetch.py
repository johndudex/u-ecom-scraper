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

Two INDEPENDENT soft-block signals, deliberately kept apart:

- ZERO LINKS (structural): the body is a real page whose item set is empty or
  invisible. Only the caller can tell (it knows the page's expected anchor),
  so it stays the discovery loop's job — see ``src/listing_discovery.py``.
- CHALLENGE SHAPE (this module): the body is NOT the page at all — it is an
  anti-bot challenge, consent wall or error stub served with a 200 status.
  ``detect_soft_block`` recognises the generic shapes (challenge markers,
  or a body under the configured minimum size) and ``fetch_page`` RETURNS a
  falsy :class:`SoftBlock` instead of a soup. It does NOT escalate and does
  NOT retry: one escalation owner, the caller, which already owns the tier
  ladder. Gated by ``SCRAPER_SOFT_BLOCK_MIN_BYTES`` (default "0" = off, so
  prod is unaffected until the env var is staged).

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

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Optional, Union

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


# ═══════════════════════════════════════════════════════════════════════════════
# SOFT-BLOCK (challenge-shape) DETECTION
#
# A 200 status proves nothing: Akamai/Cloudflare/queueing pages are served with
# 200 and a body that is not the page. These markers are deliberately GENERIC
# and site-agnostic (no per-site tuning, no is_banned's config-driven marker
# list — that one is empty under prod defaults, so it never fires).
# ═══════════════════════════════════════════════════════════════════════════════

# Case-insensitive substrings that identify a challenge page (title, heading or
# embedded challenge payload).
CHALLENGE_MARKERS: tuple = (
    "attention required",
    "access denied",
    "verify you are human",
    "verifying you are human",
    "checking your browser",
    "just a moment",
    "cf-chl",
    "cf_chl",
    "captcha",
    "_abck",
    "akamai",
)

SOFT_BLOCK_MIN_BYTES_ENV = "SCRAPER_SOFT_BLOCK_MIN_BYTES"


def soft_block_min_bytes() -> int:
    """Minimum-body floor: a listing under this many bytes is a challenge stub.

    0 (the default, and what prod ships) turns the whole detector OFF — a
    mis-set floor must never be able to reject real pages. Read per call so a
    staged env change takes effect without a process restart.
    """
    raw = os.environ.get(SOFT_BLOCK_MIN_BYTES_ENV, "0")
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


class SoftBlock:
    """Falsy signal that a 200 response was a challenge, not the page.

    Falsy ON PURPOSE: every existing caller already treats a falsy ``fetch_page``
    result as "this fetch failed", so a caller that never learns about
    :class:`SoftBlock` degrades to safe behaviour instead of trying to parse a
    challenge page. Callers that DO check (the discovery loop) escalate the
    proxy tier and re-fetch — ``fetch_page`` itself never does, so there is
    exactly one escalation owner.
    """

    __slots__ = ("reason", "markers", "body_bytes")

    def __init__(self, reason: str, markers: tuple = (), body_bytes: int = 0):
        self.reason = reason
        self.markers = tuple(markers)
        self.body_bytes = int(body_bytes)

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return (
            f"SoftBlock(reason={self.reason!r}, markers={self.markers!r}, "
            f"body_bytes={self.body_bytes})"
        )


def detect_soft_block(text: str) -> Optional[SoftBlock]:
    """Challenge-shape detector for an HTTP 200 body. ``None`` = looks real.

    Two shapes, either of which trips it:
    - a generic challenge marker in the body;
    - a body smaller than the ``SCRAPER_SOFT_BLOCK_MIN_BYTES`` floor (the
      zero-item-anchor check, approximated: no real listing is that small).
    """
    floor = soft_block_min_bytes()
    if floor <= 0 or not text:
        return None
    lowered = text.lower()
    hits = tuple(m for m in CHALLENGE_MARKERS if m in lowered)
    if hits:
        return SoftBlock("challenge_marker", hits, len(text))
    if len(text) < floor:
        return SoftBlock("under_min_bytes", (), len(text))
    return None


def _ladder_clients(
    headers: Optional[dict],
) -> tuple:
    """Shared per-run setup for the fetch factories (create_fetch_page/text/json).

    One owner so the factories cannot drift on session identity — the
    cookie round-trip across a reused Session is the anti-bot defense
    (job-58). Returns ``(proxy_config, ssl_verify, escalation, session,
    curl_session, fingerprint_enabled)``.
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

    return proxy_config, ssl_verify, escalation, session, curl_session, fingerprint_enabled


def create_fetch_page(delay_s: float = 2.0, headers: Optional[dict] = None) -> Callable:
    """Build a ``fetch_page(url, min_tier=0)`` closure for one scraper run.

    The Session is per-factory (per-scraper-process): cookies round-trip
    across every request and the connection pool is reused, which is what
    keeps an Akamai bot score from re-rising on every hit (job-58 trigger).
    ``headers`` (the template's HEADERS, UA especially) are applied once.
    """
    (
        proxy_config,
        ssl_verify,
        escalation,
        session,
        curl_session,
        fingerprint_enabled,
    ) = _ladder_clients(headers)

    def fetch_page(
        url: str, min_tier: int = 0
    ) -> Optional[Union[tuple[BeautifulSoup, int], SoftBlock]]:
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
                        # 200 is not proof of success: challenge pages ship as
                        # 200. Report the block and return — NO retry, NO tier
                        # escalation here (the caller owns the ladder; see
                        # discover_listing_urls). Off entirely unless
                        # SCRAPER_SOFT_BLOCK_MIN_BYTES is staged.
                        block = detect_soft_block(response.text)
                        if block is not None:
                            logger.warning(
                                "SOFT BLOCK (200) on tier '%s' for %s — %s; "
                                "returning the signal for the caller to escalate",
                                tier, url[:80], block,
                            )
                            return block
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


def create_fetch_text(delay_s: float = 2.0, headers: Optional[dict] = None) -> Callable:
    """Build a ``fetch_text(url, params=None, min_tier=0)`` closure — the SAME
    session, proxy ladder and soft-block contract as :func:`create_fetch_page`,
    returning ``(text, status_code)`` instead of a parsed soup.

    Why it exists [wave-15 3.4]: the API-family and HTTP-navigation templates
    run the shared ladder for discovery but fetched every item page / SSR page
    with a bare ``requests.get(url, headers=HEADERS)`` — unproxied, no
    escalation. When Phase 1 only succeeded on a proxied tier, Phase 2 re-ran
    the SAME URLs from the burned direct IP and every item fetch failed
    (identity parity: the run changes egress mid-flight and loses the
    identity that worked). Import this instead — the LLM cannot strip what it
    never sees.

    The ladder loop is deliberately a sibling of ``fetch_page``'s, not a
    refactor of it: the established factory is pinned by the full suite and
    by live jobs, so it ships byte-identical and drifts never.
    """
    (
        proxy_config,
        ssl_verify,
        escalation,
        session,
        curl_session,
        fingerprint_enabled,
    ) = _ladder_clients(headers)

    def fetch_text(
        url: str, params: Optional[dict] = None, min_tier: int = 0
    ) -> Optional[Union[tuple[str, int], SoftBlock]]:
        floor = int(getattr(fetch_text, "min_tier_floor", 0))
        tiers = resolve_tiers(
            max(min_tier, floor), escalation, fingerprint_tier=fingerprint_enabled
        )

        for tier in tiers:
            if tier == "fingerprint":
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
                        params=params,
                        proxies=proxies,
                        timeout=proxy_config.get_timeout(),
                        verify=ssl_verify,
                    )
                    if response.status_code == 200:
                        # Same contract as fetch_page: a challenge ships as
                        # 200 — report it, escalate NOWHERE (the caller owns
                        # the ladder).
                        block = detect_soft_block(response.text)
                        if block is not None:
                            logger.warning(
                                "SOFT BLOCK (200) on tier '%s' for %s — %s; "
                                "returning the signal for the caller to escalate",
                                tier, url[:80], block,
                            )
                            return block
                        return response.text, 200
                    if proxy_config.is_banned(response.status_code, response.text):
                        logger.warning(
                            "Ban detected (%s) on tier '%s' for %s — escalating",
                            response.status_code, tier, url[:80],
                        )
                        break  # next tier
                    response.raise_for_status()
                    return response.text, response.status_code
                except get_exc as e:
                    logger.error(
                        "Failed to fetch %s (attempt %s/%s, tier=%s): %s",
                        url[:80], attempt + 1, max_retries, tier, e,
                    )
                    if attempt < max_retries - 1:
                        time.sleep(cooldown)

        return None

    fetch_text.min_tier_floor = 0
    fetch_text.tiers_total = 1 + len(escalation) + (1 if fingerprint_enabled else 0)
    return fetch_text


def create_fetch_json(delay_s: float = 2.0, headers: Optional[dict] = None) -> Callable:
    """Build a ``fetch_json(url, params=None, min_tier=0)`` closure over
    :func:`create_fetch_text` — returns ``(parsed_json, status_code)``.

    For the API-family templates' per-item fetches (``scrape_product``): the
    old inline ``requests.get(...).json()`` had no ladder, so a PDP JSON
    endpoint that needed a proxied tier returned a challenge/403 and the item
    was silently dropped (``except: return None``). A 200 body that does not
    parse as JSON is a failed fetch (None), never an exception.
    """
    fetch_text = create_fetch_text(delay_s=delay_s, headers=headers)

    def fetch_json(
        url: str, params: Optional[dict] = None, min_tier: int = 0
    ) -> Optional[Union[tuple[Union[dict, list], int], SoftBlock]]:
        result = fetch_text(url, params=params, min_tier=min_tier)
        if not result:
            return result
        text, status = result
        try:
            return json.loads(text), status
        except (TypeError, ValueError) as exc:
            logger.warning(
                "fetch_json: non-JSON 200 body (%d bytes) from %s: %s",
                len(text), url[:80], exc,
            )
            return None

    fetch_json.min_tier_floor = 0
    fetch_json.tiers_total = fetch_text.tiers_total
    return fetch_json
