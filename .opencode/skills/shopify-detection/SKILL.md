---
name: shopify-detection
description: Detect Shopify ecommerce sites and leverage their product.json API, Storefront GraphQL API, and collection JSON endpoints for fast, structured product data extraction.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
---

# Shopify Detection & API Usage

## What I Do

Detect Shopify-powered ecommerce sites and use their public APIs to extract product data without browser automation. This is the fastest scraping method available when applicable.

## When to Use Me

Use this when:
- The Site Analyzer agent is analyzing a website
- Platform detection phase is running
- A potential Shopify site has been identified

## Detection Methods

### Method 1: Page Source Markers

Check for these in the HTML source:

```html
<!-- CDN references -->
cdn.shopify.com
Shopify.theme
shopify-common.js

<!-- Meta tags -->
<meta name="shopify-digital-wallet" content="...">
<meta property="og:site_name" content="...">
```

### Method 2: URL Patterns

Shopify sites typically use these URL structures:
- `/products/{handle}` - Product pages
- `/collections/{handle}` - Collection/category pages
- `/collections/all` - All products
- `/collections/{type}?sort_by=...` - Sorted collections
- `/cart` - Shopping cart
- `/search` - Search page

### Method 3: JavaScript Objects

Shopify injects product data into the page:

```javascript
// Check for Shopify global objects
window.Shopify
window.Shopify.theme
document.querySelector('[data-product-json]')

// Product JSON embedded in page
JSON.parse(document.querySelector('[data-product]')?.textContent)
```

### Method 4: HTTP Header Check

```bash
curl -sI https://www.example.com | grep -i shopify
# X-ShopId: 12345
# X-Shopify-Stage: production
```

## API Endpoints

### Product JSON (Public, No Auth)

The most reliable and commonly used approach:

```bash
# Single product by handle
curl -s "https://www.example.com/products/sneaker-shoe.json"

# All products via collections (paginated)
curl -s "https://www.example.com/collections/all/products.json?limit=250&page=1"
curl -s "https://www.example.com/collections/all/products.json?limit=250&page=2"

# Products by collection
curl -s "https://www.example.com/collections/shoes/products.json?limit=250&page=1"
```

**Response structure:**
```json
{
  "products": [
    {
      "id": 123456789,
      "title": "Product Name",
      "handle": "product-handle",
      "body_html": "<p>Full description...</p>",
      "vendor": "Brand Name",
      "product_type": "Shoes",
      "tags": ["summer", "new"],
      "published_at": "2024-01-01T00:00:00-00:00",
      "variants": [
        {
          "id": 987654321,
          "title": "Default Title",
          "option1": "Blue",
          "option2": "Large",
          "sku": "SKU-123",
          "price": "29.99",
          "compare_at_price": "49.99",
          "available": true,
          "inventory_quantity": 15
        }
      ],
      "images": [
        {
          "id": 111,
          "src": "https://cdn.shopify.com/s/files/1/.../image.jpg",
          "alt": "Product image",
          "width": 800,
          "height": 800
        }
      ],
      "options": [
        {
          "name": "Color",
          "values": ["Blue", "Red", "Green"]
        },
        {
          "name": "Size",
          "values": ["Small", "Medium", "Large"]
        }
      ],
      "created_at": "2024-01-01T00:00:00-00:00",
      "updated_at": "2024-01-15T00:00:00-00:00"
    }
  ],
  "product_count": 500,
  "next_page": "https://www.example.com/collections/all/products.json?limit=250&page=2"
}
```

### Collection JSON

```bash
# List all collections
curl -s "https://www.example.com/collections.json?limit=250"

# Products in a specific collection
curl -s "https://www.example.com/collections/shoes/products.json?limit=250&page=1"
```

**Response structure:**
```json
{
  "collections": [
    {
      "id": 111,
      "title": "Shoes",
      "handle": "shoes",
      "description": "All shoes",
      "published_at": "...",
      "image": { "src": "..." }
    }
  ]
}
```

### Storefront GraphQL API

```bash
curl -s -X POST "https://www.example.com/api/2024-01/graphql.json" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "{
      products(first: 250) {
        edges {
          node {
            id
            title
            handle
            description
            productType
            vendor
            tags
            priceRange {
              minVariantPrice { amount currencyCode }
              maxVariantPrice { amount currencyCode }
            }
            images(first: 10) {
              edges {
                node { url altText }
              }
            }
            variants(first: 100) {
              edges {
                node {
                  id
                  title
                  sku
                  availableForSale
                  price { amount currencyCode }
                  compareAtPrice { amount currencyCode }
                  selectedOptions { name value }
                }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }"
  }'
```

### Search API

```bash
curl -s "https://www.example.com/search/suggest.json?q=&resources[type]=product&resources[limit]=250"
```

## Field Mapping: Shopify JSON → Output

| Shopify Field | Output Field | Notes |
|--------------|-------------|-------|
| `title` | `title` | Direct |
| `variants[0].price` | `price` | String to float |
| `variants[0].compare_at_price` | `sale_price` | Null if no sale |
| `"USD"` (from locale) | `currency` | Default to USD |
| `images[].src` | `images` | Array of URLs |
| `body_html` | `description` | Strip HTML tags |
| `handle` | `url` | Construct: `{site}/products/{handle}` |
| `vendor` | `brand` | Direct |
| `product_type` | `category` | Direct |
| `variants[]` | `variants` | Full variant objects |
| `variants[0].available` | `availability` | "in_stock" / "out_of_stock" |
| `variants[0].sku` | `sku` | Direct |
| `tags` | `tags` | Direct |
| `published_at` | `published_date` | ISO format |

## Password-Protected Stores

Some Shopify stores are password-protected. To access:

```python
import requests

session = requests.Session()
# Visit homepage first to get password page
session.get(f"{base_url}")

# Submit password (if known)
session.post(f"{base_url}/password", data={
    "password": "store-password",
    "form_type": "storefront_password"
})

# Now access APIs normally
response = session.get(f"{base_url}/products.json")
```

## Rate Limiting

Shopify's public JSON API does not have documented rate limits, but be respectful:
- **Recommended delay:** 0.5-1 second between requests
- **Page size:** `limit=250` is maximum
- **No more than 1 request per second** to avoid IP blocks

## Common Issues

1. **403 Forbidden:** Site may have restricted JSON API access. Fall through to browser method.
2. **Empty products array:** Collection might be empty, try `/collections/all/products.json`
3. **Missing images:** Some stores disable CDN access. Use Playwright to capture.
4. **Rate limited (429):** Slow down requests, add exponential backoff.
5. **handle unknown:** Need to discover product handles from listing page first.

## When NOT to Use

- Site is NOT Shopify (detection failed)
- JSON API returns 403 or empty responses
- Store uses custom product pages that override Shopify templates
- Structured data in JSON is incomplete compared to rendered page
