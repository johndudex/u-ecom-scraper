"""Page probe tool — delegates to browser-service.

Provides ``probe_page`` — a single tool that sends an HTTP POST to the
browser-service ``/probe`` endpoint.  The service handles the full
escalation chain (HTTP -> Playwright -> UC Chrome) internally, so this
module is now a thin HTTP client with result formatting.

The probe cache remembers which escalation method worked for a domain
(e.g., ``playwright_datacenter``) so subsequent probes can skip straight
to that method instead of re-running the full chain. The cache does NOT
store page data — every probe fetches fresh content.
"""

import logging
import os
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

BROWSER_SERVICE_URL = os.environ.get(
    "BROWSER_SERVICE_URL", "http://browser-service:8001"
)
PROBE_TIMEOUT = int(os.environ.get("PROBE_TIMEOUT", "180"))
CACHE_EXPIRY_HOURS = 4

ESCALATION_STEPS = [
    ("direct_http", "none"),
    ("playwright_none", "none"),
    ("uc_chrome_none", "none"),
    ("direct_http_datacenter", "datacenter"),
    ("playwright_datacenter", "datacenter"),
    ("uc_chrome_datacenter", "datacenter"),
    ("direct_http_residential", "residential"),
    ("playwright_residential", "residential"),
    ("uc_chrome_residential", "residential"),
]


def _get_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or url
    except Exception:
        return url


def _get_cached_method(domain: str) -> dict | None:
    try:
        from django.utils import timezone
        from scraper.models import ProbeCache

        entry = ProbeCache.objects.filter(domain=domain).first()
        if not entry:
            return None
        if timezone.now() > entry.cached_at + timedelta(hours=CACHE_EXPIRY_HOURS):
            entry.delete()
            return None
        if entry.captcha_detected:
            logger.info(
                "probe_page: cache HIT but captcha_detected=True for %s, ignoring",
                domain,
            )
            return None
        entry.save(update_fields=["last_used_at"])
        logger.info(
            "probe_page: cache HIT for %s (method=%s, akamai=%s, cached %s)",
            domain,
            entry.method,
            entry.needs_akamai_bypass,
            entry.cached_at.isoformat(),
        )
        return {
            "method": entry.method,
            "needs_akamai_bypass": entry.needs_akamai_bypass,
        }
    except Exception as e:
        logger.debug("probe_page: cache check failed: %s", e)
        return None


def _save_probe_cache(domain: str, data: dict):
    if not data.get("success"):
        return
    try:
        from scraper.models import ProbeCache

        ProbeCache.objects.update_or_create(
            domain=domain,
            defaults={
                "method": data.get("method", "unknown"),
                "needs_akamai_bypass": data.get("needs_akamai_bypass", False),
            },
        )
        logger.info("probe_page: cached method for %s: %s", domain, data.get("method"))
    except Exception as e:
        logger.debug("probe_page: cache save failed: %s", e)


def _get_browser_service_url() -> str:
    try:
        from django.conf import settings

        url = getattr(settings, "BROWSER_SERVICE_URL", "")
        if url:
            return url
    except Exception:
        pass
    return BROWSER_SERVICE_URL


def _format_probe_result(data: dict) -> str:
    from src.page_analysis import (
        format_probe_result,
    )

    url = data.get("_request_url", "")
    method = data.get("method", "unknown")
    proxy_tier = data.get("proxy_tier", "none")
    status_code = data.get("status_code", 0)
    title = data.get("title", "")
    body_length = data.get("body_length", 0)
    needs_browser = data.get("needs_browser", True)
    blocked = data.get("blocked", False)
    jsonld = data.get("jsonld", [])
    meta = data.get("meta", {})
    selector_results_raw = data.get("selector_results", {})

    if isinstance(selector_results_raw, dict):
        lines = []
        for sel_name, sel_data in selector_results_raw.items():
            if isinstance(sel_data, dict):
                if sel_data.get("found"):
                    lines.append(
                        f'  {sel_name}: "{sel_data.get("text", "")}" '
                        f"[count: {sel_data.get('count', 0)}]"
                    )
                else:
                    lines.append(f"  {sel_name}: NOT FOUND")
            else:
                lines.append(f"  {sel_name}: {sel_data}")
        selector_results = "\n".join(lines) if lines else str(selector_results_raw)
    else:
        selector_results = str(selector_results_raw)

    return format_probe_result(
        url=url,
        method=method,
        proxy_tier=proxy_tier,
        status_code=status_code,
        title=title,
        body_length=body_length,
        js_needed=needs_browser,
        blocked=blocked,
        jsonld_blocks=jsonld,
        meta_tags=meta,
        selector_results=selector_results,
        error=data.get("error", ""),
    )


def _verify_captcha_free(data: dict) -> dict:
    """Use an LLM to check whether a probe's page content is a captcha page.

    Only called on successful probe results (HTTP 200 with body content).
    Returns a dict with captcha detection results.
    """
    if not data.get("success"):
        return {"captcha_detected": False, "captcha_type": "", "confidence": 0.0, "reasoning": ""}

    title = data.get("title", "")
    body_length = data.get("body_length", 0)

    if body_length < 500:
        return {"captcha_detected": False, "captcha_type": "", "confidence": 0.0, "reasoning": "Page too short for captcha check"}

    body_text = data.get("body_text", "")
    if body_text:
        body_text = body_text[:1500]

    if not body_text:
        selector_results = data.get("selector_results", {})
        if isinstance(selector_results, dict):
            for sel_name, sel_data in selector_results.items():
                if isinstance(sel_data, dict) and sel_data.get("found"):
                    body_text += f"{sel_data.get('text', '')} "
    if not body_text.strip():
        body_text = title

    try:
        from ..llm import get_small_llm
        from langchain_core.messages import HumanMessage

        llm = get_small_llm(temperature=0.0)

        prompt = (
            "You are a captcha/bot-detection page classifier for an ecommerce scraper. "
            "Analyze the page content below and determine if this is a REAL ecommerce "
            "product page or a CAPTCHA / bot-detection / verification challenge page.\n\n"
            "Respond with ONLY a JSON object (no markdown, no backticks):\n"
            '{"captcha_detected": true/false, "captcha_type": "slider|turnstile|recaptcha|akamai_challenge|cloudflare_challenge|manual_verification|none", '
            '"confidence": 0.0-1.0, "reasoning": "brief explanation"}\n\n'
            f"URL: {data.get('_request_url', '')}\n"
            f"Page title: {title}\n"
            f"Body length: {body_length} chars\n"
            f"Body text: {body_text[:1500]}\n"
            f"Blocked flag: {data.get('blocked', False)}"
        )

        response = llm.invoke([HumanMessage(content=prompt)])
        text = response.content.strip()

        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        import json as _json
        result = _json.loads(text)
        logger.info(
            "probe_page: LLM captcha check — detected=%s, type=%s, confidence=%.1f",
            result.get("captcha_detected"),
            result.get("captcha_type"),
            result.get("confidence", 0),
        )
        return result

    except Exception as exc:
        logger.warning("probe_page: LLM captcha check failed: %s", exc)
        return {"captcha_detected": False, "captcha_type": "", "confidence": 0.0, "reasoning": f"LLM check failed: {exc}"}


def run_probe_with_captcha_check(url: str, render_js: bool = True) -> dict:
    """Run probe with automatic LLM captcha verification and escalation.

    Tries each escalation step. After a successful probe, runs an LLM
    check to verify the page is not a captcha page. If captcha detected,
    continues to the next escalation step.

    Returns the first captcha-free successful result, or a result with
    captcha_detected=True if all methods hit captcha.
    """
    service_url = _get_browser_service_url()
    domain = _get_domain(url)

    from src.geo import detect_country as _detect_country
    country = _detect_country(url)

    cached = _get_cached_method(domain)
    skip_to = 0
    if cached and not cached.get("captcha_detected", False):
        skip_to = 0
        for i, (step_name, _) in enumerate(ESCALATION_STEPS):
            if step_name == cached["method"]:
                skip_to = i
                break

    methods_tried = []
    captcha_info = None

    for i, (step_name, _) in enumerate(ESCALATION_STEPS):
        if i < skip_to:
            continue

        probe_payload: dict = {
            "url": url,
            "render_js": render_js,
            "timeout": PROBE_TIMEOUT,
            "start_method": step_name,
            "country": country,
        }

        logger.info(
            "probe_page[accessibility]: trying %s for %s",
            step_name, url[:100],
        )

        try:
            resp = httpx.post(
                f"{service_url}/probe",
                json=probe_payload,
                timeout=PROBE_TIMEOUT + 10,
            )
            resp.raise_for_status()
            data = resp.json()
            data["_request_url"] = url
            methods_tried.append(step_name)

            if not data.get("success"):
                if data.get("needs_akamai_bypass"):
                    logger.info(
                        "probe_page[accessibility]: %s returned Akamai, trying bypass for %s",
                        step_name, url[:80],
                    )
                    try:
                        ak_resp = httpx.post(
                            f"{service_url}/probe-akamai",
                            json={"url": url, "timeout": PROBE_TIMEOUT},
                            timeout=PROBE_TIMEOUT + 10,
                        )
                        ak_resp.raise_for_status()
                        ak_data = ak_resp.json()
                        ak_data["_request_url"] = url
                        methods_tried.append(f"{step_name}_akamai")
                        if ak_data.get("success"):
                            captcha_result = _verify_captcha_free(ak_data)
                            if captcha_result.get("captcha_detected"):
                                captcha_info = captcha_result
                                logger.info(
                                    "probe_page[accessibility]: Akamai bypass returned captcha (%s) for %s",
                                    captcha_result.get("captcha_type"), url[:80],
                                )
                                continue
                            logger.info(
                                "probe_page[accessibility]: Akamai bypass returned real content for %s",
                                url[:80],
                            )
                            _save_probe_cache(domain, ak_data)
                            ak_data["captcha_verified"] = True
                            return ak_data
                    except Exception as ak_exc:
                        logger.warning(
                            "probe_page[accessibility]: Akamai bypass failed for %s: %s",
                            url[:80], ak_exc,
                        )
                logger.info(
                    "probe_page[accessibility]: %s failed for %s",
                    step_name, url[:80],
                )
                continue

            captcha_result = _verify_captcha_free(data)
            if captcha_result.get("captcha_detected"):
                captcha_info = captcha_result
                logger.info(
                    "probe_page[accessibility]: %s returned captcha page (%s) for %s",
                    step_name, captcha_result.get("captcha_type"), url[:80],
                )
                continue

            logger.info(
                "probe_page[accessibility]: %s returned real content for %s",
                step_name, url[:80],
            )
            _save_probe_cache(domain, data)
            data["captcha_verified"] = True
            return data

        except Exception as exc:
            logger.warning(
                "probe_page[accessibility]: %s error for %s: %s",
                step_name, url[:80], exc,
            )
            continue

    logger.warning(
        "probe_page[accessibility]: ALL methods hit captcha or failed for %s. Tried: %s",
        url[:80], methods_tried,
    )

    try:
        from scraper.models import ProbeCache
        ProbeCache.objects.update_or_create(
            domain=domain,
            defaults={
                "method": methods_tried[-1] if methods_tried else "unknown",
                "needs_akamai_bypass": False,
                "captcha_detected": True,
            },
        )
    except Exception:
        pass

    return {
        "success": False,
        "method": methods_tried[-1] if methods_tried else "none",
        "proxy_tier": "none",
        "status_code": 0,
        "title": "",
        "body_length": 0,
        "needs_browser": True,
        "blocked": True,
        "captcha_detected": True,
        "captcha_type": captcha_info.get("captcha_type", "unknown") if captcha_info else "unknown",
        "captcha_confidence": captcha_info.get("confidence", 0) if captcha_info else 0,
        "captcha_reasoning": captcha_info.get("reasoning", "") if captcha_info else "All methods returned captcha or failed",
        "methods_tried": methods_tried,
        "_request_url": url,
        "jsonld": [],
        "meta": {},
        "selector_results": {},
        "error": "All probe methods returned captcha pages or failed",
    }


def get_probe_tools() -> list:
    @tool
    def probe_page(url: str, render_js: bool = True) -> str:
        """Test page accessibility with automatic proxy escalation.

        Delegates to browser-service which runs the tier-first escalation chain:
        1. Direct HTTP (no proxy)
        2. Playwright (no proxy)
        3. UC Chrome (no proxy)
        4. Direct HTTP (datacenter proxy)
        5. Playwright (datacenter proxy)
        6. UC Chrome (datacenter proxy)
        7. Direct HTTP (residential proxy)
        8. Playwright (residential proxy)
        9. UC Chrome (residential proxy)

        If Akamai Bot Manager is detected, automatically escalates to
        the 3-layer Akamai bypass:
        Layer 1: TLS fingerprint pre-warming (curl_cffi)
        Layer 2: Playwright stealth browser with anti-fingerprinting
        Layer 3: SeleniumBase UC Chrome fallback

        The probe cache remembers which method worked for a domain,
        so subsequent probes skip straight to that method for speed.

        Returns the first successful result with page data including
        JSON-LD, meta tags, and common selector test results.

        Args:
            url: The fully-qualified URL to probe.
            render_js: If true, also try browser rendering (default true).

        Returns:
            Structured text describing what worked and what was extracted.
        """
        service_url = _get_browser_service_url()
        domain = _get_domain(url)

        from src.geo import detect_country as _detect_country
        country = _detect_country(url)

        cached = _get_cached_method(domain)
        start_method = None
        if cached:
            start_method = cached["method"]

        probe_payload: dict = {
            "url": url,
            "render_js": render_js,
            "timeout": PROBE_TIMEOUT,
        }
        if start_method:
            probe_payload["start_method"] = start_method
        if country:
            probe_payload["country"] = country

        logger.info(
            "probe_page: probing %s via %s (render_js=%s, cached_method=%s)",
            url[:200],
            service_url,
            render_js,
            start_method,
        )

        try:
            resp = httpx.post(
                f"{service_url}/probe",
                json=probe_payload,
                timeout=PROBE_TIMEOUT + 10,
            )
            resp.raise_for_status()
            data = resp.json()
            data["_request_url"] = url
            logger.info(
                "probe_page: result method=%s, success=%s, akamai=%s for %s",
                data.get("method"),
                data.get("success"),
                data.get("needs_akamai_bypass"),
                url[:100],
            )

            captcha_result = {"captcha_detected": False}
            if not start_method:
                captcha_result = _verify_captcha_free(data)
            if captcha_result.get("captcha_detected"):
                logger.info(
                    "probe_page: LLM detected captcha (%s) for %s, method=%s",
                    captcha_result.get("captcha_type"),
                    url[:80],
                    data.get("method"),
                )

            if not captcha_result.get("captcha_detected"):
                _save_probe_cache(domain, data)

            from .context import update_probe_result, get_probe_method

            update_probe_result(data)
            logger.info(
                "probe_page: updated context, probe_method=%s",
                get_probe_method(),
            )

            if data.get("needs_akamai_bypass") and not data.get("success"):
                logger.info(
                    "probe_page: Akamai detected, escalating to /probe-akamai for %s",
                    url[:100],
                )
                try:
                    ak_resp = httpx.post(
                        f"{service_url}/probe-akamai",
                        json={"url": url, "timeout": PROBE_TIMEOUT},
                        timeout=PROBE_TIMEOUT + 10,
                    )
                    ak_resp.raise_for_status()
                    ak_data = ak_resp.json()
                    ak_data["_request_url"] = url
                    logger.info(
                        "probe_page: akamai result method=%s, success=%s for %s",
                        ak_data.get("method"),
                        ak_data.get("success"),
                        url[:100],
                    )
                    _save_probe_cache(domain, ak_data)
                    from .context import update_probe_result

                    update_probe_result(ak_data)
                    return _format_probe_result(ak_data)
                except Exception as ak_exc:
                    logger.warning("probe_page: akamai escalation failed: %s", ak_exc)
                    return _format_probe_result(data)

            return _format_probe_result(data)

        except httpx.ConnectError:
            logger.error("probe_page: browser-service unreachable at %s", service_url)
            return (
                f"PROBE RESULT for {url}\n"
                f"Method: service_unavailable\n"
                f"Proxy tier: none\n"
                f"HTTP status: 0\n"
                f"Error: Browser service ({service_url}) is unreachable. "
                f"Ensure browser-service container is running.\n"
                f"\nAll methods failed — cannot test selectors."
            )
        except httpx.TimeoutException:
            logger.error("probe_page: browser-service timed out for %s", url[:100])
            return (
                f"PROBE RESULT for {url}\n"
                f"Method: timeout\n"
                f"Proxy tier: none\n"
                f"HTTP status: 0\n"
                f"Error: Browser service probe timed out after {PROBE_TIMEOUT}s.\n"
                f"\nAll methods failed — cannot test selectors."
            )
        except Exception as exc:
            logger.exception("probe_page: unexpected error for %s", url[:100])
            return (
                f"PROBE RESULT for {url}\n"
                f"Method: error\n"
                f"Proxy tier: none\n"
                f"HTTP status: 0\n"
                f"Error: {exc}\n"
                f"\nAll methods failed — cannot test selectors."
            )

    return [probe_page]
