---
name: thg-ingenuity-detection
description: Detect The Hut Group / THG Ingenuity ecommerce sites (Dermstore, Lookfantastic) by their static.thcdn.com and csp.thehut.net asset hosts, and extract from their JSON-LD ProductGroup hasVariant[].offers structure over plain direct HTTP - including the stale VERIFIED-EMPTY field-map trap that hides the variant offers.
---

# THG Ingenuity (The Hut Group) Detection & Scraping

## What I Do

Detect storefronts on THG Ingenuity (The Hut Group) and drive them over plain
direct HTTP with a Chrome TLS fingerprint. THG PDPs ship a JSON-LD ProductGroup
whose `hasVariant[].offers` entries carry price/currency/availability, so one
HTTP request covers nearly every requested field.

## When to Use Me

- Platform detection on beauty / health / apparel storefronts
- A "custom" verdict that does not feel custom (THG verdicts are unstable - see Learned 1)
- Field mapping on a THG PDP where a cached field map says the variant offers are empty

## Detection Methods

1. **Static asset hosts (primary):** `static.thcdn.com` and `csp.thehut.net` in
   page source. Recorded verbatim in the Dermstore platform verdict:
   `thg_ingenuity_custom_astro_ssr (The Hut Group; markers static.thcdn.com + csp.thehut.net)`.
2. **`/p/{slug}/{id}/` PDP path shape** with recommendation tracking params
   (`?rctxt=sponsoredAdsPLP`, `?rctxt=fbt`) on both PDP and listing seeds.
3. **URL shape is NOT a signal** - a "custom" or "unknown" verdict is common on
   this family (lookfantastic.com was recorded as `custom` in one job and `thg`
   in another).

## Scraping Mechanism

Direct HTTP with a Chrome TLS fingerprint, no proxy, no JS. All three harvest
jobs on this platform ran `direct_http` / `fingerprint_chrome_none` with zero
anti-bot events and 0-1 fix cycles. Product discovery config belongs in the
draft as a `_discovery_cfg` data dict, not hand-written helper code.

## Known THG Sites (harvest-confirmed only)

| Site | Host | Source job |
|------|------|-----------|
| Dermstore | www.dermstore.com | 915 |
| LOOKFANTASTIC | www.lookfantastic.com | 91, 312 |

Other THG brands (Mankind, Skinstore, Myprotein, etc.) have zero harvest
mentions - do not extend this table from memory.

## Learned: THG Ingenuity platform identity - asset-host tells, and an unstable verdict
**Source:** jobs 915 (Dermstore), 91 + 312 (lookfantastic.com)
**Applicability:** The Hut Group / THG Ingenuity family (Dermstore, Lookfantastic, etc.)

Detect THG by static asset hosts, not by URL shape or platform guess: static.thcdn.com + csp.thehut.net are the platform markers (job 915 platform string: 'thg_ingenuity_custom_astro_ssr (The Hut Group; markers static.thcdn.com + csp.thehut.net)'). The verdict is UNSTABLE on identical storefronts: lookfantastic.com was recorded platform='thg' in job 91 and platform='custom' in job 312 - same host, two answers. A 'custom' verdict on a beauty storefront is therefore not evidence of absence; re-check asset hosts before hand-rolling a bespoke extractor. Transport is the good news: all three jobs ran plain direct_http with a Chrome TLS fingerprint and no proxy (915 fingerprint_chrome_none, 0 fix cycles; 91 direct_http_datacenter OK, no JS; 312 direct_http proxy none) with ZERO anti-bot events recorded across all three.

## Learned: THG hasVariant[].offers carries the price - and a stale VERIFIED-EMPTY flag says it doesn't
**Source:** job 915 (Dermstore, logs 81/85); job 898 (Olive Young) for the inverse
**Applicability:** THG Ingenuity PDPs; any pipeline carrying a cached field map

THG ships JSON-LD ProductGroup whose hasVariant[].offers entries DO carry price/currency/availability (Dermstore shipped 75.0 USD straight from structured data). The trap: the static field map had hasVariant[].offers marked VERIFIED EMPTY, and trusting it would have pushed the writer onto DOM selectors. The fix was a live re-anchor probe: 'the field map flags price/currency/availability as VERIFIED EMPTY at hasVariant[].offers and requires me to re-anchor them to a populated source' -> 'the live probe proves price/currency...' (logs 81, 85). Rule: treat [VERIFIED EMPTY] as a hypothesis to re-probe, never a verdict - and it cuts both ways, because on Olive Young (898) the map's VERIFIED EMPTY warnings were CORRECT (the SSR price node only exists after hydration) and shipping anyway cost 4 high-severity tester failures. Probe, then trust the probe.

## Learned: THG field sourcing - JSON-LD primary, meta description is a stub, strings are double-encoded
**Source:** jobs 915 (Dermstore), 91 (lookfantastic)
**Applicability:** THG PDP extraction over direct HTTP

One direct-HTTP PDP request covers nearly every field: THG puts name/price/currency/availability/description/colour into JSON-LD (job 91 log 61); only size and location need DOM fallbacks, and on beauty PDPs size is often genuinely absent (a 'various shades' product has no size selector). Three field traps. (1) The meta description on THG pages is an SEO stub - take description from JSON-LD only, and strip ad-tracking params (rctxt, sponsoredAdsPLPIndex) from the canonical url or the output carries ad noise (915 log 27). (2) THG JSON-LD ships double-encoded UTF-8 (probe title rendered as 'SENT' plus a mis-decoded E-acute, 915 log 84); repair per extracted STRING, never a whole-document latin-1 round-trip - that corrupts the surrounding markup and broke the parse chain (915 logs 53-59), and every downstream string comparison needs the same fixer. (3) Availability is DOM text ('In stock | Usually dispatched within 24 hours'), not JSON-LD (91 log 69).

## Learned: THG listing pages - test-id price hook, promo drift, and probe-before-tester
**Source:** jobs 312, 91
**Applicability:** THG listing/discovery work and draft validation

Listing PDPs expose a stable data-testid='main-price' price hook - test-id selectors beat class names on THG and should be preferred whenever present (job 312). Two discovery cautions from the same job: the listing harvest followed promo modules instead of the seed's category (rows came back as advent-calendar bundles while the seed was a hair-conditioner PDP), and harvested PDP URLs carry recommendation tracking (?rctxt=fbt, ?rctxt=sponsoredAdsPLP) so the seed itself can originate from a sponsored ad tile rather than the organic grid (91). Encode discovery as a data dict (_discovery_cfg) in the draft rather than hand-written helper code - that is the template-conformant way to stay inside the writer's guardrails (91 logs 157-158). Cheap insurance: run a throwaway probe_jsonld.py to confirm the JSON-LD contract before the tester spends a cycle on the real scraper (91 logs 112-113; that same scratch file was later launched by the tester, 915 log 42).
