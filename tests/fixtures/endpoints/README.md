# Endpoint descriptor fixtures (job-12 fix plan S2 golden replay)

One JSON per recorded api_endpoint descriptor, shaped like the
`api_endpoint` entry of `navigation_analysis.json`. `expected_internal_api`
is the verdict the strategy gate MUST produce; the golden-replay test
(`tests/test_job12_strategy_gate.py::test_golden_replay_fixture_verdicts`)
runs every fixture through `_derive_strategy` and asserts it.

Sources: prod forensics 2026-08-27 (workspace + File Master artifacts,
both repos), transcribed into fixtures so the verdicts are replayable in
CI without network access.
