---
name: fanatics-commerce-detection
description: Detect Fanatics Commerce storefronts (NFL Shop, NBA Store, WNBA Store, fanatics.com) by their +p-/+t-/+z- URL tokens and frgimages.com image host, ride their Akamai wall with CloakBrowser cloak_none and no proxy, and extract price from offers[0].priceSpecification.price or OG meta tags.
---

# Fanatics Commerce Detection & Scraping

## What I Do

Detect Fanatics Commerce licensed-sports storefronts and survive their Akamai
wall. Every fetch is a browser rung: CloakBrowser `cloak_none` with NO proxy,
driven per page. Structured data is inconsistent across the family, so the
offers substructure must be probed before any reader is written.

## When to Use Me

- Akamai-fronted licensed-sports merchandise storefronts
- A ladder log showing "blocked by Akamai, trying bypass..." (not a verdict - see Learned 2)
- A probe render showing a wall instead of site content (see Learned 1)

## Detection Methods

1. **URL tokens:** `/...+t-{teamId}+p-{productId}+z-{...}` product paths
   (fanatics.com additionally prefixes `o-`).
2. **Image host:** `fanatics.frgimages.com`.
3. **Wall signature:** a render containing only "Powered and protected by
   Privacy" with no TITLE/H1/JSON-LD is an interstitial, never site content.

## Known Fanatics Sites (harvest-confirmed only)

| Site | Host | Source job |
|------|------|-----------|
| NFL Shop | nflshop.com | 792 |
| NBA Store | store.nba.com | 545 |
| WNBA Store | wnbastore.nba.com | 747 |
| Fanatics | fanatics.com | 404 |

## Learned: Fanatics Commerce identity - PrivacyWall interstitial, URL tokens, image host
**Source:** jobs 792 (NFL Shop), 545/747 (NBA/WNBA Store), 404 (fanatics.com)
**Applicability:** Fanatics Commerce storefronts (nflshop.com, store.nba.com, wnbastore.nba.com, fanatics.com)

Fanatics Commerce is Akamai-fronted and its cached probe can render a bot-wall instead of the site: job 792's cached probe rendered 'Powered and protected by Privacy' with no TITLE, no H1 and no JSON-LD. Treat that string as 'this render is a wall - discard it and re-probe live', never as site content; note the identical string also appears on ordinary privacy-consent walls elsewhere (job 941, zalando.be), so it is an interstitial signal, not a Fanatics tell. Confirm the platform instead by host + URL shape: +p-/+t-/+z- (and fanatics.com's o-) suffixed product paths appear in all four jobs (545 /t-36038306+z-831088-..., 792 /t-58155952+p-46882612800139+z-8-..., 404 /o-21202441+t-92296846+p-13886659419+z-9-...), with fanatics.frgimages.com as the image host (792). Job 792 is where PrivacyWall first entered any skill ('it was not documented in any skill until this job').

## Learned: Akamai on Fanatics - a blocked line mid-ladder is not a verdict, and cloak_none wants NO proxy
**Source:** jobs 792, 545, 747, 404
**Applicability:** Any Akamai-fronted storefront whose ladder logs a blocked line

Akamai bypass is a rung property, not a binary: on nflshop.com both direct_http and playwright_none logged 'blocked by Akamai, trying bypass...' and then SUCCEEDED on the same rung (792) - do not treat a blocked line as a verdict until the bypass attempt resolves. Same shape on 545 (direct_http 'Access Denied', bypass succeeded) and 404. Once through, the transport that holds is CloakBrowser cloak_none with NO proxy: 792 rode it per page via browser_service POST /navigate (PDP 825KB rendered; listing probe 1,668,601 bytes), 545 pulled the real 857,563-byte listing. Job 747 is the negative control: playwright_datacenter and cloak_datacenter both failed the Akamai bypass, and 'adding a datacenter proxy to the cloak browser made it WORSE, not better'. Direct HTTP is 403 domain-wide on fanatics.com (404) and mid-run HTTP fetches still 403'd on wnbastore (747), so plan every fetch through the browser rung.

## Learned: Fanatics structured data is inconsistent - probe the offers shape, keep OG meta as fallback
**Source:** jobs 545, 792, 747, 404
**Applicability:** Fanatics PDP price extraction

Do not assume one data surface across the family; four recorded variants. (a) store.nba.com nests the price at offers[0].priceSpecification.price - 'a generic reader that looks for offers.price silently drops the price field, which is exactly what happened before the probe' (545). (b) wnbastore.nba.com carries a single product-level offer, so per-size price/availability must come from DOM selectors over cloak-rendered HTML (747). (c) nflshop.com ships ZERO JSON-LD: '0 JSON-LD blocks (this is a Fanatics-platform site...) but complete Open Graph product tags', primary extraction meta_og_tags (792). (d) fanatics.com is a Kibo-family offers shape needing explicit sale-vs-list orientation rather than taking offers[0] (404). Family rule: dump the real offers substructure with a scratch probe (probe_jsonld.py / probe_listing.py) before writing readers, and keep og:price:amount / og:price:currency wired as the always-present fallback (545, 747, 792).

## Learned: Fanatics variant fields live in the DOM - enumerate sizes, order sale prices
**Source:** jobs 545, 747
**Applicability:** Fanatics PDP size/availability extraction over cloak-rendered HTML

Structured data will not give you variant-level fields here, so ride the DOM. Size selectors render with NOTHING selected: never wait for a selected size - enumerate div.size-selector-list button[data-talos='buttonSize'] and derive per-size availability from the per-size stock text, all-sizes-unavailable => out_of_stock (545). On sale PDPs the lower price is the current one: [data-talos='pdpProductPrice'] .price.primary .money-value = $17.99 vs .strike-through .money-value = $39.99 - apply that ordering rule rather than guessing which node is the sale price (545). Two recorded cautions: the cached render showed only a privacy-gate overlay with all core fields absent and the analyzer's price/size selectors were guesses marked tested:false - price and size came back missing on all 5 sample items until a saved-HTML probe fixed them (545 logs 22, 79); and discovery needs nothing special beyond the working transport - one /women/ listing pass hit the 50-URL limit_hit cap (747).
