---
name: localised-detection
description: Detect Localised Inc. ecommerce sites and leverage their public Product REST API and Algolia search integration for fast, structured product data extraction.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
  learned_from: https://americaneagle.com.au
  learned_date: 2026-06-06
---

# Localised Inc. Detection & API Usage

## What I Do

Detect ecommerce sites powered by the Localised Inc. platform and use their public Product REST API to extract product data without browser automation. Localised powers multi-regional storefronts for brands like American Eagle Outfitters.

## When to Use Me

Use this when:
- The Site Analyzer agent is analyzing a website
- Platform detection phase is running
- A potential Localised Inc. site has been identified
- Images are served from `i.localised.com`
- Footer contains "Localised Hong Kong Limited"

## Detection Methods

### Method 1: CDN & Image Markers

Check for these in the HTML source:

```html
<!-- Image CDN references -->
https://i.localised.com/img/

<!-- Asset references -->
/themes/config.json
/themes/templates/pdp_en.json
```

### Method 2: URL Patterns

Localised Inc. sites use these URL structures:
- `/{locale}/product/{slug}/{productCode}` - Product pages
- `/{locale}/{brand}/men|women/{category}` - Category pages
- Product codes: `{classId}_{styleId}_{colorId}` (e.g., `0743_3810_021`)

### Method 3: Page Source Markers

Check for these indicators in the HTML:

```html
<!-- Powered by text in footer -->
"Localised Hong Kong Limited"

<!-- Algolia integration for search -->
AlgoliaSearch
algolia appId / application_id in page source

<!-- Internal API paths referenced in JS -->
/api/product/s/
/api/basic-session
```

### Method 4: JSON Data Files

Localised sites expose configuration files:

```bash
curl -s "https://www.example.com/themes/config.json"
curl -s "https://www.example.com/data/navTree_en.json"
curl -s "https://www.example.com/data/clp-routes.json"
```

## API Endpoints

### Product API (Public, No Auth Required)

The primary method for extracting product data:

```bash
# Single product by product code
curl -s "https://www.example.com/api/product/s/{productCode}?lang=en&siteTag={SITE_TAG}"

# Example
curl -s "https://americaneagle.com.au/api/product/s/0743_3810_021?lang=en&siteTag=AE_AU"
```

**Authentication:** The browser sends `Authorization: Basic dGVzdDp0ZXN0` (decoded: `test:test`), but the API works without it. Include for safety.

**Response structure:**
```json
{
  "id": "0743_3810_021",
  "slug": "aerie-oh-snap-sweatshirt",
  "name": "Aerie Oh Snap! Sweatshirt",
  "description": "<div>...</div>",
  "brand": {
    "tag": "aerie",
    "name": "Aerie"
  },
  "sale": "ONSALE",
  "gender": "female",
  "priceMin": 65.95,
  "priceMax": 65.95,
  "priceRange": {
    "listMin": 87.95,
    "listMax": 87.95,
    "saleMin": 65.95,
    "saleMax": 65.95
  },
  "optionLevels": ["color", "size"],
  "categories": ["apparel/womens/..."],
  "categoryPageIds": ["AE > Women > Tops > Sweaters cardigans > Sweaters"],
  "options": [
    {
      "slug": "stone-harbor",
      "color": { "name": "Stone Harbor", "id": "021", "swatch": "..." },
      "availability": "AVAILABLE",
      "sale": "ONSALE",
      "media": {
        "large": ["https://i.localised.com/img/a3/product/{uuid}_LARGE.jpg"],
        "standard": ["https://i.localised.com/img/a3/product/{uuid}.jpg"],
        "thumb": ["https://i.localised.com/img/a3/product/{uuid}_THUMB.jpg"]
      },
      "options": [
        {
          "slug": "xxs",
          "size": { "name": "XXS", "tag": ["f-vanity-xxs"] },
          "availability": "AVAILABLE",
          "sku": "ae_0043762764",
          "merchantSku": "0348-1673-600",
          "stockQty": 30,
          "price": {
            "sale": { "total": 65.95, "tax": 0.00, "duty": 0.00 },
            "list": { "total": 87.95, "tax": 0.00, "duty": 0.00 }
          },
          "sale": "ONSALE"
        }
      ]
    }
  ],
  "shopTheSet": ["0437_5324_437"],
  "collections": [{ "tag": "department-womens-sweaters" }]
}
```

### Algolia Search API (Discovery Only)

Used for product discovery and search — NOT for product detail data.

```bash
# Search/query products
curl -s -X POST "https://{APP_ID}-{1,2,3}.algolianet.com/1/indexes/prd-product-{SITE_TAG}/query" \
  -H "Content-Type: application/json" \
  -d '{
    "params": "hitsPerPage=120&offset=0"
  }'
```

**Key facts:**
- App ID and API key are discoverable in page source JavaScript
- Index naming: `prd-product-{SITE_TAG}` (e.g., `prd-product-AE-AU`)
- Max 1000 results per query (`hitsPerPage=1000`)
- Use for product discovery; use the Product API for product details
- Facets available: `brand.tag`, `categories.tag`, `collections.tag`, `color.tag`, `size.tag`, `price.{CURRENCY}-{LOCALE}.sale`

### Other API Endpoints

```bash
# Shopping session (optional)
POST /api/basic-session?siteTag={SITE_TAG}

# Cart
GET /api/cart?lang=en&siteTag={SITE_TAG}

# Promotions
GET /api/cart/promotions?siteTag={SITE_TAG}
```

## Field Mapping: Localised API → Output

| Localised API Field | Output Field | Notes |
|---------------------|-------------|-------|
| `name` | `title` | Direct |
| `priceMin` | `price` | Minimum sale price across all variants |
| `priceRange.listMin` | `original_price` | Only when `sale == "ONSALE"` and `listMin > saleMin` |
| `priceRange.saleMin` | `sale_price` | Alternative to `priceMin` |
| `sale` | — | `"ONSALE"` or `"NOSALE"` — use to determine if discount exists |
| `options[].options[].availability` | `availability` | See Availability Aggregation below |
| `options[].options[].stockQty` | `stock_quantity` | Per-variant stock count |
| `brand.name` | `brand` | e.g., "AE", "Aerie" |
| `brand.tag` | — | Brand code: "ae", "aerie", "offline" |
| `categories[]` | `category` | Machine-readable path |
| `categoryPageIds[]` | `category_breadcrumb` | Human-readable breadcrumb |
| `gender` | `gender` | "male" or "female" |
| `slug` + `id` | `url` | Construct: `{base_url}/{locale}/product/{slug}/{id}` |
| `description` | `description` | HTML string — parse to extract sections |
| `options[].media.large[]` | `images` | Large image URLs |
| `options[].color.name` | — | Color display name |
| `options[].color.swatch` | — | Color swatch image URL |
| `options[].options[].sku` | `sku` | Per-variant SKU |
| `shopTheSet` | `related_products` | Array of related product codes |
| `collections[].tag` | `tags` | Facet/collection tags |

## Product Code Extraction

Product codes are 3-part underscore-separated identifiers:

```
Format: {classId}_{styleId}_{colorId}
Example: 0743_3810_021
```

**Extraction regex:**
```python
import re
PRODUCT_CODE_REGEX = re.compile(r"/product/[^/]+/(\d+_\d+_\d+)")
```

**Usage:**
```python
url = "https://americaneagle.com.au/en-au/product/aerie-oh-snap-sweatshirt/0743_3810_021?color=stone-harbor"
match = PRODUCT_CODE_REGEX.search(url)
product_code = match.group(1)  # "0743_3810_021"
```

**Deduplication:**
- Full product code (`classId_styleId_colorId`) = one record per color variant
- First two segments (`classId_styleId`) = one record per product (all colors)

## NOT_FOUND Handling

Missing or removed products return a **valid HTTP 200** response with this body:

```json
{"result": "NOT_FOUND"}
```

**You MUST check the response body, not just the HTTP status code.**

```python
data = response.json()
if isinstance(data, dict) and data.get("result") == "NOT_FOUND":
    # Skip this product
    continue
```

## Availability Aggregation

Check ALL size variants across ALL color variants:

| Condition | Result |
|-----------|--------|
| ANY variant has `availability == "AVAILABLE"` | `"In Stock"` |
| NO `AVAILABLE`, but some have `"LOWSTOCK"` | `"Low Stock"` |
| ALL variants have `"OUTOFSTOCK"` | `"Out of Stock"` |

**Stock quantity thresholds:**
| API Value | Meaning | Typical stockQty |
|-----------|---------|-----------------|
| `AVAILABLE` | In stock | > 2 units |
| `LOWSTOCK` | Low stock | 1–2 units |
| `OUTOFSTOCK` | Out of stock | 0 units |

## siteTag Locale Mapping

The `siteTag` parameter determines the market, locale, and currency:

| siteTag | Locale | Country | Currency | Domain Pattern |
|---------|--------|---------|----------|---------------|
| `AE_AU` | en-au | Australia | AUD | americaneagle.com.au |
| `AE_JP` | en-jp | Japan | JPY | americaneagle.co.jp |
| `AE_HK` | en-hk | Hong Kong | HKD | americaneagle.com.hk |
| `AE_TW` | en-tw | Taiwan | TWD | americaneagle.com.tw |
| `AE_SG` | en-sg | Singapore | SGD | americaneagle.com.sg |
| `AE_KR` | en-kr | Korea | KRW | americaneagle.co.kr |
| `AE_MY` | en-my | Malaysia | MYR | americaneagle.com.my |

**Price format varies by locale:**
- AUD: `A${price} (GST incl.)`
- Other locales: use local currency symbol

## Known Localised Inc. Sites

| Domain | siteTag | Brand | Status |
|--------|---------|-------|--------|
| americaneagle.com.au | AE_AU | American Eagle / Aerie | Verified |
| americaneagle.co.jp | AE_JP | American Eagle / Aerie | Verified |
| americaneagle.com.hk | AE_HK | American Eagle / Aerie | Verified |
| americaneagle.com.tw | AE_TW | American Eagle / Aerie | Verified |
| americaneagle.com.sg | AE_SG | American Eagle / Aerie | Verified |
| americaneagle.co.kr | AE_KR | American Eagle / Aerie | Verified |
| americaneagle.com.my | AE_MY | American Eagle / Aerie | Verified |

**Note:** Localised Inc. powers other brands beyond American Eagle. Detection should look for `i.localised.com` CDN and "Localised Hong Kong Limited" in footer, not just AE-specific domains.

## Scraping Recommendations

### Recommended Mechanism: Level 2 (Internal API)

| Aspect | Recommendation |
|--------|---------------|
| **Method** | Internal Product REST API (`/api/product/s/{code}`) |
| **Auth** | Optional Basic auth (`test:test`), works without |
| **Rate limit** | 1 second between requests (no anti-bot protection) |
| **Product discovery** | Algolia search API (for URL enumeration) |
| **Product details** | Internal Product API (NOT Algolia) |
| **Anti-bot** | None detected — CloudFront CDN with 5-second cache |
| **Pagination** | Algolia: `offset` + `length` params, max 1000 per query |

### Sale Detection Logic

```python
# Only show original_price when there is an actual discount
if data.get("sale") == "ONSALE":
    list_min = data["priceRange"]["listMin"]
    sale_min = data["priceRange"]["saleMin"]
    if list_min > sale_min:
        original_price = f"A${list_min:.2f}"
        discount_pct = round((1 - sale_min / list_min) * 100)
    else:
        original_price = ""
else:
    original_price = ""
```

### URL Handling

Product URLs may contain optional parameters to strip:
- `?color={colorSlug}` — color variant selector (safe to keep)
- `&size={size}` — size selector (safe to keep)
- `&sskey=` — session/affiliate key (strip)
- `&sku=` — redundant product code (strip)

## Common Issues

1. **NOT_FOUND with HTTP 200:** Always check response body for `{"result": "NOT_FOUND"}`. Do not rely on status codes.
2. **Listing page URLs in input:** Filter out non-product URLs that don't contain `/product/` in the path.
3. **Duplicate color variants:** Multiple URLs may point to different colors of the same product. Use `classId_styleId` prefix for deduplication.
4. **Mixed availability:** Products can have some sizes in stock and others out of stock. Report as "In Stock" if ANY variant is available.
5. **No discount but sale flag set:** If `listMin == saleMin`, treat as no sale (set `original_price` empty).

## When NOT to Use

- Site does NOT use `i.localised.com` CDN
- No "Localised Hong Kong Limited" text found in footer
- No `/api/product/s/` endpoint accessible
- JSON API returns 403 or 404 consistently
