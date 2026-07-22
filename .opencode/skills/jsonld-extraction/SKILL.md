---
name: jsonld-extraction
description: Extract structured data from JSON-LD blocks on web pages. Covers schema.org Product, Article, JobPosting, DiscussionForumPosting, WebPage, and other types for multi-content-type scraping.
---
## What I Do

Provide patterns for extracting structured data from JSON-LD `<script type="application/ld+json">` blocks on web pages. Covers schema.org Product, Article/NewsArticle/BlogPosting, JobPosting, DiscussionForumPosting, WebPage, and more.

## When to Use Me

Use this when:
- Pages contain `<script type="application/ld+json">` blocks
- You need to extract product data (name, price, availability, images)
- You need to extract article data (headline, author, datePublished, articleBody)
- You need to extract job data (title, hiringOrganization, jobLocation, qualifications)
- You need to extract forum data (DiscussionForumPosting, Question/Answer)
- You need to distinguish between multiple JSON-LD blocks (product, reviews, breadcrumbs, articles)
- You need to extract original_price (pre-discount price) from schema.org offers

## Multi-Content-Type JSON-LD Blocks

### Product (Shopping)

| Block | @type | Contains |
|-------|-------|----------|
| Product | `Product` | name, description, sku, mpn, image[], offers |
| Reviews | `Product` (with aggregateRating) | aggregateRating, review[] |
| Breadcrumbs | `BreadcrumbList` | itemListElement[] |

### Article (Articles/News/Blog)

| Block | @type | Contains |
|-------|-------|----------|
| Article | `Article`, `NewsArticle`, `BlogPosting`, `TechArticle` | headline, author, datePublished, articleBody, image[], articleSection |
| Author | `Person` or `Organization` | name, url |

```python
article_types = ("Article", "NewsArticle", "BlogPosting", "TechArticle")
for block in json_ld_blocks:
    if block.get("@type") in article_types:
        return block
```

**Key fields:**
- `headline` or `name` — Article title
- `author.name` — Author (may be dict or list of dicts)
- `datePublished` — ISO 8601 publish date
- `articleBody` — Full article text
- `articleSection` — Category/section
- `keywords` — Tags (string or list)
- `image` — Featured image (string or array)

### Job Posting (Jobs)

| Block | @type | Contains |
|-------|-------|----------|
| Job | `JobPosting` | title, hiringOrganization, jobLocation, baseSalary, qualifications, employmentType |

```python
for block in json_ld_blocks:
    if block.get("@type") == "JobPosting":
        return block
```

**Key fields:**
- `title` — Job title
- `hiringOrganization.name` — Company name
- `jobLocation.address.addressLocality` — City
- `jobLocation.address.addressRegion` — State
- `baseSalary.value.value` — Salary amount
- `baseSalary.value.unitText` — "YEAR", "MONTH", "HOUR"
- `qualifications` — Requirements (string or list)
- `employmentType` — "FULL_TIME", "PART_TIME", "CONTRACT"
- `description` — Full job description
- `datePosted` — Posting date

### Forum Thread (Forum)

| Block | @type | Contains |
|-------|-------|----------|
| Thread | `DiscussionForumPosting`, `Question` | headline, author, text, answer, interactionStatistic |

```python
forum_types = ("DiscussionForumPosting", "Question")
for block in json_ld_blocks:
    if block.get("@type") in forum_types:
        return block
```

**Key fields:**
- `headline` or `name` — Thread title
- `author.name` — Thread starter
- `text` — Initial post content
- `answer[].text` — Reply content (for Question type)
- `interactionStatistic` — View/reply counts

### Web Page (Generic)

| Block | @type | Contains |
|-------|-------|----------|
| Page | `WebPage`, `AboutPage`, `ContactPage` | name, description, mainContent |

```python
for block in json_ld_blocks:
    if block.get("@type") in ("WebPage", "AboutPage", "ContactPage", "FAQPage"):
        return block
```

**Key fields:**
- `name` — Page title
- `description` — Page description
- `mainEntity` — Main content (varies)
- `dateModified` — Last modified date

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

## Learned: JobPosting JSON-LD title may be truncated/abbreviated
**Source:** locumtenens.com (2026-07-14)
**Applicability:** any job board where JSON-LD `JobPosting.title` differs from the visible `<h1>` title.

Some sites set the JSON-LD `title` to a short provider type or category (e.g. `"DNP"`, `"NP"`) instead of the full human-readable job title shown in `<h1>`. Blindly using `jsonld.title` loses the full title.

**Guard:** Always compare `jsonld.title` length to the `<h1>` text length. If the JSON-LD title is suspiciously short (≤ ~15 chars, no spaces, or no location keywords), prefer the `<h1>` CSS extraction instead.

```python
jsonld_title = jsonld.get("title", "")
h1_title = soup.select_one("h1")  # or site-specific h1 selector

if h1_title:
    h1_text = h1_title.get_text(strip=True)
    # Prefer h1 if jsonld title looks abbreviated
    if len(jsonld_title) < 15 or " " not in jsonld_title or len(jsonld_title) < len(h1_text) * 0.3:
        item["title"] = h1_text
    else:
        item["title"] = jsonld_title
else:
    item["title"] = jsonld_title
```

## Learned: addressLocality may contain city + state (contaminated)
**Source:** locumtenens.com (2026-07-14)
**Applicability:** any site where JSON-LD `PostalAddress.addressLocality` contains more than just the city name.

Some sites populate `addressLocality` with `"City, ST"` (city + state abbreviation) instead of just the city. This contaminates the city field and causes duplicate state info when building a full location string.

**Guard:** After extracting `addressLocality`, split on comma and take only the first part. Also check if it ends with a 2-letter uppercase code (state abbreviation).

```python
raw_city = addr.get("addressLocality", "")
if raw_city:
    # Strip trailing state abbreviation: "Capron, VA" → "Capron"
    city = raw_city.split(",")[0].strip()
else:
    city = ""

state = addr.get("addressRegion", "")

# Build location from clean city + state (no duplicates)
parts = [v for v in [city, state] if v]
location = ", ".join(parts)
```

**Note:** This pattern was observed on locumtenens.com where `addressLocality` was `"Capron, VA"` — after cleaning, city=`"Capron"`, state=`"VA"`, location=`"Capron, VA"` (correct).

## Learned: og:url may return the site root, not the page URL
**Source:** locumtenens.com (2026-07-09)
**Applicability:** any scraper using `og:url` / Open Graph as the canonical URL source.

On some sites the `og:url` meta tag is misconfigured (or intentionally set) to the site
homepage (e.g. `https://www.locumtenens.com`) instead of the actual page URL. Using it
blindly makes every extracted item share the same (wrong) URL.

**Guard:** before trusting `og:url`, compare it to the site root and to `page.url` /
`<link rel="canonical">`. If `og:url` equals the site root (or differs from the request URL
by only the host), discard it and fall back to `page.url` / canonical / the discovered href.

```python
og = soup.select_one('meta[property="og:url"]')["content"] if soup.select_one('meta[property="og:url"]') else ""
site_root = urlparse(url).scheme + "://" + urlparse(url).netloc
item_url = page_url_or_canonical  # the request URL / <link rel=canonical>
if og and og.rstrip("/") != site_root.rstrip("/"):
    item_url = og  # og:url looks legit (points at a real page, not the root)
```

## Learned: JobPosting JSON-LD description may contain non-job content
**Source:** locumtenens.com (2026-07-16)
**Applicability:** any job board where JSON-LD `JobPosting.description` includes marketing, recruiter contact, or summary sections that aren't part of the actual job description.

Some job boards dump the entire page content into the JSON-LD `description` field as HTML, including:
1. **Marketing/promotional content** (e.g., "Why choose [Company]?")
2. **Recruiter contact information** (name, phone, email)
3. **Summary sections** already extracted as separate fields ("This job at a glance")

Using this description verbatim pollutes the extracted description with irrelevant content.

**Guard:** After extracting `description` from JSON-LD, strip marketing and recruiter sections:

```python
import re

def clean_job_description(html_desc: str) -> str:
    """Strip marketing and recruiter content from job description HTML."""
    if not html_desc:
        return ""
    # Strip everything after known marketing/recruiter section headings
    strip_markers = [
        r"(?i)why\s+choose\s+\w+",
        r"(?i)your\s+dedicated\s+recruiter",
        r"(?i)apply\s+now",
        r"(?i)show\s+recruiter",
    ]
    cleaned = html_desc
    for marker in strip_markers:
        cleaned = re.split(marker, cleaned)[0]
    # Also strip 'This job at a glance' summary section if present at start
    cleaned = re.sub(r"(?i)at\s+a\s+glance[:\s]+[^[<]+", "", cleaned, count=1)
    return cleaned.strip()
```

**Note:** Always verify these markers against the specific site's HTML structure. Some sites may use the same headings for legitimate job description sections.

## Learned: OCC JSON-LD offers.price may be case total, not unit price
**Source:** https://www.dollartree.com (2026-07-20)
**Applicability:** Oracle Commerce Cloud (OCC) sites that sell multi-unit cases/bulk items.

On OCC sites, `JSON-LD offers.price` can be the **CASE TOTAL** price (e.g., $42 for 24 units), NOT the per-unit price. This is indicated by `offers.priceSpecification.referenceQuantity` with `Value > 1` and `unitText: "C62"` (Oracle's internal case unit code).

```json
{
  "offers": {
    "price": 42,
    "priceSpecification": {
      "price": 42,
      "priceCurrency": "USD",
      "referenceQuantity": { "Value": "24", "unitText": "C62" }
    }
  }
}
```

**Guard:** Before using JSON-LD `offers.price`, check for `priceSpecification.referenceQuantity`. If `Value > 1`, the price is a case total. Use `meta[property='product:price:amount']` instead (available on OCC sites). If absent, calculate unit price: `offers.price / int(referenceQuantity.Value)`.

This is distinct from Kibo's `priceSpecification[]` pattern (which uses `priceType` SalePrice/ListPrice) — the OCC pattern uses `referenceQuantity` to indicate bulk quantities.

See the `oracle-commerce-cloud-detection` skill for full OCC extraction patterns.

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

## Learned: GTM dataLayer as Supplementary Extraction Source
**Source:** https://www.ayahealthcare.com (2026-07-17)
**Applicability:** Job boards and ecommerce sites using Google Tag Manager (GTM) that push structured item data into the `dataLayer`.

Many sites (especially job boards and large ecommerce platforms) push structured product/job data into the GTM `dataLayer` via inline `<script>` tags. This data often contains **cleaner numeric values** than JSON-LD (e.g., `weeklyPayLow: 128502` vs. `"$128502.00 to $205608.00 per year"`) and additional fields not present in JSON-LD (e.g., specialty codes, state abbreviations, shift info).

**Detection:** Look for `dataLayer.push` in inline scripts, especially with event names like `view_item`, `view_job_details`, `product_detail_view`, etc.

**Extraction pattern:**
```python
import re
import json

def extract_datalayer_field(html: str, event: str, field: str) -> any:
    """Extract a field from a GTM dataLayer.push() call matching an event name."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or ""
        # Look for dataLayer.push with the target event
        if f"'{event}'" in text or f'"{event}"' in text:
            # Try to extract the full pushed object
            match = re.search(r"dataLayer\.push\(\s*(\{.*?\})\s*\)", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1)).get(field)
                except (json.JSONDecodeError, TypeError):
                    pass
            # Fallback: find nested object by field name
            match = re.search(r"(\{[^{}]*" + field + r"[^{}]*\})", text, re.DOTALL)
            if match:
                try:
                    return json.loads("{" + match.group(1) + "}").get(field)
                except (json.JSONDecodeError, TypeError):
                    pass
    return None

# Example: extract structured pay data
job_details = extract_datalayer_field(html, "view_job_details", "jobDetails")
if job_details:
    pay_low = job_details.get("weeklyPayLow")    # 128502 (numeric)
    pay_high = job_details.get("weeklyPayHigh")   # 205608 (numeric)
    city = job_details.get("city")                # "New Brunswick"
    state = job_details.get("stateAbbrev")        # "NJ" (abbreviation!)
    specialty = job_details.get("expertiseText") # "Labor and Delivery"
```

**Key advantages over JSON-LD:**
- **Numeric values** — no currency formatting or string parsing needed
- **State abbreviations** — `stateAbbrev: "NJ"` instead of full name `"New Jersey"`
- **Extra fields** — specialty/expertise, shift duration, start date, facility codes
- **Common event names:** `view_item`, `view_job_details`, `product_detail_view`, `view_product`

**Caveats:**
- `dataLayer.push()` regex extraction is fragile — if the pushed object contains nested braces, the `re.DOTALL` match may fail. Use the fallback nested-field regex for safety.
- Not all sites push item details to dataLayer — some only push pageview/analytics events.
- Data is supplemental — always validate against JSON-LD or CSS extraction.

## Verified Patterns

| Platform | JSON-LD Present | Price Pattern | Multiple Product Blocks | Reviews | Notes |
|----------|---------------|--------------|------------------------|--------|-------|
| SFCC/Demandware | Yes | highPrice | Yes (Product + PowerReviews) | PowerReviews | Rich data, reliable |
| Kibo/Mozu | Yes | priceSpecification[] | Yes (Product + BazaarVoice) | BazaarVoice | Non-standard offers |
| Shopify | Yes | Rare | No | None | Single Product block |
| WooCommerce | Sometimes | Rare | No | None | May use microdata instead |
| Magento | Sometimes | Rare | No | None | Variable quality |

## Learned: baseSalary.value may be a pre-formatted string range
**Source:** https://www.ayahealthcare.com/ (2026-07-17)
**Applicability:** any job board where JSON-LD `JobPosting.baseSalary.value` is a string instead of a `QuantitativeValue` / `MonetaryAmount` object.

Per schema.org, `baseSalary.value` should be a `QuantitativeValue` with numeric `minValue`/`maxValue`. Some sites violate this and set it to a pre-formatted string like `"$128,502.00 to $205,608.00 per year"`. Scrapers that blindly prepend `"$"` will produce `$$128,502.00...`.

**Guard:** Check the type of `baseSalary.value` before formatting. If it's already a string, use it directly (optionally cleaning `$` to match your output format). If it's a dict, extract `minValue`/`maxValue` as usual.

```python
base_salary = jsonld.get("baseSalary", {})
if isinstance(base_salary, dict):
    val = base_salary.get("value", "")
    if isinstance(val, dict):
        # Standard QuantitativeValue: {minValue, maxValue, unitText}
        low = val.get("minValue")
        high = val.get("maxValue")
        unit = val.get("unitText", "")
        salary = f"${low:,.2f} - ${high:,.2f} {unit}"
    elif isinstance(val, str):
        # Non-standard: pre-formatted string like "$128,502.00 to $205,608.00 per year"
        salary = val.strip()
        # Avoid double-$: if string already has $, don't prepend another
    else:
        salary = ""
```

**Also applies to:** Product `offers.price` fields that may be pre-formatted strings with currency symbols.

## Learned: Google Tag Manager dataLayer as supplementary job data source
**Source:** https://www.ayahealthcare.com/ (2026-07-17)
**Applicability:** any site using GTM that pushes structured item data to `dataLayer` (common on WordPress job boards, corporate career sites, and marketing-heavy sites).

Many sites push rich structured data via `dataLayer.push({event: "view_job_details", jobDetails: {...}})` or similar events in inline `<script>` tags. This data often contains fields **not available in JSON-LD**, such as:
- Numeric pay ranges: `weeklyPayLow`, `weeklyPayHigh`
- State abbreviations (2-letter): `stateAbbrev` (vs JSON-LD's full `addressRegion`)
- Profession/specialty codes: `professionCode`, `expertiseCode`
- Formatted display strings: `transparentPay`, `customPayShift`

**Extraction approach:**
```python
import re, json

def extract_datalayer_job(html: str) -> dict | None:
    """Extract jobDetails from dataLayer.push() in inline scripts."""
    for script in soup.find_all("script"):
        text = script.string or ""
        if "jobDetails" in text:
            # Try to extract the pushed object
            match = re.search(r"'jobDetails'\s*:\s*(\{[^}]+\})", text)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
    return None
```

**Key advantage over JSON-LD for jobs:** dataLayer often provides **state abbreviations** (`NJ`) instead of full state names (`New Jersey`), and **numeric pay values** instead of formatted strings — both more useful for downstream processing.

**Note:** dataLayer data is site-specific (field names vary). Always verify field names against the actual script content. Common event names: `view_item`, `view_job_details`, `product_detail_view`.

## Learned: JobPosting.identifier.value may contain title text instead of reference ID
**Source:** locumtenens.com (2026-07-19)
**Applicability:** any job board where JSON-LD `JobPosting.identifier.value` is not a proper reference/job ID.

Per schema.org, `JobPosting.identifier` should contain a unique identifier for the posting (e.g., `"ORD-208000-MD-TX"`). Some sites incorrectly populate it with the job title (e.g., `"Physician Assistant (PA) - GI Radiology"`) or other non-ID text. Using this blindly as a `job_id` field produces meaningless data.

**Guard:** After extracting `identifier.value`, validate it looks like a reference ID (contains digits, possibly with hyphens/dashes, not a full sentence). If it fails validation, fall back to a CSS-based job ID selector if available.

```python
identifier = jsonld.get("identifier", {})
if isinstance(identifier, dict):
    raw_id = str(identifier.get("value", ""))
else:
    raw_id = ""

def looks_like_ref_id(s: str) -> bool:
    """True if string looks like a job reference ID, not a title/description."""
    if not s:
        return False
    # Must contain at least one digit (reference IDs almost always have numbers)
    if not any(c.isdigit() for c in s):
        return False
    # If it's very long (>60 chars) and contains many spaces, it's likely a title
    if len(s) > 60 and s.count(" ") > 3:
        return False
    return True

job_id = raw_id if looks_like_ref_id(raw_id) else ""

# Fallback: CSS selector for job ID display
if not job_id:
    jid_el = soup.select_one(".job-details-top-text.text-end")  # site-specific
    if jid_el:
        job_id = jid_el.get_text(strip=True).replace("Job ID:", "").strip()
```

**Observed bad values:** `"Physician Assistant (PA) - GI Radiology"`, `"Physician"`
**Observed good values:** `"ORD-208000-MD-TX"`, `"7008"`, `"12345"`

## Learned: Malformed JSON-LD with trailing semicolons causes silent parse failure
**Source:** https://www.adameve.com (2026-07-20)
**Applicability:** any site that emits JSON-LD blocks with trailing semicolons or other syntax errors that cause `json.loads()` to fail silently.

Some sites (particularly custom ASP.NET platforms) emit JSON-LD `<script type="application/ld+json">` blocks with a **trailing semicolon** after the closing brace: `{ "@type": "Product", ... };`. Python's `json.loads()` raises `JSONDecodeError` on this, causing the block to be silently skipped in the standard `parse_json_ld()` function. This is especially dangerous because the block *looks* valid on inspection — the semicolon is easy to miss.

**Observed on:** Adam & Eve (custom ASP.NET) — Product JSON-LD block at index 1 of 4 blocks. The BreadcrumbList block at index 0 was well-formed, masking the issue during manual inspection.

**Guard:** Always strip trailing semicolons (and whitespace) from JSON-LD raw text before parsing:

```python
def parse_json_ld_robust(html: str) -> list[dict]:
    """Parse JSON-LD blocks, handling common malformations."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string
        if not raw:
            continue
        try:
            # Strip trailing semicolons that cause JSON parse failure
            cleaned = raw.strip()
            while cleaned.endswith(";"):
                cleaned = cleaned[:-1].rstrip()
            data = json.loads(cleaned)
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                results.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return results
```

**Note:** This replaces the basic `parse_json_ld()` in the Extraction Function section above for maximum robustness.

## Learned: JSON-LD with PascalCase keys instead of schema.org camelCase
**Source:** https://www.adameve.com (2026-07-20)
**Applicability:** any site that emits JSON-LD using non-standard key casing (observed on custom ASP.NET / .NET platforms).

Some sites emit JSON-LD Product blocks using **PascalCase** keys (`Name`, `Mpn`, `Description`, `Image`, `Brand`, `Offers`, `Price`, `PriceCurrency`, `Availability`) instead of the standard schema.org camelCase (`name`, `mpn`, `description`, `image`, `brand`, `offers`, `price`, `priceCurrency`, `availability`). Scrapers that rely on lowercase key access (e.g., `product.get("name")`) will silently return `None` for all fields, even though the data is present.

This pattern is common on sites built with .NET/ASP.NET where developers use PascalCase by convention and it leaks into the JSON-LD output.

**Observed mapping:**
| PascalCase (non-standard) | camelCase (standard schema.org) |
|---------------------------|-------------------------------|
| `Name` | `name` |
| `Mpn` | `mpn` |
| `Description` | `description` |
| `Image` | `image` |
| `Brand` | `brand` |
| `Offers` | `offers` |
| `Price` | `price` |
| `PriceCurrency` | `priceCurrency` |
| `Availability` | `availability` |
| `RatingValue` | `ratingValue` |
| `RatingCount` | `ratingCount` |
| `AggregateRating` | `aggregateRating` |
| `ReviewCount` | `reviewCount` |
| `HighPrice` | `highPrice` |
| `LowPrice` | `lowPrice` |

**Guard:** Normalize keys to lowercase after parsing, or use case-insensitive access:

```python
PASCAL_TO_CAMEL = {
    "Name": "name", "Mpn": "mpn", "Description": "description",
    "Image": "image", "Brand": "brand", "Offers": "offers",
    "Price": "price", "PriceCurrency": "priceCurrency",
    "Availability": "availability", "Url": "url",
    "HighPrice": "highPrice", "LowPrice": "lowPrice",
    "RatingValue": "ratingValue", "RatingCount": "ratingCount",
    "AggregateRating": "aggregateRating", "ReviewCount": "reviewCount",
}

def normalize_jsonld_keys(obj):
    """Recursively normalize PascalCase JSON-LD keys to camelCase."""
    if not isinstance(obj, dict):
        return obj
    normalized = {}
    for key, value in obj.items():
        lower_key = PASCAL_TO_CAMEL.get(key, key)
        if isinstance(value, dict):
            normalized[lower_key] = normalize_jsonld_keys(value)
        elif isinstance(value, list):
            normalized[lower_key] = [
                normalize_jsonld_keys(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            normalized[lower_key] = value
    return normalized

# Usage: product = normalize_jsonld_keys(raw_product_block)
```

**Detection heuristic:** If `json.loads()` succeeds but `block.get("name")` returns `None` while `block.get("Name")` works, the block uses PascalCase.

## When NOT to Use

- No `<script type="application/ld+json">` on the page
- JSON-LD is empty or malformed
- Page is heavily JavaScript-rendered and JSON-LD loads after initial HTML (use Playwright)
- Structured data doesn't match visible product info (site may inject fake SEO data)

Base directory for this skill: file:///mnt/d/John/u-ecom-scraper/.opencode/skills/jsonld-extraction
