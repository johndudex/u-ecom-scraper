import json
import re
import logging

logger = logging.getLogger(__name__)

COMMON_SELECTORS = {
    "h1": "h1",
    "product_title_h1": "h1:first-of-type",
    "price_data_price": "[data-price]",
    "price_data_testid": "[data-testid*='price' i]",
    "price_class_price": "[class*='price' i]",
    "price_class_pdp": "[class*='_pdp_'] [data-testid*='price' i]",
    "availability": "[data-availability], [data-testid*='availability' i], [class*='stock' i]",
    "original_price": "[class*='not-reduced' i], [class*='compare-at' i], [class*='was-price' i], [class*='original-price' i]",
    "description": "[data-testid*='description' i], [class*='description' i]",
    "sku": "[data-sku], [itemprop='sku'], [data-testid*='sku' i]",
    "brand": "[itemprop='brand'], [data-testid*='brand' i]",
    "rating": "[itemprop='aggregateRating'], [class*='rating' i]",
}

BLOCK_PATTERNS = [
    "access denied",
    "robot check",
    "unusual activity",
    "too many requests",
    "are you a robot",
    "please verify you are a human",
    "cf-browser-verification",
    "referenceerror",
    "akamai bot manager",
    "akamai detected",
    "akamai _abck",
    "akamai_gtb",
    "akamai_bm_sv",
    "akamai_telemetry",
    "please complete the security check",
    "just a moment",
    "checking your browser",
    "please enable javascript",
    "attention required",
    "blocked",
    "ray id",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def get_user_agent() -> str:
    return _USER_AGENT


def is_blocked(text: str) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in BLOCK_PATTERNS)


def extract_jsonld(html: str) -> list[dict]:
    blocks = []
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, list):
                blocks.extend(data)
            elif isinstance(data, dict):
                blocks.append(data)
        except (json.JSONDecodeError, ValueError):
            pass
    return blocks


def has_price(jsonld_block: dict) -> bool:
    offers = jsonld_block.get("offers", {})
    if isinstance(offers, list):
        return any(o.get("price") for o in offers if isinstance(o, dict))
    if isinstance(offers, dict):
        return bool(offers.get("price"))
    return False


def extract_meta_tags(html: str) -> dict[str, str]:
    tags = {}
    for match in re.finditer(
        r'<meta\s+(?:property|name)=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']',
        html,
        re.IGNORECASE,
    ):
        tags[match.group(1).lower()] = match.group(2)
    return tags


def extract_title(html: str) -> str:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_match:
        return title_match.group(1).strip()[:200]
    return ""


def summarize_jsonld(blocks: list[dict]) -> str:
    if not blocks:
        return "None found"
    lines = []
    for i, block in enumerate(blocks):
        schema_type = block.get("@type", "unknown")
        if isinstance(schema_type, list):
            schema_type = " / ".join(schema_type)
        fields = [k for k in block.keys() if k not in ("@context", "@type")]
        offers = block.get("offers", {})
        offers_status = "EMPTY" if offers == {} or not offers else "present"
        if isinstance(offers, list):
            offers_status = f"{len(offers)} offer(s)"
        lines.append(
            f"  Block {i + 1}: type={schema_type}, "
            f"fields=[{', '.join(fields[:15])}], offers={offers_status}"
        )
    return "\n".join(lines)


def summarize_meta_tags(meta: dict[str, str]) -> str:
    og_tags = {k: v for k, v in meta.items() if k.startswith("og:")}
    if not og_tags:
        return "Open Graph tags: none"
    lines = ["Open Graph tags:"]
    for k, v in og_tags.items():
        lines.append(f"  {k}: {v[:200]}")
    return "\n".join(lines)


def test_selectors_from_html(html: str) -> str:
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(html)
        results = run_selector_tests(page)
        browser.close()
        pw.stop()
        return results
    except Exception as exc:
        logger.warning("Selector testing from HTML failed: %s", exc)
        return f"Could not test selectors: {exc}"


def test_selectors_on_page(page) -> str:
    return run_selector_tests(page)


def run_selector_tests(page) -> str:
    results = []
    for name, selector in COMMON_SELECTORS.items():
        try:
            elements = page.query_selector_all(selector)
            if not elements:
                results.append(f"  {name} ({selector}): NOT FOUND")
                continue
            first_text = ""
            for el in elements[:3]:
                text = (el.inner_text() or "").strip()[:100]
                if text:
                    first_text = text
                    break
            count = len(elements)
            if first_text:
                results.append(f"  {name} ({selector}): \"{first_text}\" [found: {count}]")
            else:
                results.append(f"  {name} ({selector}): EMPTY [found: {count}]")
        except Exception as exc:
            results.append(f"  {name} ({selector}): ERROR - {exc}")
    return "\n".join(results)


def test_selectors_selenium(driver) -> dict[str, dict]:
    results = {}
    for name, selector in COMMON_SELECTORS.items():
        try:
            elements = driver.find_elements("css selector", selector)
            if not elements:
                results[name] = {"found": False, "count": 0, "text": ""}
                continue
            first_text = ""
            for el in elements[:3]:
                text = (el.text or "").strip()[:100]
                if text:
                    first_text = text
                    break
            results[name] = {
                "found": bool(first_text),
                "count": len(elements),
                "text": first_text,
            }
        except Exception:
            results[name] = {"found": False, "count": 0, "text": "", "error": True}
    return results


def format_probe_result(
    url: str,
    method: str,
    proxy_tier: str,
    status_code: int,
    title: str,
    body_length: int,
    js_needed: bool,
    blocked: bool,
    jsonld_blocks: list[dict],
    meta_tags: dict[str, str],
    selector_results: str,
    error: str = "",
) -> str:
    parts = [
        f"PROBE RESULT for {url}",
        f"Method: {method}",
        f"Proxy tier: {proxy_tier}",
        f"HTTP status: {status_code}",
        f"Page title: {title}",
        f"Body length: {body_length} chars",
        f"JS rendering needed: {js_needed}",
        f"Anti-bot blocked: {blocked}",
    ]
    if error:
        parts.append(f"Error: {error}")
    parts.append("")
    parts.append(f"JSON-LD blocks: {len(jsonld_blocks)}")
    parts.append(summarize_jsonld(jsonld_blocks))
    parts.append("")
    parts.append(summarize_meta_tags(meta_tags))
    parts.append("")
    parts.append("Common selectors:")
    parts.append(selector_results)
    return "\n".join(parts)
