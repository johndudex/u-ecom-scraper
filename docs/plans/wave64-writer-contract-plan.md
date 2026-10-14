# Wave-64: Writer contract adherence — make the system-borne exec budget actually reach drafts

**Status**: COMPLETE — e2e 1/1 PASS (2026-10-14). Commit `24402fc` (F1 alias belt + F2 writer prompt
contract + F3 position-safe contract-env scan) + `8e24735` gitignore chore. TDD
`tests/test_wave64_exec_budget.py`→`test_wave64_writer_contract.py` 16/16; full gate after the F3 RCA
fix: **5115 passed / 5 baseline known-reds / 11 collection errors** (exact baseline arithmetic). The
first gate caught a real F3 wiring bug — `report = report or {}` manufactured a test_report on the
dead-tester `test_report=None` path (job-81 stale-report rejection); fixed by gating on
`isinstance(report, dict)` + regression pin `test_graph_wiring_never_manufactures_a_report`; job81
27/27 green after.

**e2e (job 666, jomashop watches.html re-fire)**: COMPLETED **pc=3426** — 3426/3426 on ALL FOUR core
fields, 3426 unique titles + urls, tester PASS 0.97 pre-execution. ALL mechanisms certified live:
draft consumed `SCRAPER_EXEC_TIME_BUDGET` verbatim + startup "Env gate" log (F2);
`exec_time_budget_seconds=3000` — **system-borne bound proven** (663's writer-borne 540 is dead);
`time_budget_bounded` at the discovery_coverage placement; the `EXEC BUDGET` note FIRED via
`_note_budget_bound` (silent in 663). 50,000 URLs discovered, gracefully bounded. Guardian ledger
row 12. jomashop arc: 662 FAIL pc=0 → 663 PASS 92 (writer-borne fuse) → 666 PASS 3426 (system-borne,
fully governed). Merge of waves 57-64 to ExtractorBuilderAi fork main: user-unparked 2026-10-14 —
`129cdd8` sync + `153459b` scratch cleanup + `75321c7` outputs deletion/gitignore (all pushed
`sync-w54:main`, SHA-verified, merge-tree clean vs aimleap/main).

## Critique history

- **critique-1 (REVISE, all findings verified with file:line)**: F1 PASS as designed (helper is the
  single seam — 3 call sites :1883/:2040/:2461; env_overrides pass through bs VERBATIM
  server.py:1978→2896→scraper_runner.py:565-566; in-process helper spreads LAST :2036-2046; no ambient
  SCRAPER_TIME_BUDGET_S setter repo-wide; wave-63 tests pin `{}`-disabled + call-site count only, no
  dict equality → no re-pins). F2 targeted a NONEXISTENT file — live prompt is
  `.opencode/agents/code-writer.md` (hyphen) via AGENT_PROMPT_MAP subagents.py:152-166; insertion after
  "## Template Fidelity — CRITICAL" (:78) or before "## Output Contract" (:261), 346 lines, no
  conflicting budget instructions. F3: script_checks is LLM-written, not Python — the deterministic
  precedent lives at graph.py `_invoke_code_tester` (:~11481-11511 draft_call_violation forces FAIL +
  insert(0); :11055-11070 deterministic_output_issues APPENDS with (field, description) dedupe) —
  position-0 displacement is FORBIDDEN (route_after_testing.py:233-235 reads issues[0] as remediation
  fallback). Adversarial legs: alias-on-legs coherent (540 < 600 wall by design); uncaught variant =
  units misread (name present, read as minutes) → bar 2 becomes a RANGE check + F2 pins "SECONDS";
  SessionLog payload grep is UNOBTAINABLE (nothing logs the request env: browser_http.py:239-280
  silent; runner stages silently) → belt replaced by F2's startup-log rule (stderr → job log carries
  the resolved bound). RCA evidence path corrected to shared-data/scrapers/jomashop-com/scraper.py:117.

## RCA (evidence: job 663, ledger rows 10; job 662, row 7; live artifact
shared-data/scrapers/jomashop-com/scraper.py:117 + :937)

Wave-63's N1 injects `SCRAPER_EXEC_TIME_BUDGET=3000` at all three dispatch sites. Job 663's writer —
having received the job-662 RCA through the fix loop — self-implemented a graceful budget instead of
adapting the template's: it reads **`SCRAPER_TIME_BUDGET_S`** (default **540**, a category-leg value),
placed `time_budget_bounded` at `metadata` top level (not `metadata.discovery_coverage`), and skipped
the two-deadline reserve math, the 25-item flush, and the harvest hooks. Result: the flagship PASSED
via a 540s writer-guessed bound that happens to clear the ≥10-row bar on jomashop today; on a bigger
catalogue, or on a writer roll that bounds tighter/looser, the same class lands 0-or-hollow rows, and
run_execution still has no control over the bound. The contract env exists; drafts ignore it.

Three defects, one class: (D1) the injected env name is singular — writer-borne variants reading a
*different* name get nothing; (D2) the writer prompt never says the budget env is a **contract**
(verbatim consumption, seconds units, no invented budgets, template machinery is the base); (D3)
nothing checks a draft for contract-env consumption before execution.

## Fixes

1. **F1 — alias belt (run_execution, unchanged from v1 — critique PASS)**: `_exec_budget_env()`
   (:746-775) returns BOTH `SCRAPER_EXEC_TIME_BUDGET` AND alias `SCRAPER_TIME_BUDGET_S` (same value,
   str()). Disabled ⇒ `{}` (no alias leakage). Templates must NOT read the alias (draft-side only —
   critique-verified templates read the primary name only, navigation_scraper.py:992,
   http_navigation_scraper.py:1722).
2. **F2 — writer prompt rule (`.opencode/agents/code-writer.md`, THE HYPHENATED PATH)**: contract
   block inserted after "## Template Fidelity — CRITICAL" (fallback: before "## Output Contract"):
   when adapting templates for long listings, consume `SCRAPER_EXEC_TIME_BUDGET` (alias
   `SCRAPER_TIME_BUDGET_S`) verbatim as the extraction time-budget — **units are SECONDS**; never
   invent a different env name; never hard-default a budget (absent env ⇒ unlimited, matching
   navigation_scraper.py:279-283); keep the template's two-deadline structure and periodic flush;
   record the bound at `metadata.discovery_coverage.time_budget_bounded` (+ resolved seconds); **log
   the resolved bound once at startup** (stderr, mirroring the template's "Env gate:" line) so the job
   log carries the proof.
3. **F3 — code_tester deterministic contract scan (position-safe, warning-only)**: pure function in
   `webapp/agents/draft_safety.py` (e.g. `missing_contract_env_issue(source)`) → returns an issue dict
   in the **deterministic_output_issues shape** (`issue_type: "missing_contract_env"`, severity
   `"medium"`, field `time_budget`, human description) when `SCRAPER_EXEC_TIME_BUDGET` is absent from
   the draft source. Wired at graph.py `_invoke_code_tester`'s deterministic-scan block (:11055-11070
   / :11498-11511) **APPENDED to the tail — never inserted at position 0** (issues[0] is the
   remediation-message fallback, route_after_testing.py:233-235). MEDIUM ⇒ non-veto by existing
   routing (route_after_testing.py:648) — warning-only, no verdict-math change; `MISSING_CONTRACT_ENV`
   semantics carried by the new issue_type value (nothing enumerates the set in code). No test pins
   script_checks shape (critique grep: zero hits) — nothing to re-pin there.

## MUST-NOT

No hard-fail gate on F3 (warning-only; position-0 insertion forbidden); no new env names beyond the
one alias; templates must not read the alias; no wave-63 template-machinery rewrites; no LLM in the
check; scope=firstn/filter, probe lane, wall-clock settings untouched; N2 harvest semantics untouched.

## Tests (TDD — `tests/test_wave64_writer_contract.py`)

- F1: helper returns both keys with equal str values when enabled; `{}` when disabled/0/invalid; ≥3
  `_exec_budget_env(` call sites unchanged; templates contain NO alias read (negative pin).
- F2: `.opencode/agents/code-writer.md` (exact hyphenated path, read via Path — fails loud if
  renamed) contains `SCRAPER_EXEC_TIME_BUDGET`, `SCRAPER_TIME_BUDGET_S`, "SECONDS", "never invent",
  `time_budget_bounded`, and the startup-log rule anchor.
- F3: pure-function unit tests (absent name → issue dict with issue_type/severity=medium; present
  name → None; empty source → issue); graph wiring pin: the call site APPENDS (no `insert(0` in the
  new block) and the function lives in draft_safety.py.
- Re-pin pass: `grep -rl "draft_call_violation\|deterministic_output_issues" tests/ webapp/tests/` —
  verify-then-edit before the certifying suite; wave-63 suite re-run as regression.

## Acceptance bars

1. Targeted suite green + lint clean (webapp/ + src/).
2. e2e (≤2 cap): re-fire jomashop watches.html — bar: COMPLETED, ≥10 rows @~100% core, AND the bound
   PROVEN system-borne: recorded `exec_time_budget_seconds`/`time_budget_seconds` ∈
   **[3000, 6000]** (writer's 540 default would prove the alias did NOT reach the draft) OR the
   startup "resolved budget" line visible in the job log showing 3000. Bounded-or-not both acceptable
   (small finishes under budget are fine); what must be true is that the DRAFT consumed the system
   value.
3. Verdict gate + ledger row on landing; plan doc status update; memory update.
