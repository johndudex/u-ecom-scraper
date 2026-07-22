---
name: oracle-commerce-cloud-detection
description: Detect Oracle Commerce Cloud (OCC / formerly ATG) ecommerce sites and leverage their Knockout.js client-side rendering, JSON-LD structured data, and ccstore image service for efficient product data extraction.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
  learned_from: https://www.dollartree.com
  learned_date: 2026-07-20
---

# Oracle Commerce Cloud (OCC) Detection & Scraping

## What I Do

Detect Oracle Commerce Cloud (formerly ATG/Oracle Commerce) ecommerce sites and use their Knockout.js client-side rendering, JSON-LD structured data, and meta tags for efficient product data extraction. OCC is used by major retail brands including Dollar Tree, and other large retailers.

## When to Use Me

Use this when:
- The Site Analyzer agent detects Knockout.js data-binding attributes
- Page source contains `cc-ko-oj-extensions.js`, RequireJS modules, or OCC tracking
- Image URLs use `/ccstore/v1/images/` paths
- Tracking pixels from `occa.ocs.oraclecloud.com` are present
- A site is confirmed or suspected as Oracle Commerce Cloud

## Detection Methods

### Method 1: Knockout.js Data-Binding Attributes

OCC templates use Knockout.js extensively. Look for `data-bind` attributes throughout the HTML:

```html
<div data-bind="visible: true">
<span data-bind="text: product.displayName">
```

### Method 2: OCC-Specific JavaScript Files

```html
<script src="/ccstore/v1/js/cc-ko-oj-extensions.js"></script>
<script src="/ccstore/v1/js/require.js"></script>
```

Key files to detect:
- `cc-ko-oj-extensions.js` — OCC's Knockout.js Oracle JET extensions
- `require.js` / RequireJS module loading
- Any JS file under `/ccstore/v1/` paths

### Method 3: OCC Tracking Pixel

```html
<img src="https://occa.ocs.oraclecloud.com/..." />
```

The `occa.ocs.oraclecloud.com` domain is the Oracle Cloud Analytics tracking endpoint used by OCC sites.

### Method 4: Image Service URL Pattern

OCC uses a centralized image service:

```
/ccstore/v1/images/?source=/file/{hash}/products/{sku}.jpg&height=300&width=300
```

### Method 5: Product URL Pattern

OCC product URLs follow the pattern:

```
/{descriptive-slug}/{numeric-id}
```

Example: `/vibrant-faux-sunflower-1-ct/400732`

Category URLs: `/department/{slug}` or `/{parent}/{child}` patterns.

## Scraping Mechanism

### Recommended: HTTP Requests (with caveats)

OCC sites render product data client-side via Knockout.js, BUT they also embed JSON-LD structured data and meta tags in the server-rendered HTML shell. This means:

- **JSON-LD, meta tags, and some HTML elements** are available via direct HTTP
- **Full Knockout.js-rendered DOM** requires Playwright for complete CSS-based extraction
- **Best approach:** Use HTTP requests when JSON-LD + meta tags provide sufficient data (title, price, sku, images, availability, category)

### When to Use Playwright Instead

- If JSON-LD is missing or incomplete
- If you need CSS-extracted fields (rating, review count) that require full JS rendering
- If the site has anti-bot protection (rare for OCC)

## CRITICAL: JSON-LD Price Trap — Case Price vs Unit Price

**This is the most important gotcha for OCC sites.**

On Oracle Commerce Cloud sites that sell multi-unit cases, the JSON-LD `offers.price` is the **CASE TOTAL** price (e.g., $42.00 for 24 units), NOT the per-unit price (e.g., $1.75).

### Detection

Check for `offers.priceSpecification.referenceQuantity`:

```json
{
  "offers": {
    "price": 42,
    "priceSpecification": {
      "price": 42,
      "priceCurrency": "USD",
      "referenceQuantity": {
        "Value": "24",
        "unitText": "C62"
      }
    }
  }
}
```

If `referenceQuantity.Value` > 1 and `unitText` is `"C62"` (Oracle's internal case unit code), the price is a case total.

### Correct Extraction: Use Meta Tags for Price

The **unit price** is available in:

```html
<meta property="product:price:amount" content="1.75">
<meta property="product:price:currency" content="USD">
```

**Guard: Always use `meta[property='product:price:amount']` for the per-unit price on OCC sites. Never trust `JSON-LD offers.price` without checking priceSpecification.**

```python
def extract_occ_price(soup, jsonld):
    """Extract the correct UNIT price from an OCC product page."""
    # Primary: meta tag (always the per-unit price)
    meta_price = soup.find("meta", attrs={"property": "product:price:amount"})
    if meta_price and meta_price.get("content"):
        return meta_price["content"]

    # Fallback: JSON-LD with case-price guard
    if jsonld:
        offers = jsonld.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        # Check if this is a case price
        price_spec = offers.get("priceSpecification", {})
        ref_qty = price_spec.get("referenceQuantity", {})
        if ref_qty.get("Value") and int(ref_qty["Value"]) > 1:
            # This is a case price — DO NOT use as unit price
            return None  # or calculate: offers["price"] / int(ref_qty["Value"])

        return str(offers.get("price", ""))

    return None
```

### When JSON-LD Price IS Correct

If `priceSpecification.referenceQuantity` is absent or `Value` is `"1"`, then `offers.price` is the actual unit price and can be used safely.

## Field Extraction Guide

| Field | Primary Source | Fallback | Notes |
|-------|---------------|----------|-------|
| Title | `h1.product-name` (CSS) | JSON-LD `name` | CSS available in server HTML |
| Price | `meta[property='product:price:amount']` | CSS `.list-sale-price` | **NOT** JSON-LD `offers.price` (see trap above) |
| Currency | `meta[property='product:price:currency']` | JSON-LD `offers.priceCurrency` | |
| Availability | JSON-LD `offers.availability` | — | Strip `http://schema.org/` prefix |
| SKU | JSON-LD `sku` | URL path extraction | Extract numeric ID from URL slug |
| Category | JSON-LD `category` (array) | — | Join array with ` > ` |
| Brand | JSON-LD `brand.name` | — | Often null/empty on OCC sites |
| Description | JSON-LD `description` | — | May be short/internal abbreviations |
| Image | JSON-LD `image` | `img.ccz-small` | Via ccstore image service |
| Rating | CSS `[class*='bv-rating'] button[aria-label]` | — | BazaarVoice integration; regex: `(\d+(?:\.\d+)?) out of 5` |
| Review Count | CSS `[class*='bv-rating']` text | — | BazaarVoice; regex: `\((\d+)\)` |

## og: Meta Tag Warning

OCC sites may have **untranslated i18n resource bundle keys** in `og:title` and `og:description` meta tags:

```html
<meta property="og:title" content="ns.productsocialmetatags:resources.openGraphTitle">
<meta property="og:description" content="ns.productsocialmetatags:resources.openGraphDescription">
```

**Guard:** Check if `og:title` matches the pattern `ns\.\w+:resources\.\w+` (resource bundle key). If so, do NOT use it for title extraction — fall back to JSON-LD `name` or CSS `h1`.

`og:image` and `og:url` are typically correct and safe to use.

## Rate Limiting

- **Recommended delay:** 2.0 seconds between requests
- OCC backend may have session-based throttling
- No aggressive anti-bot on most OCC sites (reCAPTCHA may exist for checkout only)

## Known OCC Sites

| Site | Notes |
|------|-------|
| dollartree.com | Knockout.js rendering, multi-unit case pricing, BazaarVoice reviews |
| (add more as they are scraped) | |
