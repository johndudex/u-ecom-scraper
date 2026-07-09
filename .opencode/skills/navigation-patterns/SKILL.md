---
description: Generic ecommerce navigation patterns for discovering product URLs. Covers mega menus, button-based nav, CSR grids, search URL patterns, pagination types, and product card selectors across SSR and SPA platforms.
---

# Navigation Patterns Skill

This skill documents recurring navigation structures across ecommerce sites and
how to discover product URLs generically. Use this when synthesizing
`navigation_analysis.json` from raw exploration findings.

## Site Rendering Types

### Server-Side Rendered (SSR)
- **Indicators**: Full HTML in initial response, product cards in page source
- **Examples**: ASP.NET (`.aspx`), PHP, Shopify, SFCC, Magento
- **Strategy**: `requests` + BeautifulSoup works for extraction
- **Nav**: Usually `<a href>` links in `<nav>` containers

### Client-Side Rendered (CSR / SPA)
- **Indicators**: `#__next`, `#__react-root`, minimal HTML in initial response,
  Material-UI classes (`MuiGrid`, `MuiCard`), empty `<div id="app">` shell
- **Examples**: Next.js, React, Vue, Nuxt
- **Strategy**: Playwright required — wait for product selectors to appear
- **Nav**: Often `<button>` triggers (no `href`), client-side routing

### Anti-Bot Protected
- **Indicators**: Cloudflare (`#challenge-running`), Akamai, PerimeterX
- **Strategy**: Playwright with stealth mode required, rate limit aggressively

## Navigation Structure Patterns

### Pattern 1: Traditional Link Nav (SSR)
```
<nav>
  <ul>
    <li><a href="/category/electronics">Electronics</a></li>
    <li><a href="/category/books">Books</a></li>
  </ul>
</nav>
```
**Sites**: Most PHP/ASP.NET sites, Shopify, WooCommerce
**Extraction**: `document.querySelectorAll('nav a[href]')`

### Pattern 2: Mega Menu with Hidden Panels (SSR)
```
<nav class="mega-nav">
  <li><button class="mega-nav__trigger">Categories</button>
    <div class="mega-nav__panel" style="display:none">
      <a href="/cat/shoes">Shoes</a>
      <a href="/cat/shirts">Shirts</a>
    </div>
  </li>
</nav>
```
**Sites**: adameve.com, Nike, large retailers
**Extraction**: Unhide panels first (`style.display = 'block'`), then extract links
**Category URL patterns**: `-ch-{id}.aspx`, `/category/{slug}`, `/c/{id}`

### Pattern 3: Button-Based Nav (CSR/SPA)
```
<nav>
  <button aria-haspopup="true">Fiction</button>
  <button aria-haspopup="true">Nonfiction</button>
</nav>
```
**Sites**: bookoutlet.com, modern React/Next.js sites, Material-UI
**Extraction**: No `href` — must click to reveal dropdown or infer URLs
**URL inference**: `/{slug}`, `/category/{slug}`, `/collections/{slug}`
**Example**: "Fiction" button → `/fiction`, "Kids" button → `/kids`

### Pattern 4: Search-Centric
```
<input type="search" placeholder="Search products">
```
**Sites**: Sites with no visible category nav, search-first UX
**Extraction**: Find search input, construct search URL

**⚠ Capped search results:** Many sites cap search-result pagination well below the
true total (e.g. a search shows "88 items" but only serves 2 pages / ~46 products,
then returns empty). If the page shows a total count greater than what pagination
serves, **do not rely on search for full discovery** — instead use the site's
**category/listing pages** (e.g. `/mens-...`, `/womens-...`), which usually paginate
to the full set. Always compare discovered count vs. the displayed total and switch
to category discovery if search is incomplete. This is generic — not site-specific.

## Search URL Patterns

### Query-Parameter Search (most common)
| Pattern | Example | Sites |
|---------|---------|-------|
| `/search?q={query}` | `/search?q=shoes` | Generic, Shopify |
| `/search?search={query}` | `/search?search=shoes` | ASP.NET |
| `/search.aspx?search={query}` | `/search.aspx?search=lingerie` | adameve.com |
| `/search.asp?search={query}` | `/search.asp?search=lingerie` | Legacy ASP |

### Path-Based Search (SPA sites)
| Pattern | Example | Sites |
|---------|---------|-------|
| `/search/{slug}` | `/search/harry-potter` | bookoutlet.com, Next.js |
| `/search/{query}` | `/search/shoes` | React SPAs |

### JS-Driven Search (no URL)
- Search input has no enclosing `<form action>`
- Submission handled by JavaScript (HawkSearch, Constructor.io, Algolia)
- **Strategy**: Try URL patterns first, fall back to typing + Enter

## Product Card Patterns

### High-Confidence Selectors (use these first)
```css
[data-cy="product-grid-item"]      /* adameve.com, Cypress-tested sites */
[data-product-id]                  /* Generic data attribute */
[data-pid]                         /* SFCC (Salesforce Commerce Cloud) */
[data-sku]                         /* SKU-based */
.MuiCard-root                      /* Material-UI cards (bookoutlet.com) */
[class*="ProductCard"]             /* React component naming */
[class*="product-card"]            /* Generic class pattern */
```

**WARNING**: `[data-productid]` (camelCase) can match rating widgets
(e.g. SFCC's `.TTteaser[data-productid]`) instead of product cards.
Always prefer `[data-pid]` for SFCC sites. Use `div.product[data-pid]`
for maximum specificity.

### Platform-Specific Card Classes
| Platform | Card Selector | Product URL Pattern |
|----------|--------------|-------------------|
| adameve.com | `[data-cy="product-grid-item"] .ae-plp-card` | `/sp-{slug}-{id}.aspx` |
| bookoutlet.com | `.MuiCard-root` | `/book/{title}/{author}/{ISBN}B` |
| Shopify | `.product-card` | `/products/{handle}` |
| Amazon | `[data-component-type="s-search-result"]` | `/dp/{ASIN}` |
| SFCC | `div.product[data-pid]` | `/p/{slug}/{PID}.html` |

### Data Attributes on Cards
Product cards often embed rich data in attributes:
- `data-pid` — SFCC product ID (e.g. `G4DLK`)
- `data-sku` / `data-product-id` — product identifier
- `data-brand` — brand name
- `data-price` — current price
- `data-productname` — full product name
- `data-ga4` / `data-gtm` — JSON blob with all product data (SFCC GTM)
- `data-impression` — JSON blob with all product data (GTM)

### Pagination Patterns

### Load More Button
```html
<a id="load-more-component" href="/category?pnum=2">Load More</a>
<button class="load-more">Show More</button>
```
**Detection**: `#load-more-component`, `button[class*="load-more"]`, text match "Show More"
**URL param**: `?pnum=2`, `?page=2`, `?pg=2`
**Max pages**: Often in `input[name="page-count"]` or `#products_total`

### Page Numbers
```html
<div class="pagination">
  <a href="/category?page=1">1</a>
  <a href="/category?page=2">2</a>
  <a rel="next" href="/category?page=2">Next →</a>
</div>
```
**Detection**: `.pagination a`, `a[rel="next"]`

### Infinite Scroll
- No visible pagination controls
- Products load on scroll via IntersectionObserver
- **Strategy**: Scroll down repeatedly, collect new product links

### SFCC Offset Pagination
```
/search?cgid=category&sz=24&start=0   ← page 1
/search?cgid=category&sz=24&start=24  ← page 2
/search?cgid=category&sz=24&start=48  ← page 3
```
- **Detection**: `<link rel="next">` in `<head>`, `start` and `sz` URL params
- **Walk**: increment `start` by `sz` until tile count < sz or `rel="next"` absent

## Multi-Category Fallback Strategy

Not all category pages have product grids. Some are SEO landing pages with
carousels. The navigation agent should try multiple categories in priority
order until one yields real products:

1. Criteria-matching categories (text/URL contains search keywords)
2. URL-pattern categories (`-ch-`, `/category/`, `/collections/`, `/shop/`)
3. Short-path categories (`/{slug}` — 1 path segment)
4. Common listing pages (`/books`, `/browse`, `/shop-all`, `/products`)

## Discovery Method Selection

When choosing `discovery_method` for `navigation_analysis.json`:

1. **`search`** — if the user provided search criteria AND the site has a working
   search URL pattern. Best for targeted scraping.
2. **`category`** — if the site has category links and the user wants broad
   product coverage. Best for full-catalog scraping.
3. **`url_pattern`** — fallback when neither search nor categories work well.
   Use detected URL patterns to construct product URLs.

## Platform Quick Reference

| Platform | Nav Type | Search URL | Product URL | Pagination |
|----------|----------|-----------|-------------|------------|
| ASP.NET | Link mega menu | `/search.aspx?search=q` | `/sp-{slug}-{id}.aspx` | Load More (`?pnum=`) |
| Next.js/MUI | Button nav | `/search/{slug}` | `/book/{slug}/{id}B` | Show More |
| Shopify | Link nav | `/search?q=q` | `/products/{handle}` | Page numbers |
| Shopify+Fredhopper | Link mega menu | `/search?q=q` | `/products/{handle}` | Numbered buttons |
| Amazon | Link nav | `/s?k=q` | `/dp/{ASIN}` | Page numbers |
| SFCC | Bootstrap mega menu | `/search?q=q` | `/p/{slug}/{PID}.html` | Offset (`start`/`sz`) |
| Centra+Next.js | Link nav (locale-prefixed) | `/{locale}/search?q=q` | `/{locale}/product/{slug}` | Infinite scroll |
| Centra+SearchSpring | Link nav (locale-prefixed) | `/{locale}/search?q=q` | `/{locale}/product/{slug}` | CSR (SearchSpring API) |

## Sites Successfully Tested

| Site | Platform | Discovery | Products Found | Key Challenge |
|------|----------|-----------|----------------|---------------|
| adameve.com | ASP.NET (SSR) | Search (`/search.aspx?search=`) | 30 | Mega menu hidden panels |
| bookoutlet.com | Next.js+MUI (CSR) | Category fallback | 18 | Button nav, Cloudflare on /search/ |
| barbequesgalore.com.au | SFCC (SSR) | Category fallback | 20 | `data-pid` vs `data-productid` (rating widget) |
| birdsnest.com.au | Shopify+Fredhopper (CSR) | Search (`/search?q=`) | 30 | Fredhopper numbered pagination detection |
| aretrotale.com | Centra+Next.js+SearchSpring (CSR) | Category fallback | 30 | Cookie consent, CSS garbage in text, locale prefix |

## Learned: Soft-404 Detection for Expired/Filled Job Postings
**Source:** https://www.locumtenens.com (2026-07-09)
**Applicability:** Any job board scraper where job postings can expire or get filled while URLs remain valid (HTTP 200)

Job board sites commonly have postings that expire or get filled, but the URL still returns
HTTP 200 with a "sorry, this job is no longer available" page. Scrapers that don't detect
this will extract near-empty items with misleading titles.

**Detection approach:**
```python
# Check h1 text and page title against expiration phrases
h1_text = soup.find("h1").get_text(strip=True).lower() if soup.find("h1") else ""
page_title = soup.title.get_text(strip=True).lower() if soup.title else ""
combined = f"{page_title} {h1_text}"

soft404_phrases = [
    "not found", "page not found", "no longer available",
    "has been filled", "position has been filled",
    "job no longer available", "this job is no longer",
    "unavailable", "discontinued",
]
for phrase in soft404_phrases:
    if phrase in combined:
        item["remarks"] = f"Soft 404: {phrase}"
        return item  # skip extraction
```

**Redirect detection:**
Also check if the final URL (after redirects) no longer matches the job URL pattern:
```python
final_url = resp.url or url
if final_url.rstrip("/") != url.rstrip("/"):
    final_path = urlparse(final_url).path
    if not re.search(r"job-\d+|/job/", final_path):
        # Redirected to homepage, search page, or non-job page
        if len(final_path) < 10 or "/search" in final_path.lower():
            item["remarks"] = f"Soft 404: redirected to {final_url[:100]}"
            return item
```

**Key tips:**
- Use specific phrases only — avoid overly broad words like "error" that appear in normal content.
- Always check both `<h1>` and `<title>` since expired-page markup varies across sites.
- For job boards with `og:url` issues (returns site root), prefer the redirect detection method.
- Mark the item with a `remarks` field rather than deleting it — preserves audit trail.

## Cookie Consent / GDPR Dialogs

Many sites (especially EU) show a cookie consent dialog that blocks the page
content. The navigation agent must auto-dismiss these before extracting nav data.

**Detection**: Look for buttons with text matching:
`"allow all"`, `"accept all"`, `"accept"`, `"i agree"`, `"agree"`, `"got it"`,
`"ok"`, `"continue"`, `"yes"`, `"allow"`, `"consent"`

**Strategy**:
```javascript
// Click the first visible consent button
const btns = document.querySelectorAll('button, a[role="button"]');
for (const b of btns) {
    const t = b.textContent.trim().toLowerCase();
    if (consentTexts.some(ct => t === ct || t.startsWith(ct)) && b.offsetParent !== null) {
        b.click();
        break;
    }
}
// Wait 3s for dialog to close, then extract nav
```

**Sites observed**: aretrotale.com (OneTrust), most EU ecommerce sites

## Locale-Prefixed URLs

Some sites use locale prefixes in all URLs that affect search URL construction:
```
/en/category/bags        (English)
/sv/category/vaskor      (Swedish — note: category slugs are localized too!)
/de/category/taschen     (German)
/en-row/category/bags    (English - Rest of World)
/en-us/category/bags     (English - US)
```

**Detection**: Check `window.location.pathname` after homepage redirect:
```javascript
const match = path.match(/^\/([a-z]{2}(?:-[a-z]{2,4})?)(?:\/|$)/i);
```

**Impact**: Search URLs must include the locale prefix:
- `/en/search?q=dress` (not `/search?q=dress`)
- Listing candidates: `/en/books`, `/en/products`

**Sites observed**: aretrotale.com (Centra), some SFCC international sites

## CSS Garbage in Product Text (Sitegainer)

**Problem**: Sites using Sitegainer inject `<style>` tags INSIDE `<a>` elements
for scoped CSS. This pollutes `element.textContent` with CSS rules:
```
.feeabecbadfa { border-width: 1px;border-color: #edeae5;border-radius: 6px;...
```

**Solution**: Use `element.innerText` instead of `element.textContent`.
`innerText` excludes content from `<style>` and `<script>` tags, returning only
visible text.

**Sites observed**: aretrotale.com (24 `<style>` tags per product card!)

## Content Wait for CSR Sites

Client-rendered product grids need polling before extraction:

```javascript
// Poll for product card selectors with timeout (12-20 seconds)
const selectors = [
    '[data-cy="product-grid-item"]', '[data-product-id]', '[data-pid]',
    '.product-tile', '.product-card', '.MuiCard-root',
    'a[href*="/product/"]', 'a[href*="/book/"]', 'a[href*="/p/"]',
];
// Check every 1.5s until 3+ elements match any selector
```

**Sites needing this**: birdsnest.com (Fredhopper), aretrotale.com (SearchSpring),
bookoutlet.com (Next.js), any React/Vue SPA

## Anti-Bot Considerations

- **Cloudflare**: Watch for `#challenge-running`, `#challenge-form`. Playwright
  with stealth is required. Load `anti-bot-handling` skill for details.
- **Cloudflare on Search Routes**: Some sites (bookoutlet.com) return HTTP 403
  specifically on `/search/` routes even in browser. Fall back to category browsing.
- **HawkSearch / Constructor.io / Algolia**: These search platforms can be
  **either server-side or client-side rendered** depending on the site's
  implementation. Do NOT assume they are API-driven/CSR. Always verify using
  the SSR/CSR detection method below before choosing a scraping strategy.
  Look for `data-cnstrc-*` attributes (Constructor.io), HawkSearch scripts,
  or Algolia API calls.
- **Rate Limiting**: Always use 2+ second delays between page loads.

## Learned: POST-to-Session-ID Search Results (ASP.NET MVC Job Boards)
**Source:** https://www.locumtenens.com (2026-07-01)
**Applicability:** ASP.NET MVC job boards and similar server-rendered platforms where search uses POST forms that return session-based result URLs

Some ASP.NET MVC sites (especially job boards) use POST-based search forms that
generate a server-side search session. The form POST returns a redirect or page
with a session ID parameter (e.g., `?sId=70123291`). All subsequent pagination
references that session ID to maintain search state.

**Key characteristics:**
- Homepage may have a simple keyword search (`input[name="Keywords"]`, GET method)
- A dedicated "QuickSearch" or "AdvancedSearch" page uses POST method with multiple
  `<select>` dropdowns (Disciplines, Specialties, Locations, etc.)
- Results page URL contains a server-generated session ID: `/Resources/JobSearch/SearchResults?sId={id}`
- Pagination uses a page param combined with the session: `?sId={id}&pgNum=2`
- Advanced filters (date range via `JobAge`, etc.) available via a separate form on the results page

**Strategy for scraping:**
```javascript
// 1. Submit the POST form (QuickSearch or AdvancedSearch) to get results with sId
// 2. Extract sId from the resulting URL
const sId = new URL(pageUrl).searchParams.get('sId');
// 3. Paginate by incrementing pgNum while keeping sId
const nextPageUrl = `/Resources/JobSearch/SearchResults?sId=${sId}&pgNum=${pageNum}`;
```

**Extraction mechanics (verified 2026-07-06):**
- Results are **server-rendered HTML** on the SearchResults page — no AJAX wait needed.
- Item links are SSR anchors matching `a[href*="/job-"]`, shaped `/{specialty}-jobs/{role}/{state}/job-{id}`.
- **Required-field gotcha:** these forms often gate submit on client-side validation (e.g. a
  `FormValidation.full.min.js` rule that requires a "Specialty" `<select>`). Fill EVERY select + click
  the form's OWN `<input type=submit>` inside `<form>` (not decorative submit-styled buttons outside
  the form). Drive the form per-specialty to enumerate the catalog.
- **Output filter:** job items have company/location, NOT a price — a price-only output filter would
  delete them. Filter on title + a content-type core field (see code-writer.md).

**Detection signals:**
- `<form method="post">` with `<select>` dropdowns for category/specialty filters
- Results URL contains an opaque `sId` or `sid` parameter (server-generated, not user-supplied)
- Pagination links contain the same `sId` value across pages
- Item URLs follow SEO-friendly patterns like `/{specialty}-jobs/{role}/{state}/job-{id}`

**Note:** The existing ASP.NET entry in the Platform Quick Reference covers
`/search.aspx?search=q` GET-based search. This POST-to-session pattern is a
distinct variant common on ASP.NET MVC job boards.

## Search Platform SSR/CSR Verification

**CRITICAL**: When a search platform is detected (HawkSearch, SearchSpring,
Fredhopper, Algolia, Constructor.io), you MUST verify whether product data is
server-side rendered (SSR) or client-side rendered (CSR) before choosing a
scraping strategy. The rendering mode varies by site, not by platform.

### Verification Method

1. Fetch the category/search page URL with **raw HTTP** (`web_fetch` or `requests`)
2. Search the raw HTML for **product card selectors**:
   ```css
   a[href*="/product/"], a[href*="/sp-"], a[href*="/p/"],
   [data-pid], [data-product-id], [data-cy="product-grid-item"],
   .product-card, .product-tile, .product-item,
   [class*="product-card"], [class*="product-tile"]
   ```
3. **Count matches** in the raw HTML (before any JavaScript runs)

### Decision Matrix

| Raw HTML has product links? | Rendering | Strategy |
|-----------------------------|-----------|----------|
| Yes (3+ links) | **SSR** | `http_requests` for both phases — no browser needed |
| No (0 links) | **CSR** | Playwright needed for Phase 1 (navigation) |

### Why This Matters

Choosing Playwright when HTTP would work adds:
- Slower execution (browser startup, page rendering)
- Fragility (browser crashes, timeouts)
- Resource cost (browser_service container)
- Deployment complexity

**Example**: adameve.com uses HawkSearch but renders product cards server-side.
A raw HTTP request returns 36 product links in the initial HTML. Despite the
search platform being present, `http_requests` works perfectly for both
navigation and extraction phases.

**Counter-example**: birdsnest.com uses Fredhopper and renders product cards
client-side. The raw HTML contains zero product links. Playwright is required
to wait for Fredhopper to render the product grid.

### When to Verify

Always verify when any of these are detected:
- `hawkSearch`, `hawksearch` in page source
- `window.fredhopper`, `ss-merch-product-*` IDs
- `_searchspringTracking`, `SearchSpringResponseTracking`
- `data-cnstrc-*` attributes (Constructor.io)
- `algolia` search scripts
- Any third-party search/merchandising platform

Record the result in `navigation_analysis.json` as:
```json
{
  "rendering_verified": "ssr|csr",
  "raw_html_product_link_count": 36,
  "verification_method": "web_fetch on category URL"
}
```

## Search Platform Patterns

### Fredhopper (Crownpeak)
- **Used by**: birdsnest.com.au (Shopify + Fredhopper overlay)
- **Signs**: `window.fredhopper`, `ss-merch-product-{n}` IDs, `ss-facet-*` classes
- **Pagination**: Numbered buttons with hashed CSS classes, or `window.liquidCustom.pagination`
- **WARNING**: `.ss-facet-show-more` is facet expansion, NOT pagination — exclude it

### SearchSpring
- **Used by**: aretrotale.com (Centra + SearchSpring)
- **Signs**: `_searchspringTracking`, `SearchSpringResponseTracking`
- **Rendering**: Can be SSR or CSR depending on implementation — always verify
- **API**: `https://modern.search.spring.io/v1/?siteId={ID}&q={query}&page={n}`

### Shopify JSON API (Open)
- **Endpoint**: `/collections/{handle}/products.json?limit=250&page={n}`
- **Single product**: `/products/{handle}.json`
- **Collections list**: `/collections.json?limit=250`
- **Advantage**: No browser needed — pure HTTP, returns full product data
- **Limitation**: Caps at 1000 results via `page=` param

## Facet vs Pagination Detection

Some filter/facet UI uses "Show More" buttons that look like pagination but
actually expand facet options. Exclude these:

```javascript
// Exclude facet show-more from pagination detection
if (loadMoreBtn && loadMoreBtn.closest(
    '.ss-facets, .ss-facet-group, [class*="facet" i], [class*="filter" i]'
)) {
    loadMoreBtn = null;  // It's a facet expander, not pagination
}
```

## Learned: searchTerm Search Parameter (Headless SFCC / Next.js)
**Source:** https://www.calvinklein.co.uk/ (2026-07-04)
**Applicability:** Headless SFCC sites with Next.js storefronts (PVH Corp brands, and potentially other decoupled SFCC implementations)

Some sites use `searchTerm` (camelCase) instead of the standard `q` parameter for search:
- URL: `/search?searchTerm={query}` (e.g., `/search?searchTerm=watches`)
- Search input ID: `#searchTerm`
- These are typically headless SFCC + Next.js sites where the frontend decouples from standard SFCC URL conventions

**Detection:** Look for `#searchTerm` input ID or `searchTerm` in URL query params after search submission.

**Add to the Search URL Patterns table:**

| Pattern | Example | Sites |
|---------|---------|-------|
| `/search?searchTerm={query}` | `/search?searchTerm=watches` | Headless SFCC/Next.js (PVH Corp) |

**Note:** This is distinct from the standard SFCC `/search?q={query}` and the ASP.NET `/search?search={query}` patterns already documented.

## Learned: GET-Based Multi-Select Filter Forms (Job Boards)
**Source:** https://www.vistastaff.com (2026-07-06)
**Applicability:** Healthcare staffing, recruitment, and other job board sites that use GET-based filter forms with multiple `<select>` dropdowns instead of text search

Many job board sites use **GET-form filter interfaces** rather than traditional text search. A `<form method="get" action="/job-board/">` contains multiple `<select>` dropdowns (e.g., `profession`, `specialty`, `state`) that construct filter URLs via query parameters. This is distinct from both the POST-to-session pattern (locumtenens.com) and text-based `?q=` search patterns.

**Key characteristics:**
- `<form method="get">` with `<select name="profession">`, `<select name="specialty">`, `<select name="state">` dropdowns
- Options use **numeric IDs** as values (e.g., `<option value="361">Physician</option>`)
- Filter URL: `/job-board/?profession=361&specialty=all&type=&state=all`
- All combinations are directly URL-constructable — no session ID needed
- Individual item URLs follow descriptive SEO slugs: `/job-board/{role}-{specialty}-in-{state}-{numericId}/`
- Pagination uses a simple **next button** (no page-number params)
- **Fully SSR** — direct HTTP requests work, no browser rendering needed

**Strategy for scraping:**
```javascript
// Enumerate filters by iterating select options
// Build URLs directly via query parameters (no form submission needed)
const baseUrl = '/job-board/';
// Example: all physician jobs
const url = `${baseUrl}?profession=361&specialty=all&state=all`;
// Example: physician cardiology in Arizona
const url = `${baseUrl}?profession=361&specialty=22&state=AZ`;
// Paginate via the next button link on results page
```

**Detection signals:**
- `<form method="get">` with multiple `<select name="...">` dropdowns (NOT a text search `<input>`)
- Select option values are numeric IDs, not text slugs
- URL constructed by appending `?param=value&param=value` from select values
- No `sId` or session parameter in results URL (distinguishes from POST-to-session pattern)

**Catalog enumeration strategy:** Drive per-profession + per-specialty combinations to cover the full job catalog. For large specialty lists (100+), start with `specialty=all` per profession, then drill into specific specialties if needed.

**Note:** This is distinct from the POST-to-session pattern (locumtenens.com, ASP.NET MVC) because:
1. Uses GET method (URLs are stable and shareable)
2. No server-side session — filter params are self-contained
3. Direct HTTP construction without form submission
4. Common on WordPress-based job boards with custom post types
