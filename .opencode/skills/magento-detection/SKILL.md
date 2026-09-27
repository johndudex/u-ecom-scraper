---
name: magento-detection
description: Detect Magento / Adobe Commerce (Luma, PWA Studio, Hyva) and pick the extraction path that survives client-rendered listings
---

- Magento 2 tells: data-ui-id attributes; the price box is span[data-price-type=finalPrice|oldPrice] inside .price-box - parse those instead of regexing text (160,163,166,588,910,916,952).
- PWA/headless Magento renders the listing body client-side (498 briscoes; 588/589 dusk; 605 King Living) - a 200 shell proves nothing; use the PageBuilder/gql API or an embedded state block.
- ProductGroup JSON-LD can be variant-empty (280) - fall back to the DOM price box; Nuxt3 Magento variants exist; pagination walks category .html paths.
- xml namespace xmlns:blc is Broadleaf, NOT Magento - don't misfire the platform verdict.
- Read the theme out of the static asset path, never out of agent prose: job 910's tester said 'Luma', the static path said hooya (910).

## Learned: Magento 2 detection tells and the PWA listing trap
**Source:** 408-job prod harvest (2026-09-26), _harvest/jobs/ job ids cited inline
**Applicability:** Magento 2 / Adobe Commerce sites, esp. PWA Studio and Nuxt fronts

Magento 2 tells: data-ui-id attributes, and the price box is
span[data-price-type=finalPrice|oldPrice] inside .price-box - parse those instead of
regexing text (160,163,166,588,910,916,952). PWA/headless Magento renders the listing
body CLIENT-SIDE (498 briscoes; 588,589 dusk; 605 King Living), so a 200 shell proves
nothing - use the PageBuilder/gql API or an embedded state block. ProductGroup JSON-LD
can be variant-empty (280): fall back to the DOM price box. Nuxt3 Magento variants
exist; pagination walks category .html paths. The xmlns:blc namespace is Broadleaf,
NOT Magento - do not misfire the platform verdict. Read the theme out of the static
asset path (/static/.../frontend/default/hooya/en_US/...), never out of an agent's
prose: job 910's tester called the theme 'Luma' while the path said hooya (910).
