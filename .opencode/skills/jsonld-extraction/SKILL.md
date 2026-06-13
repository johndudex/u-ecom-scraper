---
name: jsonld-extraction
description: Extract product data from JSON-LD structured data blocks on ecommerce pages, including price, availability, images, variants, and reviews from schema.org Product types.
---
## What I Do

Provide patterns for extracting product data from JSON-LD `<script type="application/ld+json">` blocks on ecommerce pages. Covers standard schema.org Product types and common variations.

## When to Use Me

Use this when:
- Product pages contain `<script type="application/ld+json">` blocks
- JSON-LD Product blocks have structured data for name, price, availability, images
- You need to distinguish between multiple JSON-LD blocks (product, reviews, breadcrumbs)
- You need to extract original_price (pre-discount price) from schema.org offers

## JSON-LD Block Types

A single product page may contain multiple JSON-LD blocks:

| Block | @type | Contains |
|-------|-------|----------|
| Product | `Product` | name, description, sku, mpn, image[], offers |
| Reviews | `Product` (with aggregateRating) | aggregateRating, review[] |
| Breadcrumbs | `BreadcrumbList` | itemListElement[] |
| Website | `WebSite` | Site metadata |
| Organization | `Organization` | Brand/company info |

**Critical:** Always check `@type` AND check for distinguishing fields. Two blocks with `@type: "Product"` may coexist — one for core data, one for reviews.

### Disambiguation Strategy

```python
product_block = None
reviews_block = None

for block in json_ld_blocks:
    if block.get("@type") == "Product":
        if "aggregateRating" in block:
            reviews_block = block
        elif "offers" in block:
            product_block = block
```

## Offers Structure

The `offers` field can be a single object or an array:

```json
// Single offer
"offers": {
    "@type": "Offer",
    "price": "17.00",
    "priceCurrency": "GBP",
    "availability": "http://schema.org/InStock",
    "highPrice": "29.00",
    "lowPrice": "17.00",
    "url": "/uk/product-name-123.html"
}

// Multiple offers
"offers": [
    {"@type": "Offer", "price": "17.00", "priceCurrency": "GBP", ...},
    {"@type": "Offer", "price": "19.00", "priceCurrency": "EUR", ...}
]
```

### Handling Array vs Object

```python
offers = product_block.get("offers", {})
if isinstance(offers, list):
    offers = offers[0] if offers else {}
```

## Original Price (Price Before Discount)

### Method 1: `highPrice` Field (Recommended)

Schema.org allows `highPrice` and `lowPrice` on Offer to indicate a price range. On sale items, `highPrice` = original price, `lowPrice` = sale price, `price` = current price.

```python
raw_price = offers.get("price", "")
raw_high_price = offers.get("highPrice", "")

if raw_price and raw_high_price:
    try:
        price_float = float(raw_price)
        high_price_float = float(raw_high_price)
        if high_price_float > price_float:
            # Product is on sale — highPrice is the original
            product["price"] = format_price(price_float, currency)
            product["original_price"] = format_price(high_price_float, currency)
        else:
            # Not on sale
            product["price"] = format_price(price_float, currency)
            product["original_price"] = ""
    except (ValueError, TypeError):
        pass
```

**Important:** Only set `original_price` when `highPrice > price`. Equal values mean no discount.

### Method 2: Multiple Offers

Some sites list original and sale as separate offers:

```python
offers_list = product_block.get("offers", [])
if isinstance(offers_list, list) and len(offers_list) > 1:
    prices = [float(o.get("price", 0)) for o in offers_list if o.get("price")]
    if prices:
        product["price"] = format_price(min(prices), currency)
        product["original_price"] = format_price(max(prices), currency) if max(prices) > min(prices) else ""
```

### Method 3: priceSpecification[] (Kibo Commerce / Mozu)

Kibo Commerce sites use `AggregateOffer` with a `priceSpecification[]` array. Each spec has a `priceType` indicating whether it's a sale price or the original MSRP:

```json
"offers": {
  "@type": "AggregateOffer",
  "priceCurrency": "USD",
  "priceSpecification": [
    { "@type": "PriceSpecification", "price": "37.59", "priceType": "https://schema.org/SalePrice" },
    { "@type": "PriceSpecification", "price": "46.99", "priceType": "https://schema.org/ListPrice" }
  ]
}
```

```python
def extract_price_from_kibo(product_block: dict) -> tuple[str, str]:
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

**Detection:** Check if `offers` has `priceSpecification` key (not `price` directly).

### Method 4: CSS Fallback

When JSON-LD doesn't have `highPrice`, use CSS selectors:

```python
orig_el = soup.select_one(".price--was, .original-price, .was-price, [data-original-price], .price__standard, .b-price__standard")
if orig_el:
    product["original_price"] = orig_el.get_text(strip=True)
```

Common CSS selectors for original/discounted price across platforms:
- `.price--was`, `.was-price`, `.old-price` — Generic
- `.original-price`, `.compare-at-price` — Shopify
- `.b-price__standard` — SFCC/Demandware
- `[data-original-price]` — Data attribute
- `del`, `s` — Strikethrough/old price HTML elements

## Price Formatting

Always format prices with currency symbol:

```python
def format_price(price_value: float, currency: str) -> str:
    symbols = {
        "GBP": "\u00a3", "USD": "$", "EUR": "\u20ac",
        "CAD": "C$", "AUD": "A$", "JPY": "\u00a5",
    }
    symbol = symbols.get(currency, currency + " ")
    return f"{symbol}{price_value:.2f}"
```

## Availability Mapping

```python
def map_availability(raw: str) -> str:
    mapping = {
        "http://schema.org/InStock": "In Stock",
        "http://schema.org/OutOfStock": "Out of Stock",
        "http://schema.org/PreOrder": "Pre-Order",
        "http://schema.org/LimitedAvailability": "Limited Stock",
        "http://schema.org/Discontinued": "Discontinued",
        "http://schema.org/SoldOut": "Sold Out",
    }
    return mapping.get(raw, raw.replace("http://schema.org/", ""))
```

## Ratings from External Review Systems

### PowerReviews (SFCC, some custom sites)

Some SFCC and other sites use PowerReviews, which emits a SECOND Product JSON-LD block:

```json
{
    "@type": "Product",
    "name": "Same Product Name",
    "@id": "same-product-id",
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": 4.5,
        "reviewCount": 12,
        "bestRating": 5
    },
    "review": [...]
}
```

Detect: Product block with `aggregateRating` but without `offers`.

### BazaarVoice (Kibo, many enterprise sites)

BazaarVoice is the most widely deployed review platform (thousands of sites including Best Buy, Home Depot, Sephora). It also emits a secondary Product JSON-LD block with ratings:

```json
{
    "@type": "Product",
    "name": "Product Name",
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": 4.2,
        "reviewCount": 46,
        "bestRating": 5
    }
}
```

**BazaarVoice API endpoints** (requires passkey from page source):
```
https://api.bazaarvoice.com/data/display/0.2alpha/product/summary?PassKey={key}&productid={sku}&contentType=reviews,questions
https://api.bazaarvoice.com/data/reviews.json?resource=reviews&filter=productid:eq:{sku}&passkey={key}
https://api.bazaarvoice.com/data/products.json?passkey={key}&filter=id:{sku}
```

Detect: Same disambiguation as PowerReviews (aggregateRating without offers).

## Extraction Function (Complete)

```python
import json
import re
from bs4 import BeautifulSoup

def parse_json_ld(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                results.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return results

def clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()
```

## Verified Patterns

| Platform | JSON-LD Present | Price Pattern | Multiple Product Blocks | Reviews | Notes |
|----------|---------------|--------------|------------------------|--------|-------|
| SFCC/Demandware | Yes | highPrice | Yes (Product + PowerReviews) | PowerReviews | Rich data, reliable |
| Kibo/Mozu | Yes | priceSpecification[] | Yes (Product + BazaarVoice) | BazaarVoice | Non-standard offers |
| Shopify | Yes | Rare | No | None | Single Product block |
| WooCommerce | Sometimes | Rare | No | None | May use microdata instead |
| Magento | Sometimes | Rare | No | None | Variable quality |

## When NOT to Use

- No `<script type="application/ld+json">` on the page
- JSON-LD is empty or malformed
- Page is heavily JavaScript-rendered and JSON-LD loads after initial HTML (use Playwright)
- Structured data doesn't match visible product info (site may inject fake SEO data)

Base directory for this skill: file:///mnt/d/John/u-ecom-scraper/.opencode/skills/jsonld-extraction
