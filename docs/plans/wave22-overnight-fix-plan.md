# Wave-22 — overnight RCA + fix plan (2026-09-06)

Ten independent RCA agents investigated every recent failure family. This doc
is the synthesized plan; a critique round (8-10 adversarial agents) reviews it
before implementation. Everything ships behind strict TDD.

## Failure inventory and root causes (all verified by RCA agents)

| # | Failure | Root cause (file:line) | Agent |
|---|---------|------------------------|-------|
| 371/372 (+365/370) prod 3h deaths | Infinite no-report retry loop: `route_after_testing.py:1240-1247` loops while `retry_count < MAX_TEST_RETRIES`, but the ONLY increment (`graph.py:4666-4669`) is gated on `state.get("test_report")` — never increments in the no-report arm. Prod proof: `[CASCADE] retry-no-report retry=0/2` ×6-7 per job. | 1 |
| 371/372 kill mechanism | `CELERY_TASK_SOFT_TIME_LIMIT` read via `getattr(settings, …, 10800)` (`tasks.py:219-226`) but **settings.py never defines it** — env knob silently ignored; `SoftTimeLimitExceeded()` headline is billiard's str() | 1 |
| 371/323 wedge | `product_analyzer` hangs on `playwright_browser_network_requests`; the 1800s invoke-timeout **abandons the thread** (`graph.py:2089` — "leaks until the task time limit"; 371 logged 44 rows AFTER fail). Unbounded `await _get_session()` (`playwright_tools.py:377`) outside the 120s `wait_for`, behind a module-global asyncio.Lock shared across per-phase event loops; `_close_session` runs lock-free (`:242-282`) | 2 |
| 323 headline | "analysis not found in workspace" = stale interrupt-era error_message; wave-19 T1.5 finalizer fix exists locally, **not deployed** | 2,10 |
| 324 FALSE zero-yield | Yield selection grabs the wrong file: `mtime_floor = _probe_started - 5` (`graph.py:5716`) admits the tester's just-written sample output; blank coverage read → `discovered_urls=0, stop_reason=""` → `listing_yield_failure` counts `skipped` as dead → gate overrode a PASS 0.97 (tester had found 40 URLs ×3). Browser branch discards `output_content` entirely (`graph.py:5634-5660`); `/scrape` rmtree's its staging dir | 3 |
| 338 `_discovery_cfg` NameError | Writer refactor moved the discovery call to module level referencing a `main()` local. ALL static gates are syntax-only (`ast.parse`/`compile`): `filesystem_tools.py:486-533`, `graph.py:4427-4481`, `run_execution.py:105-115`. pyflakes NOT in django image. **PLUS a second latent bomb: `OUTPUT_KEY` undefined at draft line 1110 (output-write path)** | 4 |
| 338 cascade death | Budget gate `route_after_testing.py:1750` preempts the classified action — wave-20 T1's precise "fix scraper" diagnosis never got its cycle. Crash arm (`graph.py:6141-6169`) is the only force-FAIL arm that never sets `error_message`/`execution_status` → generic headline via `tasks.py:998-1001`; `confidence→0.0` overwrite (`:6145`) kills the partial-pass escape | 5 |
| 338 empty prices | Field verifier rendered via the WRONG RUNG (`method_that_worked="direct_http"` family name → plain httpx, actual winner was `fingerprint_chrome_none`) and judged selectors on HTML truncated at 500k of 1.17M chars → 21/21 FALSE empties → "[VERIFIED EMPTY — do NOT ship]" injections → writer shipped price:"" | 6 |
| 338 analyzer evidence | "price in raw HTTP HTML" was measured on the **hydrated DOM** (94,665-char delta vs raw body; Nuxt devalue payload is integer-referenced). Draft regexes matched hydration-only literals; hardcoded `"AUD"` defeated the draft's own emission filter (0%-price rows passed 5/5). Zero devalue/embedded-payload template support exists | 7 |
| Budget observability | Step table lies: last-attempt overwrite (`graph.py:1265-1296`), phantom skipped spans (6253s for never-run steps), site_analyzer emits no phase. No job-level deadline exists; `EXECUTION_MAX_TIMEOUT` 9600s already > soft-limit budget; `writer_wall_clock_timeouts` not incremented when no draft exists (`graph.py:4992-5011`) | 8 |
| Deploy state | Prod ∈ [9090a59…HEAD]; wave-17 claim refuted; wave-20/21 committed but NOT confirmed deployed; only remote = johndudex (deploy fork via tree-sync); maintenance lock OFF; 4 prod SoftTimeLimit deaths | 9 |
| Systemic | (1) one ceiling + abandoning timeouts; (2) gates that pass on absence (`code-tester.md:79` "all sampled URLs dead ⇒ PASS 1.0" still live after 4 waves; wave-13 presence credit; `_SAMPLE_CAP=5`); (3) per-incident waves with incomplete delivery — 53% of tracked site×product targets never produce data | 10 |

**Verdict on "you patch instead of finding root cause": TRUE with receipts**
(wave-17 shipped 3 of 16 triaged defects; 371/372 died of a documented-unfixed
one; the ceiling fix was a knob-turn), **and FALSE** for wave-21 T4/T5,
wave-17's three walls, wave-20 T2 (verified root-cause grade).

## Fix set (each TDD-able; grouped by systemic class, not incident)

### Group A — wall-clock & budget (kills the 3h silent death class)
- **A1** Wire the knobs: `CELERY_TASK_SOFT_TIME_LIMIT`/`CELERY_TASK_TIME_LIMIT`
  as real `config()` settings. Honest soft-limit terminal: phase name, phase
  age, last tool, artifact state — never `SoftTimeLimitExceeded()`.
- **A2** Fix the infinite loop: increment `test_retry_count` in the no-report
  arm itself (route_after_testing returns the update), so no-report cycles are
  bounded like every other arm.
- **A3** Job budget: job-scoped deadline = soft limit − safety margin, enforced
  via `min()` clamps in `_invoke_agent_with_timeout` and `set_tool_deadline`;
  breach → named-phase failure → cleanup preserving artifacts.
- **A4** Bound the MCP session acquire: `wait_for(_get_session, 30)` + per-loop
  lock (no cross-event-loop global lock); make teardown lock-safe.
- **A5** Named fast-fail: phase hits WallClockTimeout AND its artifact is
  absent → fail the job in minutes with phase + last tool + missing artifact
  (no 2.4h zombie limping).
- **A6** `writer_wall_clock_timeouts` increments even with no draft on disk.
- **A7** Clamp `EXECUTION_MAX_TIMEOUT` to remaining job budget (it can no
  longer exceed the job's own ceiling).

### Group B — writer semantic gate & cascade fairness (338 class)
- **B1** Undefined-name gate on every writer write/edit of `.py`: AST-based
  F821 scope check (vendored stdlib checker — no new dependency; pyflakes
  absent from image). Rejects the edit and names `line:col: undefined name X`.
  Backstop: same check in `_fix_scraper_syntax`. Would have caught BOTH the
  `_discovery_cfg` NameError and the `OUTPUT_KEY` output-write bomb.
- **B2** Re-test guarantee: before every terminal `cleanup`/`human_approval`
  in route_after_testing, if current draft sha ≠ `tested_draft_sha256` → one
  forced tester pass on a dedicated counter (cannot loop).
- **B3** Grace cycle: exhausted budget + NEW remediation fingerprint (sha of
  `remediation.fix`) → one code_writer cycle (thrashing still dies; a first
  precise diagnosis gets its shot).
- **B4** Crash arm honesty: discovery-probe crash arm sets
  `error_message`/`execution_status` like its three siblings; preserve
  pre-probe confidence as `phase2_confidence` before any overwrite; partial
  pass may ride on phase-2-only evidence (empty-core veto stays).
- **B5** Terminal verdict: `_diagnose_no_execution` emits the LATEST report's
  real verdict + first high-severity issue; `_log_cascade` on terminal arms.

### Group C — evidence-integrity gates (the "pass on absence" class)
- **C1** Zero-yield binding: select the probe's output by file IDENTITY
  (snapshot workspace outputs pre-probe; the probe's file must be new), not
  mtime; blank coverage → `inconclusive`, never 0; `stop_reason="skipped"`
  never counts as dead; persist browser-branch `output_content` to the
  workspace before reading coverage.
- **C2** Verifier provenance: render via the ACTUAL winning rung (not the
  method family); record `rung`/`html_len`/`truncated` in field_verification;
  `tested=="empty"` costs coverage ONLY when `truncated=False` and
  `rung == http_method` (full fidelity) — otherwise presence credit stands.
- **C3** Hydration gate: deterministic raw_body_length vs
  rendered_html_length comparison (>5% delta ⇒ hydrated); DOM-derived fields
  from a hydrated page must be `method: playwright`-family, never css-on-raw.
  Writer gotcha: hydrated-DOM evidence is not raw HTML. sfcc-detection skill
  gets its Nuxt-CSR counter-section.
- **C4** Delete `code-tester.md:79` ("all sampled URLs dead ⇒ PASS with
  confidence 1.0") — replace with FAIL/escalate semantics.

### Group D — ship + operate (deployment is part of the fix)
- **D1** Branch/PR: wave-20 + wave-21 + wave-22, PR body drafted, deploy order
  django+celery BEFORE browser-service, maintenance-lock runbook, rollback
  note (0038 additive).
- **D2** Env checklist: `CELERY_TASK_SOFT_TIME_LIMIT`/`_TIME_LIMIT` now real
  knobs; prod `AGENT_INVOKE_TIMEOUT` 1800→900 parity; verify
  SCRAPER_SOFT_BLOCK_MIN_BYTES=20000; PROXY_* on both services.
- **D3** `/api/version/` endpoint (git SHA + wave tag) so "what does prod run"
  is forever a GET, not an archaeology dig.
- **D4** Campaign restart runbook: Tier A proven sites first
  (verabradleyoutlet, wolfandbadger, rmwilliams), never myhouse until the
  soft-limit fix is verified live, sephora.* excluded, max 2 in-flight.
- **D5** (if time) Step accounting repair: append-only attempt spans,
  site_analyzer emits phases, close skipped steps at skip site.

## Local e2e validation plan
1. Re-drive michaelhill (338 shape, live seed): undefined-name gate rejects
   the regression at edit time; if cascade still exhausts, headline carries
   the real verdict; verifier rung fix keeps title/price credit.
2. marimekko regression: 337's happy path still COMPLETED (no fix regresses
   the T4/T5 win).
3. Synthetic: no-report loop bounded (3 cycles → honest fail, not 3h).
4. Full root suite + webapp tests green; ruff clean on touched files.

## Explicitly out of scope tonight
- Devalue/Nuxt payload template helper (real but large — follow-up wave).
- PDP-shape discovery gate + sitemap/API self-heal arms (agent 3 items 3-4 —
  follow-up; C1's binding fix + inconclusive semantics already un-false-fails
  the 324 class).
- Prod deploys/merges (user's click), Railway env changes (user-owned),
  campaign restart (gated on deploy).

## Critique round outcomes (10 adversarial agents, 2026-09-06) — FINAL SET

The plan as drafted was ~4× oversubscribed for one night (critique 7). The
critiques rewrote several mechanisms and killed others. What follows is the
authoritative implementation set; items above that it contradicts are amended.

### Mechanism corrections (plan text superseded)
- **A2**: counters live in `_invoke_code_tester` (next to the wall-clock parity
  counter, `graph.py:~5979`), keyed on "no verdict" — NOT in
  `route_after_testing` (a conditional edge returning `str` cannot mutate
  state), NOT in the writer (that gate requires a truthy report; the no-report
  arm by definition lacks one).
- **A3**: deadline is TASK-scoped — stamped at task entry in `tasks.py`
  (approval-resume runs a NEW task; a `created_at` clock would insta-kill every
  resumed job). Pure `_effective_timeout(phase_timeout, deadline, now)` with
  injected clock; recomputed per-invoke in `_invoke_agent_with_timeout` and
  published via `set_tool_deadline`; floor ~300s; finalize margin reserved.
- **A4 REWRITTEN (critique 3+9)**: the session "pool" is already dead weight —
  per-call `asyncio.run` recreates the session every call
  (`playwright_tools.py:541`), so the global lock buys nothing and adds three
  cross-loop hazards (contended-lock RuntimeError laundering, non-threadsafe
  release from a foreign thread, lock-free `_close_session` vs holder
  republish). Fix = DELETE `_session/_session_stack/_session_loop/_session_lock`;
  one-shot `sse_client`+`ClientSession` per call inside ONE `AsyncExitStack`
  entered and exited in the same loop; `wait_for(..., 30)` bounds the
  currently-unbounded `enter_async_context(sse_client(...))`. Additionally
  (critique 9): clamp the whole tool-call body to `min(class_budget,
  remaining_deadline)` (120s default, 240s for `network_requests`); ONE retry
  after `_close_session()` on TimeoutError (previously 0 retries on the exact
  wedge signature). Gate: navigate+snapshot across two calls still shares the
  CDP tab.
- **A5 TRIMMED**: fires only for `code_tester`/`product_analyzer` (never
  `code_writer` — its timeout arm deliberately keeps a usable draft; never
  navigation); re-checks the FS at decision time; routes to cleanup with a
  NAMED error (recoverable via re-drive), not a new terminal class. Depends on
  A4 (abandoned threads otherwise poison the next job).
- **A7 KILLED as written**: clamping exec work under the old 10800 ceiling
  just renames the 3h death. Instead RAISE the ceiling: `soft=12960`
  (`≥ 9600 exec + ~2700 observed pre-exec + 360 grace`), `hard=13320`.
  Entry-time warning when remaining budget < `EXECUTION_MAX_TIMEOUT`.
- **B1**: use **ruff** (already in image, 0.16.4) —
  `ruff check --isolated --select F821 --no-cache --stdin-filename <path>`,
  NOT a vendored hand-rolled scope checker (two agents' hand-rolled checkers
  both false-flagged templates). F821-only filter is load-bearing (drafts
  carry benign F401/F841). Fall OPEN on checker error
  (`subprocess.run(timeout=10)`). Gate applies to code_writer's
  write_file/edit_file on draft paths only (kwarg-scoped — 8 agents share
  write_file); `_fix_scraper_syntax` backstop stays authoritative. Rejection
  message: "edit NOT applied; file unchanged" + up to 5 `line:col: undefined
  name X` + "define the name before first use (a later def does not fix a
  module-level call; `X if X in dir()` is not a fix)". NOTE: ruff also flags
  337's own live `PRODUCT_LISTING_URL` default-arm bug — the marimekko
  regression drive must expect a changed draft, not byte-identical.
- **B2**: forced re-test per-JOB cap 1 (not per-mismatch); skipped when the
  `tester_wall_clock_timeouts≥2` arm fires (same wall twice ≠ needs re-test)
  and when remaining budget is thin.
- **B3**: NEVER hash `remediation.fix` (LLM paraphrases → always-new → the
  infinite loop A2 closed reopens). Key = `sha256(json{target, field,
  sorted(field|issue_type set), exception class})`; hard per-job cap 1; never
  on `FINAL_RETRY_SENTINEL` jobs.
- **B4**: also extend confidence preservation to the CLI/ladder arm
  (`graph.py:~6321`, the other zeroing site); tag phase-2-only rides
  `discovery_unvalidated` (F15 empty-core veto survives inside
  `_scraper_has_real_items`).
- **C1**: snapshot = `(path → (mtime_ns, size))` per ATTEMPT inside
  `_probe_phase1_discovery_once` (the wrapper runs up to 3 subprocesses);
  probe's file = new OR changed; >1 new file ⇒ inconclusive, never guess;
  `_read_probe_discovered_urls` gets the same binding (else URL hygiene reads
  the tester's old seed list); browser branch persists `output_content` to a
  `probe_`-PREFIXED file (an `output_*.json` name would be re-selected by
  `run_execution.py:2122` and recreate the bug); blank coverage ⇒ inconclusive
  stamp, never `discovered_urls=0`; `listing_yield_failure` gains
  `if not cov: return False`; the "skipped never dead" clause is scoped to a
  draft-declared bare `"skipped"` — wave-20 T2's `phase1_skipped` STAYS dead
  (it is a code-bug verdict with its own tests).
- **C2 TRIMMED + burden flipped (fail-closed)**: `connectivity.http_method` is
  the cheapest-HTTP-rung, wrong key. Compare `/render`'s RETURNED method
  (`probe.py:379`) against the REQUESTED `method_that_worked`; divergence =
  render escalated = unproven. `probe.py render_page` must emit
  `html_truncated` (today it slices silently). `tested=="empty"` costs
  coverage ONLY with POSITIVE full-fidelity proof (rung match AND
  `truncated=False`); missing metadata keeps presence credit — wait, NO:
  missing metadata defaults to NOT-full-fidelity for the downgrade decision,
  i.e. the T3.13e downgrade fires only when absence itself is honest —
  implemented as: downgrade requires `truncated is False` AND rung match;
  absent fields ⇒ keep credit but record `provenance:"unknown"`. (Critique 1
  and 5 converged here from opposite directions; the recorded-fields test
  arbitrates.)
- **C3 CUT to prompt-level only** (writer gotcha + sfcc skill counter-section,
  source-pin tested); the raw-length detector needs new state plumbing and its
  5% threshold under-fires on 2M-char Nuxt pages (4.7% delta) — wave-23.
- **D2**: `AGENT_INVOKE_TIMEOUT` STAYS 1800 (900 was the canary-177-181 death
  config; 1800 produced the first glm-5.3 completion). Add
  `CODE_WRITER_LLM_TIMEOUT=1200`. Ship `CELERY_TASK_SOFT_TIME_LIMIT=12960`
  / `CELERY_TASK_TIME_LIMIT=13320`, `WAVE_TAG=wave-22`, re-verify
  `SCRAPER_SOFT_BLOCK_MIN_BYTES=20000` + `PROXY_*` on both services.
- **E2E cut to ONE full drive** (michaelhill 338-shape; marimekko regression
  = wave-21's existing tests + B1's known PRODUCT_LISTING_URL rejection).
  Mandatory first step: `docker compose restart django celery-worker
  celery-beat browser_service` (bind-mounted code, old modules in memory;
  probe.py/server.py changes make the browser_service restart load-bearing).
  During the drive: 5-minute diagnostic `curl ac.cnstrc.com` from the
  browser-service container — critique 9 found 338's Constructor.io fallback
  never got a single payload (connect failure at egress); if DNS/proxy, that
  is an env fix that flips the whole class upstream of any template work.

### Implementation waves (commit boundary = full suite green)
1. **W1 (S items)**: A1 knobs + composition test · A6 counter · B4 crash-arm
   honesty · B5 terminal verdict · C4 tester line · D3 `/api/version/`.
2. **W2**: A2 tester counter · A3 task budget · A4 pool deletion + call clamp
   + retry · A5 trimmed.
3. **W3**: B1 ruff F821 gate (largest single item).
4. **W4**: C1 identity binding + inconclusive semantics.
5. **W5 (if time)**: B2 re-test guarantee · B3 grace cycle · C2 provenance.
   Cut order at checkpoints: C2 → B3 → B2.

### Recorded for the morning report / wave-23 backlog
- `_SAMPLE_CAP=5` (route_after_testing.py:846,1126) — zero fix items tonight.
- `fields_extracted` dead state key (state.py:172) written by 4 nodes, read by
  none; Site model field is the live union.
- Writer blind-offset `read_file` (filesystem_tools.py:234) — B1 backstops the
  symptom only.
- `getattr(settings, …)` knob sweep (A1 is one instance of a class).
- Devalue helper; PDP-shape gate + self-heal arms; D5 step accounting
  (append-only spans); C3 detector; devalue egress diagnostic results.
- Park/park_unhealthy→re-test arms remain counter-free (A3 wall-clock is the
  only bound) — accepted, documented.
