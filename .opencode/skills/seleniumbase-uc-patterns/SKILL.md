---
name: seleniumbase-uc-patterns
description: Patterns for writing SeleniumBase UC Mode scrapers. Covers SB() constructor kwargs, CDP Mode JS execution, UC navigation (uc_open_with_reconnect), warmup, EMPTY_PAGE_BLOCK avoidance, xvfb usage, and two-phase architecture. This is the authoritative reference — code-writer MUST follow these patterns when strategy is seleniumbase_uc.
---

# SeleniumBase UC Mode — Authoritative Reference

## SB() Constructor — Valid Kwargs ONLY (SeleniumBase 4.44+)

```python
with SB(
    uc=True,              # Enable UC Mode (undetected-chromedriver)
    xvfb=True,           # Use Xvfb virtual display (REQUIRED for headless Linux/Docker — do NOT use headless=True with uc=True)
    locale_code="en-gb",  # Browser locale (optional)
    proxy="host:port",    # Proxy server (optional, no auth — auth requires extension_zip below)
    chromium_arg=["--proxy-server=http://host:port", "--disable-blink-Features=AutomationControlled"],  # Extra Chrome flags (optional, list or comma-separated string)
    extension_zip="/path/to/auth_ext.zip",  # Load Chrome extension ZIP (optional — for proxy auth)
    page_load_strategy="eager",  # Document readiness (optional)
) as sb:
    driver = sb.driver
```

**INVALID kwargs the LLM keeps guessing (NEVER use these):**
- `browser_args` — WRONG, use `chromium_arg`
- `chrome_args` — WRONG, use `chromium_arg`
- `headless=True` with `uc=True` — UNRELIABLE on Linux, use `xvfb=True` instead
- `driver_kwargs` — NOT a SB() parameter

**Proxy with authentication (residential proxy):**
SeleniumBase UC Mode cannot pass proxy credentials directly. Use a Chrome extension:

```python
# 1. Create proxy auth extension ZIP (the template has _make_proxy_auth_extension())
ext_path = _make_proxy_auth_extension(proxy_host, proxy_port, proxy_user, proxy_pass)

# 2. Pass both chromium_arg (for --proxy-server flag) and extension_zip (for auth)
with SB(
    uc=True, xvfb=True,
    proxy=f"{host}:{port}",
    chromium_arg=[f"--proxy-server=http://{host}:{port}"],
    extension_zip=ext_path,
) as sb:
    driver = sb.driver
```

Do NOT use `use_auto_ext` for proxy auth — that enables Chrome's built-in automation extension,
not custom extension loading. Use `extension_zip` instead.

**NOTE:** The template (`templates/undetected_chromedriver_scraper.py`) already has the correct
proxy pattern with `_make_proxy_auth_extension()` and `_make_sb_kwargs()`. Read and follow it.

## JavaScript Execution — Use driver.execute_script() Directly

**IMPORTANT: Do NOT use `sb.execute_script()` or `sb.driver.execute_script()` in UC Mode.**

When UC Mode activates CDP Mode, `sb.execute_script()` doesn't support top-level `return` statements. And `sb.driver.execute_script()` (raw WebDriver API) can crash the CDP connection.

**CORRECT pattern (from the undetected_chromedriver_scraper.py template):**

```python
with SB(uc=True, xvfb=True) as sb:
    driver = sb.driver

    # All JS execution goes through driver.execute_script() directly
    # This is the raw WebDriver API, NOT CDP Mode, so top-level return IS allowed
    title = driver.execute_script("return document.title || '';")

    # Multi-line JS — return a dict/array
    data = driver.execute_script("""
        var jsonld = document.querySelector('script[type="application/ld+json"]');
        if (jsonld) return JSON.parse(jsonld.textContent);
        return null;
    """)
```

**Do NOT wrap JS in IIFEs when using `driver.execute_script()` directly.** IIFEs are only needed with `sb.execute_script()`. Since we always use `driver.execute_script()`, IIFEs are NOT needed.

## Page Navigation — Use driver.uc_open_with_reconnect()

SeleniumBase UC Mode has `sb.open()` but it triggers built-in EMPTY_PAGE_BLOCK detection that can kill the session. Use `driver.uc_open_with_reconnect()` instead:

```python
def open_page(driver, url, reconnect_time=4):
    """Navigate to a URL using UC Mode's reconnect logic."""
    driver.uc_open_with_reconnect(url, reconnect_time=reconnect_time)
    time.sleep(3)
```

`uc_open_with_reconnect` handles:
- CDP connection drops (auto-reconnects)
- Page load timeouts
- Anti-bot redirects
- Blank page detection is more lenient than `sb.open()`

## EMPTY_PAGE_BLOCK — What It Is and How to Avoid It

SeleniumBase has a built-in detector that kills the session if the page body appears blank/empty after navigation. This triggers when:
- The page hasn't fully rendered yet
- The site serves a JavaScript-only page that takes time to hydrate
- Anti-bot redirects are slow

**Avoid it by:**
1. Using `driver.uc_open_with_reconnect()` instead of `sb.open()`
2. Adding a longer sleep after navigation (3-5 seconds minimum)
3. Not checking page content too early

```python
# WRONG — triggers EMPTY_PAGE_BLOCK on slow-rendering sites
with SB(uc=True, xvfb=True) as sb:
    sb.open(url)  # May trigger EMPTY_PAGE_BLOCK
```

```python
# CORRECT — uc_open_with_reconnect is lenient
with SB(uc=True, xvfb=True) as sb:
    driver = sb.driver
    driver.uc_open_with_reconnect(url, reconnect_time=4)
    time.sleep(5)  # Wait for JS hydration
```

## Warmup Pattern (Working — from undetected_chromedriver_scraper.py template)

```python
WARMUP_WAIT = 20  # Anti-bot sensor data collection time
UC_RECONNECT_TIME = 4  # Reconnect delay for uc_open_with_reconnect

def warmup_session(driver):
    driver.uc_open_with_reconnect(SITE_URL, reconnect_time=UC_RECONNECT_TIME)
    logger.info(f"Waiting {WARMUP_WAIT}s for anti-bot sensor data collection...")
    time.sleep(WARMUP_WAIT)

    # Check for anti-bot blocks
    block_type = driver.execute_script("""
        var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
        if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
        if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
        if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
        return null;
    """)
    if block_type:
        logger.error(f"{block_type.upper()} BLOCK DETECTED during warm-up")
        return False

    # Accept cookie consent
    driver.execute_script("""
        var btns = document.querySelectorAll("button[data-auto-id='accept-cookie-btn']");
        if (btns.length > 0) { btns[0].click(); return true; }
        var all = document.querySelectorAll('button');
        for (var i = 0; i < all.length; i++) {
            var t = all[i].textContent.trim().toLowerCase();
            if (t === 'accept' || t === 'accept all cookies' || t === 'accept all') {
                all[i].click(); return true;
            }
        }
        return false;
    """)
    time.sleep(2)

    logger.info("Warm-up complete")
    return True
```

## Per-Page Session Architecture (REQUIRED for Akamai/anti-bot sites)

Anti-bot systems (Akamai Bot Manager, Cloudflare Bot Management) detect multi-page
scraping sessions and block them. probe_page works because it creates a FRESH SB()
session per page. The scraper must do the same.

**Why this matters:** The scraper_analyzer verifies strategy works using probe_page (one fresh
SB() per probe call). But the scraper then tried to reuse ONE session for all pages and
gets blocked. This is the root cause of "probe works but scraper doesn't" failures.

**Architecture:**
```python
# WRONG — single session for all pages (gets blocked after 2-3 pages):
with SB(uc=True, xvfb=True) as sb:
    driver = sb.driver
    warmup_session(driver)  # homepage OK
    for url in product_urls:
        open_page(driver, url)  # BLOCKED on 3rd or 4th page
        data = driver.execute_script(EXTRACT_PRODUCT_JS)

# CORRECT — fresh SB() per product page (each page is independent):
def scrape_product_per_session(url, src_url, index):
    with SB(uc=True, xvfb=True) as sb:
        driver = sb.driver
        open_page(driver, url)
        data = driver.execute_script(EXTRACT_PRODUCT_JS, src_url)
        return data

for i, url in enumerate(product_urls):
    product = scrape_product_per_session(url, src_url, i + 1)
```

**Discovery phase** (Phase 1) still uses a single session (warmup once, then discover
multiple pages). This works because discovery visits are category pages (not product pages)
and typically completes before anti-bot kicks in. If discovery gets blocked, add a session reset.

**Performance:** ~5-8 seconds per product page (SB() startup + warmup). Slower than single-
session but actually produces data instead of being blocked.

## Session Stability

1. Use `driver.uc_open_with_reconnect()` for ALL page navigations (not `sb.open()`)
2. Add `time.sleep(3)` between page navigations
3. The driver is obtained from `driver = sb.driver` inside the `with SB() as sb:` block
4. Check session health with `driver.current_url` if needed — if it raises an exception, the session is dead

## URL Parsing

Strip quotes from CLI args:

```python
if args.urls:
    args.urls = [u.strip('"\'') for u in args.urls]
```

## Two-Phase Architecture

For navigation-based scraping:

**Phase 1 — Discover product URLs from category/search pages:**
```python
def discover_product_urls(driver):
    driver.uc_open_with_reconnect(PRODUCT_LISTING_URL, reconnect_time=UC_RECONNECT_TIME)
    time.sleep(3)
    return driver.execute_script("""
        var links = document.querySelectorAll('a[href*="/k"]');
        var seen = {};
        var unique = [];
        for (var i = 0; i < links.length; i++) {
            var href = links[i].getAttribute('href');
            if (href && !seen[href]) {
                seen[href] = true;
                if (href.indexOf('http') !== 0) {
                    href = window.location.origin + href;
                }
                unique.push(href);
            }
        }
        return unique;
    """) or []
```

**Phase 2 — Extract data from each product page:**
Use JSON-LD primary extraction with CSS fallbacks (see template for full example).

## JSON-LD Extraction Pattern

```python
EXTRACT_PRODUCT_JS = """
var product = {
    title: '',
    price: '',
    availability: '',
    original_price: '',
    currency: '',
    url: window.location.href,
    src_url: arguments[0] || '',
    remarks: ''
};

var jsonld = null;
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
for (var i = 0; i < scripts.length; i++) {
    try {
        var data = JSON.parse(scripts[i].textContent);
        var items = Array.isArray(data) ? data : [data];
        for (var j = 0; j < items.length; j++) {
            if (items[j]['@type'] === 'Product' || items[j]['@type'] === 'ProductGroup') {
                jsonld = items[j];
                break;
            }
        }
        if (jsonld) break;
    } catch(e) {}
}

product.title = (jsonld && jsonld.name) || '';

if (jsonld && jsonld.offers) {
    var offers = Array.isArray(jsonld.offers) ? jsonld.offers[0] : jsonld.offers;
    product.price = offers.price || '';
    var highPrice = offers.highPrice || '';
    if (highPrice && parseFloat(highPrice) > parseFloat(product.price || 0)) {
        product.original_price = highPrice;
    }
    product.currency = offers.priceCurrency || '';
    var avail = offers.availability || '';
    product.availability = avail.indexOf('InStock') !== -1 ? 'In Stock' : 'Out of Stock';
}

return product;
"""

# Usage — pass src_url as the first argument
data = driver.execute_script(EXTRACT_PRODUCT_JS, src_url)
```
