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
- Resource cost (browser-service container)
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
