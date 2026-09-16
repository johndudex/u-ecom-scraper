---
name: vinted-detection
description: Detect Vinted marketplace sites (any country TLD) and drive them correctly — the catalog/API layer is Cloudflare-blocked to datacenter egress, so item URLs + PDP JSON-LD (url_list mode) is the supported path, not search/navigation.
license: MIT
compatibility: opencode
metadata:
  audience: site-analyzer
  workflow: scraping
  learned_from: https://www.vinted.be
  learned_date: 2026-09-15
---

# Vinted Marketplace Detection & Extraction

## What I Do

Detect Vinted second-hand marketplace sites and route the job to the one
mode that works: **url_list with item (PDP) URLs**. Vinted renders product
data as JSON-LD on every PDP, so extraction from item pages is reliable
HTTP work. Everything ABOVE the item page — search, catalog listing,
taxonomy — sits behind Cloudflare and returns 403 to the proxy ladder.

## When to Use Me

Use this when:
- The target URL host is `vinted.<tld>` (vinted.com, vinted.de, vinted.be,
  vinted.fr, vinted.nl, vinted.es, vinted.it, vinted.pl, vinted.lt, ...)
- A sample page's JSON-LD contains `["Product", "Product"]`-style BRD
  entity types or `"@type": "Offer"` with a marketplace seller
- The user pastes a vinted item URL (shape `/{tld}/items/{id}-{slug}`)

## 1. Detection Heuristics

- **Host**: any `vinted.{country-tld}` registrable domain. The TLD is the
  market — catalog stock differs per TLD, don't cross-harvest.
- **PDP JSON-LD**: two adjacent `Product` entities (BRD layout), or one
  `Product` whose `offers` carries marketplace seller/condition fields
  (`item_condition`, `brand`, `status`).
- **Catalog CSR shell**: category/search pages ship an empty HTML shell;
  the grid hydrates from `/api/v2/catalog/items` client-side.

## 2. The API Wall (verified 2026-09-15, job 622 class)

`https://www.vinted.be/api/v2/catalog/items?search_text=...` returns:

| Ladder tier | Result |
|-------------|--------|
| direct HTTP (requests) | 403 |
| browser, datacenter proxy | 403 |
| browser, residential proxy | 403 |
| curl_cffi fingerprint tier | 404 (challenge variant) |

Same wall on `/api/v2/notifications` and the other `/api/v2/*` reads.
**Do not build API-first strategies for vinted.** Do not burn retries
re-probing the catalog endpoint — the verdict is structural
(Cloudflare + signed client), not tier-escalatable.

## 3. The Supported Mode: url_list over PDPs

1. `input_mode: url_list`, `item_urls` = vinted PDP URLs on ONE TLD.
2. Generated scrapers fetch each PDP with the shared ladder (PDPs answer
   HTTP where catalog endpoints 403) and read the JSON-LD `Product`.
3. Fields: `name`, `brand`, `price` (+ `currency` from offers),
   `condition`, `description`, `url` (canonical from the page).
4. Per-TLD discipline: item URLs must share the job's registrable domain;
   PDP carousels contain cross-TLD recommendations — never follow them.

## 4. What NOT to Do

- No navigation/search_term/list_page jobs: discovery above the item page
  is catalog-API-shaped and Cloudflare-walled — the walk dies at 403.
- No `max_pages`/pagination strategy: there is no reachable page-2.
- Don't set availability from the page badge; use the JSON-LD offers.

## Learned: PDP seed must flip to url_list at intake (2026-09-15)

A single vinted PDP URL submitted as `list_page` used to run a listing
traversal that found only cross-TLD carousel links and died. The intake
probe now discriminates PDP JSON-LD (canonical Product on the page's own
host) and flips `list_page → url_list` before the graph runs — a lone
PDP seed is one item, not a listing. Same flip applies to any
marketplace whose PDP carries canonical product JSON-LD.
