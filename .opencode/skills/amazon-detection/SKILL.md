---
name: amazon-detection
description: Detect Amazon ecommerce sites and leverage their consistent DOM structure, ASIN-based URL system, and mild anti-bot protection for efficient product data extraction across all Amazon TLDs.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
  learned_from: https://www.amazon.in
  learned_date: 2026-06-06
---

# Amazon Detection & Scraping

## What I Do

Detect Amazon-powered ecommerce sites and use their consistent HTML structure to extract product data via simple HTTP requests + BeautifulSoup. Amazon's DOM is identical across all regional TLDs, making selectors universally applicable once learned.

## When to Use Me

Use this when:
- The Site Analyzer agent is analyzing a website
- Platform detection phase is running
- A potential Amazon site has been identified (any amazon.{tld} domain)
- An Amazon ASIN-based product URL is encountered

## 1. Detection Heuristics

### Method 1: URL Patterns (Most Reliable)

```
amazon.{tld}
www.amazon.{tld}
smile.amazon.{tld}
```

Amazon operates over 20 regional storefronts. All share identical DOM structure:

| TLD | Market | Base URL |
|-----|--------|----------|
| `.in` | India | `https://www.amazon.in` |
| `.com` | USA | `https://www.amazon.com` |
| `.co.uk` | UK | `https://www.amazon.co.uk` |
| `.de` | Germany | `https://www.amazon.de` |
| `.co.jp` | Japan | `https://www.amazon.co.jp` |
| `.com.au` | Australia | `https://www.amazon.com.au` |
| `.com.sg` | Singapore | `https://www.amazon.com.sg` |
| `.com.my` | Malaysia | `https://www.amazon.com.my` |
| `.sa` | Saudi Arabia | `https://www.amazon.sa` |
| `.com.be` | Belgium | `https://www.amazon.com.be` |
| `.pl` | Poland | `https://www.amazon.pl` |
| `.fr` | France | `https://www.amazon.fr` |
| `.it` | Italy | `https://www.amazon.it` |
| `.es` | Spain | `https://www.amazon.es` |
| `.ca` | Canada | `https://www.amazon.ca` |
| `.com.mx` | Mexico | `https://www.amazon.com.mx` |
| `.com.br` | Brazil | `https://www.amazon.com.br` |
| `.com.tr` | Turkey | `https://www.amazon.com.tr` |
| `.ae` | UAE | `https://www.amazon.ae` |
| `.nl` | Netherlands | `https://www.amazon.nl` |
| `.se` | Sweden | `https://www.amazon.se` |
| `.eg` | Egypt | `https://www.amazon.eg` |

### Method 2: HTML Markers

Check for these in the HTML source:

```html
<!-- Navigation -->
<div id="navbar">
<div id="nav-belt">

<!-- Page title format -->
<title>Amazon.in : Product Name</title>
<title>Amazon.com: Product Name</title>

<!-- Product area -->
<div id="centerCol">
<span id="productTitle">

<!-- Buy box -->
<div id="buybox_feature_div">
<div id="addToCart_feature_div">
```

### Method 3: Cookie Patterns

Amazon session cookies set on first request:

```
session-id
session-id-time
i18n-prefs
lc-acbin
csm-hit
ubid-acbin          # per-region ubid (e.g., ubid-main for .com)
session-token
ap-fingerprint
```

**Note:** These are standard session cookies, NOT anti-bot cookies (no `__cf_bm`, no `_abck`).

### Method 4: Domain Ownership

All Amazon domains are ultimately operated by `amazon.com`. WHOIS lookup will confirm. There are no third-party Amazon-powered sites (unlike Shopify).

## 2. Anti-Bot: Amazon Native (Low Severity)

Amazon has its **OWN** anti-bot system. It is NOT Cloudflare, NOT Akamai, NOT PerimeterX. Do not apply anti-bot-handling skills meant for those systems.

### Characteristics

- **Mechanism:** IP/behavior-based rate limiting
- **No JavaScript challenge** — no Turnstile, no reCAPTCHA on normal browsing
- **No browser fingerprinting** for standard scraping rates
- **HTTP 503** as the primary block signal (not 403)
- **CAPTCHA page** as secondary block (presents `#captchacharacters` form)

### Verified Behavior (amazon.in)

- 2-3 second delay between requests: **NO CAPTCHA for 50+ consecutive requests**
- 1 second delay: works for small batches but risky for large ones
- Cookie persistence via `requests.Session()` is sufficient
- No special warmup needed beyond the first request setting session cookies

### Detection: Is This Page a Block?

```python
def is_captcha_page(soup, page_text: str) -> bool:
    """Check if the page is a CAPTCHA or bot-detection block page."""
    # Check for CAPTCHA input element
    if soup.select_one("#captchacharacters"):
        return True

    # Check for known block text markers
    markers = [
        "api-services-support@amazon.com",
        "Robot",
        "captcha",
        "unusual activity",
        "Automated requests from this computer are blocked",
    ]
    lower_text = page_text.lower()
    return any(marker.lower() in lower_text for marker in markers)
```

### Page title check:

```python
if "Robot Check" in soup.title.string or "CAPTCHA" in soup.title.string:
    # Blocked
```

### If Blocked

1. **Increase delay** to 3-5 seconds
2. **Rotate User-Agent** strings (standard desktop Chrome variants)
3. **Use proxy rotation** for batches exceeding 100 requests
4. **Wait 5-10 minutes** and retry with fresh session
5. **Do NOT try browser automation** as a bypass — Amazon's protection is IP-based, not JS-based

## 3. Scraping Mechanism

**Level 3: HTTP Requests + BeautifulSoup**

Amazon pages can be scraped with simple HTTP requests. No browser automation (Playwright) needed. This is the fastest and most efficient approach.

### Why Not Playwright?

- No JavaScript rendering required — product data is in the raw HTML
- No JavaScript challenges to solve
- Playwright adds 2-5x overhead per page (page load vs. HTTP fetch)
- Browser fingerprinting does not help bypass Amazon's IP-based protection

### Setup

```python
import requests
from bs4 import BeautifulSoup

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})

# First request sets session cookies automatically
response = session.get("https://www.amazon.in/dp/B0FNK9XK1Q", timeout=15)
soup = BeautifulSoup(response.text, "html.parser")
```

### Rate Limiting

```python
import time
import random

DELAY = 2.0          # base delay in seconds
JITTER = 1.0         # random additional 0-1 second jitter

# Between requests:
time.sleep(DELAY + random.uniform(0, JITTER))
```

| Batch Size | Recommended Delay | Jitter | Expected Duration |
|-----------|------------------|--------|-------------------|
| 1-10 | 1-2s | 0-1s | 10-30s |
| 10-50 | 2-3s | 0-1s | 30s-3min |
| 50-100 | 2-3s | 0-1s | 2-6min |
| 100-500 | 3-5s | 0-1s | 5-42min |
| 500+ | 3-5s | 0-2s + proxy rotation | 25min-2hr |

## 4. URL System & ASIN Extraction

### URL Formats

Amazon uses two canonical product URL formats, both keyed by ASIN:

```
# Format 1: Modern (preferred)
https://www.amazon.in/Brand-Name-Product-Title/dp/B0FNK9XK1Q

# Format 2: Short (redirects to Format 1)
https://www.amazon.in/dp/B0FNK9XK1Q

# Format 3: Legacy
https://www.amazon.in/gp/product/B0FNK9XK1Q
```

### ASIN Format

ASIN = Amazon Standard Identification Number. Always **10 alphanumeric characters**.

```
B0FNK9XK1Q    (current format)
B09ZL9QY3B
B0BN5X78F4
```

### Extraction Regex

```python
import re

ASIN_PATTERN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")

def extract_asin(url: str) -> str:
    match = ASIN_PATTERN.search(url)
    return match.group(1) if match else ""
```

### URL Cleaning

Amazon product URLs accumulate 10+ tracking parameters. Always clean them:

```python
def clean_product_url(url: str, base_url: str) -> str:
    """Strip ALL tracking params, return clean /dp/ASIN URL.

    BEFORE: https://www.amazon.in/Scala-Bleucoin.../dp/B0FNK9XK1Q/
            ref=dp_fod_1?th=1&psc=1&pd_rd_w=abc&pd_rd_wg=xyz
    AFTER:  https://www.amazon.in/dp/B0FNK9XK1Q
    """
    asin = extract_asin(url)
    if not asin:
        return url.split("?")[0]

    if "/gp/product/" in url:
        return f"{base_url}/gp/product/{asin}"
    return f"{base_url}/dp/{asin}"
```

Tracking parameters to strip: `ref`, `th`, `psc`, `pd_rd_w`, `pd_rd_wg`, `pd_rd_r`, `qid`, `sr`, `dib`, `dib_tag`, `smid`, `keywords`, `_encoding`, `creative`, `linkCode`, `tag`, `linkId`, `camp`, `source`.

## 5. CRITICAL: No JSON-LD Available

**Amazon does NOT use schema.org structured data (JSON-LD).** This is a critical deviation from most ecommerce platforms.

Do NOT attempt `script[type="application/ld+json"]` extraction on Amazon pages. The selectors will return empty results or, worse, pick up unrelated structured data fragments embedded in the page.

**All extraction MUST use CSS selectors on the raw HTML.** Amazon hides clean text values inside `.a-offscreen` screen-reader accessibility spans (see Section 6).

This contradicts the general approach of checking for JSON-LD first. When Amazon is detected, skip the JSON-LD step entirely.

## 6. Field Extraction Selectors

These selectors are verified across multiple Amazon TLDs (`.in`, `.sg`, `.sa`, `.pl`, `.co.jp`, `.com.be`). The DOM structure is identical across all storefronts.

### Price Extraction Pattern

Amazon hides clean price text in `.a-offscreen` screen-reader spans. These contain the full formatted value (e.g., `₹975.00`) without split markup. Always target `.a-offscreen` descendants, not the visible text.

### Complete Selector Table

| Field | Primary Selector | Fallback Selector | Notes |
|-------|-----------------|-------------------|-------|
| **Title** | `#productTitle` | — | Strip whitespace. Always present on valid pages. |
| **Price (current)** | `.apex-pricetopay-value .a-offscreen` | `#corePrice_feature_div .a-offscreen` → `#price_inside_content .a-price .a-offscreen` | 3-level fallback chain. Absent when OOS. |
| **MRP / Original** | `.basisPrice .a-offscreen` | `tr td.a-text-strike .a-offscreen` | Only extract when product has a valid price AND this differs from current price. **DO NOT use `.a-text-price .a-offscreen`** — it returns per-unit price, not strikethrough. See Section 9. |
| **Currency** | `.a-price-symbol` | Hardcode per TLD | See Section 8 for per-TLD mapping. |
| **Availability** | `#outOfStock` (element presence) | `#add-to-cart-button` (element presence) | Composite logic — see Section 6.1. |
| **Rating** | `#acrPopover .a-icon-alt` | `.a-icon-star-small .a-icon-alt` | Returns `"4.2 out of 5 stars"`. |
| **Reviews** | `#acrCustomerReviewText` | — | Returns `"(2,202)"`. |
| **Brand** | `table.prodDetTable tr` (label lookup) | — | Labels are **localized per TLD**. See Section 6.3 for per-locale mapping. English fallbacks: "Brand Name", "Brand", "Manufacturer". |
| **Savings** | `.savingsPercentage` | — | Returns `"-43%"`. Absent when no discount. |
| **ASIN** | URL regex | `input[name="ASIN"]` | 10-char alphanumeric from `/dp/` or `/gp/product/` path. |
| **Feature Bullets** | `#feature-bullets .a-unordered-list li span.a-list-item` | — | Returns list of strings. |
| **Images** | Script extraction: `'colorImages'` JSON | `#imageBlock img[src]` | See Section 6.2 for details. |
| **Tax Info** | `#vatMessage_feature_div` | — | e.g., "Inclusive of all taxes". |
| **Best Sellers Rank** | `table.prodDetTable tr` (label: "Best Sellers Rank") | — | Present on most products. |

### 6.1 Availability Detection (Composite Logic)

Do NOT rely on text parsing of the `#availability` div — it contains embedded JSON for in-stock items. Use **element presence checks** instead:

```python
def extract_availability(soup: BeautifulSoup) -> str:
    """Detect product availability using composite element checks.

    Priority:
    1. #outOfStock present           -> "Out of Stock"
    2. #add-to-cart-button present   -> "In Stock"
    3. #availability has "Only X left" -> "Low Stock"
    4. #availability has "unavailable" -> "Out of Stock"
    5. Fallback                       -> "Unknown"
    """
    # 1. Out-of-stock indicator
    if soup.select_one("#outOfStock"):
        return "Out of Stock"

    # 2. Add-to-cart button = in stock
    atc_button = soup.select_one("#add-to-cart-button")
    buy_now = soup.select_one("#buy-now-button")

    # 3. Limited stock text
    avail_div = soup.select_one("#availability")
    if avail_div:
        avail_text = avail_div.get_text()
        if "only" in avail_text.lower() and "left" in avail_text.lower():
            return "Low Stock"
        if "currently unavailable" in avail_text.lower():
            return "Out of Stock"

    if atc_button or buy_now:
        return "In Stock"

    return "Unknown"
```

**Output values:** `"In Stock"`, `"Out of Stock"`, `"Low Stock"`, `"Unknown"`

### 6.2 Image Extraction

```python
import re
import json

def extract_images(soup: BeautifulSoup) -> list[str]:
    """Extract product images from embedded JavaScript."""
    # Method 1: Parse colorImages JSON from script tags
    scripts = soup.find_all("script")
    for script in scripts:
        text = script.string or ""
        match = re.search(r"'colorImages':\s*(\[[\s\S]*?\])", text)
        if match:
            try:
                images = json.loads(match.group(1).replace("'", '"'))
                return [img.get("hiRes") or img.get("large") for img in images if img.get("hiRes") or img.get("large")]
            except (json.JSONDecodeError, KeyError):
                pass

    # Method 2: Fallback to image block
    return [img.get("src") for img in soup.select("#imageBlock img") if img.get("src")]
```

### 6.3 Brand Extraction

Brand labels in the product details table (`table.prodDetTable`) are **localized per TLD**. English-only labels will fail on non-English storefronts.

#### Localized Brand Label Mapping

| TLD | Locale | "Brand" Label | "Manufacturer" Label | Verified |
|-----|--------|---------------|---------------------|----------|
| `.in` | English | Brand | Manufacturer | ✅ |
| `.com` | English | Brand | Manufacturer | ✅ |
| `.co.uk` | English | Brand | Manufacturer | ✅ |
| `.pl` | Polish | **Marka** | **Producent** | ✅ |
| `.de` | German | **Marke** | **Hersteller** | ⚠️ (expected, not verified) |
| `.fr` | French | **Marque** | **Fabricant** | ⚠️ (expected, not verified) |
| `.es` | Spanish | **Marca** | **Fabricante** | ⚠️ (expected, not verified) |
| `.it` | Italian | **Marca** | **Produttore** | ⚠️ (expected, not verified) |
| `.co.jp` | Japanese | **ブランド** | **製造元** | ⚠️ (expected, not verified) |
| `.sa` | Arabic | **العلامة التجارية** | **الشركة المصنعة** | ⚠️ (expected, not verified) |
| `.com.br` | Portuguese | **Marca** | **Fabricante** | ⚠️ (expected, not verified) |
| `.com.mx` | Spanish | **Marca** | **Fabricante** | ⚠️ (expected, not verified) |
| `.com.tr` | Turkish | **Marka** | **Üretici** | ⚠️ (expected, not verified) |
| `.ae` | Arabic | **العلامة التجارية** | **الشركة المصنعة** | ⚠️ (expected, not verified) |
| `.nl` | Dutch | **Merk** | **Fabrikant** | ⚠️ (expected, not verified) |
| `.se` | Swedish | **Varumärke** | **Tillverkare** | ⚠️ (expected, not verified) |
| `.eg` | Arabic | **العلامة التجارية** | **الشركة المصنعة** | ⚠️ (expected, not verified) |
| `.com.au` | English | Brand | Manufacturer | ✅ |
| `.com.sg` | English | Brand | Manufacturer | ✅ |
| `.com.my` | Malay | **Jenama** | **Pengilang** | ⚠️ (expected, not verified) |
| `.com.be` | Multiple | **Marque** / **Merk** | **Fabricant** / **Fabrikant** | ⚠️ (expected, not verified) |
| `.ca` | English/French | Brand / **Marque** | Manufacturer / **Fabricant** | ⚠️ (expected, not verified) |

#### Extraction Code (Locale-Aware)

```python
# Per-TLD brand label mapping (verified entries marked, others expected)
TLD_BRAND_LABELS = {
    "pl": ("Marka", "Producent"),       # ✅ Verified on amazon.pl
    "de": ("Marke", "Hersteller"),      # Expected
    "fr": ("Marque", "Fabricant"),       # Expected
    "es": ("Marca", "Fabricante"),       # Expected
    "it": ("Marca", "Produttore"),       # Expected
    "co.jp": ("ブランド", "製造元"),      # Expected
    "sa": ("العلامة التجارية", "الشركة المصنعة"),  # Expected
    "com.br": ("Marca", "Fabricante"),    # Expected
    "com.mx": ("Marca", "Fabricante"),   # Expected
    "com.tr": ("Marka", "Üretici"),       # Expected
    "ae": ("العلامة التجارية", "الشركة المصنعة"),  # Expected
    "nl": ("Merk", "Fabrikant"),         # Expected
    "se": ("Varumärke", "Tillverkare"),   # Expected
    "eg": ("العلامة التجارية", "الشركة المصنعة"),  # Expected
    "com.my": ("Jenama", "Pengilang"),    # Expected
    "com.be": ("Marque", "Fabricant"),    # Expected (French-speaking)
    "ca": ("Marque", "Fabricant"),        # Expected (French-speaking)
}

def get_brand_labels(tld: str) -> tuple[str, str]:
    """Return (brand_label, manufacturer_label) for a given TLD."""
    return TLD_BRAND_LABELS.get(tld, ("Brand", "Manufacturer"))

def extract_brand(soup: BeautifulSoup, tld: str = "com") -> str:
    """Extract brand from product details table with locale-aware labels.

    Brand and Manufacturer labels are localized per TLD.
    Falls back to English labels if locale labels not found.
    """
    table = soup.select_one("table.prodDetTable")
    if not table:
        return ""

    rows = table.select("tr")
    brand_label, mfg_label = get_brand_labels(tld)

    # Priority: locale Brand > "Brand Name" > English "Brand" > locale Manufacturer > English "Manufacturer"
    label_priority = [brand_label, "Brand Name", "Brand", mfg_label, "Manufacturer"]

    for label in label_priority:
        for row in rows:
            th = row.select_one("th")
            td = row.select_one("td")
            if th and td:
                th_text = th.get_text(strip=True)
                # Case-insensitive match for English labels, exact for localized
                if label == th_text or th_text.lower() == label.lower():
                    value = td.get_text(strip=True)
                    # Manufacturer/Producent may include address — take first part
                    if label in (mfg_label, "Manufacturer") and "," in value:
                        value = value.split(",")[0].strip()
                    return value

    return ""
```

**Learned from:** amazon.pl scrape (2026-06-06). On amazon.pl, 100% of sampled products used "Marka" as the brand label. English "Brand" label was never present.

## 7. CRITICAL: Out-of-Stock Price Guard

When a product is out of stock, Amazon **removes ALL price elements** from the page. The `#price_inside_content`, `.apex-pricetopay-value`, and `.basisPrice` sections are all absent.

**DANGER:** Broad fallback price selectors (e.g., `.a-offscreen` without a specific parent) will grab prices from **sponsored products**, **similar product listings**, or **other products on the page**. This produces incorrect data.

**FIX:** Only extract `original_price` when the main price was successfully found:

```python
# --- Price (current/sale price) ---
price_text = ""
price_el = soup.select_one(".apex-pricetopay-value .a-offscreen")
if price_el:
    price_text = price_el.get_text(strip=True)

if not price_text:
    price_el = soup.select_one("#corePrice_feature_div .a-offscreen")
    if price_el:
        price_text = price_el.get_text(strip=True)

if not price_text:
    price_el = soup.select_one("#price_inside_content .a-price .a-offscreen")
    if price_el:
        price_text = price_el.get_text(strip=True)

product["price"] = price_text

# --- Original Price (MRP) ---
# Guard: if main price is empty (OOS), skip original_price entirely
if product["price"]:
    orig_el = soup.select_one(".basisPrice .a-offscreen")
    if orig_el:
        orig_text = orig_el.get_text(strip=True)
        # Only record if different from current price
        if orig_text and orig_text != product["price"]:
            product["original_price"] = orig_text
```

**Impact of OOS on page elements:**

| Element | In Stock | Out of Stock |
|---------|----------|--------------|
| `#productTitle` | Present | Present |
| `.apex-pricetopay-value .a-offscreen` | Present | **Absent** |
| `.basisPrice .a-offscreen` | Present | **Absent** |
| `.savingsPercentage` | Present (if discount) | **Absent** |
| `#outOfStock` | Absent | **Present** |
| `#add-to-cart-button` | Present | **Absent** |
| `#buy-now-button` | Present | **Absent** |
| `#acrPopover .a-icon-alt` (rating) | Present | Present |
| `#acrCustomerReviewText` | Present | Present |
| `table.prodDetTable` | Present | Present |
| Feature bullets | Present | Present |

## 8. Per-TLD Currency Mapping

| TLD | Currency Code | Symbol | Locale |
|-----|--------------|--------|--------|
| `.in` | INR | `₹` | India |
| `.com` | USD | `$` | USA |
| `.co.uk` | GBP | `£` | UK |
| `.de` | EUR | `€` | Germany |
| `.fr` | EUR | `€` | France |
| `.it` | EUR | `€` | Italy |
| `.es` | EUR | `€` | Spain |
| `.co.jp` | JPY | `¥` | Japan |
| `.com.au` | AUD | `A$` | Australia |
| `.com.sg` | SGD | `S$` | Singapore |
| `.com.my` | MYR | `RM` | Malaysia |
| `.sa` | SAR | `ر.س` | Saudi Arabia |
| `.com.be` | EUR | `€` | Belgium |
| `.pl` | PLN | `zł` | Poland |
| `.ca` | CAD | `C$` | Canada |
| `.com.mx` | MXN | `$` | Mexico |
| `.com.br` | BRL | `R$` | Brazil |
| `.com.tr` | TRY | `₺` | Turkey |
| `.ae` | AED | د.إ | UAE |
| `.nl` | EUR | `€` | Netherlands |
| `.se` | SEK | `kr` | Sweden |
| `.eg` | EGP | ج.م | Egypt |

**Detection:** Extract from `.a-price-symbol` element, or hardcode based on TLD when generating the scraper.

```python
TLD_CURRENCY_MAP = {
    "in": ("INR", "₹"),
    "com": ("USD", "$"),
    "co.uk": ("GBP", "£"),
    "de": ("EUR", "€"),
    "fr": ("EUR", "€"),
    "it": ("EUR", "€"),
    "es": ("EUR", "€"),
    "co.jp": ("JPY", "¥"),
    "com.au": ("AUD", "A$"),
    "com.sg": ("SGD", "S$"),
    "com.my": ("MYR", "RM"),
    "sa": ("SAR", "ر.س"),
    "com.be": ("EUR", "€"),
    "pl": ("PLN", "zł"),
    "ca": ("CAD", "C$"),
    "com.mx": ("MXN", "$"),
    "com.br": ("BRL", "R$"),
    "com.tr": ("TRY", "₺"),
    "ae": ("AED", "د.إ"),
    "nl": ("EUR", "€"),
    "se": ("SEK", "kr"),
    "eg": ("EGP", "ج.م"),
}

def get_currency_from_tld(domain: str) -> tuple[str, str]:
    """Return (currency_code, symbol) for an Amazon domain."""
    # Extract TLD from domain (e.g., "amazon.co.uk" -> "co.uk")
    parts = domain.replace("www.", "").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com"):
        tld = f"{parts[-2]}.{parts[-1]}"
    else:
        tld = parts[-1]

    return TLD_CURRENCY_MAP.get(tld, ("USD", "$"))
```

## 9. Common Issues

### Indian Number Formatting

India uses the lakh/crore system. Numbers are grouped differently:

```
₹1,29,999.00   (1 lakh 29 thousand)
₹12,34,567.00  (12 lakh 34 thousand)
₹1,00,000.00   (1 lakh)
```

Standard Western grouping: `₹129,999.00` would be `1,29,999` in Indian format. When parsing numbers for comparison, handle both formats:

```python
def parse_indian_price(price_str: str) -> float:
    """Parse Indian-formatted price string to float.
    Handles: ₹1,29,999.00 -> 129999.0
    """
    cleaned = re.sub(r"[^\d.]", "", price_str)
    return float(cleaned) if cleaned else 0.0
```

### Brand Labels Are Localized (NOT English-Only)

**Updated from amazon.pl scrape (2026-06-06).**

The product details table (`table.prodDetTable`) uses **localized labels** that vary by TLD. On amazon.pl, the brand label is "Marka" (Polish), NOT "Brand". Using English-only labels will silently fail on non-English storefronts.

| TLD | "Brand" Label | "Manufacturer" Label |
|-----|--------------|----------------------|
| `.in`, `.com`, `.co.uk`, `.com.au`, `.com.sg` | Brand | Manufacturer |
| `.pl` | **Marka** | **Producent** |
| `.de` | **Marke** (expected) | **Hersteller** (expected) |
| `.fr` | **Marque** (expected) | **Fabricant** (expected) |

**Solution:** Use the locale-aware `extract_brand()` function in Section 6.3 which looks up localized labels per TLD.

### `.a-text-price .a-offscreen` Returns Per-Unit Price (NOT Strikethrough)

**Updated from amazon.pl scrape (2026-06-06).**

The selector `.a-text-price .a-offscreen` is listed in some older references as a fallback for MRP/original price. However, on amazon.pl (and likely other TLDs), this selector returns the **per-unit price** from `apex-priceperunit-value` (e.g., `"3,45zł/100 ml"`), NOT the strikethrough/MRP price.

Using this selector as a fallback will produce **incorrect original_price data** — the per-unit price instead of the actual before-discount price.

**Solution:** Only use `.basisPrice .a-offscreen` and `tr td.a-text-strike .a-offscreen` for MRP extraction. Do NOT include `.a-text-price .a-offscreen` in the fallback chain.

### Availability Div Contains JSON

For in-stock products, the `#availability` div text contains `"In stock"` followed by a JSON object with merchant data. Do NOT parse this div's text directly — use element presence checks instead (see Section 6.1).

### 404 Products Still Return HTTP 200

Removed/deleted products do not return HTTP 404. They return HTTP 200 with a page containing `"page not found"` or `"looks like this link is broken"` text, and no `#productTitle` element. Detect by checking for `#productTitle` presence.

### URL Cleaning Required

Amazon URLs accumulate many tracking parameters. Always clean URLs before storing:

```
# Messy URL (18 params):
https://www.amazon.in/Scala-Bleucoin-Self-Adhesive-Backsplash-Water-Resistant/
dp/B0FNK9XK1Q/ref=dp_fod_1?th=1&psc=1&pd_rd_w=abc&pd_rd_wg=xyz
&pd_rd_r=def&qid=sr_1&sr=8-1&dib=eyJ&dib_tag=se&smid=A...

# Clean URL:
https://www.amazon.in/dp/B0FNK9XK1Q
```

### CAPTCHA Markers

When Amazon blocks, the page shows a CAPTCHA form. Detect with:

```python
CAPTCHA_MARKERS = [
    "api-services-support@amazon.com",
    "Robot",
    "captcha",
    "unusual activity",
    "Automated requests from this computer are blocked",
]
# Or check: soup.select_one("#captchacharacters")
# Or check page title for "Robot Check"
```

### HTTP Status Code Handling

| Status | Meaning | Action |
|--------|---------|--------|
| 200 | OK (may still be block page) | Parse page, check for CAPTCHA markers |
| 404 | Not found | Skip (rare — Amazon usually returns 200 for missing products) |
| 503 | Service Unavailable | Possible bot detection — retry with backoff |
| 429 | Too Many Requests | Slow down significantly, add 5-10s delay |

## 10. Known Amazon Sites

### Already in Scrapers

- `amazon.in` — India
- `amazon.sg` — Singapore
- `amazon.sa` — Saudi Arabia
- `amazon.pl` — Poland
- `amazon.co.jp` — Japan
- `amazon.com.be` — Belgium

### Common TLDs to Expect

All selectors in this skill apply to any Amazon TLD. When a new Amazon site is encountered:

1. Detect TLD → set currency from Section 8 mapping
2. Update `SITE_URL` and `SITE_NAME` in scraper config
3. Use identical CSS selectors — DOM is universal
4. Adjust `Accept-Language` header for the locale (e.g., `ja-JP` for `.co.jp`)

### Quick Generator for New Amazon TLD

When generating a scraper for a new Amazon TLD, the template is:

```python
SITE_NAME = "Amazon {Country}"
SITE_URL = "https://www.amazon.{tld}"
PLATFORM = "amazon_custom"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "amazon_{tld_no_dots}"  # e.g., "amazon_co_uk"

# Currency — hardcode from Section 8 mapping
CURRENCY = "USD"  # change per TLD

# All selectors remain the same across TLDs
```

## 11. Extraction Priority Order

When building an Amazon scraper, extract fields in this order to handle OOS edge cases correctly:

```
1. ASIN          — from URL regex (always available)
2. Title         — from #productTitle (always on valid pages)
3. Availability  — check BEFORE price (OOS has no price)
4. Price         — from .apex-pricetopay-value .a-offscreen (3-level fallback)
5. Original Price — ONLY if step 4 returned a value (OOS guard)
6. Savings       — from .savingsPercentage
7. Rating        — from #acrPopover .a-icon-alt
8. Reviews       — from #acrCustomerReviewText
9. Brand         — from table.prodDetTable (label lookup)
10. Feature Bullets — from #feature-bullets
```

## 12. Error Handling Patterns

```python
# If #productTitle missing — page may be blocked or invalid
if not product["title"]:
    product["remarks"] = f"ASIN={asin} | No title found — page may be invalid or blocked"
    return product

# HTTP 503 — possible bot detection, retry with backoff
if status_code == 503:
    delay = base_delay * (attempt + 1) + random.uniform(0, jitter)
    time.sleep(delay)
    continue

# HTTP 404 — product removed
if status_code == 404:
    logger.warning(f"404 Not Found: {url}")
    return None
```

## When NOT to Use

- Site is NOT Amazon (detection failed)
- Product pages require login or are behind a paywall
- Need real-time stock updates faster than 2-3s per request
- Scraping Amazon search/listing pages (not covered by this skill — only product pages)
- Need variant-level data beyond the default selected variant
