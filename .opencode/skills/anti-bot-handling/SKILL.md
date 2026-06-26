---
name: anti-bot-handling
description: Detect and handle anti-bot protection systems on ecommerce sites. Covers Cloudflare, Akamai, PerimeterX, CAPTCHAs, and stealth browser techniques.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
---

# Anti-Bot Detection & Handling

## What I Do

Identify anti-bot protection systems on ecommerce websites and provide strategies for bypassing them or working around them. Includes rate limiting recommendations and stealth browser setup.

## When to Use Me

Use this when:
- Site Analyzer agent detects protection challenges
- Requests return 403 Forbidden
- Browser shows "Just a moment..." or challenge pages
- Scrapers get blocked after a few requests

## Detection Methods

### HTTP Request Detection

```bash
# Check response headers and status
curl -sI "https://www.example.com" | grep -iE "cf-|akamai|cloudflare|server|403|503"
```

**Indicators:**
- `Server: cloudflare` → Cloudflare protection
- `X-Akamai-*` headers → Akamai protection
- `403 Forbidden` → Blocked
- `503 Service Unavailable` → Challenge page
- `Set-Cookie: __cf_bm` → Cloudflare bot management

### Browser Detection (Playwright MCP)

Navigate to site and check for challenge pages:

```
playwright_browser_navigate → site_url
playwright_browser_wait_for → time=5
playwright_browser_snapshot → check for challenge indicators
```

**Challenge page indicators:**
- "Just a moment..." text → Cloudflare
- "Checking your browser" → Generic challenge
- "Please verify you are human" → CAPTCHA
- "Access denied" → Hard block
- Nearly empty page with minimal HTML → JavaScript challenge

### Cookie Analysis

```
playwright_browser_evaluate → function: () => {
    return document.cookie;
}
```

**Protection cookies:**
- `__cf_bm`, `cf_clearance` → Cloudflare
- `_abck`, `akamai_cm_ccna_*` → Akamai
- `_pxhd`, `_pxvid` → PerimeterX / HUMAN
- `incap_ses_*`, `visid_incap_*` → Imperva

## Protection Systems

### Cloudflare

**Levels:**
1. **Basic (JS Challenge):** "Just a moment..." page, auto-resolves in 5 seconds
2. **Turnstile:** Invisible CAPTCHA widget, may need interaction
3. **Bot Management:** Blocks after detecting patterns, persistent cookies
4. **WAF:** Blocks specific request patterns

**Detection:**
- `Server: cloudflare` header
- `cf-ray`, `cf-cache-status` headers
- `__cf_bm` cookie

**Strategies:**
- **Level 1:** Wait 5 seconds for JS challenge to resolve
- **Level 2:** Use Playwright (headless) - Turnstile usually passes
- **Level 3:** Use stealth browser (undetected-chromedriver)
- **Level 4:** Reduce request rate, rotate user agents

### Akamai

**Detection:**
- `X-Akamai-*` headers
- `_abck`, `bm_sz`, `akacd_*` cookies
- "Akamai Bot Manager" in page source
- "UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS" block page

**Severity levels:**
- **Low:** `_abck` cookies present, but direct HTTP requests work (403 only for API endpoints)
- **High:** All HTTP requests return 403, even browser may be blocked without stealth

**Strategies:**

| Level | Approach | Success Rate |
|-------|----------|-------------|
| 1 | Wait 15-20s for Akamai sensor data collection | Low for high severity |
| 2 | **undetected-chromedriver (RECOMMENDED for high severity)** | High |
| 3 | Playwright MCP with stealth init scripts | Medium (may work initially, Akamai can adapt) |
| 4 | undetected-chromedriver + residential proxy | Very High |

**CRITICAL: undetected-chromedriver for Akamai**

When Akamai is detected at **high severity**, `undetected-chromedriver` is the recommended scraping engine (NOT Playwright). Key reasons:
- UC patches ChromeDriver at binary level to remove CDP detection vectors
- Playwright's Chromium fingerprint is consistently detected by Akamai sensor JS
- Even non-headless Playwright can be fingerprinted and blocked

```python
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as ec

options = uc.ChromeOptions()
options.add_argument('--disable-blink-features=AutomationControlled')
options.add_argument('--window-size=1920,1080')
options.add_argument('--lang=en-US')
options.add_argument(f'--proxy-server={proxy_url}')  # optional

driver = uc.Chrome(options=options, version_main=None)

# 1. Warmup: visit homepage, wait for Akamai
driver.get('https://www.example.com')
time.sleep(20)

# 2. Accept cookies (Akamai cookie wall blocks __NEXT_DATA__ until accepted)
clicked = driver.execute_script("""
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
if clicked:
    time.sleep(2)

# 3. Scrape
driver.get('https://www.example.com/product-page')
time.sleep(3)

# IMPORTANT: Use var-based JS, NOT arrow function IIFEs
# Selenium execute_script does NOT return values from (() => { ... })() patterns
product_data = driver.execute_script("""
var d = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
var ps = d.props.pageProps.initialState.productStore;
var keys = Object.keys(ps.products);
for (var i = 0; i < keys.length; i++) {
    var entry = ps.products[keys[i]];
    if (entry && entry.data && entry.data.name) return entry.data;
}
return null;
""")
```

**Known issues with undetected-chromedriver:**

1. **ChromeDriver version mismatch:** Use `version_main=None` (auto-detect) or pin to installed Chrome version (`version_main=145`). Error: "session not created: This version of ChromeDriver only supports Chrome version X"

2. **IIFE arrow functions return null:** `execute_script` cannot return values from `(() => { return x; })()`. Use `var`-based top-level statements instead:
   ```javascript
   // WRONG - returns null
   (() => { return document.title; })()
   // CORRECT
   return document.title;
   ```

3. **Cookie consent walls:** Some Akamai sites (e.g., adidas) show a full-page cookie wall that blocks page rendering. Must accept cookies before `__NEXT_DATA__` or any page content loads.

4. **Proxy compatibility:** Bright Data datacenter proxies may also be detected. Try direct connection first (`--no-proxy`), escalate to residential only if needed. Residential proxy must be configured with correct zone in username.

**When to recommend undetected-chromedriver in site_analysis.json:**

```json
{
  "anti_bot": {
    "detected": true,
    "type": "akamai",
    "severity": "high",
    "details": "All HTTP requests return 403. Playwright MCP may work initially but Akamai can adapt.",
    "recommendations": [
      "RECOMMEND undetected-chromedriver for scraping engine (not Playwright)",
      "Warm up session: visit homepage, wait 20s, accept cookies",
      "Use var-based JavaScript in execute_script (NOT arrow function IIFEs)",
      "Rate limit to 4-5 seconds between navigations",
      "Do NOT use direct HTTP requests — all will be blocked"
    ],
    "uc_recommended": true
  }
}
```

### PerimeterX / HUMAN

**Detection:**
- `_pxhd`, `_pxvid` cookies
- `px-cdn` script tags
- HUMAN security messages

**Strategies:**
- **Level 1:** Use Playwright with stealth plugin
- **Level 2:** Reduce request rate significantly
- **Level 3:** Use residential proxies

### CAPTCHA (reCAPTCHA, hCaptcha, Turnstile)

**Detection:**
- iframe with `recaptcha`, `hcaptcha`, `turnstile`
- Visual CAPTCHA image grid
- Checkbox "I'm not a robot"

**Strategies:**
- **Invisible reCAPTCHA:** Usually passes with Playwright
- **Checkbox reCAPTCHA:** Click checkbox, may need to solve
- **Image CAPTCHA:** Requires CAPTCHA solving service
- **Turnstile:** Usually passes with headless browser

## Rate Limiting Recommendations

Based on protection level:

| Protection | Delay (seconds) | Max Requests/Min |
|-----------|----------------|------------------|
| None | 0.5 | 120 |
| Low (basic JS) | 1-2 | 30-60 |
| Medium (Cloudflare basic) | 2-3 | 20-30 |
| High (Cloudflare Turnstile) | 3-5 | 12-20 |
| Very High (Akamai/Bot Mgmt) | 5-10 | 6-12 |
| CAPTCHA | 10+ | 6 |

## Stealth Browser Patterns

### Python: undetected-chromedriver

```python
import undetected_chromedriver as uc

options = uc.ChromeOptions()
options.add_argument('--disable-blink-features=AutomationControlled')
options.add_argument('--disable-infobars')
options.add_argument('--no-first-run')
driver = uc.Chrome(options=options)

# Session warmup (CRITICAL)
driver.get('https://www.example.com')
time.sleep(15)  # Wait for challenge to resolve

# Now scrape normally
driver.get('https://www.example.com/products')
```

### Python: Playwright with stealth

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--disable-infobars',
            '--no-first-run'
        ]
    )
    context = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        viewport={'width': 1920, 'height': 1080}
    )
    page = context.new_page()

    # Warmup
    page.goto('https://www.example.com')
    page.wait_for_timeout(10000)

    # Scrape
    page.goto('https://www.example.com/products')
```

## Session Warmup Procedure

**ALWAYS warm up sessions before scraping:**

1. Visit homepage first
2. Wait 10-15 seconds for challenge to resolve
3. Navigate to a non-product page (About, Contact)
4. Wait 5 seconds
5. Now navigate to product listing
6. Wait 5 seconds
7. Begin scraping with appropriate delays

**Why warmup matters:**
- Anti-bot systems flag immediate product page visits
- Session cookies need to be established
- Fingerprinting needs baseline data
- Risk scores need to stabilize

## What to Report Back

When protection is detected, report in `site_analysis.json`:

```json
{
  "anti_bot": {
    "detected": true,
    "type": "cloudflare",
    "severity": "medium",
    "challenge_type": "js_challenge",
    "details": "Cloudflare JS challenge resolves after ~5 seconds",
    "recommendations": [
      "Use Playwright browser automation",
      "Wait 5 seconds on initial page load",
      "Rate limit to 2-3 seconds between requests",
      "Warm up session with homepage visit first"
    ]
  }
}
```

## When NOT to Use

- Site has no protection (waste of time)
- Protection is handled by the platform (Shopify's built-in)
- Simple rate limiting that just needs delay adjustment

## Learned: TLS Fingerprinting Silent Block (No Visible Challenge)
**Source:** https://bulgari.com (2025-07-10)
**Applicability:** Luxury brand sites, LVMH group sites, and other enterprises that block based on browser TLS fingerprint without showing any challenge page

Some sites silently reject non-authentic browsers (standard Playwright, direct HTTP) without showing any visible challenge page, CAPTCHA, or "Just a moment..." message. Instead of serving a challenge, they simply return empty or error responses to browsers that fail TLS/JA3 fingerprint checks. The page loads fine only with undetected Chrome (seleniumbase UC mode).

**Detection indicators:**
- Direct HTTP requests fail (no response, timeout, or empty body)
- Standard Playwright gets blocked (page doesn't render content)
- `uc_chrome_none` or `seleniumbase_uc` connectivity method succeeds
- No anti-bot cookies (`__cf_bm`, `_abck`, etc.) are present
- No challenge page text ("Just a moment", "Checking your browser")
- No CAPTCHA iframes

**Strategy:** Use `seleniumbase_uc` (undetected Chrome) as the scraping mechanism. Standard Playwright and HTTP requests will not work. No proxy escalation needed — the block is purely fingerprint-based.

**site_analysis.json pattern:**
```json
{
  "anti_bot": {
    "detected": false,
    "type": "none",
    "severity": "low",
    "details": "No explicit anti-bot challenge detected. Site only responds to undetected Chrome browsers — likely TLS fingerprinting."
  },
  "connectivity": {
    "method_that_worked": "uc_chrome_none",
    "js_rendering_needed": true
  }
}
```

**Key distinction from Akamai/Cloudflare:** This pattern has `anti_bot.detected: false` because there's no visible protection system. The block happens at the TLS layer, not via a JavaScript challenge. This means the site_analyzer will NOT trigger anti-bot handling, but connectivity will indicate only UC Chrome works.

**Sites observed:** bulgari.com (LVMH)
