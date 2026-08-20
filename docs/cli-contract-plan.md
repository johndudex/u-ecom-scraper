# Plan — Scraper CLI-contract enforcement (guard + prompt), v2 post-critique

> Status: **v2 — critique round 1 folded in. Every CONFIRMED flaw re-verified by the
> critique agent against source + repo artifacts before acceptance.**
> Trigger: Railway job 7 (2026-08-20, rmwilliams chelsea-boots, litellm code_writer) —
> generated scraper renamed the discovery CLI (`--query`) and dropped `--listing-url` /
> `--fresh-discovery` / the `SCRAPER_LISTING_URL` env read → flags stripped →
> seed-only execution → job "completed" with **1 product**.
> v1 verdict: NO-GO (3 HIGH flaws, 6 inter-planner contradictions, 1 pre-existing
> template bug two edits were unknowingly built on). All fixed below.

## Root cause (verified)

1. **The prompt contradicts the template.** `build_code_writer_message`'s "Required
   CLI Arguments" (subagents.py:3070-3076) enumerates only
   `--input/--urls/--sample/--limit` — every discovery flag omitted. The enumerated
   list reads as authoritative → the model prunes the template's "extra" flags.
   Same incomplete list in `.opencode/agents/code-writer.md:128-129`.
2. **The tester is structurally blind**: forbidden from reading the draft
   (subagents.py:3308-3310), told to mark `phase1_discovery: true` even when
   unvalidated (:3305-3307), tests in seed mode.
3. **The only deterministic guard logs and proceeds** (run_execution.py:328-361).
4. `_enforce_env_discovery_gate` (graph.py:369-421) no-ops when the whole consumer
   was dropped — job 7's exact shape.
5. `_probe_phase1_discovery`'s check (`rc != 0 and "Traceback" in stderr`,
   graph.py:3197) misses argparse exit(2) — and see pre-existing bug P0 below.

## Pre-existing bugs to fix FIRST (found by critique, proven from artifacts)

- **P0 — `playwright_scraper.py --discover-only` is broken by construction.** The
  main() gate closes the browser at :334; the discover-only block (:362-403) then
  calls `discover_item_urls(page, ...)` on the CLOSED page at :383 →
  `_discovery_goto` swallows "target closed" (src/discovery.py:337-342) → exits 0
  with `found=0, stop_reason=navigate_error`, no Traceback. **Proof:**
  `scrapers/rmwilliams-com-au/output_2026-08-19_204624.json` and `..._184904.json`
  are both exactly `{"products": [], "total_discovered": 0, "stop_reason":
  "navigate_error"}`. Fix: hoist the discover-only check inside the live `with`
  block (or re-open a page). Without this, Edit 6 fails every playwright-family
  draft and the exit-2 hook targets a probe that never fails loudly.
- **P1 — probe-crash bounce is already half-dead (live bug).** graph.py:3320-3322
  inserts `{"severity": "high", "message": ...}` but `_summarize_test_report` reads
  `i.get("field"/"description")` (subagents.py:1978-1990) → today's probe-crash
  feedback renders as `` `?`: `` + empty body. Fix together with Edit 7 (one issue
  vocabulary).
- **P2 — `run_execution.py:301` UnboundLocalError**: `_working_url`/`_listing_reached`/
  `_respect_flag` are bound only inside the `elif input_mode in ("navigation",
  "list_page")` branch (:219-276) but read at :301 (env block, runs for search_term
  too). A search_term job with empty `discovery.listing_url` crashes the node.
  Fix while editing that function for L3.

## Shared foundation — `webapp/agents/constants.py` (verified true leaf, zero imports)

```python
CONTRACT_VIOLATION_MARKER = "CLI CONTRACT VIOLATION"
SCRAPER_ENV_LISTING = "SCRAPER_LISTING_URL"
NAV_INPUT_MODES = frozenset({"navigation", "list_page", "search_term"})
API_STRATEGIES = frozenset({"api", "internal_api"})   # + "shopify_api"? cosmetic, decide at impl

def required_cli_flags(input_mode, strategy="") -> tuple[str, ...]:
    """PROMPT-side enumeration — STRATEGY-AWARE (v2): must be ⊆ what the selected
    template family actually declares, or the anti-drift test fails day one.
    url_list → base 4 (--input/--urls/--sample/--limit);
    nav + playwright/http_navigation/navigation family → base + --fresh-discovery
      --discover-only --listing-url (search_term: --query instead);
    nav + api family → base + --fresh-discovery ONLY (api has no listing page);
    nav + ssr_div_list → base + --listing-url ONLY (template declares neither
      --fresh-discovery nor --discover-only — do NOT advertise them);
    UC/shopify → base 4 only (no discovery flags — header says so)."""
```

Anti-drift test parses each template with `_accepted_cli_flags`'s AST walk and
asserts `required_cli_flags(mode, strategy) ⊆ declared` **per template family**
(v1's blanket assertion failed day one against ssr_div_list — contradiction #1).

## FIX 1 — deterministic guard

### The checker: `cli_contract_violation(path, input_mode, strategy) -> str | None`

Beside `_accepted_cli_flags` (run_execution.py). Comment-stripped AST check,
mode-gated, satisfied by ANY of:

- **M0 (redefined v2)** "cannot silently degrade to seed-only": no `input_urls.json`/
  `INPUT_FILE` seed branch (unconditional discovery) — OR an honest-crash main()
  (UC: exits 1 with no URLs, never silent). UC passes M0 **for the right reason**.
- **M1** reads `SCRAPER_LISTING_URL` env — satisfying ONLY for non-api strategies.
- **M2 — DEMOTED (v2, critique vector 1A):** reading `SCRAPER_FORCE_DISCOVERY` is a
  *signal*, never satisfying. That env var is set NOWHERE in the repo (grep: only
  readers api_scraper.py:277, playwright_scraper.py:320, the patcher graph.py:414).
  api family's real execution trigger is the `--fresh-discovery` flag
  (api_scraper.py:276-289), always appended by run_execution.py:285. So:
- **M3** declares `--listing-url` AND consumes `args.listing_url` — or, **for api
  strategies: declares `--fresh-discovery` AND consumes `args.fresh_discovery`** (v2).
- **M4** (search_term only) declares `--query` AND consumes `args.query`.

`url_list` exempt; unparseable → None (syntax fixer owns it).

> M1-only drafts are execution-safe on playwright/http_navigation/navigation/
> requests/ssr (critique verified: the env gates at http_navigation:1070-1075,
> navigation:749-753, requests:399-403, ssr:223-225 set `args.fresh_discovery = True`
> themselves, replicating the checkpoint skip; requests declares it a no-op;
> playwright has no checkpoint). The guard asymmetry stands — corrected only for api.

### L1 — self-heal in `_invoke_code_writer` (PRIMARY, after `_fix_scraper_syntax` :3097)

`_enforce_cli_contract`: the `_fix_scraper_syntax` pattern (:2827-2882) — same agent
object, ONE HumanMessage, fresh invoke, max 2 attempts, re-AST-check each time.
**v2: the message renders the env-gate snippet FROM THE SELECTED TEMPLATE**
(`_select_template_file` state, graph.py:2889-2929 — playwright shape
`global PRODUCT_LISTING_URL` vs http_navigation family shape
`args.listing_url = _env_listing; args.fresh_discovery = True` are both live; a
playwright-shaped instruction on an http_navigation draft produces a Frankenstein
gate). Instruction: "re-add the gate **in the shape YOUR template's main() uses**
(your system prompt contains the full file); do NOT introduce a foreign shape;
edit_file only; do NOT regenerate." The full `CONTRACT_VIOLATION_FEEDBACK` text is
published in this plan's implementation notes, not deferred.

### L2 — hard gate: tester force-FAIL is the LOAD-BEARING closure (v2 correction)

- Tester (graph.py, beside `_probe_phase1_discovery`): after the LLM runs, re-check
  the draft; on violation force `overall_assessment="FAIL"`,
  `ready_for_execution=False`, prepend `{"severity": "high", "message": ...}` with
  the marker, set `feedback_for_writer` (**string** — v2 decision; update
  code-tester.md:144-153 which documents an object schema). A forced FAIL skips the
  PASS exit at route_after_testing.py:**496** — which is where job 7 actually
  escaped (v1 said :507; wrong: PASS reports with confidence ≥0.85 and no high
  severity return at :496 before :507 is ever reached, and the :468-481 phase gate
  is neutralized by the phase1-lie instruction).
- `route_after_testing.py`: add `not _contract_bad` to BOTH the :496 PASS condition
  AND the :507 ground-truth override (belt and braces); new branch after the
  `is_final_attempt` block (:514-521) with its OWN exhaustion check (the existing
  exhausted block at :566 is below): violation + retries left → `code_writer`;
  exhausted → `human_approval` (skip_approvals → `cleanup`, mirroring :587-592).
- **Boundedness verified (critique vector 3c):** route returns strings only; the
  bump at graph.py:2942-2959 fires whenever `state["test_report"]` is truthy — the
  forced FAIL guarantees it. Exactly 2 bounces, 3 code_writer runs, then exhausted.
- Accepted shadow (conscious): a contract-violating draft with a *genuine* strategy
  failure goes to code_writer before scraper_analyzer (contract beats
  classify_test_failure at :531) — burns ≤2 retries on contract fixes first.

### L3 — `run_execution` honesty floor (:335-361) + residual documentation

Discovery-critical flags stripped AND draft violates → `execution_status=FAILED` +
actionable error. Behind `DISCOVERY_CONTRACT_STRICT` (default True). Compliant
draft + stripped flags → today's behavior.

**Documented out-of-scope residual (critique vector 9):** navigation job where the
browser_traverse fallback's preconditions fail (no `method_that_worked`) →
`discovery.listing_url` empty → only `--fresh-discovery` passed, nothing stripped,
L3's precondition false → a declaring draft runs seed-only silently. The guard
calls it compliant (M1/M3), F9 won't fire (few high-quality items), no exit-2.
Mitigation option (deferred, note only): nav-mode + no `ran_phase1` signal +
item_count ≤ seed_count → FAILED.

## FIX 2 — prompt/message reinforcement (7 edits, ≈+2,150 chars steady-state)

1. **REQUIRED** "Required CLI Arguments" → complete, mode- AND strategy-conditional
   HARD CONTRACT rendered from `required_cli_flags()`; "ADD to the template's
   add_argument block, never replace it".
2. **REQUIRED** "Template fidelity" +1 sentence: argparse block + env read are CONTRACT.
3. **REQUIRED** code-writer.md:128-129 → the conditional set.
4. **REQUIRED** code-writer.md rule 4 rewrite — covers BOTH gate shapes; states the
   flag declarations are protected by nothing but the writer.
5. **REQUIRED** `# CLI CONTRACT` header above `def main():` — **5 templates**
   (playwright, http_navigation, navigation, requests, api [own variant]);
   ssr_div_list gets its own true set (no --fresh-discovery/--discover-only);
   UC/shopify get an honest note (flags stripped on nav jobs; UC exits 1 — no
   silent seed-only). No braces in header text.
6. **REQUIRED (v2-restricted)** code_tester Phase-1 step → run discovery as
   execution does, **but pass only flags THIS draft declares** (reuse
   `_accepted_cli_flags` in the instruction's construction or pre-compute the
   arg list deterministically in the message builder — do NOT have the LLM guess;
   run_scraper does NOT strip, so a passed-but-undeclared flag manufactures an
   exit-2 execution would never see — critique vector 5). Keep `--limit` (a small
   N, e.g. 50) in the probe invocation — http_navigation's `--discover-only` runs
   Phase 1 to exhaustion against `DISCOVERY_DEADLINE_SECONDS=300`
   (http_navigation_scraper.py:138) and run_scraper's timeout is 300s
   (shell_tools.py:164); uncapped = killed-at-deadline = false crash. argparse
   exit 2 → HIGH issue, marker-prefixed, target "scraper". Remove the
   phase1-lie instruction (:3305-3307). **Depends on P0 fix.**
7. **REQUIRED** `_summarize_test_report` — normalize the issue relay: read
   `message` OR `description` OR `problem` (three vocabularies are live today);
   relay marked issues verbatim into the retry seed. Fixes P1 in the same change.

## Hand-offs (1-3 line bonus fixes)

- `_probe_phase1_discovery` (graph.py:3197): treat `rc == 2 and "unrecognized
  arguments" in stderr` as a contract violation (second free detector). **Depends
  on P0** — the probe currently exits 0 with garbage on playwright drafts.
- `_attach_discovery_coverage` (graph.py:540) reads the NEWEST output, not the
  best — after P0 it can pick up a navigate_error discover-only file and downgrade
  a healthy job via `_COVERAGE_FAIL_STOP_REASONS` (route_after_testing.py:76).
  Guard it to skip discover-only artifacts.
- Delete dead `.opencode/agents/code-writer-v1.md` eventually; do not edit.
- Deploy note: `prompts.py:16 _PROMPT_CACHE` — .md edits need a worker restart.

## Tests

`tests/test_cli_contract.py` (guard): url_list exempt; job-7 shape (query-only nav)
violates; **api draft with env-read-only violates (M2 demotion regression)**; api
draft with --fresh-discovery declared+consumed passes; **UC draft passes M0 via
honest-crash wording**; all template families compliant per their OWN declaration
sets (ssr_div_list asserts its reduced set); comment-mention doesn't satisfy;
unparseable → None; route blocks PASS exit (:496) and ground-truth (:507);
exhausted → human_approval / skip_approvals → cleanup; L3 fails honestly; L1
bounded (exactly max_tries) + message contains the SELECTED template's gate shape.

`tests/test_cli_contract_prompt.py` (prompt): constants ↔ templates anti-drift
**per family**; writer message carries contract; url_list message has no discovery
flags; api strategy omits --listing-url; ssr strategy omits --fresh-discovery;
.md staleness tripwire; template headers present + accurate per family; tester
message's pre-computed args ⊆ draft-declared flags; issue-relay normalizes all
three keys; "Template fidelity" retained.

Plus: P0 regression test (playwright --discover-only on a live page stub yields
found>0 or an honest error — not silent navigate_error/0).

## E2E validation (v2 — falsifiable)

Re-run rmwilliams chelsea-boots (job 7's config). Accept:
1. `product_count >= 5` (hard floor; NOT "tens" — cross-sell contamination makes
   exact counts non-falsifiable).
2. **Every** output row's `src_url` == the discovery listing URL actually used
   (`.../footwear/men/chelsea-boots...`) — this is the real signal; job 7's
   failure had src_url = the seed detail page.
3. Worker logs: no `DISCOVERY-CRITICAL flags stripped`; `--listing-url` passed +
   `SCRAPER_LISTING_URL` set.
4. ~~stop_reason assertion~~ — dropped: playwright full runs don't emit
   `discovery_coverage` at all (proven: the compliant 20-product
   output_204813.json metadata has none). OPTIONAL follow-up: teach
   playwright_scraper.py:463-476 to emit it (5 lines, makes _read_discovery_coverage
   useful for this family).

Regression: one url_list job (adameve-class) unchanged; one search_term API job
(aya-class) still passes.

## Files touched (implementation order)

1. `templates/playwright_scraper.py` (P0) + regression test
2. `webapp/agents/nodes/run_execution.py` (P2 fix, checker, L3)
3. `webapp/agents/constants.py` (shared vocabulary)
4. `webapp/agents/graph.py` (L1 `_enforce_cli_contract`, L2 tester block,
   probe exit-2 hook, `_attach_discovery_coverage` guard)
5. `webapp/agents/nodes/route_after_testing.py` (L2 routing, :496 + :507)
6. `webapp/agents/subagents.py` (Edits 1, 2, 6, 7)
7. `.opencode/agents/code-writer.md` (Edits 3, 4), `.opencode/agents/code-tester.md`
8. `templates/{5 headers + 2 notes}`
9. `webapp/config/settings.py` (`DISCOVERY_CONTRACT_STRICT`)
10. `tests/test_cli_contract.py`, `tests/test_cli_contract_prompt.py`

## Critique-round log

- **Round 1 (2026-08-20):** NO-GO. 10 vectors: 5 CONFIRMED (api M2 false-pass —
  SCRAPER_FORCE_DISCOVERY never set anywhere; L1 gate-shape gap; issue-shape
  mismatch already live in the probe-crash path; Edit 6 manufactures exit-2 +
  rides the broken playwright --discover-only, proven by two repo artifacts;
  e2e criterion unevaluable — playwright emits no discovery_coverage), 3 REFUTED
  (constants leaf sound; template-header edits safe; M1-only drafts genuinely
  execution-safe outside api), 1 partial (loop boundedness confirmed — but job 7
  escaped at :496 not :507; the tester force-FAIL is the load-bearing closure).
  6 inter-planner contradictions resolved (ssr_div_list broke the blanket
  anti-drift test day one; three issue-key vocabularies; feedback_for_writer
  string-vs-object; escape-route narrative; "8 templates" was 9; UC true/false
  positive wording). 3 pre-existing bugs surfaced (P0 playwright discover-only
  closed-page; P1 probe-crash bounce renders empty; P2 search_term
  UnboundLocalError at run_execution.py:301). v2 folds all of it.
