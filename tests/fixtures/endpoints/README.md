# Endpoint descriptor fixtures (job-12 fix plan S2 golden replay)

One JSON per recorded api_endpoint descriptor, shaped like the
`api_endpoint` entry of `navigation_analysis.json`. `expected_internal_api`
is the verdict the strategy gate MUST produce; the golden-replay test
(`tests/test_job12_strategy_gate.py::test_golden_replay_fixture_verdicts`)
runs every fixture through `_derive_strategy` and asserts it.

Sources: prod forensics 2026-08-27 (workspace + File Master artifacts,
both repos), transcribed into fixtures so the verdicts are replayable in
CI without network access. `amn-jobs-api.json` additionally reflects a
live read-only probe of the endpoint made the same day (count 14627).

| Fixture | count | items_per_page | expected internal_api |
|---|---|---|---|
| ketchcdn-consent-config | null | 5 | false |
| useinsider-personalization | null | 1 | false |
| coveo-explicit-zero | 0 | — | false |
| zquiet-heatmap | null | — | false |
| sidley-taxonomy | null | 100 | false |
| shopify-feed-legit-no-total | null | 30 | false (honest downgrade — see note) |
| aya-jobs-api | 26955 | 5 | true |
| amn-jobs-api | 14627 | 10 | true |

