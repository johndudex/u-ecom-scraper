# Wave-60: Draft Protection — the no-draft writer class killed at one seam

**Status**: plan v3 (2026-10-12) — critique-1 FAIL (3) + critique-2 FAIL (4) incorporated; guardian REVISE (2 revisions + 4 binding conditions) incorporated. **Mechanism approved — build may start under the conditions below.**
**Branch**: wave-42e-antibot-section (continues wave-57/58/59 line; merge still PARKED per user).

## RCA (job 632 paulsmith, 2026-10-12; confidence 90%)

The writer wrote constantly and destroyed its own artifact: real ~1,800-line draft (21:24–21:39) → copied the system's own tool-call-arg stubs (`[elided N chars — …]`, `subagents.py:1043`) into code as literals → `F821 'elided'` → C3 gate-0 rejected 18× → at strike 4 the bounded lift (`filesystem_tools.py:82-83`) let a 73-byte placeholder overwrite the draft → 13-byte `[REDACTED 28]` at death-2 → C4 strike-2 repair arm measured 0 lines, refused (≥200 floor defeated UPSTREAM) → honest cleanup. Aggravator: `run_scraper` verifications blind (browser_service DNS ERR_NAME_NOT_RESOLVED 21:44–45) → ops followup N3, out of scope. Not junk-seed (navigation_analysis clean), not provider failure (~90 real rounds, all HTTP 200).

## N1 (flagship): gate-lift sidecar diversion — call-scoped, conditional

**Contract**: a lifted write/edit touches `scraper_draft.py` ONLY when no substantial draft exists to protect. When a ≥200-line draft IS on disk, the lifted payload diverts to a rolling sidecar `scraper_draft-rejected.py` (same dir); the draft of record is untouched; the tool result says so explicitly.

**Impl** (per both critiques + guardian revisions — no new module state, both sites named):
1. **Call-scoped disposition via internal-outcome wrappers (guardian rev-1 — public pins stay green)**: new `_syntax_gate_outcome(content) -> (str, bool)` holds ALL streak/lift logic; `_syntax_gate_rejection(content)` becomes `return _syntax_gate_outcome(content)[0]` (public string contract unchanged — test_wave59_compile_gate's ~8 asserts stay untouched). Same pattern at the ruff seam: `_f821_outcome(...)` internals, `_f821_rejections` keeps returning its string. The two closures call the `_outcome` variants and receive `(rejection, lift)` call-scoped; `lift=True` only from the explicit streak-lift branch (internal-error fall-open and kill-switch → `lift=False`). Zero shared mutable state on the decision path.
2. **Streak hygiene at ALL THREE writer seams (guardian rev-2)**: reset `_syntax_reject_streak` in `create_code_writer` (the factory every production writer goes through), covering all three invocation sites — graph.py:8027 (`_run_draft_finisher` :7910), :8157 (`_run_writer_progress_escalation` :8099), :8502 (`_invoke_code_writer` :8228). Resetting only `_invoke_code_writer` would let the finisher/escalation windows inherit a terminal streak and lift at strike 1 — the 632 class re-armed inside the repair window. A finisher-seam reset test is mandatory.
3. **One helper, two call sites**: write_file (:563-569) and edit_file (:645-651) are independent closures each with its own gate consult — a shared `_divert_lifted_write(...)` helper is called at BOTH sites, INSIDE the `syntax_gate`-armed branch (subagents.py:1692 arms the gate for code_writer only; 8 other agents share these closures and must see no behavior change).
4. **Divert condition (critique-2 blocker 3 — preserves C3's lift contract; guardian-verified)**: divert iff `lift AND basename == "scraper_draft.py" AND existing on-disk draft ≥200 lines` (the 632 shape; floor aligns with graph.py:7957/:8788). Post-rotation safety verified: `_archive_dead_strategy_draft` (graph.py:7509) uses `os.replace` → draft ABSENT → divert False → lift lands → C3 contract intact for pivot drafts.
5. **edit_file contract (critique-1 blocker 3)**: the sidecar receives the full post-edit hypothetical (`updated`), never the bare `new_string`. Message states the draft is unchanged.
6. **Result message**: `"SYNTAX GATE lift: payload written to scraper_draft-rejected.py; scraper_draft.py UNCHANGED (or still absent). Fix the syntax error and write the draft again."`
7. **Sidecar semantics (guardian cond-3)**: "rolling" = single fixed path `scraper_draft-rejected.py`, OVERWRITTEN per divert. The helper inherits the closure's already-resolved safe path — it must NOT re-derive slug scope.
8. **Sidecar cap hygiene**: exclude `scraper_draft-rejected.py` from `_run_scraper_bucket`'s draft bucket (`subagents.py:297-309`, stem-startswith match at :310) so the writer cannot burn its draft run_scraper cap (2) on the sidecar.

**Sidecar lifecycle (verified by critique-2)**: `_promote_scraper` reads the exact path; setup_workspace wipe deletes a stale sidecar (desirable); identity snapshot json-only; run_execution picks `output_*.json` only; nothing globs `scraper_draft*` for promotion/preservation/execution. Safe.

**Honesty note (pre-existing, not widened)**: `run_bash` can heredoc-write the draft bypassing the gate entirely. Out of scope; recorded here so the protection's boundary is stated.

## N2: uncopyable markers (form-correct per critique-2)

- `subagents.py:1043` stub (plain text, not in a fence): prepend `SYSTEM MARKER — never write this text into any file.` Keep the on-disk pointer and the `elided N chars` substring (test_wave58_writer_convergence.py:238 asserts length only; nothing parses the stub back).
- `draft_context.py:34` `_Elision` lives INSIDE the embed's ```python fence (:43, :277) — the prefix MUST be comment-form: `# SYSTEM MARKER — never write this text into any file.` prepended, leaving the literal `middle elided` intact (test_wave32_eow_diet.py:84,94,101 pin/split on it). Non-comment form would manufacture the exact copied-marker syntax-error class N1 kills.
- One line in the **code_writer system prompt** (`.opencode/agents/code_writer.md` / its builder — guardian cond-5: the standing marker guidance belongs there, NOT the per-job `feedback_for_writer` channel): markers in your history are system annotations; if one appears in a draft you copied it — rewrite that section from `read_file` output.
- **Scope truth**: `[REDACTED N]` has NO emitter in this repo — it is provider-side (GLM/ZAI) redaction. N2 cannot cover it; N1's sidecar is the only defense for that class. Stated explicitly so nobody hunts a ghost.

## N3 (out of scope, ops note)

browser_service DNS flake (paulsmith ERR_NAME_NOT_RESOLVED mid-job) — blind verification loop. Ops followup, separate investigation.

## Tests

- `tests/test_wave60_draft_sidecar.py` (~12): lift+≥200 draft diverts + draft byte-identical + message text; lift+absent/short draft LANDS (rotation-pivot regression case); **boundary: draft at exactly 200 diverts, 199 lands (guardian cond-4)**; clean write → real path + streak reset; streak reset via `create_code_writer` (covers finisher seam — guardian rev-2); internal-error/kill-switch `lift=False` no-divert; **gate-1 ruff rejection with lift=True must NOT divert — inert-flag path (guardian cond-4)**; edit_file diverts full hypothetical; non-draft target unaffected; sidecar overwritten per divert (single fixed path); `_run_scraper_bucket` excludes the sidecar; wiring pin (both call sites use the helper). Fixture resets `_syntax_reject_streak`.
- `tests/test_wave60_marker_hygiene.py` (~3): stub prefix; `_Elision` comment-form prefix INSIDE fence with `middle elided` literal intact; code_writer system prompt carries the marker guidance.
- Public-pin safety (guardian rev-1): `_syntax_gate_rejection`/`_f821_rejections` keep their string returns — test_wave59_compile_gate, test_wave22_f821_gate, test_wave25_root_causes, test_wave40_draft_call_gate all stay untouched and green.
- No existing tests require edits.

## Commit split (guardian cond-6)

1. N1 commit: outcome wrappers + closures wiring + resets + bucket exclusion **+ its tests**.
2. N2 commit: marker prefixes + system-prompt line **+ its tests**.
Both land BEFORE the certifying suite fires.

## MUST-NOT

No new writer windows; no second ledger increment; no changes to `_WRITER_ROUNDS`/recursion map/API_STRATEGIES; C3's lift contract preserved (lift still lands when no substantial draft exists); C4/C7 semantics untouched; gate baseline-identical except new tests; public gate-function signatures unchanged (string returns).

## Acceptance bars

1. Behavioral: 632-shape replay test — draft survives 2 gate-lifted junk writes at 1,800 lines; repair arm sees ≥200 lines. Rotation-pivot replay — fresh draft lands at strike-4 as today.
2. Gate: baseline-identical (6 known pre-existing reds, 11 errors) + new tests green; lint clean (webapp/ + src/).
3. e2e: re-fire paulsmith (+1 control from the replica PASS set); paulsmith must PASS or honest-fail at captcha/akamai ONLY; control must stay PASS.

## Build record (2026-10-12)

- **N1** `4690844` — outcome wrappers (`_syntax_gate_outcome`/`_f821_outcome`), `_divert_lifted_write` at both closures inside the armed branch, `reset_syntax_reject_streak` riding `create_code_writer` (all three seams), sidecar bucket exclusion. Tests: `tests/test_wave60_draft_sidecar.py` (16).
- **N2** `db4dd83` — stub + `_Elision` marker prefixes (comment-form in the fence), code-writer.md "What NOT to Do" bullet. Tests: `tests/test_wave60_marker_hygiene.py` (5).
- **One existing-test touch** (deviation from "No existing tests require edits", within guardian rev-1's intent): `test_wave59_compile_gate.py::test_gate0_wired_first_in_the_f821_seam` — a source-ORDER pin that indexed `subprocess.run(` from `def _f821_rejections`. The ruff subprocess legitimately moved into `_f821_outcome`; the pin was re-targeted at the new seam asserting the SAME invariant (gate 0 before ruff, kill switch present). All behavioral wave-59 pins untouched and green.
- MUST-NOT compliance: no new writer windows; no ledger changes; `_WRITER_ROUNDS`/recursion/API_STRATEGIES untouched; public gate signatures still string-returning (pinned); C3 lift contract preserved (absent/short-draft landing pinned at 199/absent/rotation cases); C4/C7 untouched.
