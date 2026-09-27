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
