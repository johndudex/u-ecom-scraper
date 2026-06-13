---
name: playwright-navigation
description: Playwright MCP browser navigation patterns for ecommerce scraping. Covers page navigation, accessibility snapshots, scrolling, clicking, network monitoring, and page evaluation.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer, product-analyzer
  workflow: scraping
---

# Playwright MCP Navigation Patterns

## What I Do

Provide battle-tested Playwright MCP usage patterns for navigating and analyzing ecommerce websites. Covers all common scenarios encountered during site and product analysis.

## When to Use Me

Use this when:
- Site Analyzer or Product Analyzer agents need to navigate websites
- You need to interact with dynamic page elements
- You need to monitor network requests for API detection
- You need to capture page structure for analysis

## Available Playwright MCP Tools

| Tool | Purpose |
|------|---------|
| `playwright_browser_navigate` | Go to a URL |
| `playwright_browser_snapshot` | Get accessibility tree snapshot |
| `playwright_browser_take_screenshot` | Capture visual screenshot |
| `playwright_browser_evaluate` | Run JavaScript on page |
| `playwright_browser_click` | Click an element |
| `playwright_browser_type` | Type into input fields |
| `playwright_browser_hover` | Hover over elements |
| `playwright_browser_wait_for` | Wait for text/element/time |
| `playwright_browser_network_requests` | Monitor network traffic |
| `playwright_browser_press_key` | Simulate keyboard input |
| `playwright_browser_select_option` | Select dropdown options |
| `playwright_browser_tabs` | Manage browser tabs |
| `playwright_browser_drag` | Drag and drop |
| `playwright_browser_fill_form` | Fill form fields |
| `playwright_browser_close` | Close page |

## Core Navigation Patterns

### Navigate to a Page

```
playwright_browser_navigate → {url}
playwright_browser_wait_for → time=3
```

**Always wait after navigation** for dynamic content to load.

### Get Page Structure (Most Important)

The **accessibility snapshot** is the primary analysis tool:

```
playwright_browser_snapshot → full page
```

This returns the entire page structure including:
- All visible elements with roles and text
- Element hierarchy and nesting
- ARIA labels and attributes
- Interactive elements and their states
- Use `depth` parameter to limit snapshot depth if page is very large

### Execute JavaScript on Page

```
playwright_browser_evaluate → function: () => {
    return document.querySelectorAll('.product-card').length;
}
```

Use for:
- Counting elements
- Extracting data attributes
- Parsing JSON from script tags
- Checking DOM state

## Ecommerce-Specific Patterns

### Find Product Listing

```
1. Navigate to homepage
2. Take accessibility snapshot
3. Find navigation links (role="navigation")
4. Look for: "Shop", "Products", "Collections", "Store"
5. Click the link
6. Wait for product listing to load
7. Take snapshot of product listing
```

### Handle Infinite Scroll

```
1. Take snapshot to count current products
2. Scroll to bottom:
   playwright_browser_evaluate → () => {
       window.scrollTo(0, document.body.scrollHeight);
   }
3. Wait for new content:
   playwright_browser_wait_for → time=2
4. Take snapshot again
5. Compare product count
6. Repeat until count stops increasing
```

### Handle Load More Button

```
1. Take snapshot to find "Load More" button
2. Click the button
3. Wait for new content
4. Repeat until button is disabled or hidden
```

### Handle Pagination (URL-based)

```
1. Navigate to page 1
2. Extract products
3. Navigate to page 2
4. Repeat until no more pages
```

### Monitor Network Requests for API Detection

```
1. Navigate to product listing page
2. Use playwright_browser_network_requests → filter="/api/.*product"
3. Look for JSON responses containing product data
4. Note the API endpoint URL and parameters
```

### Extract Product URLs from Listing Page

```
playwright_browser_evaluate → function: () => {
    const links = document.querySelectorAll('a[href*="/products/"]');
    const unique = new Set();
    links.forEach(a => {
        let href = a.getAttribute('href');
        if (href) {
            if (!href.startsWith('http')) {
                href = window.location.origin + href;
            }
            unique.add(href);
        }
    });
    return Array.from(unique);
}
```

### Click Through to Product Page

```
1. Take snapshot to find product link
2. Get the target element reference
3. Click the product link
4. Wait for product page to load
5. Take product page snapshot
```

## Product Page Analysis Patterns

### Extract Structured Data (JSON-LD)

```
playwright_browser_evaluate → function: () => {
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    return Array.from(scripts).map(s => {
        try { return JSON.parse(s.textContent); }
        catch { return null; }
    }).filter(Boolean);
}
```

### Extract Open Graph Data

```
playwright_browser_evaluate → function: () => {
    const meta = {};
    meta.title = document.querySelector('meta[property="og:title"]')?.content;
    meta.image = document.querySelector('meta[property="og:image"]')?.content;
    meta.description = document.querySelector('meta[property="og:description"]')?.content;
    meta.price = document.querySelector('meta[property="og:price:amount"]')?.content;
    meta.currency = document.querySelector('meta[property="og:price:currency"]')?.content;
    return meta;
}
```

### Extract Variant Options

```
playwright_browser_evaluate → function: () => {
    const variants = {};
    // Shopify variant data
    const productJson = document.querySelector('[data-product]');
    if (productJson) {
        variants.shopify = JSON.parse(productJson.textContent);
    }
    // Generic variant selectors
    const selects = document.querySelectorAll('select[name*="variant"], select[name*="option"]');
    variants.selects = Array.from(selects).map(s => ({
        name: s.name,
        options: Array.from(s.options).map(o => o.value)
    }));
    // Swatch buttons
    const swatches = document.querySelectorAll('[class*="swatch"], [class*="variant"]');
    variants.swatches = Array.from(swatches).map(s => s.textContent.trim());
    return variants;
}
```

### Expand Tabs/Accordions for Hidden Content

```
1. Take snapshot to find tab/accordion buttons
2. Look for: "Description", "Specifications", "Details", "Reviews"
3. Click each tab/accordion
4. Wait for content to appear
5. Take snapshot of expanded content
```

### Detect Anti-Bot Challenges

```
playwright_browser_navigate → {site_url}
playwright_browser_wait_for → time=5
playwright_browser_snapshot → check page content

Look for:
- "Just a moment..." text → Cloudflare
- "Checking your browser" → Generic bot detection
- "Access denied" or 403 → Blocked
- CAPTCHA iframe → CAPTCHA challenge
- Blank page with minimal HTML → JavaScript challenge
```

## Best Practices

1. **Always wait after navigation** - Dynamic content needs time to load
2. **Prefer snapshots over screenshots** - Snapshots give structured data
3. **Use evaluate for data extraction** - Faster than parsing HTML
4. **Monitor network for APIs** - Many sites use hidden APIs
5. **Handle popups and cookie banners** - Dismiss before analysis
6. **Don't click too fast** - Trigger anti-bot if clicking rapidly
7. **Save snapshots to files** - For offline analysis later

## Common Issues

1. **Page too large for snapshot**: Use `depth` parameter to limit tree depth
2. **Dynamic content not loading**: Increase wait time, check for loading spinners
3. **Popup blocking analysis**: Dismiss cookie consent, newsletter popups first
4. **Redirected to challenge page**: Anti-bot detected, note and report
5. **Elements not found**: Page structure different than expected, re-snapshot
