"""wave-41: POST /api/v1/discover-fields — live field discovery.

Renders a partner-supplied sample item page in the platform browser and
returns candidate extractable fields + a flat JSON schema (the data behind
the Extractor Builder's "available fields" view). The constraints in
sync_api.yaml are contract, not advisory: 6 req/min per key, 1 concurrent
per key, ~45s ceiling, homepage-only URLs rejected, non-public hosts
rejected (the render executes inside the platform network).

No DB reads — zero cross-tenant surface (unlike check-site, whose
fieldless response is a separate documented invariant).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.conf import settings
from django.http import JsonResponse

from . import errors, ssrf
from .ratelimit import check_discovery_rate, release_discovery_slot
from .readers import _parse_body
from .views import api_view

logger = logging.getLogger("scraper.api")


def probe_and_discover(url: str, *, navigate_timeout: int = 25,
                       llm_timeout: int = 20) -> dict:
    """Browser render + field discovery for one URL.

    Mirrors the intake UI's probe (views.intake_discover_fields) so both
    surfaces share one code path. Returns the ``discover_fields_from_html``
    result dict — ``{fields, json_schema, source, content_type}``. Raises
    ApiError on infrastructure failure: 502 site_blocked (the target page
    blocks automated access), 503 discovery_unavailable (browser service
    down / transport error), 504 discovery_timeout (page load exceeded the
    navigate budget). A rendered-but-empty page is honest zero, not an
    error — discovery of nothing is a valid answer.
    """
    import httpx

    from src.field_discovery import discover_fields_from_html

    bs_url = getattr(settings, "BROWSER_SERVICE_URL", "http://browser_service:8001")
    try:
        nav_resp = httpx.post(
            f"{bs_url}/navigate",
            json={
                "url": url,
                "stealth": "cloak",
                "return_what": "all",
                "wait_until": "domcontentloaded",
                "timeout": navigate_timeout,
            },
            timeout=navigate_timeout + 5,  # httpx budget = navigate + headroom
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise errors.ApiError(503, "discovery_unavailable",
                              "Browser service unreachable.")
    except httpx.ReadTimeout:
        raise errors.ApiError(504, "discovery_timeout",
                              "The page took too long to load.")
    except httpx.HTTPError as exc:
        raise errors.ApiError(503, "discovery_unavailable",
                              f"Browser request failed: {str(exc)[:160]}")

    if nav_resp.status_code != 200:
        raise errors.ApiError(503, "discovery_unavailable",
                              f"Browser service error (HTTP {nav_resp.status_code}).")
    try:
        nav_data = nav_resp.json()
    except ValueError:
        raise errors.ApiError(503, "discovery_unavailable",
                              "Browser service returned a malformed response.")
    if not isinstance(nav_data, dict):
        raise errors.ApiError(503, "discovery_unavailable",
                              "Browser service returned a malformed response.")
    if nav_data.get("blocked") or nav_data.get("blocked_type"):
        blocked_type = nav_data.get("blocked_type") or "antibot"
        raise errors.ApiError(502, "site_blocked",
                              f"The page blocked automated access ({blocked_type}).")

    html = nav_data.get("html") or ""
    title = nav_data.get("title") or ""
    if len(html) < 500:
        # Rendered but empty — a valid (if useless) page. Honest zero.
        return {"fields": [], "json_schema": None, "source": "none",
                "content_type": ""}

    try:
        return discover_fields_from_html(url=url, html=html, title=title,
                                         llm_timeout=llm_timeout)
    except Exception as exc:  # discovery is best-effort — never a 500
        logger.warning("api discover_fields failed for %s: %s", url[:120], exc)
        return {"fields": [], "json_schema": None, "source": "none",
                "content_type": ""}


@api_view(["POST"])
def discover_fields(request):
    body = _parse_body(request)
    url = str(body.get("url", "")).strip()
    if not url:
        raise errors.ApiError(422, "invalid_url", "url is required.")
    try:
        parsed = urlparse(url)
    except ValueError:
        raise errors.ApiError(422, "invalid_url", "url must be an absolute http(s) URL.")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise errors.ApiError(422, "invalid_url", "url must be an absolute http(s) URL.")
    if not parsed.path.strip("/"):
        raise errors.ApiError(422, "homepage_url",
                              "url must be a sample item page, not the site homepage.")
    reason = ssrf.validate_public_http_url(url, resolver=ssrf._resolve)
    if reason:
        raise errors.ApiError(422, "blocked_host", f"{reason}.")

    # Dedicated budget (sync_api.yaml x-rate-limits:field_discovery):
    # 6 req/min per key, 1 concurrent render per key. Runs AFTER the cheap
    # validation gates so malformed requests never consume the budget.
    key16 = request.api_key.key_hash[:16]
    retry_after = check_discovery_rate(key16)
    if retry_after is not None:
        raise errors.ApiError(
            429, "rate_limited", "Per-key rate limit exceeded.",
            {"limit": "6 req/min per key, 1 concurrent discovery",
             "retry_after": retry_after},
        )
    try:
        result = probe_and_discover(url)
    finally:
        release_discovery_slot(key16)
    return JsonResponse({
        "url": url,
        "fields": result.get("fields", []),
        "json_schema": result.get("json_schema"),
        "source": result.get("source", "none"),
        "content_type": result.get("content_type", ""),
    })
