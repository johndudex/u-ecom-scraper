---
name: sfcc-detection
description: Detect Salesforce Commerce Cloud (Demandware) ecommerce sites and leverage their server-rendered HTML, JSON-LD structured data, and predictable URL patterns for efficient product data extraction.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
  learned_from: apac.christianlouboutin.com
  learned_date: 2026-06-06
---

# Salesforce Commerce Cloud (SFCC / Demandware) Detection & Scraping

## What I Do

Detect Salesforce Commerce Cloud (formerly Demandware) ecommerce sites and use their server-rendered HTML, JSON-LD structured data, and predictable URL patterns for efficient product data extraction. SFCC is one of the most common enterprise ecommerce platforms, used by major fashion, beauty, and lifestyle brands worldwide.

## When to Use Me

Use this when:
- The Site Analyzer agent is analyzing a website
- Platform detection phase is running
- A potential SFCC/Demandware site has been identified
- The product analyzer needs field extraction guidance for SFCC pages

## Detection Methods

### Method 1: Asset URLs

Check for Demandware static asset patterns in the page source:

```html
<!-- Static assets -->
<img src="/on/demandware.static/Sites-LOUBOUTIN_APAC-Site/...">
<link href="https://demandware.edgesuite.net/..." rel="stylesheet">
```

### Method 2: Cookies

SFCC sites set characteristic cookies:

| Cookie | Purpose |
|--------|---------|
| `dw_dnt` | "Do Not Track" preference |
| `dwsid` | Session ID |
| `dwac_*` | Cart/analytics cookies |
| `dwcookies_*` | Cookie consent state |

### Method 3: HTML Data Attributes

SFCC templates use `data-action` attributes that map to controller actions:

```html
<div data-action="Product-Show" data-pid="1234567890">
<div data-action="Cart-Show">
<div data-action="Search-Show">
```

### Method 4: Form Actions

Form submissions route through Demandware's URL structure:

```html
<form action="/on/demandware.store/Sites-LOUBOUTIN_APAC-Site/en_AU/Product-Variation">
```

Pattern: `/on/demandware.store/Sites-{SITE_NAME}-Site/{locale}/{Controller}-{Action}`

### Method 5: Site Identifier Patterns

SFCC uses `Sites-{SITENAME}-Site` conventions throughout:

```html
<!-- In URLs, JS, meta tags -->
Sites-LOUBOUTIN_APAC-Site
Sites-{BRAND}-Site
Sites-{BRAND}_{REGION}-Site
```

### Method 6: JavaScript Globals

Check for SFCC JavaScript objects:

```javascript
// Window globals set by SFCC
window.app = { ... }
window.demandware = { ... }
window.User = { ... }

// Analytics script
dw-analytics.js
```

### Method 7: URL Patterns

Some SFCC sites still use `*.demandware.net` domains, but most brands use custom domains. If you see `demandware.net` in the URL, it is definitively SFCC.

## Scraping Mechanism

### Recommended: Level 3 (HTTP + BeautifulSoup)

SFCC sites are **server-side rendered** — no JavaScript execution is needed for core product data. This makes them excellent candidates for fast HTTP-based scraping.

**Why HTTP > Playwright for SFCC:**
- Server renders all product data in HTML (title, price, availability, images)
- JSON-LD structured data is embedded in `<head>` — complete Product schema
- `data-gtm` attributes carry additional ecommerce data (populated server-side on many sites)
- No anti-bot protection on most SFCC sites (some brands add Cloudflare as an overlay)
- Dramatically faster than browser automation

**Anti-bot note:** Most SFCC sites have NO anti-bot protection. Some enterprise brands may layer Cloudflare on top. Check for Cloudflare first — if present, use the anti-bot-handling skill.

## Product Page URL Patterns

### Standard Format

SFCC product URLs follow a predictable pattern:

```
/{locale}/{slug}-{sku}.html
```

The SKU (Product ID) is embedded in the URL filename before `.html`.

### SKU Extraction from URL

```python
import re

# Extract SKU from URL (last segment before .html)
url = "https://example.com/au_en/kate-black-3191411BK01.html"
match = re.search(r"-([A-Za-z0-9]+)\.html$", url)
if match:
    sku = match.group(1)  # "3191411BK01"
```

### Product vs Non-Product URLs

```python
from urllib.parse import urlparse

def is_product_url(url: str) -> bool:
    """Product pages end with .html. Category pages end with /."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return path.endswith(".html")
```

## Multi-Region/Locale Handling

**This is one of the most critical aspects of SFCC scraping.** SFCC supports two distinct locale URL formats:

### Format 1: EU/US — Simple 2-Letter Codes

Used for major markets with a single dominant language:

| Locale | Market | Example URL |
|--------|--------|-------------|
| `/uk/` | United Kingdom | `/uk/shoes-pid.html` |
| `/us/` | United States | `/us/shoes-pid.html` |
| `/eu/` | European Union | `/eu/shoes-pid.html` |
| `/row/` | Rest of World | `/row/shoes-pid.html` |

### Format 2: APAC — `{country}_{language}` 5-Letter Codes

Used for Asian-Pacific markets with multiple languages per country:

| Locale | Country | Currency | Languages |
|--------|---------|----------|-----------|
| `au_en` | Australia | AUD | English |
| `nz_en` | New Zealand | NZD | English |
| `sg_en` / `sg_sc` | Singapore | SGD | English, Simplified Chinese |
| `hk_en` / `hk_tc` | Hong Kong | HKD | English, Traditional Chinese |
| `my_en` / `my_sc` | Malaysia | MYR | English, Simplified Chinese |
| `ph_en` | Philippines | PHP | English |
| `kr_en` | Korea | KRW | English |
| `th_en` | Thailand | THB | English |
| `tw_en` / `tw_tc` | Taiwan | TWD | English, Traditional Chinese |
| `cn_sc` | China | CNY | Simplified Chinese |
| `jp_ja` | Japan | JPY | Japanese |

### Locale Detection

```python
import re

def extract_locale_from_url(url: str) -> str | None:
    """Extract locale code from product URL.

    Returns 'au_en', 'nz_en', etc. for APAC format.
    Returns 'uk', 'us', 'eu', 'row' for EU/US format.
    Returns None if no locale found.
    """
    # APAC format: 5-letter locale (country_language)
    match = re.search(r"/([a-z]{2}_[a-z]{2})/", url.lower())
    if match:
        return match.group(1)

    # EU/US format: 2-letter locale
    match = re.search(r"/([a-z]{2})/", url.lower())
    if match:
        locale = match.group(1)
        if locale in ("uk", "us", "eu", "row"):
            return locale

    return None
```

### Currency Derivation from Locale

```python
LOCALE_CURRENCY_MAP = {
    # APAC — 5-letter locales
    "au_en": "AUD",
    "nz_en": "NZD",
    "sg_en": "SGD",
    "sg_sc": "SGD",
    "hk_en": "HKD",
    "hk_tc": "HKD",
    "my_en": "MYR",
    "my_sc": "MYR",
    "ph_en": "PHP",
    "kr_en": "KRW",
    "th_en": "THB",
    "tw_en": "TWD",
    "tw_tc": "TWD",
    "cn_sc": "CNY",
    "jp_ja": "JPY",
    # EU/US — 2-letter locales
    "uk": "GBP",
    "us": "USD",
    "eu": "EUR",
    "row": "EUR",
}

def get_currency_from_locale(locale: str) -> str:
    """Map locale code to currency code. Returns 'USD' as fallback."""
    return LOCALE_CURRENCY_MAP.get(locale.lower(), "USD")
```

## Price Format Warning: European Number Format

**This is the single biggest gotcha when scraping SFCC sites.** SFCC renders prices in European number format even for non-European currencies:

| Visible Text | Actual Price | Currency |
|--------------|--------------|----------|
| `A$ 2.375,00` | $2,375.00 | AUD |
| `NZ$ 2.675,00` | $2,675.00 | NZD |
| `₱ 77.900,00` | ₱77,900.00 | PHP |
| `S$ 1.590,00` | $1,590.00 | SGD |

The period (`.`) is a thousands separator, and the comma (`,`) is a decimal separator. **This is misleading** — the visible text looks nothing like the actual numeric value.

### The Golden Rule

**NEVER parse visible price text. Always use clean data sources:**

### Source 1: JSON-LD `offers.price` (BEST)

```python
# JSON-LD price is a clean numeric string — no conversion needed
jsonld = extract_jsonld(soup)
price = jsonld["offers"]["price"]  # "2375.00" — already correct
```

### Source 2: HTML `content` attribute (GOOD)

```python
# The content attribute on span.value has the clean numeric value
sales_el = soup.select_one("div.price span.sales span.value")
if sales_el:
    price = sales_el.get("content", "")  # "2375.00" — already correct
```

### Source 3: HTML `content` attribute on standard price (FOR ORIGINAL PRICE)

```python
# Original/was price also uses content attribute
standard_el = soup.select_one("div.price span.standard span.value")
if standard_el:
    original_price = standard_el.get("content", "")  # "2995.00" — already correct
```

### WRONG Approaches (Do NOT do this)

```python
# WRONG: Parsing visible text with European format
text = sales_el.get_text(strip=True)  # "A$ 2.375,00"
# Parsing "2.375,00" as float gives WRONG results

# WRONG: Simple float() on the text
price = float(text.replace("A$", "").strip())  # ValueError or wrong number

# WRONG: Assuming standard number format
price = text.replace(".", "").replace(",", ".")  # Still unreliable
```

## Currency Symbol Mapping

Complete mapping for price formatting:

```python
CURRENCY_SYMBOLS = {
    # Americas & Europe
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "CAD": "C$",
    # Asia-Pacific
    "AUD": "A$",
    "NZD": "NZ$",
    "HKD": "HK$",
    "SGD": "S$",
    "MYR": "RM",
    "KRW": "₩",
    "THB": "฿",
    "TWD": "NT$",
    "PHP": "₱",
    "CNY": "¥",
    "JPY": "¥",
    "INR": "₹",
    "IDR": "Rp",
    "VND": "₫",
}
```

### Price Formatting

```python
def format_price(raw_price: str, currency_code: str) -> str:
    """Format a clean numeric price with currency prefix."""
    if not raw_price:
        return ""
    prefix = CURRENCY_SYMBOLS.get(currency_code, f"{currency_code} ")
    try:
        price_num = float(raw_price)
        return f"{prefix}{price_num:,.2f}"
    except (ValueError, TypeError):
        return f"{prefix}{raw_price}"
```

## JSON-LD Field Mapping

SFCC sites typically embed complete Product schema in `<script type="application/ld+json">` in the `<head>`. This is the **primary data source** — richer and more reliable than HTML scraping.

### JSON-LD Extraction Code

```python
import json
from bs4 import BeautifulSoup

def extract_jsonld(soup: BeautifulSoup) -> dict | None:
    """Find and parse JSON-LD Product structured data from page.

    Handles both dict and list JSON-LD formats.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string or not script.string.strip():
                continue
            data = json.loads(script.string)
            # Direct Product object
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
            # Array of objects (common pattern — Product may be alongside BreadcrumbList)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        return item
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return None
```

### Field Mapping Table

| JSON-LD Path | Output Field | Notes |
|-------------|-------------|-------|
| `name` | `title` | Product name |
| `offers.price` | `price` | Clean numeric string (e.g. `"2375.00"`) |
| `offers.priceCurrency` | `currency` | ISO code (e.g. `"AUD"`, `"NZD"`) |
| `offers.availability` | `availability` | Full schema.org URL — needs mapping |
| `sku` or `mpn` | SKU (in `remarks`) | MPN and SKU are usually identical |
| `image[0]` | image URL | First image from array |
| `image` (array) | all images | Full image gallery URLs |
| `description` | description | Full product description |
| `color` | color (in `remarks`) | Color variant name |
| `category` | category (in `remarks`) | Collection/category name |
| `url` | `url` | Canonical product URL |

### Schema.org Availability Mapping

SFCC uses full schema.org URLs for availability. Map to human-readable strings:

```python
AVAILABILITY_MAP = {
    "InStock": "In Stock",
    "OutOfStock": "Out of Stock",
    "LimitedAvailability": "Low Stock",
    "PreOrder": "Pre-Order",
    "SoldOut": "Out of Stock",
    "OnlineOnly": "Online Only",
    "Discontinued": "Discontinued",
}

def map_availability(schema_url: str) -> str:
    """Map schema.org availability URL to display string.

    'http://schema.org/InStock' -> 'In Stock'
    'https://schema.org/OutOfStock' -> 'Out of Stock'
    """
    if not schema_url:
        return ""
    segment = schema_url.rstrip("/").rsplit("/", 1)[-1]
    return AVAILABILITY_MAP.get(segment, segment)
```

### Fallback Fields

When JSON-LD is missing, fall back to HTML and meta tags:

| Field | JSON-LD | HTML Fallback | Meta Tag Fallback |
|-------|---------|---------------|-------------------|
| title | `name` | `p.product-name` | `meta[property='og:title']` (strip brand suffix) |
| price | `offers.price` | `div.price span.sales span.value[content]` | — |
| image | `image[0]` | `.product-image img[src]` | `meta[property='og:image']` |
| url | `url` | — | `link[rel='canonical'][href]` |
| description | `description` | `div.product-description` | `meta[property='og:description']` |
| SKU | `mpn` / `sku` | `div[data-pid]` | Extract from URL |

## Sale / Original Price Detection

SFCC sites show original (was) prices when a product is on sale. The standard price element appears alongside the sale price element.

### Detection Logic

```python
def extract_original_price(soup: BeautifulSoup, currency_code: str) -> str:
    """Extract original/was price. Only present when product is on sale."""
    standard_el = soup.select_one("div.price span.standard span.value")
    if standard_el:
        content_val = standard_el.get("content", "")
        if content_val:
            return format_price(content_val, currency_code)
    return ""
```

### Alternative Selectors

Different SFCC themes may use different class names:

```python
# Standard SFCC cartridge template
"div.price span.standard span.value"

# Some themes use different class naming
".b-price__standard"
"span.product__original-price"
"span[itemprop='highPrice']"
```

### Sale Detection Logic

Only set `original_price` if it differs from the current sale price:

```python
original = extract_original_price(soup, currency_code)
if original and original != product["price"]:
    product["original_price"] = original
    # Mark as on sale in remarks
```

## Complete Extraction Example

```python
import json
import re
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


def scrape_sfcc_product(url: str) -> dict:
    """Complete extraction example for an SFCC product page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    soup = BeautifulSoup(response.text, "html.parser")
    final_url = response.url

    # Detect locale and currency
    locale_match = re.search(r"/([a-z]{2}_[a-z]{2})/", url.lower())
    locale = locale_match.group(1) if locale_match else None
    currency = LOCALE_CURRENCY_MAP.get(locale, "USD") if locale else "USD"

    product = {
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": currency,
        "url": final_url,
    }

    # Primary: JSON-LD extraction
    jsonld = extract_jsonld(soup)

    if jsonld:
        product["title"] = jsonld.get("name", "")

        offers = jsonld.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        raw_price = offers.get("price", "")
        product["price"] = format_price(raw_price, currency)

        jsonld_currency = offers.get("priceCurrency", "")
        if jsonld_currency:
            product["currency"] = jsonld_currency
            if jsonld_currency != currency:
                product["price"] = format_price(raw_price, jsonld_currency)

        product["availability"] = map_availability(offers.get("availability", ""))
        product["url"] = jsonld.get("url", final_url)

        # SKU, color, category go in remarks
        remarks = []
        sku = jsonld.get("mpn", "") or jsonld.get("sku", "")
        color = jsonld.get("color", "")
        category = jsonld.get("category", "")
        if sku:
            remarks.append(f"SKU: {sku}")
        if color:
            remarks.append(f"Color: {color}")
        if category:
            remarks.append(f"Category: {category}")
        if final_url != url:
            remarks.append(f"Redirected from: {url}")

    # Secondary: Original price from HTML (sale detection)
    standard_el = soup.select_one("div.price span.standard span.value")
    if standard_el:
        content_val = standard_el.get("content", "")
        if content_val and format_price(content_val, currency) != product["price"]:
            product["original_price"] = format_price(content_val, currency)

    return product
```

## Product Discovery

### Sitemap Method

SFCC sites typically expose XML sitemaps at:

```
/{locale}/sitemap_index.xml
/{locale}/sitemap_0-product.xml
/{locale}/sitemap_1-product.xml
```

### Category Pagination

SFCC category pages support URL-based pagination:

```
/{locale}/ladies/shoes/?start=0&sz=24
/{locale}/ladies/shoes/?start=24&sz=24
```

> **Note:** `robots.txt` often blocks `/*start=*` and `/*sz=*` patterns, but the endpoints may still work. Check `robots.txt` before using this method.

## Common Issues

### 1. Brand Field Often Empty

JSON-LD `brand` field is frequently an empty string on SFCC sites. Hardcode the brand name from the site itself:

```python
# WRONG — will get empty string
brand = jsonld.get("brand", {}).get("name", "")

# CORRECT — hardcode from site context
SITE_NAME = "Christian Louboutin"
brand = SITE_NAME
```

### 2. Products May 404

Delisted products may still appear in sitemaps. Handle gracefully:

```python
if status_code == 404:
    logger.warning(f"Product delisted (404): {url}")
    # Record failure and continue
```

### 3. URL Redirects Change SKU

Some URLs redirect to the canonical product page with a different SKU in the URL:

```
Input:  /au_en/kate-black-3615482722394.html
Final:  /au_en/kate-black-3191411BK01.html  (301 redirect)
```

Use `requests.get(allow_redirects=True)` and record both the original and final URL.

### 4. `data-gtm` May Be Null in Server-Rendered HTML

Some SFCC sites populate GTM ecommerce data via JavaScript after page load. If using HTTP requests (not Playwright), `data-gtm` attributes may contain `"null"`. Rely on JSON-LD instead.

### 5. Non-Product URLs in Input

Category and listing pages may end with `/` instead of `.html`. Filter these out:

```python
def is_product_url(url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    return path.endswith(".html")
```

### 6. European Number Format on Visible Prices

See the **Price Format Warning** section above. This is the most common source of data corruption when scraping SFCC sites.

## Rate Limiting

SFCC is a hosted platform. Be respectful to the brand's production instance:

- **Recommended delay:** 1.5–2.0 seconds between requests
- **Max retries:** 3 with exponential backoff
- **Timeout:** 15 seconds per request
- **No concurrent requests** — sequential scraping only

## When NOT to Use

- Site is NOT SFCC (detection failed)
- JSON-LD is missing AND HTML elements are insufficient
- Site has Cloudflare or other anti-bot blocking that requires browser automation
- Product data is loaded via AJAX after page load (rare for SFCC, but possible)

## Known SFCC Sites

| Site | Locale Format | Notes |
|------|-------------|-------|
| apac.christianlouboutin.com | APAC (5-letter) | Informed this skill |
| (add more sites as they are scraped) | | |
