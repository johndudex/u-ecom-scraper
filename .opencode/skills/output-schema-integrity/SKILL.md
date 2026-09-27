---
name: output-schema-integrity
description: Keep output honest to the requested schema — every requested field present, empty means empty, no bookkeeping keys counted as coverage
---

- Requested fields silently vanish while the job still reads 'completed': ~20/34 (batch 05) and ~24/34 (batch 07) jobs shipped records WITHOUT the requested 'product name'/'availability'; worst cases 477 (1 of 4 fields), 502 (price+currency as empty strings), 487 (typo key 'qulity' always empty), 136 (10 records, all core fields empty).
- Cause: the tester counts bookkeeping keys (url/src_url/status_code/scraped_at) toward field coverage, so PASS != schema-complete; user-typed names ('product name', 'avaliablity') are analyzer-verified but dropped by the execution record builder (605/606/607/609 all shipped the same 6-key shape).
- Completion status is transport success, not data quality: 275 shipped 'completed' with NEEDS_FIXES and a /size-guide/ page as a product; 808 shipped 4 records at NEEDS_FIXES (0.6, 3 high); 960 recorded 'completed' with a FAIL 0.0 report; scope under-delivery (firstn:10 -> 3-9 records) never surfaced as an error (469,473,474,479,481,482,499,498,500).
- run_history record counts are the honest tally - they disagree with job.product_count and with the shipped file (597 50v10, 601 5v10, 607 1v10, 610 10v4 with a file dated 5 days later, 783 count 12 vs records 10).

## Learned: Output-schema integrity: requested fields vanish while status says completed
**Source:** 408-job prod harvest (2026-09-26), _harvest/jobs/ job ids cited inline
**Applicability:** Every job; a contract between intake, writer, tester and the record builder

Requested fields silently vanish while the job still reads 'completed': in two
batches ~20-24 of 34 jobs shipped records WITHOUT the requested 'product name'/
'availability' (477: 1 of 4 fields; 502: price+currency EMPTY strings; 487: typo key
'qulity' always empty; 136: 10 records, all core fields empty). WRITER WORKAROUND:
emit BOTH the user-typed key and a canonical alias (name + title, avaliability +
availability), because the record builder drops names the tester never checks
(605,606,607,609 all shipped the same 6-key shape). TESTER WORKAROUND: it counts
bookkeeping keys (url/src_url/status_code/scraped_at) toward coverage, so PASS !=
schema-complete - verify the requested fields in the shipped rows, never infer from
PASS. Treat run_history records as the honest tally: it disagrees with
job.product_count and with the shipped file (597,601,607,610,783). And step durations
sum to ~2x run_history duration_seconds - phase timers OVERLAP, never read them as
sequential (481,487,924).

## Learned: Currency is an independent output field, never a function of TLD or locale
**Source:** job 968, job 699, job 716, job 309, job 318, job 679

TLD, locale path and locale params do NOT determine currency - read a named currency
node and verify. L.L.Bean's 'Global Store' localizes at request time: the shipped row
carried IDR while the same PDP's JSON-LD offers.priceCurrency said USD (968). A US-geo
session on amazon.co.jp renders USD via Amazon Currency Converter, contradicting the
amazon-detection 'hardcode per TLD (co.jp->JPY)' rule (699); the same ASIN can also
return JP- or EN-titled rows across runs, so single-sample verification is
unrepresentative. GoPro's /en/in/ (India) locale path shipped USD (716). Preserve locale
params on the request - SFCC ?lang=en_AU kept AUD (309) and Rag & Bone carried ?gc=IN -
but note 318 still shipped USD with the geo param present, so the param is request
context, not the currency source. Verify by recomputation when you can: NetSuite
MSRP $59.00 x 1.1 AU GST = $64.90 matched onlinecustomerprice_detail, confirming field
and tax assumption in one step (679). Emit currency as its own field and record WHICH
node it came from.

## Learned: Price is a NUMBER with currency separate - guards, precedence, no symbol strings
**Source:** job 685, job 897, job 640, job 362, job 405, job 920, job 166, job 280, job 713

24 of the 408 harvested jobs shipped a '$59.95'-style STRING as price (166 and 280:
'$59.95', 713: '$345.00', 309: '$699.00', 318: '$398.00', 283: '$39.95', 152: '$99.95')
and every one of them PASSed the tester - symbol-joined strings are the single most
repeated schema defect in the wave. Emit price as a number and currency as its own
field. Embedded price arrays need a documented precedence, not offers[0]: BookOutlet
uses regular_price_usd, falls back to actual_price_usd, and NEVER sale_price_usd (null
off-sale) or list_price_usd (inflated was-price) (685). Guard the degenerate values:
zero/placeholder regexes ^0(?!\.), ^0\.00$, ^\$0$ (362), plus locale forms ^¥0$ and
^undefined for yen inline-symbol prices (405, which shipped ¥14,900 with the symbol
inline); the ^\$?20$ guard on 920 exists because a prior job shipped a literal '20'.
Guard contamination: 99.x membership-fee prices and '$'/Case unit suffixes on B2B (640);
a hasDiscount class on every tile is not a sale signal (897). Which node is CURRENT
beats which node is structured.

## Learned: Runner integrity - the record is the item, the artifact is the validated one
**Source:** job 607, job 675, job 679, job 610, job 682, job 283, job 320, job 676, job 609, job 293, job 542

record.url must equal the requested item: 607 shipped ONE record whose url was
/products/rise-ai-giftcard vs src_url = the seeded pajamas PDP - a nav link was scraped
instead of the seed. Assert the delivered artifact IS the tester-validated one: 675
PASSed a 12-item full-field file and run_execution wrote a different 10-item file
missing title/currency/availability; 679 delivered 4 items vs the tester's 38; 610's
file is dated 5 days after the run (4 vs 10). The strategy label must name the
VERIFIED rung: 682 recorded Method fingerprint_safari184_none while the HTTP method was
chrome_residential, 283 recorded safari184_none vs direct_http_datacenter, 320's probe
said 'Method: cloak_none' with status 403 - downstream inherits the wrong verdict.
Browser-service 429 is infrastructure, never a strategy trigger: 676's CRASH (0 samples)
retested and the IDENTICAL code PASSed; 675 reused surviving discovery; 609 burned its
draft-run budget on 429s, finalizing by static review. Missing site_analysis.json must
hard-fail the step: 682 re-derived 5 corrections, 283 burned 846s of product_analysis,
293 wrote its own site_analysis_review. Re-verify 'no pagination' under the real
scraper: 542's probe declared ?No= offsets carousel-only, the live run found 148 URLs
over No=0,48,96,144.
