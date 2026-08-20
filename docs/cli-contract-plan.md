# Plan — Scraper CLI-contract enforcement (guard + prompt), v1 pre-critique

> Status: **v1 — two deep planners (guard, prompt) folded in. Critique round pending.**
> Trigger: Railway job 7 (2026-08-20, rmwilliams chelsea-boots, litellm code_writer) —
> generated scraper renamed the discovery CLI (`--query`) and dropped `--listing-url` /
> `--fresh-discovery` / the `SCRAPER_LISTING_URL` env read. run_execution's guard
> stripped the undeclared flags, scraper fell back to `input_urls.json` (1 seed URL),
> job "completed" with **1 product** instead of the full listing.

## Root cause (verified from source, both agents agree)

Three-way hand-maintained inconsistency + no hard gate:

1. **The prompt contradicts itself.** `build_code_writer_message`'s "Required CLI
   Arguments" (subagents.py:3070-3076) enumerates ONLY `--input/--urls/--sample/--limit`
   — every discovery flag omitted. The template (which declares them) says one thing,
   the task message's authoritative-looking list says another → a reasoning model
   satisfies the enumeration and prunes the "extra" flags. Same incomplete list in
   `.opencode/agents/code-writer.md:128-129`.
2. **The tester cannot catch it.** It's forbidden from reading the draft
   (subagents.py:3308-3310), the prompt tells it to mark
   `phase1_discovery: true` even when unvalidated (subagents.py:3305-3307), and its
   test invocation uses `--sample` seed mode — never the flags execution passes.
3. **The only deterministic guard logs and proceeds**
   (run_execution.py:328-361 `DISCOVERY-CRITICAL flags stripped` → warning only).
4. `_enforce_env_discovery_gate` (graph.py:369-421) no-ops when the whole env-gate
   consumer was dropped — exactly job 7's shape.
5. `_probe_phase1_discovery`'s failure check (`rc != 0 and "Traceback" in stderr`,
   graph.py:3197) misses argparse exit(2) — no Traceback in that stderr.

## Shared foundation — `webapp/agents/constants.py` (leaf module, no imports)

Both fixes consume ONE vocabulary so they can never drift apart:

```python
SCRAPER_ENV_LISTING = "SCRAPER_LISTING_URL"
SCRAPER_ENV_FORCE = "SCRAPER_FORCE_DISCOVERY"
CONTRACT_VIOLATION_MARKER = "CLI CONTRACT VIOLATION"
NAV_INPUT_MODES = frozenset({"navigation", "list_page", "search_term"})
API_STRATEGIES = frozenset({"api", "internal_api"})

def required_cli_flags(input_mode, strategy="") -> tuple[str, ...]:
    """PROMPT-side enumeration (strict): every flag the dispatcher passes that
    the selected template family declares. url_list → base 4; nav modes →
    + --fresh-discovery --discover-only (+ --listing-url | --query);
    api strategy → + --fresh-discovery ONLY (no listing page — aya class)."""

CONTRACT_VIOLATION_FEEDBACK = "...(see §L1 message; single source)..."
```

An anti-drift test parses each template with `_accepted_cli_flags`'s own AST walk
and asserts `required_cli_flags(mode, strategy) ⊆ declared` — renaming a flag in a
template without updating the constant fails CI, and vice versa.

## FIX 1 — deterministic guard (3 layers, no new topology)

### The checker: `cli_contract_violation(path, input_mode) -> str | None`

In `run_execution.py` beside `_accepted_cli_flags` (pure AST, reuses it on the DRAFT
— already proven pre-execution at graph.py:3140). **Wiring-based, mode-gated — NOT a
hardcoded flag list** (validated against ALL 8 templates: api_scraper legitimately
reads `SCRAPER_FORCE_DISCOVERY` not the listing env; UC/shopify templates have no
discovery flags at all and a UC-based nav draft IS broken today — true positive).

Comment-stripped source check; satisfied by ANY of:

- **M0** no seed-file branch (`input_urls.json`/`INPUT_FILE` absent) — unconditional discovery
- **M1** reads `SCRAPER_LISTING_URL` env
- **M2** reads `SCRAPER_FORCE_DISCOVERY` env (api/internal_api family)
- **M3** declares `--listing-url` AND consumes `args.listing_url`
- **M4** (search_term only) declares `--query` AND consumes `args.query`

`url_list` exempt; unparseable draft → None (syntax fixer owns it); comments don't
satisfy (stripped first).

> **Tension flagged for critique:** the guard is *wiring-loose* (any trigger satisfies)
> while the prompt is *enumeration-strict* (all dispatcher-passed flags declared).
> Rationale: a draft reading only the env var works at execution (env carries the
> listing; the stripped `--listing-url` is then harmless) — bouncing it would be a
> false positive; but the prompt should still teach the full surface to minimize
> strip-noise. Critique should attack this asymmetry.

### L1 — self-heal in `_invoke_code_writer` (PRIMARY, graph.py after :3097)

`_enforce_cli_contract(...)`: the `_fix_scraper_syntax` pattern (:2827-2882) — same
agent object, ONE targeted HumanMessage, fresh invoke (no context accumulation —
this is the blessed bounded-fix pattern, NOT the reverted rescrape-routing), max 2
attempts, re-AST-check each time. Message = CONTRACT_VIOLATION_FEEDBACK: names the
exact violation, cites the template in the system prompt, instructs edit_file-only
add of the argparse lines + env gate, "do NOT regenerate".

Placement beats alternatives: (b) tester/route alone wastes a 5-10min tester LLM run
on a statically-predictable failure; (c) run_execution is post-approval, static edge
`run_execution → cleanup` — no loop back. Command(goto) from code_writer rejected —
D6 shadow-branch hazard (graph.py:4206-4208). Deterministic flag *injection* rejected
— it would silence the existing DISCOVERY-CRITICAL tripwire while discovery stays
dead (declaration ≠ wiring).

### L2 — hard gate: `_invoke_code_tester` + `route_after_testing`

- Tester (graph.py:3310-3334, beside `_probe_phase1_discovery`): re-check the draft
  AFTER the LLM runs; on violation force `overall_assessment=FAIL`,
  `ready_for_execution=False`, prepend a high issue with the marker, set
  `feedback_for_writer` = CONTRACT_VIOLATION_FEEDBACK; F19-pattern honest error at
  retry exhaustion.
- `route_after_testing.py`: compute `_contract_bad` near :455; **plug the ground-truth
  laundering hole** at :507 (`not _contract_bad` joins the override condition — job 7
  escaped exactly this way: PASS report + ≥3 real sample items); new branch after the
  `is_final_attempt → human_approval` block: violation + retries left → `code_writer`;
  exhausted → `human_approval` (or `cleanup` under skip_approvals, mirroring :587-592).
  Rides the existing `test_retry_count`/MAX_TEST_RETRIES=2 budget — no new counters.

### L3 — `run_execution` honesty floor (:335-361)

Discovery-critical flags stripped AND draft violates the contract → return
`execution_status=FAILED` + actionable `error_message` (never a silent seed-only
"completed"). Behind `DISCOVERY_CONTRACT_STRICT` kill-switch (default True, mirrors
`RESPECT_LISTING_REACHED_FLAG`). Compliant draft + stripped flags → today's behavior.

## FIX 2 — prompt/message reinforcement (7 edits, ≈+2,150 chars steady-state)

All static seed text — does not participate in the loop-growth mechanism
(docs/code-writer-context-ballooning.md).

1. **REQUIRED** `build_code_writer_message` "Required CLI Arguments" → complete,
   mode-conditional HARD CONTRACT rendered from `required_cli_flags()`;
   "ADD to the template's add_argument block, never replace it".
2. **REQUIRED** "Template fidelity" block (+1 sentence): the argparse block + env
   read are CONTRACT, not boilerplate.
3. **REQUIRED** `.opencode/agents/code-writer.md:128-129` → the conditional set.
4. **REQUIRED** `code-writer.md` rule 4 rewrite — current text shows only the
   playwright gate shape and over-promises the patcher ("backstops this" — it
   no-ops on the job-7 shape). New text covers both template shapes and states
   the flag declarations are protected by nothing but the writer.
5. **REQUIRED** `# CLI CONTRACT` header above `def main():` in the 6 discovery
   templates (template source is injected whole into the system prompt —
   subagents.py:664-675 — so this is read with certainty). Per-template truth
   (api_scraper gets its own "no listing page" variant).
6. **REQUIRED** `build_code_tester_message` Phase-1 step → run discovery EXACTLY as
   execution does (`--listing-url/--fresh-discovery/--discover-only`; `--query` for
   search_term; api → `--fresh-discovery` only); argparse exit 2 → HIGH issue whose
   problem starts with `CLI CONTRACT VIOLATION:`, target "scraper". Replacement not
   addition (preserves the 2-run / 10-call budgets). Remove the "mark
   phase1_discovery true even if unvalidated" instruction (:3305-3307).
7. **REQUIRED** `_summarize_test_report`: relay marked issues verbatim into the
   retry seed (the only path test_report issues reach the next code_writer message).

OPTIONAL: tester crash_capture treats exit-2 as a crash; code-tester.md sentence;
`_probe_phase1_discovery` exit-2 hook (see hand-offs).

## Hand-offs discovered during planning (each is a 1-3 line bonus fix)

- `_probe_phase1_discovery` (graph.py:3197): treat `rc == 2 and "unrecognized
  arguments" in stderr` as a contract violation → second free deterministic detector.
- `shell_tools.py:282-286`: env injection only fires when
  `navigation_analysis.discovery.listing_url` populated — fine, but note it.
- `.opencode/agents/code-writer-v1.md` is DEAD (prompt map → "code-writer" only) — do
  not edit it; delete eventually.
- Deploy note: `prompts.py:16 _PROMPT_CACHE` — .md edits need a worker restart on
  Railway or they're judged against a stale prompt.

## Interaction analysis (both agents, reconciled)

- **Reverted-Fix-1 history (context ballooning):** L1 = fresh single-message invoke
  (syntax-fixer pattern, shipped safe); L2 rides the EXISTING retry loop; nothing
  routes code_writer from analysis-phase nodes. Nav rescrapes already wipe workspace
  (check_tracker.py:314-326); list_page/search_term rescrapes of legacy scrapers pay
  one bounded regen — the intended trade, kill-switch de-risks.
- **skip_code_generation rescrapes:** L2 fires on the restored old draft → regen via
  the normal loop (correct: can't demand flags without regen).
- **F18/F19/sentinel:** L2's exhausted path mirrors F19 exactly; sentinel untouched.
- **RESPECT_LISTING_REACHED_FLAG / F17 / _run_category_sources:** orthogonal (static
  check; F17 only prunes URLs; category-url self-guards at :473-478).
- **`feedback_for_writer` is write-only dead code** today (no reader) — L2 gives it a
  reader via `_summarize_test_report` (Edit 7). Do not build anything else on it.

## Tests

`tests/test_cli_contract.py` (guard, 11 cases): url_list exempt; job-7 shape
(query-only nav) violates; ALL 8 templates compliant (false-positive suite: aya/
locumtenens/lw.com classes); search_term query satisfies / navigation doesn't;
comment-mention doesn't satisfy; unparseable → None; no-seed-branch satisfies;
route blocks ground-truth laundering; exhausted → human_approval / skip_approvals →
cleanup; L3 fails honestly; L1 bounded (exactly max_tries).

`tests/test_cli_contract_prompt.py` (prompt, 9 cases): constants ↔ templates
anti-drift tripwire (the load-bearing test); writer message carries contract;
url_list message has NO discovery flags; api strategy omits --listing-url; .md
staleness tripwire; template headers present; tester message uses execution-parity
args; feedback constants render; "Template fidelity" retained.

## E2E validation

Re-run rmwilliams chelsea-boots (job 7's config). Expect: no `DISCOVERY-CRITICAL
flags stripped` in worker logs; `--listing-url` passed + `SCRAPER_LISTING_URL` set;
`metadata.discovery_coverage.stop_reason ∈ {no_next_link, max_pages_hit}` (not
navigate_error); product_count = listing size (tens, NOT 1). Regression: one url_list
job (adameve-class) unchanged; one search_term API job (aya-class) still passes (M2).

## Files touched

`webapp/agents/constants.py`, `webapp/agents/nodes/run_execution.py`,
`webapp/agents/graph.py`, `webapp/agents/nodes/route_after_testing.py`,
`webapp/agents/subagents.py`, `.opencode/agents/code-writer.md`,
`.opencode/agents/code-tester.md`, `templates/{6 discovery templates}`,
`webapp/config/settings.py`, 2 new test files.
