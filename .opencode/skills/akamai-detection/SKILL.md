# Akamai Bot Manager Detection & Bypass

## Overview

Akamai Bot Manager (BMP) is an enterprise-grade anti-bot system used by major ecommerce sites (adidas, Nike, Samsung, etc.). It uses a multi-layered detection approach:

1. **TLS fingerprinting** — JA3/JA4 handshakes to identify non-browser clients
2. **JavaScript challenges** — Browser-specific computations that generate `_abck` cookies
3. **Behavioral analysis** — Mouse movements, scroll patterns, timing analysis
4. **IP reputation** — Datacenter IP ranges flagged, residential IPs more trusted

## Detection Signals

The probe system automatically detects Akamai by checking for these signals in HTTP responses:

- `sec-if-cpt-container` — Akamai challenge iframe
- `sec-cpt-if` — Challenge container element
- `akamai_beacon` — Akamai tracking beacon
- `sensor_data` — Sensor data collection endpoint
- `/akam/` — Akamai internal path
- 403 status with short body containing "access denied", "blocked", "reference #"

When detected, the probe returns `needs_akamai_bypass: true` and the system automatically escalates to the 3-layer bypass.

## 3-Layer Bypass Architecture

### Layer 1: TLS Fingerprint Pre-warming (`curl_cffi`)

- Uses `curl_cffi` to impersonate real browser TLS handshakes (Chrome 131, 124, etc.)
- Makes initial HTTP request to acquire `_abck` cookies
- Fast (~2-5s) but cannot solve JavaScript challenges alone
- Cookies saved for reuse by subsequent layers

### Layer 2: Playwright Stealth Browser

- Launches temporary Playwright Chromium instance (NOT the MCP Chrome)
- Injects comprehensive anti-fingerprinting script:
  - `navigator.webdriver` override
  - Canvas fingerprint noise injection
  - WebGL renderer spoofing
  - Chrome runtime emulation
  - AudioContext fingerprint noise
  - Performance.now timing jitter
  - iframe contentWindow patching
- Simulates human behavior: mouse movements, scrolling, delays
- Waits up to 30s for Akamai challenge to resolve
- Clicks challenge buttons if present (`#sec-cpt-if`, `button.sec-bc-button`)
- Reuses cookies from Layer 1

### Layer 3: UC Chrome Fallback (SeleniumBase)

- Uses SeleniumBase's `uc=True` mode (patched Chromium)
- Calls `sb.uc_open_with_reconnect()` with reconnect timing
- Falls back to `sb.uc_gui_click_captcha()` for manual CAPTCHA solve
- Last resort — most expensive in resources

## Automatic Escalation Flow

```
probe_page(url)
  └─> browser-service /probe
       ├─ Direct HTTP → checks for Akamai signals
       ├─ If Akamai detected: returns needs_akamai_bypass=true
       └─ probe_tools.py detects flag
            └─> browser-service /probe-akamai
                 ├─ Layer 1: TLS pre-warm (curl_cffi)
                 ├─ Layer 2: Playwright stealth
                 └─ Layer 3: UC Chrome fallback
```

## Cookie Management

- Cookies stored per-domain in `/app/data/akamai-cookies/{domain}.json`
- Cookies expire after 4 hours (`max_cookie_age_hours`)
- Valid `_abck` cookies checked: length > 30, no `~0.` pattern
- Invalid cookies auto-cleared before retry

## Concurrency

- Regular probes: `PROBE_LOCK` (1 at a time)
- Akamai probes: `AKAMAI_SEMAPHORE` (max 2 concurrent)
- Akamai probes do NOT hold the regular probe lock
- Each Akamai probe takes 30-90s depending on challenge complexity

## Known Akamai-Protected Sites

| Site | Domain | Notes |
|------|--------|-------|
| Adidas | adidas.ie, adidas.com | Primary test target |
| Nike | nike.com | Heavy Akamai deployment |
| Samsung | samsung.com | BMP + sensor data |
| Foot Locker | footlocker.com | Regional variations |

## For Scraper Code Generation

When the site analysis detects Akamai, the code-writer should use the `akamai_stealth_scraper.py` template which:
- Imports `src.akamai_bypass` modules
- Uses `AkamaiOrchestrator` for page access
- Extracts data after bypass completes
- Saves cookies between requests for session persistence
- Handles rate limiting with delays between pages

## Endpoint Reference

### POST /probe-akamai

```json
{
  "url": "https://www.adidas.ie/product/123",
  "proxy_tier": "none",
  "timeout": 120
}
```

Response format matches `/probe` response:
```json
{
  "success": true,
  "method": "akamai_playwright_stealth",
  "proxy_tier": "none",
  "status_code": 200,
  "title": "Product Page Title",
  "body_length": 45000,
  "needs_browser": true,
  "blocked": false,
  "jsonld": [...],
  "meta": {...},
  "selector_results": "...",
  "error": ""
}
```
