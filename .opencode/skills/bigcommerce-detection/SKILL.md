---
name: bigcommerce-detection
description: Detect BigCommerce storefronts (classic Stencil and headless Next.js) via the cdn11.bigcommerce.com asset host and platform meta tag, then extract from server-side JSON-LD ProductGroup, OpenGraph product metas with embedded BCData, the search.php catalog endpoint, or RSC-escaped JSON-LD.
---

# BigCommerce Detection & Scraping

## What I Do

Detect BigCommerce stores in both shapes — classic Stencil (Cornerstone and custom
themes) and headless Next.js on the BigCommerce catalog backend — and pick the right
extraction path: server-side JSON-LD, OG product metas + embedded BCData, the
`search.php` discovery endpoint, or RSC-escaped JSON-LD.

## When to Use Me

- `cdn11.bigcommerce.com` appears in og:image, script srcs, or CSS
- `<meta name='platform' content='bigcommerce.stencil'>` is present
- A Next.js/Vercel front-end has a cdn11 backend host
- Zero JSON-LD blocks on a mid-size retail theme

## Detection Methods

### Method 1: Asset CDN host (primary)

```html
<meta property="og:image" content="https://cdn11.bigcommerce.com/s-<hash>/images/...">
<script src="https://cdn11.bigcommerce.com/s-nb5it5hcrj/..."></script>
```

Survives onto headless Next.js fronts — the CDN host names the backend even when the
front-end is Vercel (284).

### Method 2: Platform meta tag (decisive when present)

```html
<meta name='platform' content='bigcommerce.stencil'>
```

### Method 3: Theme conventions (verify, never assume)

```html
<ul class="productGrid">
  <li class="product"><a href="/product-url/" ...>
    <h3 class="card-title"><a href="...">Title</a></h3>
```

Cornerstone defaults are frequently absent on custom themes (373).

## Scraping Mechanism

**direct_http, no proxy, no browser — every time in this evidence set.** All seven
harvested BigCommerce jobs ran plain HTTP / `fingerprint_chrome_none` with
`antibot_events: []`. None needed residential. Budget the browser for nothing.

### Extraction priority order

| Order | Source | Shape | Evidence |
|-------|--------|-------|----------|
| 1 | JSON-LD `Product` / `ProductGroup` | classic Stencil with JSON-LD | 373, 64 |
| 2 | OG product metas + embedded BCData | classic Stencil, zero JSON-LD | 664, 667 |
| 3 | RSC-escaped JSON-LD in the flight payload | headless Next.js | 284 |
| 4 | DOM h1 + price component | any shape, fallback | 284, 480 |

## Product Discovery

### search.php catalog shortcut (Stencil)

```
GET /search.php?search_query=<term>&section=product
```

Returns product rows directly — 461 items from the single term "pillows" (64). One HTTP
GET replaces a whole browse/categorize flow.

### Listing pagination

Server-side `?page=N`, ~21–32 cards per page. Two cheap sanity signals:

- **Anchor histogram** — class counts per href from a one-shot probe. The "missing" 32
  cards were anchors inside `.card-title` with no class of their own (664).
- **Zero page-1/page-2 anchor overlap** — a real-pagination signal that costs nothing (852).

## Headless / Next.js Notes

- Headless does **not** mean client-rendered: the grid ships in raw HTTP even when the
  rendered summary shows only "Loading..." hydration placeholders (852); an 825KB
  listing yielded 20 item links from one no-proxy GET (480).
- JSON-LD is present but **escaped inside the RSC/flight payload** as a string, so a
  naive `script[type=application/ld+json]` query finds nothing — unwind the escapes
  before `json.loads`, or fall back to DOM (284).
- Product sitemaps move to `checkout.<host>/xmlsitemap.php`, which the same-host guard
  blocks — plan discovery around card anchors (852).

## Price Traps

- **Sale items**: JSON-LD `offers.price` holds the *default* variant while the DOM shows
  the discounted variant (`£22.99 Save 74% £5.99`) — DOM wins for displayed price (284).
- **Instalment text** ("per month") is not a price — guard it (664).
- **Format drift**: document the price format hint before extracting ("44.99 (meta) or
  $44.99 (embedded JS)") so the writer emits one shape; a hint that invites a
  symbol-prefixed string is exactly what testers bounce (667).

## Common Issues

1. **`meta[property=product:price:amount]` is NOT a BigCommerce fingerprint** — the
   identical OG product vocabulary appears on Made-in-China, Shopify and Gatsby. Use it
   as a data source only; use `cdn11.bigcommerce.com` or the platform meta for identity.
2. **Cornerstone selectors absent** on custom themes — measure on the live page (373).
3. **`search.php` is not stable across attempts** — two earlier runs on one host failed
   with 0 records after 3,800s and 3,990s. Re-verify discovery with a tiny standalone
   probe script before trusting a draft (64).
4. **scope=all over a broad term** harvested 461 records unconfirmed (64).
5. **Execution is politeness-bound**: ~2.6s/item at scale (64).

## Known Sites

| Site | Shape | Evidence |
|------|-------|----------|
| bohemiantraders.com | Stencil, zero JSON-LD → OG+BCData | job 664 |
| canningvale.com | Stencil, platform meta tag | job 667 |
| ego.co.uk | Stencil, custom Tailwind theme, JSON-LD | job 373 |
| pillowtalk.com.au | Stencil, search.php discovery | job 64 |
| mountainwarehouse.com | headless Next.js + cdn11 | job 284 |
| toymate.com.au | headless Next.js | job 852 |
| sitkagear.com | headless, 825KB SSR listing | job 480 |

## Learned: the two-step BigCommerce fingerprint
**Source:** jobs 373, 664, 667 (headless variants: 284, 852, 480)
**Applicability:** Stencil and headless BigCommerce storefronts

Two checks settle BigCommerce instantly. (1) `cdn11.bigcommerce.com` in og:image, script
srcs or CSS - the platform CDN host; it survives onto headless Next.js fronts, e.g.
`cdn11.bigcommerce.com/s-nb5it5hcrj` behind a Vercel app (284). (2) A literal platform
meta tag, `<meta name='platform' content='bigcommerce.stencil'>`, removes all guesswork
when present (667). Extraction then depends on the shape: classic Stencil ships complete
server-side JSON-LD Product (name, brand, sku, offers with price/currency/availability) -
direct_http is enough, no browser (373). Do NOT assume Cornerstone selectors: one store
ran a custom Tailwind theme where the default theme selectors are absent, so measure on
the live page (373); on Bohemian Traders the cards were `ul.productGrid > li.product`
with the anchor INSIDE `.card-title` - anchors carry no class of their own, so a
class-based anchor selector reads as a block when it is really a selector bug. An anchor
histogram (class counts per href) from a one-shot probe catches exactly this (664).

## Learned: zero-JSON-LD Stencil - OG metas + embedded BCData
**Source:** jobs 664, 667 (counter-evidence 217, 483, 485, 554, 871)
**Applicability:** classic Stencil themes without JSON-LD; any JSON-LD-free storefront

Some Stencil stores ship ZERO JSON-LD blocks and still expose everything:
`meta[property=product:price:amount]` (388), `product:price:currency` (USD),
`og:availability`, plus an embedded BCData script object for the product bean - no API
key, no browser (664). Canningvale pairs the same OG metas with embedded Stencil product
JSON carrying `'available': true`; document the price format hint BEFORE extracting
("44.99 (meta) or $44.99 (embedded JS)") so the writer emits one shape - the hint that
invited a symbol-prefixed string is exactly what testers bounce (667). CAVEAT: the OG
product vocabulary is NOT a BigCommerce fingerprint - the harvest shows the identical
`product:price:amount` tags on Made-in-China (483, 485, 871), Shopify (217) and Gatsby
(554). Use OG metas as a DATA SOURCE on any platform; use cdn11.bigcommerce.com or the
platform meta tag for identity. Instalment / "per month" text is not a price - guard it.

## Learned: search.php?section=product - one GET replaces a browse flow
**Source:** job 64 (Pillow Talk)
**Applicability:** Stencil BigCommerce under search_term or navigation input modes

BigCommerce exposes search-term discovery as a plain GET:
`/search.php?search_query=<term>&section=product` returns product rows directly - 461
items from the single term "pillows", replacing an entire browse/categorize/navigation
flow over plain HTTP. The PDP side is ProductGroup JSON-LD, so the whole job is one
transport (64). Three cautions from the same job: (a) scope=all over a broad one-word
term harvested 461 records with nobody confirming the size first - check the row count
against intent before execution; (b) the two earlier attempts on this host (runs 14 and
59) failed with 0 records after 3,800s and 3,990s, so the endpoint is not stable across
attempts - re-verify discovery with a tiny standalone probe script before trusting a
draft; (c) execution ran ~2.6s/item, i.e. the politeness delay dominates at scale.

## Learned: headless BigCommerce - escaped JSON-LD and the sitemap split
**Source:** jobs 284, 852, 480
**Applicability:** BigCommerce behind Next.js / Vercel storefronts

Headless does not mean client-rendered. Mountain Warehouse (Next.js on Vercel + cdn11
backend) is fully SSR-HTML scrapeable by plain HTTP even though site_analysis labeled it
playwright (284); Toymate's grid renders server-side while the rendered summary shows
only "Loading..." hydration placeholders - raw HTTP sees the full card grid, so never
conclude client-rendering from the hydration shell (852); Sitka Gear's 825KB listing
yielded 20 item links from one no-proxy GET (480). The JSON-LD is present but escaped
inside the Next.js RSC/flight payload as a string, so a naive
`script[type=application/ld+json]` query finds nothing - unwind the escapes before
JSON.parse, or fall back to DOM selectors (284). On sale items JSON-LD offers.price
holds the default variant while the DOM shows the discounted one - DOM wins for the
displayed price (284). Product sitemaps move to `checkout.<host>/xmlsitemap.php`, which
the same-host guard blocks - plan discovery around card anchors, and use zero page-1 /
page-2 anchor overlap as a cheap real-pagination signal (852).
