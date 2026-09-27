---
name: netsuite-detection
description: Detect NetSuite SuiteCommerce Advanced (SCA) sites and use their item-search API and JSON islands for reliable extraction
---

- NetSuite SuiteCommerce Advanced storefronts ship a ~5.8KB client-rendered HTML shell AND an UNAUTHENTICATED `/api/items?url={slug}&fieldset=details` JSON endpoint returning displayname, onlinecustomerprice_detail, isinstock and matrix children over plain direct HTTP (658 West Elm AU, 679 Pottery Barn Kids AU).
- The same endpoint does double duty for discovery: `fieldset=search` with `category=` or `q=` returns total+items, so Phase 1 can be pure API pagination instead of HTML link scraping (679).
- Fingerprint: a NetSuite build comment in the HTML footer (658).
- Foreign-currency price verification: recompute from known components - MSRP $59.00 x 1.1 AU GST (custitem_tax_rate_au 10.0) = $64.90 matched onlinecustomerprice_detail, confirming field and tax assumption in one step (679).
- Both jobs still lost requested fields in the shipped rows (size empty; name/availability absent) - the s12 pattern, not a NetSuite property.

## Learned: NetSuite SCA: unauthenticated /api/items is the real lane
**Source:** 408-job prod harvest (2026-09-26), _harvest/jobs/ job ids cited inline
**Applicability:** NetSuite SuiteCommerce Advanced storefronts

NetSuite SCA HTML is a ~5.8KB client-rendered shell; the real lane is an
UNAUTHENTICATED `/api/items?url={slug}&fieldset=details` JSON endpoint returning
displayname, onlinecustomerprice_detail, isinstock and matrix children over plain
direct HTTP (658,679). The same endpoint does DISCOVERY: `fieldset=search` with
`category=` or `q=` returns total+items, so Phase 1 can be pure API pagination instead
of HTML link scraping (679). Fingerprint: a NetSuite build comment in the HTML footer
(658). Verify foreign-currency prices by recomputing them from components - MSRP x tax
rate matched onlinecustomerprice_detail (679).
