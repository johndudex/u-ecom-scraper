---
name: sap-hybris-detection
description: Detect SAP Commerce Cloud / Hybris storefronts (classicAccelerator, Spartacus/Angular, AEM-fronted) and extract product data from their inline SRG payloads, SSR JSON-LD, or schema.org microdata — including the $0.01 FROM placeholder-price trap and stale promo JSON-LD.
---

# SAP Commerce Cloud / Hybris Detection & Scraping

## What I Do

Detect SAP Commerce Cloud (Hybris) ecommerce sites across their three storefront shapes —
classicAccelerator (server-rendered), Spartacus (Angular SPA on an SSR shell), and
AEM/Kleecks-fronted hybrids — and pick the data surface each shape actually exposes:
an inline `window.__SRG_PRODUCT_DATA__` payload, SSR JSON-LD, or schema.org microdata.

## When to Use Me

- Site Analyzer is fingerprinting a mid/large retailer with an unfamiliar platform label
- A page has Organization/WebSite JSON-LD but NO Product JSON-LD
- Prices look like `$0.01` or a `FROM` placeholder in an inline JS object
- A site is Akamai-walled and a luxury/jewellery brand is suspected

## Detection Methods

### Method 1: Inline SRG payload

```html
<script>window.__SRG_PRODUCT_DATA__ = { ... 43KB product object ... };</script>
```

Present in RAW server HTML — no JS execution needed. Parse it with a regex slice plus
`json.loads`, not with a DOM query.

### Method 2: OCC / Spartacus markers

| Marker | Where |
|--------|-------|
| `meta[name=occ-backend-base-url]` | `<head>` — leaks the OCC API host |
| `api.{host}` + `/occ/v2/...` | Network calls, meta tag |
| `/medias/?context=...` | Image URLs (Hybris media service) |
| `classicAccelerator` / `Spartacus` strings | Asset URLs, image URLs |
| `/p-<productId>/<slug>?variantID=` | URL grammar (Swarovski) |
| `/p/<slug>/<BP########>-<colour>` | URL grammar (AU/NZ retail group) |
| `/p/<digits>` | URL grammar (Clicks) |
| `/product/<numericId>/<slug>` | URL grammar (Priceline) |

### Method 3: Microdata without Product JSON-LD

```html
<div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
  <meta itemprop="price" content="29.99">
  <meta itemprop="priceCurrency" content="AUD">
  <link itemprop="availability" href="http://schema.org/InStock">
```

Classic stores ship ONLY Organization/WebSite JSON-LD. If JSON-LD is thin, check for
microdata before concluding the page has no structured data.

### Method 4: DOM price/title shapes

```html
<h1 class="product-category">Pregnavit M</h1>   <!-- brand -->
<div class="product-name">30 Capsules</div>     <!-- pack size -->
<span class="price-wrapper show-price">$181.00 <s>$210.00</s></span>
```

## Scraping Mechanism

Most Hybris retail is **direct_http** (no proxy, no browser): Harris Scarfe (171),
Spotlight (269), Clicks (603), Valentino (832) and one Priceline run (13) all passed
plain HTTP. Verify with a single raw fetch before committing to a template — probe
`js_rendering_needed=True` was wrong on Clicks and cost a browser lane (603).

Two exceptions drive the anti-bot posture below.

## The Placeholder-Price Trap (never emit $0.01)

`__SRG_PRODUCT_DATA__` carries a **top-level placeholder price** — `"price": 0.01` with
`priceType: "FROM"` — while real prices live per variant:

```python
payload = parse_srg(html)
variants = payload.get("variants", [])
real = [v for v in variants if v.get("fromPrice") and v["fromPrice"] != 0.01]
# fromPrice/toPrice = $65–$99 ; fromRegularPrice/toRegularPrice = $160–$200
```

Never read the summary field. This is the Hybris analogue of the Shopify cents trap.
Style URL and size-variant URL both return 200 with the payload; `size` is intentionally
empty on the style page and populated on the variant page (269).

## Stale Promo JSON-LD

Promotions leave JSON-LD behind: 173.00 ZAR in JSON-LD vs the live R138.00 with a
"Save 20%" tag (603). The rendered DOM price must outrank JSON-LD on any promoted site,
with a remarks note. Conversely, JSON-LD may lack the was-price entirely — the
strikethrough lives in `.price-wrapper.show-price` (13).

## Anti-Bot Posture

| Rung | Evidence |
|------|----------|
| `direct_http` | Default. Works on 5 of 7 harvested Hybris hosts |
| `uc_chrome` (undetected Chrome) | Swarovski: only rung that passed, at BOTH none and datacenter proxy (150) |
| `cloak` (CloakBrowser) | Swarovski later run: needed on BOTH PDP and listing; `[LISTING-PROBE]` confirmed a 1.1MB listing before generation (328) |
| `playwright_none` | Priceline second run: all 8 HTTP rungs failed, plain Playwright passed (317) |

Read the **browser-method** line, not the summary `Method:` field — `cloak_none` hid the
fact that `uc_chrome_datacenter` was the working tier, costing three burned rungs (150).
Re-probe per run: the same host passed direct_http in one job and failed all HTTP rungs
in another.

## Common Issues

1. **JSON-LD-first drafts fail silently** — no Product JSON-LD exists on classic stores.
2. **Facet listings are single-page** — `/c-02/`-style facets stop on `no_next_link`;
   `--limit 50` discovery is enough (150). Category listings paginate with `&page=N` /
   `?page=N`, 18–32 links per page (269, 603) — a guess of infinite scroll yields 18
   links and stops.
3. **Page params can be no-ops** — `?page=2` returned the identical 24 items on one SSR
   grid; probe page 2 for a payload change before trusting param pagination (416).
4. **`require_same_domain` blocks the OCC API host** (`api.{host}` vs `www.{host}`) —
   exactly when you most want it. The base URL still leaks via the meta tag (13).
5. **direct_http on Spartacus can return a near-empty Angular bootstrap shell** (271).
6. **Total-count regexes misfire** — "2 items" matched mid-page and prematurely stopped
   pagination; only trust totals from explicit pagination controls (171).
7. **Analytics endpoints are not product APIs** — `useinsider.com/api/info/` is a
   currency-config object; three jobs burned budget on it (171, 173, 269).

## Known Sites

| Site | Shape | Evidence |
|------|-------|----------|
| spotlightstores.com | classicAccelerator + `__SRG_PRODUCT_DATA__` | job 269 |
| harrisscarfe.com.au | classic, microdata-only | job 171 |
| swarovski.com | Akamai + uc_chrome/cloak, `p-<id>?variantID=` | jobs 150, 328 |
| clicks.co.za | classicAccelerator, stale promo JSON-LD | job 603 |
| priceline.com.au | Spartacus, SSR JSON-LD + OCC | jobs 13, 317 |
| fantasticfurniture.com.au | Spartacus/Angular CSR | job 271 |
| valentino.com | SAP Hybris OCC + AEM/Kleecks | job 832 |

## Learned: __SRG_PRODUCT_DATA__ and the $0.01 "FROM" placeholder price
**Source:** job 269 (Spotlight Stores); caveat job 173
**Applicability:** Hybris classicAccelerator storefronts in the AU/NZ retail group, and any site carrying an inline SRG payload

Spotlight's PDP ships a 43KB `window.__SRG_PRODUCT_DATA__` object in RAW server HTML -
one fetch replaces all DOM scraping. Check for the inline global BEFORE writing
selectors: no Product JSON-LD exists on this store (only Organization + WebSite), so a
JSON-LD-first draft fails silently. THE TRAP: the payload's top-level price is a
placeholder - `"price": 0.01` with `priceType: "FROM"`. Real prices live per variant as
fromPrice/toPrice ranges ($65-$99) and fromRegularPrice/toRegularPrice ranges
($160-$200). Never emit the summary price: emit the matching variant, or the range. The
style URL and the size-variant URL both return 200 with the payload; `size` is
intentionally empty on the style page and populated on the variant page, so map the
field from the URL form you actually fetched. CAVEAT: this global is a retailer-group
payload convention, not a Hybris proof - job 173 (Anaconda, platform-labeled sfcc) also
chased it and found it redundant with server HTML. Confirm the backend separately.

## Learned: three data surfaces - pick by probe, not by habit
**Source:** jobs 13, 171, 271, 832 (adjacent 416)
**Applicability:** SAP Commerce Cloud: Spartacus (Angular), classicAccelerator, AEM fronts

Hybris ships three different data surfaces; probing one is not enough. (1) Spartacus
serves SSR HTML whose JSON-LD has the FULL Product even though the page is an Angular
SPA (13) - direct_http on the PDP is enough, but the same class of site can return a
near-empty Angular bootstrap shell (271), so verify with one plain fetch before choosing
a template. (2) Classic stores carry NO Product JSON-LD at all - only
Organization/WebSite - but ship complete schema.org MICRODATA: `itemprop=offers` >
`meta itemprop=price content="29.99"`, priceCurrency, availability, readable with plain
HTML parsing (171). (3) An AEM/Kleecks front (Valentino) is fully server-rendered with
ItemList JSON-LD on the listing and a complete Product block on the PDP - but the
listing ItemList offers OMIT the availability the PDP carries, and the PDP image array
can hold 1 entry while the listing holds 6, so acceptance must allow 1..n (832). When a
surface has no field, enumerate window globals (pageModel, utag_data) before declaring
it unavailable (416).

## Learned: promo prices make JSON-LD stale - the DOM outranks it
**Source:** jobs 603, 13 (same family on BigCommerce: 284)
**Applicability:** any promoted Hybris storefront - pharma, grocery, AU/NZ retail

Clicks ran a "Save 20%" promotion and the JSON-LD Product price stayed pre-promo:
173.00 ZAR in JSON-LD vs the live R138.00 in the rendered DOM. For any promoted retail
site the rendered DOM price must outrank JSON-LD, with a remarks note. Same family,
opposite direction: on Priceline JSON-LD `offers.price` carries the current price but
LACKS the was-price - the strikethrough lives in `.price-wrapper.show-price` in the DOM,
so previous_price needs a DOM-side fallback (13; the OCC
`/occ/v2/products/<sku>?fields=FULL` endpoint on the api. subdomain is the other route,
but the same-domain guard blocks it during analysis - the base URL still leaks via
`meta[name=occ-backend-base-url]`). Hybris also splits the product name across two
nodes: `h1.product-category` (brand) + `.product-name` (pack size) - concatenating them
is what produces brand + pack-size titles that survive validation (603). Images come
from the Hybris media service `/medias/?context=...` - take them verbatim (603, 271).

## Learned: anti-bot - probe both families, both surfaces; the label lies
**Source:** jobs 150, 328, 317 (contrasted with 13, 171, 269, 603, 832)
**Applicability:** Akamai-fronted Hybris (luxury) vs clean classicAccelerator retail

Most Hybris retail is direct_http-clean: Harris Scarfe, Spotlight, Clicks, Valentino and
Priceline (one run) all passed plain HTTP with no proxy. Swarovski is the counter-case:
Akamai walled every plain-HTTP and vanilla-Playwright tier; only uc_chrome passed, and it
passed at BOTH none and datacenter proxy - undetected-Chrome mattered more than the
proxy (150). A later run of the same host needed cloak on BOTH surfaces: the
[LISTING-PROBE] rung confirmed cloak_none on the 1.1MB listing before generation, which
avoids a run that fetches 10 PDPs and 0 links (328). TRAP: probe Method "cloak_none"
hides which tier actually worked - the browser-method line said uc_chrome_datacenter;
three tiers burned 82s to learn it (150). And the same host can flip between runs:
Priceline passed direct_http in job 13 but failed all 8 HTTP rungs in job 317, where
plain playwright_none succeeded (cloak also passed) - re-probe per run, never pin
transport from memory or from a previous job's artifact.
