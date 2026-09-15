# Wave-32 Fix Plan — prod job 587 (marimekko.com)

**Status: PLAN rev-4 — FINAL. Round 1 (all 7 MAJOR findings) + D5 reshape adjudication + round 2
(cross-plan interference: 5 MAJOR + 4 MINOR demanded edits incorporated — D5 finisher-condition scope,
D2 extension-gate mirror, A5 import-trap, D1 pairing honesty, writer_prompt source-pin ownership).**
**Date: 2026-09-14. Evidence base: three parallel forensic agents (discovery/traverse, re-map loop + probe, writer
economics) + prod log verification via Railway GraphQL + adversarial critique agent (76 tool calls, anchor-verified).**
**Every defect claim is anchored to file:line in the working tree (branch `file-master-artifacts`, wave-31 lineage
`abe1d4c`); line numbers re-verified by the critique agent where it mattered.**

---

## 1. What actually happened to job 587

User submitted ONE marimekko PDP URL at 18:10:30 UTC. The job ran as `list_page` with
`search_criteria = https://loveamika.com/collections/bundle-builder-products` — a **different company's** URL.
Terminal state (FM artifact log, 20:32:50 UTC): **FAILED after 2h22m** — 3 writer invocations, 2 re-maps,
3 testers, no output, no `scraper.py` promoted, draft grown 106,972 → 119,420 → 123,907 bytes without converging.

The cascade, in order:

```
RC-1  Intake accepted a cross-host listing URL (user input) → the pipeline's F17 gate
      silently dropped it → the job had NO usable Phase-1 anchor. job_restart propagates
      search_criteria faithfully (and its POST-override can even inject a fresh one),
      so re-drives inherit or re-introduce the poison.

RC-2  Site evidence lied while the site was 429-ing: the render gate overrode
      blocked_type='antibot' because an Akamai challenge shell satisfies its
      innerHTML-length arm (or wait_for_hit alone). Separately, the domain-keyed
      probe cache serves any ≤4h-old entry as clean WITHOUT content/captcha
      verification, and there is NO write path for a negative/escalated verdict —
      the cache structurally cannot learn "Akamai" (bounded: an entry dies 4h
      after its INSERT; cached_at is auto_now_add, cache hits never refresh it).

RC-3  The one component that tests Phase 1 (smoke probe) ran the draft, got rc=1
      ("No --query, --category-url, or --listing-url provided"), logged it as
      "OK", and discarded stderr. "yield inconclusive" is a total no-op that
      disarms every probe consumer. The pipeline had the diagnosis and threw it away.

RC-4  Termination was count-bounded, not evidence-bounded: a price that is dead on
      the live page was classified a "mapping" bug, each re-map was granted on
      nothing, and the loop burned 2.3h to reach the outcome fixed at minute 1.

RC-5  Writer windows were mis-budgeted at every layer: 120 "steps" = ~40 tool rounds
      (pre_model_hook tax), the W30-1 extension was invisible to the tool deadline
      (browser verification REFUSED in the final 900s), the full 119KB draft was
      re-embedded in the system prompt every turn, a step-death draft overwrote the
      last parseable FM snapshot, and +16% growth-without-convergence tripped no gate.
```

Every defect below maps to one of these five root causes. The fixes are ordered the same way:
**make the system tell the truth first (A), stop feeding it lies (B), stop paying for known-dead
work (C), make the writer's window spendable (D), and make the three chains agree (E).**

**Critique-verified outcome claim:** with E1 + C1 shipped, 587 terminates at tester #1 in both
sub-cases (no fall-through candidate → probe rc=1 no-source → C1 force-FAIL; PDP-as-listing
candidate → discovery yields 0 → the pre-existing zero-yield arm force-FAILs). The loop cannot
recur; remaining risk is bounded slower-FAILs, not loops.

---

## 2. Defect inventory (consolidated; anchors re-verified by critique)

| ID | Root cause | One-line statement | Anchor |
|----|-----------|--------------------|--------|
| D-A1 | RC-3 | `rc=1` probe exit logged "OK"; stderr captured then dropped | `graph.py:6897-6899` (classification `:6877-6894`) |
| D-A2 | RC-3 | Probe output detection: browser path round-trips only `output_*`, not `probe_output_*` (latent, not 587's cause) | `browser_service/scraper_runner.py:577-590` |
| D-A3 | RC-3 | `probe_yield=None` ("inconclusive") has NO arm — disarms zero-yield FAIL, listing retry, tier escalation (3rd observed occurrence) | `graph.py:7573-7718` |
| D-A4 | RC-1 | Probe / tester-tool / execution listing chains diverge in precedence AND guarding; probe F17 drop has no fall-through; tester tool chain has no F17 guard at all | `graph.py:6727-6754`, `shell_tools.py:570-598`, `webapp/agents/nodes/run_execution.py:801-895` |
| D-A6 | RC-2 | Render gate: `wait_for_hit` alone can satisfy it, and the body arm measures raw `innerHTML` — an Akamai shell passes; honest 429→antibot verdict nulled | `browser_service/server.py:2297-2307, 2552-2559` |
| D-A7 | RC-2 | Probe cache serves ≤4h-stale clean verdicts with captcha/content verification skipped; no write path for a negative/escalated verdict (`_save_probe_cache` early-returns on `not success`) — the cache cannot record "Akamai" | `probe_tools.py:125` (expiry), `:134` (hit-save touches only `last_used_at`), `:152-153` (no negative write), `:940-942` (verification skip) |
| D-B1 | RC-4 | Re-map handoff carries field names, never failure evidence; tester rule classifies `tested:"empty"` (source dead on live page) as a "mapping" failure | `subagents.py:2303-2337, 5083-5086` |
| D-B3 | RC-4 | Remediation fingerprint reads `rem["field"]` (singular); tester emits `fields` — wave-29 B5 tiers vacuous, wave-22 B3 grace miscloses | `route_after_testing.py:1332`, `src/writer_memory.py:123` |
| D-W1 | RC-5 | `code_writer: 120` = ~40 tool rounds (pre_model_hook → 3 super-steps/round); comment cites a 900s wall that has been 1800/2700 since W30-1 | `graph.py:1044-1047` |
| D-W2 | RC-5 | Timeout return payload carries the BASE window (1800.0s) while the thread died at the extension cap (2700s) — ops-facing number is wrong; async twin has no `_waited` at all | `graph.py:2639`, `:2344` |
| D-W3 | RC-5 | Tool deadline stamped from base before the join extends — `run_scraper` refuses every browser run in the 1800→2700s zone | `graph.py:2516`, `shell_tools.py:388-407` |
| D-W4 | RC-5 | Retry cycles re-embed the FULL on-disk draft into the system prompt; `_truncate_messages` never sees it — ~145K chars re-sent every turn | `graph.py:5736-5760`, `subagents.py:975-1029` |
| D-W5 | RC-5 | Draft-finisher arm requires `wall-clock timeout` in error + counter ≥2; a step death returns healthy-shaped messages → counter RESET — finisher unreachable for exactly the 587 shape | `graph.py:5981-6078` |
| D-W7 | RC-5 | FM per-job draft snapshot has no parse gate and runs before the syntax fixer — a broken draft overwrites the last good one; AND a SECOND unguarded restore path (`setup_workspace`, the watchdog re-drive path) restores the same unverified bytes | `graph.py:5859-5877`, `draft_safety.py:179-210`, `nodes/setup_workspace.py:244-259` |
| D-W8 | RC-5 | Draft growth (107→119→124KB) escapes both progress gates (byte-identity and step-death-AND-unchanged) | `graph.py:6120-6155, 5367-5390` |
| D-W10 | RC-3 | `read_file`/`edit_file` args summaries record only the path — `line=`/`offset=` dropped, so paging spirals are invisible in trail + ToolCallLog | `graph.py:1771-1796` |
| D-INT | RC-1 | Intake (and job_restart's POST override) accepts a listing/search URL the pipeline's F17 registrable-domain gate will drop — leaving no Phase-1 anchor; intake vocab is `nav_method ∈ {listing, search}`, partner vocab is `input_mode ∈ {list_page, search_term}` | `views.py:2908-2918`, `views.py:2568-2572`, `:652-668`, `webapp/scraper/api/writers.py:65`, F17 at `webapp/agents/nodes/run_execution.py:2010-2032` |
| D-W12 | — | **Refuted:** wave-29 writer_memory is bounded (≤800 chars + 2 lines) and not a contributor. Not fixed; listed so later rounds don't re-derive it. | `src/writer_memory.py:304-400` |
| ~~D-A7 "self-refreshing TTL"~~ | — | **REFUTED by critique:** `cached_at` is `auto_now_add` (INSERT-only); cache-hit saves touch `last_used_at` which expiry never reads. Entry dies exactly 4h after first write. RC-2/B2 rewritten accordingly. | `webapp/scraper/models.py:627` |

---

## 3. The fixes

**Conventions.** Every item: failing test FIRST (TDD), then minimal implementation, then the named
regression set re-run. `[SAFE]` = additive or strictly-narrowing behavior; `[RISKY]` = changes a
decision boundary; these carry an extra audit step. Tests live in `tests/` (root, run from `/app` in
container) unless noted. **Blast-radius lists are verified** (critique round 1 found a fabricated
entry; every list below names only files that actually assert the touched behavior — "none" is
stated where that is the truth).

### Tier A — Diagnosability (all additive; make the next 587 readable in one log line)

**W32-A1 `[SAFE]` Probe: honest exit classification + stderr persistence** (D-A1)
- Change: at `graph.py:6897-6899`, any `rc != 0` without a crash signature logs
  `_probe_phase1_discovery: exit=N rc=N (no crash signature)` and appends the last ~800 chars of
  captured stderr to the log line; keep `OK` only for `rc == 0`.
- RED: `tests/test_wave32_probe_honesty.py::test_nonzero_exit_logs_stderr_tail` (writable —
  `tests/test_job65_zero_yield_gate.py:111` already calls `_probe_phase1_discovery` directly).
- Blast: **none** (verified — no test pins the "OK" phrasing; the three probe test files monkeypatch
  the once-helper, not the message). A1 is safer than the plan's rev-1 claim.

**W32-A2 `[SAFE]` Probe: SessionLog `[PROBE]` row** (D-A1/A3 + the cache question)
- Change: after every smoke probe, `_log_event_row` a `[PROBE]` row: rc, stderr tail, candidate chain
  as resolved (primary/alt/post-F17), owned-output file list, and whether the probe-cache was
  consulted + the entry's age. Additive; existing SessionLog readers consume it unchanged. Also
  records when output detection found 0 files vs >1 (covers D-A2's blind spot diagnostically).
- RED: `tests/test_wave32_probe_honesty.py::test_probe_writes_sessionlog_row`.

**W32-A3 `[SAFE]` Truthful wall-clock payload** (D-W2)
- Change: `_invoke_agent_with_timeout` returns the actual waited time in the payload (`_error` keeps
  the literal phrase "wall-clock timeout" — all five consumers are substring `in` matches at
  `graph.py:2445, 5933, 5990, 7413, 8436`, and `tests/test_wave22_writer_timeout_accounting.py:71`
  pins the source phrase — plus `_waited_s` with actual seconds); sync twin uses its `_waited`
  (`graph.py:2563/2572`), **async twin (`:2344`) has no `_waited` and must derive it from its `t0`
  (`:2351`)**. Consumers print both numbers: "wall-clock timeout after 1800s base (thread stopped
  at 2700s)".
- RED: `tests/test_wave32_writer_telemetry.py::test_timeout_payload_carries_waited`.
- Blast: `tests/test_wave22_writer_timeout_accounting.py`, `webapp/tests/test_wave30_writer_window.py`.

**W32-A4 `[SAFE]` File-tool args summaries carry position** (D-W10)
- Change: `graph.py:1793-1794` — `read_file` summary gains `line=/num_lines=`; `edit_file` summary
  gains a 60-char prefix of `old_string`. Makes the W30-6 trail and ToolCallLog able to prove
  read-spirals.
- RED: `tests/test_wave32_writer_telemetry.py::test_read_file_summary_has_position`.
- Blast: none known; add to list if RED surfaces pins.

**W32-A5 `[SAFE]` Render-gate override names its arm + counts** (D-A6, telemetry half)
- Change: `browser_service/server.py:2552-2559` — the override `status_note` includes which arm
  satisfied the gate (`wait_for_hit` / `anchors` / `jsonld` / `body_len`) and the probe counts.
  No behavior change yet (B1 changes the gate).
- **Testability prerequisite (critique finding 5 + round-2 finding B3):** `server.py` imports
  `fastapi`/`pydantic` at module level and those are NOT in `webapp/requirements.txt` — a test
  importing it dies with `ModuleNotFoundError`, never an assertion failure. **Worse:
  `browser_service/__init__.py:1` is `from .server import app`, so even
  `import browser_service.render_gate` executes the package `__init__` and dies before any
  assertion.** Therefore B1/A5 first **extract** `_render_gate_satisfied`
  (`server.py:2297-2307`; pure data in, pure bool out — constants `_RENDER_GATE_MIN_ANCHORS=25`
  `:2289`, `_RENDER_GATE_MIN_BODY=20_000` `:2290`) and the note-builder into a dependency-free
  `browser_service/render_gate.py` imported by `server.py` (pure refactor, no behavior change),
  and both items test THAT module — loaded in tests via
  `importlib.util.spec_from_file_location` on the file path (the `test_f1_orphan_killer.py`
  dodge), NOT via the package import. Making `__init__.py` import-lazy was considered and
  rejected: a prod packaging change for a test-env convenience.
- RED: `tests/test_wave32_render_gate.py::test_override_note_names_arm` (loads `render_gate.py`
  by file path, never imports `browser_service`).

### Tier B — Stop feeding the loop false-clean evidence

**W32-B1 `[SAFE]` Render-gate honesty** (D-A6)
- Change (in `browser_service/render_gate.py` after A5's extraction):
  - (a) `wait_for_hit` alone can no longer satisfy the gate — it becomes **conjunctive**:
    `wait_for_hit AND (anchors≥25 OR jsonld≥1 OR has_price OR body_len≥20k)`. The crocs-class
    429-with-real-DOM case keeps passing via anchors/jsonld (wave-17 S15: 892KB listing DOM).
  - (b) the body arm measures `document.body.innerText.length` (not `innerHTML`), so a script-only
    challenge shell fails it. (`body_len` has exactly one consumer — `server.py:2305` — so the
    switch is contained; verified.)
  - (c) the override note carries the satisfaction arm + counts (A5).
- RED (three, per critique finding 8):
  `tests/test_wave32_render_gate.py::test_wait_for_hit_alone_does_not_satisfy`,
  `::test_script_shell_body_fails_text_length`,
  `::test_image_grid_with_waitfor_and_no_content_arms_still_overrides` (the residual regression
  class: real page, waited selector attached, but anchors<25, no jsonld, no price, innerText<20k —
  the override MUST still fire via the conjunctive arm's wait_for_hit + ... — pinned explicitly).
- Blast: `tests/test_f1_orphan_killer.py` greps `server.py` as TEXT (unaffected by the extraction);
  `tests/test_probe_ladder_full_rungs.py`, `tests/test_wave21_probe_not_found_ladder.py` exercise
  the ladder end-to-end.
- **Deploy-order note:** browser_service image deploys AFTER django+celery (standing rule).

**W32-B2 `[SAFE]` Probe-cache: give it a way to be wrong** (D-A7, narrowed by critique)
- Change: `probe_tools.py` — when the ladder records `akamai_count > 0` without a successful rung,
  write `needs_akamai_bypass=True` into the domain's cache entry (the missing negative-direction
  write; `_save_probe_cache` currently early-returns on `not data.get("success")` at `:152-153`,
  and the akamai hook points exist at `:374/:464-465/:709`). **Field scope (round-2 finding C2):
  the negative write updates `needs_akamai_bypass` ONLY** — it must never touch `method`
  (`_get_cached_method` reads both fields, `:139-142`; a naive `update_or_create` with
  `defaults={"method": ...}` would clobber a previously-learned good method on the domain).
  Wave-21 T5's rule (never cache captcha) is untouched — akamai-bypass-required is not captcha.
- Explicitly DROPPED from rev-1 (critique finding 1 — mechanism does not exist): "no re-save on
  cache-hit" (`cached_at` is `auto_now_add`; hits touch only `last_used_at`, which expiry ignores)
  and "cached_at of last verified success". The ≤4h stale window is **bounded and documented**, not
  fixed: A2's `[PROBE]` row records cache age so the next RCA can see it.
- RED: `tests/test_wave32_probe_cache_truth.py::test_ladder_akamai_writes_needs_bypass`.
- Blast: `tests/test_probe_page_cap.py` (cache interactions); wave-21 T5 tests must stay green
  (assert no captcha write — unaffected).

**W32-B3 `[SAFE]` Host gate at intake AND restart — declines exactly what F17 would drop** (D-INT)
- Change:
  - **Comparator = the pipeline's own registrable-domain comparison**
    (`webapp/agents/nodes/run_execution.py:2010-2032`, `_registrable_of`),
    promoted to a shared helper (pure refactor; `normalize_host` in `src/seed_urls.py:41` is
    full-host equality — wrong tool, critique finding 3: `shop.marimekko.com` vs `marimekko.com`
    must be ACCEPTED, matching F17).
  - **Django intake** (`views.py:2908-2918`): when `nav_method ∈ ("listing", "search")` (the
    intake vocabulary — critique finding 2; `list_page`/`search_term` are the DERIVED input_mode)
    and a provided listing/search URL's registrable domain ≠ the job url's → **422** inline form
    error, no force escape (the pipeline drops it today; declining is strictly honest).
  - **Partner API** (`webapp/scraper/api/writers.py:65` region): same check keyed on
    `input_mode ∈ ("list_page", "search_term")` → `ApiError(422, "host_mismatch",
    details={offending urls})`, shape consistent with `writers.py:77`.
  - **job_restart POST override** (`views.py:652-668`, critique finding 4 — the LIVE hole: intake
    Re-run posts the edited config, so a fresh cross-host `search_criteria` enters here and bypasses
    any intake-only gate): apply the same check when the POST explicitly provides
    `search_criteria`/`search_url`; reject with the AJAX error JSON the endpoint already speaks.
    Field-inherited values (no POST key) are NOT re-checked — a job clean at creation stays clean.
  - **Docs (same commit, user's no-silent-contract-changes gate):** `docs/specs/sync_api.yaml` 422
    documentation on POST /api/v1/jobs + error-code enumeration;
    `docs/extractor-builder-api-guide.md:39` error-code inventory + §2 request-body row;
    spec-drift tests updated in the same commit.
- RED: `tests/test_wave32_intake_host_gate.py::test_cross_host_listing_declined_at_intake`,
  `::test_same_registrable_different_host_accepted` (e.g. `shop.marimekko.com` listing on a
  `marimekko.com` job — MUST pass),
  `::test_partner_listing_urls_same_contract`,
  `::test_restart_post_override_rejects_cross_host_criteria`,
  `::test_restart_inherited_criteria_not_rechecked`.
- Blast: `tests/test_api_create.py` (validation cases), intake view tests, wave-31 dedupe tests
  (`site_host` in `dedupe.py` untouched — different comparator, different purpose).

### Tier C — Terminate on evidence, not on count

**W32-C1 `[RISKY]` Inconclusive-with-evidence routes to the force-FAIL arm** (D-A3 + D-A1's tail)
- Change: `graph.py:7573` region — when `probe_yield is None` AND the probe's rc≠0 (from A1/A2's
  recorded outcome) with a stderr marker naming a missing discovery source
  (`No --query, --category-url, or --listing-url provided`), route into the EXISTING crashed
  force-FAIL arm (`:7576-7623`) with feedback_for_writer naming the missing discovery source.
- Audit (DONE by critique finding 10): only `templates/navigation_scraper.py:1006` and
  `templates/http_navigation_scraper.py:1582` emit the marker, both `sys.exit(1)`. Other
  nonzero-no-traceback shapes stay inconclusive (unchanged).
- **Retry-axes note (pinned):** both the listing retry and tier escalation in
  `_probe_phase1_discovery` are gated on `not crashed` — routing no-source into the crashed
  arm disables both, which is CORRECT (a missing CLI source is transport-independent; today
  `probe_yield=None` already returns False from `_probe_retry_warranted`, so nothing is lost).
- RED: `tests/test_wave32_inconclusive_termination.py::test_no_source_exit_forces_fail_with_feedback`,
  `::test_no_source_exit_performs_zero_retry_attempts` (pins the retry-axes note),
  `::test_inconclusive_without_evidence_stays_noop` (the narrowing guard is itself pinned).
- Blast: `tests/test_job85_zero_yield_gates.py`, `tests/test_job65_zero_yield_gate.py`,
  `tests/test_wave19_tier_escalation_probe.py`, `tests/test_wave30_smoke_probe.py`.

**W32-C2 `[SAFE]` Evidence-based remap denial — provenance-guarded** (D-B1)
- Change: `route_after_testing.py:2215-2241` — deny the remap arm ONLY when the failing field's
  mapping carries `tested: "empty"` **AND** `render_provenance == "full"` on that field entry
  (critique finding 9: `field_verification.py:303-312` is deterministic and wave-22's fail-closed
  gate guarantees `"empty"` implies full-render provenance AT WRITE TIME — but the verdict can be
  STALE relative to the fresh sample URLs the remap handoff targets, so provenance is required,
  not just the `tested` flag). Routing: **`field_confirmation`** with the honest reason
  ("field X has no live source on the rendered page — re-mapping cannot create one"). Rationale
  vs `human_approval`: field_confirmation is the existing coverage-gap interrupt (same family as
  validate_coverage), it auto-acknowledges under local `skip_approvals` (e2e-friendlier) and maps
  to the same approval UX in prod; `MAX_REMAPS` exhaustion's auto-FAIL semantics are unchanged for
  every other path. A mapping failure with NO `tested` evidence keeps today's behavior.
- RED: `tests/test_wave32_remap_denial.py::test_dead_source_full_provenance_denies_remap`,
  `::test_empty_without_full_provenance_still_remaps` (the stale-verdict guard),
  `::test_untested_mapping_still_remaps`.
- Blast: `tests/test_wave24_cascade_honesty.py`, `tests/test_wave26_cascade_honesty.py`,
  `tests/test_wave21_remap_sample_swap.py`.

**W32-C3 `[SAFE]` Fingerprint field/fields repair** (D-B3)
- Change: `route_after_testing.py:1332` and `src/writer_memory.py:123` —
  `rem.get("field") or ",".join(sorted(rem.get("fields") or []))`. Restores wave-22 B3 grace and
  wave-29 B5 exact/degraded matching for multi-field verdicts.
- RED: `tests/test_wave32_fingerprint_fields.py::test_multi_field_fingerprint_matches`.
- Blast: `tests/test_wave22_remediation_grace.py`, `tests/test_wave29_writer_memory.py` (hex fixtures regenerate).

### Tier D — Writer economics (make the window spendable; stop rewarding bloat)

**W32-D1 `[SAFE]` Writer budget counted in rounds — co-designed with D5** (D-W1)
- Change: `graph.py:1044-1050` — `"code_writer": _WRITER_ROUNDS * 3 + 2` with
  `_WRITER_ROUNDS = 50` (≈50 tool rounds; was ≈40), comment rewritten to document the
  pre_model_hook 3-super-steps/round tax and that the wall backstop is 1800/2700 (not 900).
- **Pairing honesty (round-2 finding B4):** D5 ships DORMANT (kill-switch default `"full"`), so
  32-b's live configuration is **~50 rounds against an unbounded full embed** — exactly the shape
  rev-1's pairing rule warned about. That is accepted deliberately, with compensating observers
  live in the same group: D4's `[DRAFT-BLOAT]` row makes growth-without-convergence visible, D3
  stops step-deaths destroying the last good draft, and C1 terminates the loop the rounds would
  otherwise feed. **The marimekko replay (§5.4) validates this exact configuration.** W25-e 25e-b
  then activates the diet (E2a flip), at which point the round raise and the embed shrink land
  together. Gating the numeric raise on the kill-switch was considered and rejected — a recursion
  count that depends on an embed mode is hidden coupling; the replay + observers are the control.
- RED: `tests/test_wave32_writer_rounds.py::test_writer_recursion_derives_from_rounds`.
- Blast: **none** (round-2 finding C5 — verified: zero tests pin `AGENT_RECURSION_MAP["code_writer"]`
  anywhere; the RED test itself becomes the pin).

**W32-D2 `[SAFE]` Extension visible to the tool deadline** (D-W3)
- Change: `graph.py:2516` — for phases in `_ACTIVITY_EXTENDABLE_PHASES`, stamp
  `set_tool_deadline(now + cap)` (the extension cap) instead of the base, so `run_scraper`'s honesty
  guard stops refusing browser verification in the 1800→2700s zone. (Parent-side refresh is NOT
  viable — ContextVar is copied at thread start; verified by the writer RCA.)
- **Gate mirror (round-2 finding B2):** the stamp condition must mirror the existing extension gate
  at `graph.py:2558-2566` — `phase in _ACTIVITY_EXTENDABLE_PHASES AND allow_activity_extension` —
  so the draft-finisher window (`allow_activity_extension=False`, fixed `WRITER_FINISH_TIMEOUT`) is
  NOT stamped with a 2700s tool deadline it can never use; that would delay the disarm latch in
  exactly the salvage window W30-2 protects.
- **Trade-off note (critique finding 12):** tools get the cap even when no extension ever fires,
  and an in-flight tool survives abandonment (the disarm latch affects only the NEXT call) —
  accepted; the latch itself still fires (pinned by test).
- RED: `tests/test_wave32_writer_rounds.py::test_extendable_phase_tool_deadline_uses_cap`,
  `::test_disarm_latch_still_fires`.
- Blast: `webapp/tests/test_wave30_writer_window.py` (path corrected from rev-1), job-81 guard tests.

**W32-D3 `[SAFE]` Known-good draft freeze — BOTH restore paths** (D-W7)
  NOT parse and a `-good` key exists → restore it into the workspace before `_fix_scraper_syntax`;
  (c) `draft_safety.restore_job_draft` prefers `-good` when latest is unparseable; **(d) the SECOND
  restore path — `webapp/agents/nodes/setup_workspace.py:244-259` (watchdog re-drive, same FM key,
  currently no parse gate) — gets the same `-good` preference** (critique finding 6). **Out of
  scope, named (round-2 finding C4):** the third restore site `setup_workspace.py:262-275`
  (skip_code_generation branch) restores the PROMOTED `scrapers/{slug}/scraper.py` — a tested,
  finalized artifact — so it stays gate-less by design. Do NOT
  restore on parseable-but-untested drafts (would discard real accepted fixes).
- RED: `tests/test_wave32_draft_freeze.py::test_broken_draft_does_not_clobber_good_key`,
  `::test_step_death_restores_good_draft`,
  `::test_setup_workspace_restores_good_draft` (the twin).
- Blast: `tests/test_draft_corruption_gates.py`, tester-belt restore tests (`graph.py:7135-7152`),
  setup_workspace tests.

**W32-D4 `[SAFE]` Negative-progress tripwire (log-only)** (D-W8)
- Change: `graph.py:6120-6155` region — after a fix cycle under a FAIL verdict, if the draft grew
  >10% AND no new fields passed testing, emit a SessionLog `[DRAFT-BLOAT]` row (sizes, verdict).
  Log-only this wave. Escalation is a follow-up once the row exists on real jobs.
- RED: `tests/test_wave32_draft_freeze.py::test_growth_under_fail_logs_bloat_row`.

**W32-D5 `[SAFE]` Minimal edit-over-write embed diet — reshaped per round-2 adjudication** (D-W4's
minimal core, pulled forward on critique finding 7 so D1 is safe; the FULL redesign — AST draft map,
deterministic pre-seeding, budget accounting, activation — remains W25-e)
- Change: **new pure module `webapp/agents/draft_context.py`** (no Django imports) exposing
  `render_writer_embed(code, *, mode="full"|"bounded") -> str`, plus an `embed_mode=` kwarg on
  `create_code_writer`/`_build_agent` (`subagents.py:785-786, :975-1002, :1029`). **full** =
  today's `_embed_template` output byte-for-byte (`_embed_template` becomes the full-mode renderer;
  its own tests stay green). **bounded** = the same 6-line frame + do-not-read note + head 20K +
  tail 20K (mirroring `filesystem_tools.py:44-45` constants; new settings
  `CODE_WRITER_EMBED_FULL_MAX_CHARS=40_000`, `CODE_WRITER_EMBED_HEAD=20_000`,
  `CODE_WRITER_EMBED_TAIL=20_000`) + the pointer note "middle elided — full file at
  workspace/{slug}/scraper_draft.py; use search_content / read_file(line=)". No AST map in this wave
  (W25-e E1 adds it to the same module). **Wiring (round-2 finding B1):** `graph.py:5781` (main,
  inside `_invoke_code_writer` where `_eow_active` is in scope) passes `embed_mode="bounded"` iff
  `_eow_active and len(code) >= 40_000`; `graph.py:5470` (finisher — `_run_draft_finisher` has NO
  `_eow_active` in scope, and it is definitionally a post-death window) passes it iff
  `len(code) >= 40_000` alone; fixers inherit (same agent object). **Kill-switch
  `CODE_WRITER_EMBED_MODE`** read lazily via `getattr(settings, ...)` at call time — **default
  `"full"`, i.e. D5 ships dormant**: behavior is byte-identical to today until W25-e E2a flips the
  default behind its e2e gate. If the draft does not parse (587's exact state), degrade to raw
  head+tail — never crash the embed. D4's `[DRAFT-BLOAT]` row observes the effect once E2a activates.
- RED: `tests/test_wave32_eow_diet.py::test_small_draft_keeps_full_embed`,
  `::test_large_draft_gets_head_tail_with_pointer`,
  `::test_unparseable_draft_degrades_to_raw_head_tail`,
  `::test_full_mode_output_matches_legacy_embed_template` (byte-equal vs `_embed_template`),
  `::test_embed_mode_kill_switch_lazy_read` (**mechanics only** — asserts the lazy read honors an
  env/override at call time; it must NOT pin the default value, which is E2a's assertion to own),
  `::test_first_cycle_still_full_embed` (EOW-condition boundary, main call site),
  `::test_finisher_conditions_on_size_alone` (finisher call site).
- Blast: `tests/test_wave24_writer_prompt.py` — **two distinct pins (round-2 finding B5):**
  `:32-41` direct-call assertions on `_embed_template` stay green (it remains the full-mode
  renderer); `:50-53` source-pins `_build_agent`'s body to contain
  `_embed_template(system_prompt, template_code)` and NOT route elsewhere — **that assertion is
  amended in the same commit** to pin the new dispatch (full mode still calls `_embed_template`
  internally). Also `tests/test_draft_first_nudge.py`, `tests/test_draft_corruption_gates.py`.
- Rollback: delete the kwarg plumbed default (call sites stop passing `embed_mode=`) — with the
  default `"full"` there is no behavior change to roll back; the module itself is inert.

### Tier E — Chain parity (the three listing chains must agree)

**W32-E1 `[SAFE]` F17 fall-through + tester-tool guard** (D-A4 / D-N4b)
- Change: (a) `graph.py:6727-6754` — the probe candidate chain applies F17 to EACH candidate and
  falls through to `_alt` when `_primary` is dropped (mirrors
  `webapp/agents/nodes/run_execution.py:815-826`'s loop,
  restoring the probe's own C2 mirror contract); (b) `shell_tools.py:570-598` — the tester/
  `run_scraper` env chain applies the same F17 registrable-domain guard before injecting
  `SCRAPER_LISTING_URL`, so no chain can inject a cross-host listing into a draft env.
- RED: `tests/test_wave32_chain_parity.py::test_probe_falls_through_after_f17_drop`,
  `::test_tester_tool_never_injects_cross_host_listing`.
- Blast: `tests/test_wave19_probe_env_identity.py`, `tests/test_probe_listing_retry.py`,
  `tests/test_job77_execution_listing_fallback.py`.

---

## 4. Explicitly deferred (named, not forgotten)

| Item | Why deferred | Revisit when |
|------|-------------|--------------|
| D-N2 seed poisoning (url_examples from not-reached traversal) | With B3+E1+C1 the not-reached traversal no longer produces a live loop; widest test blast (seed contract, embedded-json model) | After wave-32 e2e confirms loop is dead |
| D-N3 PDP-as-listing fallback rewrite | Critique-verified: E1a makes the fallback USEFUL (it supplies the fall-through candidate C1 case (b) needs for its fast zero-yield verdict); rewriting it would remove that candidate | Own mini-wave, after observing new fallback behavior |
| D-N4a/D-N5 registrable-domain RESUME keying + host-level artifacts | Cross-cutting behavioral change (W31 intake is host-keyed, pipeline URL-keyed); B3 now uses the registrable COMPARATOR but does not change resume keying | Dedicated wave-33 candidate |
| D-N1 traverse stop reasons (rev-1's A6 — **CUT from this wave**) | Critique finding 13: not in 587's causal chain (RC-3 is the PROBE's log; the traversal fallback is a different channel and played no role). Cheap but scope creep | Group 32-c / wave-33 backlog |
| D-W5 step-death finisher arm | High value but RISKY (finisher must not inherit the same budget+embed or it dies identically); D3 removes the worst consequence (broken-draft snapshot) this wave | Wave-33, after D3 evidence accumulates |
| D-B2 cycle diversity (nothing makes cycle 2 differ from cycle 1) | Partially addressed by C2 (the pointless cycles stop); full B4-gating is prompt surgery | Observe post-wave-32 remap behavior |
| D-B4 analyzer-evidence channel stripping (`examples`/`tested` dropped from writer summary) | Prompt-contract change with wide writer-behavior blast | Own item with playground A/B |
| D-A2 `probe_output_*` browser round-trip | Diagnosed by A2's row; the round-trip fix touches browser_service contract | With next browser_service wave |
| Probe-cache ≤4h stale-clean window | Bounded (entries die 4h after INSERT — critique finding 1); A2's [PROBE] row now records cache age for diagnosis; B2 gives the cache its negative verdict | If e2e/prod shows stale windows still causing misdrives |

## 5. Sequencing, validation, rollout

1. **Commit group 32-a** (diagnose + evidence): A1, A2, A3, A4, A5 (+render_gate.py extraction),
   B1, B2, B3, E1 — all `[SAFE]`.
2. **Commit group 32-b** (terminate + economics): C1 (RISKY; audit DONE — two templates emit the
   marker, both exit 1), C2, C3, D1+D5 (paired, one unit), D2, D3, D4.
3. Per item: RED → GREEN → full named blast-radius set. Then `ruff check . ../src/` (from
   `/app/webapp`), full suites (root + `webapp/`), pre-existing-failure list diffed against the
   known 3 (truncation non-seed-cap, admin visibility, oversized-offset). **Any NEW failure blocks
   the goal (user's no-screw-ups gate).**
4. **Local e2e (validation gates):**
   - marimekko PDP + same-host listing drive (the 587 replay, corrected shape): COMPLETED with real
     priced products, or honest FAIL FAST at tester #1 with `[PROBE]` row — the 2.3h walk must be
     gone. Goal requires the COMPLETED arm. **Round-2 finding B8: this replay runs with NO
     `CODE_WRITER_EMBED_MODE` export** — it proves D5 dormancy and validates the 50-round + full-embed
     configuration D1's pairing honesty describes. The bounded-embed drive is W25-e §7.4's separate gate.
   - cross-host listing submission: clean 422 at intake AND at restart-override.
   - one clean url_list drive: COMPLETED (happy path + B3/E1 non-interference regression).
   - one known-Akamai site drive: render-gate override surfaces the true antibot verdict; crocs-class
     sites must still pass via the anchors/jsonld arms (conjunctive-arm fixture covers the boundary).
   - wave-31 duplicate-guard behavior unregressed (dedupe tests green).
5. **Rollout (post wake-up, user-gated):** EB sync + PR per standing rules AFTER user review; prod
   deploy order django+celery → browser_service (B1). Nothing ships to prod tonight.
6. Standing constraints unchanged: never fetch target sites directly; work through prod API only;
   TDD throughout (no production code without a watched-failing test); config/proxy.json never
   committed.

## 6. Open questions

1. 587's `parent_job`/`origin_job` — confirms 586 carried the poison and by which entry path.
   B3 now covers BOTH paths (intake + restart override), so this only affects the narrative.
2. Prod env knobs `WRITER_INVOKE_TIMEOUT` / `WRITER_MAX_TIMEOUT` / `WRITER_ACTIVITY_FRESH_S` —
   explicit or default? Affects D1 sizing (the 1800.0s float implies base=1800 via
   `_effective_timeout` either way).
3. The 123,907-byte final draft on the FM — which template it inherited (http_navigation's 85,850B
   base would confirm template-inherited bloat; the seed never embeds the 16 URLs).
4. Whether the provider prompt-caches the ~145K system prompt — determines how much of W25-e's win
   is latency vs rounds (A2-adjacent input-size logging will settle it on local drives).
5. Was the probe cache even consulted during 587's tester runs? (A2's row makes this answerable
   next time; unverifiable retroactively from prod logs.)
