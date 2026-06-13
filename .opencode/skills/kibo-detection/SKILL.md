---
name: kibo-detection
description: Detect Kibo Commerce (formerly Mozu) ecommerce sites and leverage their JSON-LD with non-standard AggregateOffer priceSpecification structure for efficient product data extraction.
---
## What I Do

Detect Kibo Commerce ecommerce sites and leverage their structured data patterns for efficient product extraction.

## When to Use Me

Use this when:
- Site Analyzer detects a non-Shopify, non-WooCommerce, non-Magento, non-SFCC site
- Page source contains Mozu/Kibo references
- API endpoints at `/api/commerce/` or `/api/platform/`
- Images served from `cdn-tp6.mozu.com`

## Detection Methods

### URL Patterns
- Product pages: `/departments/{L1}/{L2}/{L3}/{SKU}` (SKU is numeric)
- Brand pages: `/brands/{brand-name}`
- Search: `/search?q={query}`

### Page Source Markers
```
cdn-tp6.mozu.com — CDN for images/assets
catalogContent@mozu — Page document type in embedded JSON
Mozu page context — JavaScript page state object
/api/commerce/ — Kibo Commerce API base path
/api/platform/ — Kibo Platform API base path
div.mz-breadcrumb — Breadcrumb component class
```

### Cookie Patterns
Kibo sites set `kibo_*` cookies and may use `dwsid`-like session cookies.

### JavaScript Global Objects
```javascript
// Check for Kibo page context
document.querySelector('script')
// Look for Mozu references
document.documentElement.innerHTML.includes('mozu')
```

## Detection Checklist
```python
indicators = []
if 'cdn-tp6.mozu.com' in page_source: indicators.append('mozu_cdn')
if 'catalogContent@mozu' in page_source: indicators.append('mozu_page_context')
if '/api/commerce/' in page_source: indicators.append('kibo_commerce_api')
if 'div.mz-breadcrumb' in page_source: indicators.append('mozu_breadcrumb')
```

Two or more indicators = confirmed Kibo Commerce site.

## Product Page Extraction

### JSON-LD Structure (Primary Source)

Kibo sites emit rich JSON-LD with a **non-standard** offers structure using `AggregateOffer` with `priceSpecification[]`:

```json
{
  "@type": "Product",
  "name": "Product Name",
  "sku": "1234567",
  "gtin": "012345678901",
  "mpn": "ABC123",
  "brand": { "@type": "Brand", "name": "Brand Name" },
  "image": ["https://cdn-tp6.mozu.com/..."],
  "description": "Product description...",
  "offers": {
    "@type": "AggregateOffer",
    "priceCurrency": "USD",
    "availability": "https://schema.org/InStock",
    "itemCondition": "https://schema.org/NewCondition",
    "priceSpecification": [
      {
        "@type": "PriceSpecification",
        "price": "37.59",
        "priceCurrency": "USD",
        "priceType": "https://schema.org/SalePrice"
      },
      {
        "@type": "PriceSpecification",
        "price": "46.99",
        "priceCurrency": "USD",
        "priceType": "https://schema.org/ListPrice"
      }
    ]
  }
}
```

**CRITICAL:** Kibo does NOT use the standard `offers.price` / `offers.highPrice` pattern. It uses `priceSpecification[]` with `priceType` to differentiate sale vs original prices.

### Price Extraction Logic

```python
def extract_price_from_kibo_jsonld(product_block: dict) -> tuple[str, str]:
    """Extract current price and original price from Kibo JSON-LD."""
    offers = product_block.get("offers", {})
    price_specs = offers.get("priceSpecification", [])
    if not isinstance(price_specs, list):
        price_specs = [price_specs] if price_specs else []

    sale_prices = []
    list_prices = []

    for spec in price_specs:
        price = str(spec.get("price", ""))
        ptype = spec.get("priceType", "")
        if price:
            if "SalePrice" in ptype:
                sale_prices.append(float(price))
            elif "ListPrice" in ptype:
                list_prices.append(float(price))

    current = min(sale_prices) if sale_prices else min(list_prices) if list_prices else 0
    original = max(list_prices) if list_prices else 0

    price_str = f"${current:.2f}" if current else ""
    original_str = f"${original:.2f}" if original and original > current else ""
    return price_str, original_str
```

### Additional JSON-LD Fields
- `gtin` — Global Trade Item Number
- `mpn` — Manufacturer Part Number
- `brand.name` — Brand name
- `material` — Product material
- `color` — Product color
- `height`, `width`, `length` — Dimensions
- `category` — Category array
- `positiveNotes` — Marketing highlights

### CSS Selector Fallbacks

| Field | Selector | Notes |
|-------|----------|-------|
| Title | `h1` | Usually reliable |
| Current Price | `div.price` | Sale/current price |
| Original Price | `div.span-crossedout` | Strikethrough MSRP |
| Sale Badge | `div.price-title` | Text like "Online Sale" |
| SKU | Regex `Item # (\d+)` | In page text |
| MPN | Regex `Mfr # (\S+)` | In page text |
| Breadcrumbs | `div.mz-breadcrumb a` | Mozu breadcrumb component |
| Specifications | List items in Specs section | Key-value pairs |

### Review Systems
Kibo sites commonly use **BazaarVoice** for reviews:
- JSON-LD Product block with `aggregateRating` (no `offers`) = reviews block
- BazaarVoice API endpoints for review data (requires passkey)
- Rating extracted from `aggregateRating.ratingValue`

```python
# Disambiguation: Product block with aggregateRating but no offers = reviews
for block in json_ld_blocks:
    if block.get("@type") == "Product":
        if "aggregateRating" in block and "offers" not in block:
            reviews_block = block
        elif "offers" in block:
            product_block = block
```

## Third-Party Integrations
Kibo sites commonly include:
- **BazaarVoice** — Reviews and ratings
- **Monetate** — Personalization/recommendations (se.monetate.net)
- **Google Analytics 4** + Google Tag Manager
- **OneTrust** — Cookie consent (Accept All button, not blocking)

## Characteristics

- **Fully server-side rendered** — HTTP requests work, no browser needed
- **No anti-bot protection** — Standard rate limiting sufficient (2-3s delay)
- **Rich JSON-LD** — Primary data source, CSS as fallback
- **Store-specific pricing** — Prices may vary by geolocation/store selection
- **Product URLs contain SKU** — Last path segment is the numeric SKU

## Known Kibo Commerce Sites

- acehardware.com — Hardware/home improvement
- vitaminshoppe.com — Vitamins/supplements
- bootbarn.com — Western wear/boots
- moosejaw.com — Outdoor gear
- And hundreds more

## When NOT to Use

- Site is clearly Shopify/WooCommerce/Magento/SFCC (use their specific skills)
- No Mozu/Kibo indicators found in page source
- JSON-LD is absent (fall back to CSS-only extraction)

Base directory for this skill: file:///mnt/d/John/u-ecom-scraper/.opencode/skills/kibo-detection
