---
name: algolia-detection
description: Detect Algolia-powered ecommerce sites and use their public Search API to extract structured product data. Covers detection, credential extraction, the POST-based query format, the 1000-result limit workaround via facet partitioning, and field mapping.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
  learned_from: https://allthedresses.com.au
  learned_date: 2026-06-06
---

# Algolia Detection & API Usage

## What I Do

Detect ecommerce sites that use Algolia as their search/index layer and extract product data directly from the Algolia Search API. This bypasses HTML scraping entirely — the API returns structured JSON with all product fields, making it the fastest and most reliable extraction method when applicable.

## When to Use Me

Use this when:
- The Site Analyzer agent is analyzing a website
- Platform detection phase is running
- Page source or network traffic shows Algolia integration
- A product search or listing page uses Algolia-powered search

## Detection Methods

### Method 1: Page Source Markers

Check the HTML source for these indicators:

```html
<!-- Script tags -->
<script src="https://cdn.jsdelivr.net/npm/algoliasearch@..."></script>
<script src="https://cdn.jsdelivr.net/algolia@..."></script>

<!-- JavaScript globals -->
window.algolia
window.AlgoliaAnalytics
algoliaConfig
algoliaOptions

<!-- CSS classes -->
algolia-autocomplete
.ais-*
.aa-*

<!-- Data attributes -->
data-algolia-index
data-algolia-app-id
data-algolia-api-key
```

```
playwright_browser_evaluate → function: () => {
    return {
        hasAlgolia: typeof window.algolia !== 'undefined',
        hasAlgoliaAnalytics: typeof window.AlgoliaAnalytics !== 'undefined',
        algoliaConfig: typeof window.algoliaConfig !== 'undefined',
    };
}
```

### Method 2: Network Traffic (Playwright MCP)

Watch for POST requests to Algolia endpoints during page interaction (typing in search box, loading category pages, scrolling product listings):

```
playwright_browser_network_requests → filter: "algolia"
```

Look for requests matching:
- `POST https://{APP_ID}-dsn.algolia.net/1/indexes/*/queries`
- `POST https://{APP_ID}.algolia.net/1/indexes/*/queries`
- `POST https://{APP_ID}-1.algolianet.com/1/indexes/*/queries`

The wildcard `*` in the URL path is literal — it means "search across all indices." The actual index name is specified inside the request body.

### Method 3: JavaScript Variables

Many sites expose Algolia configuration in global JS variables:

```javascript
// Common patterns to check via browser_evaluate
window.algoliaConfig        // Full config object with appId, apiKey, index
window.algoliaOptions       // Alternative config name
window.__algolia             // Some bundlers use this

// Example structure
window.algoliaConfig = {
    appId: "ABC123XYZ",
    apiKey: "public-search-only-key",
    indexName: "products_production",
    // ...
}
```

## API Structure

Algolia uses a **POST-based** query format (not REST-like GET endpoints). All search queries go to a single endpoint.

### Endpoint

```
POST https://{APPLICATION_ID}-dsn.algolia.net/1/indexes/*/queries
```

Alternative hostnames that may work:
- `{APP_ID}-dsn.algolia.net` (primary)
- `{APP_ID}.algolia.net`
- `{APP_ID}-1.algolianet.com`
- `{APP_ID}-2.algolianet.com`
- `{APP_ID}-3.algolianet.com`

### Required Headers

```python
headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Algolia-Application-Id": "YOUR_APP_ID",
    "X-Algolia-API-Key": "YOUR_PUBLIC_API_KEY",
    "User-Agent": "Mozilla/5.0 ...",
}
```

> **Note:** The API key exposed in frontend JavaScript is the **public search-only key**. It is intentionally exposed — it is not a secret. It can only search, not write or delete records.

### Request Body Format

```json
{
  "requests": [
    {
      "indexName": "products_production",
      "params": "hitsPerPage=20&page=0&query=&filters=NOT hidden:true&facets=Category"
    }
  ]
}
```

The `params` field is a **URL-encoded query string** containing all query parameters. This is Algolia's standard format — NOT standard JSON query parameters.

### Key `params` Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `hitsPerPage` | Number of results per page (max 1000) | `hitsPerPage=1000` |
| `page` | 0-indexed page number | `page=0` |
| `query` | Full-text search query (empty = all) | `query=` or `query=red dress` |
| `filters` | Filter expression | `filters=NOT hidden:true AND Price > 10` |
| `facets` | Facet names to return counts for | `facets=Category,Brand` |
| `maxValuesPerFacet` | Max facet values per facet | `maxValuesPerFacet=1000` |
| `attributesToRetrieve` | Which fields to return | `attributesToRetrieve=name,price,url` |

### Response Structure

```json
{
  "results": [
    {
      "hits": [
        {
          "objectID": "prod_12345",
          "name": "Product Name",
          "price": 29.99,
          "url": "/products/slug",
          "_highlightResult": { ... }
        }
      ],
      "nbHits": 7856,
      "page": 0,
      "nbPages": 8,
      "hitsPerPage": 1000,
      "facets": {
        "Category": {
          "Dresses": 3200,
          "Tops": 1500
        }
      },
      "processingTimeMs": 12
    }
  ]
}
```

## How to Extract Credentials from Page Source

### Step 1: Find the Application ID

```python
import re

# Method 1: Look in script sources for Algolia client initialization
# Pattern: algoliasearch("APP_ID", "API_KEY") or new AlgoliaSearch("APP_ID", ...)
app_id_patterns = [
    r'algoliasearch\s*\(\s*["\']([\w]+)["\']',
    r'AlgoliaSearch\s*\(\s*["\']([\w]+)["\']',
    r'appId\s*[:=]\s*["\']([\w]+)["\']',
    r'applicationId\s*[:=]\s*["\']([\w]+)["\']',
    r'x-algolia-application-id["\']\s*:\s*["\']([\w]+)["\']',
    r'(\w{8,16})-dsn\.algolia\.net',  # Extract from CDN URL
]

# Method 2: Check network traffic for the header
# X-Algolia-Application-Id: ABC123XYZ
```

### Step 2: Find the Public API Key

```python
api_key_patterns = [
    r'algoliasearch\s*\(\s*["\'][\w]+["\']\s*,\s*["\']([\w]+)["\']',
    r'apiKey\s*[:=]\s*["\']([\w]{20,})["\']',
    r'X-Algolia-API-Key["\']\s*:\s*["\']([\w]+)["\']',
    r'search-only-api-key["\']\s*:\s*["\']([\w]+)["\']',
]
```

### Step 3: Discover the Index Name

The index name appears in the request body's `indexName` field. To find it:

1. **From network traffic:** Intercept any Algolia POST request and read the `indexName` in the body.
2. **From JavaScript globals:** Check `window.algoliaConfig.indexName` or similar.
3. **From page source regex:**

```python
index_name_patterns = [
    r'indexName\s*[:=]\s*["\']([\w_]+)["\']',
    r'"indexName"\s*:\s*"([\w_]+)"',
]
```

4. **From the facets endpoint (if known):** Make a query with `facets=*` and examine which facets return data.

## The 1000-Result Limit & Facet-Based Partitioning

**CRITICAL:** Algolia hard-limits page-based pagination to **1000 results** per query. Page 1 (`page=0`) with `hitsPerPage=1000` is the maximum you can get per filter combination. There is no way to get page 2 with results 1001-2000.

If the total number of products exceeds 1000, you **must** partition the index using facet-based filtering.

### What is Facet Partitioning?

Use Algolia's own facet data to split the product set into mutually exclusive groups, each with ≤ 1000 products, then query each group separately.

**Example:** If you have 7856 products across 15 categories:
1. Query facets to get counts: `Dresses: 3200, Tops: 1500, Bags: 800, ...`
2. Categories with ≤ 1000 products (Bags, Shoes, etc.) — fetch all directly
3. Categories with > 1000 (Dresses: 3200) — subdivide by a secondary facet (Designer)
4. Within Dresses: `DesignerA: 400, DesignerB: 350, DesignerC: 300, ...`
5. Group designers into chunks of ≤ 1000 total and fetch each chunk

### The `_fq()` Safe Quoting Helper

Always wrap filter values in double quotes to handle spaces, ampersands, and special characters:

```python
def _fq(val: str) -> str:
    """Quote a filter value for safe inclusion in Algolia filter expressions.

    Wraps values in double quotes to handle spaces, ampersands,
    and other special characters that would break filter syntax.
    """
    return f'"{val}"'
```

Usage:
```python
# WRONG — breaks if value contains spaces or special chars
f"ProductType:{product_type}"

# RIGHT — always safe
f"ProductType:{_fq(product_type)}"
# Produces: ProductType:"Jackets & Coats"
```

### The `get_facet_counts()` Pattern

Use `hitsPerPage=0` to get facet counts without fetching any actual hits (fast and free):

```python
def get_facet_counts(facet_name: str, extra_filter: str = "") -> dict[str, int]:
    """Get facet value counts for a given facet and filter.

    Uses hitsPerPage=0 to retrieve only facet metadata (no hits).
    Returns dict mapping facet values to their hit counts.
    """
    params_parts = [
        "hitsPerPage=0",       # No hits, just facet data
        "page=0",
        "query=",
        f"filters={quote(extra_filter, safe='')}",
        f"facets={facet_name}",
        "maxValuesPerFacet=1000",
    ]
    params = "&".join(params_parts)
    body = {"requests": [{"indexName": INDEX_NAME, "params": params}]}

    data = algolia_post(body)
    if not data:
        return {}

    return data["results"][0].get("facets", {}).get(facet_name, {})
```

### The `group_facet_values_into_chunks()` Algorithm

Greedy bin-packing to group facet values into chunks where each chunk's total count ≤ 1000:

```python
def group_facet_values_into_chunks(
    facet_counts: dict[str, int],
    max_per_chunk: int = 1000,
) -> list[list[tuple[str, int]]]:
    """Group facet values into chunks where each chunk's total count <= max_per_chunk.

    Sorts facet values by count (descending) and greedily packs them.
    """
    sorted_facets = sorted(facet_counts.items(), key=lambda x: -x[1])
    chunks: list[list[tuple[str, int]]] = []
    current_chunk: list[tuple[str, int]] = []
    current_sum = 0

    for value, count in sorted_facets:
        if current_sum + count > max_per_chunk and current_chunk:
            chunks.append(current_chunk)
            current_chunk = [(value, count)]
            current_sum = count
        else:
            current_chunk.append((value, count))
            current_sum += count

    if current_chunk:
        chunks.append(current_chunk)

    return chunks
```

### The `discover_products_via_partitioning()` Full Algorithm

```python
def discover_products_via_partitioning(
    primary_facet: str = "ProductType",
    secondary_facet: str = "Designer",
    base_filter: str = "",
) -> list[dict]:
    """Discover ALL products by partitioning via facets.

    1. Get primary facet counts (e.g., ProductType)
    2. For facets <= 1000: fetch all directly
    3. For facets > 1000: subdivide by secondary facet (e.g., Designer)
    4. Group secondary values into chunks of <= 1000 and fetch each
    """
    all_products = []
    seen_ids = set()

    # Step 1: Get primary facet counts
    primary_counts = get_facet_counts(primary_facet, extra_filter=base_filter)

    # Step 2: Process each primary facet value
    for value, count in sorted(primary_counts.items(), key=lambda x: -x[1]):
        primary_filter = f"{primary_facet}:{_fq(value)}"

        if count <= 1000:
            # Fits in one query - fetch directly
            hits = fetch_all_hits_for_filter(
                extra_filter=f"AND {primary_filter}"
            )
            for hit in hits:
                oid = hit.get("objectID")
                if oid and oid not in seen_ids:
                    seen_ids.add(oid)
                    all_products.append(hit)
        else:
            # Too many - subdivide by secondary facet
            secondary_counts = get_facet_counts(
                secondary_facet,
                extra_filter=f"AND {primary_filter}",
            )
            chunks = group_facet_values_into_chunks(secondary_counts)

            for chunk in chunks:
                or_filters = " OR ".join(
                    f"{secondary_facet}:{_fq(v)}" for v, _ in chunk
                )
                combined = f"AND {primary_filter} AND ({or_filters})"
                hits = fetch_all_hits_for_filter(extra_filter=combined)
                for hit in hits:
                    oid = hit.get("objectID")
                    if oid and oid not in seen_ids:
                        seen_ids.add(oid)
                        all_products.append(hit)

    return all_products
```

### Important Notes on Partitioning

1. **Always deduplicate by `objectID`** — facet overlaps can cause duplicate results
2. **Choose primary/secondary facets wisely** — use facets with many distinct values and roughly even distribution (e.g., `ProductType` then `Designer`)
3. **URL-encode the full params string** — use `urllib.parse.quote()` on filter values to handle `&`, spaces, and other characters that conflict with params syntax
4. **Log chunk progress** — partitioning can generate many API calls; track expected vs. actual counts to detect gaps

## Product Scraper Mode (Per-Product Lookup)

When building a **product scraper** (not a site crawler), use Algolia as a per-product lookup tool:

1. Extract the product ID (objectID) from the product URL
2. Query Algolia by objectID: `filters=objectID:{_fq(product_id)}`
3. Map the returned fields to the output schema

```python
def lookup_product_by_objectid(object_id: str) -> Optional[dict]:
    """Look up a single product by its Algolia objectID."""
    params = (
        f"hitsPerPage=1&page=0&query="
        f"&filters=objectID:{_fq(object_id)}"
        f"&attributesToRetrieve=*"
    )
    body = {"requests": [{"indexName": INDEX_NAME, "params": params}]}
    data = algolia_post(ENDPOINT, HEADERS, body)
    if data and data["results"][0].get("hits"):
        return data["results"][0]["hits"][0]
    return None
```

**DO NOT** use the discover/facet/partitioning patterns below when building a product scraper. Those are for site-wide catalog discovery only. Product scrapers get their URLs from `input_urls.json`.

## Field Mapping: Algolia → Output

The exact field names vary per Algolia index (they are custom-defined by each site). However, common patterns exist:

| Algolia Field (typical) | Output Field | Notes |
|------------------------|-------------|-------|
| `name` or `title` or `ProductTitle` | `title` | Product name — check for site-specific variant |
| `price` or `Price` or `RentalPrice` | `price` | May need formatting (e.g., prepend "$") |
| `compare_at_price` or `original_price` or `RetailPrice` | `original_price` | Omit if 0 or null |
| `url` or `Slug` or `product_url` | `url` | May need to construct full URL from slug |
| `availability` or `in_stock` or `PrimaryProduct` | `availability` | Algolia often uses boolean; map to "In Stock"/"Out of Stock" |
| `image` or `images` or `thumbnail_url` | (not in standard schema) | Can be added to remarks or a custom field |
| `categories` or `ProductType` | (not in standard schema) | Can be added to remarks |
| `brand` or `Designer` or `vendor` | (not in standard schema) | Can be added to remarks |
| `objectID` | (not in standard schema) | Include in remarks for traceability |

> **Tip:** Use a single query with `hitsPerPage=1` and `attributesToRetrieve=*` (or omit it) to see all available fields, then map the relevant ones.

## Rate Limiting

Algolia's public API rate limits depend on the pricing plan. For safe scraping:

| Context | Recommended Delay | Notes |
|---------|-------------------|-------|
| General scraping | 200-500ms | Safe default |
| High-volume discovery | 300ms | Proven in production (allthedresses.com.au) |
| Respectful mode | 500ms-1s | If site has additional protections |
| After error/429 | Exponential backoff | 1s, 2s, 4s... |

```python
DELAY_BETWEEN_REQUESTS = 0.3  # 300ms — proven safe
MAX_RETRIES = 3

def algolia_post(body: dict) -> Optional[dict]:
    """POST to Algolia with retry logic and rate limiting."""
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            response = requests.post(
                ENDPOINT, headers=HEADERS, json=body, timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Algolia request failed (attempt {attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                wait = DELAY_BETWEEN_REQUESTS * (2 ** (attempt + 1))
                time.sleep(wait)
    return None
```

## Reusable Code Patterns

These functions from the `allthedresses_com_au` scraper are generic enough to adapt for any Algolia-powered site:

### 1. `algolia_post()` — Core API caller with retry

```python
def algolia_post(endpoint: str, headers: dict, body: dict,
                 delay: float = 0.3, max_retries: int = 3) -> Optional[dict]:
    """Send a POST request to the Algolia API with retry logic."""
    for attempt in range(max_retries):
        try:
            time.sleep(delay)
            response = requests.post(endpoint, headers=headers, json=body, timeout=15)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Algolia API request failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(delay * (2 ** (attempt + 1)))
    return None
```

### 2. `_fq()` — Safe filter value quoting

```python
def _fq(val: str) -> str:
    """Quote a filter value for Algolia filter expressions."""
    return f'"{val}"'
```

### 3. `get_facet_counts()` — Facet-only queries (zero hits)

```python
def get_facet_counts(endpoint, headers, index_name, facet_name,
                     extra_filter="", max_values=1000):
    """Get facet value counts without fetching hits."""
    params_parts = ["hitsPerPage=0", "page=0", "query="]
    if extra_filter:
        params_parts.append(f"filters={quote(extra_filter, safe='')}")
    params_parts.extend([f"facets={facet_name}", f"maxValuesPerFacet={max_values}"])
    body = {"requests": [{"indexName": index_name, "params": "&".join(params_parts)}]}
    data = algolia_post(endpoint, headers, body)
    if not data:
        return {}
    return data["results"][0].get("facets", {}).get(facet_name, {})
```

### 4. `group_facet_values_into_chunks()` — Bin-packing for 1000 limit

See the full algorithm above in "The 1000-Result Limit" section.

### 5. `discover_products_via_partitioning()` — Full discovery

See the full algorithm above in "The 1000-Result Limit" section.

### 6. URL construction from slug

```python
def build_product_url(site_url: str, slug_field: str, hit: dict) -> str:
    """Build a full product URL from a slug field in the Algolia hit."""
    slug = hit.get(slug_field, "")
    return f"{site_url}/product/{slug}" if slug else ""
```

## What to Report Back

When Algolia is detected, report in `site_analysis.json`:

```json
{
  "platform": "custom",
  "scraping_mechanism": "algolia_api",
  "algolia": {
    "detected": true,
    "application_id": "ABC123XYZ",
    "api_key": "public-key-here",
    "index_name": "products_production",
    "endpoint": "https://abc123xyz-dsn.algolia.net/1/indexes/*/queries",
    "total_products": 7856,
    "requires_partitioning": true,
    "partition_facets": ["ProductType", "Designer"],
    "base_filter": "NOT PrimaryProduct:false",
    "notes": "Cursor pagination not supported. Facet partitioning needed for full catalog."
  }
}
```

## Common Issues

1. **CORS blocking:** The public API key is meant for frontend use. If CORS blocks server-side requests, try adding a browser-like `Origin` header or use Playwright to execute the fetch from the browser context.
2. **Wrong index name:** Sites may use multiple indices. Check network traffic for the correct one (often ends in `_production` or `prod_`).
3. **Missing fields:** The API returns only indexed fields. If a field is missing, it may not be configured in the Algolia index schema.
4. **429 Too Many Requests:** Slow down. Algolia rate-limits by API key and application.
5. **Facet not available:** Not all Algolia indices expose all attributes as facets. Use `facets=*` in a test query to discover which facets are available.
6. **Filter syntax errors:** Always URL-encode filter values and use `_fq()` for safe quoting. Unquoted values with special characters will silently produce wrong results.

## When NOT to Use

- Site does NOT use Algolia (detection failed)
- Algolia API returns 403 (API key restricted or expired)
- The Algolia index has very few products (easier to scrape via HTML)
- Cursor pagination IS supported (then you don't need facet partitioning — just paginate)
- The site uses Algolia for search only, not for product listing/browsing

## Proven Example

**allthedresses.com.au** — Dress rental marketplace with 7856+ products from 40+ vendors.

- **Scraper:** `scrapers/allthedresses_com_au/scraper.py`
- **Index:** `PROD_mp_products`
- **Partitioning:** ProductType (primary) → Designer (secondary)
- **Duration:** ~30 seconds for full catalog discovery
- **Key lesson:** `hitsPerPage=0` for facet-only queries is essential for efficient partitioning
