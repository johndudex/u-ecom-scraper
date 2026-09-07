# Wave-24 Fix Plan — writer/tester loop integrity + economics

**Source:** two-agent forensic audit of prod jobs 393/394/395 (2026-09-07) + maintenance-lock incident RCA.
All root causes verified in code on `file-master-artifacts` branch, 2026-09-07.
Evidence: `/tmp/agentA_39{3,4,5}.json`, `/tmp/agentB` machinery report (session transcript).
Outcome of the audited trio: 393 wall-clock gate FAIL, 394 Radware exhausted-cascade FAIL, 395 exhausted-cascade FAIL — all honest, all predicted.

---

## W24-1 (P0) — Zombie-writer latch is self-resetting; tester validates a moving target

**Evidence (prod):** 395 `[INVOKE-TIMEOUT]` 14:49:16 → tester run starts 14:49:40 → zombie writer `edit_file` on `scraper_draft.py` at **14:49:42** + ~20 reads through 14:55:14. Same shape on 393 (14:32:13 timeout, zombie `[TOOL]` rows 14:32:38–14:33:09 overlapping tester startup).

**Root cause (verified):** `webapp/agents/tools/context.py:42-46` — `set_tool_context()` unconditionally resets `_ctx["invocation_cancelled"] = False` at the start of every invocation, re-arming the zombie's tools. The in-code comment admits it ("a residual risk…"). `mark_invocation_cancelled` (context.py:105) latches, but the very next phase's `set_tool_context` clears the latch — so the wave-17 job-329 fix never holds across a phase boundary, which is the only scenario that matters. The backstop (draft-freeze + compile gate) protects **execution** but not the **tester**: verdicts on 393/395 were rendered against a draft being mutated mid-test.

**Fix (two layers) — design corrected by deep-agent verification 2026-09-07:**
1. **Per-invocation latch via ContextVar (NOT a thread attribute).** The literal thread-attribute design is BROKEN: sync tools do not run on the invocation thread — `langgraph/prebuilt/tool_node.py:821-823` dispatches every tool through `get_executor_for_config(config)` → a `ContextThreadPoolExecutor` pool (langchain_core 1.5.1, verified in the celery-worker image), so `threading.current_thread()` in the `BaseTool.invoke` guard never sees the zombie's token. Corrected design: a `contextvars.ContextVar` token set **inside** the thread target `_run()` (graph.py:2223-2225) before `agent.invoke`; the cancelled-set stays **process-global** in `tools/context.py` (`_ctx`), never reset by `set_tool_context`. Context propagation is verified at all three dispatch boundaries (ToolNode pool `copy_context()` on the zombie thread; pregel executor; async `run_in_executor`), so the token reaches every tool call. Each `_invoke_agent_with_timeout` call (main writer window + `_fix_scraper_syntax` graph.py:4611 + `_enforce_cli_contract` graph.py:4730) gets its own token and is individually cancellable. Stamp the async twin too (graph.py:2046) even though async phases are off. Cap/prune the cancelled-set. Add a langchain-core version guard or runtime fallback to the existing global latch (the patch leans on `ContextThreadPoolExecutor` internals; requirements pin by range).
2. **Tester-exit draft-mutation check replaces the snapshot backstop (cheaper, primary).** At tester entry take the live draft's sha/fp (the fp machinery already exists: graph.py:6233-6246, `draft_file_fp` route_after_testing.py:1181-1193); at tester exit verify it again — if changed mid-test, stamp the verdict `draft_mutated_during_test` and let the router treat it as unproven. ~15 LOC, zero path/output/fingerprint risk. The full tester-snapshot idea (running the draft from a copy) is DEFERRED: verification found it silently breaks 4 paths (browser-run outputs persist next to the scraper and the router's ground-truth glob would miss them → false FAILs; `last_tested_draft_fp` semantics; draft-relative `__file__` resolution; tester prompt hardcoded paths) and costs ~40-60 LOC, not 10.
**Size:** ~80 LOC + tests. **Tests:** latch survives next `set_tool_context` (the prod-395 regression); zombie disarmed through a REAL `agent.invoke`/ToolNode round — the existing `tests/test_draft_corruption_gates.py:223-273` tests invoke tools directly on the test thread and pass vacuously; they must be rewritten.

## W24-2 (P0) — Verification probe overwrites a good discovery result with an empty one

**Evidence (prod):** 394 cycle 1: "Phase 1 discovery is proven functional: `stop_reason=target_met pages=5 urls=240`", then a redundant `--discover-only` second pass hit `empty_render` and **overwrote** the 240-URL output with `discovered_urls=0` → false HIGH → writer cycle spent fixing a non-defect. (Sibling of 323/324's egress-throttle zeroing; wave-17 removed the S24 refetch arm but this path survives.)

**Root cause (corrected by deep-agent overwrite map — two mechanisms, not one):**
1. **In-process double discovery (the 394 mechanism).** The playwright template runs Phase 1 twice in ONE process when the env/flag gate fires and `--discover-only` is passed (playwright_scraper.py:554-569 then 606-675); the `--discover-only` block clobbers the same process's own good result and returns before the full-run write. A harness snapshot cannot restore a file the same process overwrites. 394's writer even patched it ad hoc ("[run-1 fix] Never persist a weaker/empty result over a successful discovery pass").
2. **Consumer-level loss.** Output filenames never collide (`output_{ts}_{pid}` everywhere) — the loss happens because `_find_newest_output` (run_execution.py:2191-2194) ranks every discovery artifact at 0 substantive items so ties break to the newest zero-yield probe, and `_attach_discovery_coverage`'s probe-artifact skip (graph.py:849-865) matches only `found==0 AND stop_reason=="navigate_error"` — an `empty_render`/`all_tiers_blocked`/`empty_first_page` probe artifact sails straight into `report["discovery_coverage"]` → the false HIGH. Also: navigation templates overwrite `discovered_urls_checkpoint.json` **unconditionally including 0 URLs** (no job-77 guard, unlike the `input_urls.json` writers).

**Fix (4 hooks, ordered by leverage; ⚠ = user-WIP file, needs coordination):**
1. **Consumer belt (no WIP files):** widen the `_attach_discovery_coverage` skip at graph.py:849-865 to any artifact matching the existing `_is_discovery_output()` predicate (route_after_testing.py:393-405 already implements it) — kills the false-HIGH path in one edit.
2. **Checkpoint guard (1 non-WIP template + snapshot pattern):** add `discovered_urls_checkpoint.json` to `_identity_snapshot`'s pattern tuple (graph.py:5452 — snapshot infra already exists at 5440-5471), restore on zero-yield at the end of `_probe_phase1_discovery_once` (before return ~6057); apply the job-77 "no 0-URL overwrite" guard to `navigation_scraper.py`'s checkpoint writer. (http_navigation's checkpoint has the same hole but is user-WIP.)
3. **Report-level preservation:** in `_invoke_code_tester`'s zero-yield arm (graph.py:6563-6615), preserve prior `report["discovery_coverage"]` B4-style (the arm currently overwrites it with `discovered_urls: 0` at 6575-6581, destroying the tester's own successful run's coverage).
4. **Persist-namespace hook (⚠ webapp/agents/tools/shell_tools.py — user-WIP):** at shell_tools.py:629-638, when the command was `--discover-only` and the artifact is a zero-yield discovery phase, persist under `probe_output_*` instead of `output_*` (the deterministic probe already uses that namespace deliberately, graph.py:5900-5913). Highest single-point leverage, but blocked on the user's WIP file.
The durable in-process fix ("never persist a weaker discovery result than an earlier pass in this process") belongs in the templates — ⚠ playwright_scraper.py and http_navigation_scraper.py are user-WIP; navigation_scraper.py we can land.
**Size:** ~70 LOC across 4 hooks + tests. **Tests:** zero-yield probe artifact skipped by consumer belt; checkpoint restored after zero-yield probe; prior coverage preserved in tester zero-yield arm; (WIP-gated) probe persist lands in probe_output namespace.

## W24-3 (P1) — Access-class walls burn 2 full writer cycles they can never win

**Evidence (prod):** 393: three ~1800s writer turns (total ~3,330s writer time) against a migrating stop-reason signature (`empty_first_page` → `all_tiers_blocked` → `navigate_throttled`), cycle-2 root cause **our own browser-service 429**. No scraper code can fix infra throttling. 395 similar (CSR grid yields 0 anchors on 2× HTTP-200 renders; correct Algolia re-tier fix landed one cycle too late). 394: Radware challenged all 3 cycles; tester itself said "do NOT switch away from playwright".

**Root cause:** `route_after_testing` bounded-bounce arms send access-layer failures back to `code_writer` on the full retry budget; the budget distinguishes code-fix from access-wall nowhere. Crocs-382/393 are the same class: the wave-23 gates end these jobs honestly but only after ~3h each.

**Fix (corrected by deep-agent vocabulary audit):**
- **Constant** (beside `_COVERAGE_FAIL_STOP_REASONS`, route_after_testing.py:107-113): `_ACCESS_WALL_STOP_REASONS = {"navigate_throttled", "all_tiers_blocked", "empty_render", "empty_first_page", "navigate_error"}`. Explicitly OUT: `navigate_unavailable` (own park/resume lane — counting it would FAIL jobs the gateway killed), `dedup_flat` (code bug wearing a FAIL label), `phase1_skipped` (code), `malformed_discovery_urls` (code), and `soft_block`/`captcha`/`sitemap_*` (not stop_reasons at all). Also add `navigate_throttled` to `_COVERAGE_FAIL_STOP_REASONS` so the anti-bot downgrade exemption (1826) and ground-truth veto see it consistently.
- **Counter:** `access_wall_cycles: int` in state.py; incremented in `_invoke_code_tester`'s update dict (graph.py:6192, beside `test_retest_count`) — routing functions cannot mutate state. Normalize the reason via `_normalize_probe_stop_reason` (graph.py:5395-5407) first.
- **×2 arm placement:** after the ground-truth override block ends (route_after_testing.py:1682) and BEFORE the volume-gap bounce (1687) — ahead of every code_writer bounce arm and ahead of the retest-exhausted→scraper conversion (1966-1971) that currently converts infra walls into writer cycles (exactly 393's waste). Guard with its own `_scraper_has_real_items` check (mirror 1777) so rescues still win, and wrap in `_terminal_after_retest_check`/`_terminal_after_grace_check` like sibling terminals (1933-1942). Known asymmetry to respect: 4 of the 5 access stops set `_cov_reason`, which vetoes the override at 1669 — only the final/exhausted rescues (1776-1783, 1908-1927) can save such jobs; that's acceptable and now explicit.
- **Escape hatch (reset without letting Radware through):** reuse `_remediation_scraper_diagnosis` (route_after_testing.py:154-189 — already demands `target=="scraper"` AND a concrete field/fix/issues; bare `{"target":"scraper"}` is a no-op). Reset ONCE, cap-1 field mirroring `remediation_grace_used` (state.py:115). Critical: `remediation.target == "strategy"` must NOT bypass the counter — 393 cycle-2 used exactly `target: strategy` on a BS-429 diagnosis and produced the second pointless writer cycle. Probe writer-feedback (graph.py:6593-6599) writes `feedback_for_writer`, never `remediation.target` — no leakage.
- **Open design choice (deliberate, before implementation):** `navigate_throttled` is our OWN browser-service 429 (393 cycle-2). ×2 → terminal FAIL is honest, but PARK (`park_browser_unavailable`, the `navigate_unavailable` lane) may be cheaper and preserves the queue slot. Recommend: count it for the ×2 like the others, but route the terminal to park instead of cleanup when the ONLY stops were `navigate_throttled`.
**Size:** ~80 LOC + tests. **Tests:** 2 access-wall cycles → early terminal; `navigate_unavailable`/`dedup_flat` never counted; concrete `target: scraper` remediation resets once (cap 1); `target: strategy` does not reset; ground-truth/real-items rescue wins ahead of the arm.

## W24-4 (P1) — Writers ignore "edit, don't rewrite" on code-fix cycles

**Evidence (prod):** 395 cycle 2 was a `code-fix` cascade with an explicit "do NOT rewrite from scratch" + a preserve-list; the writer spent 990s (55% of window) reading, then a **full `write_file`** regenerated the file from the template header and consumed 100% of the clock → timeout. 393 cycle 2 was forced into a rewrite by the strategy-switch template hint ("Read the template at templates/http_navigation_scraper.py and use it as your base").

**Root cause:** instruction-only constraint with no mechanical enforcement; edit-over-write base-swap (graph.py:4989-5015) hands the writer the draft as base, but nothing stops a full-file `write_file`.

**Fix:** in the writer invocation harness (same invocation-local wrapper site as the W23-2 nudge): on a **code-fix cycle** (remediation present, strategy unchanged, parseable draft on disk), intercept `write_file` targeting `scraper_draft.py`: first offense → return a nudge/error string ("draft exists and parses — apply your fix with edit_file; full rewrite requires `full_rewrite_reason` in the write"); if the write args carry `full_rewrite_reason`, allow and log it. Never applies on strategy-change cycles or when no parseable draft exists.
**Implementation notes (verified):** `write_file(path, content)` arrives as kwargs (filesystem_tools.py:341; GLM `v__` prefixes stripped in `_parse_input` before the wrapper sees them), so comparing new content size vs the existing draft is trivial at the wrap site. The fix-cycle signal does NOT reach the wrapper today — `create_code_writer → _build_agent → _get_tools_sync → _apply_guards` carries no state; a flag needs ~4 signature hops, so derive it once in the node (it already has `test_report`, remediation, strategy, `last_writer_template`, and the parseable-draft check via `draft_safety.draft_parses` — use that primitive, not the `_cw_dead`-only ast block at graph.py:5100-5111). Wrapper state spans all three windows of a cycle (main + syntax + CLI fixes share one agent instance at graph.py:5017) — the first-offense latch and W24-5 counter accumulate across them; that is the desired behavior, document it.
**Size:** ~70 LOC incl. plumbing + tests. **Tests:** code-fix write_file nudged once, allowed with reason, never on strategy-change/absent draft; counter spans syntax-fix window.

## W24-5 (P2) — Draft-nudge false-positives through entire edit-only cycles

**Evidence (prod):** 394 cycle 2: 32 `[HARNESS NUDGE]` fires across calls 12–48 while the draft demonstrably existed and was being edited (39546→45530 chars).

**Root cause (verified):** `subagents.py:apply_draft_nudge` — the `drafted` flag arms only on a `write_file` in **this** invocation, and agents are rebuilt fresh per invoke, so an edit-only fix cycle starts `drafted=False` and can never arm it.

**Fix:** (a) arm `drafted` when `edit_file` targets the draft path (an edit implies existence); (b) skip the nudge entirely when the invocation is a fix cycle (remediation present in the seed message — the nudge's purpose is first-draft forcing). (a) is one condition; (b) is a flag at the apply site (subagents.py:1404-1405).
**Size:** ~10 LOC + tests.

## W24-6 (P2) — Writer burns window reading templates that only exist in its prompt

**Evidence (prod):** 393 cycle 1: 4 failed `read_file` attempts on `templates/http_requests_scraper.py` ("File not found") — the template ships embedded in the system prompt (subagents.py:982-993), not on disk. Writers overall spend 55–75% of each window on tool-call research before the first write.

**Fix:** in the writer prompt builder, when the template is embedded, append one deterministic line: "The template is embedded in this prompt — do NOT read_file templates/*.py; that path does not exist in this container." (Do not touch the template files themselves — `templates/http_navigation_scraper.py` and `playwright_scraper.py` are user-WIP.)
**Size:** ~5 LOC.

## W24-7 (P2) — Cascade labels lie: "strategy-switch" emitted when strategy didn't change

**Evidence (prod):** 394: `[CASCADE] action=strategy-switch retry=0/2 strategy=playwright …` — scraper_analyzer returned **playwright again**; the label forced the cycle-2 writer prompt into template-rewrite framing (feeds W24-4).

**Root cause (verified):** route_after_testing.py:1982 logs `strategy-switch` whenever `_action == "strategy"`, before knowing what strategy scraper_analyzer will pick.

**Fix:** compare at scraper_analyzer exit (or pass old strategy in state and log post-hoc): same strategy → log `code-fix (strategy rerun)`; different → `strategy-switch`. Update the message builder so a rerun cycle does NOT get the template-rewrite hint (it gets edit-over-write framing instead).
**Size:** ~20 LOC + tests.

## W24-8 (P2) — Maintenance lock has no audit trail (author unrecoverable)

**Evidence (ops, 2026-09-07):** lock enabled 04:39–05:13 UTC by an unknown superuser; the lift (user-approved) overwrote the singleton's `updated_by`/`reason` before capture. `MaintenanceLock` is singleton pk=1 (`webapp/scraper/models.py:793-816`), no history; `_maintenance_state()` exposes only `{enabled, reason}`.

**Fix:** append-only `MaintenanceLockEvent(enabled, reason, updated_by, created_at)` written on every POST `/intake/maintenance/` (before mutating the singleton); surface last-event (by/at) in `_maintenance_state()` + health dashboard; admin inline history. Migration + tests.
**Size:** ~60 LOC + migration + tests.

---

## Recommended scope

- **Wave-24 core = W24-1 + W24-2 + W24-3**: verdict integrity (zombie), evidence preservation (overwrite guard), economics (access-wall early stop). Together they address every terminal of the 393/394/395 trio and the crocs-382 wall.
- **Ride-alongs (cheap, same area):** W24-5, W24-6, W24-7.
- **Separable:** W24-4 (behavior change to writer tooling — worth a local e2e validation on its own), W24-8 (ops hygiene, independent of the loop).
- TDD per repo practice; full suite green + ruff clean before any deploy; deploy stays user-gated per standing rules. NOT dispatched against prod queue until user says so.

## Explicitly deferred (not wave-24)

- Template rewrite *framing* on strategy rungs (needs prompt-design iteration; W24-7 removes the worst instance).
- Per-turn wall-clock taper (risky: 394's best cycle legitimately needed 1253s; W24-3 removes the cases that matter without touching windows).
- writer recursionLimit economics (bounded by 120; no observed harm in 393-395).
