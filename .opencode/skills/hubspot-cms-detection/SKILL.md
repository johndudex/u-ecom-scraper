---
name: hubspot-cms-detection
description: Detect HubSpot CMS websites and leverage their server-rendered HTML, data attributes, and SPA patterns for efficient product/job/content data extraction.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer, product-analyzer
  workflow: scraping
  learned_from: https://dystaffing.com
  learned_date: 2026-07-21
---

# HubSpot CMS Detection & Extraction

## What I Do

Provide detection heuristics and extraction patterns for websites built on
HubSpot CMS. HubSpot is used by 60k+ businesses for marketing sites, job boards,
and service directories.

## Detection Markers

### URL & Asset Patterns
- Assets served from `hubfs` (HubSpot File Manager): `https://*.hubspotusercontent00.net/hubfs/`
- `hsLang` query parameter in URLs (HubSpot language selector)
- `hs-cta-*` or `_hsenc` cookies/tracking parameters

### DOM / HTML Markers
- `data-hs-row-id` attributes on elements (HubSpot module rows)
- HubSpot form widgets: `[data-form-id]`, `.hbspt-form`, `.hs-form`
- HubSpot chat widgets: `.hs-chat-widget`
- `<div class="widget-type-*">` or `<div class="hs_cos_wrapper_*">` (HubSpot modules)
- `_hstc`, `_hssrc`, `hubspotutk` cookies

### Page Source Signals
- `<script src="//js.hs-scripts.com/">` or `<script src="//js.hscta.net/">`
- HubSpot Analytics: `var _hsq = window._hsq || [];`
- HubSpot content embed: `hs-blog` or `hs-page` classes

## Scraping Characteristics

### General
- **No anti-bot protection** by default — standard Playwright or even `requests` works
- **Server-side rendered** by default — most HubSpot pages render HTML on the server
- **Some sites are SPAs** — client-side JavaScript may load content dynamically
- **No standard JSON-LD** — HubSpot does not auto-generate structured data (unlike Shopify/SFCC)

### HubSpot-Specific SPA Pattern (All-Items-on-One-Page)

Some HubSpot CMS sites (especially job boards and directories) render ALL items
into the DOM on a single page, with NO individual detail URLs and NO pagination.
Items are shown/hidden via JavaScript when users interact with the page.

**Detection signals:**
- A single listing page URL (e.g., `/job-search`, `/directory`) contains ALL items
- Each item has a unique `data-*` attribute (e.g., `data-job-id`, `data-row-id`)
- Clicking an item does NOT navigate to a new URL — it reveals content within the same page
- No `<a href>` links to individual item pages
- No pagination controls (load more, page numbers, infinite scroll)

**Extraction strategy:**
```python
# Navigate to the listing page, wait for SPA to render, then extract ALL items
ITEM_SELECTOR = '.single-job[data-job-id]'  # site-specific, look for data-* keyed elements

job_elements = page.eval_on_selector_all(
    ITEM_SELECTOR,
    """els => els.map(el => ({
        id: el.dataset.jobId,
        city: el.dataset.city,
        state: el.dataset.state,
        // extract text from child elements
        title: el.querySelector('.item-title')?.textContent?.trim(),
        description: el.querySelector('.item-desc')?.innerHTML?.trim(),
    }))"""
)
```

**Key tips:**
- Use `data-*` attributes as the PRIMARY data source — HubSpot sites often store
  structured data in HTML attributes rather than visible text or JSON-LD.
- Wait adequately for SPA rendering (15-30 seconds for pages with 400+ items).
- The constructed URL for each item should use fragment identifiers since no
  real individual pages exist (e.g., `https://site.com/job-search#job-6004988`).

## Content Type Adaptations

| Content Type | HubSpot Patterns |
|-------------|-----------------|
| Job Board | `data-job-id`, `data-specialty`, `data-provider-type`, `data-city`, `data-state` on item cards |
| Product Catalog | Less common; HubSpot is primarily a marketing CMS, not ecommerce |
| Service Directory | `data-row-id` or custom `data-*` attributes per service card |
| Blog | HubSpot blog module: `.hs-blog-post`, standard SSR `<article>` elements |

## Sites Successfully Scraped

| Site | Type | Items Found | Key Pattern |
|------|------|-------------|-------------|
| dystaffing.com | Job Board (Healthcare Staffing) | 488 | SPA all-items-on-one-page, `data-*` attributes as primary source |
