import json
import logging
import os
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.page_analysis import (
    extract_jsonld,
    extract_meta_tags,
    extract_title,
    has_price,
    is_blocked,
    test_selectors_selenium,
)

from .config import get_proxy_config

logger = logging.getLogger(__name__)

DISPLAY = os.environ.get("DISPLAY", ":98")
DEFAULT_TIMEOUT = 60

PROXY_TIERS = ["none", "datacenter", "residential"]


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


def _dispatch_step(method_name: str, url: str, timeout: int):
    if method_name == "direct_http":
        return _try_direct_http(url, timeout=timeout, proxy_tier="none")
    if method_name.startswith("direct_http_"):
        tier = method_name.replace("direct_http_", "")
        return _try_direct_http(url, timeout=timeout, proxy_tier=tier)
    if method_name.startswith("playwright_"):
        tier = method_name.replace("playwright_", "")
        pw_timeout = 35 if tier != "none" else 25
        return _try_playwright(url, tier, timeout=min(timeout, pw_timeout))
    if method_name.startswith("uc_chrome_"):
        tier = method_name.replace("uc_chrome_", "")
        return _try_uc_chrome(url, tier, timeout=min(timeout, 40))
    return None


def run_probe(url: str, render_js: bool = True, timeout: int = 120, start_method: Optional[str] = None) -> dict[str, Any]:
    steps_log = []
    debug_path = "/tmp/probe_debug.json"

    def _log_step(msg):
        steps_log.append(msg)
        logger.info("PROBE [%s]: %s", url[:80], msg)

    _log_step(f"Starting probe: render_js={render_js}, timeout={timeout}, start_method={start_method}")

    skip_index = 0
    if start_method:
        for i, (step_name, _) in enumerate(ESCALATION_STEPS):
            if step_name == start_method:
                skip_index = i
                _log_step(f"Cache hint: starting at step {i} ({step_name})")
                break

    if not render_js:
        result = _try_direct_http(url, timeout=timeout)
        if result and result.get("success"):
            return result
        return result or _failure_result("all_failed", "none", "Direct HTTP failed and render_js=false")

    for i, (step_name, proxy_tier) in enumerate(ESCALATION_STEPS):
        if i < skip_index:
            continue

        _log_step(f"{step_name}: trying...")
        result = _dispatch_step(step_name, url, timeout)
        if result:
            _log_step(
                f"{step_name}: method={result.get('method')}, success={result.get('success')}, "
                f"body={result.get('body_length', 0)}, blocked={result.get('blocked')}, "
                f"err={result.get('error', '')[:120]}"
            )

        if result and result.get("needs_akamai_bypass"):
            _log_step(f"{step_name}: Akamai detected, stopping escalation")
            return result

        if result and result.get("success"):
            _log_step(f"{step_name}: SUCCEEDED")
            return result

    _log_step("ALL FAILED")
    try:
        with open(debug_path, "w") as f:
            json.dump({"url": url, "steps": steps_log}, f, indent=2)
    except Exception:
        pass
    return _failure_result("all_failed", "none", "All probe methods failed")


def _try_direct_http(url: str, timeout: int = 15, proxy_tier: str = "none") -> Optional[dict]:
    try:
        import httpx

        from src.page_analysis import get_user_agent

        config = get_proxy_config()
        proxy_url = config.build_proxy_url(proxy_tier) if proxy_tier != "none" else None

        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy_url,
            headers={"User-Agent": get_user_agent()},
        ) as client:
            resp = client.get(url)
        html = resp.text
        blocked = is_blocked(html[:5000])
        jsonld = extract_jsonld(html)
        meta = extract_meta_tags(html)
        title = extract_title(html)

        body_text = ""
        if len(html) > 500:
            import re as _re
            match = _re.search(r"<body[^>]*>(.*?)</body>", html, _re.DOTALL | _re.IGNORECASE)
            if match:
                raw = match.group(1)
                text = _re.sub(r"<[^>]+>", " ", raw)
                body_text = _re.sub(r"\s+", " ", text).strip()[:1500]

        if proxy_tier == "none" and _detect_akamai(html, resp.status_code):
            return {
                "success": False,
                "method": "direct_http",
                "proxy_tier": "none",
                "status_code": resp.status_code,
                "title": title,
                "body_length": len(html),
                "needs_browser": True,
                "blocked": True,
                "needs_akamai_bypass": True,
                "jsonld": jsonld,
                "meta": meta,
                "selector_results": "Skipped — Akamai detected",
                "error": "Akamai Bot Manager detected",
            }

        has_meaningful_content = len(html) > 2000 and not blocked
        has_price_in_jsonld = any(has_price(block) for block in jsonld)

        method_name = f"direct_http_{proxy_tier}" if proxy_tier != "none" else "direct_http"

        if has_meaningful_content:
            selector_results = "Skipped — direct HTTP"
            needs_browser = not has_price_in_jsonld and len(jsonld) == 0

            return {
                "success": True,
                "method": method_name,
                "proxy_tier": proxy_tier,
                "status_code": resp.status_code,
                "title": title,
                "body_length": len(html),
                "body_text": body_text,
                "needs_browser": needs_browser,
                "blocked": False,
                "jsonld": jsonld,
                "meta": meta,
                "selector_results": selector_results,
                "error": "",
            }

        return None

    except Exception as exc:
        logger.info("Direct HTTP (%s) failed: %s", proxy_tier, exc)
        return None


def _try_playwright(url: str, proxy_tier: str, timeout: int = 25) -> Optional[dict]:
    pw = None
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        config = get_proxy_config()
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs: dict[str, Any] = {"headless": True, "args": launch_args}

        proxy = config.build_playwright_proxy(proxy_tier) if proxy_tier != "none" else None
        if proxy:
            launch_kwargs["proxy"] = proxy

        pw = sync_playwright().start()
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.set_default_timeout(timeout * 1000)

        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2000)

        html = page.content()
        title = page.title() or ""
        blocked = is_blocked(html[:5000])
        jsonld = extract_jsonld(html)
        meta = extract_meta_tags(html)
        status_code = resp.status if resp else 0

        body_text = ""
        try:
            body_text = page.evaluate("() => document.body?.innerText?.substring(0, 1500) || ''")[:1500]
        except Exception:
            pass

        has_content = len(html) > 2000 and not blocked
        if has_content:
            from src.page_analysis import run_selector_tests

            selector_results = run_selector_tests(page)
            return {
                "success": True,
                "method": f"playwright_{proxy_tier}",
                "proxy_tier": proxy_tier,
                "status_code": status_code,
                "title": title[:200],
                "body_length": len(html),
                "body_text": body_text,
                "needs_browser": True,
                "blocked": False,
                "jsonld": jsonld,
                "meta": meta,
                "selector_results": selector_results,
                "error": "",
            }

        return {
            "success": False,
            "method": f"playwright_{proxy_tier}",
            "proxy_tier": proxy_tier,
            "status_code": status_code,
            "title": title[:200],
            "body_length": len(html),
            "body_text": body_text,
            "needs_browser": True,
            "blocked": True,
            "needs_akamai_bypass": _detect_akamai(html, status_code),
            "jsonld": jsonld,
            "meta": meta,
            "selector_results": "Skipped — page blocked or empty",
            "error": "Page blocked or empty content",
        }

    except Exception as exc:
        logger.info("Playwright (%s) failed: %s", proxy_tier, exc)
        return None
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def _try_uc_chrome(url: str, proxy_tier: str, timeout: int = 40) -> Optional[dict]:
    config = get_proxy_config()
    proxy_string = config.build_proxy_string(proxy_tier) if proxy_tier != "none" else None

    sb_kwargs = {
        "uc": True,
        "headless": True,
        "locale_code": "en",
    }
    if proxy_string:
        sb_kwargs["proxy"] = proxy_string

    try:
        from seleniumbase import SB

        with SB(**sb_kwargs) as sb:
            sb.driver.set_page_load_timeout(timeout)
            sb.open(url)
            time.sleep(3)

            title = sb.get_title() or ""
            html = sb.get_page_source()
            body_text = _get_body_text(sb, html)
            status_code = _get_status_code(sb, url)
            blocked = is_blocked(body_text[:3000]) or is_blocked(html[:5000])
            jsonld = extract_jsonld(html)

            has_meaningful_content = (
                len(html) > 5000
                and not blocked
                and (len(body_text) > 100 or len(jsonld) > 0 or len(title) > 3)
            )

            if has_meaningful_content:
                selector_results = test_selectors_selenium(sb.driver)
                return {
                    "success": True,
                    "method": f"uc_chrome_{proxy_tier}",
                    "proxy_tier": proxy_tier,
                    "status_code": status_code,
                    "title": title[:200],
                    "body_length": len(html),
                    "body_text": body_text[:1500],
                    "needs_browser": True,
                    "blocked": False,
                    "jsonld": jsonld,
                    "meta": {},
                    "selector_results": selector_results,
                    "error": "",
                }

            return {
                "success": False,
                "method": f"uc_chrome_{proxy_tier}",
                "proxy_tier": proxy_tier,
                "status_code": status_code,
                "title": title[:200],
                "body_length": len(html),
                "body_text": body_text[:1500],
                "needs_browser": True,
                "blocked": True,
                "needs_akamai_bypass": _detect_akamai(html, status_code),
                "jsonld": jsonld,
                "meta": {},
                "selector_results": {},
                "error": "Page blocked or empty content",
            }

    except Exception as exc:
        logger.info("UC Chrome (%s) failed: %s", proxy_tier, exc)
        return {
            "success": False,
            "method": f"uc_chrome_{proxy_tier}_error",
            "proxy_tier": proxy_tier,
            "status_code": 0,
            "title": "",
            "body_length": 0,
            "needs_browser": True,
            "blocked": True,
            "jsonld": [],
            "meta": {},
            "selector_results": {},
            "error": str(exc)[:500],
        }


def _get_body_text(sb, html: str) -> str:
    try:
        body_text = sb.driver.execute_script("return document.body?.innerText || ''") or ""
        if body_text:
            return body_text
    except Exception:
        pass
    try:
        body_text = sb.driver.execute_script("return document.body?.textContent || ''") or ""
        if body_text:
            return body_text
    except Exception:
        pass
    if len(html) > 200:
        import re
        match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
        if match:
            raw = match.group(1)
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()
            return text
    return ""


def _get_status_code(sb, url: str) -> int:
    try:
        logs = sb.driver.get_log("performance")
        for log in reversed(logs):
            try:
                msg = json.loads(log["message"])["message"]
                if msg.get("method") == "Network.responseReceived":
                    resp = msg.get("params", {}).get("response", {})
                    if resp.get("url", "").rstrip("/") == url.rstrip("/"):
                        return resp.get("status", 0)
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception:
        pass
    return 0


def _detect_akamai(html: str, status_code: int = 0) -> bool:
    lower = html[:5000].lower()
    signals = [
        "sec-if-cpt-container",
        "sec-cpt-if",
        "akamai_beacon",
        "sensor_data",
        "/akam/",
    ]
    if any(s in lower for s in signals):
        return True
    if status_code == 403 and len(html) < 5000:
        text = lower[:2000]
        if any(kw in text for kw in ["access denied", "blocked", "forbidden", "reference #"]):
            return True
    return False


def _failure_result(method: str, proxy_tier: str, error: str) -> dict[str, Any]:
    return {
        "success": False,
        "method": method,
        "proxy_tier": proxy_tier,
        "status_code": 0,
        "title": "",
        "body_length": 0,
        "needs_browser": True,
        "blocked": True,
        "jsonld": [],
        "meta": {},
        "selector_results": {},
        "error": error,
    }
